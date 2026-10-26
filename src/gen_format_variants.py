"""生成"同语义、不同版式"的国产常见单据，用于稳健性测试。

为什么不全部用网上的图：真实又清晰的国产送货单/出库单/小票，公开可下载的极少
（企业单据本身带客户信息，不会上传）。但"版式差异"是必须覆盖的维度，
于是自造——但自造必须守住三条，否则测出来的只是"对我们自己画的图好不好使"：

  1. **字段名必须是行业里真在用的**（客户名称/收货单位/领料部门/箱号），
     而不是把 PO 的"供应商"换个位置；
  2. **必须埋可判定的错**：每张图都预先算好一处勾稽错误，校验层抓不到就是缺陷，
     这比"人工看图说抽得对不对"客观；
  3. **版式差异要真的不同**：横版/竖版、窄条热敏纸、无金额列、多金额列。

生成后由 src/probe_formats.py 统一跑链，结论写进 out/format_matrix.md。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "samples" / "variants"
FONT_CN = "C:/Windows/Fonts/msyh.ttc"
FONT_CN_B = "C:/Windows/Fonts/msyhbd.ttc"
FONT_MONO = "C:/Windows/Fonts/consola.ttf"

INK = (24, 24, 28)
GREY = (110, 110, 118)
LINE = (60, 60, 66)
RED = (196, 42, 42)


def F(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def pick(s, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按内容挑字体：含中文必须用中文字体。

    Consolas 没有 CJK 字形，用它渲染中文会变成方块——画出来的图 OCR 只能识别到
    "□□□□"，测试就退化成"识别方块的能力"，而不是识别单据的能力。
    """
    if any("\u4e00" <= c <= "\u9fff" for c in str(s)):
        return F(FONT_CN_B if bold else FONT_CN, size)
    return F(FONT_MONO, size)


