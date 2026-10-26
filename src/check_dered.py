"""去红章改动的回归验证：同一批图，走/不走预处理，比较 GT 命中与行结构。

为什么要单独跑一遍：`suppress_red` 会**修改图像像素**，这是全链路里唯一
"有破坏力"的一步。改坏了不会报错，只会静默丢字。所以必须用 20 张带真值的
评测集做前后对照，确认：① 干净图（d0）不受影响；② 有章图（d2/d3）不变差。

指标（都不依赖大模型，纯 OCR 输出，可复现）：
    gt_hit   —— 真值 token 出现在 OCR 文本里的比例（订单号/供应商/合计/9 个明细格）
    cells_ok —— 明细行的 cell 数是否等于 4 或 5（缺序号列算 4）
               污染行的 cell 数会变成 6~8，这是"碎片混入"的直接度量

用法：
    python check_dered.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

from layout import rebuild  # noqa: E402
from ocr import run_ocr_array  # noqa: E402
from preprocess import decode_image, suppress_red  # noqa: E402

ROOT = Path(__file__).parent.parent


def tokens(gt: dict) -> list[str]:
    out = [gt["doc_no"], gt["supplier"], gt["total_amount"]]
    for it in gt["items"]:
        out += [it["数量"], it["单价"], it["金额"]]
    return out


def norm(s: str) -> str:
    """宽松匹配：去掉千分位/空格，全角数字归一，避免因格式差异误判丢字。"""
    table = str.maketrans("０１２３４５６７８９．，", "0123456789.,")
    return s.translate(table).replace(",", "").replace(" ", "")


def run_one(path: Path, use_pre: bool) -> dict:
    bgr = decode_image(path.read_bytes())
    meta = {}
    if use_pre:
        bgr, meta = suppress_red(bgr)
    res = rebuild(run_ocr_array(bgr))

    text = norm(" ".join(l.as_text() for l in res.lines))
    gt = GT[path.name]
    toks = [norm(t) for t in tokens(gt)]
    hit = sum(1 for t in toks if t in text)

    # 明细行 = 含任一物料名的行
    names = [it["物料名称"] for it in gt["items"]]
    detail = [l for l in res.lines if any(n in l.as_text() for n in names)]
    cells = [len(l.cells) for l in detail]
    cells_ok = sum(1 for c in cells if 4 <= c <= 5)

    return {
        "hit": hit,
        "total": len(toks),
        "cells": cells,
        "cells_ok": cells_ok,
        "n_detail": len(detail),
        "red": meta.get("red_ratio"),
        "dropped": len(res.dropped),
    }


def main() -> None:
    global GT
    setdir = ROOT / "eval" / "set"
    GT = json.loads((setdir / "gt.json").read_text(encoding="utf-8"))
    files = sorted(GT)

    rows = []
    for i, fn in enumerate(files, 1):
        p = setdir / fn
        a = run_one(p, use_pre=False)
        b = run_one(p, use_pre=True)
        d = b["hit"] - a["hit"]
        flag = "" if d >= 0 else "  <<< 变差"
        print(
            f"[{i:2d}/{len(files)}] {fn:30s} d{GT[fn]['dirt']} "
            f"GT {a['hit']:2d}->{b['hit']:2d}/{a['total']}  "
            f"明细cell {a['cells']}->{b['cells']}  red={b['red']:.4f}{flag}"
        )
        rows.append((fn, a, b))

    tot_a = sum(r[1]["hit"] for r in rows)
    tot_b = sum(r[2]["hit"] for r in rows)
    tot_t = sum(r[1]["total"] for r in rows)
    ok_a = sum(r[1]["cells_ok"] for r in rows)
    ok_b = sum(r[2]["cells_ok"] for r in rows)
    nd = sum(r[1]["n_detail"] for r in rows)
    worse = [r[0] for r in rows if r[2]["hit"] < r[1]["hit"]]

    print("\n" + "=" * 72)
    print(f"GT token 命中      : {tot_a}/{tot_t} -> {tot_b}/{tot_t}  ({tot_b / tot_t:.1%})")
    print(f"明细行结构正确     : {ok_a}/{nd} -> {ok_b}/{nd}")
    print(f"变差的用例         : {worse or '无'}")
    print("=" * 72)


if __name__ == "__main__":
    main()
