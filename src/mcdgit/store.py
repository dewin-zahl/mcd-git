"""本地仓库。

心智模型（这个项目的全部设计都压在这一句上）：

    本地 = 工作区，MCP = 远端。

所以 ``add`` / ``status`` / ``branch`` / ``stash`` 全部只碰 ``.mcdgit/``，
一次网络请求都不发；只有 ``fetch`` / ``commit`` / ``log`` / ``push`` 才去找 MCP。

目录结构刻意照着真 git 抄：

    .mcdgit/
    ├── config.json          门店、取餐方式
    ├── HEAD                 "ref: refs/heads/main"
    ├── index.json           暂存区
    ├── cache.json           最近一次 fetch 到的菜单
    ├── objects/ab/cdef.json 提交对象
    ├── refs/heads/main      分支指向的 sha
    └── refs/tags/v1.0       标签
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .errors import McdGitError, NotARepoError
from .model import Commit, Meal, Store, StagedItem

REPO_DIR = ".mcdgit"
IGNORE_FILE = ".mcdgitignore"
DEFAULT_BRANCH = "main"

#: 出厂默认的 ``.mcdgitignore``。按真 gitignore 的脾气，默认是空的——
#: 我们只放注释，不替用户决定他讨厌什么。你可以改这两个地方，但大多数人
#: 不会改第二个。
DEFAULT_IGNORE = """\
# .mcdgitignore —— 永不进入提交的配料
#
# 语法和 .gitignore 一样，但这里忽略的是"配料"，不是"文件"。
# 匹配规则比真 gitignore 宽松一点：大小写不敏感、支持 * 通配、
# 并且子串命中（写"洋葱"，"洋葱粒"也会被忽略）。
#
# 写在这里的配料，会在下单时被翻译成
# modification.values[].key = unselectedKey（数量 0）。
# 这个文件是真的会生效的，不只是写着好看。
#
# 想临时吃一次：mcdgit add 巨无霸 --mod 洋葱粒=1
#
# 下面这些是常见写法，取消注释即可：
# 洋葱
# 酸黄瓜
# 香菜
"""


class Repo:
    """一个 mcdgit 仓库。所有磁盘读写的唯一入口。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.git_dir = root / REPO_DIR

    # ------------------------------------------------------------------ #
    # 发现与创建
    # ------------------------------------------------------------------ #

    @classmethod
    def discover(cls, start: Path | str | None = None) -> Repo:
        """从 ``start`` 往上找 ``.mcdgit/``，跟 git 一个脾气。"""
        current = Path(start or os.getcwd()).resolve()
        for candidate in [current, *current.parents]:
            if (candidate / REPO_DIR).is_dir():
                return cls(candidate)
        raise NotARepoError(str(current))

    @classmethod
    def init(cls, root: Path | str | None = None) -> Repo:
        repo = cls(Path(root or os.getcwd()).resolve())
        if repo.git_dir.exists():
            raise McdGitError(f"Reinitialized existing mcdgit repository in {repo.git_dir}")
        (repo.git_dir / "objects").mkdir(parents=True)
        (repo.git_dir / "refs" / "heads").mkdir(parents=True)
        (repo.git_dir / "refs" / "tags").mkdir(parents=True)
        repo._write_text(repo.git_dir / "HEAD", f"ref: refs/heads/{DEFAULT_BRANCH}\n")
        repo.save_index([])
        repo.save_cache({})
        ignore_path = repo.root / IGNORE_FILE
        if not ignore_path.exists():
            ignore_path.write_text(DEFAULT_IGNORE, encoding="utf-8")
        return repo

    # ------------------------------------------------------------------ #
    # 基础 IO
    # ------------------------------------------------------------------ #

    @staticmethod
    def _read_json(path: Path, default: Any = None) -> Any:
        if not path.is_file():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise McdGitError(f"{path.name} 损坏（不是合法 JSON）：{exc}") from exc

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # ------------------------------------------------------------------ #
    # config
    # ------------------------------------------------------------------ #

    @property
    def config_path(self) -> Path:
        return self.git_dir / "config.json"

    def load_config(self) -> dict[str, Any]:
        return self._read_json(self.config_path, {}) or {}

    def save_config(self, data: dict[str, Any]) -> None:
        self._write_json(self.config_path, data)

    @property
    def store(self) -> Store | None:
        raw = self.load_config().get("store")
        return Store.from_dict(raw) if raw else None

    def require_store(self) -> Store:
        store = self.store
        if store is None:
            raise McdGitError(
                "no remote configured\n"
                "        先 `mcdgit init`（或 `mcdgit remote add <门店>`）选定一家店。"
            )
        return store

    def set_store(self, store: Store) -> None:
        config = self.load_config()
        config["store"] = store.to_dict()
        self.save_config(config)

    @property
    def take_way(self) -> str:
        return self.load_config().get("take_way", "eat-in")

    @take_way.setter
    def take_way(self, value: str) -> None:
        config = self.load_config()
        config["take_way"] = value
        self.save_config(config)

    @property
    def author(self) -> str:
        """提交作者。默认取系统用户名——你的 commit 上面得有人名。"""
        config = self.load_config()
        return config.get("author") or os.environ.get("MCDGIT_AUTHOR") or _default_author()

    # ------------------------------------------------------------------ #
    # HEAD / refs
    # ------------------------------------------------------------------ #

    @property
    def head_path(self) -> Path:
        return self.git_dir / "HEAD"

    def head_ref(self) -> str:
        raw = self.head_path.read_text(encoding="utf-8").strip() if self.head_path.is_file() else ""
        if raw.startswith("ref: "):
            return raw[5:].strip()
        return raw or f"refs/heads/{DEFAULT_BRANCH}"

    @property
    def branch(self) -> str:
        ref = self.head_ref()
        return ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else "(detached)"

    def set_branch(self, name: str) -> None:
        self._write_text(self.head_path, f"ref: refs/heads/{name}\n")

    def _ref_path(self, ref: str) -> Path:
        return self.git_dir / ref

    def read_ref(self, ref: str) -> str | None:
        path = self._ref_path(ref)
        if not path.is_file():
            return None
        value = path.read_text(encoding="utf-8").strip()
        return value or None

    def write_ref(self, ref: str, sha: str) -> None:
        self._write_text(self._ref_path(ref), sha + "\n")

    def head_sha(self) -> str | None:
        return self.read_ref(self.head_ref())

    def head_commit(self) -> Commit | None:
        sha = self.head_sha()
        return self.load_commit(sha) if sha else None

    def branches(self) -> dict[str, str]:
        result: dict[str, str] = {}
        base = self.git_dir / "refs" / "heads"
        if base.is_dir():
            for path in sorted(base.iterdir()):
                if path.is_file():
                    result[path.name] = path.read_text(encoding="utf-8").strip()
        return result

    def tags(self) -> dict[str, str]:
        result: dict[str, str] = {}
        base = self.git_dir / "refs" / "tags"
        if base.is_dir():
            for path in sorted(base.iterdir()):
                if path.is_file():
                    result[path.name] = path.read_text(encoding="utf-8").strip()
        return result

    def create_branch(self, name: str, sha: str | None = None) -> None:
        if name in self.branches():
            raise McdGitError(f"a branch named '{name}' already exists")
        self.write_ref(f"refs/heads/{name}", sha or self.head_sha() or "")

    # ------------------------------------------------------------------ #
    # 对象
    # ------------------------------------------------------------------ #

    def _object_path(self, sha: str) -> Path:
        return self.git_dir / "objects" / sha[:2] / f"{sha[2:]}.json"

    def save_commit(self, commit: Commit) -> None:
        self._write_json(self._object_path(commit.sha), commit.to_dict())

    def load_commit(self, sha: str) -> Commit | None:
        data = self._read_json(self._object_path(sha))
        return Commit.from_dict(data) if data else None

    def require_commit(self, sha: str) -> Commit:
        commit = self.load_commit(sha)
        if commit is None:
            raise McdGitError(
                f"unknown revision '{sha}'\n"
                f"        本地没有这条提交。`mcdgit fetch` 试试，或者 `mcdgit log` 看看你有什么。"
            )
        return commit

    def resolve(self, revision: str) -> str:
        """把 ``HEAD`` / 分支名 / sha 前缀 / 标签 解析成一个完整 sha。"""
        if not revision or revision == "HEAD":
            sha = self.head_sha()
            if not sha:
                raise McdGitError("HEAD 还没有指向任何提交——`mcdgit commit` 一次再说。")
            return sha
        if revision in self.tags():
            return self.tags()[revision]
        if revision in self.branches():
            return self.branches()[revision]
        for sha in self.iter_commit_shas():
            if sha.startswith(revision):
                return sha
        raise McdGitError(f"unknown revision '{revision}'")

    def iter_commit_shas(self) -> Iterator[str]:
        base = self.git_dir / "objects"
        if not base.is_dir():
            return
        for shard in sorted(base.iterdir()):
            if not shard.is_dir():
                continue
            for path in sorted(shard.iterdir()):
                if path.suffix == ".json":
                    yield shard.name + path.stem

    def all_commits(self) -> list[Commit]:
        commits = [self.load_commit(sha) for sha in self.iter_commit_shas()]
        found = [c for c in commits if c is not None]
        found.sort(key=lambda c: c.committed_at, reverse=True)
        return found

    def history(self, start: str | None = None) -> list[Commit]:
        """从 ``start``（默认 HEAD）沿 **所有** parents 往回走。

        不能只跟第一父提交：merge commit 的第二个 parent 就是被合并进来的
        那条分支，漏掉它的话提交图上会出现一个没有下文的 ``|\\``。
        顺序按提交时间倒序，和真 git 的默认行为一致。
        """
        start_sha = start or self.head_sha()
        if not start_sha:
            return []
        seen: set[str] = set()
        out: list[Commit] = []
        stack = [start_sha]
        while stack:
            sha = stack.pop()
            if not sha or sha in seen:
                continue
            seen.add(sha)
            commit = self.load_commit(sha)
            if commit is None:
                continue
            out.append(commit)
            stack.extend(commit.parents)
        out.sort(key=lambda c: (c.committed_at, c.sha), reverse=True)
        return out

    def tag_map(self) -> dict[str, list[str]]:
        """sha -> [tag, ...]，给 ``log`` 渲染 ``(tag: v1.0)`` 用。"""
        out: dict[str, list[str]] = {}
        for name, sha in self.tags().items():
            out.setdefault(sha, []).append(name)
        return out

    # ------------------------------------------------------------------ #
    # 暂存区
    # ------------------------------------------------------------------ #

    @property
    def index_path(self) -> Path:
        return self.git_dir / "index.json"

    def load_index(self) -> list[StagedItem]:
        raw = self._read_json(self.index_path, []) or []
        return [StagedItem.from_dict(item) for item in raw]

    def save_index(self, items: list[StagedItem]) -> None:
        self._write_json(self.index_path, [item.to_dict() for item in items])

    def clear_index(self) -> None:
        self.save_index([])

    # ------------------------------------------------------------------ #
    # 菜单缓存（fetch 的产物）
    # ------------------------------------------------------------------ #

    @property
    def cache_path(self) -> Path:
        return self.git_dir / "cache.json"

    def load_cache(self) -> dict[str, Any]:
        return self._read_json(self.cache_path, {}) or {}

    def save_cache(self, data: dict[str, Any]) -> None:
        data = dict(data)
        data["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        self._write_json(self.cache_path, data)

    def cached_meals(self) -> dict[str, Meal]:
        raw = self.load_cache().get("meals", {})
        out: dict[str, Meal] = {}
        for code, meal in raw.items():
            data = dict(meal)
            data.pop("code", None)
            out[code] = Meal(code=code, **data)
        return out

    # ------------------------------------------------------------------ #
    # stash
    # ------------------------------------------------------------------ #

    @property
    def stash_path(self) -> Path:
        return self.git_dir / "stash.json"

    def load_stash(self) -> list[dict[str, Any]]:
        return self._read_json(self.stash_path, []) or []

    def save_stash(self, entries: list[dict[str, Any]]) -> None:
        self._write_json(self.stash_path, entries)

    # ------------------------------------------------------------------ #
    # .mcdgitignore
    # ------------------------------------------------------------------ #

    def ignore_patterns(self) -> list[str]:
        path = self.root / IGNORE_FILE
        if not path.is_file():
            return []
        patterns: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
        return patterns

    def is_ignored(self, name: str) -> bool:
        """配料是否命中 ``.mcdgitignore``。

        匹配规则比真 gitignore 宽松：大小写不敏感、支持 ``*`` 通配、
        并且**子串命中**（写"洋葱"，"洋葱圈"也会被忽略——这是故意的）。
        """
        target = name.strip().lower()
        for pattern in self.ignore_patterns():
            needle = pattern.lower()
            if fnmatch.fnmatch(target, needle) or needle in target:
                return True
        return False

    # ------------------------------------------------------------------ #
    # 杂项
    # ------------------------------------------------------------------ #

    def short_sha(self, order_id: str) -> str:
        """订单号 → commit hash。

        默认取订单号前 7 位——所以 ``mcdgit log`` 看到的 hash，就是麦当劳系统里的
        订单号，不是哈希出来的东西。

        但有个真实存在的坑：麦当劳订单号**共享一个很长的前缀**
        （``1030108270000795607354880945`` 这种），不同单可能前 7 位完全一样。
        所以这里做了碰撞退避：撞了就往前多取几位，实在不行才退回真哈希。
        宁可 hash 丑一点，也不能让两条提交指向同一个对象。
        """
        cleaned = "".join(ch for ch in order_id if ch.isalnum())
        if cleaned:
            length = 7
            while length <= len(cleaned):
                candidate = cleaned[:length]
                if not self._object_path(candidate).exists():
                    return candidate
                length += 1
        digest = hashlib.sha1(order_id.encode("utf-8")).hexdigest()
        return digest[:7]

    def state_summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "branch": self.branch,
            "head": self.head_sha(),
            "staged": len(self.load_index()),
            "store": self.store.label if self.store else None,
        }

    def destroy(self) -> None:
        """删掉 ``.mcdgit/``。只给测试用。"""
        if self.git_dir.exists():
            shutil.rmtree(self.git_dir)


def _default_author() -> str:
    for var in ("MCDGIT_AUTHOR", "GIT_AUTHOR_NAME", "USERNAME", "USER"):
        value = os.environ.get(var)
        if value:
            return value
    return "mcdgit-user"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
