"""三层校验（服务端权威版本）+ 疑点判定。

为什么要把 validate() 从浏览器搬到服务端
------------------------------------------
浏览器里的校验有两个问题：
  1. 结果可以被使用者改掉（F12 一行），所以它不能作为入库依据；
  2. 入库由浏览器直连数据库完成，`validate_ok` 存的是浏览器算出来的布尔值，
     数据库侧没有任何独立判断。

现在：浏览器那份只用于"上传后立刻看到结果"，真正写进库的 `validate_ok` 与
`issues` 由本模块计算。规则与前端逐条对齐（同样的容差、同样的正则、同样的文案），
两边不一致时以本模块为准。

与前端唯一的差异：日期用 strptime 严格校验，`2026-02-31` 在 JS 里会被 Date.parse
滚动成 3 月 3 日，这里是错误。严格一侧更符合业务预期，且不放松任何前端已拦的项。
"""
from __future__ import annotations

import re

import patterns

# 金额容差：分位以下的浮点误差放过，超过 1.1 分钱视为不一致
TOL = 0.011

# 人民币的各种写法（归一后比较）。这是**业务约定**，不是格式猜测：
# 只有这些写法可以直接按原值入库，其余都要折算。
_CNY_ALIASES = {"CNY", "RMB", "CNH", "人民币", "元"}
_ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")

_FIELD_LABEL = {
    "doc_no": "单据编号",
    "supplier": "供应商",
    "order_date": "下单日期",
    "total_amount": "合计金额",
    "currency": "币种",
    "items": "明细",
}


def _num(v) -> float | None:
    """与前端 numOrNull 等价：剥掉非数字字符，剥完为空或 NaN 则视为缺失。"""
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


def _is_int(v) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        return v.is_integer()
    s = str(v).strip()
    return bool(re.fullmatch(r"-?\d+", s))


def _jsnum(v: float) -> str:
    """按 JS 的 Number.prototype.toString 规则渲染，保证两边告警文案逐字一致。
    Python 的 float 会把 10 打成 10.0，前端会打 10——用户看到的是同一张单的两套说法。"""
    if v is None:
        return "null"
    if float(v).is_integer():
        return str(int(v))
    return repr(float(v))


def _fix(value, how: str, why: str) -> dict:
    """一条"修改方向"。

    为什么是"方向"而不是"正确答案"：同一个疑点往往有多条自洽的解释，规则层无从
    判断是数量错、单价错还是金额错。所以这里只列**可以照着执行的候选**，每条都
    写明它是怎么来的、代价是什么，最后由人挑。系统给一个"正确答案"反而是撒谎。

    how 的取值决定这条候选的可信度，前端据此决定默认选项：
      rule       通用规则推导，与客户无关（日期写法归一），确定性、可复现
      profile    由本客户的历史编号/配置推断（"历史里长这样"），是统计结论，
                 样本可能被污染，所以默认不占最高可信度
      recompute  由同单其它数字重算得来（数量×单价、明细求和），依赖其它行也正确
      manual     没有候选，只能人工填
    """
    return {"value": value, "how": how, "why": why}


# ---------------------------------------------------------------------------
# 槽位错位：数字读对了，但装错了格子
# ---------------------------------------------------------------------------
# 这一节处理的问题和前面都不一样。前面的疑点是**数字本身**有问题（勾稽不平、
# 日期不可解析）。这里数字全对，只是被放进了错误的字段——校验层的等号一个都不会
# 响，因为每一行「数量、单价、金额」三个槽里只要有一个是空的，勾稽就被跳过了。
#
# 两个真实案例（都来自 HF 的采购单据，有字段级 gold 可以核对）：

def _price_is_line_total(items: list, total: float | None) -> dict | None:
    """判断明细的「单价」槽里装的其实是该行的金额。

    案例：法语 PO 表头是 `Prix unitaire / Remise / Montant`（单价 / 折扣 / 金额）。
    模型把 Montant 列的数字填进了 price，amount 三行全空。gold 里该行的 line_total
    正是 137,58€ / 125,79€ / 120,00€——和填进 price 的值一字不差。

    原来的规则只看见「金额全空」，报的是「3 行明细缺少金额」。**方向是反的**：
    照着去补金额，会补出 137.58 × 45866 这种比原值大十万倍的数。

    判据不做阈值猜测，而是让两个假设去竞争解释合计：
      H1 `price` 是单价   ⇒ Σ(qty×price) 应该 ≈ 合计
      H2 `price` 是行金额 ⇒ Σprice        应该 ≈ 合计
    实测这张单 H1 差五个数量级、H2 只差 9.5%（差额正是含税与运费），胜负分明。
    **只有两个假设的差距是几十倍以上时才下结论**——qty 全为 1 时两个假设等价
    （price 本来就是 amount），此时不该报，这正是靠"竞争"而不是靠"金额非空"判断的好处。
    """
    if total is None or total <= 0 or not items:
        return None
    if not all(isinstance(it, dict) for it in items):
        return None
    prices = [_num(it.get("price")) for it in items]
    amounts = [_num(it.get("amount")) for it in items]
    qtys = [_num(it.get("qty")) for it in items]
    # 只处理"单价全有、金额全无、数量全有"这一种形态；其它形态有别的成因，不在这管。
    if any(p is None for p in prices) or any(a is not None for a in amounts):
        return None
    if any(q is None for q in qtys):
        return None

    sum_price = sum(prices)
    sum_qp = sum(q * p for q, p in zip(qtys, prices))
    if sum_price <= 0:
        return None
    rel_price = abs(sum_price - total) / total
    rel_qp = abs(sum_qp - total) / total
    # 两个条件缺一不可：按行金额解释得还不错（35% 以内，容得下含税与运费），
    # 且按单价解释差得远（至少 3 倍，且比前者差 5 倍以上）。
    if rel_price > 0.35 or rel_qp < 3 or rel_price * 5 >= rel_qp:
        return None
    return {"prices": prices, "qtys": qtys, "sum_price": sum_price,
            "sum_qp": sum_qp, "rel_price": rel_price, "rel_qp": rel_qp}


