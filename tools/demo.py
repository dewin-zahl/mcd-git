#!/usr/bin/env python3
"""跑一段完整的 mcdgit 演示会话，输出可以直接贴进 README 的终端文字。

**它不会下任何真实订单** —— 全程打在本仓库自带的假 MCP 服务上
（``tests/mock_mcp.py``）。这是两种用途：

1. 生成 README 里的示例输出（每次改渲染逻辑都可以重新生成，不会过期）
2. 让人在掏钱之前先看看这东西长什么样

    python tools/demo.py
    python tools/demo.py --no-color > docs/demo.txt
"""

from __future__ import annotations

import argparse
import io
import os
import shlex
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mcdgit.cli import main  # noqa: E402
from tests.mock_mcp import MockMcpServer  # noqa: E402

#: (要显示的横幅, 实际执行的命令)。命令用 shlex 切，所以**引号必须留在里面**。
#: (要显示的横幅, 实际执行的命令)；命令用 shlex 切，所以**引号必须留在里面**。
#:
#: 剧本刻意安排了两条线：
#:   1. 洋葱的来去  —— 给 `mcdgit blame` 用
#:   2. alice 和 bob 抢同一份鸡腿堡 —— 给 `mcdgit merge` 冲突用
SCRIPT: list[tuple[str, str]] = [
    # --- 本地/远端 ---
    ("$ mcdgit init --store 1990696 --name 麦当劳南京青岛路餐厅",
     'init --store 1990696 --name 麦当劳南京青岛路餐厅'),
    ("$ mcdgit fetch", "fetch"),

    # --- diff ---
    ("", ""),
    ("$ mcdgit add 巨无霸 --mod 吉士=2", "add 巨无霸 --mod 吉士=2"),
    ("$ mcdgit add 中薯条", "add 中薯条"),
    ("$ mcdgit diff", "diff"),
    ('$ mcdgit commit -m "feat: 下午续命"', 'commit -m "feat: 下午续命"'),

    # --- 洋葱的三次人生 ---
    ("", ""),
    ("$ mcdgit add 巨无霸 --mod 洋葱粒=1", "add 巨无霸 --mod 洋葱粒=1"),
    ('$ mcdgit commit -m "feat: 试一口洋葱"', 'commit -m "feat: 试一口洋葱"'),
    ("$ mcdgit add 巨无霸", "add 巨无霸"),
    ('$ mcdgit commit -m "fix: 戒洋葱"', 'commit -m "fix: 戒洋葱"'),
    ("$ mcdgit add 巨无霸 --mod 洋葱粒=1", "add 巨无霸 --mod 洋葱粒=1"),
    ('$ mcdgit commit -m "feat: 复吸"', 'commit -m "feat: 复吸"'),
    ("", ""),
    ("$ mcdgit blame 洋葱粒", "blame 洋葱粒"),

    # --- 团餐冲突 ---
    ("", ""),
    ("$ mcdgit branch alice", "branch alice"),
    ("$ mcdgit checkout alice", "checkout alice"),
    ("$ mcdgit add 麦辣鸡腿堡", "add 麦辣鸡腿堡"),
    ('$ mcdgit commit -m "feat(alice): 这个鸡腿堡是我的"',
     'commit -m "feat(alice): 这个鸡腿堡是我的"'),
    ("$ mcdgit checkout main", "checkout main"),
    ("$ mcdgit add 麦辣鸡腿堡", "add 麦辣鸡腿堡"),
    ('$ mcdgit commit -m "feat(bob): 这个鸡腿堡也是我的"',
     'commit -m "feat(bob): 这个鸡腿堡也是我的"'),
    ("", ""),
    ("$ mcdgit merge alice", "merge alice"),
    ("", ""),
    ("$ mcdgit merge alice --strategy union", "merge alice --strategy union"),

    # --- 图 ---
    ("", ""),
    ("$ mcdgit log --graph --oneline", "log --graph --oneline"),
]


def _sanitize(text: str, repo_dir: Path) -> str:
    """把输出里真实的临时目录换成占位符。

    否则 README 里会出现 ``C:\\Users\\<真名>\\AppData\\Local\\Temp\\tmpkgqr8pq4``
    这种东西——既难看，也等于把跑文档那台机器的信息写了进去。

    Windows 上要同时试两种写法：``TemporaryDirectory`` 给到的是 8.3 短路径
    （``C:\\Users\\YOUMIN~1\\...``），而 CLI 那边 ``.resolve()`` 过，
    打印的是长路径。只替换一种会漏掉。
    """
    variants = [str(repo_dir), str(repo_dir.resolve())]
    cleaned = text.rstrip("\n")
    for variant in variants:
        cleaned = cleaned.replace(variant, "~/麦门")
        cleaned = cleaned.replace(variant.replace("\\", "/"), "~/麦门")
    return cleaned


def main_() -> int:
    parser = argparse.ArgumentParser(description="生成 mcdgit 演示输出")
    parser.add_argument("--no-color", action="store_true", help="关掉颜色（贴文档用）")
    args = parser.parse_args()

    server = MockMcpServer()
    server.__enter__()
    os.environ["MCD_MCP_URL"] = server.url
    os.environ["MCD_MCP_TOKEN"] = "demo-token"
    if args.no_color:
        os.environ["NO_COLOR"] = "1"
    else:
        os.environ.pop("NO_COLOR", None)

    with tempfile.TemporaryDirectory() as tmp:
        repo_dir = Path(tmp)
        # 让演示里有一个"永远不要洋葱"的偏好，diff 才有那行
        (repo_dir / ".mcdgitignore").write_text(
            "# 永不进入提交的配料\n洋葱粒\n", encoding="utf-8"
        )
        for banner, command in SCRIPT:
            if banner:
                print(banner)
            if not command:
                print()
                continue
            argv = ["-C", str(repo_dir), *shlex.split(command)]
            out, err = io.StringIO(), io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    main(argv)
            except SystemExit:
                pass
            body = _sanitize(out.getvalue(), repo_dir)
            if body:
                print(body)
            error = _sanitize(err.getvalue(), repo_dir)
            if error:
                print(error)
            print()

    server.__exit__(None, None, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main_())
