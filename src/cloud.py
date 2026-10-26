"""云服务 REST 客户端（服务端用）。

为什么服务端要直连，而不是继续让浏览器做
--------------------------------------------
"校验以后端判定为准"这句话要成立，后端必须自己算、自己写。原来的入库在浏览器里
完成，浏览器提交什么就存什么，`validate_ok` 也只是浏览器算的一个布尔值——
前端随手就能改成 true。现在改成服务端复算再写库，前端那份只用于显示。

实测确认（2026-10-25，非推测）
--------------------------------
`/.cloud/database/rest` 与 `/.cloud/llm/chat/completions` 的鉴权头都是
`x-wb-webapp-access-key: <publishableKey>`；服务端不带 Origin 也能通
（GET 返回真实数据；POST 非法列名返回 PG 的 PGRST204，说明已到数据库层；
 chat 用 `stream=true` 返回 200 与 SSE 流）。

边界与安全
----------
- publishableKey 是公开标识（它本来就会出现在页面里），不是密钥；真正的约束在
  平台侧（Origin 白名单、配额）。它不进日志、不进错误消息。
- 本模块只做传输，不含业务判断。
- 所有请求带显式超时；网络错误 / 429 / 5xx 重试一次（指数退避 + 抖动）；
  其余 4xx 直接失败，不重试。
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request

# 失败分类：可重试 = 网络抖动、限流、上游 5xx；不可重试 = 参数/权限问题
_RETRY_STATUS = {429, 500, 502, 503, 504}


class CloudError(RuntimeError):
    """调用云服务失败。retryable 表示"再试一次可能就通"。"""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class CloudClient:
    def __init__(self, endpoint: str, publishable_key: str, retries: int = 1):
        self.endpoint = endpoint.rstrip("/")
        self.key = publishable_key
        self.retries = retries
        # 服务端进程不受沙箱代理影响，但显式禁用代理更稳：容器内出网走直连
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------ 传输
    def _once(
        self,
        method: str,
        path: str,
        payload: dict | None,
        accept: str,
        timeout: float,
        headers: dict | None = None,
    ) -> bytes:
        url = f"{self.endpoint}{path}"
        data = None
        hdrs = {"Accept": accept, "x-wb-webapp-access-key": self.key}
        if headers:
            hdrs.update(headers)
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        with self._opener.open(req, timeout=timeout) as resp:
            return resp.read()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        accept: str = "application/json",
        timeout: float = 30.0,
        retry: bool = True,
        headers: dict | None = None,
    ) -> bytes:
        attempts = (self.retries if retry else 0) + 1
        last: CloudError | None = None
        for i in range(attempts):
            try:
                return self._once(method, path, payload, accept, timeout, headers)
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:400]
                except Exception:  # noqa: BLE001 - 读错误体失败不影响主流程
                    pass
                retryable = exc.code in _RETRY_STATUS
                last = CloudError(
                    f"云服务返回 HTTP {exc.code}: {body or exc.reason}",
                    status=exc.code,
                    retryable=retryable,
                )
                if not retryable:
                    raise last from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = CloudError(f"云服务连接失败：{type(exc).__name__}: {exc}", retryable=True)
            if i < attempts - 1:
                # 指数退避 + 抖动，避免多实例同时重试打穿上游
                time.sleep(0.5 * (2**i) + random.uniform(0, 0.3))
        assert last is not None
        raise last

    # --------------------------------------------------------------- 数据库
    def rest_select(self, table: str, query: str = "", timeout: float = 20.0) -> list:
        path = f"/.cloud/database/rest/{table}"
        if query:
            path += "?" + query.lstrip("?")
        raw = self._request("GET", path, timeout=timeout)
        return json.loads(raw.decode("utf-8") or "[]")

    def rest_insert(self, table: str, row: dict, timeout: float = 25.0) -> list:
        path = f"/.cloud/database/rest/{table}"
        raw = self._request(
            "POST",
            path,
            payload=row,
            accept="application/json",
            timeout=timeout,
            retry=False,  # 写操作不重试：重试可能产生重复记录
            headers={"Prefer": "return=representation"},  # 要回真正的写入结果（id/seq/hash）
        )
        return json.loads(raw.decode("utf-8") or "[]")

    def rest_update(self, table: str, row: dict, query: str = "", timeout: float = 25.0) -> list:
        """按条件更新（PATCH）。仅用于 doc_no_profiles 档案维护这类低频元数据，
        不进主入库链路（那条链只有 INSERT，append-only）。"""
        path = f"/.cloud/database/rest/{table}"
        if query:
            path += "?" + query.lstrip("?")
        raw = self._request(
            "PATCH",
            path,
            payload=row,
            accept="application/json",
            timeout=timeout,
            retry=False,  # 同 rest_insert：写操作不重试，避免重复写
            headers={"Prefer": "return=representation"},
        )
        return json.loads(raw.decode("utf-8") or "[]")

    # ------------------------------------------------------------------ 模型
    def chat(
        self,
        messages: list[dict],
        model: str,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        timeout: float = 120.0,
        json_mode: bool = False,
    ) -> tuple[str, dict]:
        """一次会话补全。云通道只支持流式，这里把 SSE 收完再返回。

        返回 (正文, usage)。图片走 OpenAI 兼容的 image_url + data URL。
        json_mode=True 时请求 response_format=json_object（复核结果必须是可解析 JSON）。
        """
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        raw = self._request(
            "POST",
            "/.cloud/llm/chat/completions",
            payload=payload,
            accept="text/event-stream",
            timeout=timeout,
        )
        return _parse_sse(raw)


def _parse_sse(raw: bytes) -> tuple[str, dict]:
    text_parts: list[str] = []
    usage: dict = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if obj.get("usage"):
            usage = obj["usage"]
        choices = obj.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            text_parts.append(piece)
    return "".join(text_parts), usage
