"""生成「真实形态红章」样本：用于复现公章文字混进表格的问题。

与 gen_samples.draw_stamp 的区别（这就是旧样本没暴露问题的原因）：
    旧样本 alpha=150 + 整层旋转，弧排字是逐个旋转后合成，颜色很淡（叠在灰底上
    接近 45% 灰），OCR 根本没检出印章文字。
    真实公章是**实心红油墨**：颜色饱和（R≈200, G/B≈40）、笔画边缘锐利、
    中间常压一行编码数字（如 3302055498010）、底部还可能有一行横排字
    （"财务专用章"）。这种章压在明细表上会产生大量 2~4 字的碎片词：
      · 弧排字被逐字检出 → "限公"（"公司"的倒序，圆弧上字序本身是反的）
      · 编码数字串被切碎   → "554" "980"
      · 与真实金额粘连     → "1.00"
    这些碎片一旦进入行聚类，就会把一行切成 6~7 个 cell，串列直接发生。

用法：
    python gen_seal_case.py --out ../samples
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import gen_samples as G

try:
    BICUBIC = Image.Resampling.BICUBIC
except AttributeError:  # Pillow < 9.1
    BICUBIC = Image.BICUBIC

SEAL_RED = (206, 28, 38)
SEAL_COMPANY = "宁波恒达塑胶制品有限公司"


def draw_real_seal(
    img: Image.Image,
    center: tuple[int, int],
    radius: int = 150,
    alpha: int = 205,
    company: str = SEAL_COMPANY,
    bottom_text: str = "财务专用章",
    code: str = "3302055498010",
) -> Image.Image:
    """画一个工业界真实样子的圆形红章（不透明油墨、弧排公司名、底部横排、中心编码）。"""
    size = radius * 2 + 20
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    red = (*SEAL_RED, alpha)
    cx = cy = size / 2
    r = radius

    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=red, width=7)
    # 印章编码：压在圆环下部，真实章上就是这串数字
    d.text((cx, cy + r - 16), code, font=G.load_font("mono", 17), fill=red, anchor="mm")

    # 弧排公司名：每个字绕圆心旋转，字头朝外
    f = G.load_font("song", 30)
    n = len(company)
    total_deg = 250
    start = -90 - total_deg / 2
    text_r = r - 30
    for i, ch in enumerate(company):
        deg = start + total_deg * (i / max(1, n - 1))
        rad = math.radians(deg)
        px = cx + text_r * math.cos(rad)
        py = cy + text_r * math.sin(rad)
        glyph = Image.new("RGBA", (52, 52), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((26, 26), ch, font=f, fill=red, anchor="mm")
        glyph = glyph.rotate(-(deg + 90), resample=BICUBIC, expand=False)
        layer.alpha_composite(glyph, (int(px - 26), int(py - 26)))

    # 底部横排字（真实章常见）
    d.text((cx, cy + 78), bottom_text, font=G.load_font("hei", 22), fill=red, anchor="mm")

    # 五角星
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = 40 if i % 2 == 0 else 16
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=red)

    base = img.convert("RGBA")
    base.alpha_composite(layer, (int(center[0] - size / 2), int(center[1] - size / 2)))
    return base.convert("RGB")


def build(dirt: int = 2, seed: int = 41) -> Image.Image:
    """在标准单据上盖一枚真实红章，压在明细表右半（数量/单价/金额列）。"""
    import random

    img = G.draw_base("standard")
    # 章压在明细行上，这正是用户遇到的情况：碎片插在数据当中
    img = draw_real_seal(img, (700, 520), radius=155, alpha=200)
    rnd = random.Random(seed)
    if dirt >= 2:
        img = G.draw_handwriting(img, "此单价按9折", (880, 430), angle=rnd.randint(-12, -4), size=26)
    if dirt >= 1:
        img = img.rotate(3.0, resample=BICUBIC, expand=True, fillcolor=(250, 250, 248))
        img = G.add_noise(img, 9000, seed)
    if dirt >= 2:
        img = img.point(lambda v: int(70 + v * 0.80))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "samples"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    img = build()
    p = out / "po_seal_real.png"
    img.save(p)
    print(f"[ok] {p}  {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    main()
