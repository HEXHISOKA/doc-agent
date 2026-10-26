"""在 HF 采购/到货样本上跑完整链路，并用 gold 算字段级准确率。

与 probe_formats.py 的区别：
    那个只回答"会不会崩、拦不拦"，这些图**没有真值**，说不出"抽对没抽对"。
    这批数据带字段级 gold（words + bbox 重建），所以能算准确率，
    而且数据集的生成侧保证过算术自洽（行小计、税、合计都对），
    于是「校验层报 err」在这里可以直接读成**误报**——这是上一版测不出来的东西。

字段映射（gold 是层级命名，与我们 schema 不同）：
    number                  -> doc_no
    issue_date              -> order_date
    parties.vendor.name     -> supplier      (采购订单里的供应商)
    parties.supplier.name   -> supplier      (收货单里的供应商)
    totals.total            -> total_amount
    items.N.description     -> items[N].name
    items.N.line_total      -> items[N].amount

用法：
    python src/eval_hf.py                     # 全部
    python src/eval_hf.py --only purchase     # 只跑采购订单
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cloud import CloudClient  # noqa: E402
from eval import SCHEMA_HINT, SYSTEM_A, parse_json  # noqa: E402
from layout import rebuild, to_markdown_table  # noqa: E402
from ocr import run_ocr_array  # noqa: E402
from preprocess import decode_image, suppress_red  # noqa: E402
from validate import is_ok, validate  # noqa: E402

PUBLIC_CONFIG = {
    "endpoint": "https://your-app.example.com",
    "publishableKey": "wbpk_PLACEHOLDER_REPLACE_ME",
}
MODEL = "deepseek-v4.1-flash"

MONTHS = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


# ---------------------------------------------------------------- 归一化比较
def parse_date(s: str | None) -> set[date]:
    """解析成**候选集合**而不是单个日期。

    为什么：`11.05.2025` 在法语单据里是 11 月 5 日、在英语单据里是 5 月 11 日，
    单看字符串无法判定。原版强行二选一，结果把模型正确的输出判成了错
    （6 张里 5 张 order_date 被误判）——这是**测试装置的错，不是被测对象的错**。
    改为返回所有合理解读，与模型输出有交集即算对。
    """
    if not s:
        return set()
    t = re.sub(r"\s+", " ", str(s).strip().lower())
    cands: set[date] = set()

    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)          # 2025-04-09
    if m:
        try:
            cands.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    m = re.search(r"(?<!\d)(\d{1,2})\s*[/.]\s*(\d{1,2})\s*[/.]\s*(\d{4})", t)  # 11.05.2025
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        for d, mo in ((a, b), (b, a)):     # 欧式 DD.MM 与美式 MM/DD 都收
            if 1 <= mo <= 12 and 1 <= d <= 31:
                try:
                    cands.add(date(y, mo, d))
                except ValueError:
                    pass
    m = re.search(r"(\d{1,2})\s*([a-zéûôà]+)\s*(\d{4})", t)             # 9 avril 2025
    if m and m.group(2) in MONTHS:
        try:
            cands.add(date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))))
        except ValueError:
            pass
    return cands


def parse_num(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s)
    t = re.sub(r"[€$¥£\s\u00a0]", "", t)
    t = re.sub(r"(?i)(eur|usd|cny|rmb|ht|ttc|tva)", "", t)
    if not re.search(r"\d", t):
        return None
    neg = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("-()")
    # 欧式 1.234,56 / 英式 1,234.56 的判别：最后出现的分隔符决定小数位
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") \
            else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if len(t.split(",")[-1]) <= 2 else t.replace(",", "")
    v = re.search(r"[-+]?\d*\.?\d+", t)
    if not v:
        return None
    try:
        x = float(v.group())
    except ValueError:
        return None
    return -x if neg else x


def eq_text(a, b) -> bool:
    """文本比较：去空白、大小写、常见标点。OCR 分词差异不该算错。"""
    def k(s) -> str:
        s = str(s or "").lower()
        s = re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)
        return s
    return bool(k(a)) and k(a) == k(b)


def eq_num(a, b) -> bool:
    x, y = parse_num(a), parse_num(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= max(0.02, abs(y) * 1e-4)


def eq_date(a, b) -> bool:
    x, y = parse_date(a), parse_date(b)
    return bool(x and y and (x & y))


# ---------------------------------------------------------------- 链路
def run_one(client: CloudClient, path: Path, rec: dict) -> dict:
    t0 = time.time()
    out: dict = {"name": rec["name"], "type": rec["type"], "profile": rec["profile"],
                 "errors": []}
    try:
        bgr = decode_image(path.read_bytes())
        bgr, _ = suppress_red(bgr)
        items = run_ocr_array(bgr)
        out["ocr_n"] = len(items)
        confs = [float(i.get("score") or 0) for i in items]
        out["ocr_conf"] = round(sum(confs) / len(confs), 4) if confs else 0.0
        text = to_markdown_table(rebuild(items))
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"ocr: {type(exc).__name__}: {exc}")
        out["crash"] = "ocr"
        return out
    out["text_len"] = len(text)

    user = (f"单据 OCR 文本如下：\n\n{text}\n\n请按以下 schema 输出 JSON：\n{SCHEMA_HINT}")
    try:
        t = time.time()
        raw, usage = client.chat(
            [{"role": "system", "content": SYSTEM_A}, {"role": "user", "content": user}],
            model=MODEL, temperature=0.0, max_tokens=1600, timeout=120.0, json_mode=True)
        out["llm_ms"] = int((time.time() - t) * 1000)
        out["tokens"] = usage.get("total_tokens") or usage
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"llm: {type(exc).__name__}: {exc}")
        out["crash"] = "llm"
        return out

    data = parse_json(raw)
    if data is None:
        out["crash"] = "json_bad"
        out["errors"].append(f"json: {raw[:160]!r}")
        return out
    out["extract"] = data

    iss = validate(data)
    out["verdict"] = "通过" if is_ok(iss) else "拦截"
    out["issues"] = [{"level": i["level"], "layer": i["layer"], "field": i["field"],
                      "msg": i["msg"][:150]} for i in iss]
    out["total_ms"] = int((time.time() - t0) * 1000)
    return out


# ---------------------------------------------------------------- 对 gold
def compare(extract: dict, gold: dict) -> list[dict]:
    rows: list[dict] = []

    def add(field: str, gk: str | None, got, kind: str) -> None:
        if not gk or gk not in gold or gold[gk] in (None, ""):
            return
        g = gold[gk]
        ok = eq_num(got, g) if kind == "num" else eq_date(got, g) if kind == "date" \
            else eq_text(got, g)
        rows.append({"field": field, "gold": g, "got": got, "ok": ok, "kind": kind})

    is_grn = "goods_received" in json.dumps(gold) or "parties.supplier.name" in gold
    add("doc_no", "number", extract.get("doc_no"), "text")
    add("supplier", "parties.supplier.name" if is_grn else "parties.vendor.name",
        extract.get("supplier"), "text")
    if not is_grn:
        add("order_date", "issue_date", extract.get("order_date"), "date")
        add("total_amount", "totals.total", extract.get("total_amount"), "num")
    # 收货单不比 order_date / total_amount —— 不是放水，是这两个字段在两种单据里
    # 语义根本不同：
    #   · 收货单上有两个日期（收货日与「订单日期」），gold 的 issue_date 是**收货日**，
    #     而我们 schema 的 order_date 是**下单日**。模型抽到下单日反而是对的，
    #     拿它去比收货日只会把对的判成错（6 张里错判了 4 张）。
    #   · 收货单没有金额，只有 订购/实收/拒收/验收 四个数量汇总，
    #     逼它填 total_amount 会导致「把 1650 件当成 1650 元」。
    # 这两条本身就是结论：**一套 schema 灌所有单据类型会静默产生语义错误。**

    got_items = extract.get("items") or []
    n_gold = len([k for k in gold if re.fullmatch(r"items\.\d+\.description", k)])
    for i in range(n_gold):
        g = got_items[i] if i < len(got_items) else {}
        add(f"items[{i}].name", f"items.{i}.description", g.get("name"), "text")
        add(f"items[{i}].amount", f"items.{i}.line_total", g.get("amount"), "num")
    if len(got_items) != n_gold:
        rows.append({"field": "items.count", "gold": n_gold, "got": len(got_items),
                     "ok": False, "kind": "count"})
    return rows


def report(recs: list[dict]) -> None:
    print("\n" + "=" * 100)
    print(f"{'样本':44s} {'判定':6s} {'字段':>7s} {'OCR':>5s}  错在")
    print("=" * 100)
    for r in recs:
        bad = [x["field"] for x in r["rows"] if not x["ok"]]
        print(f"{r['name'][:43]:44s} {str(r.get('verdict') or r.get('crash')):6s} "
              f"{r.get('hit', 0):>3d}/{r.get('total', 0):<3d} {r.get('ocr_n') or 0:>5d}  "
              f"{','.join(bad)[:44]}")
    H = sum(r.get("hit", 0) for r in recs)
    T = sum(r.get("total", 0) for r in recs)
    block = sum(1 for r in recs if r.get("verdict") == "拦截")
    print("-" * 100)
    print(f"字段准确率 {H}/{T} = {H / T * 100:.1f}%   拦截 {block}/{len(recs)} 张")
    for r in recs:
        if r.get("errors"):
            print(f"  ! {r['name']}: {r['errors'][0][:120]}")


def recount(files: list[str]) -> None:
    """拿已存的抽取结果重算，不花模型调用。

    规则改了（校验判定）或评测口径改了（字段映射、日期解析）之后，
    没必要把 12 张图重新过一遍 OCR + LLM——那既慢又引入新的不确定性。
    """
    recs: list[dict] = []
    for f in files:
        for r in json.loads(Path(f).read_text(encoding="utf-8")):
            d = r.get("extract")
            # 重跑校验：verdict 是旧规则算的，必须按当前 validate 重判
            if d:
                iss = validate(d)
                r["verdict"] = "通过" if is_ok(iss) else "拦截"
                r["issues"] = [{"level": i["level"], "layer": i["layer"], "field": i["field"],
                                "msg": i["msg"][:150]} for i in iss]
                r["rows"] = compare(d, r["gold"])
                r["hit"], r["total"] = sum(1 for x in r["rows"] if x["ok"]), len(r["rows"])
            recs.append(r)
    report(recs)
    out = ROOT / "out" / "hf_eval_all.json"
    out.write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已合并写入 {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--out", default=str(ROOT / "out" / "hf_eval.json"))
    ap.add_argument("--recount", nargs="*", default=None,
                    help="不重跑模型，只用已存结果按当前规则重算")
    args = ap.parse_args()

    if args.recount:
        recount(args.recount)
        return

    manifest = json.loads((ROOT / "samples" / "hf" / "manifest.json").read_text(encoding="utf-8"))
    if args.only:
        manifest = [m for m in manifest if args.only in m["type"]]
    client = CloudClient(PUBLIC_CONFIG["endpoint"], PUBLIC_CONFIG["publishableKey"])

    recs = []
    for n, m in enumerate(manifest, 1):
        p = ROOT / m["file"]
        print(f"[{n}/{len(manifest)}] {m['name']} …", flush=True)
        r = run_one(client, p, m)
        r["gold"] = m["gold"]
        r["degradation"] = m["degradation"]
        r["rows"] = compare(r.get("extract") or {}, m["gold"]) if r.get("extract") else []
        hit = sum(1 for x in r["rows"] if x["ok"])
        r["hit"], r["total"] = hit, len(r["rows"])
        recs.append(r)
        print(f"    {r.get('verdict') or r.get('crash')}  字段 {hit}/{len(r['rows'])}  "
              f"ocr={r.get('ocr_n')}  {r.get('total_ms')}ms")

    (Path(args.out)).write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 100)
    print(f"{'样本':44s} {'判定':6s} {'字段':>7s} {'OCR':>5s}  错在")
    print("=" * 100)
    for r in recs:
        bad = [x["field"] for x in r["rows"] if not x["ok"]]
        print(f"{r['name'][:43]:44s} {str(r.get('verdict') or r.get('crash')):6s} "
              f"{r['hit']:>3d}/{r['total']:<3d} {r.get('ocr_n') or 0:>5d}  "
              f"{','.join(bad)[:44]}")
    H = sum(r["hit"] for r in recs)
    T = sum(r["total"] for r in recs)
    block = sum(1 for r in recs if r.get("verdict") == "拦截")
    print("-" * 100)
    print(f"字段准确率 {H}/{T} = {H / T * 100:.1f}%   "
          f"拦截 {block}/{len(recs)} 张（这些单的算术自洽，拦截即误报）")
    for r in recs:
        if r["errors"]:
            print(f"  ! {r['name']}: {r['errors'][0][:120]}")


if __name__ == "__main__":
    main()
