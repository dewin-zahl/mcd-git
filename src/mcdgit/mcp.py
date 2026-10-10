"""麦当劳 MCP 客户端 —— 只用标准库，`git clone` 完就能跑。

MCP 的 streamable-http 传输本质是三件事：
  1. POST 一个 JSON-RPC 请求
  2. 响应可能是 ``application/json``，也可能是 ``text/event-stream``（SSE）
  3. 服务端如果在响应头里给了 ``Mcp-Session-Id``，后续请求要带上

这个文件把这三件事封成一个 ``call()``，剩下的交给上层。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import ConfigError, McpAuthError, McpError, McpToolError

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "mcdgit"

#: 允许从别的 MCP 客户端配置里"蹭"一个 token，省得用户重复粘贴。
#: 只读 ``headers.Authorization``，不写入、不回显、不落盘。
_TOKEN_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("env", ("MCD_MCP_TOKEN",)),
    ("pi", ("~/.pi/agent/mcp.json",)),
    ("claude", ("~/.claude.json", "~/.claude/settings.json")),
    ("cursor", ("~/.cursor/mcp.json",)),
)


def _discover_token() -> tuple[str | None, str]:
    """按 env → pi → claude → cursor 的顺序找一个 token。

    返回 ``(token, 来源描述)``。找到的第一个能用就用，不做验证（验证要发请求）。
    """
    for source, candidates in _TOKEN_SOURCES:
        if source == "env":
            for var in candidates:
                raw = os.environ.get(var)
                if raw:
                    return _strip_bearer(raw), f"${var}"
            continue
        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            token = _find_bearer(data)
            if token:
                return token, str(path)
    return None, ""


def _find_bearer(node: Any) -> str | None:
    """在任意嵌套结构里找 ``headers.Authorization``（或直接的 ``token``）。"""
    if isinstance(node, dict):
        headers = node.get("headers")
        if isinstance(headers, dict):
            for key, value in headers.items():
                if key.lower() == "authorization" and isinstance(value, str):
                    found = _strip_bearer(value)
                    if found:
                        return found
        for key in ("token", "apiKey", "api_key"):
            value = node.get(key)
            if isinstance(value, str) and len(value) > 12:
                return _strip_bearer(value)
        for value in node.values():
            found = _find_bearer(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_bearer(item)
            if found:
                return found
    return None


def _strip_bearer(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


class McpClient:
    """一个极简的 MCP streamable-http 客户端。

    典型用法::

        with McpClient() as mcp:
            mcp.initialize()
            data = mcp.call("query_meals", storeCode="1234", orderType=1)
    """

    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        verbose: bool = False,
    ) -> None:
        self.url = (url or os.environ.get("MCD_MCP_URL") or DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.verbose = verbose
        self._session_id: str | None = None
        self._next_id = 1
        self._ready = False

        if token:
            self.token = _strip_bearer(token)
            self.token_source = "--token"
        else:
            self.token, self.token_source = _discover_token()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def __enter__(self) -> McpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """尽最大努力通知服务端结束会话；**任何**失败都无所谓。

        这里故意捕得很宽：TCP 连接被服务端/系统中断（Windows 上很常见，
        ``WinError 10053``）不应该让一个已经干完活的命令变成失败。
        """
        if self._session_id and self._ready:
            try:
                self._request("DELETE", {}, expect_response=False)
            except Exception:  # noqa: BLE001
                pass
        self._session_id = None
        self._ready = False

    def initialize(self) -> dict[str, Any]:
        """握手。幂等，重复调用只发一次。"""
        if self._ready:
            return {}
        if not self.token:
            raise ConfigError(
                "找不到麦当劳 MCP Token。\n"
                "        任选一种方式：\n"
                "          1. export MCD_MCP_TOKEN=<your-token>\n"
                "          2. mcdgit init --token <your-token>\n"
                "        Token 申请：https://open.mcd.cn/mcp → 右上角控制台 → 激活"
            )
        result = self._request(
            "POST",
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": "0.1.0"},
                },
            },
        )
        # notifications/initialized 是通知，没有 id，也不需要回包
        self._request(
            "POST",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_response=False,
        )
        self._ready = True
        return result

    # ------------------------------------------------------------------ #
    # 调用
    # ------------------------------------------------------------------ #

    def list_tools(self) -> list[dict[str, Any]]:
        self.initialize()
        result = self._request(
            "POST",
            {"jsonrpc": "2.0", "id": self._id(), "method": "tools/list", "params": {}},
        )
        return list(result.get("tools", []))

    def call(self, tool: str, **arguments: Any) -> Any:
        """调用一个 MCP 工具，返回**已解析的**业务数据。

        麦当劳 MCP 把业务结果放在 ``content[0].text`` 里，且通常是一段 JSON
        字符串。这里顺手解开，让上层拿到的直接是 dict/list。
        失败时抛 :class:`McpToolError`。
        """
        self.initialize()
        cleaned = {k: v for k, v in arguments.items() if v is not None}
        result = self._request(
            "POST",
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "tools/call",
                "params": {"name": tool, "arguments": cleaned},
            },
        )
        payload = self._unwrap_tool_result(tool, result)
        return payload

    @staticmethod
    def _unwrap_tool_result(tool: str, result: dict[str, Any]) -> Any:
        if result.get("isError"):
            raise McpToolError(tool, _flatten_content(result))

        text = _flatten_content(result)
        if not text:
            return None

        parsed = _extract_json(text)
        if parsed is None:
            # 完全不是 JSON：麦当劳 MCP 有时直接回一句人话错误
            return text

        # 麦当劳 MCP 外层通常是 {"success":..,"code":..,"message":..,"data":..}
        if isinstance(parsed, dict) and "data" in parsed:
            code = parsed.get("code")
            if _is_failure(parsed):
                message = parsed.get("message") or parsed.get("msg") or f"code={code}"
                raise McpToolError(tool, str(message))
            return parsed["data"]
        return parsed

    # ------------------------------------------------------------------ #
    # 传输层
    # ------------------------------------------------------------------ #

    def _id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": f"{CLIENT_NAME}/0.1.0",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _request(
        self,
        method: str,
        body: dict[str, Any],
        *,
        expect_response: bool = True,
    ) -> dict[str, Any]:
        if self.verbose:
            print(f"[mcp] {method} {self.url} {body.get('method', '')}", file=sys.stderr)

        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self._session_id = session
                raw = response.read().decode("utf-8", errors="replace")
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise McpAuthError() from exc
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise McpError(f"HTTP {exc.code} from {self.url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise McpError(f"连不上 {self.url}：{exc.reason}") from exc
        except TimeoutError as exc:
            raise McpError(f"请求超时（{self.timeout}s）：{self.url}") from exc
        except OSError as exc:
            # 连接被对端/系统中断（Windows 上体现为 WinError 10053）
            raise McpError(f"与 {self.url} 的连接中断：{exc}") from exc

        if not expect_response:
            return {}

        message = _parse_body(raw, content_type)
        if message is None:
            raise McpError(f"服务端没有返回 JSON-RPC 响应（Content-Type: {content_type}）")
        if "error" in message:
            error = message["error"]
            raise McpError(
                f"{error.get('code')}: {error.get('message')}"
                if isinstance(error, dict)
                else str(error)
            )
        return message.get("result") or {}


def _parse_body(raw: str, content_type: str) -> dict[str, Any] | None:
    """兼容 ``application/json`` 和 ``text/event-stream`` 两种响应。"""
    if not raw.strip():
        return None

    if "text/event-stream" in content_type or raw.lstrip().startswith("event:"):
        for line in raw.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk:
                    try:
                        return json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 有些网关会忽略 Content-Type，退化成 SSE
        return _parse_body(raw, "text/event-stream")


def _is_failure(parsed: dict[str, Any]) -> bool:
    """麦当劳 MCP 用 success=False 或 非零 code 表示业务失败。"""
    if parsed.get("success") is False:
        return True
    code = parsed.get("code")
    if code is None:
        return False
    return code not in (0, 200, "0", "200")


def _extract_json(text: str) -> Any | None:
    """从 MCP 返回的文本里抠出 JSON。

    麦当劳 MCP 的 ``content[0].text`` 是三段拼接的：

        "每个门店的基本信息按单行展示：\n- **门店名称**: ...\n"   ← 给 LLM 的字段说明
        "{\"success\":true,\"code\":200,\"data\":[...]}"          ← 真正的数据
        "## 展示结果时，每个门店的 storeCode 必须完整展示"          ← 尾部提醒

    所以既不能整体 ``json.loads``，也不能"从后往前找最后一个 ``{``"——
    尾部/内部的嵌套对象会把结果带偏。正确做法是**找信封**：
    第一个同时含 ``success`` 或 ``code`` 的对象就是它。
    """
    text = text.strip()
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    fallback: tuple[int, Any] | None = None
    for start, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        if "success" in value or "code" in value:
            return value  # 信封找到了，不用再往后看
        span = end - start
        if fallback is None or span > fallback[0]:
            fallback = (span, value)
    return fallback[1] if fallback else None


def _flatten_content(result: dict[str, Any]) -> str:
    """把 MCP 的 content 数组拼成一坨文本。"""
    blocks = result.get("content") or []
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif "text" in block:
                parts.append(str(block["text"]))
        elif isinstance(block, str):
            parts.append(block)
    if not parts and result.get("structuredContent"):
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return "\n".join(parts).strip()
