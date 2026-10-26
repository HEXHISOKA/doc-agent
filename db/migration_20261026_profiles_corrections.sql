-- 数据库迁移记录（2026-10-26）：新增 doc_no_profiles + corrections + 有效版本视图
-- 背景：
--   ① 编号归一上线后，"只询问一次、之后记住"需要一个持久档案（工厂级）。
--   ③ append-only 主表无法纠错，按会计红字冲销原则建 corrections 独立哈希链。
-- 本文件为 DDL 原文存档，与线上已执行内容一致。回滚方法见文件末尾。

-- ============================================================ ② doc_no_profiles
-- 工厂级编号格式档案。source: manual(实施方直接给样例) | confirmed(人工确认入库时沉淀)。
-- examples 为 JSONB 数组，每条是一个被确认过的标准编号写法。
CREATE TABLE IF NOT EXISTS doc_no_profiles (
  id BIGSERIAL PRIMARY KEY,
  factory_key TEXT NOT NULL DEFAULT 'default' UNIQUE,
  owner_id TEXT DEFAULT auth.uid(),
  examples JSONB NOT NULL DEFAULT '[]'::jsonb,
  source TEXT NOT NULL DEFAULT 'confirmed',
  confirmed_by TEXT,
  confirmed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT doc_no_profiles_source_ok CHECK (source IN ('manual','confirmed'))
);
ALTER TABLE doc_no_profiles ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT, UPDATE ON doc_no_profiles TO anon, authenticated, service_role;
CREATE POLICY doc_no_profiles_read_all  ON doc_no_profiles FOR SELECT USING (true);
CREATE POLICY doc_no_profiles_insert_ok ON doc_no_profiles FOR INSERT
  WITH CHECK ((owner_id IS NULL) OR (owner_id = auth.uid()));
CREATE POLICY doc_no_profiles_update_ok ON doc_no_profiles FOR UPDATE
  USING ((owner_id IS NULL) OR (owner_id = auth.uid()))
  WITH CHECK ((owner_id IS NULL) OR (owner_id = auth.uid()));

-- ============================================================ ③ corrections
-- 更正记录（红字冲销）：原 doc_parses 行一字不动，纠错 = 追加一条更正。
-- 独立哈希链：自己的 seq/prev_hash/record_hash 序列，advisory lock 用 918273646
--（主链 doc_parses 用 918273645，互不干扰）。
CREATE TABLE IF NOT EXISTS corrections (
  id BIGSERIAL PRIMARY KEY,
  owner_id TEXT DEFAULT auth.uid(),
  seq BIGINT DEFAULT 0,
  prev_hash TEXT DEFAULT ''::text,
  record_hash TEXT DEFAULT ''::text,
  corrects_id BIGINT NOT NULL REFERENCES doc_parses(id),
  field TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT NOT NULL,
  reason TEXT,
  corrected_by TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT corrections_field_ok CHECK (
    field IN ('doc_no','supplier','order_date','total_amount')
    OR field ~ '^items\\[[0-9]+\\]\\.(name|qty|price|amount)$')
);
ALTER TABLE corrections ENABLE ROW LEVEL SECURITY;
GRANT SELECT, INSERT ON corrections TO anon, authenticated, service_role;
CREATE POLICY corrections_read_all   ON corrections FOR SELECT USING (true);
CREATE POLICY corrections_insert_ok  ON corrections FOR INSERT
  WITH CHECK ((owner_id IS NULL) OR (owner_id = auth.uid()));

CREATE OR REPLACE FUNCTION corrections_chain_fn() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
  last_hash TEXT;
  last_seq  BIGINT;
  payload   TEXT;
BEGIN
  PERFORM pg_advisory_xact_lock(918273646);
  SELECT record_hash, seq INTO last_hash, last_seq
    FROM corrections ORDER BY id DESC LIMIT 1;
  NEW.prev_hash := COALESCE(last_hash, repeat('0', 64));
  NEW.seq := COALESCE(last_seq, 0) + 1;
  payload := NEW.prev_hash || '|' || NEW.seq::text || '|'
          || NEW.corrects_id::text || '|'
          || COALESCE(NEW.field, '') || '|'
          || COALESCE(NEW.old_value, '') || '|'
          || COALESCE(NEW.new_value, '') || '|'
          || COALESCE(NEW.reason, '') || '|'
          || COALESCE(NEW.corrected_by, '') || '|'
          || COALESCE(NEW.created_at::text, '');
  NEW.record_hash := encode(sha256(convert_to(payload, 'UTF8')), 'hex');
  RETURN NEW;
