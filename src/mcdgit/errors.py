"""异常类型。所有面向用户的错误都从这里出去，保证 CLI 只打印一行人话。"""

from __future__ import annotations


class McdGitError(Exception):
    """所有 mcdgit 错误的基类。CLI 会捕获它并打印 ``fatal: <msg>``。"""

    exit_code = 1


class ConfigError(McdGitError):
    """配置缺失或非法（例如没设置 MCD_MCP_TOKEN）。"""


class NotARepoError(McdGitError):
    """当前目录不是 mcdgit 仓库。"""

    def __init__(self, path: str = ".") -> None:
        super().__init__(
            f"not a mcdgit repository (or any of the parent directories): {path}\n"
            f"        先跑 `mcdgit init`——毕竟你得先决定去哪家店。"
        )


class McpError(McdGitError):
    """MCP 传输层或协议层出错。"""


class McpAuthError(McpError):
    """Token 无效或过期。"""

    def __init__(self) -> None:
        super().__init__(
            "MCP 认证失败（401/403）。请检查 MCD_MCP_TOKEN 是否有效：\n"
            "        https://open.mcd.cn/mcp → 控制台 → 激活 → 复制 Token"
        )


class McpToolError(McpError):
    """MCP 工具返回了业务错误。"""

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        super().__init__(f"{tool} 调用失败：{message}")


class NothingToCommitError(McdGitError):
    """暂存区是空的。"""

    def __init__(self) -> None:
        super().__init__(
            "nothing to commit, working tree clean\n"
            "        连一份薯条都没加，你提交个寂寞。"
        )


class MergeConflict(McdGitError):
    """团餐合并冲突：有人和你抢同一份餐品。"""

    exit_code = 1

    def __init__(self, meal: str, parties: list[str]) -> None:
        self.meal = meal
        self.parties = parties
        lines = [f"CONFLICT (content): Merge conflict in {meal}", ""]
        for who in parties:
            lines.append(f"        {who}: 我要")
        lines.append("")
        lines.append(
            "        解决：--strategy ours（我点的） / --strategy theirs（他点的） "
            "/ --strategy union（都点上）"
        )
        super().__init__("\n".join(lines))
