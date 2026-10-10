"""渲染层 —— 这个项目能不能被截图传播，全看这个文件。

三条硬规则：

1. **中文对齐必须算显示宽度。** 汉字是两个终端列，用 ``len()`` 对齐会塌。
2. **颜色要能在非 TTY / ``NO_COLOR`` / ``--no-color`` 下自动关掉**，否则
   重定向到文件就是一堆乱码转义符。
3. **Windows 终端默认不认 ANSI**，要手动开 VT 处理。
"""

from __future__ import annotations

import os
import sys
import unicodedata
from typing import Iterable, Sequence

from .model import Commit, CommitItem, Meal, ModOption, StagedItem, Store

# --------------------------------------------------------------------------- #
# 颜色
# --------------------------------------------------------------------------- #

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"


class Palette:
    """一个可开关的调色板。关掉之后所有方法原样返回文本。"""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.enabled and text else text

    def bold(self, text: str) -> str:
        return self._wrap(BOLD, text)

    def dim(self, text: str) -> str:
        return self._wrap(DIM, text)

    def red(self, text: str) -> str:
        return self._wrap(RED, text)

    def green(self, text: str) -> str:
        return self._wrap(GREEN, text)

    def yellow(self, text: str) -> str:
        return self._wrap(YELLOW, text)

    def blue(self, text: str) -> str:
        return self._wrap(BLUE, text)

    def magenta(self, text: str) -> str:
        return self._wrap(MAGENTA, text)

    def cyan(self, text: str) -> str:
        return self._wrap(CYAN, text)

    def add(self, text: str) -> str:
        return self.green(text)

    def delete(self, text: str) -> str:
        return self.red(text)


