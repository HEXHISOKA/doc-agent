# -*- coding: utf-8 -*-
"""编号自动归一（info 降级）的回归测试。

对应 2026-10-26 的改动：validate 的 L2 doc_no 分支，在
「suggest 给出唯一候选 + 候选完全符合画像」时自动归一并降级为 info；
review_targets 对 info 不触发复核。

跑法：python src/test_normalize.py   （无外部依赖，不连库不连网）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import patterns  # noqa: E402
from validate import review_targets, validate  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  —— {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_extract(doc_no: str) -> dict:
    """一张最小但合法的单据（不含编号外的疑点，隔离变量）。"""
    return {
        "doc_no": doc_no,
        "supplier": "甬江五金有限公司",
        "order_date": "2026-10-24",
        "total_amount": 6000.0,
        "currency": "CNY",
        "items": [{"name": "六角螺栓 M8", "qty": 500, "price": 8.0, "amount": 4000.0},
                  {"name": "平垫圈 M8", "qty": 1000, "price": 2.0, "amount": 2000.0}],
    }


# ------------------------------------------------------------------ 画像
# 强画像：3 条同前缀历史（>= MIN_SAMPLES=3），前缀 PO、数字段 11 位
STRONG = patterns.learn([
    ("PO20260101001", "甬江五金有限公司"),
    ("PO20260102002", "甬江五金有限公司"),
    ("PO20260103003", "甬江五金有限公司"),
    ("PO20260104004", "别的供应商"),          # 全局画像的第四票
])
STRONG_SUP = STRONG[0]["甬江五金有限公司"]

# 弱画像：历史只有 1 条，低于 MIN_SAMPLES → 不许自动归一
WEAK = patterns.learn([("PO20260101001", "甬江五金有限公司")])
WEAK_SUP = WEAK[0]["甬江五金有限公司"]

# 位数不同的画像：历史都是 9 位数字段，P0 样本的形近候选也是 9 位
# （这条用于"候选不合 accepts"的反例：靠结构/位数卡住自动归一）
ODD = patterns.learn([
    ("PO123456789", "甬江五金有限公司"),
    ("PO123456780", "甬江五金有限公司"),
    ("PO123456781", "甬江五金有限公司"),
])
ODD_SUP = ODD[0]["甬江五金有限公司"]

# ------------------------------------------------------------------ 用例

# ① 强证据：P0… 只差一个形近字符、候选支持度更高、候选完全符合画像
#    → 自动归一，info 事件，doc_no 被改写
ex = make_extract("P020261024001")
issues = validate(ex, profile=STRONG_SUP)
doc_iss = [i for i in issues if i.get("field") == "doc_no"]
info = [i for i in doc_iss if i["level"] == "info"]
warn = [i for i in doc_iss if i["level"] == "warn"]
check("①强证据：编号被归一为 PO 前缀", ex["doc_no"].startswith("PO"), f"实际 {ex['doc_no']}")
check("①强证据：产出 info 而非 warn", len(info) == 1 and len(warn) == 0,
      f"info={len(info)} warn={len(warn)}")
check("①强证据：原值留档在 normalized.from",
      bool(info) and info[0]["normalized"]["from"] == "P020261024001")

# ② info 不得触发看图复核：一张只有归一事件的单，needs_review 必须为 False
needs, targets, triggers = review_targets(ex, issues, None, [])
check("②info 不触发复核", needs is False, f"needs={needs} triggers={triggers}")
check("②targets 里没有 doc_no", all(t["field"] != "doc_no" for t in targets))

# ③ 弱证据（历史 1 条 < MIN_SAMPLES）：不许自动归一，保持 warn 问人
ex2 = make_extract("P020261024001")
issues2 = validate(ex2, profile=WEAK_SUP)
doc2 = [i for i in issues2 if i.get("field") == "doc_no"]
check("③弱证据：不归一", ex2["doc_no"] == "P020261024001")
check("③弱证据：仍是 warn（询问一次）",
      len(doc2) == 1 and doc2[0]["level"] == "warn", f"{[i['level'] for i in doc2]}")
# 样本不足时 warn **不带 fixes**——这是 patterns 的既有契约（suggest docstring：
# "样本不足以断定正确写法，请人工核对"）。宁可不指方向，也不硬凑一个没有
# 证据支持的值。此时人用「按图上改 / 手动填写」兜底；前 3 条确认入库后，
# 画像立起来，此后同类差异走①的自动归一，不再问。
check("③弱证据：无 fixes（样本不足不给方向，宁缺毋滥）",
      not (doc2 and doc2[0].get("fixes")),
      f"fixes={doc2[0].get('fixes') if doc2 else None}")

# ④ 候选不完全符合画像（accepts 不过）：不许自动归一
#    历史前缀 PO、数字段 9 位；样本 P0+9 位归一后仍是 9 位——accepts 应通过。
#    换成位数确实不同的值：历史 9 位，来了个 P0+12 位的编号，形近候选也 12 位，
#    超出 dmax+1 → accepts=False → 必须保持 warn。
ex4 = make_extract("P020261024001001")   # P0 + 12 位数字
issues4 = validate(ex4, profile=ODD_SUP)
doc4 = [i for i in issues4 if i.get("field") == "doc_no"]
check("④候选不合画像：不自动归一",
      ex4["doc_no"] == "P020261024001001",
      f"实际 {ex4['doc_no']}")
check("④候选不合画像：warn 保留（fixes 仍给方向，由人定）",
      any(i["level"] == "warn" for i in doc4),
      f"{[(i['level'], i['msg'][:30]) for i in doc4]}")

# ⑤ 空画像（没有任何历史）：什么都不说、不问也不猜（patterns 原有行为，回归确认）
ex5 = make_extract("P020261024001")
issues5 = validate(ex5, profile=patterns.EMPTY)
check("⑤空画像：无编号类提示",
      not [i for i in issues5 if i.get("field") == "doc_no"])

# ⑥ 干净编号（本来就符合画像）：不打扰（回归确认）
ex6 = make_extract("PO20261024001")
issues6 = validate(ex6, profile=STRONG_SUP)
check("⑥正常编号：无任何编号类提示",
      not [i for i in issues6 if i.get("field") == "doc_no"])

# ------------------------------------------------------------------ 汇总
print()
if FAILS:
    print(f"✗ {len(FAILS)} 项未通过：{FAILS}")
    sys.exit(1)
print("✓ 全部通过")
