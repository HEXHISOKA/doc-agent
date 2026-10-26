"""验证：OCR 对形近字符（O/0、l/1、S/5）是原样保留还是归一成数字。

这个结果直接决定「混淆字符检测」该做在后端 OCR 文本层，还是必须在原图上做。
结论会影响设计，所以先实测再写代码。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ocr import run_ocr_array

FONT = "C:/Windows/Fonts/consola.ttf"          # 等宽，O 与 0 区分度高
FONT2 = "C:/Windows/Fonts/arial.ttf"


def draw(lines: list[tuple[str, int, str]], w: int = 900, pad: int = 30) -> Image.Image:
    h = pad * 2 + sum(sz + 18 for _, sz, _ in lines)
    img = Image.new("RGB", (w, h), (255, 255, 255))
    d = ImageDraw.Draw(img)
    y = pad
    for text, size, fp in lines:
        f = ImageFont.truetype(fp, size)
        d.text((pad, y), text, font=f, fill=(20, 20, 20))
        y += size + 18
    return img


CASES = [
    ("等宽字体 / 字母O冒充数字0", [
        ("单价 1OO.OO  数量 50  金额 5000.OO", 34, FONT),
        ("合计金额: 5,OOO.OO", 34, FONT),
    ]),
    ("等宽字体 / 小写l冒充数字1", [
        ("单价 lOOO.OO  数量 3  金额 3000.OO", 34, FONT),
    ]),
    ("比例字体 / 正常数字（对照组）", [
        ("单价 100.00  数量 50  金额 5000.00", 34, FONT2),
        ("合计金额: 5,000.00", 34, FONT2),
    ]),
    ("比例字体 / 混淆字符", [
        ("单价 1OO.OO  数量 50  金额 5OOO.OO", 34, FONT2),
    ]),
]


def main() -> None:
    for i, (name, lines) in enumerate(CASES, 1):
        img = draw(lines)
        arr = np.asarray(img)[:, :, ::-1].copy()
        items = run_ocr_array(arr)
        texts = [it["text"] for it in items]
        print(f"--- case {i}: {name}")
        for t in texts:
            print(f"    OCR -> {t!r}")
        joined = " ".join(texts)
        print(f"    保留字母? {'是' if any(c in joined for c in 'OlISsB') else '否（已被归一）'}")
        img.save(Path(__file__).parent.parent / "out" / f"confuse_{i}.png")
        print()


if __name__ == "__main__":
    main()
