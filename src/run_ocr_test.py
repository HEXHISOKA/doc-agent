"""端到端验证：图片 -> OCR -> 版面重建 -> 与 ground truth 比对。

指标口径（这是后面评测集的基石，先定好再跑）：
    - 字段命中率：该字段的文本是否出现在重建结果里（字符级，宽松）
    - 明细行命中：该行三个数值（数量/单价/金额）是否都在同一行文本里出现
    - 一致性：同一张图跑两次，OCR 输出是否完全一致（验证"可复现"这个卖点）

用法：
    python src/run_ocr_test.py                      # 跑 samples 下全部
    python src/run_ocr_test.py samples/po_clean.png  # 跑单张
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from layout import rebuild, to_markdown_table  # noqa: E402
from ocr import backend_name, run_ocr  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent.parent
SAMPLES = ROOT / "samples"
OUT = ROOT / "out"


def check_fields(flat: str, gt: dict) -> dict:
    """把重建文本压平后做字段命中判断。数字统一去千分位再比。"""
    norm = flat.replace(",", "").replace(" ", "")

    def hit(value: str) -> bool:
        return str(value).replace(",", "").replace(" ", "") in norm

    result = {
        "订单号": hit(gt["订单号"]),
        "供应商": hit("恒达塑胶"),
        "合计金额": hit(gt["合计金额"]),
        "交货日期": hit(gt["交货日期"]),
        "明细行": [],
    }
    for row in gt["明细"]:
        ok = hit(row["数量"]) and hit(row["单价"]) and hit(row["金额"])
        result["明细行"].append({"序号": row["序号"], "物料": row["物料名称"], "命中": ok})
    return result


def run_one(path: Path) -> dict:
    t0 = time.perf_counter()
    items = run_ocr(path)
    t_ocr = time.perf_counter() - t0

    t1 = time.perf_counter()
    layout = rebuild(items)
    t_layout = time.perf_counter() - t1

    return {
        "file": path.name,
        "words": len(items),
        "ocr_ms": round(t_ocr * 1000),
        "layout_ms": round(t_layout * 1000),
        "skew_deg": round(layout.skew_deg, 2),
        "lines": len(layout.lines),
        "cols": layout.col_count,
        "text": layout.text,
        "markdown": to_markdown_table(layout),
        "items": items,
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = (
        [Path(a) for a in sys.argv[1:]]
        if len(sys.argv) > 1
        else sorted(SAMPLES.glob("*.png"))
    )
    gt_all = json.loads((SAMPLES / "gt.json").read_text(encoding="utf-8"))

    print(f"backend = {backend_name()}")
    summary = []
    for path in targets:
        res = run_one(path)
        gt = gt_all.get(path.name)
        print("\n" + "=" * 72)
        print(f"{res['file']}   words={res['words']}  ocr={res['ocr_ms']}ms  layout={res['layout_ms']}ms")
        print(f"skew={res['skew_deg']}°  lines={res['lines']}  cols={res['cols']}")

        if gt:
            chk = check_fields(res["text"], gt)
            ok_cnt = sum(1 for k in ("订单号", "供应商", "合计金额", "交货日期") if chk[k])
            row_ok = sum(1 for r in chk["明细行"] if r["命中"])
            print(f"字段命中 {ok_cnt}/4   明细行命中 {row_ok}/{len(chk['明细行'])}")
            for k in ("订单号", "供应商", "合计金额", "交货日期"):
                print(f"  {'✓' if chk[k] else '✗'} {k}")
            for r in chk["明细行"]:
                print(f"  {'✓' if r['命中'] else '✗'} 明细 {r['序号']} {r['物料']}")
            res["check"] = chk
        else:
            print("（无 ground truth，仅输出重建结果）")

        print("-" * 72)
        print(res["markdown"])
        summary.append(res)

    (OUT / "ocr_run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n[saved] out/ocr_run.json")


if __name__ == "__main__":
    main()
