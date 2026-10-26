"""单据解析服务 —— OCR / 复核 / 校验 / 入库。

责任分工（这一点在面试里比代码本身更重要）：
    OCR 留在后端：确定性、可复现（同一张图连跑三次结果逐字节相同）、
        可存档、可做回归测试。这是把它放后端的理由，不是"省 token"。
    模型抽取在后端（2026-10-25 从浏览器搬来）：搬家的理由是**可归因**，不是性能。
        抽取在浏览器时，服务端只收到一个 JSON，分不清它是"模型读图得到的"还是
        "人手工写的"——一份算术自洽的伪造数据同样会被判为 validate_ok=true 并入库。
        搬到后端后，extract 只能由服务端自己调模型产生，来源可证。顺带两件事：
        ① 提示词不再发到浏览器；② 模型 id 由服务端固定（前端可选 33 个模型，
        实测其中 2 个会被图上文字劫持出错误的供应商名）。
    校验与入库在后端：这一层不能交给浏览器——否则 `validate_ok` 就只是客户端
        提交的一个布尔值，前端随手就能改成 true。现在由服务端复算校验、服务端
        写库，前端那份只用于显示。
    看图复核在后端：三层校验查不了"字段与照片是否一致"（供应商名读错就是这种情况，
        字形近、格式合法、不参与勾稽）。复核提示词与仲裁规则留在服务端，客户端拿不到。

数据流（2026-10-26 抽取迁移为图片直读后）：
    正常单据  图片直读抽取 → 服务端校验（出处层以 OCR 文本为参照，零 token）
              → 入库（不调用模型复核，零额外成本）
    有疑点    图片直读抽取 → 校验发现疑点 → 原图+OCR 文本发给模型复核
              → 页面标黄"图上似为 X，请确认" → 人点一下 → 入库

路由：
    GET  /             单页界面（从 web/index.html 读取）
    GET  /api/config   下发云服务 publicConfig（前端据此初始化 SDK）
    GET  /api/health   健康检查
    GET  /api/eval     评测摘要（读 eval/report.json，页面不手抄数字）
    POST /api/ocr      接收图片，返回识别文本 + 逐词坐标 + 版面重建结果 + doc_id
    POST /api/extract  按 doc_id 取服务端缓存的原图，由服务端调视觉模型直读抽取。
                       只收 doc_id —— 抽取来源可证。OCR 文本不再进抽取 prompt，
                       只作为出处层参照与复核证据（本地 RapidOCR，零 token）。
                       模型由 EXTRACT_MODEL 固定。
    POST /api/submit   commit=false 只校验并返回疑点；commit=true 校验后写库
    POST /api/review   对指定 doc_id 的原图做看图复核，返回逐字段核对结果
    POST /api/apply    人工选定方向后，由模型把方向执行成一组具体字段改动（patch），
                       再经服务端闸门核对；只返回结果，不写库（写库仍走 /api/submit）
    POST /api/correct  对已入库记录追加更正（红字冲销：原记录不动，更正进独立哈希链；
                       支持批量 changes，表格模式一次提交多个格）
    GET  /api/corrections?corrects_id=N   某条记录的全部更正记录
    GET  /api/profile  编号格式档案现状（来源 / 样例 / 画像结构）
    POST /api/profile/examples  实施方直接设定标准编号样例（工厂级，最高优先级证据）
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.server
import json
import os
import re
import sys
import time
import traceback
import uuid
from collections import OrderedDict
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR / "src"))

from apply import apply_patches, explain as explain_patches, run_apply, verify_patches  # noqa: E402
from cloud import CloudClient, CloudError  # noqa: E402
from contentrisk import detect as detect_content_risk  # noqa: E402
from grounding import check as check_grounding, summary as grounding_summary  # noqa: E402
from guard import detect  # noqa: E402
from layout import rebuild, to_markdown_table  # noqa: E402
from ocr import backend_name, run_ocr_array  # noqa: E402
from preprocess import decode_image, suppress_red  # noqa: E402
from prompt import build_messages_direct  # noqa: E402
from review import encode_for_vision, run_review  # noqa: E402
import patterns  # noqa: E402
from validate import is_ok, review_targets, validate as validate_extract  # noqa: E402

MAX_BYTES = 12 * 1024 * 1024

# ---------------------------------------------------------------------------
# 云服务公开配置
#
# 这四个值来自 workbuddy_cloud_service 的 activate 返回（publicConfig），是唯一
# 允许下发到前端的凭据。publishableKey 只表明"这是哪个应用"，本身不带权限，
# 服务端仍强制 Origin 精确匹配，所以它可以出现在前端代码里（它最终一定会出现在
# 浏览器里），但不应写进日志。
#
# endpoint 之所以在服务端持有而不是写死在前端：前端一旦把域名写死，应用换域名
# 或换环境就必然静默失效；由服务端统一提供，改一处即可。
# 注意：应用发布域名变更后，这里的 endpoint 必须同步更新，否则前端初始化会
# 因 Origin 不匹配而失败（表现为模型调用报 auth_ 前缀的错误）。
# ---------------------------------------------------------------------------
PUBLIC_CONFIG = {
    "resourceId": os.environ.get("WBCS_RESOURCE_ID", ""),
    "endpoint": os.environ.get("WBCS_ENDPOINT", ""),
    "oauthRelayBaseUrl": os.environ.get(
        "WBCS_OAUTH_RELAY", "https://www.workbuddy.cn/v2/as/genie-baas/oauth"
    ),
    "publishableKey": os.environ.get("WBCS_PUBLISHABLE_KEY", ""),
}


def _require_public_config():
    """启动即校验：缺配置直接退出，而不是等第一次调用模型时才报 auth_ 错误。"""
    missing = [k for k in ("endpoint", "publishableKey") if not PUBLIC_CONFIG[k]]
    if missing:
        raise SystemExit(
            "缺少云服务配置：%s。部署前请设置环境变量 WBCS_ENDPOINT / "
            "WBCS_PUBLISHABLE_KEY（本仓库不存放实例凭据）。" % ", ".join(missing)
        )

WEB_DIR = BASE_DIR / "web"
FALLBACK_PAGE = (
    "<!doctype html><meta charset='utf-8'>"
    "<h1>页面文件缺失</h1><p>web/index.html 未随项目一同部署。</p>"
)

# ---------------------------------------------------------------------------
# 服务端持有的云通道客户端
#
# 为什么服务端也需要它：校验要由服务端复算、入库要由服务端执行、复核要由服务端
# 发图，这三件事都必须有一个"客户端改不了"的执行者。publishableKey 本身是公开
# 标识（它一定会出现在浏览器里），真正的约束在平台侧（Origin 白名单与配额）。
# ---------------------------------------------------------------------------
_require_public_config()
CLOUD = CloudClient(PUBLIC_CONFIG["endpoint"], PUBLIC_CONFIG["publishableKey"])
# 抽取模型由服务端固定，不接受客户端指定。
# 前端曾把 33 个模型列成下拉框随便选 —— 实测 glm-5v-turbo / hunyuan-chat 会被
# 图上文字劫持，把供应商改成清单外的公司名。抽取这种"给什么模型、问什么问题"
# 的决定权一旦交给客户端，再用什么规则都补不回来。
EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "deepseek-v4.1-flash")
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "deepseek-v4.1-flash")
TABLE = "doc_parses"

# 上传的原图只在内存里短暂保留，够复核那一次调用用即可。
# 为什么不落磁盘：单据可能含真实客户信息，进程退出就应当什么都不剩。
#
# 为什么要按会话隔离（原先是全局一个 FIFO，DOC_MAX=6）：
#   所有人共用一个队列 ⇒ 第 7 个人上传，第 1 个人的图被挤掉，他点「看图复核」
#   得到「原图已失效」。单用户演示发现不了，两个人同时用就出事。
#   现在每会话独立 FIFO + 全局上限兜底，一个人连传 20 张也挤不掉别人的。
#
# 为什么还要一个 doc_id → 会话 的全局索引：
#   cookie 在 iframe 里可能被浏览器按第三方 cookie 拦掉（平台若用 iframe 嵌页面
#   就会这样）。那种情况下 cookie 拿不到、会话每次都是新的，doc_id 就永远找不回。
#   所以查找顺序是「本会话 → 全局索引」：隔离用于淘汰、不用于鉴权。
#   doc_id 是 128 bit 随机值，知道它本身就是能力，跨会话查到不等于越权。
DOC_TTL = 15 * 60
DOC_MAX = 8            # 每个会话同时保留几张
DOC_MAX_TOTAL = 64     # 全局上限，防止无人清理时内存无限增长
SID_COOKIE = "wb_sid"
_SESS: "OrderedDict[str, OrderedDict[str, dict]]" = OrderedDict()
_ALL: "OrderedDict[str, str]" = OrderedDict()   # doc_id -> sid，按插入顺序


def _doc_at(doc_id: str) -> float:
    sid = _ALL.get(doc_id)
    if not sid:
        return 0.0
    return (_SESS.get(sid, {}).get(doc_id) or {}).get("at") or 0.0


def _drop_doc(doc_id: str) -> None:
    sid = _ALL.pop(doc_id, None)
    if sid and sid in _SESS:
        _SESS[sid].pop(doc_id, None)
        if not _SESS[sid]:
            _SESS.pop(sid, None)

# 历史供应商名用于"疑似形近字错误"比对，60 秒缓存足够，避免每张单都打数据库
_SUPPLIERS: dict = {"at": 0.0, "names": []}
_SUPPLIER_TTL = 60.0


def _model_json(raw: str) -> dict | None:
    """把模型返回收成一个 dict。json_mode 不保证不裹 ```，仍要容错。"""
    s = (raw or "").strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()
    for cand in (s, s[s.find("{"):s.rfind("}") + 1] if "{" in s and "}" in s else ""):
        if not cand:
            continue
        try:
            v = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict):
            return v
    return None


def load_page() -> bytes:
    """每次请求现读，改前端不必重启服务。"""
    p = WEB_DIR / "index.html"
    if p.exists():
        return p.read_bytes()
    return FALLBACK_PAGE.encode("utf-8")


def _load_eval_summary() -> dict:
    """把评测摘要交给前端，避免在页面里手抄评测数字。

    2026-10-26 起优先读 `report_live.json`：那是打线上真实链路（/api/ocr →
    /api/extract → /api/submit → 存疑 /api/review）跑出来的，与页面正在服务的
    这一份代码是同一个版本。`report.json` 是旧脚本在本地跑的结果，其中的 A 列
    还是"纯 OCR 文本抽取"——那个方案已经不存在了，留着只作历史对照，
    不能再拿它当页面的评测结论。
    """
    p = None
    for name in ("report_live.json", "report.json"):
        cand = BASE_DIR / "eval" / name
        if cand.exists():
            p = cand
            break
    if p is None:
        return {"available": False, "error": "评测报告未随项目部署（eval/report_live.json 缺失）"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {
        "available": True,
        "model": d.get("model"),
        "n": d.get("n"),
        "summary": d.get("summary") or {},
        "fallback": d.get("fallback_stats") or {},
        "mode": d.get("mode") or "offline",
        "note": d.get("note") or "",
        "generated_at": d.get("generated_at")
        or time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime)),
    }
    # 难例集（退化字体/低质扫描）的报告一并下发：页面上"正常字体"与"退化字体"
    # 两个场景的对比都必须来自实测文件，不能在 HTML 里手抄数字。
    hard = BASE_DIR / "eval" / "report_live_simsun_abc.json"
    if hard.exists():
        try:
            hd = json.loads(hard.read_text(encoding="utf-8"))
            out["hard"] = {
                "label": "退化字体 / 低质扫描（编号宋体 20px，O 与 0 同字形）",
                "n": hd.get("n"),
                "summary": hd.get("summary") or {},
                "fallback": hd.get("fallback_stats") or {},
                "generated_at": hd.get("generated_at"),
            }
        except (OSError, json.JSONDecodeError):
            pass  # 难例集报告坏了不影响主报告的展示
    return out


def cache_doc(blob: bytes, filename: str, text: str, content_risk: dict, meta: dict,
              sid: str = "") -> str:
    """把原图（压成适合模型看的 JPEG）与 OCR 结果暂存在内存，返回 doc_id。

    复核发生在抽取之后，那时请求已经结束；用 doc_id 找回原图，避免让浏览器把
    同一张图再传一遍（也避免图片在客户端与服务端之间来回搬）。
    """
    now = time.time()
    for k in [k for k in list(_ALL) if now - _doc_at(k) > DOC_TTL]:
        _drop_doc(k)
    try:
        image_b64 = encode_for_vision(blob)
    except Exception:  # noqa: BLE001 - 压图失败不该让整条链路失败，退回不带图
        image_b64 = ""
    doc_id = uuid.uuid4().hex
    _SESS.setdefault(sid, OrderedDict())[doc_id] = {
        "at": now,
        "sid": sid,
        "filename": filename,
        "image_b64": image_b64,
        "text": text,
        "content_risk": content_risk or {},
        "words": meta.get("words"),
        "ocr_ms": meta.get("ocr_ms"),
        "skew_deg": meta.get("skew_deg"),
    }
    _ALL[doc_id] = sid
    # 先淘汰本会话最老的，再按全局上限淘汰全局最老的
    while len(_SESS[sid]) > DOC_MAX:
        _drop_doc(next(iter(_SESS[sid])))
    while len(_ALL) > DOC_MAX_TOTAL:
        _drop_doc(next(iter(_ALL)))
    return doc_id


def get_doc(doc_id: str, sid: str = "") -> dict | None:
    """按 doc_id 取回落暂存的原图。

    先查本会话，查不到再查全局索引（cookie 被拦时的兜底）。
    doc_id 猜不到，所以跨会话命中不是越权，只是让 iframe 场景能正常工作。
    """
    doc_id = doc_id or ""
    bucket = _SESS.get(sid) if sid else None
    v = bucket.get(doc_id) if bucket else None
    if v is None:
        owner = _ALL.get(doc_id)
        v = _SESS.get(owner, {}).get(doc_id) if owner else None
    if not v:
        return None
    if time.time() - v["at"] > DOC_TTL:
        _drop_doc(doc_id)
        return None
    _ALL.move_to_end(doc_id)
    return v


def supplier_history() -> list[str]:
    """取库内出现过的供应商名，用于形近字比对。失败返回空表——比对是增强项，
    不能因为读不到历史就把入库整条堵死。"""
    now = time.time()
    if now - _SUPPLIERS["at"] < _SUPPLIER_TTL:
        return _SUPPLIERS["names"]
    try:
        rows = CLOUD.rest_select(TABLE, "select=supplier&supplier=not.is.null&limit=500")
        names = sorted({r.get("supplier") for r in rows if r.get("supplier")})
    except (CloudError, json.JSONDecodeError):
        names = _SUPPLIERS["names"]
    _SUPPLIERS.update({"at": now, "names": names})
    return names


_PROFILE = {"at": 0.0, "by_supplier": {}, "global": patterns.EMPTY}
_PROFILE_TTL = 600


def doc_profiles():
    """各客户的单据编号格式画像（见 patterns.py）。

    来源优先级：配置文件（环境变量 DOC_RULES）> 库内历史单据 > 空画像。
    空画像是**安全默认值**：校验层据此只说"无法判断格式"，而不会拿别家客户的
    规律去套这张单——宁可少一条提示，也不要多一条误报。
    """
    now = time.time()
    if now - _PROFILE["at"] < _PROFILE_TTL:
        return _PROFILE["by_supplier"], _PROFILE["global"]

    conf = patterns.from_config(os.environ.get("DOC_RULES"))
    if conf is not None:
        _PROFILE.update({"at": now, "by_supplier": {}, "global": conf})
        return {}, conf

    # 工厂级档案（doc_no_profiles）优先于历史推断：实施方给的样例与人工确认
    # 沉淀的写法是"显式认定"，比从历史反推的统计结论权威；且历史里可能混着
    # 还没被纠出的错读。档案读不到或建不出画像时才退回历史——兜底顺序。
    manual: list[str] = []
    for r in _profile_examples():
        vals = r.get("examples")
        if isinstance(vals, list):
            manual.extend(str(x) for x in vals if str(x).strip())
    if manual:
        prof = patterns.from_examples(manual)
        if prof is not None:
            _PROFILE.update({"at": now, "by_supplier": {}, "global": prof})
            return {}, prof

    try:
        rows = CLOUD.rest_select(TABLE, "select=doc_no,supplier&doc_no=not.is.null&limit=1000")
        by_supplier, global_profile = patterns.learn(
            [(r.get("doc_no"), r.get("supplier")) for r in rows])
    except (CloudError, json.JSONDecodeError):
        # 读不到历史不是错误：画像是增强项，不能因此把校验整条堵死
        return _PROFILE["by_supplier"], _PROFILE["global"]

    _PROFILE.update({"at": now, "by_supplier": by_supplier, "global": global_profile})
    return by_supplier, global_profile


def validate_with_profile(extract: dict) -> list[dict]:
    """按这张单的供应商挑画像再校验。画像缺失时自动退化成通用层。"""
    by_supplier, global_profile = doc_profiles()
    prof = patterns.pick(by_supplier, global_profile, (extract or {}).get("supplier"))
    return validate_extract(extract, profile=prof)


PROFILE_TABLE = "doc_no_profiles"
CORRECTIONS_TABLE = "corrections"
# 与库层 CHECK 约束同款白名单：更正只允许落在这四个业务字段上。
# item_count 是明细行的派生值（len(items)），单独更正没有意义。
CORRECTABLE_FIELDS = ("doc_no", "supplier", "order_date", "total_amount")

# 明细更正：命名与入库前表格编辑/校验层完全一致（items[0].qty，0 基）。
# 白名单三处同源：DB CHECK 约束、这里、前端渲染——谁也不比谁多。
_ITEM_FIELD_RE = re.compile(r"^items\[(\d+)\]\.(name|qty|price|amount)$")
_ITEM_NUMERIC = {"qty", "price", "amount"}
MAX_CHANGES = 16  # 单次批量上限：整单表头 4 格 + 明细若干格，够用且防滥用

# 有效版本视图：业务侧"现在值是多少"的唯一定义点。更正的 old_value 从这里读，
# 保证同字段多次更正时 old→new 链条连贯（原表读会把旧值读重）。
EFFECTIVE_VIEW = "v_doc_parses_effective"
_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def _profile_examples() -> list[dict]:
    """读工厂级编号档案（doc_no_profiles 表）。失败返回空——档案是增强项，
    不能因为读档案失败就把校验整条堵死（与 supplier_history 同一原则）。"""
    try:
        return CLOUD.rest_select(
            PROFILE_TABLE,
            "select=examples,source,confirmed_by,confirmed_at,updated_at"
            "&factory_key=eq.default&order=updated_at.desc&limit=1")
    except (CloudError, json.JSONDecodeError):
        return []


def profile_summary() -> dict:
    """档案现状（GET /api/profile）。给维护界面看：证据来源、样例清单、画像结构。
    source 的取值：config(环境变量) > profiles(档案) > history(库内推断) > empty。"""
    by_supplier, glob = doc_profiles()
    rows = _profile_examples()
    meta = rows[0] if rows else {}
    manual = [str(x) for r in rows for x in (r.get("examples") or []) if str(x).strip()]
    if os.environ.get("DOC_RULES"):
        src = "config"
    elif manual:
        src = "profiles"
    else:
        src = getattr(glob, "source", "") or "history"
    return {"ok": True, "source": src, "examples": manual,
            "confirmed_by": meta.get("confirmed_by"),
            "confirmed_at": meta.get("confirmed_at"),
            "describe": (glob.describe() if glob is not None and not glob.empty else "暂无可用画像"),
            "profile": (glob.to_dict() if glob is not None and not glob.empty else None)}


def _profile_record_confirmed(doc_no) -> None:
    """人工确认入库的编号沉淀进工厂档案——"只问一次，之后记住"的沉淀侧。

    与画像学习的关系：doc_profiles() 本来就会定期从库内历史重学；这里的价值是
    ① 把"人确认过的写法"显式留痕（source/confirmed_by/confirmed_at 可查可反证），
    ② 档案证据不依赖学习窗口和样本量阈值。重复写法不重复记。全程失败静默：
    档案是增强项，绝不能影响入库主流程。
    """
    no = "" if doc_no is None else str(doc_no).strip()
    if not no or len(no) > 60:
        return
    try:
        rows = CLOUD.rest_select(PROFILE_TABLE,
                                 "select=id,examples&factory_key=eq.default&limit=1")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if rows:
            row = rows[0]
            examples = row.get("examples") if isinstance(row.get("examples"), list) else []
            if no in [str(x) for x in examples]:
                return
            examples = ([str(x) for x in examples] + [no])[-50:]  # 上限 50 条防膨胀
            CLOUD.rest_update(PROFILE_TABLE,
                              {"examples": examples, "updated_at": now_iso},
                              query=f"id=eq.{row.get('id')}")
        else:
            CLOUD.rest_insert(PROFILE_TABLE, {
                "factory_key": "default", "examples": [no],
                "source": "confirmed", "confirmed_by": "submit",
                "confirmed_at": now_iso})
    except (CloudError, json.JSONDecodeError):
        pass


def handle_profile_examples(payload: dict) -> dict:
    """POST /api/profile/examples：实施方直接设定本厂标准编号样例。

    覆盖式更新——给的是"标准写法清单"，不是往历史里掺样本；覆盖式是刻意的：
    实施方给清单这个动作本身就意味着"以这份为准"。写完立刻作废画像缓存，
    下一次校验马上用新档案。
    """
    raw = payload.get("examples")
    if not isinstance(raw, list) or not raw:
        return {"ok": False, "error": "examples 必须是非空数组"}
    vals: list[str] = []
    for x in raw[:20]:
        t = str(x).strip()
        if not (4 <= len(t) <= 60):
            return {"ok": False, "error": f"样例「{t[:20]}」长度须在 4-60 个字符之间"}
        vals.append(t)
    by = str(payload.get("by") or "").strip()[:60] or "实施方"
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        rows = CLOUD.rest_select(PROFILE_TABLE, "select=id&factory_key=eq.default&limit=1")
        if rows:
            CLOUD.rest_update(PROFILE_TABLE,
                              {"examples": vals, "source": "manual", "confirmed_by": by,
                               "confirmed_at": now_iso, "updated_at": now_iso},
                              query=f"id=eq.{rows[0].get('id')}")
        else:
            CLOUD.rest_insert(PROFILE_TABLE,
                              {"factory_key": "default", "examples": vals, "source": "manual",
                               "confirmed_by": by, "confirmed_at": now_iso})
    except CloudError as exc:
        return {"ok": False, "upstream": True, "error": f"写档案失败：{exc}"}
    _PROFILE["at"] = 0.0
    return {"ok": True, "examples": vals, "summary": profile_summary()}


def _validate_change(field: str, new_value) -> tuple:
    """校验单个更正项。返回 (规范化后的新值, 错误信息)；合法时错误信息为 None。

    与单字段版一致的约束，另补 order_date 格式检查：更正值会进有效版本视图
    被下游当日期用，垃圾格式进去比拒绝用户一次的代价高。
    表头字段与明细字段（items[i].key）分开校验：明细数值格与表头金额同规。
    """
    m = _ITEM_FIELD_RE.match(field)
    if not m and field not in CORRECTABLE_FIELDS:
        return None, (f"field 只允许 {'/'.join(CORRECTABLE_FIELDS)} 或 items[行号].name/qty/price/amount")
    if new_value is None or str(new_value).strip() == "":
        return None, "更正值不能为空（不支持用更正清空字段）"
    new_value = str(new_value).strip()
    if len(new_value) > 200:
        return None, "new_value 超长（上限 200）"
    if field == "total_amount":
        try:
            v = float(new_value)
        except ValueError:
            return None, "金额必须是数字"
        if v < 0 or v > 10 ** 12:
            return None, "金额超出合理范围"
    elif field == "order_date" and not _DATE_RE.match(new_value):
        return None, "日期格式应为 YYYY-MM-DD"
    elif m and m.group(2) in _ITEM_NUMERIC:
        try:
            v = float(new_value)
        except ValueError:
            return None, f"明细的 {m.group(2)} 必须是数字"
        if v < 0 or v > 10 ** 12:
            return None, f"明细的 {m.group(2)} 超出合理范围"
    return new_value, None


def handle_correct(payload: dict) -> dict:
    """POST /api/correct：对已入库记录追加更正（会计红字冲销）。

    支持批量：payload 带 changes=[{field,new_value},...]（表格模式一次改多个格），
    或兼容旧版单字段 field/new_value。批量时先全部校验再落库——要么整批合法，
    要么一条都不写，不会出现"改了一半"的中间态留在界面上。

    约束（每一条都有理由）：
    - 字段白名单 = 表头四字段 + 明细 items[i].name/qty/price/amount（0 基行号），
      与 DB CHECK 约束、前端渲染三处同源；item_count 是派生值，单独更正无意义；
    - old_value 由服务端从有效版本现读（表头查视图、明细从 extract_json 叠加
      已有更正）——不是原表，也不是客户端说了算；同格第二次更正的 old_value
      应是"上一次更正后的值"，否则更正链的 old→new 叙事就断了；
    - 与现值相同的项目直接跳过（不是错误）——表格模式天然会带上没改的格子；
    - 原记录 doc_parses 一字不动：UPDATE 会断主链哈希。这不是缺陷是设计，
      纠错 = 追加，两条链各自完整。
    """
    cid = payload.get("corrects_id")
    reason = str(payload.get("reason") or "").strip()
    by = str(payload.get("corrected_by") or "").strip()[:60] or "web"
    if isinstance(cid, bool) or not isinstance(cid, int):
        return {"ok": False, "error": "corrects_id 必须是整数"}
    if len(reason) > 500:
        return {"ok": False, "error": "reason 超长（上限 500）"}

    raw_changes = payload.get("changes")
    if raw_changes is None:
        raw_changes = [{"field": payload.get("field"), "new_value": payload.get("new_value")}]
    if not isinstance(raw_changes, list) or not raw_changes:
        return {"ok": False, "error": "changes 必须是非空数组"}
    if len(raw_changes) > MAX_CHANGES:
        return {"ok": False, "error": f"一次更正的项目数超过上限（{MAX_CHANGES}）"}

    seen: set = set()
    changes: list[tuple[str, str]] = []
    for ch in raw_changes:
        if not isinstance(ch, dict):
            return {"ok": False, "error": "changes 每项必须是 {field, new_value}"}
        field = str(ch.get("field") or "").strip()
        if field in seen:
            return {"ok": False, "error": f"字段 {field} 在一次更正里出现多次"}
        seen.add(field)
        val, err = _validate_change(field, ch.get("new_value"))
        if err:
            return {"ok": False, "error": f"{field}: {err}"}
        changes.append((field, val))

    # 旧值从有效版本读：表头字段查有效版本视图（视图已叠加历史更正）；
    # 明细字段视图管不到（值在 extract_json 里），在此单独叠加——
    # extract_json 原值按 id 序应用该记录已有的 items[i] 更正，得到的才是
    # 「当前有效值」。同格第二次更正的 old_value 必须是上一次改后的值，
    # 否则更正链的 old→new 叙事同样会断。
    try:
        rows = CLOUD.rest_select(
            EFFECTIVE_VIEW,
            "select=id,doc_no,supplier,order_date,total_amount,extract_json"
            f"&id=eq.{cid}&limit=1")
    except CloudError as exc:
        return {"ok": False, "upstream": True, "error": f"读取原记录失败：{exc}"}
    if not rows:
        return {"ok": False, "error": f"记录 id={cid} 不存在"}
    cur = rows[0]

    item_fields = [f for f, _v in changes if _ITEM_FIELD_RE.match(f)]
    eff_items = None
    if item_fields:
        try:
            ex = cur.get("extract_json")
            ex = json.loads(ex) if isinstance(ex, str) else ex
            items = ex.get("items") if isinstance(ex, dict) else None
            if isinstance(items, list):
                eff_items = [dict(it) if isinstance(it, dict) else {} for it in items]
        except (json.JSONDecodeError, TypeError):
            eff_items = None
        if eff_items is None:
            return {"ok": False, "error": "该记录没有可读的明细数据，无法更正明细"}
        # 叠加已有明细更正（按 id 升序，与链顺序一致）
        try:
            prior = CLOUD.rest_select(
                CORRECTIONS_TABLE,
                "select=field,new_value&corrects_id=eq."
                f"{cid}&field=like.items%5B*&order=id.asc")
        except CloudError as exc:
            return {"ok": False, "upstream": True, "error": f"读取已有更正失败：{exc}"}
        for p in prior:
            m = _ITEM_FIELD_RE.match(str(p.get("field") or ""))
            if not m:
                continue
            idx, key = int(m.group(1)), m.group(2)
            if 0 <= idx < len(eff_items):
                eff_items[idx][key] = p.get("new_value")

    def old_of(field: str):
        m = _ITEM_FIELD_RE.match(field)
        if not m:
            v = cur.get(field)
        else:
            idx, key = int(m.group(1)), m.group(2)
            if idx >= len(eff_items):
                return None, f"明细只有 {len(eff_items)} 行，没有第 {idx + 1} 行"
            v = eff_items[idx].get(key)
        return ("" if v is None else str(v)), None

    pending: list[tuple[str, str, str]] = []  # (field, old_s, new_value)
    for field, val in changes:
        old_s, err = old_of(field)
        if err:
            return {"ok": False, "error": err}
        if old_s == val:
            continue  # 没变的格子跳过，不是错误
        pending.append((field, old_s, val))
    if not pending:
        return {"ok": False, "error": "没有实际变更：提交的值与现值全部相同，无需更正"}

    applied: list[dict] = []
    for field, old_s, val in pending:
        try:
            inserted = CLOUD.rest_insert(CORRECTIONS_TABLE, {
                "corrects_id": cid, "field": field,
                "old_value": old_s, "new_value": val,
                "reason": reason[:500], "corrected_by": by})
        except CloudError as exc:
            # 中途失败如实上报：已落链的更正不回滚（链上记录不可撤销），
            # 未写的不再继续。界面会展示"成功 N 条 / 失败原因"，用户重试剩余的。
            resp = {"ok": False, "upstream": True,
                    "error": f"写更正失败（已成功 {len(applied)} 条，剩余未写）：{exc}"}
            if applied:
                resp["partial"] = True
                resp["corrections"] = applied
            return resp
        rec = inserted[0] if isinstance(inserted, list) and inserted else {}
        applied.append(rec)
    return {"ok": True, "corrections": applied,
            "correction": applied[0] if applied else None}


def corrections_for(cid) -> dict:
    """某条入库记录的全部更正（GET /api/corrections?corrects_id=N）。"""
    if isinstance(cid, bool) or not isinstance(cid, int):
        return {"ok": False, "error": "corrects_id 必须是整数"}
    try:
        rows = CLOUD.rest_select(
            CORRECTIONS_TABLE,
            "select=id,seq,record_hash,field,old_value,new_value,reason,corrected_by,created_at"
            f"&corrects_id=eq.{cid}&order=id.asc")
    except (CloudError, json.JSONDecodeError):
        return {"ok": False, "upstream": True, "error": "读取更正记录失败"}
    return {"ok": True, "corrections": rows}


def extract_sig(extract: dict) -> str:
    """抽取结果的内容指纹。用来判断"这次要入库的，是不是刚才复核过的那一份"。

    它不是安全边界（客户端拿不到它也伪造不了复核结果），只是一个复用的依据：
    复核结果存在服务端 doc 缓存里，只有内容指纹一致才复用，内容一变就重跑。
    """
    try:
        raw = json.dumps(extract, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = repr(extract)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def validate_with_source(extract: dict, doc: dict | None) -> tuple[list[dict], list[dict]]:
    """服务端权威校验 = 规则层（L1~L3）+ 出处层（L4）。

    为什么出处层必须挂在这里、而不是只在前端显示：
        L1~L3 全部围绕数字，而注入的最优形态是「只改名字不动数字」——三行金额
        一字不改，勾稽照样平。出处层是唯一会去看「这个值在不在图上」的判据。
        它一旦只在浏览器里跑，攻击者跳过页面直接 POST 就能绕开。

    没有 doc（原图已失效）时只跑规则层，并在返回里明确标注出处层未执行——
        「没验」和「验过并通过」是两件事，不能让它俩看起来一样。
    """
    issues = validate_with_profile(extract)
    details: list[dict] = []
    # 编号被 L2 归一（见 validate 的 info 事件）时，出处校验对**归一前的原值**做：
    # 归一后的值（PO…）在原文里没有连续出处——原文写的是 P0…，拿归一值对原文
    # 会得到 ~0.917 的覆盖率、落 warn 档，把"写法被归一"误报成"值没有出处"，
    # 再把看图复核触发回来，归一就白做了。出处层要回答的问题始终是
    # "这个值是不是来自这张图"，而对编号来说图上的字面就是 OCR 原值；
    # 归一的依据（历史画像）已由 L2 info 事件记录，两者互不越界。
    g_extract = extract
    for _i in issues:
        if _i.get("field") == "doc_no" and isinstance(_i.get("normalized"), dict):
            g_extract = dict(extract)
            g_extract["doc_no"] = _i["normalized"]["from"]
            break
    if doc and (doc.get("text") or "").strip():
        g_issues, details = check_grounding(g_extract, doc["text"])
        issues = issues + g_issues
    return issues, details


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "docagent/0.3"

    def _session(self) -> str:
        """本次请求的会话 id。没有就现造一个，并在响应里种 cookie。

        为什么用 cookie 而不是别的：它天然跨请求、由浏览器自动带回，不需要前端
        改一行代码。但它只是**分桶的依据**，不是权限——见 get_doc 的说明。
        """
        if getattr(self, "_sid", ""):
            return self._sid
        sid = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SID_COOKIE and 16 <= len(v) <= 64:
                sid = v
                break
        if not sid:
            sid = uuid.uuid4().hex[:24]
            self._sid_new = True
        self._sid = sid
        return sid

    def _doc(self, payload: dict) -> dict | None:
        """按 doc_id 取原图，带上本会话（先本会话、后全局索引）。"""
        return get_doc(str(payload.get("doc_id") or ""), self._session())

    def _send(
        self,
        body: bytes,
        code: int = 200,
        ctype: str = "application/json; charset=utf-8",
        head_only: bool = False,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_sid_new", False):
            # HttpOnly：前端不需要读它，也就不该让它被脚本碰到
            self.send_header(
                "Set-Cookie",
                f"{SID_COOKIE}={self._sid}; Path=/; Max-Age=3600; SameSite=Lax; HttpOnly",
            )
            self._sid_new = False
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200, head_only: bool = False) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            code,
            head_only=head_only,
        )

    def do_GET(self) -> None:  # noqa: N802
        self._serve_get(HEAD_ONLY=False)

    def do_HEAD(self) -> None:  # noqa: N802
        # 链接预览、聊天工具缩略图、部分浏览器打开前会先发 HEAD。
        # 不实现的话默认回 501，探活方会判定"站点不可用"。
        self._serve_get(HEAD_ONLY=True)

    def _serve_get(self, HEAD_ONLY: bool) -> None:  # noqa: N803
        path = self.path.split("?")[0]
        # 打开页面的同时把会话 cookie 种下去。不这么做的话，浏览器第一次并发发两个
        # /api/ocr 时两个请求都没有 cookie，服务端会各造一个会话，于是同一用户的两张图
        # 落在两个桶里——上限就是 8+8 而不是 8。
        self._session()
        if path.endswith("/favicon.ico"):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.endswith("/api/health"):
            self._json(
                {"ok": True, "backend": backend_name(), "python": sys.version.split()[0]},
                head_only=HEAD_ONLY,
            )
            return
        if path.endswith("/api/config"):
            # 不含任何服务端密钥；publishableKey 本就会被浏览器持有。
            # extractModel 只是"告诉页面服务端用哪个模型"用于展示，
            # 抽取请求不携带它 —— 带了也不会被采纳。
            self._json({"ok": True, **PUBLIC_CONFIG, "extractModel": EXTRACT_MODEL},
                       head_only=HEAD_ONLY)
            return
        if path.endswith("/api/eval"):
            # 评测数字必须由脚本从 report.json 读出来，不能在页面里手抄——
            # 手抄的数字改一次数据就对不上了，而"可复现"正是这个项目的卖点之一。
            self._json({"ok": True, **_load_eval_summary()}, head_only=HEAD_ONLY)
            return
        if path.endswith("/api/profile"):
            self._json(profile_summary(), head_only=HEAD_ONLY)
            return
        if path.endswith("/api/corrections"):
            qstr = self.path.split("?", 1)[1] if "?" in self.path else ""
            q = dict(pair.split("=", 1) for pair in qstr.split("&") if "=" in pair)
            try:
                cid = int(q.get("corrects_id", ""))
            except ValueError:
                self._json({"ok": False, "error": "corrects_id 必须是整数"},
                           400, head_only=HEAD_ONLY)
                return
            self._json(corrections_for(cid), head_only=HEAD_ONLY)
            return
        # 兜底：除 /api/* 外的一切 GET 都返回页面。
        # 平台可能用任意前缀代理（/preview、/p/xxx、多层子路径），
        # 精确匹配 "/" 只在"路径恰好是根"时成立，前缀一变用户就看到 not found。
        if path.startswith("/api/"):
            self._json({"ok": False, "error": "not found"}, 404, head_only=HEAD_ONLY)
            return
        self._send(
            load_page(),
            ctype="text/html; charset=utf-8",
            head_only=HEAD_ONLY,
        )

    # ---------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        try:
            payload = self._read_json()
            if payload is None:
                return
            if path.endswith("/api/ocr"):
                self._post_ocr(payload)
            elif path.endswith("/api/extract"):
                self._post_extract(payload)
            elif path.endswith("/api/submit"):
                self._post_submit(payload)
            elif path.endswith("/api/review"):
                self._post_review(payload)
            elif path.endswith("/api/apply"):
                self._post_apply(payload)
            elif path.endswith("/api/correct"):
                self._json(handle_correct(payload))
            elif path.endswith("/api/profile/examples"):
                self._json(handle_profile_examples(payload))
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except CloudError as exc:
            # 上游（数据库/模型通道）失败与本地逻辑错误必须分开报：
            # 前者可重试且不影响已算好的结果，后者是缺陷。
            sys.stderr.write(f"[docagent] cloud error: {exc}\n")
            self._json({"ok": False, "upstream": True, "retryable": exc.retryable,
                        "error": str(exc)}, 502)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(traceback.format_exc())
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._json({"ok": False, "error": "请求体为空"}, 400)
            return None
        if length > MAX_BYTES:
            self._json({"ok": False, "error": "文件超过 12MB 上限"}, 413)
            return None
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": f"请求体不是合法 JSON：{exc}"}, 400)
            return None
        if not isinstance(data, dict):
            self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
            return None
        return data

    # ------------------------------------------------------------------ OCR
    def _post_ocr(self, payload: dict) -> None:
        raw = payload.get("data_b64") or ""
        if "," in raw[:64]:
            raw = raw.split(",", 1)[1]
        try:
            blob = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            self._json({"ok": False, "error": "base64 解码失败"}, 400)
            return

        # 内存内解码，不落临时文件：用户单据不在磁盘留痕
        # 解码失败是"这张图不行"，不是服务出错——必须是 400 且给一句人话。
        # 之前这里直接抛，走到 do_POST 的兜底 except，用户看到的是 500 +
        # "RuntimeError: 无法解码图片"，看起来像系统挂了。
        try:
            bgr = decode_image(blob)
        except RuntimeError as exc:
            self._json({
                "ok": False,
                "error": "图片无法解码：文件可能不完整或格式不支持，请重新上传原图（不要用截图工具二次压缩）",
                "detail": str(exc)[:200],
            }, 400)
            return

        t_pre = time.perf_counter()
        # ① 去红章：必须在 OCR 之前。红油墨与黑印刷在 RGB 里可分，
        #    放到 OCR 之后就无法区分"印章碎片"和"真实的一个字"。
        clean, pre_meta = suppress_red(bgr)
        pre_ms = (time.perf_counter() - t_pre) * 1000

        t0 = time.perf_counter()
        items = run_ocr_array(clean)
        ocr_ms = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        # ② 版面重建（内含补漏规则：剔除倾斜异常的词，如灰度章弧排字）
        layout = rebuild(items)
        layout_ms = (time.perf_counter() - t1) * 1000

        # ③ 注入检测：必须在抽取之前跑，这样告警能和「模型是否真的被劫持」
        #    形成对照（检测到了 → 但金额仍是真实值 → 证明两条防线都生效）
        injection = detect(layout.text)

        # ④ 内容风险：数字字面量异常 / 影子字段 / 币种。
        #    和注入检测并列但意图不同——注入问「有人在指挥模型吗」，
        #    这一层问「这张单据的数字本身可信吗」，两者都要在抽取前拿到原文。
        content_risk = detect_content_risk(layout.text)

        # ⑤ 原图暂存：后面看图复核要用（那时请求早已结束），只留在内存。
        filename = str(payload.get("filename") or "")[:120]
        doc_id = cache_doc(
            blob, filename, layout.text, content_risk,
            {"words": len(items), "ocr_ms": round(ocr_ms), "skew_deg": round(layout.skew_deg, 2)},
            sid=self._session(),
        )

        self._json(
            {
                "ok": True,
                "doc_id": doc_id,
                "filename": filename,
                "bytes": len(blob),
                "words": len(items),
                "preprocess": pre_meta,
                "pre_ms": round(pre_ms, 1),
                "dropped": layout.dropped,
                "ocr_ms": round(ocr_ms),
                "layout_ms": round(layout_ms, 1),
                "skew_deg": round(layout.skew_deg, 2),
                "lines": len(layout.lines),
                "cols": layout.col_count,
                "markdown": to_markdown_table(layout),
                "text": layout.text,
                "items": items,
                "injection": injection,
                "content_risk": content_risk,
            }
        )

    # --------------------------------------------------------------- 字段抽取
    def _post_extract(self, payload: dict) -> None:
        """按 doc_id 抽取（2026-10-26 迁移为图片直读）。**只认 doc_id**。

        迁移的理由（评测实证，见 eval/report_live_v2.json 与报告补记 2/3/4）：
        OCR 串联抽取把 OCR 的系统性偏差（O/0、表格错位）直接喂给模型，明细
        准确率反而低于直读。迁移后抽取看原图（信息无损），OCR 降级为出处层
        的参照文本（本地跑、零 token）与复核裁决的第二证据源——它不再生产
        抽取结果，只提供"独立于视觉模型"的第二读数。

        来源可证的约束不变：抽取只能由服务端拿自己缓存的原图、用自己固定的
        模型算出来。客户端连"怎么问"都决定不了。
        """
        doc_id = str(payload.get("doc_id") or "")
        doc = get_doc(doc_id, self._session())
        if not doc:
            self._json({
                "ok": False,
                "error": "原图已失效（服务端每会话保留 15 分钟、最多 8 张），请重新上传",
            }, 410)
            return

        image_b64 = doc.get("image_b64") or ""
        if not image_b64:
            self._json({
                "ok": False,
                "error": "这张图的原图缓存不可用，请重新上传后再抽取",
            }, 400)
            return

        text = (doc.get("text") or "").strip()
        messages, nonce = build_messages_direct(image_b64)
        t0 = time.perf_counter()
        raw, usage = CLOUD.chat(messages, model=EXTRACT_MODEL,
                                temperature=0.1, json_mode=True, timeout=120.0)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        extract = _model_json(raw)
        if extract is None:
            # 不把原文整段回显：那等于把「换个说法就能套出内容」的探测面留给对方
            self._json({
                "ok": False,
                "error": "模型未返回可解析的 JSON，请重试或换一张更清晰的原图",
                "model": EXTRACT_MODEL,
                "raw_head": (raw or "")[:200],
            }, 502)
            return

        # 出处层在抽取返回时就跑一次，让人在页面上直接看到「每个值在原文哪里」。
        # 注意这只是展示：真正决定能不能入库的那次在 /api/submit 里重算，
        # 客户端拿到的这个结果改不动服务端的判定。
        # OCR 文本为空的图（识别不出的手写单等）不再拒绝抽取——直读不需要
        # 文本；此时出处层没有参照物可对，如实降级为仅规则层校验。
        if text:
            g_issues, g_details = check_grounding(extract, text)
            grounding_note = ""
        else:
            g_issues, g_details = [], []
            grounding_note = "本图无 OCR 文本，出处层无参照物未运行，仅规则层校验"

        self._json({
            "ok": True,
            "doc_id": doc_id,
            "extract": extract,
            "model": EXTRACT_MODEL,
            "extract_mode": "direct",
            "nonce": nonce,
            "usage": usage,
            "extract_ms": round(elapsed_ms),
            "chars": len(text),
            "injection": detect(text),
            "grounding": grounding_summary(g_details),
            "grounding_details": g_details,
            "grounding_issues": g_issues,
            "grounding_note": grounding_note,
        })

    # --------------------------------------------------------------- 校验/入库
    def _post_submit(self, payload: dict) -> None:
        """两层语义：commit=false 只校验，commit=true 校验后写库。

        刻意忽略客户端传来的 validate_ok / issues —— 这两个值如果由客户端给，
        整个改动就白做了。这里永远用自己的计算结果，客户端那份只用于显示。

        commit 分支的四道闸门（2026-10-25 加固）：
          ① 必须有 doc —— 没有原图就无法验证抽取值来自这张图，直接拒绝；
          ② 出处层（L4）参与判定 —— 名字类的错，勾稽查不出来；
          ③ needs_review 时**服务端自己**跑一次看图复核，不接受客户端"我已经复核过了"；
          ④ 复核结果与待入库值冲突 → 拒绝入库，把冲突原样退回。
        第 ③ 条每单多一次视觉模型调用。贵，但"入库"是终态操作：
        脏数据一旦进库，后面所有对账、付款、评级都以它为准。
        """
        extract = payload.get("extract")
        if not isinstance(extract, dict):
            self._json({"ok": False, "error": "缺少抽取结果 extract"}, 400)
            return
        doc = self._doc(payload)
        issues, g_details = validate_with_source(extract, doc)
        ok = is_ok(issues)
        grounding = grounding_summary(g_details)
        # 「没验」与「验过并通过」必须能区分：前者 grounding.checked==0
        grounding["ran"] = bool(doc and (doc.get("text") or "").strip())

        if not payload.get("commit"):
            needs, targets, triggers = review_targets(
                extract, issues, (doc or {}).get("content_risk"), supplier_history()
            )
            self._json({
                "ok": True,
                "validated_by": "server",
                "validate_ok": ok,
                "issues": issues,
                "needs_review": needs,
                "review_targets": targets,
                "review_triggers": triggers,
                "grounding": grounding,
                "grounding_details": g_details,
                # 原图还在不在，必须显式告诉前端：commit=false 这一路在 doc 失效时
                # 不会报错（只是不跑出处层），界面需要据此提示"早点重新上传"，
                # 否则用户点入库时才会撞上 410。
                "source_available": bool(doc),
            })
            return

        # ① 原图必须还在：否则这份 extract 的来源不可证，不能入库
        if not doc:
            self._json({
                "ok": False,
                "code": "source_unavailable",
                "error": "原图已失效（服务端只保留 15 分钟、最多 8 张），"
                         "无法确认这份数据来自这张图，已拒绝入库。请重新上传后再提交。",
            }, 410)
            return

        # ③ needs_review ⇒ 服务端强制复核一次，不采信客户端自报的复核结果
        needs, targets, triggers = review_targets(
            extract, issues, doc.get("content_risk"), supplier_history()
        )
        forced = None
        if needs:
            if not doc.get("image_b64"):
                self._json({
                    "ok": False,
                    "code": "image_unavailable",
                    "error": "这笔单据需要看图复核，但服务端没有保住原图（压缩失败），"
                             "无法完成复核，已拒绝入库。请重新上传。",
                }, 409)
                return
            cache = doc.get("review_cache") or {}
            reuse = cache.get("sig") == extract_sig(extract) and cache.get("findings") is not None
            try:
                if reuse:
                    # 复用的是**服务端自己**刚跑出来的结果，不是客户端交上来的。
                    # 客户端那份 review 字段只作记录，从不参与判定。
                    findings = cache["findings"]
                    usage = cache.get("usage") or {}
                else:
                    findings, usage = run_review(
                        CLOUD, doc["image_b64"], doc.get("text") or "",
                        extract, targets, REVIEW_MODEL,
                    )
            except Exception as exc:  # noqa: BLE001
                # 复核跑不起来 ≠ 复核通过。拒绝入库并标可重试，
                # 否则"让复核失败"就成了一条免检通道。
                sys.stderr.write(f"[docagent] forced review failed: {exc}\n")
                self._json({
                    "ok": False,
                    "code": "review_unavailable",
                    "retryable": True,
                    "error": f"这笔单据有疑点，需要看图复核，但复核调用失败（{type(exc).__name__}）。"
                             f"数据尚未入库，请稍后重试。",
                }, 502)
                return

            # ④ 复核读到值与待入库值不一致 → 停在门口，把证据退回
            conflicts = [f for f in findings
                         if f.get("verdict") == "mismatch" and f.get("apply_value")]
            if conflicts:
                self._json({
                    "ok": False,
                    "code": "review_conflict",
                    "committed": False,
                    "error": "看图复核读到的值与待入库的值不一致，已拒绝入库。"
                             "请先按复核结果修正后再提交。",
                    "conflicts": conflicts,
                    "issues": issues,
                    "review_triggers": triggers[:6],
                }, 409)
                return
            forced = {
                "auto": True,
                "reused": bool(reuse),
                "model": REVIEW_MODEL,
                "findings": findings,
                "reviewed_fields": [t["field"] for t in targets],
                "usage": {"prompt_tokens": usage.get("prompt_tokens"),
                          "completion_tokens": usage.get("completion_tokens")},
            }

        stats = payload.get("ocr") if isinstance(payload.get("ocr"), dict) else {}
        items = extract.get("items") if isinstance(extract.get("items"), list) else []
        total = extract.get("total_amount")
        try:
            total_num = float(total) if total is not None and str(total) != "" else None
        except (TypeError, ValueError):
            total_num = None

        client_review = payload.get("review") if isinstance(payload.get("review"), dict) else None
        # 复核记录必须如实：哪次是服务端强制跑的、客户端是否也提交过一份。
        # 这个字段进 hash（v2 起），改它会被链查出。
        review = ({"server_forced": forced, "client": client_review}
                  if forced else client_review)
        row = {
            # owner_id 由数据库 DEFAULT 填充，服务端绝不代填
            "filename": doc.get("filename") or str(payload.get("filename") or "")[:120] or None,
            "doc_no": extract.get("doc_no"),
            "supplier": extract.get("supplier"),
            "order_date": extract.get("order_date"),
            "total_amount": total_num,
            "item_count": len(items),
            "extract_json": extract,
            "validate_ok": ok,
            "issues": issues,
            "ocr_words": doc.get("words") or stats.get("words"),
            "ocr_ms": doc.get("ocr_ms") or stats.get("ocr_ms"),
            "skew_deg": doc.get("skew_deg") or stats.get("skew_deg"),
            "model_id": str(payload.get("model_id") or "")[:60] or None,
            "review_json": review,
            "validated_by": "server",
        }
        inserted = CLOUD.rest_insert(TABLE, row)
        # 编号写法沉淀进工厂档案（失败静默，见函数内注释）。
        _profile_record_confirmed(extract.get("doc_no"))
        rec = inserted[0] if isinstance(inserted, list) and inserted else {}
        self._json({
            "ok": True,
            "validated_by": "server",
            "id": rec.get("id"),
            "seq": rec.get("seq"),
            "record_hash": rec.get("record_hash"),
            "hash_ver": rec.get("hash_ver"),
            "validate_ok": ok,
            "issues": issues,
            "grounding": grounding,
            # 明细也一并返回：入库后那一屏要能点开「核对详情」看到每个字段对上多少，
            # 否则界面上只剩一句"已入库"，事后没人能回答"当时核对到什么程度"。
            "grounding_details": g_details,
            "forced_review": bool(forced),
            "reviewed_fields": (forced or {}).get("reviewed_fields") or [],
        })

    # ----------------------------------------------------------------- 复核
    def _post_review(self, payload: dict) -> None:
        doc = self._doc(payload)
        if not doc or not doc.get("image_b64"):
            self._json({"ok": False, "expired": True,
                        "error": "原图已失效（超过 15 分钟或服务重启），请重新上传单据"}, 400)
            return
        extract = payload.get("extract") if isinstance(payload.get("extract"), dict) else {}
        # 复核目标一律由服务端重算，不采用客户端传来的 targets。
        # 原因：target 里带 rule_fix（规则建议值），而"看图结果是否与规则一致"决定
        # 要不要放行形近字符的修改建议。这个值若由浏览器给，双源互证就等于自己跟
        # 自己印证，仲裁规则当场失效。客户端那份只影响它自己的显示。
        # 与 /api/submit 用同一套判定（规则层 + 出处层）。两边若不一致，
        # 页面复核过的字段和入库时要求复核的字段就会对不上。
        issues, _ = validate_with_source(extract, doc)
        _, targets, _ = review_targets(extract, issues, doc.get("content_risk"), supplier_history())
        if not targets:
            self._json({"ok": True, "findings": [], "skipped": True, "reason": "没有需要复核的疑点"})
            return
        # 模型不接受客户端指定：换弱模型可以降低复核判据的质量，
        # 换贵模型可以烧配额。两条都不该由请求方决定。
        model = REVIEW_MODEL
        findings, usage = run_review(
            CLOUD, doc["image_b64"], doc.get("text") or "", extract, targets, model
        )
        # 存一份到 doc 缓存：commit 时若待入库内容与现在完全一致，直接复用这次结果，
        # 不再多花一次视觉调用。内容一变（人改过值），指纹就对不上，必然重跑。
        doc["review_cache"] = {
            "sig": extract_sig(extract),
            "findings": findings,
            "reviewed_fields": [t["field"] for t in targets],
            "usage": {"prompt_tokens": usage.get("prompt_tokens"),
                      "completion_tokens": usage.get("completion_tokens")},
        }
        self._json({
            "ok": True,
            "model": model,
            "findings": findings,
            "reviewed_fields": [t["field"] for t in targets],
            "usage": {"prompt_tokens": usage.get("prompt_tokens"),
                      "completion_tokens": usage.get("completion_tokens")},
        })

    # ------------------------------------------------- 按人工决定执行修改
    def _post_apply(self, payload: dict) -> None:
        """人给方向，模型执行，服务端核对——三步缺一不可，顺序也不能换。

        `decisions` 是人在页面上做完的选择，每条 {field, choice, value, text}。
        模型只负责把它展开成具体字段改动（连带重算行金额、合计、合并重复行），
        它给出的每一条改动都要过 verify_patches，不合规的丢掉并回报原因。

        这个接口**不写库**。写库仍然只有 /api/submit 一条路，且那条路会用自己的
        规则再校验一遍——执行说"改成这样"，存储说"这样合不合规"，两件事分开。
        """
        extract = payload.get("extract")
        if not isinstance(extract, dict):
            self._json({"ok": False, "error": "缺少抽取结果 extract"}, 400)
            return
        raw = payload.get("decisions")
        decisions = [d for d in raw if isinstance(d, dict) and isinstance(d.get("field"), str)] \
            if isinstance(raw, list) else []
        if not decisions:
            self._json({"ok": False, "error": "没有提供任何修改决定"}, 400)
            return
        if len(decisions) > 40:
            self._json({"ok": False, "error": "一次提交的修改项过多"}, 400)
            return

        # 只有"真的要改"的才送进模型。全是「保持当前值」时不该花一次调用，
        # 也不该让模型有机会在这些字段上做任何事。
        active = [d for d in decisions
                  if d.get("value") is not None or str(d.get("choice")) == "merge"]
        if not active:
            issues, _ = validate_with_source(extract, self._doc(payload))
            self._json({"ok": True, "model": None, "degraded": None, "patches": [],
                        "rejected": [], "notes": "没有选中任何修改，数据保持原样",
                        "extract": extract, "issues": issues,
                        "validate_ok": is_ok(issues), "to_confirm": [], "usage": {}})
            return

        doc = self._doc(payload)
        image = None
        if doc and doc.get("image_b64"):
            try:
                image = encode_for_vision(doc["image_b64"])
            except Exception:  # noqa: BLE001
                image = None  # 图读不出来就只按文字执行，不因此让整次操作失败
        # 模型不接受客户端指定：换弱模型可以降低复核判据的质量，
        # 换贵模型可以烧配额。两条都不该由请求方决定。
        model = REVIEW_MODEL

        degraded = None
        merge_wanted = sum(1 for d in active if str(d.get("choice")) == "merge")
        if payload.get("mode") == "direct":
            # 表格模式：纯人工修改，人工给的值就是终值。不进模型——确定性操作
            # 不该引入不可控的"帮忙"，也不该为此花一次调用。merge 需要模型判断
            # 两行是否同一物料，direct 下没有模型，遇到就如实降级说明。
            patches_raw, notes, usage = _local_patches(active), "人工表格直接修改", {}
            if merge_wanted:
                degraded = f"表格模式不执行「合并重复行」（需模型判断两行是否同一物料），{merge_wanted} 项已忽略"
        else:
            try:
                patches_raw, notes, usage = run_apply(
                    CLOUD, image, (doc or {}).get("text") or "", extract, active, model
                )
                if not patches_raw:
                    # 模型什么都没给：不是错误，但不能就这么过去——否则人点了"改"
                    # 却没改，还以为改了。这里回落成本地确定性改动，并在结果里说明。
                    degraded = "模型没有返回任何改动，已按你选定的值直接执行"
                    patches_raw, notes = _local_patches(active), "按人工选定的值执行"
            except Exception as exc:  # noqa: BLE001
                # 模型挂了不能卡住入库：改成"照人选的值直接写"。这也是降级路径，
                # 必须让界面知道这次没有模型参与，不能假装两种判据都用过。
                degraded = f"模型不可用（{type(exc).__name__}），已按你选定的值直接执行"
                patches_raw, notes, usage = _local_patches(active), "按人工选定的值执行", {}
            if degraded and merge_wanted:
                degraded += f"；其中 {merge_wanted} 项「合并重复行」需要模型判断两行是否同一物料，本次未执行"

        accepted, rejected = verify_patches(patches_raw, extract, active)

        # 人的决定是**硬约束**，不是给模型的参考。模型漏掉某个被点名的字段时
        # （它经常只处理自己觉得有问题的那几个），必须按人选的值补上——
        # 否则会出现"页面提示已执行、数据其实没变"这种最难解释的状态。
        covered = {p["field"] for p in accepted if p["op"] == "set"}
        gaps = [d for d in active
                if d.get("value") is not None and str(d.get("choice")) != "keep"
                and str(d.get("field")) not in covered]
        if gaps:
            fill, rejected_fill = verify_patches(_local_patches(gaps), extract, active)
            accepted += fill
            rejected += rejected_fill
            gap_note = f"模型未覆盖你指定的 {len(gaps)} 个字段，已按你选定的值直接写入"
            notes = f"{notes}；{gap_note}" if notes else gap_note

        after = apply_patches(extract, accepted)
        # 补丁改完之后要重跑**出处层**：如果模型把供应商改成了原文里没有的名字，
        # 这里就该立刻看出来，而不是等入库时才被拦。
        issues, g_details = validate_with_source(after, doc)
        self._json({
            "ok": True,
            "model": model if not degraded else None,
            "degraded": degraded,
            "patches": accepted,
            "rejected": rejected,
            "notes": notes,
            "extract": after,
            "issues": issues,
            "validate_ok": is_ok(issues),
            "grounding": grounding_summary(g_details),
            "grounding_details": g_details,
            "to_confirm": explain_patches(accepted, after, active),
            "usage": {"prompt_tokens": usage.get("prompt_tokens"),
                      "completion_tokens": usage.get("completion_tokens")},
        })

    def log_message(self, fmt: str, *args) -> None:
        # 只记方法/路径/状态码，不记请求体——单据内容不进日志
        sys.stderr.write("[docagent] " + fmt % args + "\n")


def _local_patches(decisions: list[dict]) -> list[dict]:
    """模型不可用时的降级：只把"人已经选定的那个值"写进去，不做任何联动推导。

    为什么降级不做联动：重算行金额、合计、合并重复行都属于"语义→算术"的推导，
    本地硬算等于替人做决定，而且极易算错（改单价后哪几行受影响，本地没有依据）。
    宁可不联动，让 /api/submit 的校验把算不平的地方如实报出来，也不要悄悄
    改出一个"看起来对"的结果。
    """
    out: list[dict] = []
    for d in decisions:
        if str(d.get("choice")) == "merge" and d.get("value") is None:
            continue  # 合并行必须由模型判断两行是不是同一个物料，本地不猜
        out.append({"op": "set", "field": d.get("field"), "to": d.get("value"),
                    "why": "按人工选定的值直接执行（本次未使用模型）"})
    return out


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[docagent] listening on 0.0.0.0:{port}", flush=True)
    print(f"[docagent] ocr backend = {backend_name()}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