def _total_has_no_source(items: list, total: float | None,
                         has_price_signal: bool) -> dict | None:
    """判断「合计金额」这一格里的数字根本不是钱。

    案例：法语收货单的表尾是 `Total commandé / Total reçu / Total refusé /
    Total accepté`（订购 / 实收 / 拒收 / 验收 的**件数**）。模型把 1 650 填进了
    total_amount，还顺手给了个 currency=EUR。gold 里写得很清楚：
    summary.total_accepted = "1650"，是三行各 550 件的和。

    这种错最危险的地方在于它**看起来像正常单据**：有编号、有供应商、有日期、
    有合计、还是正数——四层校验全过。只有把「合计」和「数量之和」摆在一起才看得见。

    返回 None 表示这张单的合计有价格支撑，不归这里管。
    """
    if total is None or has_price_signal or not items:
        return None
    qtys = [_num(it.get("qty")) for it in items if isinstance(it, dict)]
    qty_sum = None if (not qtys or any(q is None for q in qtys)) else sum(qtys)
    return {"qty_sum": qty_sum, "qtys": qtys, "n": len(qtys)}


def validate(d: dict, profile: "patterns.DocProfile | None" = None) -> list[dict]:
    """返回 issue 列表。level: err 阻断 / warn 提示；layer: L1 类型 / L2 业务 / L3 勾稽。

    每条 issue 可选带 `fixes`：可执行的修改方向列表（见 _fix）。只要给出 fixes，
    页面就会把它渲染成"选一个方向"的选项，而不是干巴巴一行红字——只告诉人"这里有
    问题"却不动手，人只能自己回头翻照片一个个敲。

    `profile` 是这家客户的编号格式画像（见 patterns.py）。**不传就只做通用判断**：
    编号格式属于"客户级知识"，没有画像时本模块不猜——这比拿某一家客户的规律去
    套所有人安全得多。
    """
    out: list[dict] = []

    # ---- L1 类型
    doc_no = d.get("doc_no")
    if doc_no is not None and not isinstance(doc_no, str):
        out.append({"level": "err", "layer": "L1", "field": "doc_no", "msg": "单据编号不是字符串"})

    date = d.get("order_date")
    if date not in (None, ""):
        raw_date = str(date).strip()
        # 日期写法随客户/来源而变（2026/8/21、2026年8月21日、20260821）。
        # 只认一种写法等于把"这一版数据的写法"当成唯一写法——归一化才是通用做法，
        # 归不了再报错，而不是先判它"格式不对"。
        iso = patterns.normalize_date(raw_date)
        if iso is None:
            out.append({"level": "err", "layer": "L1", "field": "order_date",
                        "msg": f"下单日期「{raw_date}」不是可识别的日期"
                               f"（支持 2026-08-21 / 2026/8/21 / 2026年8月21日 / 20260821）"})
        elif iso != raw_date:
            out.append({"level": "warn", "layer": "L1", "field": "order_date",
                        "msg": f"下单日期写作「{raw_date}」，入库前统一为 {iso}",
                        "fixes": [_fix(iso, "rule",
                                       "多种日期写法指同一天，统一成 ISO 便于入库与比较；"
                                       "这一步只归一写法，不改变日期本身")]})

    items = d.get("items")
    if not isinstance(items, list) or not items:
        out.append({"level": "err", "layer": "L1", "field": "items", "msg": "模型未抽到任何明细行"})
    else:
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                out.append({"level": "err", "layer": "L1", "field": f"items[{i}]",
                            "msg": f"第 {i + 1} 行不是对象"})
                continue
            qty = it.get("qty")
            if qty is not None and not _is_int(qty):
                out.append({"level": "err", "layer": "L1", "field": f"items[{i}].qty",
                            "msg": f"第 {i + 1} 行数量不是整数：{qty}"})
            for k in ("qty", "price", "amount"):
                v = it.get(k)
                if v is not None and _num(v) is None:
                    out.append({"level": "err", "layer": "L1", "field": f"items[{i}].{k}",
                                "msg": f"第 {i + 1} 行 {k} 不是数字：{v}"})

    # ---- L2 业务规则
    #
    # 编号格式是**客户级知识**，不是通用规律。原来这里写死 `P0\d{9,}`，等于把
    # 某一家客户的编号规则当成了世界的规则：换成 `XK-2026-0042` 立刻误报，而且
    # 误报得理直气壮。现在改由画像（patterns.py）判定，画像来自历史单据或配置，
    # 拿不到画像就**不猜**——只说"没法判断"，不说"格式不对"。
    no = "" if doc_no is None else str(doc_no).strip()
    if no:
        prof = profile or patterns.EMPTY
        cand, why = prof.suggest(no)
        if cand:
            # 自动归一闸门。suggest 已保证三件事：只差一个形近字符、候选在历史里
            # 的支持度严格高于当前写法、样本数 ≥ MIN_SAMPLES。这里再加第四道——
            # 归一后的值必须**完全符合**画像（accepts 独立复查结构/前缀/位数）。
            # 四条同时成立，归一就不是猜：原值不符、候选是唯一更站得住脚的写法、
            # 且候选本身就是本厂格式。
            # 此时降级为 info（归一事件）而不是 warn（疑点）：
            #   · warn 会触发看图复核（见 review_targets ②），而形近字符在图上
            #     根本没有判据（O 与 0 像素级同形），复核必然空转，人被迫在两个
            #     看不见差别的选项里选一个——问一遍是浪费，问二十遍是折磨；
            #   · info 的语义是"已按本厂规律自动处理，供过目，不需要你做选择"。
            #     这张单若有其他疑点仍会停在人工队列，但人不再被问 O/0。
            #   · "只询问一次，之后记住"由此成立：证据弱时（历史样本不足）仍走
            #     warn 问人；人确认入库后，正确编号进历史 → 画像支持度增强 →
            #     下次达到闸门自动归一。档案 = 入库历史本身，无需另建配置。
            # 归一前的原值保留在 normalized.from，随 issues 落库可追溯；出处层
            # （L4）会拿原值对原文（见 app.validate_with_source），不会把
            # "写法被归一"误报成"值没有出处"。
            if prof.accepts(cand):
                d["doc_no"] = cand
                out.append({"level": "info", "layer": "L2", "field": "doc_no",
                            "msg": f"单据编号「{no}」已按{prof.describe()}归一为「{cand}」，"
                                   f"入库前请核对；不同意可手动改回",
                            "normalized": {"from": no, "to": cand, "basis": why}})
            else:
                out.append({"level": "warn", "layer": "L2", "field": "doc_no",
                            "msg": f"单据编号「{no}」与历史格式不符，疑似有一个字识别错：{why}",
                            "fixes": [_fix(cand, "profile", why)]})
        elif why:
            out.append({"level": "warn", "layer": "L2", "field": "doc_no",
                        "msg": f"单据编号「{no}」{why}"})

    total = _num(d.get("total_amount"))
    items_now = d.get("items") if isinstance(d.get("items"), list) else []
    # "没有金额"要分两种情况，不能一律当漏抽：
    #   · 单据本来就没有价格列（收货单、装箱单、出库单）——明细里 qty 有值但
    #     price/amount 全空。这是结构如此，不是识别失败。
    #   · 单据有价格列但没抽到——通常表现为部分行有金额、部分没有，或单价在而金额空。
    # 判据用「有没有任何一行带价格信息」：一条都没有 ⇒ 更像无价单据。
    # 这个是拿 HF 的 6 张收货单测出来的：它们压根没有金额，原来的必填规则 5/6 全报
    # 「未抽到合计金额」，把"schema 不适配"记成了"这张单有问题"。
    has_price_signal = any(
        _num(it.get("price")) is not None or _num(it.get("amount")) is not None
        for it in items_now if isinstance(it, dict))

    # 有价单据必须有下单日期：没有日期就无法确定价格有效期与交期归属，
    # 而日期恰恰是退化图最容易丢掉的一项（实测传真件那张日期整行没抽到，
    # 但因为没有金额疑点，系统照样放行了）。
    # 无价单据（收货单/装箱单）只提醒：这类单据的日期语义本就不同。
    if not d.get("order_date"):
        if has_price_signal or total is not None:
            out.append({"level": "err", "layer": "L2", "field": "order_date",
                        "msg": "未抽到单据日期。有价单据缺日期无法确定价格与交期归属，"
                               "请核对原图该字段是否被遮挡或模糊。"})
        else:
            out.append({"level": "warn", "layer": "L2", "field": "order_date",
                        "msg": "未抽到单据日期（本单无金额信息，收货/装箱类单据的日期"
                               "语义与采购订单不同，可人工确认后补填）。",
                        "fixes": []})

    if total is None:
        if items_now and not has_price_signal:
            out.append({
                "level": "warn", "layer": "L2", "field": "total_amount",
                "msg": "本单未识别到任何金额信息（明细只有数量，无单价/金额）。"
                       "收货单、装箱单、出库单这类单据本来就没有价格列，属正常；"
                       "若这张其实是采购订单，请核对是否整列漏抽了金额。",
                "fixes": [],
            })
        else:
            out.append({"level": "err", "layer": "L2", "field": "total_amount",
                        "msg": "未抽到合计金额"})
    elif total <= 0:
        out.append({"level": "err", "layer": "L2", "field": "total_amount", "msg": "合计金额必须大于 0"})
    elif total > 1e8:
        out.append({"level": "warn", "layer": "L2", "field": "total_amount",
                    "msg": "合计金额超过 1 亿，请人工复核"})

    cur = str(d.get("currency") or "").strip().upper()
    if cur and cur not in _CNY_ALIASES:
        # 这里刻意不给 fixes：币种错不是"改个值"能解决的，要按汇率折算，
        # 折算率系统无从知道。给一条假建议（原样填回去）比不给更糟——它会让
        # 人以为"系统已经处理了"。
        if _ISO_CURRENCY.match(cur):
            out.append({"level": "warn", "layer": "L2", "field": "currency",
                        "msg": f"币种为 {cur}（ISO 4217 代码），不是人民币。入库前必须按汇率折算"
                               f"——直接当 CNY 处理会让金额差几倍"})
        else:
            # 认不出币种代码 ≠ 一定是外币：也可能是 OCR 把 "CNY" 读成了 "CN V"。
            # 两种可能都成立，所以只提示、不推断。
            out.append({"level": "warn", "layer": "L2", "field": "currency",
                        "msg": f"币种「{cur}」不是标准的三字母代码，无法判断是否需要折算，请人工确认"})

    # 无价单据上填了币种，等于报告一个从来没读到的值。
    # 实测 5 张同来源的收货单（同为法语页面、同一批供应商）分别给出 CNY / CNY /
    # EUR / USD / CNY——同一批单据的币种各不相同，说明这个字段不是读出来的，
    # 是模型在"必填"约束下按地址或语言补齐的。金额为空时它不影响入库，
    # 但不能当成已核实字段往下游传。
    if cur and not has_price_signal and total is None:
        out.append({"level": "warn", "layer": "L2", "field": "currency",
                    "code": "currency_inferred",
                    "msg": f"本单没有任何金额，币种却填了 {cur}。单据上没有金额也就没有"
                           f"币种标识可读，这个值来自模型的推断或默认值，不是票面明示的。"
                           f"金额栏为空时它不影响入库，但请勿当作已核实字段传给下游。",
                    "fixes": []})

    # ---- L3 槽位错位：数字读对了，装错了格子
    #
    # 这两个判断必须排在「明细之和 vs 合计」之前。原因不是顺序美观：老逻辑看到
    # 「金额全空」会报「N 行明细缺少金额」，那是一条**方向相反**的提示（见
    # _price_is_line_total 的说明）。先把成因定下来，再决定要不要走通用分支。
    slot = _price_is_line_total(items_now, total)
    no_source = _total_has_no_source(items_now, total, has_price_signal)
    diagnosed = bool(slot or no_source)
    if slot:
        n = len(slot["prices"])
        ratio = slot["sum_qp"] / total
        out.append({
            "level": "err", "layer": "L3", "field": "items[0].amount",
            "code": "slot_price_is_amount",
            "msg": (f"第 1~{n} 行填进「单价」的其实是各行金额：单价之和 "
                    f"{slot['sum_price']:.2f} 与合计 {total:.2f} 只差 "
                    f"{slot['rel_price'] * 100:.1f}%（差额通常正是税额与运费），"
                    f"而「单价×数量」之和是 {slot['sum_qp']:.0f}——合计的 {ratio:.0f} 倍。"
                    f"按单价解释不成立，按行金额解释正好吻合：表头的「金额」列被读进了"
                    f"单价这一格，「单价」列没读到。若原样入库，系统会把金额再乘一次数量，"
                    f"各行金额被放大 {ratio:.0f} 倍。"),
            "fixes": [
                _fix(round(p, 2), "recompute",
                     f"把该值移到第 {i + 1} 行 amount。依据：这些数值按「行金额」求和与合计"
                     f"吻合度远高于按「单价×数量」求和；单价仍需对照原图的单价列重读，"
                     f"这一项系统给不出值")
                for i, p in enumerate(slot["prices"])
            ],
        })
    if no_source:
        q = no_source["qty_sum"]
        if q is not None and abs(total - q) <= TOL:
            out.append({
                "level": "err", "layer": "L3", "field": "total_amount",
                "code": "total_is_qty",
                "msg": (f"合计金额 {total:.2f} 恰好等于 {no_source['n']} 行数量之和"
                        f"（{' + '.join(_jsnum(x) for x in no_source['qtys'])} = {_jsnum(q)}），"
                        f"而明细里没有任何单价或金额。这个「合计」是件数的合计，不是钱的合计"
                        f"——收货单 / 装箱单的表尾本来就是订购数、实收数、拒收数，"
                        f"没有金额栏。请清空合计金额，件数应登记在数量栏。"
                        f"另：本单没有金额，币种同属无从核实。"),
                "fixes": [],
            })
        else:
            out.append({
                "level": "err", "layer": "L3", "field": "total_amount",
                "code": "total_no_source",
                "msg": (f"明细里没有任何单价或金额，但合计金额填了 {total:.2f}——"
                        f"这个数字在单据内部找不到任何来源可以核对（既不是各行金额之和，"
                        f"也不是数量之和），多半是表尾另一栏的数值被当成了合计。"
                        f"请对照原图确认该栏的表头写的到底是金额还是数量。"),
                "fixes": [],
            })

    # ---- L3 勾稽：单据内部数字互相验证
    if isinstance(items, list) and items:
        row_sum = 0.0
        missing_amount = 0
        pending: list[tuple[int, float]] = []  # (行号, 由 数量×单价 可算出的金额)
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            q, p, a = _num(it.get("qty")), _num(it.get("price")), _num(it.get("amount"))
            if a is not None:
                row_sum += a
            elif q is not None and p is not None:
                # 金额没抽到，但数量与单价都在：这一行"可以算出来"，先记账不算数。
                # 关键：**不能直接把它加进 row_sum 去下结论**——列错位时 qty 可能
                # 被识别成 42.50、price 被识别成 1275，q×p 会得出天文数字，反而
                # 制造一条"合计错了 5 万"的假告警。只有在补出来的总和恰好能
                # 对上合计时（见下），才敢把这些值当作"漏抽的金额"交给用户。
                pending.append((i + 1, round(q * p, 2)))
            elif q is not None or p is not None:
                # 有数量或单价、却连算都算不出来：真正的资料不全。
                missing_amount += 1
            if q is not None and p is not None and a is not None:
                calc = q * p
                if abs(calc - a) > TOL:
                    # 相差一个数量级，成因通常不是"金额写错"，而是列错位：
                    # 把金额读成了数量（200 × 200 = 40000 对 200），或者数量整列
                    # 被识别成了别的字段。此时给"按 q×p 重算"等于教用户把 200
                    # 改成 40000（放大 200 倍），是会造成数据损毁的建议。
                    ratio = (calc / a) if a else float("inf")
                    wild = ratio > 10 or ratio < 0.1
                    # 尾数级差异（千分之一以内）：q×p 与金额几乎相等，只差最后几位。
                    # 实测成因是**单价的小数位被多读一位**：60 × 6.426 = 385.56 对
                    # 金额 385.20，而 385.20 ÷ 60 = 6.42——真实单价是 6.42，
                    # 抽取时把右边一列的首位数字粘到了单价后面。
                    # 这种情况下"按 q×p 改金额"方向是反的：该改的是单价。
                    # 规则层没有依据判断该改哪个，所以只提示、不给值。
                    tail = bool(a) and abs(calc - a) / abs(a) < 0.001
                    if wild:
                        detail = ("——两者相差一个数量级以上，更像列错位或数量被识别成了金额，"
                                  "请对照原图逐列核对后再改")
                    elif tail:
                        detail = (f"——只差 {calc - a:+.2f}（{abs(calc - a) / abs(a) * 100:.3f}%），"
                                  f"属尾数级差异，通常是单价小数位被多读/少读一位，"
                                  f"请核对该行单价，不要改金额")
                    else:
                        detail = ""
                    out.append({
                        "level": "err", "layer": "L3", "field": f"items[{i}].amount",
                        "msg": (f"第 {i + 1} 行勾稽不通过：{_jsnum(q)} × {_jsnum(p)} = {calc:.2f}，"
                                f"但金额记为 {a:.2f}" + detail),
                        "fixes": [] if (wild or tail) else
                        [_fix(round(calc, 2), "recompute",
                              f"按 {_jsnum(q)} × {_jsnum(p)} 重算；"
                              f"若票面金额本身没错，则该改数量或单价（需人工核对）")]})
        # 槽位错位在上面已经定过成因，这里就不再走「明细之和 vs 合计」的通用分支：
        # 否则同一件事会同时出两条互相矛盾的提示——一条说"金额被填进了单价格"，
        # 另一条说"缺 3 行金额"，后者方向是反的。
        if not diagnosed and total is not None and abs(row_sum - total) > TOL:
            diff = total - row_sum
            cand = row_sum + sum(v for _, v in pending)
            if pending and not missing_amount and abs(cand - total) <= TOL:
                # 把"漏抽的那几行金额"补上后，合计恰好对上：这证明缺的确实只是
                # 金额字段，而不是合计写错。此时才可以把补算值作为修改方向给出——
                # 因为它同时被"单价×数量"和"合计"两个独立来源印证。
                for row, val in pending:
                    out.append({
                        "level": "warn", "layer": "L3",
                        "field": f"items[{row - 1}].amount",
                        "msg": f"第 {row} 行没抽到金额；按 单价×数量 应为 {val:.2f}，"
                               f"且补齐后明细之和与合计 {total:.2f} 正好吻合",
                        "fixes": [_fix(val, "recompute",
                                       "按该行 单价×数量 推算，并用合计反向验证过")],
                    })
            elif missing_amount or pending:
                # 有行缺金额时，差额可能只是缺的那几行，不是"合计错了"。
                # 这时给出"把合计改成 row_sum"的方向，会让人把一张正确的单改错——
                # 所以不给方向。但**仍然要拦**：明细不全的单不能自动入库，
                # 否则"抽漏一行"会变成"静默放行一张没核对过的单"。
                out.append({
                    "level": "err", "layer": "L3", "field": "total_amount",
                    "msg": f"有 {missing_amount + len(pending)} 行明细缺少金额，无法核对"
                           f"「明细之和=合计」（已计入之和 {row_sum:.2f}，合计 {total:.2f}，"
                           f"差额 {diff:.2f}）。请先补齐这些行的金额，再判断合计是否有误。",
                    "fixes": [],
                })
            elif row_sum > 0 and 0.001 <= abs(diff) / row_sum <= 0.25:
                # 差额落在「常见税额 / 运费 / 折扣」的区间里，说明这张单的「合计」
                # 与明细**很可能不是同一个口径**：明细是税前或未打折，合计是含税、
                # 含运费或已折扣。这个恒等式在这里本来就不成立。
                #
                # 这条是拿真实的采购订单测出来的（法语 PO：Total HT + TVA + 运费 = Total TTC）。
                # 原来一律判 err 并建议"把合计改成明细之和"——照做就是把含税总额改成
                # 税前小计，少记一笔税。6 张里 4 张踩了这个坑。
                #
                # 为什么不写"税率 8%"这种具体归因：规则层只有结构化字段，看不到单上
                # 有没有税额行，也就无法把差额拆成"税 + 运费"。硬拆只能靠猜，
                # 猜错比不说更坏。所以这里只报事实与区间，把判断留给看过原图的人。
                out.append({
                    "level": "warn", "layer": "L3", "field": "total_amount",
                    "msg": f"明细之和 {row_sum:.2f} 与合计 {total:.2f} 相差 {diff:+.2f}"
                           f"（占明细 {diff / row_sum * 100:+.1f}%），落在常见税额 / 运费 / "
                           f"折扣区间，判断两者不是同一口径（合计很可能含税或含运费）。"
                           f"已放行，但**系统未抽税额字段，无法替你核对差额来源**——"
                           f"入库前请确认税额是否已单列，避免重复计税或漏记。",
                    "fixes": [],
                })
            # 差额过大时不给"改合计"的方向。
            # 真实成因：这张单可能根本不是"明细求和=合计"的口径——对账单的合计是
            # 期末余额（含期初与已收），发票的合计含税额，装箱单压根没有金额。
            # 此时给一个具体目标值（把 205950 改成 54950）是**会造成数据损毁的建议**，
            # 比不给建议坏得多。系统没有依据时就不该假装有依据。
            elif abs(diff) > max(0.5 * abs(total), 1000.0):
                out.append({
                    "level": "err", "layer": "L3", "field": "total_amount",
                    "msg": f"明细金额之和 {row_sum:.2f} 与合计 {total:.2f} 相差 {diff:.2f}，"
                           f"差距过大，两者很可能不是同一套勾稽口径"
                           f"（例如对账单的「合计」是期末余额、发票的「合计」含税额）。"
                           f"请先确认单据类型与「合计」对应的口径，不要直接改数。",
                    "fixes": [],
                })
            else:
                out.append({"level": "err", "layer": "L3", "field": "total_amount",
                            "msg": f"明细金额之和 {row_sum:.2f} 与合计 {total:.2f} 不一致，"
                                   f"差额 {diff:.2f}",
                            "fixes": [_fix(round(row_sum, 2), "recompute",
                                           "按明细各行金额求和；仅当明细全部正确时才应改合计——"
                                           "也可能是漏了某一行明细")]})

        # ---- L3 重复行冲突：同一个物料出现两行，单价却不一样
        # 这是"每条都自洽、合起来矛盾"的典型：两行各自的 qty×price=amount 都成立，
        # 合计也对得上，前三层一个错都挑不出来。只有把两行并排比较才看得见。
        # 现实成因有三种，系统无法区分：真拆成两批采购（单价可不同）、
        # OCR 把其中一行的单价读错、或表格里同一物料被重复录入。
        out.extend(_duplicate_rows(items))
    # 兼容旧调用方：`fix` 始终等于第一条候选值。它只用于一眼可见的显示，
    # 真正要动手改，必须走 fixes（那里有 how / why，人才能判断该不该采纳）。
    for iss in out:
        if iss.get("fixes") and "fix" not in iss:
            iss["fix"] = iss["fixes"][0]["value"]
    return out


