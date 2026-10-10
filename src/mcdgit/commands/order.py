"""订单命令：commit / push / revert / cherry-pick / merge / clone。

这四个命令是整个项目的"远端"——只有它们会真的影响现实世界。
"""

from __future__ import annotations

import argparse
from typing import Any

from .. import render, services
from ..context import Context
from ..errors import McdGitError, MergeConflict, NothingToCommitError
from ..model import Commit, CommitItem, Meal, ModState, StagedItem
from ..store import Repo, now_iso


# --------------------------------------------------------------------------- #
# commit
# --------------------------------------------------------------------------- #


def commit(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    index = repo.load_index()
    if not index and not args.allow_empty:
        raise NothingToCommitError()

    store = repo.require_store()
    message = _message(args, index)

    print(ctx.p.dim(f"正在向 {store.label} 提交订单..."))
    commit_obj = services.place_order(
        repo,
        ctx,
        store,
        index,
        message,
        take_way=args.take_way,
        reservation=args.reservation,
        note=args.note or "",
    )

    insertions, deletions = services.summarize_changes(index)
    print()
    print(
        render.render_commit_result(
            ctx.p,
            commit_obj,
            branch=repo.branch,
            files=len(index),
            insertions=insertions,
            deletions=deletions,
        )
    )
    repo.clear_index()
    print()
    print(
        ctx.p.dim("  订单已创建，但**还没付钱**。")
        + " `mcdgit push` 去支付。"
    )
    return 0


def _message(args: argparse.Namespace, index: list[StagedItem]) -> str:
    if args.message:
        return args.message
    if args.message_file:
        try:
            with open(args.message_file, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError as exc:
            raise McdGitError(f"读不到 {args.message_file}：{exc}") from exc
    names = "、".join(item.meal.name for item in index[:3])
    suffix = " 等" if len(index) > 3 else ""
    return f"feat: 点了 {names}{suffix}"


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #


def push(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    head = repo.head_commit()
    if head is None:
        raise McdGitError("还没有提交可以 push。`mcdgit commit` 先。")

    target = head
    if args.target:
        sha = repo.resolve(args.target)
        loaded = repo.load_commit(sha)
        if loaded is None:
            raise McdGitError(f"找不到提交 {args.target}")
        target = loaded

    print(ctx.p.dim(f"推送 {target.sha} 到 origin..."))
    url = services.payment_url(ctx, target.order_id)

    print()
    print(ctx.p.bold("To mcd://store/" + str(target.store.get("store_code", "?"))))
    print(f"   {ctx.p.yellow(target.sha)}  {target.subject}")
    print()
    print(render.status_line(ctx.p, "订单号", ctx.p.bold(target.order_id)))
    print(render.status_line(ctx.p, "金额", ctx.p.bold(render.yuan(target.total_fen))))
    if target.note:
        print(render.status_line(ctx.p, "备注", target.note))
    print()

    if url:
        print(ctx.p.green("支付链接：") + ctx.p.cyan(url))
    else:
        print(ctx.p.yellow("⚠") + " 没有从接口拿到支付链接。")
        print(ctx.p.dim("  请打开麦当劳 App → 我的订单 → 找到上面这个订单号完成支付。"))
    print()
    print(ctx.p.dim("  支付完成后跑 `mcdgit fetch --status` 确认。—— 远端的事，得等远端说了算。"))
    return 0


# --------------------------------------------------------------------------- #
# revert
# --------------------------------------------------------------------------- #


def revert(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """退单 + 回退分支。

    这里和真 git 有个**有意的差异**，写下来免得被当成 bug：

    真 git 的 ``revert`` 会创建一个"反向提交"。但麦当劳不会因为你退单
    就给你一个新的反向订单号，而我们这个项目的 commit hash 就是订单号——
    没有新订单号就没有新提交。所以 ``mcdgit revert`` 的语义是
    **退掉这份订单，并把分支指针退回它的父提交**。
    """
    sha = repo.resolve(args.revision or "HEAD")
    commit = repo.require_commit(sha)

    reason = args.reason or "1"
    print(ctx.p.dim(f"正在退单 {commit.order_id} ..."))
    services.cancel(ctx, commit.order_id, reason)
    print(ctx.p.green("✓") + f" 订单 {commit.order_id} 已取消")

    if repo.head_sha() == sha:
        parent = commit.parents[0] if commit.parents else None
        if parent:
            repo.write_ref(repo.head_ref(), parent)
            print(
                ctx.p.bold(f"{repo.branch} -> {parent}")
                + ctx.p.dim(f"  (was {sha})")
            )
        else:
            (repo.git_dir / "refs" / "heads" / repo.branch).unlink(missing_ok=True)
            print(ctx.p.dim("分支已退回空状态（这是第一次提交）。"))
    return 0


# --------------------------------------------------------------------------- #
# cherry-pick
# --------------------------------------------------------------------------- #


def cherry_pick(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """把历史提交里的餐品重新放回暂存区。"""
    sha = repo.resolve(args.revision)
    commit = repo.require_commit(sha)
    index = repo.load_index()

    added = 0
    for item in commit.items:
        existing = next((i for i in index if i.meal.code == item.product_code), None)
        if existing is not None:
            existing.quantity += item.quantity
            added += 1
            continue
        index.append(
            StagedItem(
                meal=Meal(
                    code=item.product_code,
                    name=item.name,
                    price_fen=item.unit_price_fen,
                ),
                quantity=item.quantity,
                mods=list(item.mods),
            )
        )
        added += 1

    repo.save_index(index)
    print(
        ctx.p.bold(f"[{ctx.p.yellow(sha)}] {commit.subject}")
        + ctx.p.dim(f"  → 拣选了 {added} 项到暂存区")
    )
    for item in commit.items:
        print(ctx.p.green("+") + f" {item.name}" + ctx.p.dim(f" ×{item.quantity}"))
    print()
    print(ctx.p.dim("  `mcdgit commit` 提交这一单。"))
    return 0


# --------------------------------------------------------------------------- #
# merge（团餐拼单）
# --------------------------------------------------------------------------- #


def merge(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """把另一个分支的订单合到当前分支——在麦门语境里这就是"团餐拼单"。"""
    other_name = args.branch
    if other_name not in repo.branches():
        raise McdGitError(f"没有名为 '{other_name}' 的分支。")

    mine = repo.head_commit()
    theirs = repo.load_commit(repo.branches()[other_name])
    if mine is None or theirs is None:
        raise McdGitError("两个分支都必须至少有一次提交才能 merge。")

    strategy = args.strategy or ""
    merged, conflicts = _merge_items(mine.items, theirs.items, strategy)

    if conflicts:
        raise MergeConflict(conflicts[0][0], conflicts[0][1])

    store = repo.require_store()
    if store.be_type != 6:
        print(
            ctx.p.yellow("⚠")
            + f" 当前远端的 beType={store.be_type}（{render_be_type(store.be_type)}），"
            "不是团餐(6)。"
        )
        print(ctx.p.dim("  合并出来的结果会被放进暂存区，但下单走的仍是当前门店类型。"))

    index = [
        StagedItem(
            meal=_meal_from_item(item), quantity=item.quantity, mods=list(item.mods)
        )
        for item in merged
    ]
    repo.save_index(index)

    if args.no_commit:
        print(ctx.p.bold(f"Merge made by the '{strategy or 'union'}' strategy."))
        for item in merged:
            print(ctx.p.green("+") + f" {item.name}" + ctx.p.dim(f" ×{item.quantity}"))
        print()
        print(ctx.p.dim("  已放入暂存区。`mcdgit commit -m \"merge: ...\"` 提交这一单。"))
        return 0

    message = args.message or f"merge: {repo.branch} ← {other_name}"
    commit_obj = services.place_order(repo, ctx, store, index, message)
    # 真正的 merge commit：两个 parent
    commit_obj.parents = [mine.sha, theirs.sha]
    repo.save_commit(commit_obj)
    repo.write_ref(repo.head_ref(), commit_obj.sha)
    repo.clear_index()

    insertions, deletions = services.summarize_changes(index)
    print(
        render.render_commit_result(
            ctx.p,
            commit_obj,
            branch=repo.branch,
            files=len(index),
            insertions=insertions,
            deletions=deletions,
        )
    )
    print()
    print(ctx.p.dim(f"  merge commit 有两个 parent：{mine.sha} 和 {theirs.sha}"))
    return 0


def render_be_type(be_type: int) -> str:
    from ..model import BE_TYPES

    return BE_TYPES.get(be_type, "未知")


def _meal_from_item(item: CommitItem) -> Meal:
    """把提交里的餐品还原成一道可再次下单的 `Meal`。"""
    return Meal(code=item.product_code, name=item.name, price_fen=item.unit_price_fen)


def _merge_items(
    mine: list[CommitItem], theirs: list[CommitItem], strategy: str
) -> tuple[list[CommitItem], list[tuple[str, list[str]]]]:
    """三方合并的极简版：按 productCode 求并集，重叠即冲突。"""
    mine_by_code = {item.product_code: item for item in mine}
    theirs_by_code = {item.product_code: item for item in theirs}
    overlap = sorted(set(mine_by_code) & set(theirs_by_code))

    if overlap and not strategy:
        name = mine_by_code[overlap[0]].name
        return [], [(name, ["alice", "bob"])]

    merged: list[CommitItem] = []
    for code, item in mine_by_code.items():
        if code in overlap and strategy == "theirs":
            continue
        merged.append(item)
    for code, item in theirs_by_code.items():
        if code in overlap:
            if strategy == "ours":
                continue
            if strategy == "union":
                current = mine_by_code[code]
                current.quantity += item.quantity
                continue
        merged.append(item)
    return merged, []


# --------------------------------------------------------------------------- #
# clone
# --------------------------------------------------------------------------- #


def clone(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """复刻一份订单到暂存区。

    支持两种来源：本地已有的 sha，或者**别人给你的订单号**。
    后者是传播机制——把订单号发出去，对方一条命令就能复刻。
    """
    source = args.source
    items: list[CommitItem] = []

    try:
        sha = repo.resolve(source)
        commit = repo.require_commit(sha)
        items = list(commit.items)
        label = f"{sha} {commit.subject}"
    except McdGitError:
        # 本地没有这条提交，就当作"别人发给你的订单号"
        items = _items_from_order(ctx, repo, source)
        label = f"order {source}"

    if not items:
        raise McdGitError(
            f"复刻失败：本地没有 `{source}` 这条提交，也没能把它当订单号取回来。"
        )

    index = repo.load_index()
    for item in items:
        existing = next((i for i in index if i.meal.code == item.product_code), None)
        if existing is not None:
            existing.quantity += item.quantity
            continue
        index.append(
            StagedItem(
                meal=_meal_from_item(item), quantity=item.quantity, mods=list(item.mods)
            )
        )
    repo.save_index(index)

    print(ctx.p.bold(f"Cloning into '{repo.branch}'...") + ctx.p.dim(f"  from {label}"))
    for item in items:
        print(ctx.p.green("+") + f" {item.name}" + ctx.p.dim(f" ×{item.quantity}"))
    print()
    print(ctx.p.dim("  已复刻到暂存区。`mcdgit diff` 看看，`mcdgit commit` 下单。"))
    return 0


def _items_from_order(ctx: Context, repo: Repo, order_id: str) -> list[CommitItem]:
    """从一份麦当劳订单里反解出餐品列表。

    字段名各接口不统一，所以广撒网；名字缺失时回落到**本地菜单缓存**——
    订单详情不保证带 `productName`，只拿得到 `productCode` 的话，
    复刻出来就会显示成 "1001 ×2"（这个坑被测试抓到过一次）。

    特调同样是尽力而为：详情里的 `modification.values[]` 带着 code/key/quantity，
    但没有配料名。如果本地缓存里还有这道餐品，就顺手拉一次 detail 把名字补齐，
    这样复刻出来的提交还能被 `mcdgit blame` 追溯。拉不到就算了，不报错。
    """
    try:
        detail = services.order_status(ctx, order_id)
    except McdGitError:
        return []
    if not detail:
        return []

    raw_items = None
    for key in ("productList", "items", "orderItems", "productInfos"):
        value = detail.get(key)
        if isinstance(value, list) and value:
            raw_items = value
            break
    if raw_items is None:
        return []

    cache = repo.cached_meals()
    items: list[CommitItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("productCode") or raw.get("code") or "")
        name = str(raw.get("productName") or raw.get("name") or "")
        cached = cache.get(code)
        if not name:
            name = cached.name if cached else code
        quantity = int(raw.get("quantity") or 1)
        unit = int(raw.get("price") or raw.get("unitPrice") or 0)
        if not unit and cached:
            unit = cached.price_fen

        items.append(
            CommitItem(
                product_code=code,
                name=name,
                quantity=quantity,
                unit_price_fen=unit,
                subtotal_fen=unit * quantity,
                mods=_mods_from_order(ctx, repo, raw, cached),
            )
        )
    return items


def _mods_from_order(
    ctx: Context, repo: Repo, raw: dict[str, Any], cached: Meal | None
) -> list[ModState]:
    """把订单里的 modification 反解回配料快照（尽力而为）。"""
    entry = raw.get("modification")
    if not isinstance(entry, dict):
        return []
    values = [v for v in (entry.get("values") or []) if isinstance(v, dict)]
    if not values:
        return []

    option_names: dict[str, tuple[str, int]] = {}
    if cached is not None:
        try:
            store = repo.require_store()
            meal = services.load_mods(ctx, store, cached)
            option_names = {o.code: (o.name, o.selected) for o in meal.mods}
        except McdGitError:
            option_names = {}

    mods = []
    for value in values:
        code = str(value.get("code") or "")
        qty = int(value.get("quantity") or 0)
        found = option_names.get(code)
        name = found[0] if found else code
        # 拿不到菜单默认值时，就把当前数量当作默认——于是 clone 出来是"干净工作区"，
        # 不会凭空多出一堆 diff。而 key/quantity 仍然是原样发出去的。
        default_qty = found[1] if found else qty
        key = str(value.get("key") or "")
        group = 0
        if "-" in key:
            head = key.split("-", 1)[0]
            if head.isdigit():
                group = int(head)
        mods.append(
            ModState(
                code=code,
                name=name,
                qty=qty,
                default_qty=default_qty,
                key=key,
                group=group,
            )
        )
    return mods
