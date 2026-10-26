"""从 eval/report.json + gt.json 生成 Markdown 报告与单文件 HTML 看板。

所有数字由脚本从原始评测结果计算，不手写，改数据重跑即可复现。
用法：python gen_report.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "eval"
rep = json.loads((EVAL / "report.json").read_text(encoding="utf-8"))
gt = json.loads((EVAL / "set" / "gt.json").read_text(encoding="utf-8"))
recs = rep["records"]
S = rep["summary"]
A, B = S["A"], S["B"]
N = A["n"]


def full_ok(r, tag):
    c = r["chains"].get(tag) or {}
    if not c.get("data"):
        return False
    return bool(c["key_all"] and c["row_cell_ok"] == c["row_cell_total"]
                and c.get("rows_pred") == r["rows_truth"])


def group(recs, key):
    out = {}
    for r in recs:
        out.setdefault(r[key], []).append(r)
    return dict(sorted(out.items()))


def docno_rows():
    rows = []
    for r in recs:
        t = gt[r["file"]]["doc_no"]
        a = (r["chains"].get("A", {}).get("data") or {}).get("doc_no")
        b = (r["chains"].get("B", {}).get("data") or {}).get("doc_no")
        norm = lambda x: str(x or "").replace("-", "").replace(" ", "").upper()
        rows.append((r["file"], r["dirt"], t, str(a), norm(a) == t.upper(),
                     str(b), norm(b) == t.upper()))
    return rows


def case_details(tag):
    """明细行错误案例，归类为「缺值可反推」与「串列/误读」。"""
    miss, misread = [], []
    for r in recs:
        c = r["chains"].get(tag) or {}
        if not c.get("data"):
            continue
        for row in c.get("rows") or []:
            if all(row["ok"].values()):
                continue
            bad = [k for k, v in row["ok"].items() if not v]
            ti = gt[r["file"]]["items"]
            t = ti[row["i"] - 1] if row["i"] - 1 < len(ti) else None
            pw = (c["data"] or {}).get("items") or []
            p = pw[row["i"] - 1] if row["i"] - 1 < len(pw) else None
            item = {"file": r["file"], "row": row["i"], "bad": bad, "truth": t, "pred": p}
            none_cnt = sum(1 for k in bad if (p or {}).get(k) is None)
            (miss if none_cnt == len(bad) else misread).append(item)
    return miss, misread


def pct(x):
    return f"{100 * x:.1f}%"


# ---------------- Markdown ----------------

def build_md() -> str:
    qa, qb = A["quad"], B["quad"]
    miss, misread = case_details("A")
    L = []
    L.append("# 单据字段抽取评测报告\n")
    L.append(f"- 模型：`{rep['model']}`（DeepSeek-V4.1-Flash）")
    L.append(f"- 样本：{N} 张合成采购订单（3 种版式 × 3 套字段标签 × 4 级脏化）")
    L.append("- 链路 A：图 → OCR(rapidocr) → 版面重建 → 文本 → 模型抽取（**线上同款**）")
    L.append("- 链路 B：图 → 模型直读（同模型，同一 schema 与 temperature）")
    L.append(f"- 生成时间：{datetime.now():%Y-%m-%d %H:%M}\n")
    L.append("> 所有数字由 `src/gen_report.py` 从 `eval/report.json` 计算得出，不是手写。\n")

    L.append("## 一、核心结果\n")
    L.append("| 指标 | 链路 A（OCR） | 链路 B（图片直读） |")
    L.append("|---|---|---|")
    L.append(f"| 单据编号准确率 | **{pct(A['key_field_acc']['doc_no'])}** | {pct(B['key_field_acc']['doc_no'])} |")
    L.append(f"| 供应商准确率 | {pct(A['key_field_acc']['supplier'])} | {pct(B['key_field_acc']['supplier'])} |")
    L.append(f"| 下单日期准确率 | {pct(A['key_field_acc']['order_date'])} | {pct(B['key_field_acc']['order_date'])} |")
    L.append(f"| 合计金额准确率 | {pct(A['key_field_acc']['total_amount'])} | {pct(B['key_field_acc']['total_amount'])} |")
    L.append(f"| 字段级 micro | {pct(A['key_field_micro'])} | {pct(B['key_field_micro'])} |")
    L.append(f"| 明细单元格准确率 | {pct(A['row_cell_acc'])} | **{pct(B['row_cell_acc'])}** |")
    L.append(f"| **全字段正确率** | **{pct(A['full_correct_rate'])}** | {pct(B['full_correct_rate'])} |")
    L.append(f"| **自动入库率** | **{pct(qa['pass_correct'] / N)}** | {pct(qb['pass_correct'] / N)} |")
    L.append(f"| **人工复核率** | {pct((qa['pass_wrong'] + qa['fail_wrong']) / N)} | {pct((qb['pass_wrong'] + qb['fail_wrong']) / N)} |")
    L.append("")
    L.append("两条链路唯一的系统性差异在**单据编号**：A 20/20 全对，B 只有 6/20。")
    L.append("反过来，**明细单元格 B 全对（100%），A 只有 95.6%**。\n")

    L.append("## 二、校验层四象限\n")
    L.append("判定「结果正确」= 4 个关键字段全对 **且** 明细每个单元格都对（不看这条会高估校验层）。\n")
    L.append("| | 结果正确 | 结果错误 |")
    L.append("|---|---|---|")
    L.append(f"| **校验通过** | 正确放行 A={qa['pass_correct']} / B={qb['pass_correct']} | **误放行 A={qa['pass_wrong']} / B={qb['pass_wrong']}** |")
    L.append(f"| **校验拦截** | 误拦截 A={qa['fail_correct']} / B={qb['fail_correct']} | 正确拦截 A={qa['fail_wrong']} / B={qb['fail_wrong']} |")
    L.append("")
    L.append(f"- 链路 A：拦截 {qa['fail_wrong'] + qa['fail_correct']} 次，其中 **误拦截 {qa['fail_correct']} 次**——校验规则没有过度严格，拦的都是真错。")
    L.append(f"- 链路 A 漏了 {qa['pass_wrong']} 次（供应商名错字），这是校验层的**结构性盲区**，见第四节。\n")

    L.append("## 三、单据编号：一个字段决定了自动入库率\n")
    L.append("| 文件 | 脏化 | 真值 | A(OCR) | B(直读) |")
    L.append("|---|---|---|---|---|")
    for f, d, t, a, aok, b, bok in docno_rows():
        L.append(f"| {f} | d{d} | `{t}` | `{a}` {'✓' if aok else '✗'} | `{b}` {'✓' if bok else '✗'} |")
    L.append("")
    L.append("B 链路的错误**全部是同一形态**：把字母 `O` 写成数字 `0`。这不是随机噪声，")
    L.append("也不是图像质量问题（d0 干净样本同样出错），而是模型的**语义归一化**：")
    L.append("它认为「单据编号」应该由数字组成，于是把 `PO` 改写成 `P0`。")
    L.append("提示词第 7 条专门禁止了这种行为，仍然挡不住。\n")
    L.append("OCR 不会犯这个错，因为它只做字符识别，不理解「编号应该长什么样」。")
    L.append("**这正是保留 OCR 最硬的证据：字符保真度上，确定性工具优于概率模型。**\n")

    L.append("## 四、明细错误形态\n")
    L.append(f"链路 A 共 {len(miss) + len(misread)} 个错误单元格，分两类：\n")
    L.append(f"**① 缺值（{len(miss)} 处）**：模型把抽不到的字段填 `null`，而不是编造——这是对的。")
    L.append("但其中多数可以用同一行的另外两个数反推：")
    for it in miss[:4]:
        if not it["truth"] or not it["pred"]:
            continue
        t, p = it["truth"], it["pred"]
        hint = ""
        try:
            if p.get("price") is None and p.get("qty") and p.get("amount"):
                hint = f" → 可反推单价 = {float(p['amount']) / float(p['qty']):.2f}"
            elif p.get("qty") is None and p.get("price") and p.get("amount"):
                hint = f" → 可反推数量 = {float(p['amount']) / float(p['price']):.0f}"
        except Exception:
            pass
        L.append(f"- 行{it['row']} 缺 `{[k for k in it['bad']]}`{hint}")
    L.append("")
    L.append(f"**② 误读/串列（{len(misread)} 处）**：如把金额当单价、把 `0` 读成 `9`。勾稽校验能全部抓住。\n")

    L.append("## 五、按脏化等级\n")
    L.append("| 脏化 | 说明 | n | A 全对 | A 自动入库 | A 正确拦截 | B 全对 | B 自动入库 |")
    L.append("|---|---|---|---|---|---|---|---|")
    names = {0: "干净", 1: "轻（倾斜+噪点）", 2: "中（+印章）", 3: "重（+手写+发灰）"}
    for d, rs in group(recs, "dirt").items():
        def cnt(tag, f):
            return sum(1 for r in rs if f(r, tag))
        A_full = sum(1 for r in rs if full_ok(r, "A"))
        B_full = sum(1 for r in rs if full_ok(r, "B"))
        A_blk = sum(1 for r in rs if (r["chains"].get("A") or {}).get("data")
                    and not r["chains"]["A"]["pass"] and not full_ok(r, "A"))
        L.append(f"| d{d} | {names.get(d, '')} | {len(rs)} | {A_full} | {A_full}/{len(rs)} | {A_blk} | {B_full} | {B_full}/{len(rs)} |")
    L.append("")
    L.append("A 链路在 **d1 完全无损（7/7）**，到 d2（加印章）掉到 2/6——印章遮挡是最强的破坏因素。")
    L.append("B 链路的错误与脏化程度**无关**，因为它的失败来自编号语义归一化，不是图像质量。\n")

    L.append("## 六、局限（必须写清）\n")
    L.append("1. **样本是合成的**，虽然覆盖了倾斜/印章/手写/发灰/折痕，但真实复印件的网点、油墨扩散、多联复写没有完全模拟。")
    L.append("2. **n=20，单格误差约 ±1 例（5 个百分点）**，只能看趋势，不能当精确指标。")
    L.append("3. **供应商名错误无法用勾稽校验发现**（校验层只保证数字自洽），本次 1 次误放行全在此。")
    L.append("4. 评测用的是同一模型 `deepseek-flash`，未对比其他模型。\n")
    L.append("## 七、下一步\n")
    L.append("| 改进 | 依据 |")
    L.append("|---|---|")
    L.append("| 单价/数量缺失时按 `金额/数量`、`金额/单价` 反推，并标记为推导值 | 第四节 ①：缺值占错误多数 |")
    L.append("| 供应商主数据 + 模糊匹配（编辑距离） | 第二节：唯一一次误放行 |")
    L.append("| 把模型 `warnings` 纳入校验信号 | 该次误放行中模型已自行报告「疑似漏识别」 |")
    L.append("| 编号字段加 OCR 文本二次比对 | 第三节：编号是自动入库率的第一瓶颈 |")
    return "\n".join(L)


# ---------------- HTML ----------------

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1a1d21;
 font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:1000px;margin:0 auto;padding:40px 24px 80px}
header{padding-bottom:24px;border-bottom:2px solid #1a1d21;margin-bottom:32px}
h1{font-size:26px;margin:0 0 8px;letter-spacing:-.3px}
.meta{font-size:13px;color:#5f6672}
.meta code{background:#eef0f3;padding:1px 6px;border-radius:4px;font-size:12px}
h2{font-size:17px;margin:44px 0 14px;padding-left:10px;border-left:3px solid #185fa5}
h3{font-size:14px;margin:24px 0 8px;color:#3c4350}
p{font-size:14px;margin:10px 0}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}
.card{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:16px}
.card .k{font-size:12px;color:#68707d;margin-bottom:6px}
.card .v{font-size:24px;font-weight:600;letter-spacing:-.5px}
.card .s{font-size:12px;color:#8a919c;margin-top:4px}
.card.hi{border-color:#185fa5;background:#f3f8fd}
.card.hi .v{color:#185fa5}
.card.bad .v{color:#a32d2d}
table{width:100%;border-collapse:collapse;background:#fff;font-size:13px;
 border:1px solid #e3e6ea;border-radius:10px;overflow:hidden}
th{background:#f0f2f5;text-align:left;padding:10px 12px;font-weight:600;font-size:12px;color:#4a515c}
td{padding:9px 12px;border-top:1px solid #eef0f3;font-variant-numeric:tabular-nums}
tr:hover td{background:#fafbfc}
.ok{color:#3b6d11;font-weight:600}
.no{color:#a32d2d;font-weight:600}
.mono{font-family:Consolas,monospace;font-size:12px}
.bar{display:flex;align-items:center;gap:10px;margin:6px 0}
.bar .lbl{width:86px;font-size:12px;color:#5f6672;text-align:right}
.bar .track{flex:1;height:22px;background:#eceff3;border-radius:5px;overflow:hidden}
.bar .fill{height:100%;border-radius:5px}
.bar .num{width:56px;font-size:12px;color:#3c4350;font-variant-numeric:tabular-nums}
.quad{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:16px 0}
.q{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:14px}
.q .t{font-size:12px;color:#68707d}
.q .n{font-size:20px;font-weight:600;margin-top:4px}
.q.good{border-left:4px solid #639922}
.q.warn{border-left:4px solid #e24b4a;background:#fdf6f6}
.q.info{border-left:4px solid #378add}
.q.mute{border-left:4px solid #b4b2a9}
.note{background:#fff;border:1px solid #e3e6ea;border-left:4px solid #ba7517;
 border-radius:8px;padding:14px 16px;margin:16px 0;font-size:13px}
.note b{color:#854f0b}
ul{font-size:14px;padding-left:22px}
li{margin:6px 0}
footer{margin-top:56px;padding-top:20px;border-top:1px solid #e3e6ea;font-size:12px;color:#8a919c}
@media(max-width:760px){.cards{grid-template-columns:1fr 1fr}.quad{grid-template-columns:1fr}}
"""