def _item_key(it: dict) -> str:
    """把物料名归一到"同一个东西"：去空白标点、转小写，并把规格括注算进去。

    刻意不用编辑距离：明细里「纸箱(五层)」和「纸箱(三层)」是两个物料，
    差两个字符，用距离合并会把它们并成一行——那是改错数据，不是发现疑点。
    """
    name = it.get("name")
    if not isinstance(name, str):
        return ""
    return _norm_name(name).lower()


def _duplicate_rows(items: list) -> list[dict]:
    out: list[dict] = []
    seen: dict[str, tuple[int, float]] = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        key = _item_key(it)
        price = _num(it.get("price"))
        if not key or price is None:
            continue
        if key not in seen:
            seen[key] = (i, price)
            continue
        j, pj = seen[key]
        if abs(price - pj) <= 1e-6:
            continue
        name = str(it.get("name") or "")[:30]
        lo, hi = (price, pj) if price < pj else (pj, price)
        # 三条方向都给：哪个对系统无从判断（可能是分批采购，也可能读错一行），
        # 所以把"统一到低价""统一到高价""合并成一行"并列，由人挑。
        out.append({
            "level": "warn", "layer": "L3", "field": f"items[{i}].price",
            "msg": f"第 {i + 1} 行与第 {j + 1} 行是同一物料「{name}」，"
                   f"单价却不同（{_jsnum(price)} vs {_jsnum(pj)}）",
            "fixes": [
                _fix(pj, "rule", f"统一为第 {j + 1} 行的单价 {_jsnum(pj)}；"
                                 f"若两次是分批采购、价格本就不同，则不应统一"),
                _fix(price, "rule", f"统一为第 {i + 1} 行的单价 {_jsnum(price)}；"
                                    f"这一行在后，可能是更正后的价"),
                {"value": None, "how": "merge", "why": f"两行可能是同一物料被录了两遍："
                                                       f"合并为一行，数量与金额相加、单价按金额÷数量重算"
                                                       f"（会删掉第 {i + 1} 行）"},
            ],
        })
    return out


