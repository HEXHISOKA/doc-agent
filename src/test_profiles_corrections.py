"""档案沉淀 + 更正链的单元测试（stub 掉 CloudClient，不碰线上库）。

覆盖：
- patterns.from_examples：样例建画像 / 空样例 / 垃圾样例；
- handle_correct：参数校验白名单、记录不存在、新旧同值拒绝、成功路径；
- _profile_record_confirmed：首次插入 / 去重跳过 / 追加更新；
- handle_profile_examples：坏输入拒绝、成功后作废画像缓存。

跑法：python src/test_profiles_corrections.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # app.py
sys.path.insert(0, str(ROOT / "src"))  # src 内模块

import patterns  # noqa: E402
import app        # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


class FakeCloud:
    """按调用顺序返回脚本化结果，并记录调用轨迹。"""

    def __init__(self, select_results=None, insert_results=None, insert_errors=None):
        self.select_results = list(select_results or [])
        self.insert_results = list(insert_results or [])
        self.insert_errors = list(insert_errors or [])
        self.select_calls, self.insert_calls, self.update_calls = [], [], []

    def rest_select(self, table, query="", timeout=20.0):
        self.select_calls.append((table, query))
        return self.select_results.pop(0) if self.select_results else []

    def rest_insert(self, table, row, timeout=25.0):
        self.insert_calls.append((table, row))
        err = self.insert_errors.pop(0) if self.insert_errors else None
        if err is not None:
            raise err
        return self.insert_results.pop(0) if self.insert_results else []

    def rest_update(self, table, row, query="", timeout=25.0):
        self.update_calls.append((table, row, query))
        return []


print("== patterns.from_examples ==")
prof = patterns.from_examples(["PO20260101001", "PO20260102002", "PO20260103003"])
check("正常样例建出画像", prof is not None and not prof.empty)
check("画像接受同格式编号", prof is not None and prof.accepts("PO20260104004"))
check("空样例返回 None", patterns.from_examples([]) is None)
check("垃圾样例返回 None", patterns.from_examples(["!!!!"]) is None)
check("source 正确传递", prof is not None and prof.source == "profiles")

print("== handle_correct 参数校验 ==")
fake = FakeCloud()
app.CLOUD = fake
r = app.handle_correct({"corrects_id": "12", "field": "doc_no", "new_value": "X"})
check("corrects_id 非整数拒绝", not r["ok"])
r = app.handle_correct({"corrects_id": 12, "field": "item_count", "new_value": "3"})
check("白名单外字段拒绝", not r["ok"] and "只允许" in r["error"])
r = app.handle_correct({"corrects_id": 12, "field": "doc_no", "new_value": "  "})
check("空新值拒绝", not r["ok"])
r = app.handle_correct({"corrects_id": 12, "field": "total_amount", "new_value": "abc"})
check("金额非数字拒绝", not r["ok"])
r = app.handle_correct({"corrects_id": 12, "field": "total_amount", "new_value": "-5"})
check("负金额拒绝", not r["ok"])
check("参数校验阶段不触库", not fake.select_calls)

print("== handle_correct 服务端取旧值 ==")
# 记录不存在
fake = FakeCloud(select_results=[[]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 999, "field": "doc_no", "new_value": "PO1"})
check("记录不存在拒绝", not r["ok"] and "不存在" in r["error"])
check("旧值查自有效版本视图", fake.select_calls and fake.select_calls[0][0] == "v_doc_parses_effective")
check("查询带全部可更正字段与明细数据", fake.select_calls
      and fake.select_calls[0][1].startswith("select=id,doc_no,supplier,order_date,total_amount,extract_json&id=eq.999"))
# 新旧同值
fake = FakeCloud(select_results=[[{"id": 5, "doc_no": "PO20260101001"}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 5, "field": "doc_no", "new_value": "PO20260101001"})
check("新旧同值拒绝", not r["ok"] and "无需更正" in r["error"])
check("同值路径未写入", not fake.insert_calls)
# 成功路径：old_value 由服务端读出，客户端说了不算
fake = FakeCloud(
    select_results=[[{"id": 5, "doc_no": "P020261024001"}]],
    insert_results=[[{"id": 1, "seq": 1, "record_hash": "a" * 64}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 5, "field": "doc_no", "new_value": "PO20261024001",
                        "reason": "印章压字，看图复核确认", "corrected_by": "王工"})
check("成功路径 ok", r.get("ok") is True and r["correction"]["seq"] == 1)
row = fake.insert_calls[0][1]
check("old_value 服务端现读", row["old_value"] == "P020261024001")
check("写入字段齐全", row["field"] == "doc_no" and row["new_value"] == "PO20261024001"
      and row["reason"] == "印章压字，看图复核确认" and row["corrected_by"] == "王工")
check("写入目标表是 corrections", fake.insert_calls[0][0] == "corrections")

print("== handle_correct 批量（表格模式） ==")
# 批量：两个格子真变了，一个没变 → 只落 2 条
fake = FakeCloud(
    select_results=[[{"id": 5, "doc_no": "PO1", "supplier": "甲", "order_date": "2026-10-24", "total_amount": 100}]],
    insert_results=[[{"id": 1, "seq": 1, "field": "doc_no"}], [{"id": 2, "seq": 2, "field": "supplier"}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 5, "reason": "批量", "changes": [
    {"field": "doc_no", "new_value": "PO2"},
    {"field": "supplier", "new_value": "甲"},      # 未变，应跳过
    {"field": "order_date", "new_value": "2026-10-25"}]})
check("批量成功且只落变更项", r.get("ok") is True and len(r["corrections"]) == 2)
check("未变的格子没写库", len(fake.insert_calls) == 2
      and fake.insert_calls[0][1]["field"] == "doc_no"
      and fake.insert_calls[1][1]["field"] == "order_date")
check("单字段兼容键仍返回", r["correction"]["seq"] == 1)
# 全部同值 → 明确拒绝
fake = FakeCloud(select_results=[[{"id": 5, "doc_no": "PO1", "supplier": "甲"}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 5, "reason": "x", "changes": [{"field": "doc_no", "new_value": "PO1"}]})
check("全部同值拒绝", not r["ok"] and "没有实际变更" in r["error"])
# 同字段出现两次 → 拒绝
fake = FakeCloud()
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 5, "reason": "x", "changes": [
    {"field": "doc_no", "new_value": "A"}, {"field": "doc_no", "new_value": "B"}]})
check("同字段重复拒绝", not r["ok"] and "多次" in r["error"])
# 日期格式闸
r = app.handle_correct({"corrects_id": 5, "reason": "x", "changes": [{"field": "order_date", "new_value": "10月24日"}]})
check("日期格式非法拒绝", not r["ok"] and "YYYY-MM-DD" in r["error"])
check("校验阶段不触库", not fake.select_calls)
# 批量中途失败：已落链的不回滚，如实上报 partial
err = app.CloudError("upstream down")  # 必须用真实异常类，否则不会被捕获
fake = FakeCloud(
    select_results=[[{"id": 5, "doc_no": "PO1", "supplier": "甲", "order_date": "2026-10-24", "total_amount": 100}]],
    insert_results=[[{"id": 1, "seq": 1}]],
    insert_errors=[None, err])  # 第 1 条成功，第 2 条才失败
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 5, "reason": "x", "changes": [
    {"field": "doc_no", "new_value": "PO2"}, {"field": "supplier", "new_value": "乙"}]})
check("中途失败 ok=False 且标注 partial", not r["ok"] and r.get("partial") is True)
check("已成功的一条在返回里", len(r.get("corrections") or []) == 1)
check("失败后停止继续写（共 2 次尝试：1 成功 1 失败）", len(fake.insert_calls) == 2)
# 清空字段拒绝
r = app.handle_correct({"corrects_id": 5, "reason": "x", "changes": [{"field": "supplier", "new_value": ""}]})
check("用更正清空字段拒绝", not r["ok"] and "不能为空" in r["error"])

print("== handle_correct 明细更正（items[i].key） ==")
ITEMS = [{"name": "封箱胶带", "qty": 800, "price": 2.05, "amount": 1640},
         {"name": "铝箔袋", "qty": 3000, "price": 2.35, "amount": 7050}]
EX = json.dumps({"doc_no": "PO20260821113", "items": ITEMS}, ensure_ascii=False)
# 参数校验：明细数值格非数字 / 非法键名，均不触库
fake = FakeCloud(); app.CLOUD = fake
r = app.handle_correct({"corrects_id": 17, "reason": "x", "changes": [{"field": "items[0].qty", "new_value": "abc"}]})
check("明细数量非数字拒绝", not r["ok"] and "必须是数字" in r["error"])
r = app.handle_correct({"corrects_id": 17, "reason": "x", "changes": [{"field": "items[0].evil", "new_value": "1"}]})
check("明细非法键名拒绝", not r["ok"] and "只允许" in r["error"])
r = app.handle_correct({"corrects_id": 17, "reason": "x", "changes": [{"field": "items[0].qty", "new_value": "-3"}]})
check("明细负数拒绝", not r["ok"])
check("明细参数校验不触库", not fake.select_calls)
# 成功路径：old_value 从 extract_json 现读（数字 800 → 字符串 "800"）
fake = FakeCloud(
    select_results=[[{"id": 17, "doc_no": "PO1", "extract_json": EX}]],
    insert_results=[[{"id": 9, "seq": 3, "record_hash": "b" * 64}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 17, "reason": "单价录错", "changes": [
    {"field": "items[0].qty", "new_value": "900"}]})
check("明细更正成功", r.get("ok") is True and len(r["corrections"]) == 1)
row = fake.insert_calls[0][1]
check("明细 old_value 取自 extract_json", row["old_value"] == "800" and row["new_value"] == "900")
check("明细字段名按白名单格式落链", row["field"] == "items[0].qty")
check("明细更正前先查已有明细更正", any(c[0] == "corrections" for c in fake.select_calls))
# 第二次改同一格：old_value 必须是上一次更正后的值（有效值叠加）
fake = FakeCloud(
    select_results=[
        [{"id": 17, "doc_no": "PO1", "extract_json": EX}],
        [{"field": "items[0].qty", "new_value": "900"}]],
    insert_results=[[{"id": 10, "seq": 4, "record_hash": "c" * 64}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 17, "reason": "再改", "changes": [
    {"field": "items[0].qty", "new_value": "950"}]})
row = fake.insert_calls[0][1]
check("同格二次更正 old_value=上次改后值", r.get("ok") is True and row["old_value"] == "900")
# 行号越界
fake = FakeCloud(select_results=[[{"id": 17, "doc_no": "PO1", "extract_json": EX}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 17, "reason": "x", "changes": [
    {"field": "items[5].qty", "new_value": "1"}]})
check("明细行号越界拒绝", not r["ok"] and "没有第 6 行" in r["error"])
# 记录没有明细数据
fake = FakeCloud(select_results=[[{"id": 17, "doc_no": "PO1", "extract_json": None}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 17, "reason": "x", "changes": [
    {"field": "items[0].qty", "new_value": "1"}]})
check("无明细数据的记录拒绝明细更正", not r["ok"] and "没有可读的明细" in r["error"])
# 表头 + 明细混合批量：各落各的，old_value 各自从正确来源取
fake = FakeCloud(
    select_results=[[{"id": 17, "doc_no": "PO1", "supplier": "甲", "extract_json": EX}]],
    insert_results=[[{"id": 11, "seq": 5}], [{"id": 12, "seq": 6}]])
app.CLOUD = fake
r = app.handle_correct({"corrects_id": 17, "reason": "混合", "changes": [
    {"field": "supplier", "new_value": "乙"},
    {"field": "items[1].price", "new_value": "2.4"}]})
check("混合批量成功且各落一条", r.get("ok") is True and len(r["corrections"]) == 2
      and fake.insert_calls[0][1]["field"] == "supplier"
      and fake.insert_calls[1][1]["field"] == "items[1].price")
check("混合批量明细 old_value 正确", fake.insert_calls[1][1]["old_value"] == "2.35")

print("== _profile_record_confirmed 沉淀 ==")
# 首次：无档案行 → 插入
fake = FakeCloud(select_results=[[]])
app.CLOUD = fake
app._profile_record_confirmed("PO20261024001")
check("首次插入档案行", fake.insert_calls and fake.insert_calls[0][0] == "doc_no_profiles")
row = fake.insert_calls[0][1]
check("首次插入 source=confirmed", row["source"] == "confirmed" and row["examples"] == ["PO20261024001"])
# 重复：已存在 → 不写
fake = FakeCloud(select_results=[[{"id": 1, "examples": ["PO20261024001"]}]])
app.CLOUD = fake
app._profile_record_confirmed("PO20261024001")
check("重复写法不重复记", not fake.insert_calls and not fake.update_calls)
# 追加：已有别的样例 → PATCH 追加且封顶
fake = FakeCloud(select_results=[[{"id": 1, "examples": ["PO20260101001"]}]])
app.CLOUD = fake
app._profile_record_confirmed("PO20261024001")
check("新写法走更新", fake.update_calls and fake.update_calls[0][1]["examples"]
      == ["PO20260101001", "PO20261024001"])
check("更新带 id 条件", fake.update_calls[0][2] == "id=eq.1")
# 空编号：不触库
fake = FakeCloud()
app.CLOUD = fake
app._profile_record_confirmed(None)
app._profile_record_confirmed("  ")
check("空编号不触库", not fake.select_calls)

print("== handle_profile_examples ==")
r = app.handle_profile_examples({"examples": []})
check("空样例拒绝", not r["ok"])
r = app.handle_profile_examples({"examples": ["ab"]})
check("过短样例拒绝", not r["ok"] and "4-60" in r["error"])
app._PROFILE["at"] = 123.0
fake = FakeCloud(select_results=[[]])
app.CLOUD = fake
r = app.handle_profile_examples({"examples": ["PO20260101001", "PO20260102002"], "by": "实施方张工"})
check("实施方样例保存成功", r.get("ok") is True and r["examples"][0] == "PO20260101001")
row = fake.insert_calls[0][1]
check("实施方样例 source=manual", row["source"] == "manual" and row["confirmed_by"] == "实施方张工")
# at 已被重刷（保存 → 作废缓存 → summary 立即重学），绝不能还停留在旧时间戳
check("保存后画像立刻重学", app._PROFILE["at"] != 123.0)

print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
