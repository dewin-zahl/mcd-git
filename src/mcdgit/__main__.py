"""``python -m mcdgit`` 入口。顺手把 Windows 控制台切到 UTF-8。"""

from __future__ import annotations

import sys


def _force_utf8() -> None:
    """Windows 控制台默认 GBK，遇到 '' 之类的字符会直接抛 UnicodeEncodeError。

    这个坑在第一次跑 ``mcdgit menu`` 时就会撞上，所以在最早的入口处解决。
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


_force_utf8()

from .cli import main  # noqa: E402  （必须在重配编码之后导入）

if __name__ == "__main__":
    raise SystemExit(main())
