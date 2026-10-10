"""命令包。

每个命令都是 ``f(args, ctx, repo) -> int`` 这样的纯函数，
返回进程退出码。渲染全部走 ``render.py``，MCP 交互全部走 ``services.py``。
"""
