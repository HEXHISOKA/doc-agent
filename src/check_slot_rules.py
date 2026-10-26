"""槽位错位两条规则的回归测试。

为什么单独写这个文件
--------------------
这两条规则靠的是**两个假设的误差比值**（`_price_is_line_total` 里 `rel_price * 5 < rel_qp`
那一行），不是硬编码的阈值。比值型判据最容易悄悄坏掉：调一下容差、换一个求和口径，
它就会开始把正常单据判成错位，而且**照样给出一条读起来很合理的解释**——比彻底崩掉更危险。

所以这里把「必须报」和「必须不报」两种边界都钉住：

  必须报：金额被读进单价格（法语 PO 的经典形态）
  必须报：件数合计被填进金额栏
  必须报：合计在单内无来源
  必须不报：数量全是 1 时，「单价」与「行金额」两种解释完全等价——此时没有任何
           证据支持"装错格子"，报了就是编造成因
  必须不报：正常单据
  必须不报：本来就没有金额的收货单（没有合计时）

跑法：python src/check_slot_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import validate as V  # noqa: E402


def msgs(d: dict) -> list[tuple[str, str]]:
    return [(i["level"], i["msg"]) for i in V.validate(d)]


def has(ms: list[tuple[str, str]], level: str, *keys: str) -> bool:
    return any(lv == level and all(k in m for k in keys) for lv, m in ms)


def case(name: str, d: dict, *, expect_err: tuple[str, ...] | None = None,
         forbid_err: tuple[str, ...] = ()) -> bool:
    ms = msgs(d)
    errs = [m for lv, m in ms if lv == "err"]
    ok = True
    if expect_err:
        if not has(ms, "err", *expect_err):
            ok = False
            print(f"  ✗ {name}：期望出现含 {expect_err} 的 err，实际 err={errs}")
    for k in forbid_err:
        if any(k in m for _, m in ms):
            ok = False
            print(f"  ✗ {name}：不该出现含「{k}」的提示，实际={[m for _, m in ms if k in m]}")
    if ok:
        print(f"  ✓ {name}")
    return ok


def main() -> int:
    base = {"doc_no": "T-001", "supplier": "某供应商", "order_date": "2026-10-25",
            "currency": "CNY"}
    results = []

    print("1) 金额被读进单价格（法语 PO 形态，gold line_total = 137,58 / 125,79 / 120,00）")
    results.append(case(
        "应判为槽位错位，且不再报「缺金额」",
        {**base,
         "items": [
             {"name": "A", "qty": 45866, "price": 137.58, "amount": None},
             {"name": "B", "qty": 41936, "price": 125.79, "amount": None},
             {"name": "C", "qty": 40006, "price": 120.006, "amount": None},
         ],
         "total_amount": 423.63},
        expect_err=("填进「单价」",),
        # 老逻辑会给这条方向相反的提示，必须消失
        forbid_err=("缺少金额",),
    ))

    print("2) 数量全为 1：两种解释等价，不得下结论")
    results.append(case(
        "不报槽位错位（证据不足）",
        {**base,
         "items": [
             {"name": "A", "qty": 1, "price": 50.0, "amount": None},
             {"name": "B", "qty": 1, "price": 60.0, "amount": None},
             {"name": "C", "qty": 1, "price": 40.0, "amount": None},
         ],
         "total_amount": 150.0},
        forbid_err=("填进「单价」",),
    ))

    print("3) 件数合计被填进金额栏（收货单形态）")
    results.append(case(
        "应指出这是件数的合计",
        {**base,
         "items": [
             {"name": "A", "qty": 550, "price": None, "amount": None},
             {"name": "B", "qty": 550, "price": None, "amount": None},
             {"name": "C", "qty": 550, "price": None, "amount": None},
         ],
         "total_amount": 1650.0},
        expect_err=("件数的合计",),
        forbid_err=("缺少金额",),
    ))

    print("4) 合计在单内无来源（既不等于金额之和，也不等于数量之和）")
    results.append(case(
        "应指出找不到来源",
        {**base,
         "items": [{"name": "A", "qty": 10, "price": None, "amount": None}],
         "total_amount": 12345.0},
        expect_err=("找不到任何来源",),
    ))

    print("5) 无价单据且没有合计（正常收货单）")
    results.append(case(
        "不得报任何 err",
        {**base,
         "items": [{"name": "A", "qty": 100, "price": None, "amount": None}],
         "total_amount": None},
        forbid_err=("件数的合计", "找不到任何来源", "填进「单价」"),
    ))

    print("6) 正常采购订单（数量×单价=金额，明细之和=合计）")
    results.append(case(
        "不得报任何 err",
        {**base,
         "items": [
             {"name": "圆钢 Φ20", "qty": 2, "price": 3200.0, "amount": 6400.0},
             {"name": "圆钢 Φ30", "qty": 1, "price": 3180.0, "amount": 3180.0},
         ],
         "total_amount": 9580.0},
        forbid_err=("缺少金额", "填进「单价」", "件数的合计", "找不到任何来源"),
    ))

    bad = results.count(False)
    print(f"\n{len(results) - bad}/{len(results)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
