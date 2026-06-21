"""RepoMap 生成器。

基于 tree-sitter 解析工作区源文件，提取类与函数符号，用 networkx
构建文件间引用图并运行 PageRank 排序，生成紧凑的仓库结构摘要文本。

设计要点：
- 可选依赖：tree-sitter-language-pack 与 networkx 为可选依赖。
  is_available() 检测 tree-sitter 是否可导入；不可用时 build() 直接
  返回空字符串，不触发后续导入，不抛异常。
- SQLite 缓存：以「文件路径 + mtime」为键缓存解析结果，避免重复解析。
- token 预算：使用 TokenCounter 估算累计 token，超出 max_tokens 时停止追加。
- 异常静默：所有解析/图算法异常被捕获，返回已生成部分或空字符串。
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import structlog

from codepilot.context.token_counter import TokenCounter

logger = structlog.get_logger(__name__)

# 遍历时忽略的目录名
_IGNORED_DIRS: frozenset[str] = frozenset(
    {".git", "__pycache__", ".venv", "node_modules", "dist", ".codepilot"}
)

# tree-sitter 节点类型 → 符号 kind 映射
_SYMBOL_NODE_TYPES: dict[str, str] = {
    "class_definition": "class",
    "function_definition": "def",
    "method_definition": "def",
}


@dataclass
class Symbol:
    """提取到的符号信息。"""

    name: str
    kind: str  # "class" | "def"
    line: int
    signature: str = ""


class RepoMapper:
    """仓库结构摘要生成器。

    依赖注入：所有外部依赖通过构造函数传入；不依赖全局状态。
    可选依赖缺失时降级为空输出，不抛异常。
    """

    def __init__(
        self,
        workspace_root: Path,
        max_tokens: int = 1024,
        cache_db: Path | None = None,
    ) -> None:
        """初始化 RepoMapper。

        Args:
            workspace_root: 工作区根目录，扫描此目录下的源文件。
            max_tokens: 摘要文本的最大 token 预算。
            cache_db: SQLite 缓存文件路径；为 None 时使用
                workspace_root/.codepilot_cache/repomap.db。
        """
        self.workspace_root = workspace_root
        self.max_tokens = max_tokens
        if cache_db is None:
            cache_db = workspace_root / ".codepilot" / "repomap_cache.db"
        self.cache_db = cache_db
        self._token_counter = TokenCounter()
        # 测试钩子：强制标记为不可用（不触发真实导入）
        self._force_unavailable: bool = False
        # 解析调用计数（用于测试验证缓存命中）
        self._parse_call_count: int = 0
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    # ------------------------------------------------------------------
    # 可用性检测
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """检测 tree-sitter-language-pack 是否可导入。

        成功返回 True，失败返回 False；永不抛异常。
        """
        if self._force_unavailable:
            return False
        try:
            import tree_sitter_language_pack  # noqa: F401
        except Exception:  # pragma: no cover - tree-sitter 已安装时无法触发
            return False
        return True

    # ------------------------------------------------------------------
    # 公共构建 API
    # ------------------------------------------------------------------

    def build(self, relevant_files: list[Path] | None = None) -> str:
        """生成仓库结构摘要。

        Args:
            relevant_files: 指定扫描的文件列表；为 None 时遍历整个工作区。

        Returns:
            紧凑文本表示。tree-sitter 不可用时返回空字符串。
        """
        if not self.is_available():
            return ""
        try:
            return self._build_impl(relevant_files, query=None)
        except Exception as e:
            logger.warning("repomap build 失败", error=str(e))
            return ""

    def build_for_query(self, query: str) -> str:
        """生成与查询相关的仓库结构摘要。

        与 build 相同，但优先选取文件名或符号名与 query 关键词匹配的文件。

        Args:
            query: 用户查询文本。

        Returns:
            紧凑文本表示。tree-sitter 不可用时返回空字符串。
        """
        if not self.is_available():
            return ""
        try:
            return self._build_impl(None, query=query)
        except Exception as e:
            logger.warning("repomap build_for_query 失败", error=str(e))
            return ""

    # ------------------------------------------------------------------
    # 内部构建实现
    # ------------------------------------------------------------------

    def _build_impl(
        self,
        relevant_files: list[Path] | None,
        query: str | None,
    ) -> str:
        """实际构建逻辑。

        Args:
            relevant_files: 指定文件列表；为 None 时遍历工作区。
            query: 查询关键词；非 None 时按相关性排序。

        Returns:
            紧凑文本表示。
        """
        # 1. 发现文件
        if relevant_files is not None:
            files = [f for f in relevant_files if f.suffix == ".py" and f.exists()]
        else:
            files = self._discover_files()

        # 2. 解析每个文件（带缓存）
        file_symbols: dict[Path, list[Symbol]] = {}
        for f in files:
            syms = self._get_symbols(f)
            if syms is not None:
                file_symbols[f] = syms

        if not file_symbols:
            return ""

        # 3. 构建引用图并排序
        scores = self._rank_files(file_symbols)

        # 4. 文件排序
        if query:
            ordered = self._order_for_query(file_symbols, scores, query)
        else:
            ordered = sorted(
                file_symbols.keys(),
                key=lambda p: scores.get(p, 0.0),
                reverse=True,
            )

        # 5. 生成紧凑文本（受 token 预算约束）
        return self._render(ordered, file_symbols)

    def _discover_files(self) -> list[Path]:
        """遍历 workspace_root 下所有 .py 文件，忽略指定目录。"""
        files: list[Path] = []
        for root, dirs, filenames in os.walk(self.workspace_root):
            # 原地过滤目录，阻止向下遍历
            dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(Path(root) / fn)
        return files

    # ------------------------------------------------------------------
    # 符号提取与缓存
    # ------------------------------------------------------------------

    def _get_symbols(self, file_path: Path) -> list[Symbol] | None:
        """获取文件符号列表（带 SQLite 缓存）。

        缓存键为「文件路径 + mtime」；mtime 变化时缓存失效重新解析。

        Args:
            file_path: 源文件路径。

        Returns:
            符号列表；文件不可读时返回 None。
        """
        try:
            mtime = str(file_path.stat().st_mtime)
        except OSError:
            return None

        key = f"{file_path}|{mtime}"

        # 尝试命中缓存
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # 缓存未命中：实际解析
        self._parse_call_count += 1
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        symbols = self._parse_python(source)
        self._cache_put(key, symbols)
        return symbols

    def _parse_python(self, source: str) -> list[Symbol]:
        """使用 tree-sitter 解析 Python 源码，提取类与函数符号。

        解析失败时返回空列表（不抛异常）。
        """
        try:
            from tree_sitter_language_pack import get_parser
        except Exception as e:  # pragma: no cover - tree-sitter 已安装时无法触发
            logger.debug("tree-sitter 不可用", error=str(e))
            return []

        try:
            parser = get_parser("python")
            # 兼容不同版本 tree-sitter：新版接受 str，旧版接受 bytes
            try:
                tree = parser.parse(source)
            except TypeError:  # pragma: no cover - 当前 binding 接受 str
                tree = parser.parse(bytes(source, "utf-8"))  # type: ignore[arg-type]
        except Exception as e:
            logger.debug("tree-sitter 解析失败", error=str(e), file_len=len(source))
            return []

        symbols: list[Symbol] = []
        root = self._tree_root(tree)
        if root is not None:
            self._walk(root, symbols, source)
        return symbols

    @staticmethod
    def _tree_root(tree: Any) -> Any:
        """获取 tree 的根节点（兼容 root_node 属性与方法）。"""
        # Python binding：root_node 属性
        root = getattr(tree, "root_node", None)
        if root is not None and not callable(root):
            return root
        # C binding：root_node() 方法
        root_fn = getattr(tree, "root_node", None)
        if callable(root_fn):
            try:
                return root_fn()
            except Exception:
                return None
        return None

    def _walk(self, node: Any, symbols: list[Symbol], source: str) -> None:
        """递归遍历 AST，收集类与函数定义节点。

        兼容 tree-sitter 两种 Python binding：
        - Python binding：node.type / node.children 属性
        - C binding：node.kind() / node.child(i) 方法
        """
        kind = self._node_kind(node)
        if kind in _SYMBOL_NODE_TYPES:
            name_node = self._field_child(node, "name")
            if name_node is not None:
                name = self._node_text(name_node, source)
                sym_kind = _SYMBOL_NODE_TYPES[kind]
                line = self._node_start_row(node) + 1
                signature = self._extract_signature(node, source)
                symbols.append(
                    Symbol(
                        name=name,
                        kind=sym_kind,
                        line=line,
                        signature=signature,
                    )
                )
        for child in self._node_children(node):
            self._walk(child, symbols, source)

    def _extract_signature(self, node: Any, source: str) -> str:
        """提取符号签名（函数参数列表或类基类列表）。"""
        kind = self._node_kind(node)
        try:
            if kind == "function_definition":
                params = self._field_child(node, "parameters")
                if params is not None:
                    return self._node_text(params, source)
            elif kind == "class_definition":
                supers = self._field_child(node, "superclasses")
                if supers is not None:
                    return self._node_text(supers, source)
        except Exception:
            return ""
        return ""

    # ------------------------------------------------------------------
    # tree-sitter 节点兼容层（同时支持 Python binding 与 C binding）
    # ------------------------------------------------------------------

    @staticmethod
    def _node_kind(node: Any) -> str:
        """获取节点类型名（兼容 type 属性与 kind() 方法）。"""
        kind = getattr(node, "type", None)
        if isinstance(kind, str):
            return kind
        kind_fn = getattr(node, "kind", None)
        if callable(kind_fn):
            try:
                result = kind_fn()
                if isinstance(result, str):
                    return result
            except Exception:
                return ""
        return ""

    @staticmethod
    def _field_child(node: Any, field_name: str) -> Any:
        """按字段名获取子节点（child_by_field_name 在两种 binding 中均为方法）。"""
        fn = getattr(node, "child_by_field_name", None)
        if not callable(fn):
            return None
        try:
            return fn(field_name)
        except Exception:
            return None

    @staticmethod
    def _node_children(node: Any) -> list[Any]:
        """获取子节点列表（兼容 children 属性与 child(i) 方法）。"""
        # Python binding：children 属性返回列表
        children = getattr(node, "children", None)
        if isinstance(children, list):
            return children
        # C binding：child_count() + child(i)
        count_fn = getattr(node, "child_count", None)
        child_fn = getattr(node, "child", None)
        if callable(count_fn) and callable(child_fn):
            try:
                count = count_fn()
                return [child_fn(i) for i in range(int(count))]
            except Exception:
                return []
        return []

    @staticmethod
    def _node_start_row(node: Any) -> int:
        """获取节点起始行号（0-based）。

        兼容 start_point 属性（元组）与 start_position() 方法（Point 对象）。
        """
        # Python binding：start_point 返回 (row, col) 元组
        sp = getattr(node, "start_point", None)
        if isinstance(sp, tuple) and sp:
            return int(sp[0])
        # C binding：start_position() 返回 Point 对象
        pos_fn = getattr(node, "start_position", None)
        if callable(pos_fn):
            try:
                pos = pos_fn()
                row = getattr(pos, "row", None)
                if isinstance(row, int):
                    return row
                # Point 可能是元组
                if isinstance(pos, tuple) and pos:
                    return int(pos[0])
            except Exception:
                return 0
        return 0

    @staticmethod
    def _node_text(node: Any, source: str) -> str:
        """安全提取 tree-sitter 节点文本。

        优先用 start_byte/end_byte 从 source 切片（C binding），
        其次用 node.text 属性（Python binding，可能为 bytes/str）。
        """
        # C binding：用字节范围从 source 切片
        start_fn = getattr(node, "start_byte", None)
        end_fn = getattr(node, "end_byte", None)
        if callable(start_fn) and callable(end_fn):
            try:
                start = int(start_fn())
                end = int(end_fn())
                return source[start:end]
            except Exception:  # pragma: no cover - 字节切片不会失败
                pass
        # Python binding：node.text 属性
        text = getattr(node, "text", None)  # pragma: no cover - C binding 走字节切片
        if isinstance(text, bytes):  # pragma: no cover
            return text.decode("utf-8", errors="replace")
        if isinstance(text, str):  # pragma: no cover
            return text
        return ""  # pragma: no cover

    # ------------------------------------------------------------------
    # 引用图与排序
    # ------------------------------------------------------------------

    def _rank_files(self, file_symbols: dict[Path, list[Symbol]]) -> dict[Path, float]:
        """构建文件间引用图并运行 PageRank。

        若文件 A 定义的符号在文件 B 内容中被引用，则添加 A -> B 的边。
        networkx 不可用或图算法失败时回退到均匀分数。
        """
        try:
            import networkx as nx
        except Exception as e:
            logger.debug("networkx 不可用，回退均匀排序", error=str(e))
            count = max(len(file_symbols), 1)
            return {f: 1.0 / count for f in file_symbols}

        graph: Any = nx.DiGraph()
        for f in file_symbols:
            graph.add_node(f)

        # 符号名 → 定义该符号的文件列表
        name_to_files: dict[str, list[Path]] = {}
        for f, syms in file_symbols.items():
            for s in syms:
                name_to_files.setdefault(s.name, []).append(f)

        # 扫描每个文件内容，发现符号引用并建边
        for f in file_symbols:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for name, def_files in name_to_files.items():
                if name in content:
                    for df in def_files:
                        if df != f:
                            graph.add_edge(df, f)

        try:
            scores = nx.pagerank(graph)
            return dict(scores)
        except Exception as e:
            logger.debug("pagerank 失败，回退均匀排序", error=str(e))
            count = max(len(file_symbols), 1)
            return {f: 1.0 / count for f in file_symbols}

    def _order_for_query(
        self,
        file_symbols: dict[Path, list[Symbol]],
        scores: dict[Path, float],
        query: str,
    ) -> list[Path]:
        """按与 query 的相关性排序文件（相关度高的在前）。

        相关性：文件名匹配 +2，符号名匹配 +1；同相关度按 PageRank 排序。
        """
        query_lower = query.lower()
        keywords = [w for w in query_lower.split() if w]

        def relevance(f: Path) -> tuple[int, float]:
            score = scores.get(f, 0.0)
            rel = 0
            fname = f.name.lower()
            for kw in keywords:
                if kw in fname:
                    rel += 2
            for s in file_symbols.get(f, []):
                for kw in keywords:
                    if kw in s.name.lower():
                        rel += 1
            return (rel, score)

        return sorted(file_symbols.keys(), key=relevance, reverse=True)

    # ------------------------------------------------------------------
    # 文本渲染
    # ------------------------------------------------------------------

    def _render(
        self,
        ordered: list[Path],
        file_symbols: dict[Path, list[Symbol]],
    ) -> str:
        """按顺序生成紧凑文本，受 max_tokens 预算约束。

        累计 token 将超过 max_tokens 时停止追加（首条始终输出）。
        """
        lines: list[str] = []
        current_tokens = 0

        for f in ordered:
            syms = file_symbols.get(f, [])
            # 计算相对路径（更紧凑）
            try:
                rel = f.relative_to(self.workspace_root)
                path_str = str(rel)
            except ValueError:
                path_str = str(f)

            entry_lines = [path_str]
            for s in syms:
                entry_lines.append(f"  {s.kind} {s.name}{s.signature}: ...")
            entry = "\n".join(entry_lines) + "\n"

            entry_tokens = self._token_counter.count_text(entry)
            # 预算控制：已有内容且追加后将超出预算则停止
            if lines and current_tokens + entry_tokens > self.max_tokens:
                break
            lines.append(entry)
            current_tokens += entry_tokens

        return "".join(lines)

    # ------------------------------------------------------------------
    # SQLite 缓存
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """初始化 SQLite 缓存数据库（失败静默）。"""
        try:
            self.cache_db.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.cache_db))
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS symbols ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("repomap 缓存初始化失败", error=str(e))
            self._conn = None

    def _cache_get(self, key: str) -> list[Symbol] | None:
        """从缓存读取符号列表。"""
        if self._conn is None:
            return None
        try:
            cursor = self._conn.execute(
                "SELECT value FROM symbols WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            if row is None:
                return None
            data = json.loads(row[0])
            return [Symbol(**item) for item in data]
        except Exception as e:
            logger.debug("缓存读取失败", error=str(e))
            return None

    def _cache_put(self, key: str, symbols: list[Symbol]) -> None:
        """写入缓存。"""
        if self._conn is None:
            return
        try:
            payload = json.dumps([asdict(s) for s in symbols])
            self._conn.execute(
                "INSERT OR REPLACE INTO symbols (key, value) VALUES (?, ?)",
                (key, payload),
            )
            self._conn.commit()
        except Exception as e:
            logger.debug("缓存写入失败", error=str(e))

    def close(self) -> None:
        """关闭缓存连接。"""
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()
            self._conn = None


__all__ = ["RepoMapper", "Symbol"]
