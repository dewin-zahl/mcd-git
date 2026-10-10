"""历史命令：log / show / blame。"""

from __future__ import annotations

import argparse
from .. import render, services
from ..context import Context
from ..errors import McdGitError
from ..model import Commit, ModState
from ..store import Repo


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #


def log(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    commits = repo.all_commits() if args.all else repo.history(args.revision)
    if args.max_count:
        commits = commits[: args.max_count]
    print(
        render.render_log(
            ctx.p,
            commits,
            oneline=args.oneline,
            graph=args.graph,
            stat=args.stat,
            branch=repo.branch,
            tags=repo.tag_map(),
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #


def show(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    sha = repo.resolve(args.revision or "HEAD")
    commit = repo.require_commit(sha)
    tags = repo.tag_map().get(sha, [])

    lines = [
        ctx.p.yellow(f"commit {commit.sha}")
        + (ctx.p.cyan(f" ({', '.join('tag: ' + t for t in tags)})") if tags else ""),
        f"OrderId: {ctx.p.bold(commit.order_id)}",
        f"Author:  {commit.author}",
        f"Date:    {commit.committed_at}",
        "",
    ]
    for text in commit.message.splitlines():
        lines.append(f"    {text}")
    lines.append("")
    lines.append(render.rule(ctx.p))

    for item in commit.items:
        price = render.yuan(item.subtotal_fen)
        lines.append(
            f" {ctx.p.green('+')} {render.pad(item.name, 26)}"
            f"{ctx.p.dim('×' + str(item.quantity))}  {price}"
        )
        for state in item.mods:
            if not state.changed:
                continue
            sym = ctx.p.red("-") if state.qty == 0 else ctx.p.green("+")
            note = ctx.p.dim("   ← .mcdgitignore") if state.ignored else ""
            lines.append(f"     {sym} {state.spec}{note}")
    lines.append("")
    lines.append(
        render.status_line(ctx.p, "合计", ctx.p.bold(render.yuan(commit.total_fen)))
        + (ctx.p.green(f"  优惠 -{render.yuan(commit.discount_fen)}") if commit.discount_fen else "")
    )
    if commit.take_way:
        lines.append(render.status_line(ctx.p, "取餐方式", commit.take_way))
    if commit.store:
        lines.append(
            render.status_line(
                ctx.p, "远端", commit.store.get("store_name") or commit.store.get("store_code", "")
            )
        )
    if commit.parents:
        lines.append(render.status_line(ctx.p, "parent", ", ".join(commit.parents)))
    print("\n".join(lines))
    return 0


# --------------------------------------------------------------------------- #
# blame
# --------------------------------------------------------------------------- #


def blame(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """追溯一个配料在你的历史上每一次来和去。

    和真 git 的 ``blame`` 有个有趣的差别：真 git blame 的是"谁写了这一行"，
    mcdgit blame 的是"这一行**什么时候又跑回来了**"。因为作者永远是你自己——
    这才是这个功能在麦门语境下真正能提供的价值。
    """
    keyword = args.keyword.strip()
    # 一定要用 history() 而不是 all_commits()：blame 只该沿 HEAD 的祖先链走，
    # 而且必须按提交图排序。同一秒内产生的多条提交如果只按时间戳排，
    # 顺序是随机的，"引入/移除"会讲成假故事。
    commits = list(reversed(repo.history(args.revision)))
    if not commits:
        print(ctx.p.dim("还没有任何提交，无从 blame。"))
        return 0

    events: list[tuple[Commit, str]] = []
    ever_present = False
    present = False
    last_qty = 0

    for commit in commits:
        qty = _quantity_of(commit, keyword)
        if qty > 0 and not present:
            action = "加回" if ever_present else "引入"
            events.append((commit, action))
            ever_present = True
        elif qty == 0 and present:
            events.append((commit, "移除"))
        elif qty > last_qty and present:
            events.append((commit, "加量"))
        present = qty > 0
        last_qty = qty

    print(
        render.render_blame(
            ctx.p, keyword, events, total_commits=len(commits)
        )
    )
    return 0


def _quantity_of(commit: Commit, keyword: str) -> int:
    """这份订单里该配料的总数量。默认带上的也算——这就是快照的意义。"""
    needle = keyword.lower()
    total = 0
    for item in commit.items:
        for state in item.mods:
            if _matches(state, needle):
                total += state.qty
    return total


def _matches(state: ModState, needle: str) -> bool:
    return needle in state.name.lower() or needle == state.code.lower()


# --------------------------------------------------------------------------- #
# 附：配料一览
# --------------------------------------------------------------------------- #


def ingredients(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """列出你历史上出现过的所有配料，按出现次数排序。"""
    commits = repo.all_commits()
    if not commits:
        print(ctx.p.dim("还没有任何提交。"))
        return 0

    counts: dict[str, tuple[int, int]] = {}
    for commit in commits:
        for item in commit.items:
            for state in item.mods:
                total, times = counts.get(state.name, (0, 0))
                counts[state.name] = (total + state.qty, times + 1)

    print(ctx.p.bold("你的配料史记") + ctx.p.dim(f"  共 {len(commits)} 次提交"))
    print()
    for name, (total, times) in sorted(counts.items(), key=lambda kv: -kv[1][0]):
        bar = "▇" * min(30, total)
        flag = ctx.p.red(" ← 忽略中") if repo.is_ignored(name) else ""
        print(
            f"  {render.pad(name, 16)} {ctx.p.green(render.pad(bar, 31))}"
            f"{ctx.p.dim(f'{total:>3} 份 / {times} 单')}{flag}"
        )
    return 0
