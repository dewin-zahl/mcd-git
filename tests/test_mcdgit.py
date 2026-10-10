"""测试。标准库 unittest，不需要装任何东西。

    cd mcd-git && python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mcdgit.mcp import _extract_json  # noqa: E402
from tests.mock_mcp import MockMcpServer  # noqa: E402


# --------------------------------------------------------------------------- #
# MCP 响应解析
# --------------------------------------------------------------------------- #

#: 真实抓包的形状：说明文本 + 内嵌 JSON + 尾部提示
REAL_PAYLOAD = (
    "每个门店的基本信息按单行展示：\n"
    "- **门店名称**: data[].storeName\n"
    "- **门店编号**: data[].storeCode, 唯一编号，工具入参\n"
    '{"success":true,"code":200,"message":"请求成功","data":[{"storeCode":"1990696",'
    '"storeName":"麦当劳南京青岛路餐厅","address":"青岛路33号","distance":539,'
    '"reservation":true,"reservationTimeOptions":[{"date":"2026-10-10","today":true,'
    '"reservationOptionText":"下午茶(14:44至16:45)"}]}]}'
    "## 展示结果时，每个门店的 storeCode 和 beCode 必须完整展示，"
)


class TestExtractJson(unittest.TestCase):
    def test_finds_envelope_in_wrapped_text(self) -> None:
        parsed = _extract_json(REAL_PAYLOAD)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["code"], 200)
        self.assertEqual(parsed["data"][0]["storeCode"], "1990696")

    def test_does_not_mistake_nested_object_for_envelope(self) -> None:
        """早期的实现从后往前找 `{`，会把 reservationTimeOptions 当成数据。"""
        parsed = _extract_json(REAL_PAYLOAD)
        self.assertIn("data", parsed)
        self.assertNotIn("reservationOptionText", parsed)

    def test_plain_json_still_works(self) -> None:
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(_extract_json("就是一句人话错误"))


# --------------------------------------------------------------------------- #
# 端到端（打到 mock MCP）
# --------------------------------------------------------------------------- #


class CliTestCase(unittest.TestCase):
    """跑真 CLI，但 MCP 指向假服务。"""

    server: MockMcpServer

    def setUp(self) -> None:
        self.server = MockMcpServer()
        self.server.__enter__()
        self.addCleanup(lambda: self.server.__exit__(None, None, None))

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo_dir = Path(self.tmp.name)

        self._old_env = {
            "MCD_MCP_URL": os.environ.get("MCD_MCP_URL"),
            "MCD_MCP_TOKEN": os.environ.get("MCD_MCP_TOKEN"),
            "NO_COLOR": os.environ.get("NO_COLOR"),
        }
        os.environ["MCD_MCP_URL"] = self.server.url
        os.environ["MCD_MCP_TOKEN"] = "mock-token"
        os.environ["NO_COLOR"] = "1"
        self.addCleanup(self._restore_env)

        self.run_cli("init", "--store", "1990696", "--name", "麦当劳南京青岛路餐厅")
        self.run_cli("fetch")

    def _restore_env(self) -> None:
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        from mcdgit.cli import main

        out, err = io.StringIO(), io.StringIO()
        args = ["-C", str(self.repo_dir), *argv]
        with redirect_stdout(out), redirect_stderr(err):
            code = main(args)
        return code, out.getvalue(), err.getvalue()

    # ------------------------------------------------------------------ #

    def test_fetch_caches_menu(self) -> None:
        code, out, _ = self.run_cli("menu")
        self.assertEqual(code, 0)
        self.assertIn("巨无霸", out)

    def test_dine_in_never_sends_be_code(self) -> None:
        """到店自取传 beCode 会被真实 MCP 拒绝——这是最容易犯的错。"""
        self.run_cli("menu")
        for tool in ("query-meals", "calculate-price", "create-order"):
            for args in self.server.calls_of(tool):
                self.assertNotIn("beCode", args, f"{tool} 不该带 beCode")

    def test_ignore_rules_become_unselected_key(self) -> None:
        """`.mcdgitignore` 写"洋葱粒" → 请求体里必须是 unselectedKey。"""
        (self.repo_dir / ".mcdgitignore").write_text("洋葱粒\n", encoding="utf-8")
        code, out, _ = self.run_cli("add", "巨无霸")
        self.assertEqual(code, 0)
        self.assertIn("洋葱粒", out)

        self.run_cli("commit", "-m", "test")
        payload = self._create_order_payload()
        onion = self._find_mod(payload, "2004")
        self.assertEqual(onion["quantity"], 0)
        self.assertEqual(onion["key"], "0-0", "必须用 unselectedKey")

    def test_all_mods_are_sent_not_just_changes(self) -> None:
        """官方要求：组内所有特调项都必须传入，不能只传改动。"""
        (self.repo_dir / ".mcdgitignore").write_text("洋葱粒\n", encoding="utf-8")
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "test")
        payload = self._create_order_payload()
        codes = {v["code"] for v in payload["modification"]["values"]}
        self.assertEqual(codes, {"2001", "2002", "2003", "2004"})

    def test_explicit_mod_overrides_ignore(self) -> None:
        (self.repo_dir / ".mcdgitignore").write_text("洋葱粒\n", encoding="utf-8")
        self.run_cli("add", "巨无霸", "--mod", "洋葱粒=2")
        self.run_cli("commit", "-m", "test")
        payload = self._create_order_payload()
        onion = self._find_mod(payload, "2004")
        self.assertEqual(onion["quantity"], 2)
        self.assertEqual(onion["key"], "0-2", "按 <组号>-<数量> 生成 key")

    def test_add_is_idempotent(self) -> None:
        self.run_cli("add", "巨无霸")
        print_out = self.run_cli("add", "巨无霸")[1]
        self.assertIn("已更新", print_out)
        _, out, _ = self.run_cli("status")
        self.assertIn("×1", out)
        self.assertIn("¥26.00", out)

    def test_quantity_flag_multiplies(self) -> None:
        self.run_cli("add", "巨无霸", "-q", "3")
        _, out, _ = self.run_cli("diff")
        self.assertIn("¥78.00", out)

    def test_commit_sha_is_order_id_prefix(self) -> None:
        self.run_cli("add", "巨无霸")
        code, out, err = self.run_cli("commit", "-m", "feat: 下午续命")
        self.assertEqual(code, 0, err)

        payload = self._create_order_payload()
        self.assertTrue(payload)
        order_id = self.server.state.orders
        self.assertEqual(len(order_id), 1)
        real_order = next(iter(order_id))

        _, log, _ = self.run_cli("log", "--oneline")
        self.assertIn(real_order[:7], log, "commit hash 必须就是订单号前 7 位")

    def test_commit_clears_index_and_does_not_pay(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "test")
        _, out, _ = self.run_cli("status")
        self.assertIn("nothing to commit", out)
        order = next(iter(self.server.state.orders.values()))
        self.assertEqual(order["status"], "UNPAID", "commit 不该产生支付")

    def test_push_surfaces_payment_url(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "test")
        _, out, _ = self.run_cli("push")
        self.assertIn("https://pay.mock.mcd.cn/", out)

    def test_revert_cancels_and_rewinds(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "第一单")
        self.run_cli("add", "中薯条")
        self.run_cli("commit", "-m", "第二单")

        _, before, _ = self.run_cli("log", "--oneline")
        code, out, err = self.run_cli("revert", "HEAD")
        self.assertEqual(code, 0, err)
        self.assertIn("已取消", out)

        statuses = {o["status"] for o in self.server.state.orders.values()}
        self.assertIn("CANCELLED", statuses)

        _, after, _ = self.run_cli("log", "--oneline")
        self.assertLess(len(after.splitlines()), len(before.splitlines()))

    def test_blame_tracks_onion_history(self) -> None:
        self.run_cli("add", "巨无霸", "--mod", "洋葱粒=1")
        self.run_cli("commit", "-m", "第一单")
        self.run_cli("add", "巨无霸")  # 默认带洋葱粒，但下面加回 ignore
        (self.repo_dir / ".mcdgitignore").write_text("洋葱粒\n", encoding="utf-8")
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "戒洋葱")
        self.run_cli("add", "巨无霸", "--mod", "洋葱粒=1")
        self.run_cli("commit", "-m", "复吸")

        code, out, err = self.run_cli("blame", "洋葱粒")
        self.assertEqual(code, 0, err)
        self.assertIn("引入", out)
        self.assertIn("移除", out)
        self.assertIn("加回", out)
        self.assertIn("上次你明明删掉了", out)

    def test_stash_roundtrip(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("stash", "push", "-m", "先不吃")
        _, out, _ = self.run_cli("status")
        self.assertIn("nothing to commit", out)

        _, listed, _ = self.run_cli("stash", "list")
        self.assertIn("先不吃", listed)

        self.run_cli("stash", "pop")
        _, out2, _ = self.run_cli("status")
        self.assertIn("巨无霸", out2)

    def test_cherry_pick_restores_items(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "第一单")
        sha = next(iter(self.server.state.orders))[:7]

        code, out, err = self.run_cli("cherry-pick", sha)
        self.assertEqual(code, 0, err)
        self.assertIn("巨无霸", out)
        _, status, _ = self.run_cli("status")
        self.assertIn("巨无霸", status)

    def test_ingredients_summarises_toppings(self) -> None:
        self.run_cli("add", "巨无霸", "--mod", "吉士=2")
        self.run_cli("commit", "-m", "第一单")
        code, out, err = self.run_cli("ingredients")
        self.assertEqual(code, 0, err)
        self.assertIn("吉士", out)
        self.assertIn("你的配料史记", out)

    def test_clone_from_someone_elses_order_id(self) -> None:
        """传播机制：别人把订单号发给你，你就能复刻。"""
        self.run_cli("add", "巨无霸", "-q", "2")
        self.run_cli("commit", "-m", "第一单")
        order_id = next(iter(self.server.state.orders))

        self.run_cli("reset")  # 清空暂存区，确保下面的内容确实来自 clone

        code, out, err = self.run_cli("clone", order_id)
        self.assertEqual(code, 0, err)
        self.assertIn("巨无霸", out)
        self.assertIn("Cloning into", out)

        _, status, _ = self.run_cli("status")
        self.assertIn("巨无霸", status)
        self.assertIn("×2", status)

    def test_clone_unknown_source_fails_cleanly(self) -> None:
        code, _, err = self.run_cli("clone", "0000000000000000")
        self.assertEqual(code, 1)
        self.assertIn("复刻失败", err)

    def test_show_renders_order_detail(self) -> None:
        self.run_cli("add", "巨无霸", "--mod", "吉士=2")
        self.run_cli("commit", "-m", "feat: 下午续命")
        code, out, err = self.run_cli("show", "HEAD")
        self.assertEqual(code, 0, err)
        self.assertIn("feat: 下午续命", out)
        self.assertIn("OrderId:", out)
        self.assertIn("吉士 1 -> 2", out)

    def test_rm_topping(self) -> None:
        self.run_cli("add", "巨无霸")
        code, out, err = self.run_cli("rm", "吉士")
        self.assertEqual(code, 0, err)
        self.assertIn("去掉", out)
        _, diff, _ = self.run_cli("diff")
        self.assertIn("吉士 1 -> 0", diff)

    def test_rm_meal_unstages(self) -> None:
        self.run_cli("add", "巨无霸")
        code, out, _ = self.run_cli("rm", "巨无霸")
        self.assertEqual(code, 0)
        self.assertIn("取消暂存", out)
        _, status, _ = self.run_cli("status")
        self.assertIn("nothing to commit", status)

    def test_branch_and_tag(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "第一单")
        self.run_cli("tag", "v1.0")
        self.run_cli("branch", "加辣")

        _, branches, _ = self.run_cli("branch")
        self.assertIn("加辣", branches)

        self.run_cli("checkout", "加辣")
        _, status, _ = self.run_cli("status")
        self.assertIn("On branch 加辣", status)

        _, tags, _ = self.run_cli("tag")
        self.assertIn("v1.0", tags)

    def test_log_graph_renders(self) -> None:
        self.run_cli("add", "巨无霸")
        self.run_cli("commit", "-m", "feat: 下午续命")
        code, out, _ = self.run_cli("log", "--graph")
        self.assertEqual(code, 0)
        self.assertIn("*", out)
        self.assertIn("feat: 下午续命", out)

    def test_unknown_meal_gives_actionable_error(self) -> None:
        code, _, err = self.run_cli("add", "螺蛳粉")
        self.assertEqual(code, 1)
        self.assertIn("菜单里没有", err)

    def test_commit_empty_index_fails(self) -> None:
        code, _, err = self.run_cli("commit", "-m", "空提交")
        self.assertEqual(code, 1)
        self.assertIn("nothing to commit", err)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _create_order_payload(self) -> dict:
        calls = self.server.calls_of("create-order")
        self.assertTrue(calls, "没有调用过 create-order")
        items = calls[-1]["items"]
        self.assertTrue(items)
        return items[0]

    @staticmethod
    def _find_mod(payload: dict, code: str) -> dict:
        for value in payload["modification"]["values"]:
            if value["code"] == code:
                return value
        raise AssertionError(f"请求体里缺少特调项 {code}")


# --------------------------------------------------------------------------- #
# 纯本地逻辑（不需要 MCP）
# --------------------------------------------------------------------------- #


class TestLocalRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from mcdgit.store import Repo

        self.repo = Repo.init(Path(self.tmp.name))

    def test_short_sha_is_order_prefix(self) -> None:
        order_id = "1030108270000795607354880945"
        self.assertEqual(self.repo.short_sha(order_id), "1030108")

    def test_ignore_substring_match(self) -> None:
        (self.repo.root / ".mcdgitignore").write_text("洋葱\n", encoding="utf-8")
        self.assertTrue(self.repo.is_ignored("洋葱粒"))
        self.assertFalse(self.repo.is_ignored("生菜"))

    def test_ignore_supports_glob(self) -> None:
        (self.repo.root / ".mcdgitignore").write_text("辣*\n", encoding="utf-8")
        self.assertTrue(self.repo.is_ignored("辣椒酱"))
        self.assertFalse(self.repo.is_ignored("甜酱"))

    def test_ignore_comments_are_skipped(self) -> None:
        (self.repo.root / ".mcdgitignore").write_text("# 洋葱\n生菜\n", encoding="utf-8")
        self.assertFalse(self.repo.is_ignored("洋葱"))
        self.assertTrue(self.repo.is_ignored("生菜"))

    def test_be_code_rule_per_be_type(self) -> None:
        from mcdgit.model import Store

        for be_type, expected in ((1, False), (5, True), (2, True), (6, True)):
            store = Store(store_code="1", be_code="BE1", be_type=be_type)
            args = store.mcp_args()
            self.assertEqual(
                "beCode" in args, expected, f"beType={be_type} 的 beCode 规则不对"
            )
            self.assertEqual(args["orderType"], 1 if be_type in (1, 5) else 2)

    def test_discover_walks_up(self) -> None:
        from mcdgit.store import Repo

        nested = self.repo.root / "a" / "b"
        nested.mkdir(parents=True)
        found = Repo.discover(nested)
        self.assertEqual(found.root, self.repo.root)

    def test_staged_item_roundtrip(self) -> None:
        from mcdgit.model import Meal, ModState, StagedItem

        item = StagedItem(
            meal=Meal(code="1001", name="巨无霸", price_fen=2600),
            quantity=2,
            mods=[ModState(code="2004", name="洋葱粒", qty=0, default_qty=1, key="0-0", ignored=True)],
        )
        restored = StagedItem.from_dict(json.loads(json.dumps(item.to_dict())))
        self.assertEqual(restored.meal.name, "巨无霸")
        self.assertEqual(restored.quantity, 2)
        self.assertEqual(restored.mods[0].key, "0-0")
        self.assertTrue(restored.mods[0].removed)

    def test_diff_flags_menu_price_vs_actual_price(self) -> None:
        """query-meals 给促销价、calculate-price 给实付价，两者经常不一样。

        实测踩到过一次：菜单显示 ¥49.90，实付 ¥117.50。不能让用户以为被悄悄加价，
        所以落差必须显式画出来。
        """
        from mcdgit import render
        from mcdgit.model import Meal, StagedItem

        palette = render.Palette(False)
        item = StagedItem(meal=Meal(code="1001", name="分享餐", price_fen=4990), quantity=1)

        diverged = "\n".join(
            render.render_diff_item(palette, item, price_fen=11750)
        )
        self.assertIn("¥117.50", diverged)
        self.assertIn("菜单价 ¥49.90", diverged)

        same = "\n".join(render.render_diff_item(palette, item, price_fen=4990))
        self.assertNotIn("菜单价", same)

    def test_merge_conflict_needs_strategy(self) -> None:
        from mcdgit.commands.order import _merge_items
        from mcdgit.model import CommitItem

        mine = [CommitItem(product_code="A", name="鸡翅", quantity=1)]
        theirs = [CommitItem(product_code="A", name="鸡翅", quantity=1)]
        merged, conflicts = _merge_items(mine, theirs, "")
        self.assertFalse(merged)
        self.assertEqual(conflicts[0][0], "鸡翅")

        merged, conflicts = _merge_items(mine, theirs, "union")
        self.assertFalse(conflicts)
        self.assertEqual(merged[0].quantity, 2)

    def test_merge_ours_drops_theirs(self) -> None:
        from mcdgit.commands.order import _merge_items
        from mcdgit.model import CommitItem

        mine = [CommitItem(product_code="A", name="鸡翅", quantity=1)]
        theirs = [CommitItem(product_code="B", name="巨无霸", quantity=1)]
        merged, conflicts = _merge_items(mine, theirs, "ours")
        self.assertFalse(conflicts)
        self.assertEqual({i.product_code for i in merged}, {"A", "B"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
