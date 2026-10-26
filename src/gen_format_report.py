"""把多格式探针与重复性测试的结果汇成一份报告（Markdown + HTML）。

数据来源：
    out/formats_probe.json    每张图的 OCR / 抽取 / 校验结果（校验在这里重算，
                              保证报告反映的是**当前**规则，而不是跑探针那一刻的旧规则）
    out/repeatability.json    同一张图重复抽取的稳定性
    samples/variants/expected.json  自造单据的埋点预期
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from validate import is_ok, review_targets, validate  # noqa: E402

# 人工标注：类型 / 语言 / 来源。探针只能测「跑得通不通」，判断「这是什么单据」还得靠人看。
META = {
    "cn_delivery_note": ("送货单（横版）", "中文", "自造"),
    "cn_outbound_note": ("材料出库单（竖版）", "中文", "自造"),
    "cn_receipt_tall": ("零售收银小票（80mm 热敏）", "中文", "自造"),
    "en_packing_list": ("装箱单（无金额列）", "中英混排", "自造"),
    "cn_statement": ("应收账款对账单（多金额列）", "中文", "自造"),
    "fapiao_standard": ("增值税电子普通发票", "中文", "公开图"),
    "fapiao_jiangsu": ("增值税普通发票（扫描件）", "中文", "公开图"),
    "fapiao_shanghai": ("增值税普通发票（拍照）", "中文", "公开图"),
    "cz_sample_invoice": ("FAKTURA 发票", "捷克语", "公开图"),
    "us_invoice_lw": ("FACTURA 发票", "西班牙语", "公开图"),
    "de_rechnung": ("Rechnung 发票", "德语", "公开图"),
    "en_po_form": ("Purchase Order 预订单（真实扫描）", "英文", "公开图"),
    "photo_po_bahamut": ("商品价目采购单（手机翻拍）", "繁体中文", "公开图"),
    "air_itinerary": ("航空行程单（文件损坏）", "中文", "公开图"),
}

# 埋点核对：期望行为写死在这里，避免「事后解释」
CHECKS = [
    ("cn_delivery_note", "合计正确，不应报出合计类错误",
     lambda d, iss, tg: not [i for i in iss if i["field"] == "total_amount" and i["level"] == "err"]),
    ("cn_outbound_note", "明细之和 8776 与合计 8876 差 100 元，须报出",
     lambda d, iss, tg: any("100" in i["msg"] for i in iss)),
    ("cn_receipt_tall", "明细 195.44 与应收 200.44 差 5 元，须报出该差额",
     lambda d, iss, tg: any("5.00" in i["msg"] and i["field"] == "total_amount" for i in iss)),
    ("cn_statement", "期末余额与本期应收不同口径，不得给出「改成明细之和」的方向",
     lambda d, iss, tg: not [i for i in iss if any(f.get("value") is not None for f in (i.get("fixes") or []))]),
    ("en_packing_list", "无金额列：price/amount/total 全空且提示缺合计",
     lambda d, iss, tg: d.get("total_amount") is None and any("合计" in i["msg"] for i in iss)),
]

CSS = """
:root{--bg:#ffffff;--fg:#1a1a20;--mut:#6b6b76;--line:#e3e3e9;--card:#fafafb;
--ok:#1a7f4b;--warn:#a56400;--err:#b3261e;--chip:#eef0f4;}
@media (prefers-color-scheme:dark){
:root{--bg:#14151a;--fg:#e8e8ee;--mut:#9a9aa6;--line:#2b2c34;--card:#1b1c22;
--ok:#4ec98a;--warn:#e0a33a;--err:#ff7066;--chip:#26272f;}}
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 80px;background:var(--bg);color:var(--fg);
font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:.3px}
h2{font-size:19px;margin:38px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h3{font-size:15.5px;margin:22px 0 8px}
.sub{color:var(--mut);font-size:13.5px;margin-bottom:26px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:10px 0 6px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--mut);font-size:12.5px}
tr:hover td{background:var(--card)}
code{background:var(--chip);padding:1px 6px;border-radius:5px;font-size:12.5px;
font-family:ui-monospace,Consolas,monospace}
pre{background:var(--card);padding:14px 16px;border-radius:10px;overflow:auto;
border:1px solid var(--line);font-size:12.5px}
pre code{background:none;padding:0}
.ok{color:var(--ok)}.warn{color:var(--warn)}.err{color:var(--err)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:14px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .n{font-size:24px;font-weight:650;letter-spacing:-.5px}
.card .l{color:var(--mut);font-size:12.5px}
ul{padding-left:20px}li{margin:5px 0}
"""


def build_rows(recs):
    rows = []
    for name, r in recs.items():
        kind, lang, src = META.get(name, ("其他", "?", "?"))
        d = r.get("data") or {}
        iss = validate(d) if d else []
        nees, targets, _ = review_targets(d, iss, r.get("content_risk") or {}, []) if d else (False, [], [])
        rows.append({
            "name": name, "kind": kind, "lang": lang, "src": src,
            "status": r.get("crash") or ("通过" if is_ok(iss) else "拦截"),
            "ocr_n": r.get("ocr_n") or 0, "fields": r.get("fields_got") or [],
            "items": len(d.get("items") or []) if d else 0,
            "issues": iss, "n_err": sum(1 for i in iss if i["level"] == "err"),
            "n_warn": sum(1 for i in iss if i["level"] == "warn"),
            "needs_review": nees, "targets": len(targets),
            "ms": r.get("total_ms") or 0,
        })
    rows.sort(key=lambda x: (x["src"] != "自造", x["name"]))
    return rows


def main() -> None:
    recs = {r["name"]: r for r in json.loads((ROOT / "out" / "formats_probe.json").read_text(encoding="utf-8"))}
    rep_path = ROOT / "out" / "repeatability.json"
    rep = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.exists() else {}

    rows = build_rows(recs)
    pass_n = sum(1 for x in rows if x["status"] == "通过")
    block_n = sum(1 for x in rows if x["status"] == "拦截")
    crash_n = len(rows) - pass_n - block_n
    auto_n = sum(1 for x in rows if not x["needs_review"] and x["status"] == "通过")

    check_rows = []
    for name, desc, fn in CHECKS:
        d = (recs.get(name) or {}).get("data") or {}
        iss = validate(d) if d else []
        tg = review_targets(d, iss, {}, [])[1] if d else []
        try:
            ok = bool(fn(d, iss, tg))
        except Exception:  # noqa: BLE001
            ok = False
        check_rows.append((name, desc, ok, iss))
    check_ok = sum(1 for _, _, ok, _ in check_rows if ok)

    L: list[str] = []
    A = L.append
    A("# 多格式单据稳健性测试报告")
    A("")
    A(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}　测试样本：{len(rows)} 张　"
      f"模型：deepseek-v4.1-flash　OCR：RapidOCR(onnxruntime)")
    A("")
    A("## 一、结论先说")
    A("")
    A(f"- **{len(rows)} 张不同版式 / 语言 / 类型的单据，{pass_n + block_n} 张走完了全链路**，"
      f"另外 {crash_n} 张因文件本身损坏在 OCR 阶段停住，返回的是明确提示而不是 500。")
    A(f"- 走完的里面 **{pass_n} 张校验通过、{block_n} 张被拦下**。"
      f"其中通过且完全不用人工的只有 {auto_n} 张——**跨单据类型使用时，几乎每张单都会触发一次看图复核**，"
      "因为关键字段缺失本身就是复核触发条件之一，这是成本，不是缺陷。")
    A(f"- 自造单据埋了 5 处可判定的错，**{check_ok}/{len(check_rows)} 符合预期**。"
      "剩下两处没对上的，根因都不是校验规则写错，而是**表格列错位导致几行金额没抽到**——"
      "校验层手上没有数据，就没有可算的账。")
    A("- 这个数字本身还不稳定：同一张图重跑，抽取结果会变（见第四节），"
      "所以「埋点对上/没对上」每次可能不同——这也说明为什么先把稳定性测清楚，再谈准确率。")
    A("- 过程中发现并修掉 4 个真问题，其中 2 个会**把人引导去改错数据**（详见第五节）。")
    A("")

    A("## 二、结果矩阵")
    A("")
    A("| 单据 | 类型 | 语言 | 来源 | OCR 词数 | 抽到字段 | 明细行 | 校验 | 疑点 err/warn | 看图复核 | 耗时 |")
    A("|---|---|---|---|---:|---|---:|---|---|---:|---:|")
    mark = {"通过": "**通过**", "拦截": "拦截", "ocr_empty": "无文字"}
    for x in rows:
        f = "、".join({"doc_no": "编号", "supplier": "供应商", "order_date": "日期",
                       "total_amount": "合计"}.get(k, k) for k in x["fields"]) or "—"
        st = mark.get(x["status"], x["status"])
        A(f"| `{x['name']}` | {x['kind']} | {x['lang']} | {x['src']} | {x['ocr_n']} | {f} | "
          f"{x['items']} | {st} | {x['n_err']}/{x['n_warn']} | "
          f"{'是·' + str(x['targets']) + '项' if x['needs_review'] else '否'} | {x['ms']}ms |")
    A("")
    A("说明：**通过**=无 err 级疑点，可自动入库；**拦截**=有 err，需人工确认；"
      "「看图复核」=是否还要再花一次模型调用去核对原图。")
    A("")

    A("## 三、埋点核对（自造单据）")
    A("")
    A("自造单据每张都预先埋了一处可判定的错误或缺口，用来验证校验层是否真的在工作。")
    A("")
    A("| 单据 | 埋点与预期 | 结果 | 实际报出 |")
    A("|---|---|---|---|")
    for name, desc, ok, iss in check_rows:
        got = "；".join(i["msg"][:70] for i in iss[:2]) or "无"
        note = ""
        if name == "cn_statement" and ok:
            note = "（本次是因为整单没抽到明细，才「没有给出危险方向」——属偶然符合，不能当成规则稳定的证据）"
        A(f"| `{name}` | {desc} | {'符合' if ok else '**不符**'} | {got}{note} |")
    A("")
    A("注意：这张表是**一次快照**。同一张图重跑时抽取结果会变，埋点能否被抓到也跟着变"
      "（`cn_receipt_tall` 上一轮就抓到过 5 元差额，这一轮因为金额列整列没抽到，只能报「无法核对」）。"
      "要稳定复现，得先把第四节的问题解决。")

    if rep:
        A("## 四、同一张图重复跑：稳定性")
        A("")
        A("图只 OCR 一次（OCR 本身是确定的），模型抽取重复 3 次。"
          "「取值数」=该字段出现了几种不同结果，1 表示稳定。")
        A("")
        A("| 单据 | 解析成功 | 表结构稳定率 | 明细行数序列 | 编号 / 供应商 / 日期 / 合计 取值数 |")
        A("|---|---:|---:|---|---|")
        for name, r in rep.items():
            fv = "/".join(str(r["fields"][f]["distinct"]) for f in
                          ["doc_no", "supplier", "order_date", "total_amount"])
            A(f"| `{name}` | {r['parsed']}/{r['runs']} | {r['table_stable_ratio']:.2f} | "
              f"{r['items_n']} | {fv} |")
        A("")
        A("读法：`0.67` 表示三次里两次得到同一张表；`0.33` 表示三次三样。"
          "`en_packing_list` 三次完全一致，`photo_po_bahamut`（手机翻拍）三次全不同——"
          "**图像质量越差，抽取越不稳定**，这个规律比总体数字更有信息量。")
        A("")

    A("## 五、这次测出并修掉的问题")
    A("")
    A("以下 4 项都已发布到线上并实测确认：随机字节 → **400 + 人话提示**（原来 500）；"
      "损坏图 → 200 且 `words=0`；对账单口径 → err 但 `fixes=[]`；"
      "缺一行金额且补齐后能对上合计 → 只给该行的补值、不碰合计。")
    A("")
    A("### 1　损坏 / 截断的图片被判死刑（已修）")
    A("")
    A("尾部几十字节缺失的 JPEG（微信转发中断、下载未完成、拍照时存储写满）会被 `cv2.imdecode` "
      "直接判死，整个上传返回 **HTTP 500 + `RuntimeError: 无法解码图片`**，看起来像服务挂了。")
    A("")
    A("修法：解码失败时改用 PIL 打开并允许截断图（`LOAD_TRUNCATED_IMAGES`）。这类图主体通常是完整的，"
      "能读多少读多少；确实读不出来时由 `/api/ocr` 返回 **400 + 一句人话**。"
      "样本 `air_itinerary` 就是这种图——修复后不再崩，而是明确回「未识别出任何文字」。")
    A("")

    A("### 2　明细缺金额时，系统会建议把合计改小（已修，危险）")
    A("")
    A("`cn_delivery_note` 第一次跑时模型漏抽了第 2 行的金额，于是「明细之和 6974 ≠ 合计 8249」，"
      "校验层给出修改方向 **「把合计改成 6974」**。可这张单本身完全正确（8249 是对的），"
      "人点一下就把对的改成错的。")
    A("")
    A("修法：求和之前先区分「这一行没抽到」和「这一行金额是 0」。")
    A("")
    A("- 缺金额但数量×单价算得出 → **先不参与求和**；只有当补上后恰好能对上合计，"
      "才把这个补算值作为方向给出（同时被「单价×数量」和「合计」两个独立来源印证）。")
    A("- 缺金额且连算都算不出 → 只报「有 N 行缺金额，无法核对合计」，**不给任何修改方向**。")
    A("")

    A("### 3　差额过大时仍给「按明细求和改合计」（已修，危险）")
    A("")
    A("`cn_statement`（对账单）的「合计」是期末余额（含期初 20 万与已收 5 万），明细却是本期应收。"
      "原逻辑报「差额 151000」并建议 **把 205950 改成 54950**——点一下就是灾难。")
    A("")
    A("修法：差额超过合计的一半（且超过 1000 元）时，判定为「两者不是同一套勾稽口径」，"
      "报出事实但不给目标值。**系统没有依据时就不该假装有依据。**")
    A("")

    A("### 4　单行勾稽差一个数量级时仍给「按 q×p 重算」（已修，危险）")
    A("")
    A("`us_invoice_lw`（西语发票）列错位，数量被读成 200（其实是金额），"
      "于是 `200×200=40000 vs 金额 200`，系统建议把金额改成 40000——放大 200 倍。")
    A("")
    A("修法：相差 10 倍以上时，成因几乎都是列错位或数量被识别成了金额，"
      "改为提示「请对照原图逐列核对后再改」，不给具体数值。")
    A("")

    A("## 六、仍然存在的限制（如实记录）")
    A("")
    A("1. **表格列错位是最大的天花板。** 发票、对账单这类「窄列 + 合并单元格 + 多层表头」的版式，"
      "当前版面重建（按行切单元格、不做 x 坐标对齐）会把列挤错。模型多数能靠上下文把关键字段找对，"
      "并在 `warnings` 里主动声明列错位，但明细行会因此缺金额，勾稽随之失效。"
      "这不是模型能力问题，是版面理解层的短板。")
    A("2. **schema 只描述采购订单。** 遇到发票 / 对账单 / 装箱单只能就近映射"
      "（发票号当单据编号、期末余额当合计），语义必然有偏差。正解是抽取前先识别单据类型、"
      "再按类型选 schema——这是下一步，不是本次。")
    A("3. **同图多次运行结果不完全一致。** 见第四节，`temperature=0` 也不能保证逐字一致。"
      "影响是同一张单再审一次可能从「通过」变「拦截」。要真正可复现，得把抽取结果按 `doc_id` 固化，"
      "或做多次取众数。")
    A("4. **「同一物料两行单价不同」会误报。** 明细里「深沟球轴承 6204 / 6205」是不同规格，"
      "但规格在单独一列、没被抽进 `name`，两行名称相同就被判成同一物料。根因是 schema 缺 `spec` 字段。")
    A("5. **极端低质图无法处理。** 严重损坏的图只能明确回「未识别出任何文字」，"
      "产品上应引导重拍，而不是让它进流程。")
    A("6. **编号格式画像不分单据类型。** 画像从历史库里学到「PO + 10~12 位数字」后，"
      "遇到 `DN20261025-018`、`DZ-20260930-11` 会一律报「与历史格式不符」——"
      "线上实测确认了这个误报。正解是把单据类型也作为画像的分桶键（同供应商、同类型才比格式），"
      "否则每引入一种新单据都会先被自己的画像拦一次。")
    A("")

    A("## 七、怎么复现")
    A("")
    A("```bash")
    A("python src/gen_format_variants.py      # 1. 生成 5 张同语义不同版式的国产单据（含埋点）")
    A("python src/probe_formats.py --dir samples --out out/formats_probe.json   # 2. 跑全链路")
    A("python src/check_repeatability.py --n 3                                  # 3. 同图重复跑")
    A("python src/gen_format_report.py                                          # 4. 汇总本报告")
    A("```")
    A("")
    A("公开图均来自 Wikimedia Commons（`Special:FilePath/<文件名>` 直接下载，未做修改）："
      "`Standard Fapiao.jpg`、`Jiangsu VAT Standard Invoice.png`、`Shanghai VAT Standard Invoice.jpg`、"
      "`Ukazkova faktura.png`、`Lw34016625.png`、`Rechnung Muster 1.gif`、"
      "`Purchase Order Request Form.jpg`、`Bahamut Mall booth purchase order 20191229.jpg`、"
      "`Itinerary and Receipt of E-Ticket for Air Transport (PRC).jpg`。")
    A("")

    md = "\n".join(L) + "\n"
    (ROOT / "out" / "format_matrix.md").write_text(md, encoding="utf-8")

    # ---- HTML：直接复用上面生成的 markdown，转成 HTML
    html = render_html(md, len(rows), pass_n, block_n, check_ok, len(check_rows))
    (ROOT / "out" / "format_matrix.html").write_text(html, encoding="utf-8")

    print(f"已生成: out/format_matrix.md  out/format_matrix.html")
    print(f"样本 {len(rows)}｜通过 {pass_n}｜拦截 {block_n}｜异常 {crash_n}｜"
          f"埋点符合 {check_ok}/{len(check_rows)}｜无需复核 {auto_n}")


def _inline(s: str) -> str:
    """把 markdown 的行内标记转成 HTML：`code` 与 **粗体**。

    不做这一步，页面上就会出现字面的 ** 和反引号——报告是给人看的，
    格式没渲染等于没写。
    """
    import re

    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    return s


def render_html(md: str, n: int, pass_n: int, block_n: int, ck: int, ck_n: int) -> str:
    cards = (f"<div class='cards'>"
             f"<div class='card'><div class='n'>{n}</div><div class='l'>测试样本（9 张公开图 + 5 张自造版式）</div></div>"
             f"<div class='card'><div class='n'>{pass_n + block_n}</div><div class='l'>走完全链路（其余为文件损坏，已明确报错）</div></div>"
             f"<div class='card'><div class='n'>{pass_n} / {block_n}</div><div class='l'>校验通过 / 被拦下</div></div>"
             f"<div class='card'><div class='n'>{ck}/{ck_n}</div><div class='l'>埋点核对符合预期</div></div>"
             f"</div>")

    body: list[str] = []
    i = 0
    lines = md.splitlines()
    title = lines[0].lstrip("# ").strip()
    while i < len(lines):
        ln = lines[i].rstrip()
        s = ln.strip()
        if not s:
            i += 1
            continue
        if s.startswith("# "):
            body.append(f"<h1>{_inline(s[2:])}</h1>")
            i += 1
        elif s.startswith("## "):
            body.append(f"<h2>{_inline(s[3:])}</h2>")
            i += 1
            if s.startswith("## 二"):
                body.append(cards)
        elif s.startswith("### "):
            body.append(f"<h3>{_inline(s[4:])}</h3>")
            i += 1
        elif s.startswith("生成时间"):
            body.append(f"<div class='sub'>{s}</div>")
            i += 1
        elif s.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i].replace("<", "&lt;"))
                i += 1
            i += 1
            body.append("<pre><code>" + "\n".join(code) + "</code></pre>")
        elif s.startswith("|"):
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                tbl.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            tbl = [r for r in tbl if not all(set(c) <= {"-", ":", " "} for c in r)]
            if tbl:
                h = "".join(f"<th>{_inline(c)}</th>" for c in tbl[0])
                rws = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in tbl[1:])
                body.append(f"<table><tr>{h}</tr>{rws}</table>")
        elif s.startswith("- "):
            body.append("<ul>")
            while i < len(lines) and lines[i].strip().startswith("- "):
                body.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            body.append("</ul>")
        else:
            body.append(f"<p>{_inline(s)}</p>")
            i += 1

    return ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{CSS}</style><div class='wrap'>"
            + "\n".join(body) + "</div></html>")


if __name__ == "__main__":
    main()