END $fn$;

CREATE TRIGGER corrections_chain BEFORE INSERT ON corrections
  FOR EACH ROW EXECUTE FUNCTION corrections_chain_fn();

-- 验签：与主链 verify_chain 同一套结构，只扫 corrections 自己的链。
CREATE OR REPLACE FUNCTION verify_corrections_chain()
 RETURNS TABLE(is_ok boolean, total_rows bigint, broken_id bigint, message text)
 LANGUAGE plpgsql SECURITY DEFINER AS $fn$
DECLARE
  r RECORD;
  expect_prev TEXT := repeat('0', 64);
  expect_seq  BIGINT := 1;
  calc     TEXT;
  payload  TEXT;
  cnt      BIGINT := 0;
BEGIN
  FOR r IN SELECT * FROM corrections ORDER BY id LOOP
    cnt := cnt + 1;
    IF r.prev_hash <> expect_prev THEN
      RETURN QUERY SELECT false, cnt, r.id, 'prev_hash 与前一条更正不匹配（更正链前序被改动或删除）'::text;
      RETURN;
    END IF;
    IF r.seq <> expect_seq THEN
      RETURN QUERY SELECT false, cnt, r.id, 'seq 不连续（更正链中间有记录被删除）'::text;
      RETURN;
    END IF;
    payload := r.prev_hash || '|' || r.seq::text || '|'
            || r.corrects_id::text || '|'
            || COALESCE(r.field, '') || '|'
            || COALESCE(r.old_value, '') || '|'
            || COALESCE(r.new_value, '') || '|'
            || COALESCE(r.reason, '') || '|'
            || COALESCE(r.corrected_by, '') || '|'
            || COALESCE(r.created_at::text, '');
    calc := encode(sha256(convert_to(payload, 'UTF8')), 'hex');
    IF calc <> r.record_hash THEN
      RETURN QUERY SELECT false, cnt, r.id, 'record_hash 不匹配（更正内容被改动）'::text;
      RETURN;
    END IF;
    expect_prev := r.record_hash;
    expect_seq  := expect_seq + 1;
  END LOOP;
  RETURN QUERY SELECT true, cnt, NULL::bigint, '更正链完整，全部更正可验证'::text;
END $fn$;
GRANT EXECUTE ON FUNCTION verify_corrections_chain() TO anon, authenticated, service_role;

-- ============================================================ 有效版本视图
-- 业务查询"现在值是多少"走这个视图（自动取每字段最新更正后的值）；
-- 审计对账看 doc_parses 原表 + corrections 全量。原记录一字不动。
CREATE OR REPLACE VIEW v_doc_parses_effective AS
SELECT
  p.id, p.seq, p.filename, p.owner_id, p.validate_ok, p.record_hash, p.created_at,
  COALESCE(
    (SELECT c.new_value FROM corrections c
      WHERE c.corrects_id = p.id AND c.field = 'doc_no' ORDER BY c.id DESC LIMIT 1),
    p.doc_no) AS doc_no,
  COALESCE(
    (SELECT c.new_value FROM corrections c
      WHERE c.corrects_id = p.id AND c.field = 'supplier' ORDER BY c.id DESC LIMIT 1),
    p.supplier) AS supplier,
  COALESCE(
    (SELECT c.new_value FROM corrections c
      WHERE c.corrects_id = p.id AND c.field = 'order_date' ORDER BY c.id DESC LIMIT 1),
    p.order_date) AS order_date,
  COALESCE(
    (SELECT c.new_value::numeric FROM corrections c
      WHERE c.corrects_id = p.id AND c.field = 'total_amount'
        AND c.new_value ~ '^-?[0-9]+(\.[0-9]+)?$' ORDER BY c.id DESC LIMIT 1),
    p.total_amount) AS total_amount,
  (SELECT count(*) FROM corrections c WHERE c.corrects_id = p.id) AS correction_count,
  p.extract_json
FROM doc_parses p;
GRANT SELECT ON v_doc_parses_effective TO anon, authenticated, service_role;

-- ============================================================ 回滚方法（按需，倒序执行）
-- DROP VIEW IF EXISTS v_doc_parses_effective;
-- DROP FUNCTION IF EXISTS verify_corrections_chain();
-- DROP TRIGGER IF EXISTS corrections_chain ON corrections;
-- DROP FUNCTION IF EXISTS corrections_chain_fn();
-- DROP TABLE IF EXISTS corrections;
-- DROP TABLE IF EXISTS doc_no_profiles;
