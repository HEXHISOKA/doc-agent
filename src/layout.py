"""版面重建：把 OCR 的词框还原成有行列关系的表格文本。

为什么需要这一层：
    通用 OCR 只回答「这个位置有个词、长这样」，不回答「它属于第几行第几列」。
    多行明细表一旦顺序错乱，数量会和别人的单价配到一起（行业里叫"串列"），
    这是单据识别最主要的错误来源。所以必须先用坐标把表格结构重建出来。

三步：
    1. 倾斜校正 —— 用所有词框上边角度的中位数估计整体倾斜，反向旋转坐标
    2. 行聚类   —— 按垂直重叠比例归行（不写死行高，适应字号混排）
    3. 列切分   —— 行内按间隙切 cell，阈值以**字高**为基准

关于阈值基准的踩坑记录（值得写进设计文档）：
    一开始用「词框宽度中位数」当基准，结果列没切开。原因是词框宽度差异极大
    （"XX精密制造有限公司" 宽 356px，"金额" 宽 51px），中位数被长词拉高，
    阈值涨到 140px，而真实列间距只有 101px。
    改用字高（单据内字号统一，字高稳定在 30px 左右）后，阈值 18px，
    同格内字间距通常 < 字高、列之间通常 > 2 倍字高，区分度立刻清晰。

二维矩阵是 best-effort，见 build_grid 的说明——这里刻意不假装它完美。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass
class Word:
    text: str
    box: list[tuple[float, float]]
    score: float
    left: float = 0.0
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def width(self) -> float:
        return self.right - self.left


@dataclass
class Cell:
    text: str
    left: float
    right: float

    @property
    def cx(self) -> float:
        return (self.left + self.right) / 2

    @property
    def width(self) -> float:
        return self.right - self.left


@dataclass
class Line:
    words: list[Word] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def cy(self) -> float:
        return (self.top + self.bottom) / 2

    def as_text(self) -> str:
        return " | ".join(c.text for c in self.cells)


@dataclass
class LayoutResult:
    lines: list[Line]
    skew_deg: float
    text: str
    col_count: int
    grid: list[list[str]] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)


def word_angle(w: Word) -> float:
    """词框上边的倾角（度）。OCR 返回的四边形保留了文字自身的旋转。"""
    if len(w.box) < 2:
        return 0.0
    (x0, y0), (x1, y1) = w.box[0], w.box[1]
    if x1 - x0 <= 0:
        return 0.0
    return math.degrees(math.atan2(y1 - y0, x1 - x0))


def filter_noise(
    words: list[Word], skew: float, max_dev_deg: float = 12.0
) -> tuple[list[Word], list[dict]]:
    """剔除"整体倾斜之外还自己歪着"的词。

    为什么这条规则能抓公章（以及斜着写的手写批注）：
        整页倾斜是**同一个角度**（本项目用所有词框角度的中位数估计），
        每个正常文字的词框角度都应该≈skew。而圆形公章的弧排字，
        每个字都被**绕圆心单独旋转**过，角度从 -60° 到 +60° 连续分布，
        与页面倾斜的差值天然很大。手写批注同理（笔迹方向与印刷基线无关）。

    为什么阈值取 12° 而不是 5°：
        实测正常印刷词的偏差在 0~3.5° 之间（含 OCR 框抖动），
        而部分检测器会把框回归成**轴对齐矩形**（角度恰为 0），
        在 3~6° 的倾斜页面上这类词的偏差可以达到 6°。取 12° 留了余量。

    这条规则只能算"减少残留"，不能算"消除"——灰度复印件上的印章
    （红已变灰）弧顶附近的字旋转角接近 0，会被漏掉。
    所以主防线是 preprocess.suppress_red，这条是补漏，不承担主责。
    """
    kept: list[Word] = []
    dropped: list[dict] = []
    for w in words:
        dev = word_angle(w) - skew
        if abs(dev) > max_dev_deg:
            dropped.append(
                {"text": w.text, "dev_deg": round(dev, 2), "why": "倾斜异常"}
            )
        else:
            kept.append(w)
    return kept, dropped


def _rotate(x: float, y: float, cx: float, cy: float, deg: float) -> tuple[float, float]:
    rad = math.radians(-deg)
    dx, dy = x - cx, y - cy
    return (
        cx + dx * math.cos(rad) - dy * math.sin(rad),
        cy + dx * math.sin(rad) + dy * math.cos(rad),
    )


def estimate_skew(words: list[Word]) -> float:
    """用每个词框上边的倾角中位数估计整体倾斜（度）。旋转整图后行才水平，行聚类才稳。"""
    angles = []
    for w in words:
        if len(w.box) < 2:
            continue
        (x0, y0), (x1, y1) = w.box[0], w.box[1]
        if x1 - x0 <= 0:
            continue
        a = math.degrees(math.atan2(y1 - y0, x1 - x0))
        if -20 < a < 20:
            angles.append(a)
    return statistics.median(angles) if angles else 0.0


def _reproject(words: list[Word], skew: float) -> None:
    if not words:
        return
    xs = [p[0] for w in words for p in w.box]
    ys = [p[1] for w in words for p in w.box]
    gx = (min(xs) + max(xs)) / 2
    gy = (min(ys) + max(ys)) / 2
    for w in words:
        pts = [_rotate(x, y, gx, gy, skew) for x, y in w.box]
        w.left = min(p[0] for p in pts)
        w.right = max(p[0] for p in pts)
        w.top = min(p[1] for p in pts)
        w.bottom = max(p[1] for p in pts)


def cluster_lines(words: list[Word], min_overlap: float = 0.45) -> list[Line]:
    """按垂直重叠比例做行聚类。"""
    ordered = sorted(words, key=lambda w: (w.top, w.left))
    lines: list[Line] = []
    for w in ordered:
        best: Line | None = None
        best_overlap = 0.0
        for line in lines:
            overlap = min(line.bottom, w.bottom) - max(line.top, w.top)
            if overlap <= 0:
                continue
            ratio = overlap / max(1e-6, min(line.height, w.height))
            if ratio >= min_overlap and ratio > best_overlap:
                best, best_overlap = line, ratio
        if best is None:
            lines.append(Line(words=[w]))
        else:
            best.words.append(w)
    for line in lines:
        line.words.sort(key=lambda w: w.left)
    lines.sort(key=lambda l: l.cy)
    return lines


def split_cells(line: Line) -> list[Cell]:
    """行内按间隙切 cell。阈值 = 0.6 × 中位字高（不用词宽，理由见模块注释）。"""
    if not line.words:
        return []
    heights = [w.height for w in line.words if w.height > 0]
    unit = statistics.median(heights) if heights else 24.0
    gap_threshold = max(unit * 0.6, 10.0)

    groups: list[list[Word]] = [[line.words[0]]]
    for prev, cur in zip(line.words, line.words[1:]):
        if cur.left - prev.right > gap_threshold:
            groups.append([cur])
        else:
            groups[-1].append(cur)

    cells = []
    for g in groups:
        cells.append(
            Cell(
                text=" ".join(w.text for w in g),
                left=min(w.left for w in g),
                right=max(w.right for w in g),
            )
        )
    return cells


def build_grid(lines: list[Line]) -> list[list[str]]:
    """按 cell 顺序排成等宽矩阵，供人眼核对与导出。

    为什么不做"按坐标对齐到列"：试过了，不行。
    单据里表头文字是**居中**于列、明细内容是**左对齐**于列、数字是**右对齐**于列，
    三种对齐方式混在同一列里，它们的 x 分布各不相同（实测："物料名称"表头中心在 341，
    而明细 "PE密封垫片" 中心在 236，相差 105px 却属于同一列）。
    任何基于 x 位置的一维聚类都会在"文本列"上崩掉。

    工业界的解法是深度学习表格识别（PP-Structure / TableFormer 一类），代价是重依赖
    + 不可解释。本项目刻意不引入：因为**下游是 LLM，它不需要严格的二维矩阵**，
    每行 cells 的顺序文本已经足够它理解 `物料 | 数量 | 单价 | 金额` 的对应关系。
    把不可靠的列对齐隔离在外，比假装解决它更安全。

    所以这里如实按顺序填充：缺列（如 OCR 漏检序号）就留空，不猜。
    """
    if not lines:
        return []
    n = max((len(l.cells) for l in lines), default=0)
    return [[c.text for c in l.cells] + [""] * (n - len(l.cells)) for l in lines]


def rebuild(ocr_items: list[dict], drop_below: float = 0.0, filter_skewed: bool = True) -> LayoutResult:
    """入口：ocr_items 为 [{'box': [[x,y],...], 'text': str, 'score': float}, ...]"""
    words = [
        Word(text=it["text"], box=[tuple(p) for p in it["box"]], score=float(it.get("score", 1.0)))
        for it in ocr_items
        if it.get("text", "").strip() and float(it.get("score", 1.0)) >= drop_below
    ]
    if not words:
        return LayoutResult(lines=[], skew_deg=0.0, text="", col_count=0, grid=[])

    skew = estimate_skew(words)
    dropped: list[dict] = []
    if filter_skewed:
        # 先算 skew 再过滤：skew 用中位数估计，少数印章字不会把它带偏
        words, dropped = filter_noise(words, skew)
    _reproject(words, skew)
    lines = cluster_lines(words)
    for line in lines:
        line.cells = split_cells(line)

    col_count = max((len(l.cells) for l in lines), default=0)
    text = "\n".join(l.as_text() for l in lines)
    grid = build_grid(lines)
    return LayoutResult(
        lines=lines,
        skew_deg=skew,
        text=text,
        col_count=col_count,
        grid=grid,
        dropped=dropped,
    )


def to_markdown_table(result: LayoutResult) -> str:
    """渲染成 markdown 表格，便于人眼核对。用 grid（对齐视图）。"""
    grid = result.grid or [[c.text for c in l.cells] for l in result.lines]
    if not grid:
        return ""
    n = max(len(r) for r in grid)
    rows = [(r + [""] * (n - len(r))) for r in grid]

    widths = [max(3, max(_visual_len(r[i]) for r in rows)) for i in range(n)]
    out = []
    for idx, r in enumerate(rows):
        out.append("| " + " | ".join(c + " " * (widths[i] - _visual_len(c)) for i, c in enumerate(r)) + " |")
        if idx == 0:
            out.append("| " + " | ".join("-" * widths[i] for i in range(n)) + " |")
    return "\n".join(out)


def _visual_len(s: str) -> int:
    """中文按 2 宽度算，让 markdown 表格在等宽字体下对齐。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in s)