def is_ok(issues: list[dict]) -> bool:
    """通过 = 没有任何 err 级问题。warn 不阻断（例如币种非人民币只需折算）。"""
    return not any(i["level"] == "err" for i in issues)


# ---------------------------------------------------------------------------
# 疑点判定：决定"这张单要不要多花一次看图复核的钱"
#
# 判据全部来自实测过的错误类型，不靠感觉。特别注意**不能**用"模型 warnings 非空"
# 作判据——评测集里正常单据的 warnings 平均有 3~4 条（模型会主动说明"未发现冲突"
# "不含指令"），拿它当触发器等于每张图都复核，成本直接翻倍。
# ---------------------------------------------------------------------------
_SUSPECT_WORDS = ("冲突", "疑似", "无法确定", "不确定", "看不清", "模糊", "两种", "重复")
# 模型的 warnings 里大量"未发现冲突""不存在重复"这类**否定**表述，
# 逐字匹配风险词会把它们当成疑点。先剔除否定句再判断。
_NEGATIONS = ("未发现", "未出现", "未检出", "无冲突", "不存在", "没有发现", "没有冲突")

_KEY_FIELDS = (("doc_no", "单据编号"), ("supplier", "供应商"), ("total_amount", "合计金额"))


def _norm_name(s: str) -> str:
    return re.sub(r"[\s（）()【】\[\]·.,，、\-_/]", "", s or "")