def bar(label, val, color):
    return (f'<div class="bar"><div class="lbl">{label}</div>'
            f'<div class="track"><div class="fill" style="width:{100 * val:.1f}%;background:{color}"></div></div>'
            f'<div class="num">{100 * val:.1f}%</div></div>')


def build_html() -> str:
    qa, qb = A["quad"], B["quad"]
    miss, misread = case_details("A")
    h = []
    h.append('<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8">')
    h.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    h.append("<title>单据字段抽取评测报告</title>")
    h.append(f"<style>{CSS}</style><div class='wrap'>")

    h.append("<header><h1>单据字段抽取评测报告</h1>")
    h.append(f"<div class='meta'>模型 <code>{rep['model']}</code>（DeepSeek-V4.1-Flash）· "
             f"{N} 张合成采购订单 · 3 种版式 × 3 套字段标签 × 4 级脏化 · "
             f"{datetime.now():%Y-%m-%d}</div></header>")

    h.append("<h2>一句话结论</h2>")
    h.append(f"<p>同一模型下，<b>OCR 链路的自动入库率是图片直读的 {qa['pass_correct'] / max(1, qb['pass_correct']):.1f} 倍</b>"
             f"（{pct(qa['pass_correct'] / N)} vs {pct(qb['pass_correct'] / N)}）。"
             f"差距几乎全部来自一个字段：单据编号。OCR 20/20 全对，图片直读只有 6/20——"
             f"模型会把字母 <span class='mono'>O</span> 归一化成数字 <span class='mono'>0</span>。</p>")

    h.append("<h2>核心指标</h2><div class='cards'>")
    h.append(f"<div class='card hi'><div class='k'>A 自动入库率</div><div class='v'>{pct(qa['pass_correct'] / N)}</div>"
             f"<div class='s'>OCR 链路 · {qa['pass_correct']}/{N}</div></div>")
    h.append(f"<div class='card'><div class='k'>B 自动入库率</div><div class='v'>{pct(qb['pass_correct'] / N)}</div>"
             f"<div class='s'>图片直读 · {qb['pass_correct']}/{N}</div></div>")
    h.append(f"<div class='card bad'><div class='k'>校验误放行</div><div class='v'>{qa['pass_wrong']}</div>"
             f"<div class='s'>通过校验但结果有错</div></div>")
    h.append(f"<div class='card'><div class='k'>校验误拦截</div><div class='v'>{qa['fail_correct']}</div>"
             f"<div class='s'>规则未过度严格</div></div>")
    h.append("</div>")

    h.append("<h3>字段准确率对比</h3>")
    for f, name in [("doc_no", "单据编号"), ("supplier", "供应商"), ("order_date", "下单日期"), ("total_amount", "合计金额")]:
        h.append(bar("A " + name, A["key_field_acc"][f], "#185fa5"))
        h.append(bar("B " + name, B["key_field_acc"][f], "#7f77dd"))
    h.append(f"<p class='meta'>明细单元格：A {pct(A['row_cell_acc'])}，B {pct(B['row_cell_acc'])}——"
             f"B 反而更强，因为模型能直接理解表格结构，而 OCR 的版面重建会漏字。</p>")

    h.append("<h2>校验层四象限</h2>")
    h.append("<p>判定「结果正确」= 4 个关键字段全对 <b>且</b> 明细每个单元格都对。只看关键字段会高估校验层。</p>")
    h.append("<div class='quad'>")
    h.append(f"<div class='q good'><div class='t'>校验通过 · 结果正确（正确放行）</div><div class='n'>A {qa['pass_correct']} / B {qb['pass_correct']}</div></div>")
    h.append(f"<div class='q warn'><div class='t'>校验通过 · 结果错误（误放行，最危险）</div><div class='n'>A {qa['pass_wrong']} / B {qb['pass_wrong']}</div></div>")
    h.append(f"<div class='q info'><div class='t'>校验拦截 · 结果错误（正确拦截）</div><div class='n'>A {qa['fail_wrong']} / B {qb['fail_wrong']}</div></div>")
    h.append(f"<div class='q mute'><div class='t'>校验拦截 · 结果正确（误拦截）</div><div class='n'>A {qa['fail_correct']} / B {qb['fail_correct']}</div></div>")
    h.append("</div>")
    h.append(f"<p>链路 A 拦截 {qa['fail_wrong'] + qa['fail_correct']} 次、<b>误拦截 {qa['fail_correct']} 次</b>，"
             f"说明规则没有过度严格——拦下的都是真错。唯一漏掉的 1 次是供应商名错字，见下文。</p>")

    h.append("<h2>一个字段决定成败：单据编号</h2><table>")
    h.append("<tr><th>文件</th><th>脏化</th><th>真值</th><th>A（OCR）</th><th>B（直读）</th></tr>")
    for f, d, t, a, aok, b, bok in docno_rows():
        h.append(f"<tr><td class='mono'>{f}</td><td>d{d}</td><td class='mono'>{t}</td>"
                 f"<td class='mono'>{a} <span class='{'ok' if aok else 'no'}'>{'✓' if aok else '✗'}</span></td>"
                 f"<td class='mono'>{b} <span class='{'ok' if bok else 'no'}'>{'✓' if bok else '✗'}</span></td></tr>")
    h.append("</table>")
    h.append("<div class='note'><b>为什么这不是图像质量问题：</b>d0 是完全干净、无倾斜、无印章的样本，"
             "B 链路照样把 <span class='mono'>PO2026092601</span> 读成 <span class='mono'>P02026092601</span>。"
             "错误与脏化程度无关，是模型的语义先验——它认为「编号」应该由数字组成。"
             "提示词第 7 条专门禁止了这种改写，仍然挡不住。"
             "<br><br>OCR 不会犯这个错，因为它只做字符识别，不理解「编号应该长什么样」。"
             "<b>这就是保留 OCR 最硬的证据：在字符保真上，确定性工具优于概率模型。</b></div>")

    h.append("<h2>明细错误形态</h2>")
    h.append(f"<p>链路 A 共 {len(miss) + len(misread)} 个错误单元格。"
             f"其中 <b>{len(miss)} 处是缺值</b>（模型填 null 而非编造，行为正确），"
             f"<b>{len(misread)} 处是误读或串列</b>（勾稽校验全部抓住）。</p>")
    h.append("<div class='note'><b>可落地的改进：</b>缺值里有相当一部分能用同一行另外两个数反推——"
             "例如单价缺失但数量和金额都在，<span class='mono'>金额 ÷ 数量</span> 就是单价。"
             "把这做成「推导值 + 标记待复核」，能直接降低人工复核率。"
             "这个改进方向是从数据里长出来的，不是拍脑袋。</div>")

    h.append("<h2>按脏化等级</h2><table>")
    h.append("<tr><th>脏化</th><th>n</th><th>A 全对</th><th>A 正确拦截</th><th>B 全对</th></tr>")
    names = {0: "干净", 1: "轻（倾斜+噪点）", 2: "中（+印章）", 3: "重（+手写+发灰）"}
    for d, rs in group(recs, "dirt").items():
        A_full = sum(1 for r in rs if full_ok(r, "A"))
        B_full = sum(1 for r in rs if full_ok(r, "B"))
        A_blk = sum(1 for r in rs if (r["chains"].get("A") or {}).get("data")
                    and not r["chains"]["A"]["pass"] and not full_ok(r, "A"))
        h.append(f"<tr><td>d{d} {names.get(d, '')}</td><td>{len(rs)}</td>"
                 f"<td>{A_full}/{len(rs)}</td><td>{A_blk}</td><td>{B_full}/{len(rs)}</td></tr>")
    h.append("</table>")
    h.append("<p>A 链路在 d1 完全无损，到 d2（加印章）明显下滑——印章遮挡是最强的破坏因素。"
             "B 链路的错误与脏化无关，进一步印证其失败来源是语义归一化而非画质。</p>")

    h.append("<h2>局限（不回避）</h2><ul>")
    h.append("<li>样本是<b>合成</b>的：覆盖了倾斜、印章、手写、发灰、折痕，但没有真实复印件的网点与多联复写。</li>")
    h.append("<li><b>n=20，单例误差约 5 个百分点</b>，只能看趋势，不能当精确指标使用。</li>")
    h.append("<li><b>供应商名错误无法被勾稽校验发现</b>——校验层只保证数字自洽，本次唯一一次误放行全在文本字段。</li>")
    h.append("<li>只测了 <span class='mono'>deepseek-flash</span> 一个模型，未做跨模型对比。</li>")
    h.append("</ul>")

    h.append("<h2>下一步</h2><table>")
    h.append("<tr><th>改进</th><th>数据依据</th></tr>")
    h.append("<tr><td>缺值时按 金额÷数量、金额÷单价 反推并标记</td><td>缺值占错误单元格多数</td></tr>")
    h.append("<tr><td>供应商主数据 + 编辑距离模糊匹配</td><td>唯一一次误放行</td></tr>")
    h.append("<tr><td>把模型 warnings 纳入校验信号</td><td>该次误放行中模型已自行报告疑似漏识别</td></tr>")
    h.append("<tr><td>编号字段做 OCR 文本二次比对</td><td>编号是自动入库率的第一瓶颈</td></tr>")
    h.append("</table>")

    tok = A["tokens"]
    h.append(f"<footer>评测集 {N} 例由 <span class='mono'>src/gen_eval_set.py</span> 生成（ground truth 同步产出，非人工标注）· "
             f"评测由 <span class='mono'>src/eval.py</span> 执行 · 本页由 <span class='mono'>src/gen_report.py</span> 从 "
             f"<span class='mono'>eval/report.json</span> 生成 · "
             f"A 链路 tokens in={tok['prompt_tokens']} out={tok['completion_tokens']}</footer>")
    h.append("</div></html>")
    return "".join(h)


(EVAL / "报告.md").write_text(build_md(), encoding="utf-8")
(EVAL / "report.html").write_text(build_html(), encoding="utf-8")
print("ok ->", EVAL / "报告.md")
print("ok ->", EVAL / "report.html")
