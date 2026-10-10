"""领域对象。

刻意不用 pydantic：这个项目要保证 ``git clone`` 完直接能跑，零第三方依赖。

这里只有一个设计决策需要解释：**为什么特调用"完整快照"而不是"差异"**。

官方文档里有这么一句话，很容易读漏：

    对于包含 unselectedKey 的特调组，该组内所有特调项都必须传入；
    选中的用 selectedKey，未选中的用 unselectedKey。

所以发给 ``create-order`` 的 ``modification.values[]`` 必须是**整道餐品的
全部配料**，而不是"你改了什么"。这条路顺便带来一个好处：
每次提交都留下了配料快照，于是 ``mcdgit blame 洋葱`` 可以追溯到历史上
每一次它的来去——包括那些你没主动改、只是默认带上的。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


# --------------------------------------------------------------------------- #
# 门店
# --------------------------------------------------------------------------- #

BE_TYPES: dict[int, str] = {
    1: "到店自取",
    2: "麦乐送",
    5: "得来速",
    6: "企业团餐",
}
BE_TYPE_TO_ORDER_TYPE: dict[int, int] = {1: 1, 5: 1, 2: 2, 6: 2}

#: 只有这几个场景需要带 beCode 出门。
#: 到店自取(1) 传了 beCode 会被 MCP 直接拒绝——踩过坑，写死在这。
BE_TYPES_NEEDING_BE_CODE: frozenset[int] = frozenset({5, 2, 6})


@dataclass
class Store:
    """一个"C"端门店。``be_type`` 决定后面每一步该带哪些参数。"""

    store_code: str
    store_name: str = ""
    address: str = ""
    be_type: int = 1
    be_code: str = ""
    address_id: str = ""

    @property
    def order_type(self) -> int:
        return BE_TYPE_TO_ORDER_TYPE[self.be_type]

    @property
    def label(self) -> str:
        kind = BE_TYPES.get(self.be_type, "未知")
        return f"{self.store_name or self.store_code} ({kind})"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Store:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def mcp_args(self) -> dict[str, Any]:
        """生成喂给 MCP 的门店参数。

        - ``beType=1``（到店自取）→ ``orderType=1``，**不传** ``beCode``
        - ``beType=5``（得来速）→ ``orderType=1``，**必传** ``beCode``
        - ``beType=2``（麦乐送）→ ``orderType=2``，**必传** ``beCode``
        - ``beType=6``（团餐）→ ``orderType=2``，**必传** ``beCode``
        """
        args: dict[str, Any] = {
            "storeCode": self.store_code,
            "orderType": self.order_type,
            "beType": self.be_type,
        }
        if self.be_type in BE_TYPES_NEEDING_BE_CODE:
            args["beCode"] = self.be_code
        return args


# --------------------------------------------------------------------------- #
# 餐品与特调
# --------------------------------------------------------------------------- #


@dataclass
class ModOption:
    """菜单上的一个可特调配料（吉士、生菜、酱……）。

    ``selected_key`` / ``unselected_key`` 形如 ``"0-1"`` / ``"0-0"``，
    编码的是 ``<组号>-<数量>``。它们就是 git 里的"这一行"和"这行被删掉"。
    """

    code: str
    name: str
    price_fen: int = 0
    selected: int = 1
    max_quantity: int = 1
    selected_key: str = ""
    unselected_key: str = ""
    group: int = 0


@dataclass
class ModState:
    """一次提交里，某个配料的**最终状态**（不是变化量）。

    ``default_qty`` 是菜单默认值，``qty`` 是实际下单值。
    两者不等 = 你对配料动了手。
    """

    code: str
    name: str
    qty: int
    default_qty: int
    key: str = ""
    group: int = 0
    price_fen: int = 0
    ignored: bool = False

    @property
    def changed(self) -> bool:
        return self.qty != self.default_qty

    @property
    def removed(self) -> bool:
        return self.qty == 0 and self.default_qty > 0

    @property
    def action(self) -> str:
        """``""`` 表示没动过。"""
        if not self.changed:
            return ""
        if self.qty == 0:
            return "移除"
        if self.default_qty == 0:
            return "引入"
        if self.qty > self.default_qty:
            return "加量"
        return "减量"

    @property
    def symbol(self) -> str:
        return "-" if self.qty == 0 else "+"

    @property
    def spec(self) -> str:
        return f"{self.name} {self.default_qty} -> {self.qty}"

    @property
    def action_label(self) -> str:
        return self.action or "保持"


@dataclass
class Meal:
    """菜单上的一道餐品。"""

    code: str
    name: str
    price_fen: int = 0
    original_price_fen: int = 0
    image: str = ""
    support_modify: bool = False
    mods: list[ModOption] = field(default_factory=list)
    rounds: list[dict[str, Any]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @property
    def price(self) -> str:
        return f"{self.price_fen / 100:.2f}"


@dataclass
class CommitItem:
    """提交里的一道餐品，含配料完整快照。"""

    product_code: str
    name: str
    quantity: int
    unit_price_fen: int = 0
    subtotal_fen: int = 0
    mods: list[ModState] = field(default_factory=list)
    round_choices: dict[str, str] = field(default_factory=dict)

    @property
    def changes(self) -> list[ModState]:
        return [m for m in self.mods if m.changed]

    def mcp_payload(self) -> dict[str, Any]:
        """转成 ``create-order`` 需要的 item 结构。

        因为 ``mods`` 本来就是完整快照，这里不需要再补默认值——
        这正好满足官方"组内所有特调项都必须传入"的要求。
        """
        payload: dict[str, Any] = {
            "productCode": self.product_code,
            "quantity": self.quantity,
        }
        if self.mods:
            payload["modification"] = {
                "values": [
                    {"code": m.code, "key": m.key, "quantity": m.qty} for m in self.mods
                ]
            }
        return payload

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommitItem:
        mods = [ModState(**m) for m in data.get("mods", [])]
        return cls(
            product_code=data.get("product_code", ""),
            name=data.get("name", ""),
            quantity=data.get("quantity", 1),
            unit_price_fen=data.get("unit_price_fen", 0),
            subtotal_fen=data.get("subtotal_fen", 0),
            mods=mods,
            round_choices=dict(data.get("round_choices", {})),
        )


# --------------------------------------------------------------------------- #
# 提交
# --------------------------------------------------------------------------- #


@dataclass
class Commit:
    """一次提交 = 一份真实订单。

    ``sha`` 取的是麦当劳订单号的前 7 位——所以你的 commit hash 就是你的订单号。
    这不是比喻，两个是同一个东西。
    """

    sha: str
    order_id: str
    message: str
    author: str
    committed_at: str
    items: list[CommitItem] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    total_fen: int = 0
    discount_fen: int = 0
    store: dict[str, Any] = field(default_factory=dict)
    take_way: str = ""
    tags: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0] if self.message else ""

    @property
    def short_date(self) -> str:
        try:
            return datetime.fromisoformat(self.committed_at).strftime("%m-%d %H:%M")
        except ValueError:
            return self.committed_at[:16]

    @property
    def total(self) -> str:
        return f"{self.total_fen / 100:.2f}"

    @property
    def item_count(self) -> int:
        return sum(item.quantity for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Commit:
        known = set(cls.__dataclass_fields__)
        filtered = {k: v for k, v in data.items() if k in known}
        filtered["items"] = [CommitItem.from_dict(i) for i in data.get("items", [])]
        return cls(**filtered)


# --------------------------------------------------------------------------- #
# 暂存区
# --------------------------------------------------------------------------- #


@dataclass
class StagedItem:
    """暂存区里的一条待提交餐品。``mods`` 同样是完整快照。"""

    meal: Meal
    quantity: int = 1
    mods: list[ModState] = field(default_factory=list)

    @property
    def changes(self) -> list[ModState]:
        return [m for m in self.mods if m.changed]

    @property
    def ignored(self) -> list[ModState]:
        return [m for m in self.mods if m.ignored]

    @property
    def subtotal_fen(self) -> int:
        return self.meal.price_fen * self.quantity

    def to_commit_item(self) -> CommitItem:
        return CommitItem(
            product_code=self.meal.code,
            name=self.meal.name,
            quantity=self.quantity,
            unit_price_fen=self.meal.price_fen,
            subtotal_fen=self.subtotal_fen,
            mods=list(self.mods),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "meal": asdict(self.meal),
            "quantity": self.quantity,
            "mods": [asdict(m) for m in self.mods],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StagedItem:
        meal_data = dict(data.get("meal", {}))
        meal_data["mods"] = [ModOption(**m) for m in meal_data.get("mods", [])]
        raw_mods = data.get("mods")
        if raw_mods is None:
            # 兼容早期格式：只有 changes，没有快照
            raw_mods = [c for c in data.get("changes", [])]
            for entry in raw_mods:
                entry.setdefault("default_qty", entry.get("qty", entry.get("to_qty", 1)))
        mods = [ModState(**m) for m in raw_mods]
        return cls(
            meal=Meal(**meal_data),
            quantity=data.get("quantity", 1),
            mods=mods,
        )
