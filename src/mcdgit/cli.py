"""命令行入口。

命令只做三件事：解析参数 → 找仓库 → 调 ``commands/`` 里的处理函数。
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import history, order, setup, worktree
from .context import Context
from .errors import McdGitError
from .model import BE_TYPES
from .render import enable_windows_ansi
from .store import Repo

PROG = "mcdgit"

#: 不需要（也不该）在仓库里执行的命令。
NO_REPO_COMMANDS = frozenset({"init", "stores"})

EPILOG = """\
常用流程：
  mcdgit init --city 南京市 --keyword 鼓楼   # 选一家店
  mcdgit fetch                              # 拉菜单
  mcdgit add 巨无霸 --mod 洋葱=0             # 加进暂存区
  mcdgit diff                               # 看看改了什么
  mcdgit commit -m "feat: 下午续命"          # 下单（还不花钱）
  mcdgit push                               # 扫码支付

对了，`.mcdgitignore` 是真的会生效的——写进去的配料会变成
create-order 请求里的 unselectedKey。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="mcd-git —— 把麦当劳点餐变成版本控制",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    # 注意 dest：init 有一个同名位置参数，用 --directory 会互相覆盖。
    parser.add_argument("-C", "--directory", dest="cwd", help="在指定目录而不是当前目录执行")
    parser.add_argument("--no-color", action="store_true", help="关掉颜色输出")
    parser.add_argument("--token", help="麦当劳 MCP Token（默认读 $MCD_MCP_TOKEN）")
    parser.add_argument("--endpoint", help="MCP 地址（默认 https://mcp.mcd.cn）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印 MCP 请求细节")

    sub = parser.add_subparsers(dest="command", metavar="<command>")

    _add_setup(sub)
    _add_worktree(sub)
    _add_order(sub)
    _add_history(sub)
    return parser


# --------------------------------------------------------------------------- #
# 参数定义
# --------------------------------------------------------------------------- #


def _common_store_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--be-type",
        type=int,
        default=1,
        choices=sorted(BE_TYPES),
        help="1 到店自取 / 2 麦乐送 / 5 得来速 / 6 企业团餐（默认 1）",
    )
    parser.add_argument("--city", help="城市，如 南京市")
    parser.add_argument("--keyword", help="位置关键词，如 鼓楼")
    parser.add_argument("--pick", type=int, help="在第 N 个结果里选（默认自动选第一个）")


