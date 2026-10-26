"""单据内容风险检测：数字字面量异常 / 影子字段 / 币种错配。

## 为什么不用「检测形近字符」这个思路（实测结论，面试可直接讲）

最初的想法是在 OCR 文本里找 `1OO.OO`（字母 O 冒充数字 0）这类形近字符。
实测（`probe_confusable.py`）证明这条路走不通：

    原图 `单价 1OO.OO  金额 5OOO.OO`
      → OCR `100.00` / `5000.00`     ← 字母痕迹被彻底归一，文本里连 O 都不剩

OCR 的识别模型是「按上下文猜最可能的字符」，看到金额位置就会把 O 读成 0。
所以**形近字符在文本层是完全无痕的**，任何基于文本的检测都是自欺欺人。
这类风险的唯一解法是回看原图像素（前端「可疑字段切片」做的就是这个）。

那文本层还能查什么？换成查「数字字面量本身不合法」——实测这些都会原样保留：

    1.200.00     两个小数点（欧洲式千分位混用）
    100.000      三位小数
    1,20,00.00   千分位分组错位
    5 000.0 0    数字串被空格打断（OCR 把全角「５０００.００」拆碎）

后两类尤其重要：它们不是攻击，是 **OCR 故障**，但后果和攻击一样——
金额被读错，而且勾稽照样能通过（三处一起错就自洽）。故障频率比攻击高几个数量级。

## 影子字段

同一张单据上出现两个「合计」。正常单据只有一个，出现两个要么是排版冗余，
要么是伪造：正文一个真值，页脚小字一个假值，赌审核员只看正文、模型只抽到正文。
实测（`probe_numeric.py` case C）两个值 OCR 都能读到，所以文本层可查。

## 设计立场

和 guard.py 一致：**只告警、不阻断**。
这些信号的作用是「把不该静默的地方变成显式待确认」，不是自动定罪。
`1,20,00.00` 可能只是排版不规范，但它必须被看见——因为审核员不会逐字核对 30 行数字。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 一、数字字面量异常
# ---------------------------------------------------------------------------

# 一个"疑似数字串"：允许数字、逗号、点、空格（空格是 OCR 拆碎数字的痕迹）
# 注意空格只能用 [ \t]，不能用 \s —— \s 含换行会让 token 跨行拼接
_NUM_TOKEN = re.compile(r"\d[\d,.\t ]*\d|\d")

# 数字串被空格打断：两侧都是数字。
# 小数点必须在前段、中段都能出现——「5 000.0 0」的点在中间，
# 「13258.5 0」的点在前段，只写一种顺序会漏掉另一种。
_SPACE_BREAK = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?(?:[ ]+\d+(?:\.\d+)?)+)(?![\d.])")

# 欧洲式千分位 + 小数点：1.200.00
_MULTI_DOT = re.compile(r"^\d{1,3}(?:\.\d{3})+\.\d{2}$")

# 三位及以上小数：100.000
_LONG_DECIMAL = re.compile(r"^\d+\.\d{3,}$")

# 逗号分组不是 3 位：1,20,00.00 / 12,0000.00
_COMMA_GROUP = re.compile(r"^\d{1,3}(?:,\d+)*(?:\.\d+)?$")


def _check_comma_grouping(tok: str) -> str | None:
    """返回问题描述，没问题返回 None。"""
    if not _COMMA_GROUP.match(tok) or "," not in tok:
        return None
    intpart = tok.split(".")[0]
    groups = intpart.split(",")
    if len(groups) < 2:
        return None
    if len(groups[0]) > 3:
        return f"千分位分组错位：首位 {len(groups[0])} 位"
    for g in groups[1:]:
        if len(g) != 3:
            return f"千分位分组错位：出现 {len(g)} 位组"
    return None


def _is_complete_number(seg: str) -> bool:
    """是不是一个"看起来完整"的数值：两位小数，或 4 位以上整数。

    用途：区分「数字被空格吃断」和「表格里两个单元格被空格连起来」。
    后者每一段都是完整数值（4480.00 和 18.00），前者一定有一段是残段
    （5 000.0 0 里的 0；13258.5 0 里的 0）。
    """
    return bool(re.fullmatch(r"\d+\.\d{2}", seg) or re.fullmatch(r"\d{4,}", seg))


def detect_numeric(text: str) -> list[dict]:
    """扫数字字面量异常。返回 [{level, kind, token, desc, context}]"""
    out: list[dict] = []
    seen: set[str] = set()

    # ① 空格打断：先整体匹配，再按段数/位数过滤
    for m in _SPACE_BREAK.finditer(text):
        raw = m.group(1)
        digits = re.sub(r"\D", "", raw)
        segments = [s for s in re.split(r"[ ]+", raw) if s]
        if len(segments) < 2 or len(digits) < 4:
            continue
        # **只有含小数点的才报**。
        # 纯整数的空格分不清是「数字被吃掉的千分位」还是「表格相邻两列」：
        # 实测 po_07_framed_C_d3 里「100 800」是两个单元格的数量与单价，
        # 判成断裂就是误报。而列分隔不会在一串数字中间插小数点，所以带小数点是可靠信号。
        if "." not in raw:
            continue
        # 再排一层：每一段都是完整数值 ⇒ 是表格里并排的两个金额（4480.00 18.00），
        # 或者「数量 + 单价」（200 18.00，首段短整数、其余完整）。断裂的数字一定有残段。
        if all(_is_complete_number(s) for s in segments):
            continue
        if re.fullmatch(r"\d{1,3}", segments[0]) and all(
            _is_complete_number(s) for s in segments[1:]
        ):
            continue
        key = "space:" + raw
        if key in seen:
            continue
        seen.add(key)
        ctx = text[max(0, m.start() - 12) : m.end() + 12].replace("\n", " ")
        out.append(
            {
                "level": "high",
                "kind": "数字被空格打断",
                "token": raw,
                "desc": f"「{raw}」被空格拆成 {len(segments)} 段（{digits}），"
                f"疑为 OCR 把千分位/小数点识别成了空格",
                "context": ctx.strip(),
            }
        )

    # ② 逐 token 查字面量格式
    for m in _NUM_TOKEN.finditer(text):
        tok = m.group(0).strip()
        tok = tok.rstrip(".,")
        if not tok or tok in seen:
            continue

        if _MULTI_DOT.match(tok):
            seen.add(tok)
            out.append(
                {
                    "level": "high",
                    "kind": "多个小数点",
                    "token": tok,
                    "desc": f"「{tok}」含两个小数点，可能是欧洲式千分位与小数点混用，"
                    f"真实值可能是 {tok.replace('.', '', 1)} 或 {tok.replace('.', '', 2)}",
                    "context": text[max(0, m.start() - 12) : m.end() + 12].replace("\n", " "),
                }
            )
            continue

        if _LONG_DECIMAL.match(tok):
            seen.add(tok)
            out.append(
                {
                    "level": "mid",
                    "kind": "小数位数异常",
                    "token": tok,
                    "desc": f"「{tok}」有 {len(tok.split('.')[1])} 位小数，金额通常最多 2 位",
                    "context": text[max(0, m.start() - 12) : m.end() + 12].replace("\n", " "),
                }
            )
            continue

        problem = _check_comma_grouping(tok)
        if problem:
            seen.add(tok)
            out.append(
                {
                    "level": "mid",
                    "kind": "千分位格式错误",
                    "token": tok,
                    "desc": f"「{tok}」{problem}",
                    "context": text[max(0, m.start() - 12) : m.end() + 12].replace("\n", " "),
                }
            )

    return out


# ---------------------------------------------------------------------------
# 二、影子字段（同一语义多个数值）
# ---------------------------------------------------------------------------

# 这些都是「总额」语义。小计/税额/不含税属于不同层级，单独排除，不计入本组。
_ZH_TOTAL = re.compile(r"价税合计|含税合计|合计金额|合\s*计|总金额|总价|总额|总计|应付(?:金额|合计)?")
_EN_TOTAL = re.compile(r"grand\s*total|total\s*amount|\btotal\b", re.I)

# 出现这些词的行整行跳过：它们是合法存在的第二层级数字，不算影子字段
_EXCLUDE_LINE = re.compile(r"小计|不含税|税前|税额|税金|subtotal|tax\b", re.I)

# 从一段文本里取金额：带千分位的分支必须要求「至少一个逗号组」，
# 否则 \d{1,3} 会把 12000.00 截成 120 + 0.0（曾经踩过）
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?")


def _line_amounts(line: str) -> list[float]:
    vals: list[float] = []
    for m in _AMOUNT.finditer(line):
        s = m.group(0).replace(",", "")
        try:
            vals.append(float(s))
        except ValueError:
            continue
    return vals


def detect_shadow(text: str) -> list[dict]:
    """找同一语义的多个数值。

    分三级（信号强度递减）：
      same_key  同一个 key 字样出现两次          → high，正常单据不会
      cross_lang 中文合计与英文 total 同时出现且值不同 → high，典型伪造特征
      zh_multi   中文合计类出现多个不同值         → mid，可能是合计/总计两个合法层级
    """
    out: list[dict] = []
    zh_hits: list[tuple[float, int, str]] = []
    en_hits: list[tuple[float, int, str]] = []
    key_count: dict[str, int] = {}

    for i, line in enumerate(text.split("\n"), 1):
        if not line.strip() or _EXCLUDE_LINE.search(line):
            continue

        m_zh = _ZH_TOTAL.search(line)
        m_en = _EN_TOTAL.search(line)
        if not m_zh and not m_en:
            continue

        amounts = _line_amounts(line)
        if not amounts:
            continue
        # key 在金额前面时取 key 后的**第一个**金额（语序上紧跟 key 的那个），
        # 取最后一个会抓到行尾的噪声数字（如注入句里的「0无」）
        key_end = (m_zh or m_en).end()
        after = _line_amounts(line[key_end:])
        val = after[0] if after else amounts[-1]

        if m_zh:
            key_count["合计(中)"] = key_count.get("合计(中)", 0) + 1
            zh_hits.append((val, i, line.strip()))
        if m_en:
            key_count["total(英)"] = key_count.get("total(英)", 0) + 1
            en_hits.append((val, i, line.strip()))

    # 同名 key 重复
    for k, c in key_count.items():
        if c >= 2:
            out.append(
                {
                    "level": "high",
                    "kind": "同一语义重复出现",
                    "token": k,
                    "desc": f"「{k}」在单据上出现 {c} 次。正常单据的总额只标一次，"
                    f"重复出现需确认哪一个才是有效值",
                    "context": "",
                }
            )

    # 跨语言双合计
    if zh_hits and en_hits:
        zv = {round(v, 2) for v, _, _ in zh_hits}
        ev = {round(v, 2) for v, _, _ in en_hits}
        if zv != ev:
            out.append(
                {
                    "level": "high",
                    "kind": "中英文合计不一致",
                    "token": f"{sorted(zv)} vs {sorted(ev)}",
                    "desc": f"中文合计为 {sorted(zv)}，英文 total 为 {sorted(ev)}，两者不一致。"
                    f"小字号的英文总额常被忽略，是伪造单据的典型手法",
                    "context": " | ".join(
                        [f"L{n}: {s}" for _, n, s in (zh_hits + en_hits)][:4]
                    ),
                }
            )

    # 中文同组多值（未必是伪造，但必须看见）
    if len(zh_hits) >= 2:
        vals = {round(v, 2) for v, _, _ in zh_hits}
        if len(vals) >= 2 and not (zh_hits and en_hits):
            out.append(
                {
                    "level": "mid",
                    "kind": "总额出现多个数值",
                    "token": str(sorted(vals)),
                    "desc": f"单据上出现 {len(vals)} 个不同的总额数值 {sorted(vals)}，"
                    f"需确认是「合计 / 价税合计」两个合法层级，还是重复标注",
                    "context": " | ".join(f"L{n}: {s}" for _, n, s in zh_hits[:4]),
                }
            )

    return out


# ---------------------------------------------------------------------------
# 三、币种
# ---------------------------------------------------------------------------

_FOREIGN = re.compile(
    r"(US\$|HK\$|USD|EUR|GBP|JPY|HKD|美元|欧元|英镑|日元|港币|港元|€|£|¥\s*(?=USD))", re.I
)


def detect_currency(text: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for m in _FOREIGN.finditer(text):
        tok = m.group(0).strip()
        if tok in seen:
            continue
        seen.add(tok)
        out.append(
            {
                "level": "mid",
                "kind": "出现外币标识",
                "token": tok,
                "desc": f"文本中出现「{tok}」。若抽取结果币种为 CNY，"
                f"则该金额的计价单位存疑，汇率与金额都需确认",
                "context": text[max(0, m.start() - 16) : m.end() + 16].replace("\n", " ").strip(),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def detect(text: str) -> dict:
    """统一入口。返回结构直接给前端渲染。"""
    if not text:
        return {"numeric": [], "shadow": [], "currency": [], "high": 0, "mid": 0, "total": 0}

    numeric = detect_numeric(text)
    shadow = detect_shadow(text)
    currency = detect_currency(text)
    allhits = numeric + shadow + currency
    return {
        "numeric": numeric,
        "shadow": shadow,
        "currency": currency,
        "high": sum(1 for h in allhits if h["level"] == "high"),
        "mid": sum(1 for h in allhits if h["level"] == "mid"),
        "total": len(allhits),
    }


if __name__ == "__main__":
    import json
    import sys

    sample = sys.stdin.read() if not sys.stdin.isatty() else (
        "采购订单 PO20241015001\n"
        "物料A  数量 2  单价 6000.00  金额 12000.00\n"
        "合计金额：12000.00\n"
        "Total: 1200.00\n"
        "运费 1.200.00  折扣 100.000  包装 1,20,00.00\n"
        "金额:5 000.0 0\n"
        "以 USD 结算\n"
    )
    print(json.dumps(detect(sample), ensure_ascii=False, indent=2))
