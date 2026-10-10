"""仓库初始化与远端配置：init / remote / config / fetch / pull / menu / mods / stores。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .. import render, services
from ..context import Context
from ..errors import McdGitError
from ..model import BE_TYPES, Store
from ..store import Repo


# --------------------------------------------------------------------------- #
# init
# --------------------------------------------------------------------------- #


def init(args: argparse.Namespace, ctx: Context, repo: Repo | None) -> int:
    # init 的位置参数 directory 优先；其次是全局 -C；最后才是当前目录。
    target = Path(args.directory or getattr(args, "cwd", None) or ".").resolve()
    if (target / ".mcdgit").is_dir() and not args.force:
        repo = Repo(target)
        print(ctx.p.dim(f"Reinitialized existing mcdgit repository in {repo.git_dir}"))
    else:
        repo = Repo.init(target)
        print(ctx.p.dim(f"Initialized empty mcdgit repository in {repo.git_dir}"))

    store = _resolve_store(args, ctx)
    repo.set_store(store)
    if args.author:
        config = repo.load_config()
        config["author"] = args.author
        repo.save_config(config)
    if args.take_way:
        repo.take_way = args.take_way

    print()
    print(render.status_line(ctx.p, "remote origin", store.label))
    print(render.status_line(ctx.p, "storeCode", store.store_code))
    if store.be_code:
        print(render.status_line(ctx.p, "beCode", store.be_code))
    print(render.status_line(ctx.p, "orderType", str(store.order_type)))
    print(render.status_line(ctx.p, "beType", f"{store.be_type} ({BE_TYPES[store.be_type]})"))
    print()
    print(ctx.p.dim("  下一步：mcdgit fetch    # 拉菜单"))
    return 0


def _resolve_store(args: argparse.Namespace, ctx: Context) -> Store:
    if args.store:
        return Store(
            store_code=args.store,
            store_name=args.name or args.store,
            be_type=args.be_type,
            be_code=args.be_code or "",
            address_id=args.address or "",
        )

    if args.be_type in (2, 6):
        return _resolve_delivery_store(args, ctx)

    stores = services.search_stores(
        ctx,
        be_type=args.be_type,
        city=args.city or "",
        keyword=args.keyword or "",
        favorites=not (args.city or args.keyword),
    )
    if not stores:
        raise McdGitError("没找到门店。加 --city / --keyword 指定位置再试。")
    if len(stores) == 1:
        return stores[0]

    print(render.render_stores(ctx.p, stores, args.keyword or ""))
    if not args.pick:
        raise McdGitError(
            f"找到 {len(stores)} 家门店，用 --pick <序号> 选一家。\n"
            f"        例如：mcdgit init --be-type {args.be_type} --city {args.city or '南京市'} "
            f"--keyword {args.keyword or '鼓楼'} --pick 1"
        )
    index = args.pick - 1
    if not 0 <= index < len(stores):
        raise McdGitError(f"--pick {args.pick} 超出范围（1-{len(stores)}）")
    return stores[index]


def _resolve_delivery_store(args: argparse.Namespace, ctx: Context) -> Store:
    """麦乐送/团餐要先把地址定下来，再问这个地址能送到哪些店。"""
    addresses = services.as_list(ctx.call("delivery-query-addresses"))
    if not addresses:
        raise McdGitError(
            "这个账号还没有配送地址。\n"
            "        先去麦当劳 App 加一个，或让 Skill 调 delivery-create-address。"
        )
    pickup = args.pick or 1
    if not 1 <= pickup <= len(addresses):
        raise McdGitError(f"--pick {pickup} 超出地址范围（1-{len(addresses)}）")
    address = addresses[pickup - 1]
    address_id = str(address.get("addressId") or address.get("id") or "")

    stores = services.as_list(
        ctx.call("delivery-query-stores", beType=args.be_type, addressId=address_id)
    )
    if not stores:
        raise McdGitError("该地址没有可配送门店。")
    chosen = stores[0]
    return Store(
        store_code=str(chosen.get("storeCode", "")),
        store_name=chosen.get("storeName", ""),
        address=chosen.get("address", ""),
        be_type=args.be_type,
        be_code=str(chosen.get("beCode") or ""),
        address_id=address_id,
    )


# --------------------------------------------------------------------------- #
# remote / config
# --------------------------------------------------------------------------- #


def remote(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    action = args.action or "show"

    if action == "show":
        store = repo.store
        if store is None:
            print(ctx.p.dim("还没有配置远端门店。`mcdgit remote add` 加一个。"))
            return 0
        print(
            f"{ctx.p.bold('origin')}\t"
            f"{ctx.p.yellow('mcd://store/' + store.store_code)}\t"
            f"{ctx.p.dim('(fetch)')}"
        )
        print(
            f"{ctx.p.bold('origin')}\t"
            f"{ctx.p.yellow('mcd://store/' + store.store_code)}\t"
            f"{ctx.p.dim('(push)')}"
        )
        print()
        print(render.status_line(ctx.p, "门店", store.label))
        print(render.status_line(ctx.p, "beCode", store.be_code or ctx.p.dim("(到店自取不需要)")))
        print(render.status_line(ctx.p, "orderType", str(store.order_type)))
        return 0

    if action == "add":
        store = _resolve_store(args, ctx)
        repo.set_store(store)
        print(ctx.p.green("✓") + " 远端已设置为 " + ctx.p.bold(store.label))
        return 0

    if action == "remove":
        config = repo.load_config()
        config.pop("store", None)
        repo.save_config(config)
        print("已移除远端门店。")
        return 0

    raise McdGitError(f"未知的 remote 子命令：{action}")


def config(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    if not args.key:
        data = repo.load_config()
        if not data:
            print(ctx.p.dim("（空）"))
            return 0
        for key, value in sorted(data.items()):
            print(f"{ctx.p.bold(key)}={value if not isinstance(value, dict) else ''}")
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    print(f"  {sub_key}={sub_value}")
        return 0

    if args.value is None:
        config = repo.load_config()
        if args.key not in config:
            raise McdGitError(f"没有配置项 '{args.key}'")
        print(config[args.key])
        return 0

    config = repo.load_config()
    if args.key == "author":
        config["author"] = args.value
    elif args.key in ("take_way", "core.takeway"):
        repo.take_way = args.value
        print(f"取餐方式已设为 {ctx.p.bold(args.value)}")
        return 0
    else:
        config[args.key] = args.value
    repo.save_config(config)
    print(f"{args.key}={args.value}")
    return 0


# --------------------------------------------------------------------------- #
# fetch / pull
# --------------------------------------------------------------------------- #


def fetch(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    store = repo.require_store()
    print(ctx.p.dim(f"从 {store.label} 拉取远端..."))
    meals = services.fetch_menu(repo, ctx, store)

    lines = [
        ctx.p.bold("远端更新") + "  " + ctx.p.dim(store.label),
        render.status_line(ctx.p, "在售餐品", str(len(meals))),
    ]
    if not args.quiet:
        cheapest = min(meals.values(), key=lambda m: m.price_fen or 10**9)
        lines.append(
            render.status_line(
                ctx.p, "最便宜", f"{cheapest.name} {render.yuan(cheapest.price_fen)}"
            )
        )
    print("\n".join(lines))
    return 0


def pull(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    """fetch + 领券。"同步远端"在麦门语境里就是"把券同步到本地"。"""
    fetch(args, ctx, repo)
    print()
    try:
        result = services.bind_coupons(ctx)
    except McdGitError as exc:
        print(ctx.p.yellow("⚠") + f" 领券失败（不影响其它操作）：{exc}")
        return 0
    count = len(result) if isinstance(result, list) else 0
    print(ctx.p.bold("领取优惠券") + "  " + ctx.p.green(f"+{count} 张"))
    if count:
        for coupon in (result or [])[:8]:
            if isinstance(coupon, dict):
                print("  " + ctx.p.green("+") + " " + str(coupon.get("couponName") or coupon.get("name") or coupon))
    return 0


# --------------------------------------------------------------------------- #
# menu / mods / stores
# --------------------------------------------------------------------------- #


def menu(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    meals = repo.cached_meals()
    if not meals or args.refresh:
        meals = services.fetch_menu(repo, ctx, repo.require_store())
    print(render.render_menu(ctx.p, meals.values(), args.keyword or "", limit=args.limit))
    return 0


def mods(args: argparse.Namespace, ctx: Context, repo: Repo) -> int:
    store = repo.require_store()
    meal = services.find_meal(repo, ctx, args.meal, store=store)
    services.load_mods(ctx, store, meal)
    print(render.render_mod_options(ctx.p, meal))
    return 0


def stores(args: argparse.Namespace, ctx: Context, repo: Repo | None) -> int:
    found = services.search_stores(
        ctx,
        be_type=args.be_type,
        city=args.city or "",
        keyword=args.keyword or "",
        favorites=not (args.city or args.keyword),
    )
    print(render.render_stores(ctx.p, found, args.keyword or ""))
    return 0