def _add_setup(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = sub.add_parser("init", help="初始化仓库并选定门店")
    p.add_argument("directory", nargs="?", help="目标目录（默认当前目录）")
    _common_store_args(p)
    p.add_argument("--store", help="直接指定 storeCode，跳过搜索")
    p.add_argument("--name", help="配合 --store 用的门店名")
    p.add_argument("--be-code", help="配合 --store 用的 beCode（得来速/外送/团餐必填）")
    p.add_argument("--address", help="麦乐送/团餐的 addressId")
    p.add_argument("--author", help="提交作者名")
    p.add_argument("--take-way", help="取餐方式，如 eat-in / locker-in")
    p.add_argument("--force", action="store_true", help="已存在仓库时也重新初始化")
    p.set_defaults(handler=setup.init)

    p = sub.add_parser("remote", help="查看/修改远端门店")
    p.add_argument("action", nargs="?", default="show", choices=["show", "add", "remove"])
    _common_store_args(p)
    p.add_argument("--store")
    p.add_argument("--name")
    p.add_argument("--be-code")
    p.add_argument("--address")
    p.set_defaults(handler=setup.remote)

    p = sub.add_parser("config", help="读写本地配置")
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(handler=setup.config)

    p = sub.add_parser("fetch", help="拉取远端菜单")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(handler=setup.fetch)

    p = sub.add_parser("pull", help="fetch 并同步优惠券到本地")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(handler=setup.pull)

    p = sub.add_parser("menu", help="查看菜单")
    p.add_argument("keyword", nargs="?")
    p.add_argument("--refresh", action="store_true", help="强制重新拉取")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(handler=setup.menu)

    p = sub.add_parser("mods", help="查看某道餐品的可特调项")
    p.add_argument("meal")
    p.set_defaults(handler=setup.mods)

    p = sub.add_parser("stores", help="搜索门店（不需要在仓库里跑）")
    _common_store_args(p)
    p.set_defaults(handler=setup.stores)


def _add_worktree(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = sub.add_parser("status", aliases=["st"], help="查看暂存区")
    p.set_defaults(handler=worktree.status)

    p = sub.add_parser("add", help="把餐品加进暂存区")
    p.add_argument("meals", nargs="+", help="餐品名或 code")
    p.add_argument("-q", "--quantity", type=int, default=1)
    p.add_argument(
        "--mod",
        action="append",
        metavar="NAME=数量",
        help="显式指定配料数量，可重复，如 --mod 培根=2",
    )
    p.add_argument("--all", action="store_true")
    p.set_defaults(handler=worktree.add)

    p = sub.add_parser("rm", help="取消暂存，或从餐品里去掉某个配料")
    p.add_argument("target", help="餐品名，或 配料名，或 配料名=数量")
    p.set_defaults(handler=worktree.rm)

    p = sub.add_parser("diff", help="查看暂存区的特调改动与价格")
    p.add_argument("--stat", action="store_true", help="只看摘要")
    p.set_defaults(handler=worktree.diff)

    p = sub.add_parser("reset", help="取消暂存（不影响本地菜单缓存）")
    p.add_argument("target", nargs="?")
    p.set_defaults(handler=worktree.reset)

    p = sub.add_parser("stash", help="把暂存区先收起来")
    p.add_argument("action", nargs="?", default="push", choices=["push", "pop", "list", "drop", "clear"])
    p.add_argument("-m", "--message")
    p.set_defaults(handler=worktree.stash)

    p = sub.add_parser("branch", aliases=["br"], help="分支管理")
    p.add_argument("name", nargs="?")
    p.add_argument("-d", "--delete")
    p.set_defaults(handler=worktree.branch)

    p = sub.add_parser("checkout", aliases=["co"], help="切换分支")
    p.add_argument("target")
    p.set_defaults(handler=worktree.checkout)

    p = sub.add_parser("tag", help="给提交打标签")
    p.add_argument("name", nargs="?")
    p.add_argument("-d", "--delete")
    p.set_defaults(handler=worktree.tag)


def _add_order(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = sub.add_parser("commit", aliases=["ci"], help="提交订单（调 create-order，但还不花钱）")
    p.add_argument("-m", "--message")
    p.add_argument("-F", "--message-file")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--take-way", help="取餐方式 code，来自 calculate-price 的 takeWayList")
    p.add_argument("--reservation", help="预约时间，格式 yyyy-MM-dd HH:mm")
    p.add_argument("--note", help="订单备注（≤50 字）")
    p.set_defaults(handler=order.commit)

    p = sub.add_parser("push", help="支付（唯一真的花钱的地方）")
    p.add_argument("target", nargs="?")
    p.add_argument("--check", action="store_true", help="顺便查一下支付状态")
    p.set_defaults(handler=order.push)

    p = sub.add_parser("revert", help="退单并回退分支")
    p.add_argument("revision", nargs="?")
    p.add_argument("--reason", help="1 改主意了 / 2 重复下单 / 3 点错了 / 4 地址错了 / 5 时间错了 / -1 其它")
    p.set_defaults(handler=order.revert)

    p = sub.add_parser("cherry-pick", help="把历史某单里的餐品再点一次")
    p.add_argument("revision")
    p.set_defaults(handler=order.cherry_pick)

    p = sub.add_parser("merge", help="合并另一个分支（团餐拼单）")
    p.add_argument("branch")
    p.add_argument("--strategy", choices=["ours", "theirs", "union"], help="冲突解决策略")
    p.add_argument("-m", "--message")
    p.add_argument("--no-commit", action="store_true", help="只放进暂存区")
    p.set_defaults(handler=order.merge)

    p = sub.add_parser("clone", help="复刻一份订单（本地 sha 或别人的订单号）")
    p.add_argument("source")
    p.set_defaults(handler=order.clone)


def _add_history(sub: argparse._SubParsersAction) -> None:  # noqa: SLF001
    p = sub.add_parser("log", aliases=["lg"], help="查看点餐历史")
    p.add_argument("revision", nargs="?")
    p.add_argument("--oneline", action="store_true")
    p.add_argument("--graph", action="store_true")
    p.add_argument("--stat", action="store_true")
    p.add_argument("-n", "--max-count", type=int)
    p.add_argument("--all", action="store_true", help="列出所有提交，不只当前分支")
    p.set_defaults(handler=history.log)

    p = sub.add_parser("show", help="查看某次提交的详情")
    p.add_argument("revision", nargs="?")
    p.set_defaults(handler=history.show)

    p = sub.add_parser("blame", help="追溯一个配料在你历史上的来去")
    p.add_argument("keyword")
    p.add_argument("revision", nargs="?", help="从哪个提交开始追溯（默认 HEAD）")
    p.set_defaults(handler=history.blame)

    p = sub.add_parser("ingredients", help="你的配料史记")
    p.set_defaults(handler=history.ingredients)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    enable_windows_ansi()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "handler", None):
        parser.print_help()
        return 0

    ctx = Context(
        no_color=getattr(args, "no_color", False),
        verbose=getattr(args, "verbose", False),
        token=getattr(args, "token", None),
        endpoint=getattr(args, "endpoint", None) or _env_endpoint(),
    )

    try:
        repo = None
        if args.command not in NO_REPO_COMMANDS:
            repo = Repo.discover(getattr(args, "cwd", None))
        return args.handler(args, ctx, repo)
    except McdGitError as exc:
        print(f"{ctx.p.red('fatal:')} {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n中断。", file=sys.stderr)
        return 130
    finally:
        ctx.close()


def _env_endpoint() -> str | None:
    import os

    return os.environ.get("MCD_MCP_URL")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
