"""命令共享的运行时上下文。

关键设计：**MCP 客户端是懒加载的**。
``status`` / ``log`` / ``branch`` / ``stash`` 这些命令一次网络都不发，
所以本地操作可以离线跑——跟真 git 一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp import McpClient
from .render import Palette, make_palette


@dataclass
class Context:
    """一次 CLI 调用的全部环境。"""

    no_color: bool = False
    verbose: bool = False
    token: str | None = None
    endpoint: str | None = None
    palette: Palette = field(init=False)
    _client: McpClient | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.palette = make_palette(self.no_color)

    @property
    def p(self) -> Palette:
        return self.palette

    def mcp(self) -> McpClient:
        """拿到 MCP 客户端（第一次调用时才建连接）。"""
        if self._client is None:
            self._client = McpClient(
                url=self.endpoint, token=self.token, verbose=self.verbose
            )
        return self._client

    def call(self, tool: str, **arguments: Any) -> Any:
        return self.mcp().call(tool, **arguments)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
