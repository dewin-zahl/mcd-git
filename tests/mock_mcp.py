"""一个假的麦当劳 MCP Server，只给测试用。

存在的理由有两个：

1. **安全。** ``create-order`` 会真的在麦当劳系统里建一张订单。自动测试不能
   干这种事——所以端到端测试全部打到这个假服务上。
2. **保真。** 它刻意复刻了真实服务那些反直觉的行为：响应体是
   "给 LLM 的说明文本 + 内嵌 JSON"，``content[0].text`` 里还夹着 Markdown。
   解析器如果只处理干净 JSON，在这里就会挂。

用真实抓包的数据当 fixture，shape 一律照抄。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STORE_CODE = "1990696"
STORE_NAME = "麦当劳南京青岛路餐厅"

MEALS: dict[str, dict[str, Any]] = {
    "1001": {"name": "巨无霸", "currentPrice": "26", "originalPrice": "26"},
    "1002": {"name": "中薯条", "currentPrice": "14.5", "originalPrice": "14.5"},
    "1003": {"name": "麦辣鸡腿堡", "currentPrice": "24", "originalPrice": "26"},
}

def _v(code: str, name: str, selected: int) -> dict[str, Any]:
    return {
        "code": code,
        "price": 0,
        "name": name,
        "maxQuantity": 1,
        "minQuantity": 0,
        "selectedQuantity": selected,
        "selectedKey": f"0-{selected}",
        "unselectedKey": "0-0",
    }


#: 照抄 query-meal-detail 的真实结构，含 selectedKey / unselectedKey。
MOD_DETAIL: dict[str, dict[str, Any]] = {
    "1001": {
        "code": "1001",
        "name": "巨无霸",
        "supportModify": True,
        "rounds": [],
        "modification": {
            "items": [
                {
                    "maxValues": 5,
                    "minValues": 0,
                    "values": [
                        _v("2001", "巨无霸酱", 1),
                        _v("2002", "吉士", 1),
                        _v("2003", "生菜", 1),
                        _v("2004", "洋葱粒", 1),
                    ],
                }
            ]
        },
    },
    "1002": {
        "code": "1002",
        "name": "中薯条",
        "supportModify": False,
        "rounds": [],
    },
    "1003": {
        "code": "1003",
        "name": "麦辣鸡腿堡",
        "supportModify": True,
        "rounds": [],
        "modification": {
            "items": [
                {
                    "maxValues": 2,
                    "minValues": 0,
                    "values": [_v("2001", "巨无霸酱", 1), _v("2003", "生菜", 1)],
                }
            ]
        },
    },
}


class State:
    """记录这个假服务被怎么用了，用来断言。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.orders: dict[str, dict[str, Any]] = {}
        self._order_seq = 0

    def next_order_id(self) -> str:
        """刻意让前 7 位在不同订单间就不同——真实订单号确实会共享长前缀，
        但测试不该依赖"碰撞退避"能兜住，那是安全网不是常态。"""
        self._order_seq += 1
        return f"103010{self._order_seq}8270000795607354880945"

    def reset(self) -> None:
        self.calls.clear()
        self.orders.clear()
        self._order_seq = 0


# --------------------------------------------------------------------------- #
# 工具实现
# --------------------------------------------------------------------------- #


def _tool_query_meals(state: State, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "categories": [
            {"name": "人气热卖", "meals": [{"code": code} for code in MEALS]},
        ],
        "meals": MEALS,
    }


def _tool_query_meal_detail(state: State, args: dict[str, Any]) -> dict[str, Any]:
    code = str(args.get("code", ""))
    if code not in MOD_DETAIL:
        return _fail(600404, f"餐品 {code} 不存在")
    return MOD_DETAIL[code]