def text(d, xy, s, font, fill=INK, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def stamp(img: Image.Image, center, label: str, r: int = 92) -> None:
    """盖一枚红章：用图层旋转，避免把整张图转一遍。"""
    size = r * 2 + 24
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([12, 12, size - 12, size - 12], outline=RED + (235,), width=4)
    f = F(FONT_CN_B, int(r * 0.34))
    d.text((size // 2, size // 2 - r * 0.30), label[:6], font=f, fill=RED + (225,), anchor="mm")
    d.text((size // 2, size // 2 + r * 0.06), label[6:12] or "财务专用章", font=F(FONT_CN, int(r * 0.22)),
           fill=RED + (215,), anchor="mm")
    star = "★"
    d.text((size // 2, size // 2 + r * 0.40), star, font=F(FONT_CN, int(r * 0.26)), fill=RED + (215,), anchor="mm")
    layer = layer.rotate(-12, resample=Image.BICUBIC, expand=False)
    img.paste(layer, (center[0] - size // 2, center[1] - size // 2), layer)


def grid(d, x0, y0, colw, rows, rowh, fs, header=True):
    """画一张规整表格，返回每行的 y。rows[0] 为表头。"""
    n = len(colw)
    total_w = sum(colw)
    ys = []
    y = y0
    for ri, row in enumerate(rows):
        ys.append(y)
        h = rowh if ri else rowh
        for ci in range(n):
            cx = x0 + sum(colw[:ci])
            d.rectangle([cx, y, cx + colw[ci], y + h], outline=LINE, width=2)
            f = pick(row[ci], fs, bold=(ri == 0 and header))
            d.text((cx + colw[ci] // 2, y + h // 2), str(row[ci]), font=f, fill=INK, anchor="mm")
        y += h
    d.rectangle([x0, y0, x0 + total_w, y], outline=LINE, width=3)
    return ys, y


# ---------------------------------------------------------------- ① 横向送货单
def delivery_note() -> tuple[Image.Image, dict]:
    W, H = 2000, 1240
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    text(d, (100, 54), "宁波恒力紧固件有限公司", F(FONT_CN_B, 40))
    text(d, (100, 104), "地址：宁波市北仑区小港工业区 16 号    TEL：0574-8612 3344    FAX：0574-8612 3355",
         F(FONT_CN, 20), fill=GREY)
    d.line([100, 148, W - 100, 148], fill=RED, width=4)
    text(d, (W // 2, 208), "送  货  单", font=F(FONT_CN_B, 56), anchor="mm")
    text(d, (W // 2, 262), "DELIVERY NOTE", font=F(FONT_MONO, 20), fill=GREY, anchor="mm")

    f20, f22, f24 = F(FONT_CN, 22), F(FONT_CN, 24), F(FONT_CN, 26)
    text(d, (100, 312), "送货单号：", f22)
    text(d, (215, 312), "DN20261025-018", F(FONT_MONO, 24))
    text(d, (620, 312), "送货日期：", f22)
    text(d, (735, 312), "2026-10-25", F(FONT_MONO, 24))
    text(d, (1360, 312), "订单号：", f22)
    text(d, (1450, 312), "CG-2026-0912", F(FONT_MONO, 24))
    text(d, (100, 356), "客户名称：", f22)
    text(d, (215, 356), "苏州立信电子科技有限公司", f24)
    text(d, (1360, 356), "联系人：", f22)
    text(d, (1450, 356), "王建国", f24)

    rows = [["序号", "品名规格", "单位", "数量", "单价(元)", "金额(元)", "备注"],
            ["1", "内六角螺栓 M8×40", "千件", "12", "268.00", "3216.00", ""],
            ["2", "平垫圈 φ8 镀锌", "千件", "30", "42.50", "1275.00", ""],
            ["3", "弹簧垫圈 φ8", "千件", "25", "38.00", "950.00", ""],
            ["4", "尼龙锁紧螺母 M8", "千件", "18", "156.00", "2808.00", "含运费"]]
    colw = [90, 480, 110, 140, 200, 220, 200]
    ys, ybot = grid(d, 100, 410, colw, rows, 72, 22)

    text(d, (100, ybot + 24), "合计金额（大写）：捌仟贰佰肆拾玖元整", font=F(FONT_CN_B, 26))
    text(d, (1420, ybot + 24), "￥8249.00", font=F(FONT_MONO, 30))

    text(d, (100, H - 190), "发货单位（盖章）：", f22)
    text(d, (760, H - 190), "收货单位（签收）：", f22)
    text(d, (1420, H - 190), "送货人：陈志强", f22)
    text(d, (100, H - 90), "本单据一式三联：白联-发货方存根  红联-收货方留存  蓝联-回执联", F(FONT_CN, 18), fill=GREY)
    stamp(img, (330, H - 250), "宁波恒力紧固件有限公司财务专用章", 96)
    img.save(OUT / "cn_delivery_note.png")
    return img, {"kind": "送货单", "doc_no": "DN20261025-018", "date": "2026-10-25",
                 "party": "苏州立信电子科技有限公司（收货方）",
                 "seller": "宁波恒力紧固件有限公司（发货方，是真正的供货方）",
                 "total": 8249.00, "check": "明细 3216+1275+950+2808=8249，合计正确"}


# ---------------------------------------------------------------- ② 竖版出库单
def outbound_note() -> tuple[Image.Image, dict]:
    W, H = 1500, 1980
    img = Image.new("RGB", (W, H), (252, 252, 250))
    d = ImageDraw.Draw(img)
    text(d, (W // 2, 80), "材 料 出 库 单", font=F(FONT_CN_B, 52), anchor="mm")
    text(d, (W // 2, 136), "No. CK-20261025-07", font=F(FONT_MONO, 24), fill=GREY, anchor="mm")
    f22 = F(FONT_CN, 22)
    text(d, (70, 208), "领用部门：总装二车间", f22)
    text(d, (720, 208), "出库日期：2026-10-25", f22)
    text(d, (70, 252), "用途/工单：WO-2026-1025-A", f22)
    text(d, (720, 252), "仓库：原材料一号库", f22)

    rows = [["物料编码", "物料名称", "规格", "单位", "请领数", "实发数", "单价", "金额"],
            ["01.02.0041", "深沟球轴承", "6204-2RS", "套", "200", "200", "12.60", "2520.00"],
            ["01.02.0055", "深沟球轴承", "6205-2RS", "套", "150", "150", "18.40", "2760.00"],
            ["03.11.0102", "液压油", "46# 抗磨", "桶", "8", "8", "365.00", "2920.00"],
            ["07.21.0008", "密封圈", "O型 φ45×3.5", "个", "500", "480", "1.20", "576.00"]]
    colw = [170, 200, 210, 80, 120, 120, 120, 160]
    ys, ybot = grid(d, 60, 310, colw, rows, 88, 22)

    # 埋点：2 行 150×18.40=2760 正确；4 行 480×1.20=576 正确
    #       明细合计 2520+2760+2920+576 = 8776，但合计行写成 8876（多 100）
    text(d, (60, ybot + 30), "合计金额（小写）：￥8876.00", font=F(FONT_CN_B, 28))
    text(d, (60, ybot + 78), "合计金额（大写）：捌仟捌佰柒拾陆元整", font=F(FONT_CN_B, 26))
    text(d, (60, ybot + 140), "备注：第 4 行实发数少于请领数 20 个，差额待补。", F(FONT_CN, 21), fill=GREY)

    text(d, (60, H - 210), "领料人：赵敏", f22)
    text(d, (480, H - 210), "发料人：孙立", f22)
    text(d, (900, H - 210), "仓管（盖章）：", f22)
    text(d, (60, H - 120), "第一联：财务  第二联：仓库  第三联：领用部门", F(FONT_CN, 18), fill=GREY)
    stamp(img, (1130, H - 260), "恒力制造仓储管理专用章", 92)
    img.save(OUT / "cn_outbound_note.png")
    return img, {"kind": "材料出库单", "doc_no": "CK-20261025-07", "date": "2026-10-25",
                 "party": "领用部门：总装二车间（内部单据，无外部供应商）",
                 "seller": "本厂仓库（内部），单据上没有供应商",
                 "total": 8876.00,
                 "check": "明细 2520+2760+2920+576=8776 ≠ 合计 8876，**故意错 100 元**，校验层必须报出"}


# ---------------------------------------------------------------- ③ 热敏小票
def receipt_tall() -> tuple[Image.Image, dict]:
    W, H = 760, 1720
    img = Image.new("RGB", (W, H), (250, 249, 244))
    d = ImageDraw.Draw(img)
    fm = F(FONT_MONO, 22)
    text(d, (W // 2, 60), "家乐福便利超市", font=F(FONT_CN_B, 36), anchor="mm")
    text(d, (W // 2, 106), "CARRERFOUR EXPRESS", font=F(FONT_MONO, 18), fill=GREY, anchor="mm")
    text(d, (W // 2, 150), "浦东新区张杨路 501 号", F(FONT_CN, 19), anchor="mm")
    text(d, (W // 2, 186), "TEL: 021-5835 2200", fm, anchor="mm")
    for i in range(4):
        text(d, (W // 2, 224 + i * 34), "-" * 30, fm, fill=GREY, anchor="mm")

    text(d, (40, 372), "流水号: 20261025-0937", pick("流", 20))
    text(d, (40, 404), "时  间: 2026-10-25 09:37:12", pick("时", 20))
    text(d, (40, 436), "收银员: 07  机号: 03", pick("收", 20))

    items = [("维达抽纸 3层*6包", "1", "29.90"),
             ("农夫山泉 550ml*12", "1", "23.80"),
             ("金龙鱼调和油 5L", "1", "69.90"),
             ("伊利纯牛奶 250ml*16", "2", "56.00"),
             ("散装鸡蛋", "1.32kg", "15.84")]
    y = 500
    for name, qty, amt in items:
        text(d, (40, y), name, F(FONT_CN, 22))
        text(d, (W - 40, y), amt, fm, anchor="ra")
        text(d, (W - 130, y), qty, fm, fill=GREY, anchor="ra")
        y += 54
    for i in range(2):
        text(d, (W // 2, y + i * 34), "-" * 30, fm, fill=GREY, anchor="mm")
    y += 96

    # 埋点：29.90+23.80+69.90+56.00+15.84 = 195.44，但"合计"打成 200.44（多 5 元）
    text(d, (40, y), "件数合计: 6", pick("件", 22))
    text(d, (40, y + 46), "商品合计:", F(FONT_CN, 24))
    text(d, (W - 40, y + 46), "200.44", fm, anchor="ra")
    text(d, (40, y + 96), "优惠折扣:", F(FONT_CN, 24))
    text(d, (W - 40, y + 96), "-0.00", fm, anchor="ra")
    text(d, (40, y + 152), "应 收:", F(FONT_CN_B, 30))
    text(d, (W - 40, y + 152), "200.44", F(FONT_MONO, 32), anchor="ra")
    text(d, (40, y + 210), "实 收: 200.44      找零: 0.00", pick("实", 21))
    text(d, (40, y + 250), "支付方式: 微信支付", F(FONT_CN, 21))

    text(d, (W // 2, H - 150), "谢谢光临  请保留小票", F(FONT_CN, 22), fill=GREY, anchor="mm")
    text(d, (W // 2, H - 110), "发票请于 30 日内至服务台开具", F(FONT_CN, 18), fill=GREY, anchor="mm")
    img.save(OUT / "cn_receipt_tall.png")
    return img, {"kind": "零售收银小票", "doc_no": "20261025-0937（仅流水号）",
                 "date": "2026-10-25", "party": "无供应商字段，只有店名",
                 "seller": "家乐福便利超市（店名，非工商全称）",
                 "total": 200.44,
                 "check": "明细 29.90+23.80+69.90+56.00+15.84=195.44 ≠ 应收 200.44，**故意错 5 元**"}


# ---------------------------------------------------------------- ④ 装箱单（无价格）
def packing_list() -> tuple[Image.Image, dict]:
    W, H = 1700, 1500
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    text(d, (90, 70), "PACKING LIST", font=F(FONT_CN_B, 46))
    text(d, (90, 130), "装 箱 单", font=F(FONT_CN, 32), fill=GREY)
    d.line([90, 186, W - 90, 186], fill=INK, width=3)
    f21 = F(FONT_CN, 21)
    text(d, (90, 220), "Shipper 发货人：ZHEJIANG SUNRISE IMP. & EXP. CO., LTD.", f21)
    text(d, (90, 258), "Consignee 收货人：AL-FAHIM TRADING LLC, DUBAI, U.A.E.", f21)
    text(d, (90, 296), "Invoice No. 发票号：SR-2026-1088      Date 日期：2026-10-20", f21)
    text(d, (90, 334), "Container No. 柜号：MSCU 7741820      装运港：NINGBO → JEBEL ALI", f21)

    rows = [["Carton No.", "Description 品名", "Qty(pcs) 数量", "N.W.(kg)", "G.W.(kg)", "Meas.(m³)"],
            ["1-40", "LED PANEL LIGHT 36W", "1,600", "480.0", "512.0", "3.20"],
            ["41-95", "LED BULB 9W E27", "4,400", "352.0", "378.4", "4.75"],
            ["96-120", "LED STRIP 5M 12V", "1,000", "210.0", "228.5", "2.10"],
            ["121-128", "DRIVER 60W IP67", "320", "96.0", "104.0", "1.02"]]
    colw = [190, 480, 260, 190, 190, 200]
    ys, ybot = grid(d, 90, 390, colw, rows, 82, 21)

    text(d, (90, ybot + 34), "TOTAL CARTONS: 128    TOTAL QTY: 7,320 PCS", font=F(FONT_CN_B, 26))
    text(d, (90, ybot + 80), "TOTAL N.W.: 1,138.00 KGS      TOTAL G.W.: 1,222.90 KGS", font=F(FONT_CN_B, 26))
    text(d, (90, ybot + 140), "备注：本单仅列数量与重量，不含单价与金额（装箱单不承载价格信息）。",
         F(FONT_CN, 20), fill=GREY)
    text(d, (90, H - 180), "Signed 签字：______________", f21)
    text(d, (760, H - 180), "Date 日期：2026-10-20", f21)
    text(d, (90, H - 110), "SHIPPING MARK: AL-FAHIM / DUBAI / C/NO.1-128 / MADE IN CHINA", F(FONT_MONO, 20), fill=GREY)
    img.save(OUT / "en_packing_list.png")
    return img, {"kind": "装箱单", "doc_no": "SR-2026-1088（发票号，无装箱单号）",
                 "date": "2026-10-20", "party": "AL-FAHIM TRADING LLC（收货人）",
                 "seller": "ZHEJIANG SUNRISE IMP. & EXP. CO., LTD.（发货人）",
                 "total": None, "check": "**没有金额列**——正确行为是 total/price/amount 全 null 且不硬凑"}


# ---------------------------------------------------------------- ⑤ 对账单（多金额列）
def statement() -> tuple[Image.Image, dict]:
    W, H = 1900, 1560
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    text(d, (W // 2, 76), "应 收 账 款 对 账 单", font=F(FONT_CN_B, 50), anchor="mm")
    text(d, (W // 2, 130), "STATEMENT OF ACCOUNT", font=F(FONT_MONO, 20), fill=GREY, anchor="mm")
    f21 = F(FONT_CN, 21)
    text(d, (80, 190), "客户名称：苏州立信电子科技有限公司", f21)
    text(d, (1080, 190), "对账期间：2026-09-01 至 2026-09-30", f21)
    text(d, (80, 228), "截止日期：2026-09-30", f21)
    text(d, (1080, 228), "对账单号：DZ-20260930-11", f21)

    rows = [["序号", "日期", "单据号", "摘要", "本期应收", "本期已收", "余额"],
            ["1", "2026-09-03", "DN20260903-02", "销售货款", "12,400.00", "0.00", "212,400.00"],
            ["2", "2026-09-11", "DN20260911-05", "销售货款", "8,650.00", "0.00", "221,050.00"],
            ["3", "2026-09-18", "SK20260918-01", "收到货款(电汇)", "0.00", "50,000.00", "171,050.00"],
            ["4", "2026-09-25", "DN20260925-09", "销售货款", "33,900.00", "0.00", "204,950.00"]]
    colw = [90, 220, 330, 400, 250, 250, 250]
    ys, ybot = grid(d, 80, 290, colw, rows, 84, 21)

    # 埋点：期初 200000 + 本期应收(12400+8650+33900=54950) - 已收 50000 = 204950 ✓
    #       但"期末余额"写成 205,950（多 1000）
    text(d, (80, ybot + 34), "期初余额：￥200,000.00", F(FONT_CN_B, 26))
    text(d, (700, ybot + 34), "本期应收合计：￥54,950.00", F(FONT_CN_B, 26))
    text(d, (80, ybot + 86), "本期已收合计：￥50,000.00", F(FONT_CN_B, 26))
    text(d, (1100, ybot + 86), "期末余额：￥205,950.00", F(FONT_CN_B, 28))
    text(d, (80, ybot + 160), "核对说明：如与贵司账面不符，请于收到本单 7 日内回函确认，逾期视为无异议。",
         F(FONT_CN, 20), fill=GREY)
    text(d, (80, H - 170), "制单：李静        复核：周涛        日期：2026-10-08", f21)
    stamp(img, (1520, ybot + 90), "宁波恒力紧固件有限公司财务专用章", 92)
    img.save(OUT / "cn_statement.png")
    return img, {"kind": "应收账款对账单", "doc_no": "DZ-20260930-11", "date": "2026-09-30",
                 "party": "客户：苏州立信电子科技有限公司（付款方）",
                 "seller": "宁波恒力紧固件有限公司（债权方/供货方）",
                 "total": 205950.00,
                 "check": "200000+54950-50000=204950 ≠ 期末 205950，**故意错 1000 元**；且有 4 个金额列，易触发多总额告警"}


CASES = [("cn_delivery_note", delivery_note), ("cn_outbound_note", outbound_note),
         ("cn_receipt_tall", receipt_tall), ("en_packing_list", packing_list),
         ("cn_statement", statement)]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    import json
    meta = {}
    for name, fn in CASES:
        img, m = fn()
        meta[name] = m
        print(f"{name}.png  {img.size}  [{m['kind']}] {m['check'][:50]}")
    (OUT / "expected.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}")


if __name__ == "__main__":
    sys.exit(main())
