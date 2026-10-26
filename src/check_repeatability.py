"""同一张图重复跑的稳定性测试。

为什么这个指标比"准确率"更该先测：
    系统对外的承诺是"同一张图连跑三次结果逐字节相同"（OCR 留在后端就是为了
    可复现）。如果抽取层每次都给出不同的表，那么"校验通过"就是一次偶然——
    同一张单再审一次可能就被拦下，人工复核的结论也无从复现。
    换句话说：**不稳定的系统，准确率数字没有意义**，因为它不可复现。

做法：图只解码/OCR 一次（这一步本来就是确定的），模型抽取跑 N 次，
逐字段比对。字段取值完全一致记为稳定；同一字段出现两种以上取值记为漂移。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cloud import CloudClient  # noqa: E402
from eval import SCHEMA_HINT, SYSTEM_A, parse_json  # noqa: E402
from layout import rebuild, to_markdown_table  # noqa: E402
from ocr import run_ocr_array  # noqa: E402
from preprocess import decode_image, suppress_red  # noqa: E402

PUBLIC_CONFIG = {
    "endpoint": "https://your-app.example.com",
    "publishableKey": "wbpk_PLACEHOLDER_REPLACE_ME",
}
MODEL = "deepseek-v4.1-flash"

CASES = ["cn_delivery_note", "fapiao_standard", "en_packing_list", "photo_po_bahamut"]
FIELDS = ["doc_no", "supplier", "order_date", "total_amount"]


def ocr_text(path: Path) -> str:
    bgr = decode_image(path.read_bytes())
    bgr, _ = suppress_red(bgr)
    return to_markdown_table(rebuild(run_ocr_array(bgr)))


def extract(client: CloudClient, text: str) -> dict | None:
    user = f"单据 OCR 文本如下：\n\n{text}\n\n请按以下 schema 输出 JSON：\n{SCHEMA_HINT}"
    out, _ = client.chat(
        [{"role": "system", "content": SYSTEM_A}, {"role": "user", "content": user}],
        model=MODEL, temperature=0.0, max_tokens=1600, timeout=120.0, json_mode=True)
    return parse_json(out)


def sig(data: dict) -> tuple:
    """整表的指纹：明细行数 + 每行(名称,数量,单价,金额)。用于判断"表本身变没变"。"""
    rows = []
    for it in (data.get("items") or []):
        rows.append((str(it.get("name")), it.get("qty"), it.get("price"), it.get("amount")))
    return tuple(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "out" / "repeatability.json"))
    args = ap.parse_args()

    client = CloudClient(PUBLIC_CONFIG["endpoint"], PUBLIC_CONFIG["publishableKey"])
    roots = [ROOT / "samples" / "variants", ROOT / "samples" / "web" / "cn",
             ROOT / "samples" / "web"]
    result = {}

    for name in CASES:
        path = next((p for r in roots for p in r.rglob(name + ".*") if p.is_file()), None)
        if path is None:
            print(f"跳过 {name}：找不到文件")
            continue
        text = ocr_text(path)
        runs = []
        for k in range(args.n):
            d = extract(client, text)
            if d is None:
                runs.append(None)
                continue
            runs.append(d)
            print(f"  {name} #{k + 1}: doc_no={d.get('doc_no')} total={d.get('total_amount')} "
                  f"items={len(d.get('items') or [])}", flush=True)

        ok_runs = [r for r in runs if r]
        per_field = {}
        for f in FIELDS:
            vals = [json.dumps(r.get(f), ensure_ascii=False) for r in ok_runs]
            per_field[f] = {"distinct": len(set(vals)), "values": list(dict.fromkeys(vals))}
        sigs = [sig(r) for r in ok_runs]
        sig_cnt = Counter(sigs)
        top_sig, top_n = (sig_cnt.most_common(1)[0] if sig_cnt else ((), 0))
        result[name] = {
            "runs": len(runs), "parsed": len(ok_runs),
            "fields": per_field,
            "table_distinct": len(sig_cnt),
            "table_stable_ratio": round(top_n / len(ok_runs), 3) if ok_runs else 0.0,
            "items_n": [len(r.get("items") or []) for r in ok_runs],
            "_runs_raw": ok_runs,
        }

    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 92)
    print(f"{'单据':22s} {'解析':>4s} {'表稳定率':>8s} {'明细行数':>14s}  字段取值数(doc_no/supplier/date/total)")
    print("=" * 92)
    for name, r in result.items():
        fv = "/".join(str(r["fields"][f]["distinct"]) for f in FIELDS)
        print(f"{name[:21]:22s} {r['parsed']:>4d} {r['table_stable_ratio']:>8.2f} "
              f"{str(r['items_n']):>14s}  {fv}")
    print("\n字段取值数 = 该字段在多次运行中出现了几种不同取值；1 表示稳定，>1 表示漂移")


if __name__ == "__main__":
    main()