def _tool_calculate_price(state: State, args: dict[str, Any]) -> dict[str, Any]:
    items = args.get("items") or []
    product_list = []
    original = 0
    for item in items:
        code = str(item.get("productCode", ""))
        quantity = int(item.get("quantity") or 1)
        unit = int(round(float(MEALS.get(code, {}).get("currentPrice", "0")) * 100))
        subtotal = unit * quantity
        product_list.append(
            {
                "productCode": code,
                "productName": MEALS.get(code, {}).get("name", code),
                "quantity": quantity,
                "originalSubtotal": subtotal,
                "subtotal": subtotal,
            }
        )
        original += subtotal
    return {
        "productOriginalPrice": original,
        "productPrice": original,
        "originalPrice": original,
        "discount": 0,
        "price": original,
        "productList": product_list,
        "takeWayList": [
            {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
            {"code": "locker-in", "title": "外带", "subtitle": "店内取餐柜"},
        ],
    }


def _tool_create_order(state: State, args: dict[str, Any]) -> dict[str, Any]:
    order_id = state.next_order_id()
    request_items = args.get("items") or []
    state.orders[order_id] = {
        "orderId": order_id,
        "status": "UNPAID",
        "payUrl": f"https://pay.mock.mcd.cn/{order_id}",
        # 真实订单详情的主字段是 productList（带菜名），items 是原始请求体。
        # 两个都留着，用来验证"名字缺失时能回落"的现象。
        "productList": [
            {
                "productCode": item.get("productCode"),
                "productName": MEALS.get(str(item.get("productCode")), {}).get("name", ""),
                "quantity": item.get("quantity", 1),
                "price": int(round(float(MEALS.get(str(item.get("productCode")), {})
                                       .get("currentPrice", "0")) * 100)),
                "modification": item.get("modification"),
            }
            for item in request_items
            if isinstance(item, dict)
        ],
        "items": request_items,
        "request": args,
    }
    return {"orderId": order_id, "payUrl": state.orders[order_id]["payUrl"]}


def _tool_query_order(state: State, args: dict[str, Any]) -> dict[str, Any]:
    order_id = str(args.get("orderId", ""))
    order = state.orders.get(order_id)
    if order is None:
        return _fail(600404, f"订单 {order_id} 不存在")
    return order


def _tool_cancel_order(state: State, args: dict[str, Any]) -> dict[str, Any]:
    order_id = str(args.get("orderId", ""))
    if order_id not in state.orders:
        return _fail(600404, f"订单 {order_id} 不存在")
    state.orders[order_id]["status"] = "CANCELLED"
    return {"orderId": order_id, "status": "CANCELLED"}


def _tool_nearby_stores(state: State, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"storeCode": STORE_CODE, "storeName": STORE_NAME, "address": "青岛路33号", "distance": 539},
        {"storeCode": "1990097", "storeName": "麦当劳南京广州路餐厅", "address": "广州路14号", "distance": 558},
    ]


def _tool_auto_bind_coupons(state: State, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"couponName": "满 40 减 5"}, {"couponName": "薯条第二份半价"}]


def _fail(code: int, message: str) -> dict[str, Any]:
    return {"__fail__": True, "code": code, "message": message}


TOOLS: dict[str, Any] = {
    "query-meals": _tool_query_meals,
    "query-meal-detail": _tool_query_meal_detail,
    "calculate-price": _tool_calculate_price,
    "create-order": _tool_create_order,
    "query-order": _tool_query_order,
    "cancel-order": _tool_cancel_order,
    "query-nearby-stores": _tool_nearby_stores,
    "auto-bind-coupons": _tool_auto_bind_coupons,
}

TOOL_SCHEMAS = [
    {"name": name, "description": f"mock {name}", "inputSchema": {"type": "object"}}
    for name in TOOLS
]


# --------------------------------------------------------------------------- #
# HTTP 层
# --------------------------------------------------------------------------- #


class _Handler(BaseHTTPRequestHandler):
    state: State

    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass  # 测试时不要刷屏

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            request = json.loads(raw)
        except json.JSONDecodeError:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            return

        method = request.get("method")
        request_id = request.get("id")

        if method == "initialize":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "mock-mcd", "version": "0"},
                    },
                },
                session_id="mock-session",
            )
            return

        if method in ("notifications/initialized", "notifications/cancelled"):
            self._send({}, status=202)
            return

        if method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOL_SCHEMAS}})
            return

        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            self.state.calls.append((name, arguments))
            handler = TOOLS.get(name)
            if handler is None:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32602, "message": f"unknown tool: {name}"},
                    }
                )
                return
            payload = handler(self.state, arguments)
            self._send({"jsonrpc": "2.0", "id": request_id, "result": _wrap(name, payload)})
            return

        self._send(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": method}}
        )

    def do_DELETE(self) -> None:  # noqa: N802
        self._send({}, status=204)

    def _send(self, payload: dict[str, Any], *, status: int = 200, session_id: str = "") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if status != 204:
            self.wfile.write(body)


def _wrap(tool: str, payload: Any) -> dict[str, Any]:
    """复刻真实服务的返回格式：说明文本 + 内嵌 JSON + 尾部提示。

    这一段是故意的。如果哪天解析器被"优化"成只 ``json.loads``，测试会立刻红。
    """
    if isinstance(payload, dict) and payload.get("__fail__"):
        envelope = {
            "success": False,
            "code": payload["code"],
            "message": payload["message"],
            "data": None,
        }
    else:
        envelope = {
            "success": True,
            "code": 200,
            "message": "请求成功",
            "datetime": "2026-10-10 16:36:25",
            "traceId": "mock",
            "data": payload,
        }
    text = (
        f"{tool} 的返回字段说明：\n"
        f"- **data**: 业务数据\n"
        f"- **code**: 200 表示成功\n"
        f"{json.dumps(envelope, ensure_ascii=False)}\n"
        f"## 展示结果时，每个字段必须完整展示，严禁省略"
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


class MockMcpServer:
    """可以 ``with`` 用的假 MCP 服务。"""

    def __init__(self, token: str = "mock-token") -> None:
        self.token = token
        self.state = State()
        handler = type("_BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> MockMcpServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def calls_of(self, tool: str) -> list[dict[str, Any]]:
        return [args for name, args in self.state.calls if name == tool]
