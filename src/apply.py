"""人定方向，模型执行：把人工的决策翻译成一组具体的字段修改（patch）。

为什么"一键改"不能只是前端赋值
------------------------------
页面上一个单选项背后，往往不止一个字段要动：

  · 选了「第 3 行按数量×单价重算」 → 该行金额变了，**合计也得跟着变**；
  · 选了「这两行是同一个物料，合并成一行」 → 数量相加、金额相加、单价按
    金额÷数量重算，还要删掉一行、后面的行号整体前移；
  · 选了「按照片上的写法改成 26.4」 → 若这一行金额由它算出，金额、合计连带变。

这些联动关系是"语义到算术"的推导，前端一个 `setValue` 做不了——做得了的那部分
也容易漏改，漏改就会在入库时被勾稽拦下，用户看到的是"我明明点了改，怎么还报错"。

所以这里让模型来"执行"：它擅长把人的一句意图展开成一组具体的字段变更。

但执行权不等于决定权，这是本模块的核心约束
------------------------------------------
输入里的 decisions 是**人已经做完的决定**（选了哪个值、还是手动填了什么）。
模型的任务只是把决定落地，它不能否决、不能加戏。以下四条每条都有代码兜底
（见 verify_patches），模型违反哪一条，那条 patch 就被丢掉，并在界面上写明原因：

  1. 人工给了 value 的，patch 的值必须与它相等（不许"顺手改成更好的"）；
  2. 只能改被点名的字段 + 由算术必然联动的字段（行金额、合计）；
  3. 不许新增行、不许动没点名的行（删行只允许 merge_items 且两行都被点名）；
  4. 每个改动必须能由算术解释，解释不了就标成"待确认"，不能静默通过。

一句话：**人按下方向，模型负责执行，服务端负责核对执行结果对不对。**
"""
from __future__ import annotations

import json
import re

# 允许被直接修改的顶层字段，值是文本长度上限（防止模型把整段话塞进供应商名）
_TEXT_FIELDS = {"doc_no": 64, "supplier": 128, "order_date": 10, "currency": 8}
_ITEM_FIELD_RE = re.compile(r"^items\[(\d+)\]\.(qty|price|amount|name)$")

_DIRECT_FIELDS = set(_TEXT_FIELDS) | {"total_amount"}


APPLY_SYSTEM = """你是单据数据的修改执行器，不是决策者。

输入是：单据当前的抽取结果、人工已经做出的明确决定（decisions）。
你的唯一任务：把这些人工作出的决定，翻译成一组具体的字段修改，让数据变成"决定执行之后应该有的样子"。

铁律（违反任何一条，对应的修改会被服务端丢弃，整批回退）：
1. decisions 里带 value 的，value 是人工给出的最终值，必须原样使用，一个字都不许改；
   你无权"顺手把它改得更合理"。
2. 只允许修改 decisions 中出现的字段，以及由这些字段经算术关系必然要跟着变的字段：
   - 某行的 qty 或 price 变了 → 该行 amount = qty × price 要重算；
   - 某行 amount 变了，或发生了行合并 → total_amount = 所有行 amount 之和要重算。
   其它任何字段一律不许动。
3. 不许新增明细行；不许删除未在 decisions 中被点名的行。
4. 每一条 patch 必须写 why：说清它依据的是哪一条决定，或者哪一个等式。
5. 数字按原始精度给出，不要四舍五入成整数，不要编造看起来整齐的数。
6. 只输出 JSON，不要解释文字，不要 markdown 代码块。

输出格式：
{"patches":[
   {"op":"set","field":"items[2].amount","to":4100,"why":"第3行按 500 × 8.20 重算"},
   {"op":"set","field":"total_amount","to":77100,"why":"各行金额之和"}
 ],
 "notes":"一句话说明这批改动做了什么"}
op 只有两种取值：
  set          改一个字段。field 用 items[行号].qty|price|amount|name 或 total_amount|doc_no|supplier|order_date|currency
  merge_items  把 drop 行并入 keep 行：数量相加、金额相加、单价 = 金额 ÷ 数量；keep/drop 从 0 开始计数
"""


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if not s or s in {"-", ".", "-."}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_num_field(field: str) -> bool:
    if field == "total_amount":
        return True
    m = _ITEM_FIELD_RE.match(field)
    return bool(m and m.group(2) in ("qty", "price", "amount"))


