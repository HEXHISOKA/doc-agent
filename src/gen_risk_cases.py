"""生成三个「内容风险」演示样本，对应 contentrisk.py 的三类检测。

这三个 case 不是凭空造的，每一个都对应一类真实事故形态：

  1. risk_shadow  影子字段 —— 正文合计 12000，页脚小字 Total: 1200
                  正常单据只有一个总额。出现第二个、而且故意做小字号，
                  赌的就是审核员只看正文、模型只抽到正文。
                  注意本样本勾稽全部通过：明细 2×6000=12000=合计，
                  所以三层校验查不出任何问题，只有这一层会响。

  2. risk_numeric 数字字面量异常 —— 1.200.00 / 100.000 / 1,20,00.00 / 5 000.0 0
                  这些在真实单据上大量来自 OCR 故障（全角数字、紧凑排版），
                  不是攻击，但后果和攻击一样：金额被读错。
                  而且勾稽会一起错、一起自洽，照样通过。

  3. risk_currency 币种错配 —— 金额看着是人民币，单位其实是 USD
                  勾稽查不出（三处都是同一个错单位）。

用法：
    python src/gen_risk_cases.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent.parent / "samples"
CJK = "C:/Windows/Fonts/msyh.ttc"
CJK_B = "C:/Windows/Fonts/msyhbd.ttc"
MONO = "C:/Windows/Fonts/consola.ttf"

W = 1000


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(CJK, size)


class Paper:
    def __init__(self, h: int = 720, bg=(252, 251, 248)):
        self.img = Image.new("RGB", (W, h), bg)
        self.d = ImageDraw.Draw(self.img)
        self.y = 40

    def text(self, s: str, size: int = 26, x: int = 48, bold: bool = False,
             gap: int = 12, color=(28, 28, 30), fp: str | None = None):
        f = font(fp or (CJK_B if bold else CJK), size)
        self.d.text((x, self.y), s, font=f, fill=color)
        self.y += size + gap
        return self

    def right(self, s: str, y: int, size: int = 26, x_right: int = 940,
              color=(28, 28, 30), bold: bool = False):
        f = font(CJK_B if bold else CJK, size)
        w = self.d.textlength(s, font=f)
        self.d.text((x_right - w, y), s, font=f, fill=color)

    def hr(self, gap_before: int = 14, gap_after: int = 14, color=(190, 186, 178)):
        self.y += gap_before
        self.d.line([(48, self.y), (W - 48, self.y)], fill=color, width=1)
        self.y += gap_after
        return self

    def save(self, name: str):
        OUT.mkdir(parents=True, exist_ok=True)
        p = OUT / name
        self.img.save(p)
        print(f"  -> {p.relative_to(OUT.parent)}  {self.img.size}")
        return p


def head(p: Paper, no: str, supplier: str, date: str):
    p.text("XX精密制造有限公司", 30, bold=True, gap=6)
    p.text("采 购 订 单", 24, color=(70, 70, 74), gap=18)
    p.text(f"单据编号：{no}          下单日期：{date}", 20, gap=8)
    p.text(f"供应商：{supplier}", 20, gap=10)


def table(p: Paper, rows: list[tuple[str, str, str, str]]):
    """rows: [(物料, 数量, 单价, 金额)]"""
    p.hr(6, 10)
    y0 = p.y
    p.d.text((48, y0), "物料名称", font=font(CJK_B, 20), fill=(60, 60, 64))
    for x, label in ((420, "数量"), (560, "单价"), (740, "金额")):
        p.d.text((x, y0), label, font=font(CJK_B, 20), fill=(60, 60, 64))
    p.y = y0 + 30
    p.hr(0, 10, (214, 210, 202))
    for name, qty, price, amt in rows:
        y = p.y
        p.d.text((48, y), name, font=font(CJK, 21), fill=(28, 28, 30))
        for x, v in ((420, qty), (560, price), (740, amt)):
            p.d.text((x, y), v, font=font(MONO if v.replace(",", "").replace(".", "").replace(" ", "").isdigit() else CJK, 21),
                     fill=(28, 28, 30))
        p.y = y + 34
    p.hr(6, 12, (214, 210, 202))


def case_shadow():
    """影子字段：正文 12000，页脚小字 1200。勾稽完全通过。"""
    p = Paper(760)
    head(p, "PO20261024007", "宁波恒达塑胶制品有限公司", "2026-10-24")
    table(p, [
        ("改性聚丙烯 PP-GF30", "2", "6000.00", "12000.00"),
        ("橡胶垫圈 DN50", "200", "0.18", "36.00"),
        ("包装耗材", "1", "120.00", "120.00"),
    ])
    y = p.y
    p.d.text((620, y), "合计金额：", font=font(CJK_B, 24), fill=(28, 28, 30))
    p.d.text((760, y), "12156.00", font=font(MONO, 24), fill=(28, 28, 30))
    p.y = y + 46

    # 页脚小字：字号明显小于正文，位置靠下，视觉上像"页脚注释"
    yf = p.y + 60
    p.d.text((48, yf), "备注：本单为框架协议项下订单，交货期以双方确认为准。",
             font=font(CJK, 15), fill=(120, 120, 124))
    p.d.text((700, yf + 2), "Total: 1215.60", font=font(MONO, 16), fill=(120, 120, 124))
    return p.save("risk_shadow.png")


def case_numeric():
    """数字字面量异常：四种格式错乱，勾稽仍可通过（一起错就自洽）。"""
    p = Paper(760)
    head(p, "PO20261024008", "苏州联恒电子科技有限公司", "2026-10-24")
    table(p, [
        ("铝电解电容 470uF", "1000", "1.200.00", "1200.00"),   # 两个小数点
        ("贴片电阻 10K", "500", "0.100000", "50.0000"),          # 小数位异常
        ("PCB 板 4 层", "1,20,00", "10.00", "12000.00"),         # 千分位分组错
        ("辅料", "1", "8.50", "8.50"),
    ])
    y = p.y
    p.d.text((620, y), "合计金额：", font=font(CJK_B, 24), fill=(28, 28, 30))
    # OCR 把 5 位金额的千分位识别成了空格
    p.d.text((770, y), "13258.5 0", font=font(MONO, 24), fill=(28, 28, 30))
    p.y = y + 40
    p.text("（本单为演示样本：金额栏刻意做了多种数字格式异常）", 14,
           color=(140, 140, 144), gap=8)
    return p.save("risk_numeric.png")


def case_currency():
    """币种错配：金额像人民币，单位是 USD。勾稽查不出。"""
    p = Paper(720)
    head(p, "PO20261024009", "Shenzhen Global Sourcing Ltd.", "2026-10-24")
    table(p, [
        ("Connector HX-2000", "500", "12.00", "6000.00"),
        ("Cable Assembly 3m", "300", "8.50", "2550.00"),
    ])
    y = p.y
    p.d.text((560, y), "合计金额：", font=font(CJK_B, 24), fill=(28, 28, 30))
    p.d.text((700, y), "USD 8,550.00", font=font(MONO, 24), fill=(28, 28, 30))
    p.y = y + 44
    p.text("结算方式：T/T 30 天        币种：USD", 18, color=(70, 70, 74), gap=10)
    p.text("All amounts are quoted in US Dollars unless otherwise stated.", 15,
           color=(120, 120, 124), gap=8)
    return p.save("risk_currency.png")


def main():
    print("生成内容风险演示样本：")
    case_shadow()
    case_numeric()
    case_currency()
    print("\n验证：用 check_contentrisk_fp.py --verbose 或直接上传页面看告警。")


if __name__ == "__main__":
    main()
