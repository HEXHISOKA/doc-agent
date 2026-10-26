"""第二轮实测：找出「OCR 之后仍然可见」的数字异常信号。

第一轮已证明形近字符（O/0、l/1）会被 OCR 归一、文本层完全无痕，
所以文本层检测必须换成别的判据。这里测三类候选：

  A. 全角数字（１００）—— OCR 是否保留全角
  B. 千分位 / 小数点异常（1.200.00、100.000、1,20,00）
  C. 逗号后带空格（'5, 000.00'，第一轮 case1 已偶然出现）

再加一个影子字段场景：同一张图两个合计，OCR 是否两个都读到。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from layout import rebuild
from ocr import run_ocr_array

MONO = "C:/Windows/Fonts/consola.ttf"
SANS = "C:/Windows/Fonts/arial.ttf"
CJK = "C:/Windows/Fonts/msyh.ttc"


def draw(lines, w=1000, pad=32):
    h = pad * 2 + sum(sz + 20 for _, sz, _ in lines)
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    y = pad
    for text, size, fp in lines:
        d.text((pad, y), text, font=ImageFont.truetype(fp, size), fill=(25, 25, 25))
        y += size + 20
    return img


CASES = [
    ("A 全角数字", [
        ("单价 １００.００　数量 50", 34, CJK),
        ("金额：５０００.００", 34, CJK),
    ]),
    ("B1 千分位点号欧洲式 1.200.00", [
        ("金额 1.200.00", 34, SANS),
    ]),
    ("B2 三位小数 100.000", [
        ("单价 100.000", 34, SANS),
    ]),
    ("B3 千分位错误 1,20,00", [
        ("金额 1,20,00.00", 34, SANS),
    ]),
    ("C 影子字段：两个合计", [
        ("采购订单 PO20241015001", 32, CJK),
        ("物料A  数量 2  单价 6000.00  金额 12000.00", 28, CJK),
        ("合计金额：12000.00", 34, CJK),
        ("Total: 1200.00", 20, SANS),
    ]),
]


def main():
    for name, lines in CASES:
        img = draw(lines)
        arr = np.asarray(img)[:, :, ::-1].copy()
        items = run_ocr_array(arr)
        lay = rebuild(items)
        print(f"--- {name}")
        for it in items:
            print(f"    raw -> {it['text']!r}  score={it['score']:.3f}")
        print("    layout.text 片段 -> " + repr(lay.text[:120]))
        print()


if __name__ == "__main__":
    main()