def _get(extract: dict, field: str):
    m = _ITEM_FIELD_RE.match(field)
    if not m:
        return extract.get(field)
    items = extract.get("items")
    idx = int(m.group(1))
    if not isinstance(items, list) or idx >= len(items) or not isinstance(items[idx], dict):
        return None
    return items[idx].get(m.group(2))


def _set(extract: dict, field: str, value) -> None:
    m = _ITEM_FIELD_RE.match(field)
    if m:
        items = extract.get("items")
        idx = int(m.group(1))
        if not isinstance(items, list) or idx >= len(items):
            return
        items[idx][m.group(2)] = value
        return
    extract[field] = value


def build_user_message(extract: dict, decisions: list[dict], ocr_text: str) -> str:
    brief = []
    for d in decisions:
        item = {
            "field": d.get("field"),
            "人工决定": d.get("choice"),
            "值": d.get("value"),
            "备注": (d.get("text") or "")[:200] or None,
        }
        mw = d.get("merge_with")
        if mw is not None and str(mw).isdigit():
            item["合并对象"] = f"items[{int(mw)}]（两行合并成一行，行号从 0 开始）"
        brief.append(item)
    return (
        "人工决定（必须原样执行，不得更改其中给出的值）：\n"
        + json.dumps(brief, ensure_ascii=False, indent=1)
        + "\n\n当前抽取结果：\n"
        + json.dumps(extract, ensure_ascii=False)[:8000]
        + "\n\nOCR 原文（可能含识别错误，仅用于理解上下文，不是事实来源）：\n<DOC>\n"
        + (ocr_text or "")[:4000]
        + "\n</DOC>"
    )


def run_apply(
    client,
    image_b64: str | None,
    ocr_text: str,
    extract: dict,
    decisions: list[dict],
    model: str,
    timeout: float = 150.0,
) -> tuple[list[dict], str, dict]:
    """调模型产出 patch。返回 (原始 patch 列表, notes, usage)。

    返回值是**未经核对**的模型输出，一定要再过 verify_patches。
    """
    content: list[dict] = [{"type": "text", "text": build_user_message(extract, decisions, ocr_text)}]
    if image_b64:
        content.append({"type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64," + image_b64}})
    messages = [
        {"role": "system", "content": APPLY_SYSTEM},
        {"role": "user", "content": content},
    ]
    text, usage = client.chat(messages, model=model, temperature=0.0,
                              timeout=timeout, json_mode=True)
    obj = _loads(text) or {}
    patches = obj.get("patches")
    if not isinstance(patches, list):
        return [], "", usage
    return [p for p in patches if isinstance(p, dict)], str(obj.get("notes") or "")[:300], usage


