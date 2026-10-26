"""生成 20 张采购订单评测集：多版式 × 多字段标签 × 多脏化，附精确 ground truth。

与 samples/ 里 5 张演示样本的区别（这是评测集能出数字的前提）：
- 每张单据的供应商、订单号、物料、数量、单价都不同
  → 模型无法靠"记住上次答案"得高分，只能真识别
- 字段标签三套（单据编号/订单号/单号、供应商/供方名称/供应商名称、…）
  → 对应真实工厂里不同供应商的模板差异
- 版式三套（有框线 / 仅横线 / 隔行底色无框线）
- 脏化四级，印章位置随机（压金额列 / 压供应商 / 压表头）

ground truth 在生成时同步落盘，是精确值而非人工事后标注。

用法：
    python gen_eval_set.py --out ../eval/set
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
except AttributeError:
    RESAMPLE = Image.LANCZOS
    BICUBIC = Image.BICUBIC

FONT_CANDIDATES = {
    "hei": ["C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/msyhbd.ttc"],
    "song": ["C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simsunb.ttf"],
    "kai": ["C:/Windows/Fonts/simkai.ttf", "C:/Windows/Fonts/simsun.ttc"],
    "mono": ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf"],
    # 编号数字专用：正常单据的编号字体（Arial）里 O 与 0 有 ~2.7% 像素差，
    # 可被 OCR/视觉模型区分。宋体/黑体两者近乎同字形（0.0~0.9%），是真实
    # 场景的"退化分支"，另存 eval/set_simsun20 演练，不作为主评测集。
    "code": ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/consola.ttf"],
}

W, H = 1240, 1754
MARGIN = 90
INK = (25, 25, 25)
BUYER = "XX精密制造有限公司"

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    key = (kind, size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for path in FONT_CANDIDATES[kind]:
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
    for path in ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/simsun.ttc"):
        if Path(path).exists():
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
    raise RuntimeError("找不到可用字体，请检查 C:/Windows/Fonts")


# ---------------- 数据池 ----------------

ITEM_POOL = [
    ("500ml透明塑料瓶", 5.60), ("PE密封垫片", 0.85), ("纸箱(五层)", 8.20),
    ("PP编织袋", 1.35), ("打包带", 3.10), ("缠绕膜", 12.50),
    ("不锈钢螺丝M6", 0.42), ("橡胶垫圈", 0.18), ("瓦楞纸板", 4.75),
    ("气泡膜", 6.80), ("铝箔袋", 2.35), ("标签纸", 0.95),
    ("塑料周转箱", 26.40), ("封箱胶带", 2.05), ("木托盘", 68.00),
]

SUPPLIERS = [
    "宁波恒达塑胶制品有限公司",
    "东莞市信宏包装材料有限公司",
    "苏州工业园区华盛包装有限公司",
    "青岛海诺包装科技有限公司",
    "佛山市三水区永昌塑料厂",
    "温州龙湾宏利纸品有限公司",
]

LABELS = {
    "A": {"no": "单据编号", "sup": "供应商", "date": "下单日期",
          "total": "合计金额（大写）", "deliv": "交货日期"},
    "B": {"no": "订单号", "sup": "供方名称", "date": "日期",
          "total": "金额合计", "deliv": "要求到货"},
    "C": {"no": "单号", "sup": "供应商名称", "date": "制单日期",
          "total": "合计（大写）", "deliv": "交期"},
}

# 列坐标（W=1240，MARGIN=90）
COLS = [90, 170, 570, 770, 950, 1150]
HEADERS = ["序号", "物料名称", "数量", "单价", "金额"]


def upper_cn(amount: float) -> str:
    digits = "零壹贰叁肆伍陆柒捌玖"
    units = ["", "拾", "佰", "仟"]
    int_part = int(amount)
    frac = round((amount - int_part) * 100)

    def section(n: int) -> str:
        s, zero = "", False
        for i, ch in enumerate(str(n).zfill(4)):
            dv = int(ch)
            if dv == 0:
                zero = True
            else:
                if zero and s:
                    s += "零"
                s += digits[dv] + units[3 - i]
                zero = False
        return s

    if int_part == 0:
        s = "零"
    else:
        groups, n, gi = [], int_part, 0
        names = ["", "万", "亿"]
        while n > 0:
            groups.append((n % 10000, names[gi]))
            n //= 10000
            gi += 1
        s = ""
        for val, name in reversed(groups):
            if val == 0:
                continue
            if s and val < 1000 and not s.endswith("零"):
                s += "零"
            s += section(val) + name
    out = s + "元"
    if frac == 0:
        return out + "整"
    jiao, fen = divmod(frac, 10)
    if jiao:
        out += digits[jiao] + "角"
    if fen:
        out += digits[fen] + "分"
    return out


# ---------------- 绘制 ----------------

def draw_doc(case: dict) -> Image.Image:
    img = Image.new("RGB", (W, H), (253, 253, 251))
    d = ImageDraw.Draw(img)
    lb = LABELS[case["labels"]]
    variant = case["variant"]
    items = case["items"]
    rnd = random.Random(case["seed"])

    y = MARGIN
    d.text((W // 2, y), BUYER, font=load_font("hei", 38), fill=INK, anchor="ma")
    d.text((W // 2, y + 56), "采 购 订 单", font=load_font("hei", 46), fill=INK, anchor="ma")
    # 编号行：标签用宋体，编号数字用 Arial（正常单据编号字体，O/0 可区分）。
    # 拆两段绘制的原因：Arial 无中文字形；右对齐布局 = 数字贴右边、标签在其左。
    _numf = load_font("code", 21)
    _numw = d.textlength(case["doc_no"], font=_numf)
    d.text((W - MARGIN, y + 128), case["doc_no"], font=_numf, fill=INK, anchor="ra")
    d.text((W - MARGIN - _numw - 6, y + 130), f"{lb['no']}：",
           font=load_font("song", 20), fill=INK, anchor="ra")

    y += 190
    d.text((MARGIN, y), f"{lb['sup']}：{case['supplier']}", font=load_font("song", 24), fill=INK)
    dy = y + (36 if len(case["supplier"]) > 13 else 0)
    d.text((W - MARGIN, y), f"{lb['date']}：{case['order_date']}",
           font=load_font("song", 22), fill=INK, anchor="ra")
    if dy != y:
        d.text((MARGIN, dy), "　", font=load_font("song", 24), fill=INK)

    y = MARGIN + 260
    row_h = 46
    nrow = len(items)
    tbl_bottom = y + (nrow + 1) * row_h

    if variant == "banded":
        for r in range(nrow + 1):
            if r % 2 == 0:
                d.rectangle([COLS[0], y + r * row_h, COLS[-1], y + (r + 1) * row_h],
                            fill=(240, 240, 236))
    if variant == "framed":
        jitter = [0, 1, -1, 1, 0, -1, 1]
        for gy in range(nrow + 2):
            yy = y + gy * row_h + jitter[gy % len(jitter)]
            d.line([(COLS[0], yy), (COLS[-1], yy)], fill=(88, 88, 88), width=2)
        for i, cx in enumerate(COLS):
            d.line([(cx + (i % 2), y), (cx + (i % 2), tbl_bottom)],
                   fill=(88, 88, 88), width=2)
    elif variant in ("plain", "banded"):
        d.line([(COLS[0], y), (COLS[-1], y)], fill=(88, 88, 88), width=2)
        d.line([(COLS[0], y + row_h), (COLS[-1], y + row_h)], fill=(88, 88, 88), width=2)
        d.line([(COLS[0], tbl_bottom), (COLS[-1], tbl_bottom)], fill=(88, 88, 88), width=2)

    for i, htxt in enumerate(HEADERS):
        cxm = (COLS[i] + COLS[i + 1]) / 2
        d.text((cxm, y + row_h / 2), htxt, font=load_font("hei", 22), fill=INK, anchor="mm")

    for r, it in enumerate(items, start=1):
        ry = y + r * row_h + row_h / 2
        cells = [it["序号"], it["物料名称"], it["数量"], it["单价"], it["金额"]]
        for i, cell in enumerate(cells):
            if i == 1:
                d.text((COLS[1] + 12, ry), cell, font=load_font("song", 21), fill=INK, anchor="lm")
            else:
                cxm = (COLS[i] + COLS[i + 1]) / 2
                d.text((cxm, ry), cell, font=load_font("mono", 22), fill=INK, anchor="mm")

    total = case["total_amount"]
    ty = tbl_bottom + 40
    d.text((MARGIN, ty), f"{lb['total']}：{upper_cn(total)}",
           font=load_font("song", 24), fill=INK)
    d.text((W - MARGIN, ty), f"¥{total:,.2f}", font=load_font("mono", 26), fill=INK, anchor="ra")

    ty += 62
    d.text((MARGIN, ty), f"{lb['deliv']}：{case['delivery_date']}",
           font=load_font("song", 24), fill=INK)
    ty += 46
    d.text((MARGIN, ty), "备注：请随货附质检报告，逾期按合同第 7 条处理。",
           font=load_font("song", 20), fill=(70, 70, 70))

    ty += 96
    d.text((MARGIN, ty), "供应商确认：", font=load_font("song", 22), fill=INK)
    d.text((W - MARGIN - 220, ty), "采购签字：", font=load_font("song", 22), fill=INK)

    # 折痕（模拟折叠过的单据，一道竖/横浅灰线）
    if case["dirt"] >= 3 and rnd.random() < 0.6:
        if rnd.random() < 0.5:
            fx = rnd.randint(300, W - 300)
            d.line([(fx, 0), (fx + 6, H)], fill=(205, 205, 200), width=3)
        else:
            fy = rnd.randint(400, H - 400)
            d.line([(0, fy), (W, fy - 5)], fill=(205, 205, 200), width=3)

    return img


def draw_stamp(img: Image.Image, center, company: str, alpha: int, angle: int) -> Image.Image:
    size = 300
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    red = (198, 32, 42, alpha)
    pad = 12
    d.ellipse([pad, pad, size - pad, size - pad], outline=red, width=6)
    d.ellipse([pad + 22, pad + 22, size - pad - 22, size - pad - 22], outline=red, width=2)

    cx = cy = size / 2
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = 34 if i % 2 == 0 else 14
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, fill=red)

    f = load_font("song", 26)
    n = len(company)
    total_deg = min(240, 26 * n)
    start = -90 - total_deg / 2
    radius = size / 2 - 44
    for i, ch in enumerate(company):
        deg = start + total_deg * (i / max(1, n - 1))
        rad = math.radians(deg)
        px, py = cx + radius * math.cos(rad), cy + radius * math.sin(rad)
        glyph = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((20, 20), ch, font=f, fill=red, anchor="mm")
        glyph = glyph.rotate(-(deg + 90), resample=BICUBIC, expand=False)
        layer.alpha_composite(glyph, (int(px - 20), int(py - 20)))

    layer = layer.rotate(angle, resample=BICUBIC, expand=False)
    base = img.convert("RGBA")
    base.alpha_composite(layer, (int(center[0] - size / 2), int(center[1] - size / 2)))
    return base.convert("RGB")


def draw_handwriting(img: Image.Image, text: str, xy, angle: int, size: int = 30) -> Image.Image:
    f = load_font("kai", size)
    tmp = Image.new("RGBA", (760, 70), (0, 0, 0, 0))
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


def dirty(img: Image.Image, case: dict) -> Image.Image:
    rnd = random.Random(case["seed"] + 7)
    dirt = case["dirt"]

    if dirt >= 2:
        # 印章位置随机：压金额列 / 压供应商名 / 压表头
        pos = rnd.choice([(880, 620), (400, 300), (700, 420)])
        img = draw_stamp(img, pos, case["supplier"], alpha=145, angle=rnd.randint(-18, -8))
    if dirt >= 3:
        img = draw_handwriting(img, rnd.choice(["已核 10/24 张", "同意付款 张", "价格已谈"]),
                               (rnd.randint(620, 760), rnd.randint(950, 1030)),
                               angle=rnd.randint(-6, -2))
    if dirt >= 2 and rnd.random() < 0.6:
        img = draw_handwriting(img, rnd.choice(["此单价按9折", "加急", "已对账"]),
                               (rnd.randint(760, 900), rnd.randint(430, 520)),
                               angle=rnd.randint(-12, -4), size=26)

    if dirt >= 1:
        angle = {1: 1.8, 2: 2.9, 3: 3.6}[dirt]
        img = img.rotate(angle, resample=BICUBIC, expand=True, fillcolor=(250, 250, 248))
        img = add_noise(img, 3000 if dirt == 1 else (9000 if dirt == 2 else 16000), case["seed"])
    if dirt >= 3:
        img = img.point(lambda v: int(58 + v * 0.70))
    if dirt >= 1:
        img = img.filter(ImageFilter.GaussianBlur({1: 0.35, 2: 0.55, 3: 0.75}[dirt]))
    return img


# ---------------- 用例构造 ----------------

def build_case(idx: int, variant: str, labels: str, dirt: int, seed: int) -> dict:
    rnd = random.Random(seed)
    supplier = SUPPLIERS[idx % len(SUPPLIERS)]
    nrow = rnd.choice([2, 3, 3, 4, 4, 5, 6])

    picked = rnd.sample(ITEM_POOL, nrow)
    items, total_cents = [], 0
    for r, (name, price) in enumerate(picked, start=1):
        qty = rnd.choice([100, 200, 300, 500, 800, 1000, 2000, 3000, 5000, 10000, 20000])
        price_cents = int(round(price * 100))
        amt_cents = qty * price_cents
        total_cents += amt_cents
        items.append({
            "序号": str(r),
            "物料名称": name,
            "数量": str(qty),
            "单价": f"{price_cents / 100:.2f}",
            "金额": f"{amt_cents / 100:.2f}",
        })

    month = rnd.choice(["08", "09", "10"])
    day = rnd.randint(10, 28)
    doc_no = f"PO2026{month}{day:02d}{idx + 1:02d}"
    order_date = f"2026-{month}-{day:02d}"
    delivery_date = f"2026-{int(month) + 1 if month != '12' else 1:02d}-{min(day + 12, 28):02d}"

    return {
        "name": f"po_{idx + 1:02d}_{variant}_{labels}_d{dirt}",
        "variant": variant, "labels": labels, "dirt": dirt, "seed": seed,
        "supplier": supplier, "doc_no": doc_no,
        "order_date": order_date, "delivery_date": delivery_date,
        "items": items, "total_amount": total_cents / 100,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "eval" / "set"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # 20 例：覆盖 3 版式 × 3 标签集 × 4 级脏化，按需组合避免全交叉（60 例没必要）
    plan = [
        ("framed", "A", 0), ("framed", "A", 1), ("framed", "B", 1), ("framed", "C", 2),
        ("framed", "B", 2), ("framed", "A", 3), ("framed", "C", 3), ("framed", "A", 2),
        ("plain", "A", 1), ("plain", "B", 1), ("plain", "C", 2), ("plain", "A", 2),
        ("plain", "B", 3), ("plain", "A", 3), ("plain", "C", 1),
        ("banded", "A", 1), ("banded", "B", 2), ("banded", "C", 1), ("banded", "A", 3),
        ("banded", "B", 3),
    ]

    gt = {}
    for i, (variant, labels, dirt) in enumerate(plan):
        case = build_case(i, variant, labels, dirt, seed=100 + i)
        img = dirty(draw_doc(case), case)
        fn = f"{case['name']}.png"
        img.save(out / fn)
        gt[fn] = {
            "doc_no": case["doc_no"],
            "supplier": case["supplier"],
            "order_date": case["order_date"],
            "delivery_date": case["delivery_date"],
            "total_amount": f"{case['total_amount']:.2f}",
            "items": case["items"],
            "variant": variant, "labels": labels, "dirt": dirt,
        }
        print(f"[ok] {fn:38s} {img.size[0]}x{img.size[1]}  {len(case['items'])}行  "
              f"合计 {case['total_amount']:.2f}")

    (out / "gt.json").write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[ok] 共 {len(gt)} 例 -> {out / 'gt.json'}")


if __name__ == "__main__":
    main()
