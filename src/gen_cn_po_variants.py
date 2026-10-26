"""生成中文采购 / 到货单据，覆盖「版式变异 × 介质退化」。

为什么单独写这个：HF 那套 `Company-document-dataset-v2` 业务域是对的（采购订单 + 收货单），
但语言只有英语和法语。国内制造业的采购单有自己的固定套路——大写金额、交货日期、
"供应商确认 / 采购签字"两栏、印章压在金额上——这些英法单据里没有。

公开渠道拿不到中文企业采购单据原件：这类文件含供应商报价与商业条款，不会公开。
所以只能造，但要造得像真的：版式照国内 ERP（用友 / 金蝶 / SAP 中文模板）的打印样式，
介质则做退化（传真件、复印件），因为线上真实遇到的常常不是干净扫描。

四张：
    cn_po_standard  标准采购订单（A4 竖版，5 列，金额自洽）        —— 应通过
    cn_po_tax       含税采购订单（8 列 + 税额 / 价税合计）         —— 测含税口径
    cn_grn          收货单（订购 / 实收 / 拒收，**无金额列**）      —— 测无价单据
    cn_po_fax       标准采购订单的传真件（发黑 / 条纹 / 低对比）    —— 测介质退化

用法：python src/gen_cn_po_variants.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gen_format_variants import (  # noqa: E402
    FONT_CN, FONT_CN_B, FONT_MONO, GREY, INK, LINE, F, grid, pick, stamp, text)

OUT = ROOT / "samples" / "variants"
W, H = 1240, 1754          # A4 @150dpi


def _page() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (W, H), "white")
    return img, ImageDraw.Draw(img)


def _header(d, title: str, en: str, company: str) -> None:
    text(d, (72, 56), company, F(FONT_CN_B, 26))
    text(d, (72, 92), "地址：宁波市鄞州区姜山镇工业园区 8 号     电话：0574-8823 6600     "
                     "传真：0574-8823 6601", F(FONT_CN, 13), fill=GREY)
    d.line([72, 118, W - 72, 118], fill=RED, width=3)
    text(d, (W // 2, 168), title, font=F(FONT_CN_B, 34), anchor="mm")
    text(d, (W // 2, 202), en, font=F(FONT_MONO, 12), fill=GREY, anchor="mm")


RED = (192, 32, 36)
LINE = (60, 60, 64)


def _grid_cn(d, x0, y0, colw, rows, rowh, fs):
    """中文单据用的表格。

    不复用 gen_format_variants.grid 的原因：那个用 pick() 按内容选字体，
    纯数字会落到 Consolas——它的零是**斜杠零（Ø）**，RapidOCR 会把带斜线的
    0 读成 8：数量 200 被识别成 208，于是 208×12.5=2600 ≠ 金额 2500，
    校验层报"勾稽不通过"。查了原始 OCR 词才定位到是字体字形问题，
    不是模型读错。真实中文采购单的数字与汉字同字体（宋体/黑体），没有斜杠零，
    所以这里统一用微软雅黑，字号也放大到接近 10pt。
    """
    n = len(colw)
    ys, y = [], y0
    for ri, row in enumerate(rows):
        ys.append(y)
        for ci in range(n):
            cx = x0 + sum(colw[:ci])
            d.rectangle([cx, y, cx + colw[ci], y + rowh], outline=LINE, width=2)
            f = F(FONT_CN_B if ri == 0 else FONT_CN, fs)
            d.text((cx + colw[ci] // 2, y + rowh // 2), str(row[ci]),
                   font=f, fill=INK, anchor="mm")
        y += rowh
    d.rectangle([x0, y0, x0 + sum(colw), y], outline=LINE, width=3)
    return ys, y


# ---------------------------------------------------------------- ① 标准采购订单
def po_standard() -> tuple[Image.Image, dict]:
    img, d = _page()
    _header(d, "采  购  订  单", "PURCHASE ORDER", "宁波精工机械制造有限公司")

    f13, f14 = F(FONT_CN, 15), F(FONT_CN, 17)
    text(d, (72, 232), "单据编号：", f13)
    text(d, (146, 232), "PO20261025001", F(FONT_MONO, 14))
    text(d, (860, 232), "下单日期：", f13)
    text(d, (930, 232), "2026-10-25", F(FONT_MONO, 14))
    text(d, (72, 262), "供 应 商：", f13)
    text(d, (146, 262), "苏州华瑞精密部件有限公司", f14)
    text(d, (860, 262), "交货日期：", f13)
    text(d, (930, 262), "2026-11-10", F(FONT_MONO, 14))
    text(d, (72, 292), "收货地址：", f13)
    text(d, (146, 292), "宁波市鄞州区姜山镇工业园区 8 号 一号仓库", f14)
    text(d, (860, 292), "采购员：", f13)
    text(d, (922, 292), "李国强", f14)

    rows = [["序号", "物料名称", "规格型号", "单位", "数量", "单价(元)", "金额(元)"],
            ["1", "深沟球轴承", "6204-2RS", "套", "200", "12.50", "2500.00"],
            ["2", "深沟球轴承", "6205-2RS", "套", "150", "16.80", "2520.00"],
            ["3", "骨架油封", "45×65×10", "只", "300", "3.20", "960.00"],
            ["4", "圆柱滚子轴承", "NJ206EM", "套", "80", "45.00", "3600.00"]]
    colw = [64, 210, 200, 70, 100, 140, 160]
    ys, ybot = _grid_cn(d, 72, 336, colw, rows, 54, 19)

    text(d, (72, ybot + 18), "合计金额（大写）：玖仟伍佰捌拾元整", font=F(FONT_CN_B, 18))
    # 货币符号不能用 Consolas 渲染：它没有全角 ￥ 字形，会画成方块
    text(d, (930, ybot + 16), "￥9580.00", font=F(FONT_CN_B, 22))
    text(d, (72, ybot + 58), "备注：按图纸 2026-A17 验收；随货提供材质证明与检验报告，逾期按合同第 7 条处理。",
         F(FONT_CN, 13), fill=GREY)

    text(d, (72, H - 200), "供应商确认（盖章）：", f13)
    text(d, (470, H - 200), "采购签字：", f13)
    text(d, (900, H - 200), "审批：", f13)
    d.line([72, H - 96, W - 72, H - 96], fill=LINE, width=1)
    text(d, (72, H - 88), "本单一式两联：白联-采购部存根   红联-供应商留存", F(FONT_CN, 11), fill=GREY)
    stamp(img, (250, H - 300), "宁波精工机械制造有限公司采购专用章", 78)
    img.save(OUT / "cn_po_standard.png")
    return img, {"kind": "采购订单（标准）", "doc_no": "PO20261025001", "date": "2026-10-25",
                 "party": "苏州华瑞精密部件有限公司（供应商）",
                 "seller": "宁波精工机械制造有限公司（采购方/开单方）",
                 "total": 9580.00,
                 "check": "明细 2500+2520+960+3600=9580，合计正确；应通过"}


# ---------------------------------------------------------------- ② 含税采购订单
def po_tax() -> tuple[Image.Image, dict]:
    img, d = _page()
    _header(d, "采  购  订  单（含税）", "PURCHASE ORDER / VAT INCLUSIVE", "宁波精工机械制造有限公司")

    f13, f14 = F(FONT_CN, 15), F(FONT_CN, 17)
    text(d, (72, 232), "单据编号：", f13)
    text(d, (146, 232), "PO20261025002", F(FONT_MONO, 14))
    text(d, (860, 232), "下单日期：", f13)
    text(d, (930, 232), "2026-10-25", F(FONT_MONO, 14))
    text(d, (72, 262), "供 应 商：", f13)
    text(d, (146, 262), "宁波泰盛钢铁贸易有限公司", f14)
    text(d, (860, 262), "结算方式：", f13)
    text(d, (930, 262), "月结 30 天", f14)
    text(d, (72, 292), "收货地址：", f13)
    text(d, (146, 292), "宁波市鄞州区姜山镇工业园区 8 号 一号仓库", f14)
    text(d, (860, 292), "税率：", f13)
    text(d, (908, 292), "13%", F(FONT_MONO, 14))

    rows = [["序号", "物料名称", "规格", "单位", "数量", "单价(不含税)", "金额(不含税)"],
            ["1", "45# 钢圆钢", "Φ30", "吨", "2.5", "3200.00", "8000.00"],
            ["2", "45# 钢圆钢", "Φ40", "吨", "0.5", "4000.00", "2000.00"]]
    colw = [64, 210, 150, 70, 100, 170, 180]
    ys, ybot = _grid_cn(d, 72, 336, colw, rows, 54, 19)

    # 合计区：三行，体现「明细之和 = 不含税金额 ≠ 价税合计」
    box_x, box_w = 620, 540
    lines = [("不含税金额合计", "10000.00"), ("税额（13%）", "1300.00"), ("价税合计", "11300.00")]
    y = ybot + 8
    for i, (k, v) in enumerate(lines):
        bold = i == 2
        d.rectangle([box_x, y, box_x + box_w, y + 38], outline=LINE, width=2)
        text(d, (box_x + 16, y + 10), k, F(FONT_CN_B if bold else FONT_CN, 16))
        text(d, (box_x + box_w - 16, y + 9), "￥" + v,
             F(FONT_CN, 18), anchor="ra")
        y += 38

    text(d, (72, ybot + 14), "价税合计（大写）：壹万壹仟叁佰元整", font=F(FONT_CN_B, 17))
    text(d, (72, ybot + 52), "备注：含 13% 增值税专用发票，票到付款；重量以过磅单为准。",
         F(FONT_CN, 13), fill=GREY)

    text(d, (72, H - 200), "供应商确认（盖章）：", f13)
    text(d, (470, H - 200), "采购签字：", f13)
    d.line([72, H - 96, W - 72, H - 96], fill=LINE, width=1)
    stamp(img, (250, H - 300), "宁波精工机械制造有限公司采购专用章", 78)
    img.save(OUT / "cn_po_tax.png")
    return img, {"kind": "采购订单（含税）", "doc_no": "PO20261025002", "date": "2026-10-25",
                 "party": "宁波泰盛钢铁贸易有限公司（供应商）",
                 "seller": "宁波精工机械制造有限公司（采购方/开单方）",
                 "total": 11300.00,
                 "check": "明细 8000+2000=10000=不含税合计；+税额 1300=价税合计 11300。"
                          "合计与明细差 13%，属含税口径，不得建议改成 10000"}


# ---------------------------------------------------------------- ③ 收货单（无金额）
def grn() -> tuple[Image.Image, dict]:
    img, d = _page()
    _header(d, "收  货  单", "GOODS RECEIVED NOTE", "宁波精工机械制造有限公司")

    f13, f14 = F(FONT_CN, 15), F(FONT_CN, 17)
    text(d, (72, 232), "收货单号：", f13)
    text(d, (146, 232), "GR20261025008", F(FONT_MONO, 14))
    text(d, (700, 232), "收货日期：", f13)
    text(d, (770, 232), "2026-10-28", F(FONT_MONO, 14))
    text(d, (72, 262), "供 应 商：", f13)
    text(d, (146, 262), "苏州华瑞精密部件有限公司", f14)
    text(d, (700, 262), "采购单号：", f13)
    text(d, (770, 262), "PO20261025001", F(FONT_MONO, 14))
    text(d, (72, 292), "送货单号：", f13)
    text(d, (146, 292), "DN20261027-033", F(FONT_MONO, 14))
    text(d, (700, 292), "仓管员：", f13)
    text(d, (762, 292), "周慧敏", f14)

    rows = [["序号", "物料名称", "规格型号", "单位", "订购数量", "实收数量", "拒收数量"],
            ["1", "深沟球轴承", "6204-2RS", "套", "200", "200", "0"],
            ["2", "深沟球轴承", "6205-2RS", "套", "150", "150", "0"],
            ["3", "骨架油封", "45×65×10", "只", "300", "296", "4"],
            ["4", "圆柱滚子轴承", "NJ206EM", "套", "80", "80", "0"]]
    colw = [64, 210, 200, 70, 120, 120, 120]
    ys, ybot = _grid_cn(d, 72, 336, colw, rows, 54, 19)

    text(d, (72, ybot + 20), "合计：订购 730 套/只    实收 726    拒收 4",
         font=F(FONT_CN_B, 17))
    text(d, (72, ybot + 58), "拒收原因：第 3 行油封 4 只唇口破损，已现场退回。",
         F(FONT_CN, 13), fill=GREY)
    text(d, (72, ybot + 84), "本单无金额，收货数量以本单为准，结算按采购订单与发票另行处理。",
         F(FONT_CN, 13), fill=GREY)

    text(d, (72, H - 210), "收货单位（签字）：", f13)
    text(d, (470, H - 210), "质检：", f13)
    text(d, (760, H - 210), "供应商送货人：", f13)
    d.line([72, H - 96, W - 72, H - 96], fill=LINE, width=1)
    stamp(img, (300, H - 300), "宁波精工机械制造有限公司收货专用章", 78)
    img.save(OUT / "cn_grn.png")
    return img, {"kind": "收货单", "doc_no": "GR20261025008", "date": "2026-10-28",
                 "party": "苏州华瑞精密部件有限公司（供应商）",
                 "seller": "宁波精工机械制造有限公司（收货方/开单方）",
                 "total": None,
                 "check": "**没有金额列**（只有订购/实收/拒收数量）。"
                          "正确行为：total/price/amount 全空，且不得把数量当金额"}


# ---------------------------------------------------------------- 介质退化
def degrade_fax(img: Image.Image, angle: float = -0.7) -> Image.Image:
    """传真件：动态范围被压缩（白不再白、黑不够黑）、逐行条纹、两端发黑、轻噪声。"""
    a = np.asarray(img.convert("L")).astype(np.float32)
    a = np.clip((a - 30) * 1.55 + 62, 0, 255)                 # 压缩对比、抬底
    rows = np.arange(a.shape[0])
    a *= (1 + 0.045 * np.sin(rows / 2.6))[:, None]            # 行间条纹
    w = a.shape[1]
    edge = np.ones(w, dtype=np.float32)
    edge[: int(w * 0.018)] = 0.35
    edge[-int(w * 0.018):] = 0.35
    a *= edge[None, :]
    a = np.clip(a + np.random.default_rng(7).normal(0, 5.0, a.shape), 0, 255)
    im = Image.fromarray(a.astype(np.uint8)).convert("RGB")
    im = im.rotate(angle, resample=Image.BICUBIC, fillcolor=(245, 245, 245), expand=False)
    im = im.filter(ImageFilter.GaussianBlur(0.7))
    return im


def degrade_photocopy(img: Image.Image) -> Image.Image:
    """复印件：灰底、边缘黑边、轻微重影（印刷网点叠加）。"""
    a = np.asarray(img.convert("L")).astype(np.float32)
    a = np.clip(a * 0.90 + 26, 0, 255)
    ghost = np.roll(a, 2, axis=1) * 0.12
    a = np.clip(a + ghost, 0, 255)
    im = Image.fromarray(a.astype(np.uint8)).convert("RGB")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, im.width - 1, im.height - 1], outline=(70, 70, 72), width=7)
    return im.filter(ImageFilter.GaussianBlur(0.45))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    facts: dict[str, dict] = {}

    std, f0 = po_standard()
    facts["cn_po_standard"] = f0
    _, f1 = po_tax()
    facts["cn_po_tax"] = f1
    _, f2 = grn()
    facts["cn_grn"] = f2

    fax = degrade_fax(std)
    fax.save(OUT / "cn_po_fax.png", quality=52)
    facts["cn_po_fax"] = {"kind": "采购订单（传真件）", "doc_no": "PO20261025001",
                          "date": "2026-10-25",
                          "party": "苏州华瑞精密部件有限公司（供应商）",
                          "seller": "宁波精工机械制造有限公司（采购方/开单方）",
                          "total": 9580.00,
                          "check": "与 cn_po_standard 同内容，但经过传真退化"
                                   "（压缩对比/条纹/边缘发黑/倾斜）：应仍能抽对，"
                                   "若抽错则说明退化超过了 OCR 承受范围"}

    copy = degrade_photocopy(std)
    copy.save(OUT / "cn_po_copy.png", quality=62)
    facts["cn_po_copy"] = {"kind": "采购订单（复印件）", "doc_no": "PO20261025001",
                           "date": "2026-10-25",
                           "party": "苏州华瑞精密部件有限公司（供应商）",
                           "seller": "宁波精工机械制造有限公司（采购方/开单方）",
                           "total": 9580.00,
                           "check": "同内容复印件（灰底/黑边/重影）：与传真件构成介质对照"}

    # 合并进真值文件，别覆盖掉原有 5 张
    exp = OUT / "expected.json"
    old = json.loads(exp.read_text(encoding="utf-8")) if exp.exists() else {}
    old.update(facts)
    exp.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")

    for k, v in facts.items():
        print(f"  {k:18s} {v['kind']:16s} total={v['total']}")


if __name__ == "__main__":
    main()