def enable_windows_ansi() -> None:
    """在 Windows 控制台上打开 ANSI 转义支持。失败就静默放过。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle_id in (-11, -12):  # STDOUT, STDERR
            handle = kernel32.GetStdHandle(handle_id)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # noqa: BLE001 - 开不了就算了，不该让 CLI 挂在这
        pass


def make_palette(no_color: bool = False, stream: object | None = None) -> Palette:
    stream = stream or sys.stdout
    if no_color or os.environ.get("NO_COLOR"):
        return Palette(False)
    if not getattr(stream, "isatty", lambda: False)():
        return Palette(False)
    if os.environ.get("TERM") == "dumb":
        return Palette(False)
    return Palette(True)


# --------------------------------------------------------------------------- #
# 宽度计算
# --------------------------------------------------------------------------- #


def display_width(text: str) -> int:
    """终端里的真实列宽。汉字/全角符号算 2。"""
    width = 0
    for ch in strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def strip_ansi(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\033":
            while i < len(text) and text[i] not in "mK":
                i += 1
            i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def pad(text: str, width: int, align: str = "left") -> str:
    """按**显示宽度**补齐。``pad("巨无霸", 10)`` 会补 6 个空格而不是 7 个。"""
    gap = max(0, width - display_width(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def truncate(text: str, width: int, ellipsis: str = "…") -> str:
    if display_width(text) <= width:
        return text
    budget = width - display_width(ellipsis)
    out = ""
    for ch in text:
        if display_width(out + ch) > budget:
            break
        out += ch
    return out + ellipsis


# --------------------------------------------------------------------------- #
# 组件
# --------------------------------------------------------------------------- #

RULE_WIDTH = 62


def rule(palette: Palette, char: str = "─") -> str:
    return palette.dim(char * RULE_WIDTH)


def yuan(fen: int) -> str:
    return f"¥{fen / 100:.2f}"


def money(palette: Palette, fen: int, signed: bool = False) -> str:
    text = yuan(fen)
    if not signed:
        return text
    if fen > 0:
        return palette.green(f"+{text}")
    if fen < 0:
        return palette.red(f"-{yuan(-fen)}")
    return text


def status_line(palette: Palette, label: str, value: str) -> str:
    return f"{pad(palette.dim(label), 16)} {value}"


def header(palette: Palette, title: str, subtitle: str = "") -> str:
    lines = [palette.bold(title)]
    if subtitle:
        lines.append(palette.dim(subtitle))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #


def render_diff_item(
    palette: Palette,
    item: StagedItem,
    *,
    price_fen: int | None = None,
    show_header: bool = True,
) -> list[str]:
    """渲染一道餐品的特调改动，git diff 风格。"""
    lines: list[str] = []
    name = item.meal.name

    if show_header:
        lines.append(palette.bold(f"diff --mcdgit a/{name} b/{name}"))
        lines.append(palette.red(f"--- a/{name}"))
        lines.append(palette.green(f"+++ b/{name}"))

    if not item.changes:
        lines.append(palette.dim("  (无特调改动)"))
    else:
        lines.append(palette.cyan(f"@@ {name} 特调 @@"))
        for change in item.changes:
            prefix = "-" if change.qty == 0 else "+"
            body = f"  {change.spec}"
            suffix = ""
            if change.ignored:
                suffix = palette.dim("   ← .mcdgitignore")
            elif change.price_fen:
                suffix = palette.dim(f"   {yuan(change.price_fen)}")
            rendered = f"{prefix}{body}{suffix}"
            lines.append(palette.delete(rendered) if prefix == "-" else palette.add(rendered))

    if price_fen is not None and item.quantity >= 1:
        actual = item.quantity * price_fen
        menu = item.quantity * item.meal.price_fen
        line = palette.dim(f"  ×{item.quantity} → ") + palette.bold(yuan(actual))
        if menu and menu != actual:
            # query-meals 给的是**促销价**，calculate-price 给的是**实付价**，
            # 两者经常不一样（实测见过 49.90 vs 117.50）。以 calculate-price 为准，
            # 但要把落差写出来，不能让用户以为被加价了。
            line += palette.yellow(f"   （菜单价 {yuan(menu)}，以实付为准）")
        lines.append(line)
    return lines


def render_diff(
    palette: Palette,
    items: Sequence[StagedItem],
    *,
    prices: dict[str, int] | None = None,
    total_fen: int = 0,
    original_fen: int = 0,
    discount_fen: int = 0,
) -> str:
    """``mcdgit diff`` 的完整输出。

    ``prices`` 是 ``{productCode: 券后小计(分)}``——由 ``calculate-price`` 得来。
    一定要从外面传进来：在这里重算会丢掉单价行（踩过）。
    """
    if not items:
        return palette.dim("没有改动。`mcdgit add <餐品>` 先加点东西进去。")

    prices = prices or {}
    lines: list[str] = []
    for index, item in enumerate(items):
        if index:
            lines.append("")
        lines.extend(
            render_diff_item(
                palette, item, price_fen=prices.get(item.meal.code, item.meal.price_fen)
            )
        )

    insertions = 0
    deletions = 0
    for item in items:
        for change in item.changes:
            if change.qty == 0:
                deletions += 1
            else:
                insertions += 1
        if not item.changes:
            insertions += 1

    files = len(items)
    lines.append("")
    lines.append(rule(palette))
    summary = (
        f" {files} file{'s' if files != 1 else ''} changed, "
        f"{insertions} insertion{'s' if insertions != 1 else ''}(+), "
        f"{deletions} deletion{'s' if deletions != 1 else ''}(-)"
    )
    lines.append(palette.dim(summary))
    if total_fen:
        tail = f" 合计 {palette.bold(yuan(total_fen))}"
        if original_fen and original_fen != total_fen:
            tail += palette.dim(f"（原价 {yuan(original_fen)}）")
        if discount_fen:
            tail += "  " + palette.green(f"优惠 -{yuan(discount_fen)}")
        lines.append(tail)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def render_status(
    palette: Palette,
    *,
    store: Store | None,
    branch: str,
    staged: Sequence[StagedItem],
    staged_total_fen: int,
    head: Commit | None,
    take_way: str,
    ignored: Sequence[str] = (),
) -> str:
    lines = [palette.bold(f"On branch {branch}")]
    if store is not None:
        lines.append(status_line(palette, "remote origin", store.label))
        lines.append(status_line(palette, "storeCode", store.store_code))
    lines.append(status_line(palette, "取餐方式", take_way))
    if head is not None:
        lines.append(
            status_line(
                palette,
                "HEAD",
                f"{palette.yellow(head.sha)} {head.subject}"
                + palette.dim(f"  ({head.short_date})"),
            )
        )
    else:
        lines.append(status_line(palette, "HEAD", palette.dim("(还没有任何提交)")))

    lines.append("")

    if not staged:
        lines.append("nothing to commit, working tree clean")
        if head is None:
            lines.append(palette.dim("  提示：`mcdgit fetch` 看菜单，`mcdgit add <餐品>` 加进暂存区。"))
        return "\n".join(lines)

    lines.append(palette.bold("Changes to be committed:"))
    lines.append(palette.dim('  (use "mcdgit reset <餐品>" to unstage)'))
    for item in staged:
        name = pad(item.meal.name, 26)
        marks = len(item.changes)
        detail = palette.dim(f"×{item.quantity}") + "  " + yuan(item.subtotal_fen)
        if marks:
            detail += palette.yellow(f"  [特调 ×{marks}]")
        lines.append(palette.green(f"        新点：  ") + name + detail)

    lines.append("")
    lines.append(status_line(palette, "待提交合计", palette.bold(yuan(staged_total_fen))))
    if ignored:
        lines.append(status_line(palette, ".mcdgitignore", palette.dim("、".join(ignored))))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# log
# --------------------------------------------------------------------------- #


def compute_lanes(commits: Sequence[Commit]) -> list[tuple[list[str], str, str]]:
    """给每条提交算出提交图要画的字符。

    返回 ``[(提交行之前要补的连线, 首行前缀, 续行前缀), ...]``，与 ``commits`` 一一对应。

    这是一个简化但**正确**的车道（lane）算法：

    - ``lanes[i]`` = "第 i 条车道正在等哪个 sha"
    - 提交落在某条车道上，把该车道替换成它的第一个 parent；多出来的 parent
      占用一条空闲车道——merge 就是在这里分叉的
    - 当多条车道同时等同一个 sha 时（两条分支从同一个 base 分出来，合并时
      就会这样），**必须把多余车道清掉**，否则同一个 sha 会挂在两三条车道上，
      图会一直往下画 `| |` 收不住，并且那个 sha 之后再也匹配不上

    车道宽度固定 2 字符，所以带中文的分支名不会把图挤歪。
    """
    lanes: list[str | None] = []
    rows: list[tuple[list[str], str, str]] = []

    for commit in commits:
        waiting = [i for i, slot in enumerate(lanes) if slot == commit.sha]

        before: list[str] = []
        if len(waiting) > 1:
            idx = waiting[0]
            closing = waiting[1:]
            before.append(_connector(lanes, idx, closing))
            for extra in closing:
                lanes[extra] = None
        elif waiting:
            idx = waiting[0]
        else:
            idx = next((i for i, slot in enumerate(lanes) if slot is None), len(lanes))
            if idx == len(lanes):
                lanes.append(commit.sha)
            else:
                lanes[idx] = commit.sha

        head = _draw(lanes, star=idx)

        parents = list(commit.parents)
        lanes[idx] = parents[0] if parents else None
        for extra_parent in parents[1:]:
            if extra_parent in lanes:
                continue
            slot = next((i for i, value in enumerate(lanes) if value is None), len(lanes))
            if slot == len(lanes):
                lanes.append(extra_parent)
            else:
                lanes[slot] = extra_parent

        body = _draw(lanes)
        rows.append((before, head, body))

    return rows


def _draw(lanes: Sequence[str | None], star: int = -1) -> str:
    """把车道状态画成一行前缀。``star`` 指定的车道画 ``*``。

    尾部空车道会被裁掉——不裁的话，一条已经并回主干的分支会让后面每一行
    都多背几个空格，图会很虚。
    """
    text = "".join(
        "* " if i == star else ("| " if slot else "  ")
        for i, slot in enumerate(lanes)
    )
    while len(text) > 2 and text.endswith("  "):
        text = text[:-2]
    return text


def _connector(lanes: Sequence[str | None], keep: int, closing: Sequence[int]) -> str:
    """画 ``|/`` —— 表示右边的车道并回左边那条。

    这一段对齐方式和其它行不同，是有意的：git 自己也这么画。
    ``|/`` 两个字符就覆盖了两条车道，比一格一格画短，看起来才像"汇合"。
    """
    width = max([keep, *closing]) + 1
    chars: list[str] = []
    for i in range(width):
        if i == keep:
            chars.append("|")
        elif i in closing:
            chars.append("/")
        elif lanes[i]:
            chars.append("|")
        else:
            chars.append(" ")
    return "".join(chars)


def render_log(
    palette: Palette,
    commits: Sequence[Commit],
    *,
    oneline: bool = False,
    graph: bool = False,
    stat: bool = False,
    branch: str = "",
    tags: dict[str, list[str]] | None = None,
) -> str:
    """``mcdgit log``。``--graph`` 会画 ASCII 提交图。"""
    if not commits:
        return palette.dim("还没有提交。你的麦门历史一片空白。")

    tags = tags or {}
    rows = compute_lanes(commits) if graph else [([], "", "")] * len(commits)
    lines: list[str] = []

    for index, commit in enumerate(commits):
        before, first, cont = rows[index]
        pad_head = first if graph else ""
        pad_body = cont if graph else "    "
        lines.extend(before)

        decoration = ""
        refs = []
        if index == 0 and branch:
            refs.append(f"HEAD -> {branch}")
        refs.extend(f"tag: {name}" for name in tags.get(commit.sha, []))
        # 其他分支指针
        for name, sha in (tags.get("__branches__") or []):  # pragma: no cover
            _ = (name, sha)
        if refs:
            decoration = palette.cyan(f" ({', '.join(refs)})")

        merge_mark = palette.yellow(" (merge)") if len(commit.parents) > 1 else ""

        if oneline:
            lines.append(
                f"{pad_head}{palette.yellow(commit.sha)}{decoration}{merge_mark} "
                f"{commit.subject}"
            )
        else:
            lines.append(f"{pad_head}{palette.yellow('commit ' + commit.sha)}{decoration}")
            lines.append(f"{pad_body}Author: {commit.author}")
            lines.append(f"{pad_body}Date:   {commit.committed_at}")
            lines.append(f"{pad_body}")
            for text in commit.message.splitlines() or [""]:
                lines.append(f"{pad_body}    {text}")
            lines.append(f"{pad_body}")

        # --oneline 与真 git 一致：一条提交一行，不展开明细（要明细加 --stat）
        for item in commit.items if (stat or not oneline) else []:
            mark = palette.yellow("~") if item.changes else palette.green("+")
            body = f"{pad_body}  {mark} {item.name}"
            if item.quantity > 1:
                body += palette.dim(f" ×{item.quantity}")
            body += palette.dim(f"  {yuan(item.subtotal_fen)}")
            lines.append(body)
            for state in item.changes:
                sym = palette.red("-") if state.qty == 0 else palette.green("+")
                note = palette.dim("   ← .mcdgitignore") if state.ignored else ""
                lines.append(f"{pad_body}      {sym} {state.spec}{note}")

        if not oneline or stat:
            files = len(commit.items)
            total = palette.dim(
                f"    {files} item{'s' if files != 1 else ''}, "
                f"{commit.item_count} 份, 合计 "
            ) + palette.bold(yuan(commit.total_fen))
            lines.append(f"{pad_body}  {total}")

        if index != len(commits) - 1 and not graph:
            lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# blame
# --------------------------------------------------------------------------- #


def render_blame(
    palette: Palette,
    keyword: str,
    events: Sequence[tuple[Commit, str]],
    *,
    total_commits: int,
) -> str:
    """``mcdgit blame <配料>`` —— 追溯一个配料在你历史上的来去。

    ``events`` 是 ``(commit, action)``，action 取值 ``引入`` / ``移除`` / ``加量``。
    """
    lines = [palette.bold(f"blame: {keyword}") , palette.dim(f"扫描 {total_commits} 次提交"), ""]
    if not events:
        lines.append(f"  {palette.green('✓')} 你的历史里从来没有出现过「{keyword}」。")
        lines.append(palette.dim("    这是自律，还是你根本没吃过？"))
        return "\n".join(lines)

    for commit, action in events:
        if action == "移除":
            mark = palette.red("-")
            colour = palette.red
        elif action == "加量":
            mark = palette.green("++")
            colour = palette.green
        elif action == "加回":
            mark = palette.yellow("+")
            colour = palette.yellow
        else:
            mark = palette.green("+")
            colour = palette.green
        lines.append(
            f"{mark} {palette.yellow(commit.sha)}  {palette.dim(commit.short_date)}  "
            f"{colour(action)} {keyword}"
            + palette.dim(f"   「{commit.subject}」")
        )

    # 最后一个动作如果是"又加回来了"，补一刀
    if events[-1][1] == "加回":
        lines.append("")
        lines.append(palette.dim("  ← 上次你明明删掉了，这次又是为什么？"))
    elif len(events) >= 2 and events[-1][1] == "移除" and events[-2][1] in ("引入", "加回"):
        lines.append("")
        lines.append(palette.dim("  ← 终于想通了。"))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 门店列表
# --------------------------------------------------------------------------- #


def render_stores(palette: Palette, stores: Sequence[Store], keyword: str = "") -> str:
    if not stores:
        return palette.dim(f"没有找到门店{('：' + keyword) if keyword else ''}。")
    lines = []
    for index, store in enumerate(stores, start=1):
        name = pad(truncate(store.store_name or store.store_code, 30), 32)
        lines.append(
            f"  {palette.yellow(str(index) + '.')} {palette.bold(name)}"
            f"{palette.dim(store.store_code)}"
        )
        if store.address:
            lines.append(f"      {palette.dim(store.address)}")
    return "\n".join(lines)


def render_menu(palette: Palette, meals: Iterable[Meal], keyword: str = "", limit: int = 40) -> str:
    selected = [
        meal
        for meal in meals
        if not keyword or keyword.lower() in meal.name.lower() or keyword in meal.code
    ]
    if not selected:
        return palette.dim(f"菜单里没有匹配「{keyword}」的餐品。")

    lines = []
    for meal in selected[:limit]:
        name = pad(truncate(meal.name, 30), 32)
        price = palette.dim(f"{meal.price:>7}")
        flag = palette.cyan(" [可特调]") if meal.support_modify else ""
        lines.append(f"  {name}{price}{flag}")
    if len(selected) > limit:
        lines.append(palette.dim(f"  … 还有 {len(selected) - limit} 条，用更具体的关键词过滤"))
    return "\n".join(lines)


def render_mod_options(palette: Palette, meal: Meal) -> str:
    if not meal.mods:
        return palette.dim(f"{meal.name} 不支持特调。")
    lines = [palette.bold(f"{meal.name} 的特调清单"), ""]
    for option in meal.mods:
        check = palette.green("[x]") if option.selected else palette.dim("[ ]")
        name = pad(truncate(option.name, 20), 22)
        extra = palette.dim(f"    删掉: mcdgit rm {option.name}")
        lines.append(f"  {check} {name}{extra}")
    return "\n".join(lines)


def render_commit_result(
    palette: Palette,
    commit: Commit,
    *,
    branch: str,
    files: int,
    insertions: int,
    deletions: int,
) -> str:
    lines = [
        palette.bold(
            f"[{branch} {palette.yellow(commit.sha)}] {commit.subject}"
        ),
        palette.dim(
            f" {files} file{'s' if files != 1 else ''} changed, "
            f"{insertions} insertion{'s' if insertions != 1 else ''}(+), "
            f"{deletions} deletion{'s' if deletions != 1 else ''}(-)"
        ),
    ]
    for item in commit.items:
        lines.append(f"   {palette.green('+')} {item.name}" + palette.dim(f"  {yuan(item.subtotal_fen)}"))
        for change in item.changes:
            sym = palette.red("-") if change.qty == 0 else palette.green("+")
            note = palette.dim("  ← .mcdgitignore") if change.ignored else ""
            lines.append(f"       {sym} {change.spec}{note}")
    lines.append("")
    lines.append(
        status_line(palette, "订单号", palette.bold(commit.order_id))
        + palette.dim("   ← 就是上面那个 hash")
    )
    lines.append(status_line(palette, "合计", palette.bold(yuan(commit.total_fen))))
    return "\n".join(lines)


def render_help_hint(palette: Palette, text: str) -> str:
    return palette.dim(text)
