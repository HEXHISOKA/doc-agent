"""看图复核：把「原图 + OCR 文本 + 已抽取字段」一起给模型，只核对被点名的疑点。

为什么需要这一层
----------------
三层校验只能检查"字段内部"和"字段之间"，检查不了"字段与照片是否一致"。
评测集里唯一一张误放行的单据（po_13_plain_B_d3）就是这类：OCR 把「达」读成「送」、
丢了「品」，模型忠实转写，四层防线全部放过——因为公司名是自由文本，没有格式规则、
不参与勾稽、字面量本身也完全正常。

实测（2026-10-25）：同一张图，OCR 读成「宁波恒送塑胶制有限公司」，
模型看图读出「宁波恒达塑胶制品有限公司」（= 真值）。所以复核能补上这个盲区。

三条刻意的设计取舍
------------------
1. **只在有疑点时发图**。正常单据零额外成本：不触发就不调用模型。
2. **看图只做复核，不做改写**。模型返回的 `image_value` 只是"证据"，
   改不改数据由人点按钮决定。模型永远不直接改库。
3. **数字类字段不仲裁**。模型与 OCR 不一致时只标记，不采信任何一方——
   实测 OCR 认数字比看图准（编号 100% vs 30%），让模型裁决会引入新错误。
"""
from __future__ import annotations

import base64
import io
import json
import re

from PIL import Image

VERDICTS = {"match", "mismatch", "unreadable"}
DIRTY = {"none", "seal", "blur", "duplicate", "occlusion", "handwriting", "layout", "other"}

DIRTY_LABEL = {
    "none": "未发现异常",
    "seal": "印章覆盖",
    "blur": "模糊/分辨率不足",
    "duplicate": "多行重复",
    "occlusion": "被遮挡",
    "handwriting": "手写批注",
    "layout": "版面错位",
    "other": "其他",
}

REVIEW_SYSTEM = """你是采购订单的单据复核员。任务是把单据照片与 OCR 文本、已抽取字段做交叉核对，只回答被点名要求复核的字段。

规则：
1. 只输出一个 JSON 对象，不要输出解释、不要 markdown 代码块、不要任何前后缀。
2. 输出格式：
{"findings":[{"field":"字段名","ocr_value":"当前值","image_value":"照片上读到的值","verdict":"match|mismatch|unreadable","dirty_cause":"none|seal|blur|duplicate|occlusion|handwriting|layout","reason":"一句话说明"}]}
3. 只复核「待复核字段」里列出的 field，不要新增字段，field 必须原样照抄。
4. image_value 以照片为准逐字抄写：不要补全公司名、不要按常见金额推测、不要凑成整数。看不清就填 null，verdict 填 unreadable。
5. 数字类字段（金额、数量、单价）若与 OCR 不一致：verdict 填 mismatch、image_value 如实给出，但不要判断该采信哪一方——是否采信由系统规则决定。
6. dirty_cause 说明该字段为什么可能被读错：seal=印章覆盖、blur=模糊或分辨率不足、duplicate=多行内容重复、occlusion=被其他内容遮挡、handwriting=手写批注干扰、layout=版面错位导致串行、none=未发现异常。
7. 看不清就说看不清。禁止编造，禁止猜测。"""


def encode_for_vision(blob: bytes, max_side: int = 1800, quality: int = 88) -> str:
    """把原图压成适合模型看的 JPEG（data URL 的 base64 部分）。

    为什么要压：手机原图动辄 5MB，直接送进模型 token 会爆；长边 1800px 对
    辨认「达/送」这种同号字绰绰有余（实测已确认），同时把 token 控制在 1k 左右。
    """
    im = Image.open(io.BytesIO(blob))
    im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def build_user_message(ocr_text: str, extract: dict, targets: list[dict]) -> str:
    brief = [{"field": t["field"], "label": t.get("label"), "value": t.get("value"),
              "why": t.get("reason")} for t in targets]
    return (
        "待复核字段：\n" + json.dumps(brief, ensure_ascii=False, indent=1) + "\n\n"
        "OCR 全文（可能含识别错误，仅供参考，不是事实）：\n<DOC>\n"
        + (ocr_text or "")[:6000]
        + "\n</DOC>\n\n"
        "已抽取结果（JSON，可能有错）：\n"
        + json.dumps(extract, ensure_ascii=False)[:6000]
    )


def run_review(
    client,
    image_b64: str,
    ocr_text: str,
    extract: dict,
    targets: list[dict],
    model: str,
    timeout: float = 150.0,
) -> tuple[list[dict], dict]:
    """调用模型复核，返回 (清洗后的 findings, usage)。"""
    if not targets:
        return [], {}
    messages = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": build_user_message(ocr_text, extract, targets)},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image_b64}},
            ],
        },
    ]
    text, usage = client.chat(
        messages, model=model, temperature=0.0, timeout=timeout, json_mode=True
    )
    try:
        return sanitize_findings(text, targets), usage
    except ValueError as exc:
        # 流式返回偶尔会在中途断掉（实测：JSON 刚写到 findings 第二项就结束），
        # 这是**传输问题**，不是"模型不肯复核"。重试一次能救回绝大多数；
        # 还是不行就把异常抛上去，让调用方按"复核未完成"拒收——绝不静默当成通过。
        if "不是合法 JSON" not in str(exc):
            raise
        text2, usage2 = client.chat(
            messages, model=model, temperature=0.0, timeout=timeout, json_mode=True
        )
        return sanitize_findings(text2, targets), (usage2 or usage)


