"""多格式单据稳健性探针。

目的不是算准确率（这些图没有标注真值），而是回答一个更基础的问题：
    **换一种版式、换一种语言、换一种单据类型，整条链路会不会崩？**

对每张图依次跑：
    解码 → OCR → 版面重建 → 模型抽取 → 三层校验
每一步都单独捕获异常：稳健性的第一道门槛是"不崩"，不是"抽得准"。

判定口径（不依赖真值）：
    crash      任一步抛异常（最严重）
    ocr_empty  识别不到任何文字
    json_bad   模型输出解析不出 JSON
    fields     五个业务字段各抽到几个（doc_no/supplier/order_date/total_amount/items）
    issues     校验给出的疑点数量与层级分布

用法：
    python src/probe_formats.py [--dir samples/web] [--limit N] [--only 关键字]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cloud import CloudClient  # noqa: E402
from eval import SCHEMA_HINT, SYSTEM_A, parse_json  # noqa: E402
from layout import rebuild, to_markdown_table  # noqa: E402
from ocr import backend_name, run_ocr_array  # noqa: E402
from preprocess import decode_image, suppress_red  # noqa: E402
from validate import validate as validate_extract  # noqa: E402

# 与 app.py 同一套云通道配置（线上前端走的就是这个通道）
PUBLIC_CONFIG = {
    "endpoint": "https://your-app.example.com",
    "publishableKey": "wbpk_PLACEHOLDER_REPLACE_ME",
}
MODEL = "deepseek-v4.1-flash"
EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
FIELDS = ["doc_no", "supplier", "order_date", "total_amount"]


def first_frame(path: Path):
    """解码必须与线上同款（decode_image，优先 cv2）。

    早先用 PIL 直接解码，遇到截断 JPEG 会抛 OSError，于是把"探针自己解不开"
    报成了"服务崩溃"——这是拿测试装置的问题去指控被测对象。线上走 cv2，
    截断图多半仍能解出一部分，结论完全不同。
    """
    return decode_image(path.read_bytes())


def probe(client: CloudClient, path: Path) -> dict:
    rec: dict = {"file": str(path.relative_to(ROOT)), "name": path.stem,
                 "size": path.stat().st_size, "stages": {}, "errors": []}
    t0 = time.time()

    # ① 解码（与 /api/ocr 同一条路径、同一个函数）
    try:
        bgr = first_frame(path)
        rec["wh"] = [int(bgr.shape[1]), int(bgr.shape[0])]
        rec["stages"]["decode"] = True
    except Exception as exc:  # noqa: BLE001
        rec["errors"].append(f"decode: {type(exc).__name__}: {exc}")
        rec["crash"] = "decode"
        return rec

    # ② OCR（先去红章，与 /api/ocr 的顺序一致：红色油墨必须在 OCR 之前去掉，
    #    否则章上的公司名会混进正文。第一版探针漏了这步，把"对账单 supplier 被
    #    印章污染"记成了模型缺陷，其实是探针少吃了一道预处理）
    try:
        t = time.time()
        bgr, pre_meta = suppress_red(bgr)
        rec["red_suppressed"] = pre_meta
        rec["stages"]["preprocess"] = True
    except Exception as exc:  # noqa: BLE001
        rec["errors"].append(f"preprocess: {type(exc).__name__}: {exc}")
        # 去红章失败不阻断：退回原图继续，这也是线上应有的降级

    try:
        t = time.time()
        items = run_ocr_array(bgr)
        rec["ocr_ms"] = int((time.time() - t) * 1000)
        rec["ocr_n"] = len(items)
        rec["ocr_backend"] = backend_name()
        confs = [float(i.get("score") or 0) for i in items]
        rec["ocr_conf_avg"] = round(sum(confs) / len(confs), 4) if confs else 0.0
        rec["stages"]["ocr"] = True
    except Exception as exc:  # noqa: BLE001
        rec["errors"].append(f"ocr: {type(exc).__name__}: {exc}")
        rec["crash"] = "ocr"
        return rec

    # ③ 版面重建
    text = ""
    try:
        res = rebuild(items)
        text = to_markdown_table(res)
        rec["text_len"] = len(text)
        rec["tables"] = len(getattr(res, "tables", []) or [])
        rec["stages"]["layout"] = True
    except Exception as exc:  # noqa: BLE001
        rec["errors"].append(f"layout: {type(exc).__name__}: {exc}")
        # 版面重建失败不致命：降级为纯文本继续，这本身就是要验证的降级路径
        text = "\n".join(str(i.get("text") or "") for i in items)
        rec["degraded_to_plain_text"] = True

    if not text.strip():
        rec["crash"] = "ocr_empty"
        rec["errors"].append("OCR 未识别出任何文字")
        return rec
    rec["text_head"] = text[:900]

    # ④ 模型抽取
    user = (f"单据 OCR 文本如下：\n\n{text}\n\n"
            f"请按以下 schema 输出 JSON：\n{SCHEMA_HINT}")
    try:
        t = time.time()
        out, usage = client.chat(
            [{"role": "system", "content": SYSTEM_A}, {"role": "user", "content": user}],
            model=MODEL, temperature=0.0, max_tokens=1600, timeout=120.0, json_mode=True,
        )
        rec["llm_ms"] = int((time.time() - t) * 1000)
        rec["llm_tokens"] = usage.get("total_tokens") or usage
    except Exception as exc:  # noqa: BLE001
        rec["errors"].append(f"llm: {type(exc).__name__}: {exc}")
        rec["crash"] = "llm"
        return rec

    data = parse_json(out)
    if data is None:
        rec["crash"] = "json_bad"
        rec["errors"].append(f"模型输出无法解析为 JSON: {out[:200]!r}")
        rec["raw_out"] = out[:800]
        return rec
    rec["stages"]["extract"] = True
    rec["data"] = data

    got = [f for f in FIELDS if data.get(f) not in (None, "", [])]
    rec["fields_got"] = got
    rec["fields_n"] = len(got) + (1 if data.get("items") else 0)
    rec["items_n"] = len(data.get("items") or [])

    # ⑤ 校验
    try:
        issues = validate_extract(data)
        rec["issues"] = len(issues)
        rec["issue_layers"] = sorted({i.get("layer") for i in issues})
        rec["issue_fields"] = [i.get("field") for i in issues][:8]
        rec["stages"]["validate"] = True
    except Exception as exc:  # noqa: BLE001
        rec["errors"].append(f"validate: {type(exc).__name__}: {exc}\n{traceback.format_exc()[-500:]}")
        rec["crash"] = "validate"

    rec["total_ms"] = int((time.time() - t0) * 1000)
    return rec


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "samples" / "web"))
    ap.add_argument("--out", default=str(ROOT / "out" / "formats_probe.json"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--exclude", default=r"^(po_|risk_|review_|confuse)",
                    help="默认跳过原有 PO 回归集，那个由 eval.py 负责")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_absolute():
        root = ROOT / root
    root = root.resolve()
    files = sorted(p for p in root.rglob("*")
                   if p.suffix.lower() in EXTS and p.is_file())
    if args.only:
        files = [p for p in files if args.only in p.name]
    if args.exclude:
        import re as _re

        pat = _re.compile(args.exclude)
        files = [p for p in files if not pat.match(p.name)]
    if args.limit:
        files = files[:args.limit]
    print(f"共 {len(files)} 张")

    client = CloudClient(PUBLIC_CONFIG["endpoint"], PUBLIC_CONFIG["publishableKey"])
    recs = []
    for i, p in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {p.name} ...", flush=True)
        r = probe(client, p)
        recs.append(r)
        tag = r.get("crash") or "ok"
        print(f"    -> {tag}  ocr_n={r.get('ocr_n')} fields={r.get('fields_got')} "
              f"items={r.get('items_n')} issues={r.get('issues')}", flush=True)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 96)
    print(f"{'文件':38s} {'结果':10s} {'字数':>5s} {'字段':>3s} {'明细':>3s} {'疑点':>3s} {'ms':>6s}")
    print("=" * 96)
    for r in recs:
        print(f"{r['name'][:37]:38s} {str(r.get('crash') or 'ok'):10s} "
              f"{r.get('ocr_n') or 0:>5d} {r.get('fields_n') or 0:>3d} "
              f"{r.get('items_n') or 0:>3d} {r.get('issues') or 0:>3d} {r.get('total_ms') or 0:>6d}")
    ok = sum(1 for r in recs if not r.get("crash"))
    print(f"\n不崩: {ok}/{len(recs)}")
    for r in recs:
        if r.get("errors"):
            print(f"  ! {r['name']}: {r['errors'][0][:160]}")


if __name__ == "__main__":
    main()
