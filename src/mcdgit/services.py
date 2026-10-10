"""服务层 —— 所有 MCP 交互都收在这里，外加"配料快照"的构造。

命令模块只负责"编排 + 渲染"，不直接拼 MCP 参数。
这样参数规则（``beCode`` 该不该传、``unselectedKey`` 怎么回传）只有一处实现，
改的时候不会漏。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .context import Context
from .errors import McdGitError
from .model import Commit, Meal, ModOption, ModState, StagedItem, Store
from .store import Repo, now_iso


# --------------------------------------------------------------------------- #
# 门店
# --------------------------------------------------------------------------- #


def search_stores(
    ctx: Context,
    *,
    be_type: int = 1,
    city: str = "",
    keyword: str = "",
    favorites: bool = True,
) -> list[Store]:
    """找门店。先试收藏，收藏为空自动退回关键词搜索。"""
    if favorites and not city:
        try:
            raw = ctx.call("query-nearby-stores", beType=be_type, searchType=1)
            stores = [_store_from(item, be_type) for item in as_list(raw)]
            if stores:
                return stores
        except McdGitError:
            pass  # 收藏为空是常态，不是错误

    if not city or not keyword:
        raise McdGitError(
            "收藏列表为空，需要城市和关键词才能搜索。\n"
            "        例如：mcdgit init --city 南京市 --keyword 鼓楼"
        )
    raw = ctx.call(
        "query-nearby-stores", beType=be_type, searchType=2, city=city, keyword=keyword
    )
    return [_store_from(item, be_type) for item in as_list(raw)]


def _store_from(data: dict[str, Any], be_type: int) -> Store:
    return Store(
        store_code=str(data.get("storeCode", "")),
        store_name=data.get("storeName", ""),
        address=data.get("address", ""),
        be_type=be_type,
        be_code=str(data.get("beCode") or ""),
    )


def as_list(payload: Any) -> list[dict[str, Any]]:
    """把各种形状的 MCP 返回值统一成 list[dict]。"""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("data", "list", "stores", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


# --------------------------------------------------------------------------- #
# 菜单
# --------------------------------------------------------------------------- #


def fetch_menu(repo: Repo, ctx: Context, store: Store) -> dict[str, Meal]:
    """拉菜单并写进 ``.mcdgit/cache.json``。"""
    payload = ctx.call("query-meals", **store.mcp_args())
    meals_raw = payload.get("meals", {}) if isinstance(payload, dict) else {}
    categories = payload.get("categories", []) if isinstance(payload, dict) else []

    tags_by_code: dict[str, list[str]] = {}
    for category in categories:
        for entry in category.get("meals", []) or []:
            code = str(entry.get("code", ""))
            if code and entry.get("tags"):
                tags_by_code.setdefault(code, []).extend(entry["tags"])

    meals: dict[str, Meal] = {}
    for code, raw in meals_raw.items():
        if not isinstance(raw, dict) or not raw.get("name"):
            continue
        meals[str(code)] = Meal(
            code=str(code),
            name=raw["name"],
            price_fen=_yuan_to_fen(raw.get("currentPrice")),
            original_price_fen=_yuan_to_fen(raw.get("originalPrice")),
            image=raw.get("image", ""),
            tags=tags_by_code.get(str(code), []),
        )

    if not meals:
        raise McdGitError(f"{store.label} 当前没有可取到的在售餐品（可能已打烊）。")

    repo.save_cache(
        {
            "store": store.to_dict(),
            "meals": {
                code: {
                    "name": meal.name,
                    "price_fen": meal.price_fen,
                    "original_price_fen": meal.original_price_fen,
                    "image": meal.image,
                    "tags": meal.tags,
                }
                for code, meal in meals.items()
            },
        }
    )
    return meals


def _yuan_to_fen(value: Any) -> int:
    """MCP 的价格是**字符串元**（``"33.5"``），内部一律用分。"""
    if value is None or value == "":
        return 0
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError):
        return 0


def find_meal(repo: Repo, ctx: Context, query: str, *, store: Store | None = None) -> Meal:
    """按名字/code 找一道餐品。"""
    meals = repo.cached_meals()
    if not meals:
        meals = fetch_menu(repo, ctx, store or repo.require_store())

    query = query.strip()
    if query in meals:
        return meals[query]

    lowered = query.lower()
    for meal in meals.values():
        if meal.name.lower() == lowered:
            return meal

    partial = [m for m in meals.values() if lowered in m.name.lower() or lowered in m.code]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise McdGitError(
            f"菜单里没有「{query}」。\n"
            f"        `mcdgit menu {query}` 看看有没有相近的，或 `mcdgit fetch` 刷新菜单。"
        )
    listing = "\n".join(f"          {m.code}  {m.name}" for m in partial[:10])
    raise McdGitError(f"「{query}」匹配到 {len(partial)} 道餐品，请说得再具体点：\n{listing}")


def load_mods(ctx: Context, store: Store, meal: Meal) -> Meal:
    """拉特调清单，填进 ``meal.mods``。

    MCP 的 ``modification.items[].values[]`` 里，``selectedKey`` / ``unselectedKey``
    形如 ``"0-1"`` / ``"0-0"``，编码的是 ``<组号>-<数量>``。
    """
    if meal.mods:
        return meal

    detail = ctx.call("query-meal-detail", code=meal.code, **store.mcp_args())
    if not isinstance(detail, dict):
        return meal

    meal.support_modify = bool(detail.get("supportModify"))
    meal.rounds = detail.get("rounds") or []
    meal.mods = parse_mods(detail.get("modification"))
    if detail.get("name"):
        meal.name = detail["name"]
    return meal


def parse_mods(modification: Any) -> list[ModOption]:
    options: list[ModOption] = []
    if not isinstance(modification, dict):
        return options
    for group_index, group in enumerate(modification.get("items", []) or []):
        for value in group.get("values", []) or []:
            options.append(
                ModOption(
                    code=str(value.get("code", "")),
                    name=str(value.get("name", "")),
                    price_fen=int(value.get("price") or 0),
                    selected=int(value.get("selectedQuantity") or 0),
                    max_quantity=int(value.get("maxQuantity") or 1),
                    selected_key=str(value.get("selectedKey") or ""),
                    unselected_key=str(value.get("unselectedKey") or ""),
                    group=group_index,
                )
            )
    return options


# --------------------------------------------------------------------------- #
# 配料快照 —— 本项目的核心
# --------------------------------------------------------------------------- #


def build_mods(
    repo: Repo,
    meal: Meal,
    overrides: dict[str, int] | None = None,
    *,
    respect_ignore: bool = True,
) -> list[ModState]:
    """构造一道餐品的**完整配料快照**。

    优先级从低到高：

    1. 菜单默认值（``ModOption.selected``）
    2. ``.mcdgitignore`` 里命中的配料 → 强制 0（这就是 ignore 规则的真实作用）
    3. 用户在命令行显式指定的 ``--mod``

    第 2 步不是装饰：命中的配料最终会以 ``unselectedKey`` 的形式写进
    ``create-order`` 的请求体，改变麦当劳实际做出来的东西。
    """
    overrides = overrides or {}
    states: list[ModState] = []

    for option in meal.mods:
        if option.code in overrides:
            qty = overrides[option.code]
            key = _key_for(option, qty)
            ignored = False
        elif respect_ignore and repo.is_ignored(option.name) and option.selected > 0:
            qty = 0
            key = option.unselected_key or _key_for(option, 0)
            ignored = True
        else:
            qty = option.selected
            key = option.selected_key or _key_for(option, qty)
            ignored = False

        states.append(
            ModState(
                code=option.code,
                name=option.name,
                qty=qty,
                default_qty=option.selected,
                key=key,
                group=option.group,
                price_fen=option.price_fen,
                ignored=ignored,
            )
        )
    return states


def _key_for(option: ModOption, qty: int) -> str:
    """按 ``<组号>-<数量>`` 生成特调 key。"""
    return f"{option.group}-{qty}"


def find_option(meal: Meal, name: str) -> ModOption | None:
    """按名字或 code 找一个特调项。"""
    target = name.strip().lower()
    for option in meal.mods:
        if option.code == name.strip() or option.name.lower() == target:
            return option
    for option in meal.mods:
        if target and target in option.name.lower():
            return option
    return None


def edit_mod(meal: Meal, mods: list[ModState], name: str, qty: int) -> ModState:
    """在快照上改一个配料，返回被改的那条。"""
    option = find_option(meal, name)
    if option is None:
        raise McdGitError(
            f"{meal.name} 没有「{name}」这个特调项。\n"
            f"        `mcdgit mods {meal.code}` 看看有哪些。"
        )
    for state in mods:
        if state.code == option.code:
            state.qty = qty
            state.key = _key_for(option, qty) if qty else (
                option.unselected_key or _key_for(option, 0)
            )
            state.ignored = False
            return state
    raise McdGitError(f"{meal.name} 的快照里缺少「{option.name}」")  # pragma: no cover


def ignored_names(repo: Repo) -> list[str]:
    return repo.ignore_patterns()


# --------------------------------------------------------------------------- #
# 价格
# --------------------------------------------------------------------------- #


def price_items(
    repo: Repo, ctx: Context, store: Store, items: Sequence[StagedItem]
) -> dict[str, Any]:
    """调 ``calculate-price``。返回 MCP 原始结构（单位：分）。"""
    if not items:
        return {}
    payload = [item.to_commit_item().mcp_payload() for item in items]
    return ctx.call("calculate-price", items=payload, **store.mcp_args()) or {}


def effective_price_fen(priced: dict[str, Any], staged: StagedItem) -> int:
    """从 ``productList`` 里取回这道餐品的实际小计（券后）。"""
    for entry in priced.get("productList", []) or []:
        if str(entry.get("productCode")) == staged.meal.code:
            subtotal = entry.get("subtotal")
            if subtotal is not None:
                return int(subtotal)
    return staged.subtotal_fen


# --------------------------------------------------------------------------- #
# 下单
# --------------------------------------------------------------------------- #


def place_order(
    repo: Repo,
    ctx: Context,
    store: Store,
    items: Sequence[StagedItem],
    message: str,
    *,
    take_way: str | None = None,
    reservation: str | None = None,
    note: str = "",
    priced: dict[str, Any] | None = None,
) -> Commit:
    """调 ``create-order``，把返回的订单号直接当 commit hash。

    ⚠️ 注意：``create-order`` 只**创建订单**，钱还没花。
    真正花钱在 ``mcdgit push``（扫码支付）。这正是"本地 / 远端"语义成立的原因。
    """
    if not items:
        raise McdGitError("暂存区是空的，commit 不了。")

    priced = priced if priced is not None else price_items(repo, ctx, store, items)
    take_way_code = take_way or repo.take_way

    payload: dict[str, Any] = {
        "items": [item.to_commit_item().mcp_payload() for item in items],
        "takeWayCode": take_way_code,
        "needTableware": False,
        **store.mcp_args(),
    }
    if reservation:
        payload["reservationDate"] = reservation
    if note:
        payload["remark"] = note[:50]

    result = ctx.call("create-order", **payload) or {}
    order_id = _extract_order_id(result)
    if not order_id:
        raise McdGitError(f"create-order 没有返回订单号，原始响应：{result}")

    sha = repo.short_sha(order_id)
    parents = [repo.head_sha()] if repo.head_sha() else []

    commit = Commit(
        sha=sha,
        order_id=order_id,
        message=message,
        author=repo.author,
        committed_at=now_iso(),
        items=[item.to_commit_item() for item in items],
        parents=parents,
        total_fen=int(priced.get("price") or sum(i.subtotal_fen for i in items)),
        discount_fen=int(priced.get("discount") or 0),
        store=store.to_dict(),
        take_way=take_way_code,
        note=note,
    )
    repo.save_commit(commit)
    repo.write_ref(repo.head_ref(), commit.sha)
    return commit


def _extract_order_id(result: Any) -> str:
    if isinstance(result, str):
        return result.strip()
    if not isinstance(result, dict):
        return ""
    for key in ("orderId", "orderNo", "orderID", "id"):
        value = result.get(key)
        if value:
            return str(value)
    for key in ("data", "order", "result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            found = _extract_order_id(nested)
            if found:
                return found
    return ""


def order_status(ctx: Context, order_id: str) -> dict[str, Any]:
    detail = ctx.call("query-order", orderId=order_id)
    return detail if isinstance(detail, dict) else {}


def payment_url(ctx: Context, order_id: str) -> str:
    """尝试从订单详情里抠出支付链接。字段名各接口不统一，所以广撒网。"""
    try:
        detail = order_status(ctx, order_id)
    except McdGitError:
        return ""
    return _find_url(detail)


def _find_url(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    for key in ("payUrl", "paymentUrl", "payLink", "qrCodeUrl", "url", "payCode"):
        value = node.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for value in node.values():
        if isinstance(value, list):
            for entry in value:
                found = _find_url(entry)
                if found:
                    return found
        else:
            found = _find_url(value)
            if found:
                return found
    return ""


def cancel(ctx: Context, order_id: str, reason: str = "1") -> None:
    ctx.call("cancel-order", orderId=order_id, cancelReasonCode=reason)


# --------------------------------------------------------------------------- #
# 其它
# --------------------------------------------------------------------------- #


def bind_coupons(ctx: Context) -> Any:
    """``mcdgit pull`` 的"同步远端"部分：一键领券。"""
    return ctx.call("auto-bind-coupons")


def summarize_changes(items: Iterable[StagedItem]) -> tuple[int, int]:
    """返回 ``(insertions, deletions)``，用来假装 git 的 diffstat。"""
    insertions = deletions = 0
    for item in items:
        changes = item.changes
        if not changes:
            insertions += 1
            continue
        for change in changes:
            if change.qty == 0:
                deletions += 1
            else:
                insertions += 1
    return insertions, deletions
