"""工作区命令：status / add / rm / diff / reset / stash / branch / tag / checkout。

这一组命令**全部不发网络请求**（除了 ``diff`` 要算价），
所以离线也能用——这正是"本地 = 工作区"的含义。
"""

from __future__ import annotations

import argparse

from .. import render, services
from ..context import Context
from ..errors import McdGitError
from ..model import StagedItem
from ..store import Repo, now_iso


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def status(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    staged = repo.load_index()
    total = sum(item.subtotal_fen for item in staged)
    print(
        render.render_status(
            ctx.p,
            store=repo.store,
            branch=repo.branch,
            staged=staged,
            staged_total_fen=total,
            head=repo.head_commit(),
            take_way=repo.take_way,
            ignored=services.ignored_names(repo),
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# add
# --------------------------------------------------------------------------- #


def add(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """把餐品放进暂存区。

    **幂等**，和真 ``git add`` 一样：重复 add 同一道餐品会更新它的特调，
    而不是又点一份。想要多份用 ``-q 2``。
    """
    store = repo.require_store()
    index = repo.load_index()
    quantity = args.quantity if args.quantity and args.quantity > 0 else 1

    if args.all:
        raise McdGitError(
            "`mcdgit add --all` 会把整个菜单都加进来（134 份），你是不是想点 `mcdgit menu`？"
        )

    for query in args.meals:
        meal = services.find_meal(repo, ctx, query, store=store)
        services.load_mods(ctx, store, meal)

        # 完整配料快照：默认值 + .mcdgitignore + 命令行 --mod
        mods = services.build_mods(repo, meal)
        for override in args.mod or []:
            name, _, raw = override.partition("=")
            try:
                target = int(raw)
            except ValueError as exc:
                raise McdGitError(f"--mod 需要 NAME=数量 格式，收到「{override}」") from exc
            services.edit_mod(meal, mods, name.strip(), target)

        changes = [m for m in mods if m.changed]
        existing = next((i for i in index if i.meal.code == meal.code), None)
        if existing is not None:
            existing.quantity = quantity
            existing.mods = mods
            print(
                ctx.p.green("✓")
                + f" 已更新 {existing.meal.name} ×{existing.quantity}"
                + ctx.p.dim(f"  {render.yuan(existing.subtotal_fen)} 菜单价")
            )
        else:
            item = StagedItem(meal=meal, quantity=quantity, mods=mods)
            index.append(item)
            extra = ctx.p.yellow(f"  [特调 ×{len(changes)}]") if changes else ""
            print(
                ctx.p.green("✓")
                + f" 已暂存 {item.meal.name} ×{quantity}"
                + ctx.p.dim(f"  {render.yuan(item.subtotal_fen)} 菜单价")
                + extra
            )

        for state in mods:
            if state.ignored:
                print(
                    ctx.p.dim("    ") + ctx.p.red(f"- {state.name}")
                    + ctx.p.dim("  ← .mcdgitignore")
                )

    repo.save_index(index)
    return 0


# --------------------------------------------------------------------------- #
# rm
# --------------------------------------------------------------------------- #


def rm(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """两用：``rm <餐品>`` 取消暂存；``rm <配料>`` 从暂存餐品里去掉配料。"""
    index = repo.load_index()
    if not index:
        raise McdGitError("暂存区是空的，没什么可 rm 的。")

    query = args.target.strip().lower()
    code = ""
    quantity = 0
    if "=" in args.target:
        code, _, raw = args.target.partition("=")
        query = code.strip().lower()
        code = code.strip()
        try:
            quantity = int(raw)
        except ValueError as exc:
            raise McdGitError(f"预期 NAME=数量，收到「{args.target}」") from exc

    # 1) 先把整个餐品撤下来
    for item in list(index):
        if item.meal.code == args.target or item.meal.name.lower() == query:
            index.remove(item)
            repo.save_index(index)
            print(ctx.p.red("✗") + f" 已取消暂存 {item.meal.name}")
            return 0

    # 2) 再当配料处理
    for item in index:
        option = services.find_option(item.meal, args.target)
        if option is None:
            continue
        state = services.edit_mod(item.meal, item.mods, option.name, quantity)
        repo.save_index(index)
        verb = "去掉" if state.qty == 0 else f"改为 {state.qty} 份"
        print(ctx.p.red("✗") + f" {item.meal.name}：{verb} {option.name}")
        return 0

    raise McdGitError(
        f"暂存区里没有「{args.target}」。\n"
        f"        用 `mcdgit status` 看看你暂存了什么，或 `mcdgit mods <餐品>` 看可改的配料。"
    )


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def diff(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    index = repo.load_index()
    if not index:
        print(ctx.p.dim("暂存区是空的。`mcdgit add <餐品>` 先加点东西。"))
        return 0

    store = repo.require_store()
    priced = services.price_items(repo, ctx, store, index)

    total = int(priced.get("price") or sum(i.subtotal_fen for i in index))
    original = int(priced.get("originalPrice") or sum(i.meal.original_price_fen for i in index))
    discount = int(priced.get("discount") or 0)

    rendered = []
    prices = {
        item.meal.code: services.effective_price_fen(priced, item) for item in index
    }
    for item in index:
        rendered.append(
            render.render_diff_item(
                ctx.p,
                item,
                price_fen=prices[item.meal.code],
                show_header=not args.stat,
            )
        )
    if args.stat:
        print(ctx.p.bold(" 餐品改动摘要"))
        print()
        for block in rendered:
            print("\n".join("  " + line for line in block))
        print()
        print(render.rule(ctx.p))
        print(
            ctx.p.dim(
                f" {len(index)} file{'s' if len(index) != 1 else ''} changed, "
                f"{sum(len(i.changes) for i in index)} 项特调"
            )
        )
        print(
            f" 合计 {ctx.p.bold(render.yuan(total))}"
            + (ctx.p.dim(f"（原价 {render.yuan(original)}）") if original != total else "")
            + (ctx.p.green(f"  优惠 -{render.yuan(discount)}") if discount else "")
        )
        return 0

    print(
        render.render_diff(
            ctx.p,
            index,
            prices=prices,
            total_fen=total,
            original_fen=original,
            discount_fen=discount,
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# reset / stash
# --------------------------------------------------------------------------- #


def reset(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    index = repo.load_index()
    if args.target:
        keep = [i for i in index if i.meal.name != args.target and i.meal.code != args.target]
        if len(keep) == len(index):
            raise McdGitError(f"暂存区里没有「{args.target}」")
        repo.save_index(keep)
        print(ctx.p.red("✗") + f" 已取消暂存 {args.target}")
        return 0
    if not index:
        print(ctx.p.dim("暂存区本来就是空的。"))
        return 0
    repo.clear_index()
    print(ctx.p.red("✗") + f" 已清空暂存区（{len(index)} 项）" + ctx.p.dim("  还没点呢，不花钱。"))
    return 0


def stash(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    action = args.action or "push"
    entries = repo.load_stash()

    if action == "list":
        if not entries:
            print(ctx.p.dim("stash 是空的。"))
            return 0
        for index, entry in enumerate(entries):
            count = len(entry.get("items", []))
            print(
                f"{ctx.p.yellow(f'stash@{{{index}}}')}: "
                f"{entry.get('message', 'WIP')} "
                + ctx.p.dim(f"({count} 项, {entry.get('at', '')})")
            )
        return 0

    if action == "push":
        index = repo.load_index()
        if not index:
            raise McdGitError("暂存区是空的，没东西可 stash。")
        entries.insert(
            0,
            {
                "message": args.message or f"WIP on {repo.branch}",
                "at": now_iso(),
                "items": [i.to_dict() for i in index],
            },
        )
        repo.save_stash(entries)
        repo.clear_index()
        print(
            ctx.p.bold(f"Saved working directory and index state {ctx.p.yellow('stash@{0}')}")
        )
        print(ctx.p.dim(f"  {len(index)} 项已收好，等你想吃了再说。"))
        return 0

    if action == "pop":
        if not entries:
            raise McdGitError("stash 是空的，弹不出东西。")
        entry = entries.pop(0)
        restored = [StagedItem.from_dict(d) for d in entry.get("items", [])]
        repo.save_stash(entries)
        repo.save_index(restored)
        print(ctx.p.bold(f"On branch {repo.branch}") + "  " + entry.get("message", ""))
        for item in restored:
            print(ctx.p.green("+") + f" {item.meal.name} ×{item.quantity}")
        return 0

    if action == "drop":
        if not entries:
            raise McdGitError("stash 是空的。")
        entry = entries.pop(0)
        repo.save_stash(entries)
        print(f"Dropped stash@{{0}} ({entry.get('message', '')})")
        return 0

    if action == "clear":
        repo.save_stash([])
        print("stash 已清空。")
        return 0

    raise McdGitError(f"未知的 stash 子命令：{action}")


# --------------------------------------------------------------------------- #
# branch / checkout / tag
# --------------------------------------------------------------------------- #


def branch(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    if args.delete:
        if args.delete == repo.branch:
            raise McdGitError("不能删掉当前所在的分支。")
        path = repo.git_dir / "refs" / "heads" / args.delete
        if not path.is_file():
            raise McdGitError(f"没有名为 '{args.delete}' 的分支。")
        path.unlink()
        print(f"Deleted branch {args.delete}")
        return 0

    if args.name:
        repo.create_branch(args.name)
        print(ctx.p.green("✓") + f" 已创建分支 {ctx.p.bold(args.name)}")
        print(ctx.p.dim(f"  换个口味？`mcdgit checkout {args.name}`"))
        return 0

    for name, sha in repo.branches().items():
        marker = ctx.p.green("*") if name == repo.branch else " "
        commit = repo.load_commit(sha) if sha else None
        subject = ctx.p.dim(f"  {commit.subject}") if commit else ctx.p.dim("  (空分支)")
        print(f"{marker} {render.pad(name, 20)} {ctx.p.yellow(sha or '-')}{subject}")
    return 0


def checkout(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """切分支。

    允许切到 HEAD 已指向、但还没有任何提交的分支。刚 init 完的仓库里
    refs/heads/main 并不存在（真 git 也是这个行为，叫 "unborn branch"），
    这里如果严格查 refs，新仓库里一条 `checkout main` 就会直接失败。
    """
    if args.target != repo.branch and args.target not in repo.branches():
        raise McdGitError(
            f"没有名为 '{args.target}' 的分支。\n"
            f"        `mcdgit branch {args.target}` 新建一个，或 `mcdgit branch` 看现有的。"
        )
    repo.set_branch(args.target)
    print(ctx.p.bold(f"Switched to branch '{args.target}'"))
    return 0


def tag(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    if args.delete:
        path = repo.git_dir / "refs" / "tags" / args.delete
        if not path.is_file():
            raise McdGitError(f"没有标签 '{args.delete}'")
        path.unlink()
        print(f"Deleted tag '{args.delete}'")
        return 0

    if args.name:
        sha = repo.head_sha()
        if not sha:
            raise McdGitError("还没有提交，给谁打标签？")
        repo.write_ref(f"refs/tags/{args.name}", sha)
        print(ctx.p.green("✓") + f" 已给 {ctx.p.yellow(sha)} 打上标签 {ctx.p.bold(args.name)}")
        return 0

    tags = repo.tags()
    if not tags:
        print(ctx.p.dim("还没有任何标签。`mcdgit tag v1.0` 给当前提交打一个。"))
        return 0
    for name, sha in tags.items():
        print(f"{ctx.p.bold(name)}\t{ctx.p.yellow(sha)}")
    return 0
