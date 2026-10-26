-- doc_parses 升级前备份（2026-10-25 20:00）
-- 用途：本次只改 schema（加 hash_ver 列）与两个函数体，不动任何行数据。
-- 这份文件保存「改动前」的 DDL 原文，万一要回滚可以直接照抄执行。
-- 数据完整性以 db_upgrade_backup.json 里的 (id, seq, record_hash) 清单为准：
--   升级后逐行比对，三者全同 = 一行数据都没动。
--
-- 回滚方法（按需）：
--   ALTER TABLE public.doc_parses DROP COLUMN IF EXISTS hash_ver;
--   然后执行本文件下方的两个 CREATE OR REPLACE FUNCTION 原文。

-- ============================================================ 改动前：trigger

CREATE OR REPLACE FUNCTION public.doc_parses_chain_fn()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
  last_hash TEXT;
  last_seq  BIGINT;
  payload   TEXT;
BEGIN
  PERFORM pg_advisory_xact_lock(918273645);
  SELECT record_hash, seq INTO last_hash, last_seq
    FROM doc_parses ORDER BY id DESC LIMIT 1;
  NEW.prev_hash := COALESCE(last_hash, repeat('0', 64));
  NEW.seq := COALESCE(last_seq, 0) + 1;
  payload := NEW.prev_hash || '|' || NEW.seq::text || '|'
          || COALESCE(NEW.filename, '') || '|'
          || COALESCE(NEW.doc_no, '') || '|'
          || COALESCE(NEW.supplier, '') || '|'
          || COALESCE(NEW.order_date, '') || '|'
          || COALESCE(NEW.total_amount::text, '') || '|'
          || COALESCE(NEW.item_count::text, '') || '|'
          || COALESCE(NEW.extract_json::text, '') || '|'
          || COALESCE(NEW.validate_ok::text, '') || '|'
          || COALESCE(NEW.model_id, '');
  NEW.record_hash := encode(sha256(convert_to(payload, 'UTF8')), 'hex');
  RETURN NEW;
END $function$;

-- ============================================================ 改动前：verify

CREATE OR REPLACE FUNCTION public.verify_chain()
 RETURNS TABLE(is_ok boolean, total_rows bigint, broken_id bigint, message text)
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
  r RECORD;
  expect_prev TEXT := repeat('0', 64);
  expect_seq  BIGINT := 1;
  calc     TEXT;
  payload  TEXT;
  cnt      BIGINT := 0;
BEGIN
  FOR r IN SELECT * FROM doc_parses ORDER BY id LOOP
    cnt := cnt + 1;
    IF r.prev_hash <> expect_prev THEN
      RETURN QUERY SELECT false, cnt, r.id, 'prev_hash 与前一条记录不匹配（前序记录被改动或被删除）'::text;
      RETURN;
    END IF;
    IF r.seq <> expect_seq THEN
      RETURN QUERY SELECT false, cnt, r.id, 'seq 不连续（中间有记录被删除）'::text;
      RETURN;
    END IF;
    payload := r.prev_hash || '|' || r.seq::text || '|'
            || COALESCE(r.filename, '') || '|'
            || COALESCE(r.doc_no, '') || '|'
            || COALESCE(r.supplier, '') || '|'
            || COALESCE(r.order_date, '') || '|'
            || COALESCE(r.total_amount::text, '') || '|'
            || COALESCE(r.item_count::text, '') || '|'
            || COALESCE(r.extract_json::text, '') || '|'
            || COALESCE(r.validate_ok::text, '') || '|'
            || COALESCE(r.model_id, '');
    calc := encode(sha256(convert_to(payload, 'UTF8')), 'hex');
    IF calc <> r.record_hash THEN
      RETURN QUERY SELECT false, cnt, r.id, 'record_hash 不匹配（本条数据内容被改动）'::text;
      RETURN;
    END IF;
    expect_prev := r.record_hash;
    expect_seq  := expect_seq + 1;
  END LOOP;
  RETURN QUERY SELECT true, cnt, NULL::bigint, '链完整，全部记录可验证'::text;
END $function$;
