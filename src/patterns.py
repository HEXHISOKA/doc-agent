"""格式画像：让"这家客户的单据长什么样"从数据里来，而不是写死在代码里。

为什么单独抽这一层
------------------
`re.fullmatch(r"P0\\d{9,}")` 看上去像通用规则，其实是把**某一家客户的编号规律
当成了世界的规律**。换一家（`XK-2026-0042`、`2026-08-0021`、纯流水号）它立刻
变成误报，而且误报得理直气壮——因为规则不知道自己只是猜的。

所以规则按"依据从哪来"分三层，越靠上越硬：

  A 通用层（不依赖任何外部知识）
    类型对不对、日期是否真实存在、数量×单价=金额、明细之和=合计。
    这些是会计恒等式，换任何客户都成立，永远可以下结论。

  B 画像层（本模块负责）
    编号格式、币种习惯、必填字段。**从历史单据学，或从配置读**。
    换客户只换这一层，代码不动。样本不足时宁可闭嘴，也不猜一个值。

  C 单内自证（不需要任何历史）
    同一个编号在文档别处也出现、同一物料两行单价不同。证据来自这张单据内部，
    比任何历史统计都硬。

关于"格式"怎么表示（踩过一次坑）
--------------------------------
最初我把编号压成一个字符类串（`PO20261024001` → `LLDDDDDDDDDDD`）当形状用。
结果 `PO2026082113`（10 位流水）和 `PO20261024001`（11 位流水）被判成两种
格式——**长度被耦合进了形状里**，而长度本来就是会变的（流水号进位、年份改版）。

现在拆成三件互不干扰的事实：
  · 结构   `LD` = 一段字母 + 一段数字；`LSDSD` = 字母段/分隔符/数字段/…
  · 前缀   字母段的内容本身（`PO`、`XK`）——这是区分形近字符的唯一依据
  · 数字长度  区间（历史 min~max），不参与"是不是同一格式"的判定，只做范围检查
这样 `PO` 10 位和 11 位是同一格式，而在 `PO` 的位置写成 `P0` 会被抓到。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

# 少于这个样本数，只允许提示"可疑"，不给具体候选值。
# 3 是经验值：两条历史里出现一次"另一种写法"，分不清那是另一种格式还是一次误识别。
MIN_SAMPLES = 3

# 形近字符组：OCR 混淆几乎都发生在这几组内部（0/O、1/l/I、5/S、8/B、2/Z…）。
# 只在组内替换，不做通用编辑距离——否则会"修"出完全不相关的编号。
_HOMOGLYPH_GROUPS = (
    frozenset("0Oo"),
    frozenset("1lI|"),
    frozenset("5S"),
    frozenset("8B"),
    frozenset("2Z"),
    frozenset("6b"),
    frozenset("9g"),
    frozenset("4A"),
)

_DATE_PATTERNS = (
    re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"),
    re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日?$"),
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),
)


# --------------------------------------------------------------------- 基础工具

def _cls(ch: str) -> str:
    """字符类：D 数字 / L 字母 / S 分隔符（- _ / 空格 等）。"""
    if "0" <= ch <= "9":
        return "D"
    if ("A" <= ch <= "Z") or ("a" <= ch <= "z"):
        return "L"
    return "S"


def char_class_seq(value) -> str:
    """逐字符的字符类串，只用于展示和日志（不参与判定，原因见模块说明）。"""
    return "".join(_cls(c) for c in str(value or ""))


def segments(value) -> list[tuple[str, str]]:
    """切成 (类型, 内容) 段：`XK-2026-0042` → [("L","XK"),("S","-"),("D","2026"),…]"""
    out: list[tuple[str, str]] = []
    for ch in str(value or ""):
        kind = _cls(ch)
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1] + ch)
        else:
            out.append((kind, ch))
    return out


def shape_of(value) -> str:
    """结构串：各段类型首字母拼接，如 `LD`、`LSDSD`。"""
    return "".join(kind for kind, _ in segments(value))


def normalize_name(name) -> str:
    """供应商名归一：去空白/括号/标点并小写，用于把同一家公司的不同写法归到一桶。"""
    if not name:
        return ""
    return re.sub(r"[\s（）()\[\]【】·,，.。:：/\\-]+", "", str(name)).lower()


def homoglyph_variants(value: str, limit: int = 240) -> list[str]:
    """只改**一个**形近字符能得到的候选（`P020261024001` → `PO20261024001`）。

    刻意限制在"改一处"：两处以上同时错的情形，凭形近关系能凑出的候选太多，
    给出来等于让概率决定数据。
    """
    out: list[str] = []
    seen = {value}
    for i, ch in enumerate(value):
        for group in _HOMOGLYPH_GROUPS:
            if ch not in group:
                continue
            for alt in group:
                if alt == ch:
                    continue
                cand = value[:i] + alt + value[i + 1:]
                if cand in seen:
                    continue
                seen.add(cand)
                out.append(cand)
                if len(out) >= limit:
                    return out
    return out


def normalize_date(value) -> str | None:
    """多种常见写法 → ISO `YYYY-MM-DD`；不是合法日期返回 None。

    这属于 A 层（通用知识）：`2026年8月21日`、`2026/8/21`、`20260821` 指同一天，
    与客户无关。原来只认 `YYYY-MM-DD`，等于把"这一版数据的写法"当成了唯一写法。
    """
    s = "" if value is None else str(value).strip()
    if not s:
        return None
    for rx in _DATE_PATTERNS:
        m = rx.match(s)
        if not m:
            continue
        y, mo, d = (int(x) for x in m.groups())
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None          # 2 月 30 日这种：格式对、日期不存在
    return None


def _major(counter: dict, key) -> bool:
    """这个取值算不算"多数派"。

    频次 ≥2，或历史里所有样本都是它。前者抗污染（历史混进一条错识别的编号
    不会把画像带偏），后者照顾冷启动（只有一两条历史时不至于全盘否认）。
    """
    if not counter:
        return True
    hit = counter.get(key, 0)
    return hit >= 2 or (hit > 0 and hit == sum(counter.values()))


# ------------------------------------------------------------------- 画像本体

class DocProfile:
    """一类单据（通常是一个客户）的编号格式画像。

    只记三件事：结构序列、字母前缀、数字段长度区间。**不记正则**——
    正则一旦写出来就固定了，而"格式"本身是随客户变的统计事实。
    """

    __slots__ = ("structs", "letters", "dmin", "dmax", "samples", "source", "example")

    def __init__(self, structs: dict | None = None, letters: dict | None = None,
                 dmin: int = 0, dmax: int = 0, samples: int = 0,
                 source: str = "empty", example: str = ""):
        self.structs: dict[str, int] = dict(structs or {})
        self.letters: dict[str, int] = dict(letters or {})
        self.dmin = int(dmin or 0)
        self.dmax = int(dmax or 0)
        self.samples = int(samples)
        self.source = source
        self.example = example

    # -- 判定 ---------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not self.structs

    def accepts(self, value) -> bool:
        """这个编号算不算"本客户的正常编号"。

        三步各自独立，任一步不成立就判不正常——因为三步查的是不同的事：
        结构（段序列）→ 前缀（字母段内容）→ 数字段长度（流水号位数）。
        合在一起查会把"长度变了"误判成"格式变了"，这正是第一版踩的坑。
        """
        if self.empty:
            return True                      # 没有依据就不下判断

        segs = segments(value)
        if not segs:
            return False
        if not _major(self.structs, "".join(k for k, _ in segs)):
            return False

        head = "".join(text for kind, text in segs if kind == "L")
        if self.letters and not _major(self.letters, head):
            return False

        if self.dmin:
            for kind, text in segs:
                if kind != "D":
                    continue
                # 允许比历史上限多一位：流水号跨年/进位会让位数涨一位，
                # 那不是格式变了。再多就不正常了。
                if not (self.dmin <= len(text) <= self.dmax + 1):
                    return False
        return True

    def score(self, value) -> tuple[int, int]:
        """这个值在历史里的"支持度"：(字母前缀出现次数, 结构出现次数)。

        为什么要打分而不是只问"见没见过"：历史本身会被污染——实测库里 9 条
        有 4 条是错误编号（`P0…`），于是 `P` 这个前缀"见过很多次"，只按出现与否
        判断就会认定它正常。比"见过"更有意义的是"哪个解释支持度更高"：
        `PO` 5 次 高于 `P` 4 次，所以 `P0…` 更可能是 `PO…` 的误识别。
        """
        segs = segments(value)
        if not segs:
            return (0, 0)
        head = "".join(text for kind, text in segs if kind == "L")
        shape = "".join(kind for kind, _ in segs)
        return (self.letters.get(head, 0), self.structs.get(shape, 0))

    # -- 说明 ---------------------------------------------------------
    def describe(self) -> str:
        if self.empty:
            return "尚无历史样本"
        where = "配置" if self.source == "config" else "历史单据"
        tip = self.example or "-"
        n = self.structs.get(shape_of(tip), 0) if tip else 0
        bits = [f"{where}中 {max(n, 1)}/{self.samples} 条形如 {tip}"]
        if self.letters:
            top = max(self.letters.items(), key=lambda kv: kv[1])[0]
            if top:
                bits.append(f"前缀 {top}")
        if self.dmin:
            bits.append(f"数字段 {self.dmin}~{self.dmax} 位")
        return "，".join(bits)

    # -- 给候选 -------------------------------------------------------
    def suggest(self, value) -> tuple[str | None, str | None]:
        """返回 (候选值, 说明)。候选为 None 表示**不给建议**，说明里写原因。

        三种情形要分清楚，界面上也分别显示：
          · 格式正常            → (None, None)，不打扰
          · 不符、有候选        → (候选, 依据)，形近字符改一处就符合画像
          · 不符、没候选        → (None, 原因)，只提示，不编一个值
        """
        text = "" if value is None else str(value).strip()
        if not text:
            return None, None
        if self.empty:
            # 空画像**不产生任何提示**。
            # 说一句"我判断不了"听上去无害，实际会让每张单据都挂一条提示、
            # 每张都触发一次看图复核——把真正的问题淹没在噪音里。
            # "没有依据"是系统状态，不是这张单据的疑点，不该记在单据头上。
            return None, None

        # 第一步：有没有"更站得住脚"的写法？
        # 这一步放在 accepts 之前，才能处理"错误写法在历史里也见过"的情形。
        best_score = self.score(text)
        best_cand = None
        for cand in homoglyph_variants(text):
            sc = self.score(cand)
            if sc > best_score:
                best_cand, best_score = cand, sc
        if best_cand is not None and self.samples >= MIN_SAMPLES:
            return best_cand, (f"{self.describe()}；当前写法在历史中的支持度低于"
                              f"「{best_cand}」，且两者只差一个形近字符")

        if self.accepts(text):
            return None, None
        if self.samples < MIN_SAMPLES:
            return None, (f"只有 {self.samples} 条历史样本，不足以断定正确写法，请人工核对")
        for cand in homoglyph_variants(text):
            if self.accepts(cand):
                return cand, (f"{self.describe()}；当前写法与该格式不符，"
                              f"且只差一个形近字符")
        return None, f"与{self.describe()}的格式不符，但没有只差形近字符的候选，请人工核对"

    # -- 序列化（配置 / 缓存）-----------------------------------------
    def to_dict(self) -> dict:
        return {"structs": self.structs, "letters": self.letters,
                "dmin": self.dmin, "dmax": self.dmax,
                "samples": self.samples, "source": self.source, "example": self.example}

    @classmethod
    def from_dict(cls, d: dict) -> "DocProfile":
        if not isinstance(d, dict):
            return cls()
        structs = d.get("structs") if isinstance(d.get("structs"), dict) else {}
        letters = d.get("letters") if isinstance(d.get("letters"), dict) else {}
        return cls(
            {str(k): int(v) for k, v in structs.items() if str(k)},
            {str(k): int(v) for k, v in letters.items()},
            int(d.get("dmin") or 0), int(d.get("dmax") or 0),
            int(d.get("samples") or 0),
            str(d.get("source") or "config"),
            str(d.get("example") or ""),
        )


EMPTY = DocProfile()


# --------------------------------------------------------------------- 学习

def _build(values: list[str], source: str) -> DocProfile:
    if not values:
        return DocProfile(source=source)

    structs: dict[str, int] = {}
    letters: dict[str, int] = {}
    lens: list[int] = []
    for v in values:
        segs = segments(v)
        shape = "".join(k for k, _ in segs)
        structs[shape] = structs.get(shape, 0) + 1
        head = "".join(t for k, t in segs if k == "L")
        letters[head] = letters.get(head, 0) + 1
        lens.extend(len(t) for k, t in segs if k == "D")

    top_shape = max(structs.items(), key=lambda kv: (kv[1], kv[0]))[0]
    example = next((v for v in values if shape_of(v) == top_shape), values[0])
    return DocProfile(structs, letters, min(lens) if lens else 0,
                      max(lens) if lens else 0, len(values), source, example)


def learn(pairs) -> tuple[dict[str, DocProfile], DocProfile]:
    """从 (编号, 供应商) 序列学习：按供应商分桶，外加一个全局画像。

    为什么要分桶：**编号格式是客户级的，不是行业级的**。同一份单据里，
    供应商名把单据归到某个客户，格式随之切换；供应商认不出来时才退回全局。
    """
    buckets: dict[str, list[str]] = {}
    all_values: list[str] = []
    for no, supplier in pairs:
        text = "" if no is None else str(no).strip()
        if not text:
            continue
        all_values.append(text)
        buckets.setdefault(normalize_name(supplier), []).append(text)
    by_supplier = {k: _build(v, "history") for k, v in buckets.items() if v}
    return by_supplier, _build(all_values, "history")


def pick(by_supplier: dict[str, DocProfile], global_profile: DocProfile,
         supplier) -> DocProfile:
    """按本次单据的供应商挑选画像；认不出来就用全局画像。"""
    key = normalize_name(supplier)
    prof = by_supplier.get(key) if key else None
    if prof is not None and not prof.empty:
        return prof
    return global_profile or EMPTY


def from_config(path: str | None) -> DocProfile | None:
    """从 JSON 配置读画像（换客户时由实施方提供，不改代码）。

    两种写法都支持：
      {"doc_no_examples": ["PO20260101001", "PO20260101002"]}   ← 实施方最省事
      {"structs": {...}, "letters": {...}, "dmin": 11, "dmax": 11}  ← 导入导出用
    读不到或格式不对返回 None，让调用方退回历史学习。
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    examples = raw.get("doc_no_examples")
    if isinstance(examples, list) and examples:
        prof = _build([str(x) for x in examples], "config")
        return prof if not prof.empty else None
    if isinstance(raw.get("structs"), dict):
        prof = DocProfile.from_dict({**raw, "source": "config"})
        return prof if not prof.empty else None
    return None


def from_examples(examples, source: str = "profiles") -> DocProfile | None:
    """直接用一组编号样例建画像（doc_no_profiles 表渠道）。

    来源两种：实施方在上线时直接给的标准写法（source=manual，最权威），
    和人工确认入库时沉淀下来的写法（source=confirmed，逐条积累）。
    样例为空、或拼不出有效画像时返回 None，让调用方退回历史学习——
    档案是优先级最高的证据，但绝不是"有就硬套"。
    """
    vals = [str(x).strip() for x in (examples or []) if str(x).strip()]
    # 卫生闸：一个编号样例至少要含字母或数字。纯符号串（"!!!!"）能拼出
    # "合法"的画像结构，但它只会让真编号全部报不符——垃圾进不如不进。
    vals = [x for x in vals if any(c.isalnum() for c in x)]
    if not vals:
        return None
    prof = _build(vals, source)
    return prof if not prof.empty else None
