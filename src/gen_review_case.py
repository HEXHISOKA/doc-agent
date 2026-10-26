"""生成"看图复核"演示样本：让 OCR 在供应商名上错一个字，而整张图仍然可读。

为什么需要专门造一张
--------------------
评测集里那张误放行（po_13_plain_B_d3）在当前构建下已经读对了——OCR 改进之后，
自然错误不再复现，拿它现场演示会"演不出来"。演示样本必须**稳定复现**。

做法
----
在供应商名中某一个字上压一条很浅的**扫描划痕**（浅灰细线）。选这个手法而不是模糊，
理由是实测对比过：
    - 高斯模糊要大到 2.6 半径才能让线上 OCR 出错，但那个强度下模型自己也说"看不清"，
      复核就没意义了；
    - 一条 5px 宽的浅灰划痕就能让 OCR 把「塑」读成「望」，而模型仍能读出「塑」，
      因为它靠上下文补全，OCR 的字符切分则被划痕打断了。

⚠️ OCR 构建不同（Python 版本 / rapidocr 版本）阈值会漂移：本地 3.13 环境要更宽的划痕，
线上 3.11 环境 5px 即可。换环境后请重跑本脚本确认，不要假设。

用法
----
    python src/gen_review_case.py                # 逐档扫描并保存可用的一档
    python src/gen_review_case.py --width 5      # 直接指定划痕宽度
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))
from gen_samples import MARGIN, SUPPLIER, draw_base  # noqa: E402
from layout import rebuild  # noqa: E402
from ocr import run_ocr_array  # noqa: E402
from preprocess import suppress_red  # noqa: E402
from validate import edit_distance, _norm_name  # noqa: E402

# 供应商名里第 5 个字（"塑"）的大致坐标：左边距 + "供应商：" 4 字 + 前 4 个字
_CHAR_W = 24
_NAME_X0 = MARGIN + 4 * _CHAR_W
_TARGET_IDX = 4
_STREAK_X = _NAME_X0 + _TARGET_IDX * _CHAR_W + 10
_STREAK_Y = (MARGIN + 176, MARGIN + 210)


def add_scratch(img: Image.Image, x: int, width: int, gray: int = 250) -> Image.Image:
    """在指定位置画一条浅灰斜线，模拟扫描划痕。"""
    out = img.copy()
    d = ImageDraw.Draw(out)
    y0, y1 = _STREAK_Y
    d.line([(x, y0), (x - 3, y1)], fill=(gray, gray, gray), width=width)
    return out


def ocr_supplier(img: Image.Image) -> str:
    bgr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()
    clean, _ = suppress_red(bgr)
    lay = rebuild(run_ocr_array(clean))
    for line in (lay.text or "").splitlines():
        if "供应商" in line or "供方" in line:
            line = line.split("|")[0]          # 同一行右侧还有日期列，先截断
            for sep in ("：", ":"):
                if sep in line:
                    return line.split(sep, 1)[1].strip()
    return ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "samples/review_supplier.png"))
    ap.add_argument("--width", type=int, default=0, help="0 = 自动扫描 2~7px")
    args = ap.parse_args()

    base = draw_base("clean")
    truth = _norm_name(SUPPLIER)
    print(f"真值：{SUPPLIER}\n")

    widths = [args.width] if args.width else list(range(2, 8))
    chosen = None
    for w in widths:
        img = add_scratch(base, _STREAK_X, w)
        got = ocr_supplier(img)
        dist = edit_distance(truth, _norm_name(got)) if got else 99
        mark = "  ← 可用" if 1 <= dist <= 2 else ""
        print(f"  划痕 {w}px: 距离 {dist:2d} | OCR 读到「{got}」{mark}")
        if 1 <= dist <= 2 and chosen is None:
            chosen = (w, img, got)

    if not chosen:
        print("\n没有找到合适的一档：把 --width 调大再试，或换环境后重跑。")
        return

    w, img, got = chosen
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    meta = {
        "file": out.name,
        "truth_supplier": SUPPLIER,
        "ocr_supplier": got,
        "scratch_width_px": w,
        "note": "供应商名中一个字上压了浅色扫描划痕；OCR 读错，模型仍可读。用于演示看图复核。",
    }
    Path(str(out).replace(".png", ".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已保存 {out}（划痕 {w}px），OCR 读成「{got}」")


if __name__ == "__main__":
    main()
