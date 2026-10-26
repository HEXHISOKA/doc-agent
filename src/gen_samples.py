"""生成贴近真实工厂场景的采购订单样本（含 ground truth）。

设计要点：
- 版式可切换：standard（有表格线）/ align（无线靠对齐）/ dense（多明细）
- 脏化程度可调：倾斜、噪点、灰度、印章遮挡、手写批注
- 同时输出 gt.json，供后续算字段级准确率

用法：
    python gen_samples.py --out ../samples
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

try:
    RESAMPLE = Image.Resampling.LANCZOS
    BICUBIC = Image.Resampling.BICUBIC
except AttributeError:  # Pillow < 9.1
    RESAMPLE = Image.LANCZOS
    BICUBIC = Image.BICUBIC

FONT_CANDIDATES = {
    "hei": ["C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyhbd.ttc"],
    "song": ["C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simsunb.ttf"],
    "kai": ["C:/Windows/Fonts/simkai.ttf", "C:/Windows/Fonts/simsun.ttc"],
    "mono": ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf"],
}

W, H = 1240, 1754
MARGIN = 90
INK = (25, 25, 25)


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    # 兜底：任何存在的 ttf，避免脚本直接崩
    for path in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/simsun.ttc"):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    raise RuntimeError("找不到任何可用字体，请检查 C:/Windows/Fonts")


# ---------- 数据 ----------

ITEMS = [
    ("1", "500ml透明塑料瓶", 10000, 5.60),
    ("2", "PE密封垫片", 20000, 0.85),
    ("3", "纸箱(五层)", 500, 8.20),
]
ITEMS_DENSE = ITEMS + [
    ("4", "PP编织袋", 3000, 1.35),
    ("5", "打包带", 800, 3.10),
]

ORDER_NO = "PO20261024001"
SUPPLIER = "宁波恒达塑胶制品有限公司"
BUYER = "XX精密制造有限公司"
ORDER_DATE = "2026-10-24"
DELIVERY_DATE = "2026-11-15"


def amount_of(items):
    return round(sum(qty * price for _, _, qty, price in items), 2)


def upper_cn(amount: float) -> str:
    """把金额转成中文大写，用于勾稽校验测试。"""
    digits = "零壹贰叁肆伍陆柒捌玖"
    units = ["", "拾", "佰", "仟"]
    int_part = int(amount)
    frac = round((amount - int_part) * 100)

    def section(n: int) -> str:
        s = ""
        zero = False
        for i, ch in enumerate(str(n).zfill(4)):
            d = int(ch)
            if d == 0:
                zero = True
            else:
                if zero and s:
                    s += "零"
                s += digits[d] + units[3 - i]
                zero = False
        return s

    if int_part == 0:
        s = "零"
    else:
        groups = []
        n = int_part
        names = ["", "万", "亿"]
        gi = 0
        while n > 0:
            groups.append((n % 10000, names[gi]))
            n //= 10000
            gi += 1
        s = ""
        for val, name in reversed(groups):
            if val == 0:
                continue
            part = section(val)
            if s and val < 1000 and not s.endswith("零"):
                s += "零"
            s += part + name
    out = s + "元"
    if frac == 0:
        return out + "整"
    jiao, fen = divmod(frac, 10)
    if jiao:
        out += digits[jiao] + "角"
    if fen:
        out += digits[fen] + "分"
    return out


# ---------- 绘制 ----------


def draw_base(variant: str) -> Image.Image:
    img = Image.new("RGB", (W, H), (252, 252, 250))
    d = ImageDraw.Draw(img)
    items = ITEMS_DENSE if variant == "dense" else ITEMS

    d.text((W // 2, MARGIN), BUYER, font=load_font("hei", 40), fill=INK, anchor="ma")
    d.text((W // 2, MARGIN + 60), "采 购 订 单", font=load_font("hei", 46), fill=INK, anchor="ma")
    d.text((W - MARGIN, MARGIN + 130), f"单据编号：{ORDER_NO}", font=load_font("song", 20), fill=INK, anchor="ra")

    y = MARGIN + 180
    d.text((MARGIN, y), f"供应商：{SUPPLIER}", font=load_font("song", 24), fill=INK)
    d.text((W - MARGIN, y), f"下单日期：{ORDER_DATE}", font=load_font("song", 22), fill=INK, anchor="ra")

    y += 50
    cols = [MARGIN, MARGIN + 80, MARGIN + 420, MARGIN + 580, MARGIN + 740, W - MARGIN]
    headers = ["序号", "物料名称", "数量", "单价", "金额"]

    row_h = 46
    if variant != "align":
        # 表格线：故意带 1~2px 抖动，模拟复印/扫描畸变
        jitter = [0, 1, -1, 1, 0, -1]
        for gy in range(len(items) + 2):
            yy = y + gy * row_h + jitter[gy % len(jitter)]
            d.line([(cols[0], yy), (cols[-1], yy)], fill=(90, 90, 90), width=2)
        for i, cx in enumerate(cols):
            d.line([(cx + (i % 2), y), (cx + (i % 2), y + (len(items) + 1) * row_h)],
                   fill=(90, 90, 90), width=2)

    for i, htxt in enumerate(headers):
        cxm = (cols[i] + cols[i + 1]) / 2
        d.text((cxm, y + row_h / 2), htxt, font=load_font("hei", 22), fill=INK, anchor="mm")

    for r, (no, name, qty, price) in enumerate(items, start=1):
        ry = y + r * row_h + row_h / 2
        cells = [no, name, f"{qty}", f"{price:.2f}", f"{qty * price:.2f}"]
        for i, cell in enumerate(cells):
            anchor = "lm" if i == 1 else "mm"
            cx = cols[i] + 12 if i == 1 else (cols[i] + cols[i + 1]) / 2
            d.text((cx, ry), cell, font=load_font("mono" if i >= 2 else "song", 22), fill=INK, anchor=anchor)

    total = amount_of(items)
    ty = y + (len(items) + 1) * row_h + 36
    d.text((MARGIN, ty), f"合计金额（大写）：{upper_cn(total)}", font=load_font("song", 24), fill=INK)
    d.text((W - MARGIN, ty), f"¥{total:,.2f}", font=load_font("mono", 26), fill=INK, anchor="ra")

    ty += 60
    d.text((MARGIN, ty), f"交货日期：{DELIVERY_DATE}", font=load_font("song", 24), fill=INK)
    ty += 44
    d.text((MARGIN, ty), "备注：请随货附质检报告，逾期按合同第 7 条处理。", font=load_font("song", 20), fill=(70, 70, 70))

    ty += 90
    d.text((MARGIN, ty), "供应商确认：", font=load_font("song", 22), fill=INK)
    d.text((W - MARGIN - 220, ty), "采购签字：", font=load_font("song", 22), fill=INK)
    return img


def draw_stamp(img: Image.Image, center, company: str, alpha: int, angle: int) -> Image.Image:
    """画一个仿公章：双圆环 + 弧排公司名 + 中间五角星，整体旋转后半透明叠加。"""
    size = 300
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    red = (198, 32, 42, alpha)
    pad = 12
    d.ellipse([pad, pad, size - pad, size - pad], outline=red, width=6)
    d.ellipse([pad + 22, pad + 22, size - pad - 22, size - pad - 22], outline=red, width=2)

    # 五角星
    cx = cy = size / 2
    r_out, r_in = 34, 14
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=red)

    # 弧排文字
    f = load_font("song", 26)
    n = len(company)
    total_deg = min(240, 26 * n)
    start = -90 - total_deg / 2
    radius = size / 2 - 44
    for i, ch in enumerate(company):
        deg = start + total_deg * (i / max(1, n - 1))
        rad = math.radians(deg)
        px = cx + radius * math.cos(rad)
        py = cy + radius * math.sin(rad)
        glyph = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((20, 20), ch, font=f, fill=red, anchor="mm")
        glyph = glyph.rotate(-(deg + 90), resample=BICUBIC, expand=False)
        layer.alpha_composite(glyph, (int(px - 20), int(py - 20)))

    layer = layer.rotate(angle, resample=BICUBIC, expand=False)
    base = img.convert("RGBA")
    base.alpha_composite(layer, (int(center[0] - size / 2), int(center[1] - size / 2)))
    return base.convert("RGB")


def draw_handwriting(img: Image.Image, text: str, xy, angle: int, size: int = 30) -> Image.Image:
    """用楷体 + 轻微倾斜模拟手写批注。"""
    f = load_font("kai", size)
    tmp = Image.new("RGBA", (700, 70), (0, 0, 0, 0))
    ImageDraw.Draw(tmp).text((8, 35), text, font=f, fill=(28, 46, 140, 235), anchor="lm")
    tmp = tmp.rotate(angle, resample=BICUBIC, expand=True)
    base = img.convert("RGBA")
    base.alpha_composite(tmp, (int(xy[0]), int(xy[1])))
    return base.convert("RGB")


def add_noise(img: Image.Image, amount: int, seed: int) -> Image.Image:
    rnd = random.Random(seed)
    px = img.load()
    w, h = img.size
    for _ in range(amount):
        x, y = rnd.randrange(w), rnd.randrange(h)
        v = rnd.randint(130, 215)
        px[x, y] = (v, v, v)
    return img


def render(variant: str, dirt: int, seed: int) -> Image.Image:
    img = draw_base(variant)
    rnd = random.Random(seed)

    if dirt >= 2:
        # 印章压在明细行区域，故意盖住一个金额
        img = draw_stamp(img, (860, 560), "宁波恒达塑胶制品有限公司", alpha=150, angle=rnd.randint(-18, -8))
    if dirt >= 1:
        img = draw_handwriting(img, "已核 10/24 张", (700, 980), angle=rnd.randint(-6, -2))
    if dirt >= 2:
        img = draw_handwriting(img, "此单价按9折", (880, 430), angle=rnd.randint(-12, -4), size=26)

    if dirt >= 1:
        angle = 2.2 if dirt == 1 else 3.4
        img = img.rotate(angle, resample=BICUBIC, expand=True, fillcolor=(250, 250, 248))
        img = add_noise(img, 4000 if dirt == 1 else 12000, seed)
    if dirt >= 2:
        # 复印件发灰：压低对比度
        img = img.point(lambda v: int(60 + v * 0.72))
    if dirt >= 1:
        img = img.filter(ImageFilter.GaussianBlur(0.4 if dirt == 1 else 0.7))
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "samples"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cases = [
        ("po_clean", "standard", 0, 11),
        ("po_tilt", "standard", 1, 12),
        ("po_stamp", "standard", 2, 13),
        ("po_align", "align", 1, 14),
        ("po_dense", "dense", 2, 15),
    ]

    gt = {}
    for name, variant, dirt, seed in cases:
        img = render(variant, dirt, seed)
        path = out / f"{name}.png"
        img.save(path)
        items = ITEMS_DENSE if variant == "dense" else ITEMS
        gt[f"{name}.png"] = {
            "订单号": ORDER_NO,
            "供应商": SUPPLIER,
            "下单日期": ORDER_DATE,
            "交货日期": DELIVERY_DATE,
            "合计金额": f"{amount_of(items):.2f}",
            "明细": [
                {"序号": no, "物料名称": nm, "数量": f"{q}", "单价": f"{p:.2f}", "金额": f"{q * p:.2f}"}
                for no, nm, q, p in items
            ],
            "variant": variant,
            "dirt": dirt,
        }
        print(f"[ok] {path.name}  {img.size[0]}x{img.size[1]}  variant={variant} dirt={dirt}")

    (out / "gt.json").write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] ground truth -> {out / 'gt.json'}")


if __name__ == "__main__":
    main()