def sanitize_findings(text: str, targets: list[dict]) -> list[dict]:
    """把模型输出收敛成可信结构：字段白名单、枚举钳制、仲裁规则。

    模型输出不可信的部分一律丢弃而不是"修一修猜一猜"——宁可少一条 finding，
    也不能让客户端拿到一个模型自己编出来的字段。

    仲裁规则（本节最重要，来自实测事故）
    ------------------------------------
    复核结果里 model 给出的 `image_value` 只是一个"建议"，点不点由人定。但建议本身
    也可能是错的：实测中模型把单据编号 `PO2026082113` 看成 `P02026082113`（字母 O
    读成数字 0），如果直接把它做成"按图上改"，就会把本来正确的编号改错。

    所以三条硬规则：
      1. 数字字段数值相等（`550,290.00` vs `550290`）不算不一致——只是写法不同。
      2. 两个字符串只差形近字符（O/0、I/1、S/5、B/8、Z/2）时，默认不给修改建议。
         字符级的 O/0 判定连人类都要靠上下文，模型不比你更权威。
      3. 例外：如果规则层（validate.py）也独立算出了同一个值，两个来源一致，
         这时才放行。看图和规则是两条互不依赖的推理路径，它们同时指向 `PO2026082113`
         时，把 `P0…` 改成 `PO…` 就不再是"听模型的"，而是两个判据的交叉验证。
         这是唯一允许改动形近字符的条件，别的地方一律不动。
    """
    obj = _loads(text)
    if not obj:
        raise ValueError("复核结果不是合法 JSON：" + (text or "")[:160])

    allowed = {t["field"]: t for t in targets}
    out: list[dict] = []
    seen: set[str] = set()
    for f in obj.get("findings") or []:
        if not isinstance(f, dict):
            continue
        field = f.get("field")
        if field not in allowed or field in seen:
            continue
        seen.add(field)
        verdict = str(f.get("verdict") or "").lower()
        if verdict not in VERDICTS:
            verdict = "unreadable"
        cause = str(f.get("dirty_cause") or "none").lower()
        if cause not in DIRTY:
            cause = "other"
        image_value = f.get("image_value")
        if image_value is not None and not isinstance(image_value, (str, int, float)):
            image_value = None
        if isinstance(image_value, (int, float)):
            image_value = str(image_value)
        if image_value is not None:
            image_value = str(image_value).strip() or None
        if verdict == "unreadable":
            image_value = None

        ocr_value = _as_text(f.get("ocr_value") if f.get("ocr_value") is not None
                             else allowed[field].get("value"))
        reason = str(f.get("reason") or "")[:200]

        apply_value = image_value if (verdict == "mismatch" and image_value) else None
        if apply_value:
            tgt = allowed[field]
            rule_fix = tgt.get("rule_fix")
            if _is_numeric_field(field) and _num_equal(ocr_value, image_value):
                # 写法差异（千分位/小数位）不是错误，直接判一致，避免给出无意义的修改建议
                verdict, apply_value = "match", None
            elif _norm_ident(ocr_value) == _norm_ident(image_value):
                # 注意：这里必须用**逐字符**比较，不能用 _norm_ident。_norm_ident 把
                # O 和 0 映射成同一个字符，`P0…` 与 `PO…` 归一后完全相同，无法区分
                # "图上读对了"和"图上跟着 OCR 读错了"。互证要求字符级一致。
                if (rule_fix is not None
                        and _same_text(image_value, rule_fix)
                        and not _same_text(ocr_value, rule_fix)):
                    # 双源互证：图上读到的值等于规则建议，而当前值不等于。
                    verdict = "mismatch"
                    reason = (reason + "（与业务规则推算出的值一致，看图与规则两条独立判据"
                                       "相互印证，可以采纳）")[:240]
                else:
                    apply_value = None
                    reason = (reason + "（仅 O/0、I/1 这类形近字符差异，图上与当前值无法区分，"
                                        "不提供修改建议）")[:240]

        out.append({
            "field": field,
            "label": allowed[field].get("label") or field,
            "ocr_value": ocr_value,
            "image_value": image_value,
            "verdict": verdict,
            "dirty_cause": cause,
            "dirty_label": DIRTY_LABEL.get(cause, "其他"),
            "reason": reason,
            # 只有 mismatch 且图上确实读出了值，才建议采纳
            "apply_value": apply_value,
        })
    return out


_HOMOGLYPH = str.maketrans({
    "O": "0", "o": "0", "I": "1", "i": "1", "l": "1", "L": "1",
    "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2",
})


def _norm_ident(s) -> str:
    """把形近字符统一后再比：相同说明只是"O 还是 0"这类不可判差异。"""
    if s is None:
        return ""
    t = re.sub(r"[\s\-_/（）()]", "", str(s))
    return t.translate(_HOMOGLYPH).lower()


def _same_text(a, b) -> bool:
    """逐字符比较（只忽略空白与包裹性标点）。用于双源互证——归一化会把 O/0 抹平，
    恰好抹掉的就是我们要判的那一位。"""
    if a is None or b is None:
        return False
    strip = lambda s: re.sub(r"[\s\-_/（）()]", "", str(s))
    return strip(a) == strip(b)


def _num_equal(a, b) -> bool:
    x, y = _to_num(a), _to_num(b)
    return x is not None and y is not None and abs(x - y) < 0.005


def _to_num(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None


def _is_numeric_field(field: str) -> bool:
    return field == "total_amount" or bool(re.fullmatch(r"items\[\d+\]\.(qty|price|amount)", field))


def _as_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return str(v)
    return str(v)[:200]


def _loads(text: str):
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?", "", s).strip()
    s = re.sub(r"```$", "", s).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 模型偶尔会在 JSON 前后带一句话，取第一个 { 到最后一个 } 再试一次
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                return None
        return None
