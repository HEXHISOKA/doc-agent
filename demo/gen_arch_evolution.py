# -*- coding: utf-8 -*-
"""架构变迁与优化效果演示图（给甲方/面试官一眼看懂）"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H = 2000, 1390
INK = (26, 26, 26)
GREY = (90, 90, 90)
LGREY = (150, 150, 150)
ACCENT = (31, 95, 168)
ACCENT_BG = (238, 244, 251)
RED = (176, 48, 48)
REDBG = (253, 238, 238)
GREEN = (30, 122, 70)
BOX_BG = (250, 250, 250)
BOX_BR = (138, 138, 138)
BAR_GREY = (184, 194, 204)

img = Image.new("RGB", (W, H), (255, 255, 255))
d = ImageDraw.Draw(img)

F = lambda s, b=False: ImageFont.truetype(
    "C:/Windows/Fonts/msyhbd.ttc" if b else "C:/Windows/Fonts/msyh.ttc", s)


def tw(t, f):
    return d.textlength(t, font=f)


def box(x, y, w, h, text, fill=BOX_BG, br=BOX_BR, tcol=INK, size=22, bold=False,
        dash=False, width=2):
    if dash:
        step, gap = 9, 6
        for x0 in range(int(x), int(x + w), step + gap):
            d.line([(x0, y), (min(x0 + step, x + w), y)], fill=br, width=width)
            d.line([(x0, y + h), (min(x0 + step, x + w), y + h)], fill=br, width=width)
        for y0 in range(int(y), int(y + h), step + gap):
            d.line([(x, y0), (x, min(y0 + step, y + h))], fill=br, width=width)
            d.line([(x + w, y0), (x + w, min(y0 + step, y + h))], fill=br, width=width)
    else:
        d.rounded_rectangle([x, y, x + w, y + h], radius=8, fill=fill,
                            outline=br, width=width)
    f = F(size, bold)
    d.text((x + w / 2, y + h / 2), text, font=f, fill=tcol, anchor="mm")


def arrow(x0, x1, yc, col=LGREY):
    d.line([(x0, yc), (x1 - 12, yc)], fill=col, width=3)
    d.polygon([(x1, yc), (x1 - 14, yc - 8), (x1 - 14, yc + 8)], fill=col)


def pipeline(x, yc, nodes, gap=26):
    """nodes: list of (label, style) style in ('n','ocr','ref','hero')"""
    f = F(22)
    cx = x
    for i, (label, st) in enumerate(nodes):
        w = tw(label, f) + 34
        if st == "ocr":
            box(cx, yc - 28, w, 56, label, fill=REDBG, br=(192, 80, 80), tcol=RED)
        elif st == "ref":
            box(cx, yc - 28, w, 56, label, fill=(242, 242, 242), br=(153, 153, 153),
                tcol=GREY, dash=True)
        elif st == "hero":
            box(cx, yc - 28, w, 56, label, fill=ACCENT, br=ACCENT, tcol=(255, 255, 255),
                bold=True)
        else:
            box(cx, yc - 28, w, 56, label)
        cx += w
        if i < len(nodes) - 1:
            arrow(cx + 4, cx + gap - 4, yc)
            cx += gap
    return cx


def note(x, yc, lines, col, maxw=None):
    f = F(21)
    for i, t in enumerate(lines):
        for j, seg in enumerate(wrap(t, f, maxw) if maxw else [t]):
            d.text((x, yc - 16 * (len(lines) - 1) + i * 32 + j * 26), seg, font=f,
                   fill=col, anchor="lm")


def wrap(t, f, maxw):
    out, cur = [], ""
    for ch in t:
        if tw(cur + ch, f) > maxw:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    out.append(cur)
    return out


# ---------------------------------------------------------------- 标题
d.text((70, 52), "智能单据自动录入系统：架构变迁与优化效果", font=F(44, True), fill=INK)
d.text((70, 122), "四代演进 · 两套评测集（各 20 张）实测 · 现行生产链路已上线",
       font=F(25), fill=GREY)
d.text((W - 70, 60), "2026-10-26", font=F(22), fill=LGREY, anchor="ra")

# ---------------------------------------------------------------- 架构演进
d.text((70, 196), "① 架构演进 —— OCR 从「数据的来源」退到「校验的证据」",
       font=F(30, True), fill=ACCENT)

NX = 1545  # 备注列起点
rows = [
    ("v0 纯 OCR",
     [("拍照图片", "n"), ("OCR 识别", "ocr"), ("人工录入", "n")],
     ["死因：OCR 有损识别，错字直接入库"], RED, None),
    ("v1 OCR 串联",
     [("拍照图片", "n"), ("OCR", "ocr"), ("LLM 读文本抽取", "n"), ("三层校验", "n"), ("入库", "n")],
     ["死因：明细 95.6%（OCR 丢列错位）；", "「编号 100%」被对照实验证实为先验猜对"], RED, None),
    ("v2 直读+校验",
     [("拍照图片", "n"), ("视觉模型直读", "hero"), ("三层校验", "n"), ("入库", "n")],
     ["死因：出处层没有参照物，误放行防不住"], RED, None),
    ("v3 现行·已上线",
     [("拍照图片", "n"), ("视觉模型直读", "hero"), ("三层校验+编号归一", "n"),
      ("疑点看图复核", "n"), ("人工确认→入库", "n")],
     ["效果：全字段 100%、误放行 0（两场景实测）"], GREEN, "v3"),
]

RY, RH = 258, 112
for bi, (badge, nodes, nlines, ncol, hl) in enumerate(rows):
    yc = RY + bi * RH + RH // 2
    if hl:
        d.rounded_rectangle([55, yc - 52, W - 55, yc + 52], radius=12,
                            fill=ACCENT_BG, outline=ACCENT, width=2)
    # 代际徽章
    bw = 176
    box(70, yc - 28, bw, 56, badge, fill=(255, 255, 255) if not hl else ACCENT_BG,
        br=ACCENT if hl else BOX_BR, tcol=ACCENT if hl else INK,
        size=23, bold=True, width=3 if hl else 2)
    end = pipeline(70 + bw + 24, yc, nodes)
    note(max(NX, end + 30), yc, nlines, ncol, maxw=(W - 75) - max(NX, end + 30))

# v3 行下方：OCR 参照虚线框 + 虚线箭头
yc3 = RY + 3 * RH + RH // 2
fx = 70 + 176 + 24 + 118  # 直读框右缘附近
box(fx, yc3 + 36, 700, 44, "OCR 文本（本地零 token）→ 出处层参照 ＋ 复核第二证据源",
    fill=(242, 242, 242), br=(140, 140, 140), tcol=GREY, size=20, dash=True)
d.line([(fx + 118, yc3 + 36), (fx + 118, yc3 + 30)], fill=LGREY, width=2)

# ---------------------------------------------------------------- 分隔线
dy = 852
d.line([(70, dy), (W - 70, dy)], fill=(210, 210, 210), width=2)

# ---------------------------------------------------------------- 效果对比
d.text((70, dy + 22), "② 优化效果 —— v1 → v3（同一评测口径实测）",
       font=F(30, True), fill=ACCENT)
# 图例
lx = W - 660
d.rectangle([lx, dy + 30, lx + 26, dy + 56], fill=BAR_GREY)
d.text((lx + 36, dy + 43), "v1 原版：OCR 串联", font=F(21), fill=INK, anchor="lm")
d.rectangle([lx + 280, dy + 30, lx + 306, dy + 56], fill=ACCENT)
d.text((lx + 316, dy + 43), "v3 现行：直读 + 防线后挂", font=F(21), fill=INK, anchor="lm")

groups = [
    ("全字段正确率", [("v1", 60, "60%"), ("v3", 100, "100%")], 100, "+40pp", "%"),
    ("明细单元格正确率", [("v1", 95.6, "95.6%"), ("v3", 100, "100%")], 100, "+4.4pp", "%"),
    ("误放行（错数据入库）", [("v1", 2, "2 张"), ("v3", 0, "0 张")], 2, "2 → 0", "张"),
    ("抽取延迟 p50", [("v1", 3718, "3718ms"), ("v3", 500, "500ms")], 3718, "-87%", "ms"),
]
BASE = 1195
BH = 190
gw = (W - 140) / 4
for gi, (title, bars, vmax, delta, unit) in enumerate(groups):
    cx0 = 70 + gi * gw
    ccx = cx0 + gw / 2
    d.text((ccx, dy + 92), title, font=F(24, True), fill=INK, anchor="mm")
    bwid, bgap = 120, 84
    x0 = ccx - bwid - bgap / 2
    for bi, (lab, val, vtxt) in enumerate(bars):
        bx = x0 + bi * (bwid + bgap)
        h = BH * val / vmax
        col = ACCENT if lab == "v3" else BAR_GREY
        if h > 2:
            d.rectangle([bx, BASE - h, bx + bwid, BASE], fill=col)
        f = F(26, True)
        tcol = ACCENT if lab == "v3" else GREY
        d.text((bx + bwid / 2, BASE - h - 18), vtxt, font=f, fill=tcol, anchor="mm")
        d.text((bx + bwid / 2, BASE + 22), lab, font=F(21), fill=GREY, anchor="mm")
    # 提升徽章
    df = F(24, True)
    dwid = tw(delta, df) + 40
    dyv = BASE + 58
    d.rounded_rectangle([ccx - dwid / 2, dyv, ccx + dwid / 2, dyv + 42], radius=21,
                        fill=ACCENT_BG, outline=ACCENT, width=2)
    d.text((ccx, dyv + 21), delta, font=df, fill=ACCENT, anchor="mm")

d.text((70, H - 52),
       "口径声明：单集 20 张、±5 个点；v3 = 已上线生产链路（评测脚本与生产共用同一份提示词）；"
       "v3 拦截的 4~5 张全为模型自报不确定性的保守转人工，数据实际全对。",
       font=F(21), fill=GREY)

out = os.path.join(HERE, "架构变迁与优化效果.png")
img.save(out)
print("saved:", out, img.size)