def edit_distance(a: str, b: str) -> int:
    """Levenshtein。字符串短（公司名 20 字以内），直接 DP，不需要优化。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def check_supplier(name, history: list[str]) -> tuple[str, int] | None:
    """与库内历史供应商比对。返回 (最相近的供应商, 编辑距离) 或 None。

    判定门槛：去掉空白标点后，长度差 ≤ 2 且编辑距离 1~2。
    - 完全相同的名字不触发（那是匹配上了）。
    - 距离 ≥ 3 视为"另一家公司"，不打扰人工——这条规则宁可漏，不可频繁误报，
      因为每报一次都要花一次看图复核。
    """
    n = _norm_name(name if isinstance(name, str) else "")
    if not n or not history:
        return None
    best: tuple[str, int] | None = None
    for h in history:
        hn = _norm_name(h)
        if not hn or hn == n or abs(len(hn) - len(n)) > 2:
            continue
        dist = edit_distance(n, hn)
        if 1 <= dist <= 2 and (best is None or dist < best[1]):
            best = (h, dist)
    return best


def review_targets(
    data: dict,
    issues: list[dict],
    content_risk: dict | None,
    supplier_history: list[str] | None = None,
) -> tuple[bool, list[dict], list[str]]:
    """决定是否需要看图复核，并给出待复核字段。

    返回 (needs_review, targets, triggers)。
    targets 每项 {field, label, value, reason, rule_fix, rule_why}；
    triggers 是给用户看的触发原因。

    rule_fix 是这条字段在规则层已经算出的建议值。它随 target 一起交给看图复核，
    作用不是"替模型做决定"，而是给模型一个**独立的第二判据**：当看图读到的值
    恰好等于规则建议，两个互不依赖的来源相互印证，这条建议才敢交给用户采纳
    （见 review.sanitize_findings 的仲裁规则）。
    """
    targets: dict[str, dict] = {}
    triggers: list[str] = []

    def add(field: str, reason: str) -> None:
        if field in targets:
            return
        labels = dict(_KEY_FIELDS)
        label = labels.get(field)
        if label is None:
            if field.startswith("items["):
                label = "明细 " + field[6:-1] + " 的金额" if field.endswith(".amount") else "明细 " + field[6:-1]
            else:
                label = field
        value = data.get(field, None) if not field.startswith("items[") else _item_value(data, field)
        rule_fix = rule_why = None
        for iss in issues:
            if iss.get("field") == field and iss.get("fixes"):
                rule_fix = iss["fixes"][0]["value"]
                rule_why = iss["fixes"][0]["why"]
                break
        targets[field] = {"field": field, "label": label, "value": value, "reason": reason,
                          "rule_fix": rule_fix, "rule_why": rule_why}

    # ① 勾稽不通过：最硬的疑点，说明三处数字至少一处错
    for iss in issues:
        if iss["layer"] == "L3":
            add(iss["field"], iss["msg"])
            triggers.append(iss["msg"])

    # ② 单据编号与历史格式不符（形近字符、写法异常）
    #    原来只认"P0/O 混淆"这一种，等于把某一家客户的编号规则写死在触发条件里。
    #    现在只要有编号类疑点就去核一次图——看原图是唯一能在字符层面下结论的手段。
    #    ⚠ 契约：level=="info" 的编号事件（L2 已按画像自动归一）**不得**在这里
    #    被当成疑点——它已经处理完了，再问一遍就是重复劳动。这里只认 warn/err。
    for iss in issues:
        if iss["field"] == "doc_no" and iss["level"] in ("warn", "err"):
            add("doc_no", iss["msg"])
            triggers.append(iss["msg"])

    # ③ 币种非人民币
    #    两个排除条件，都是为了不花冤枉钱：
    #    · `currency_inferred`（单据上根本没有金额，币种是模型推断的）；
    #    · 整张单没有任何价格/金额——币种告警的意义是"入库前要按汇率折算"，
    #      没有金额就没有东西要折算，看图也看不到一个不存在的币种标识。
    _rows = data.get("items") if isinstance(data.get("items"), list) else []
    _has_money = any(
        (it.get("price") is not None or it.get("amount") is not None)
        for it in _rows if isinstance(it, dict))
    for iss in issues:
        if (iss["field"] == "currency" and iss["level"] == "warn" and _has_money
                and iss.get("code") != "currency_inferred"):
            add("currency", iss["msg"])
            triggers.append(iss["msg"])

    # ④ 关键字段缺失
    for field, label in (("doc_no", "单据编号"), ("supplier", "供应商"), ("order_date", "下单日期")):
        if not data.get(field):
            add(field, f"{label}未抽到")
            triggers.append(f"{label}未抽到")

    # ⑤ 内容风险层命中（影子字段 / 数字字面量异常）——这两类勾稽查不出来
    cr = content_risk or {}
    for hit in (cr.get("shadow") or []):
        if hit.get("level") == "high":
            add("total_amount", hit.get("desc") or "存在多个总额")
            triggers.append(hit.get("desc") or "存在多个总额")
    for hit in (cr.get("numeric") or []):
        if hit.get("level") == "high":
            add("total_amount", hit.get("desc") or "数字字面量异常")
            triggers.append(hit.get("desc") or "数字字面量异常")

    # ⑥ 供应商与历史记录仅差 1~2 个字符：OCR 形近字错误的典型形态，
    #    也是三层校验完全覆盖不到的自由文本盲区（评测里唯一一张"误放行"就是它）
    hit = check_supplier(data.get("supplier"), supplier_history or [])
    if hit:
        near, dist = hit
        msg = f"供应商与历史记录「{near}」仅差 {dist} 个字符，疑似识别错误"
        add("supplier", msg)
        triggers.append(msg)

    # ⑦ 模型自报的不确定（仅认含风险词且不是否定句的条目）
    #    模型说"某某无法确定"时，说明连它自己都没把握——这种也要看图核一次，
    #    否则"模型自己承认不确定"反而成了被忽略的信号。
    for w in (data.get("warnings") or []):
        if not isinstance(w, str):
            continue
        if any(n in w for n in _NEGATIONS):
            continue
        if not any(k in w for k in _SUSPECT_WORDS):
            continue
        field = _field_from_warning(w)
        if field:
            add(field, "模型自报不确定：" + w[:80])
        triggers.append(w[:120])

    # ⑧ 出处层（L4）命中：值在原文里找不到，或与单据「供应商：」那一行矛盾
    #
    #    只把 supplier 送进复核，不让 L4 的明细行名也触发一次看图：
    #    复核 prompt 是按关键字段设计的，处理任意 items[i].name 既没测过、
    #    也容易把一次调用摊在几十个物料名上。明细名的告警仍然展示给人看。
    for iss in issues:
        if iss.get("layer") == "L4" and iss.get("field") == "supplier":
            add("supplier", iss["msg"])
            triggers.append(iss["msg"])

    if not targets:
        return False, [], []

    # 一次看图调用的成本与字段数无关，所以顺带把关键字段一起核——
    # 否则"只核了供应商、编号其实也错了"这种漏法会在演示时很难看。
    for field, label in _KEY_FIELDS:
        if field in targets:
            continue
        if data.get(field):
            add(field, "随本次复核一并确认")
    return True, list(targets.values()), triggers[:6]


def _field_from_warning(w: str) -> str | None:
    """把模型的自由文本警告落到具体字段上。认不出就返回 None——不要瞎猜，
    猜错会把一次看图复核花在无关字段上。"""
    if any(k in w for k in ("编号", "单号", "订单号")):
        return "doc_no"
    if any(k in w for k in ("供应商", "供方", "客户名称", "单位名称")):
        return "supplier"
    if any(k in w for k in ("日期", "年月日", "下单时间")):
        return "order_date"
    if any(k in w for k in ("合计", "总额", "总价")):
        return "total_amount"
    return None


def _item_value(data: dict, field: str):
    m = re.fullmatch(r"items\[(\d+)\](?:\.(\w+))?", field)
    if not m:
        return None
    items = data.get("items")
    if not isinstance(items, list):
        return None
    idx = int(m.group(1))
    if idx >= len(items) or not isinstance(items[idx], dict):
        return None
    sub = m.group(2)
    return items[idx].get(sub) if sub else items[idx]