def _loads(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|```$", "", text).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None


def _item_key(it) -> str:
    """物料归一：去空白标点、转小写。用于判断"两行是不是同一个东西"。

    刻意不用编辑距离：明细里「纸箱(五层)」与「纸箱(三层)」差两个字符，
    用距离判定会把它们判成同一物料然后合并——那是改坏数据，不是修数据。
    """
    if not isinstance(it, dict):
        return ""
    name = it.get("name")
    if not isinstance(name, str):
        return ""
    return re.sub(r"[\s（）()【】\[\]·.,，、\-_/]", "", name).lower()


def verify_patches(
    patches: list[dict],
    extract: dict,
    decisions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """服务端闸门：逐条核对模型给的 patch，不合规的丢掉并说明原因。

    返回 (accepted, rejected)。rejected 每项 {patch, code, why}。
    code 是要显示给人看的分类，不是给机器看的错误码堆。
    """
    accepted: list[dict] = []
    rejected: list[dict] = []

    # 人被点名的字段 / 行
    named_fields: set[str] = set()
    named_rows: set[int] = set()
    pinned: dict[str, object] = {}      # 人工已给值的字段 → 那个值
    for d in decisions:
        field = str(d.get("field") or "")
        if not field:
            continue
        named_fields.add(field)
        m = _ITEM_FIELD_RE.match(field)
        if m:
            named_rows.add(int(m.group(1)))
        # 「合并重复行」针对的是两行，人只点了其中一行，另一行由 merge_with 带过来。
        # 不带上就会被判成"动了没点名的行"而拒绝——那不是安全，那是不通。
        mw = d.get("merge_with")
        if isinstance(mw, int) and not isinstance(mw, bool):
            named_rows.add(mw)
        elif isinstance(mw, str) and mw.isdigit():
            named_rows.add(int(mw))
        if d.get("value") is not None and str(d.get("choice")) != "keep":
            pinned[field] = d["value"]

    items = extract.get("items") if isinstance(extract.get("items"), list) else []
    n_rows = len(items)

    for p in patches:
        op = str(p.get("op") or "set")
        why = str(p.get("why") or "")[:200]

        # ---- set
        if op == "set":
            field = str(p.get("field") or "")
            if not (_ITEM_FIELD_RE.match(field) or field in _DIRECT_FIELDS):
                rejected.append({"patch": p, "code": "field_not_allowed",
                                 "why": f"「{field}」不在允许修改的字段范围内"})
                continue

            # ① 人工已给值的字段：模型必须原样采用。
            #    不一致时不整条丢弃——丢弃会导致"人点了改，结果什么都没改"，
            #    比改错更难解释。这里强制回人工的值，并把这次越界写进 note 让界面显示。
            coerced = None
            if field in pinned:
                want = pinned[field]
                got = p.get("to")
                same = (abs(_num(want) - _num(got)) < 1e-9
                        if _is_num_field(field) and _num(want) is not None and _num(got) is not None
                        else str(want).strip() == str(got).strip())
                if not same:
                    coerced = f"模型建议改成 {got}，已按人工指定的 {want} 执行"
                    why = (why + "｜" + coerced)[:260] if why else coerced
                    p = dict(p, to=want)

            # ② 改动权限：被点名字段 或 勾稽联动字段
            m = _ITEM_FIELD_RE.match(field)
            if m:
                row = int(m.group(1))
                if row >= n_rows:
                    rejected.append({"patch": p, "code": "row_out_of_range",
                                     "why": f"明细只有 {n_rows} 行，模型给出了第 {row + 1} 行"})
                    continue
                is_link = (field.endswith(".amount") and row in named_rows)
                if row not in named_rows or not is_link:
                    if field not in named_fields:
                        rejected.append({"patch": p, "code": "field_not_named",
                                         "why": f"第 {row + 1} 行没有被点名，不允许顺带修改「{m.group(2)}」"})
                        continue
            elif field == "total_amount":
                pass  # 合计是勾稽联动的终点，永远允许重算
            elif field not in named_fields:
                rejected.append({"patch": p, "code": "field_not_named",
                                 "why": f"「{field}」没有被点名，不允许顺带修改"})
                continue

            # ③ 类型闸门
            val = p.get("to")
            if _is_num_field(field):
                nv = _num(val)
                if nv is None:
                    rejected.append({"patch": p, "code": "bad_type",
                                     "why": f"「{field}」是数字字段，模型给了 {val!r}"})
                    continue
                if field.endswith((".qty", ".price", ".amount")) or field == "total_amount":
                    if nv < 0:
                        rejected.append({"patch": p, "code": "bad_value",
                                         "why": f"「{field}」不能为负数（{nv}）"})
                        continue
                val = nv
            else:
                s = "" if val is None else str(val).strip()
                limit = _TEXT_FIELDS.get(field)
                if limit is None:      # items[i].name
                    m2 = _ITEM_FIELD_RE.match(field)
                    limit = 80 if (m2 and m2.group(2) == "name") else 64
                if not s and not field.endswith(".name"):
                    rejected.append({"patch": p, "code": "empty_value",
                                     "why": f"「{field}」被改成了空值"})
                    continue
                if len(s) > limit:
                    rejected.append({"patch": p, "code": "too_long",
                                     "why": f"「{field}」超长（{len(s)} 字，上限 {limit}）"})
                    continue
                val = s
                if field == "order_date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
                    rejected.append({"patch": p, "code": "bad_type",
                                     "why": f"下单日期不是 YYYY-MM-DD：{val}"})
                    continue

            accepted.append({"op": "set", "field": field, "to": val,
                             "from": _get(extract, field), "why": why,
                             "pinned_by_user": field in pinned,
                             "coerced": coerced})

        # ---- merge_items
        elif op == "merge_items":
            try:
                keep = int(p.get("keep"))
                drop = int(p.get("drop"))
            except (TypeError, ValueError):
                rejected.append({"patch": p, "code": "bad_type", "why": "合并行没给出有效的行号"})
                continue
            if not (0 <= keep < n_rows and 0 <= drop < n_rows) or keep == drop:
                rejected.append({"patch": p, "code": "row_out_of_range",
                                 "why": f"合并行号不合法（keep={keep}, drop={drop}, 共 {n_rows} 行）"})
                continue
            if keep not in named_rows or drop not in named_rows:
                rejected.append({"patch": p, "code": "field_not_named",
                                 "why": "只允许合并被人工点名的那几行"})
                continue
            k1, k2 = _item_key(items[keep]), _item_key(items[drop])
            if not k1 or k1 != k2:
                rejected.append({"patch": p, "code": "not_same_item",
                                 "why": f"第 {keep + 1} 行与第 {drop + 1} 行不是同一物料，不允许合并"})
                continue
            accepted.append({"op": "merge_items", "keep": keep, "drop": drop, "why": why})

        else:
            rejected.append({"patch": p, "code": "bad_op",
                             "why": f"不认识的操作 {op!r}，已丢弃"})

    # 同字段只保留最后一条（模型有时把"重算金额"和"再改一次"都输出）
    seen: dict[str, int] = {}
    final: list[dict] = []
    for a in accepted:
        key = a["field"] if a["op"] == "set" else f"merge:{a['keep']}:{a['drop']}"
        if key in seen:
            final[seen[key]] = a
        else:
            seen[key] = len(final)
            final.append(a)

    # 去掉"改前改后一样"的条目：模型经常顺手确认一下合计（77100 → 77100），
    # 显示出来会让人以为动了钱，还会稀释真正的改动。
    #
    # ⚠ 必须按字段类型选比较方式。`_num` 会把非数字字符剥掉，
    # `_num("P020261024001")` 与 `_num("PO20261024001")` 都是 20261024001.0 ——
    # 用数值比较去重，恰好会把「P0 → PO」这个最核心的修正判成"没改"而静默丢弃。
    final = [p for p in final
             if p["op"] != "set"
             or not _same_value(p.get("from"), p.get("to"), _is_num_field(p["field"]))]
    return final, rejected


def _same_value(a, b, numeric: bool = False) -> bool:
    """改前改后是否等价。numeric=True 才允许"550,290.00 与 550290 视为相同"，
    文本字段一律逐字符比较——编号、供应商名差一个字符就是两个东西。"""
    if a is None or b is None:
        return a is None and b is None
    if numeric:
        na, nb = _num(a), _num(b)
        if na is not None and nb is not None:
            return abs(na - nb) < 1e-9
    return str(a).strip() == str(b).strip()


def apply_patches(extract: dict, patches: list[dict]) -> dict:
    """把通过闸门的 patch 真的落到一份**副本**上，原对象不动。

    合并行必须按行号从大到小处理，否则先删小的会让后面的行号整体前移，
    第二次删除就删错了行——这类错在演示时最难解释，所以这里显式排序。
    """
    out = json.loads(json.dumps(extract))
    merges = [p for p in patches if p["op"] == "merge_items"]
    for p in sorted(merges, key=lambda x: x["drop"], reverse=True):
        items = out.get("items")
        if not isinstance(items, list):
            continue
        keep, drop = p["keep"], p["drop"]
        if drop >= len(items) or keep >= len(items) or keep == drop:
            continue
        # 按 drop 从大到小处理，删掉高行号不会影响低行号，keep/drop 始终有效。
        # 这里不要再"跟着挪 keep"——挪了反而会把内容并到隔壁行。
        src = items[drop]
        dst = items[keep]
        q1, q2 = _num(dst.get("qty")), _num(src.get("qty"))
        a1, a2 = _num(dst.get("amount")), _num(src.get("amount"))
        if q1 is not None and q2 is not None:
            dst["qty"] = q1 + q2
        if a1 is not None and a2 is not None:
            dst["amount"] = round(a1 + a2, 4)
        qt, at = _num(dst.get("qty")), _num(dst.get("amount"))
        if qt and at is not None:
            dst["price"] = round(at / qt, 6)
        del items[drop]

    for p in patches:
        if p["op"] == "set":
            _set(out, p["field"], p["to"])
    return out


def explain(patches: list[dict], after: dict, decisions: list[dict] | None = None) -> list[dict]:
    """哪些改动需要人再看一眼？分两类，都会挡住"直接入库"。

    ① 算不平：改动之后勾稽仍然不成立，说明还没改完或者改错了。
    ② 动了没被点名的金额：`total_amount` 是唯一一个"改了就等于改了这单的钱"的
       联动字段。模型按"各行金额求和"重算它，算术上完全正确，但业务上可能恰恰相反
       ——票面合计 70000、明细只有 30100，很可能是**漏了一行明细**，此时对的
       是合计，错的是明细。系统没有依据判断是哪一种，所以不许静默改，必须让人看。

    为什么不一刀切拒绝：票面本来就可能跟算术不符（折扣、税额、抹零），
    强行要求等式成立会把正确数据判成错误。所以只降级成"待确认"，把算不平的
    那句话原样摆到人面前，由人决定要不要入库。
    """
    named = {str(d.get("field")) for d in (decisions or []) if isinstance(d, dict)}
    items = after.get("items") if isinstance(after.get("items"), list) else []
    notes: list[dict] = []

    touched_rows = set()
    for p in patches:
        if p["op"] == "merge_items":
            touched_rows.add(p["keep"])
        else:
            m = _ITEM_FIELD_RE.match(p["field"])
            if m:
                touched_rows.add(int(m.group(1)))

    for i in sorted(touched_rows):
        if i >= len(items) or not isinstance(items[i], dict):
            continue
        q, pr, am = _num(items[i].get("qty")), _num(items[i].get("price")), _num(items[i].get("amount"))
        if q is None or pr is None or am is None:
            continue
        if abs(q * pr - am) > 0.011:
            notes.append({"field": f"items[{i}].amount", "verified": False,
                          "why": f"改动后第 {i + 1} 行仍算不平：{q} × {pr} = {round(q * pr, 4)}，金额为 {am}"})

    total = _num(after.get("total_amount"))
    amounts = [_num(it.get("amount")) for it in items if isinstance(it, dict)]
    known = [a for a in amounts if a is not None]
    if total is not None and known and len(known) == len(amounts):
        s = round(sum(known), 4)
        if abs(s - total) > 0.011:
            notes.append({"field": "total_amount", "verified": False,
                          "why": f"改动后明细之和 {s} 与合计 {total} 仍差 {round(total - s, 4)}"})

    # 合计被联动改过，但人没点名它 —— 金额变更必须由人确认
    if "total_amount" not in named:
        for p in patches:
            if p["op"] == "set" and p["field"] == "total_amount":
                notes.append({
                    "field": "total_amount", "verified": False, "linkage": True,
                    "why": f"合计不是你点的字段，是按各行金额重算出来的（{p.get('from')} → {p.get('to')}）。"
                           f"如果这张单本来就漏了明细，对的恰恰是合计——请对着票面看一眼再入库",
                })
                break

    return notes