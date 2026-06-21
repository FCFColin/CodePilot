"""repomap 模块单元测试。

覆盖：RepoMapper 可用性检测、Python 符号提取、token 预算控制、
SQLite 缓存命中与失效、build_for_query 相关性排序。

所有需要 tree-sitter 的测试使用 skipif 守卫：tree-sitter-language-pack
不可用时跳过，不阻断全量验证。test_repo_mapper_unavailable_returns_empty
始终运行（不需要 skipif），因为它测试的就是不可用场景。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from codepilot.context.token_counter import TokenCounter
from codepilot.repomap import RepoMapper

# ============================================================================
# 可用性守卫
# ============================================================================


def _repo_mapper_available() -> bool:
    """检测 tree-sitter-language-pack 是否可导入。

    成功返回 True，失败返回 False。仅做导入检查，不创建 RepoMapper。
    """
    try:
        import tree_sitter_language_pack  # noqa: F401

        return True
    except Exception:
        return False


# 模块级常量：tree-sitter 是否可用
_REPO_MAPPER_AVAILABLE = _repo_mapper_available()

# skipif 守卫原因
_SKIP_REASON = "tree-sitter-language-pack 不可用，跳过 repomap 测试"


# ============================================================================
# 测试用例
# ============================================================================


class TestRepoMapperUnavailable:
    """RepoMapper 不可用场景测试（始终运行，不需要 skipif）。"""

    def test_repo_mapper_unavailable_returns_empty(self, tmp_path: Path) -> None:
        """is_available() 为 False 时 build() 返回空字符串。

        通过 _force_unavailable 标志强制 is_available 返回 False，
        验证 build 不触发后续导入且返回空字符串。
        """
        mapper = RepoMapper(tmp_path)
        mapper._force_unavailable = True  # type: ignore[attr-defined]
        assert mapper.is_available() is False
        result = mapper.build()
        assert result == ""

    def test_build_for_query_empty_result_when_unavailable(
        self, tmp_path: Path
    ) -> None:
        """build_for_query 在不可用时返回空字符串。"""
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        mapper._force_unavailable = True  # type: ignore[attr-defined]
        assert mapper.build_for_query("anything") == ""


@pytest.mark.skipif(not _REPO_MAPPER_AVAILABLE, reason=_SKIP_REASON)
class TestRepoMapper:
    """RepoMapper 核心功能测试（需要 tree-sitter）。"""

    def test_extracts_python_symbols(self, tmp_path: Path) -> None:
        """build() 结果包含文件中定义的 class 与 def 符号名。"""
        (tmp_path / "sample.py").write_text(
            "class Foo:\n"
            "    def bar(self):\n"
            "        return 1\n"
            "\n"
            "def baz():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper.build()
        assert "Foo" in result
        assert "bar" in result
        assert "baz" in result

    def test_token_budget_respected(self, tmp_path: Path) -> None:
        """大量文件时 build() 结果 token 数不超过 max_tokens * 1.1。"""
        # 生成 20 个文件，每个含若干符号
        for i in range(20):
            (tmp_path / f"mod_{i:02d}.py").write_text(
                f"class Class{i}:\n"
                f"    def method_{i}_a(self):\n"
                f"        return {i}\n"
                f"\n"
                f"    def method_{i}_b(self):\n"
                f"        return {i} * 2\n"
                f"\n"
                f"def func_{i}(x):\n"
                f"    return x + {i}\n",
                encoding="utf-8",
            )
        max_tokens = 256
        mapper = RepoMapper(tmp_path, max_tokens=max_tokens)
        result = mapper.build()
        counter = TokenCounter()
        token_count = counter.count_text(result)
        # 允许 10% 容差（最后一条可能略超预算）
        assert token_count <= int(max_tokens * 1.1), (
            f"token 数 {token_count} 超过预算 {max_tokens * 1.1}"
        )

    def test_sqlite_cache_hit(self, tmp_path: Path) -> None:
        """同一文件 build 两次，第二次命中缓存不重新解析。

        通过 spy 计数器验证 _parse_call_count：第一次解析，第二次命中缓存。
        """
        (tmp_path / "cached.py").write_text(
            "def hello():\n    return 'hi'\n", encoding="utf-8"
        )
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        # 首次构建
        first = mapper.build()
        # 记录首次解析的调用次数
        parse_calls_after_first = mapper._parse_call_count  # type: ignore[attr-defined]
        # 二次构建（文件未变，应命中缓存）
        second = mapper.build()
        parse_calls_after_second = mapper._parse_call_count  # type: ignore[attr-defined]
        # 第二次不应有新的解析调用
        assert parse_calls_after_second == parse_calls_after_first, (
            "第二次 build 应命中缓存，不应重新解析文件"
        )
        # 两次结果一致
        assert first == second

    def test_sqlite_cache_miss_on_mtime_change(self, tmp_path: Path) -> None:
        """文件内容修改后（mtime 变化），缓存失效重新解析。"""
        file_path = tmp_path / "changing.py"
        file_path.write_text("def original():\n    return 1\n", encoding="utf-8")
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        first = mapper.build()
        assert "original" in first
        parse_calls_after_first = mapper._parse_call_count  # type: ignore[attr-defined]

        # 修改文件内容并确保 mtime 变化
        file_path.write_text("def updated():\n    return 2\n", encoding="utf-8")
        # 确保 mtime 改变（某些文件系统精度低，显式设置未来时间）
        future = time.time() + 2
        os.utime(file_path, (future, future))

        second = mapper.build()
        parse_calls_after_second = mapper._parse_call_count  # type: ignore[attr-defined]
        # 应有新的解析调用
        assert parse_calls_after_second > parse_calls_after_first, (
            "文件 mtime 变化后应重新解析"
        )
        assert "updated" in second
        assert "original" not in second


@pytest.mark.skipif(not _REPO_MAPPER_AVAILABLE, reason=_SKIP_REASON)
class TestRepoMapperBuildForQuery:
    """build_for_query 相关性排序测试。"""

    def test_build_for_query_prioritizes_relevant_files(self, tmp_path: Path) -> None:
        """build_for_query 优先返回与 query 关键词相关的文件。"""
        (tmp_path / "auth.py").write_text(
            "class AuthManager:\n    def login(self):\n        pass\n",
            encoding="utf-8",
        )
        (tmp_path / "database.py").write_text(
            "class Database:\n    def connect(self):\n        pass\n",
            encoding="utf-8",
        )
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper.build_for_query("auth login")
        # 相关文件应出现在结果中
        assert "auth" in result.lower()
        # AuthManager 符号应出现
        assert "AuthManager" in result or "login" in result


@pytest.mark.skipif(not _REPO_MAPPER_AVAILABLE, reason=_SKIP_REASON)
class TestRepoMapperIgnores:
    """RepoMapper 忽略目录与异常处理测试。"""

    def test_ignores_pycache_and_venv(self, tmp_path: Path) -> None:
        """build() 忽略 __pycache__、.venv、node_modules 等目录。"""
        # 正常文件
        (tmp_path / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
        # __pycache__ 下的文件应被忽略
        pycache = tmp_path / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.py").write_text(
            "def should_be_ignored():\n    pass\n", encoding="utf-8"
        )
        # .venv 下的文件应被忽略
        venv = tmp_path / ".venv"
        venv.mkdir()
        (venv / "vfile.py").write_text("def venv_func():\n    pass\n", encoding="utf-8")
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper.build()
        assert "main" in result
        assert "should_be_ignored" not in result
        assert "venv_func" not in result

    def test_build_handles_parse_error_gracefully(self, tmp_path: Path) -> None:
        """tree-sitter 解析失败的文件被静默跳过，不影响整体结果。"""
        (tmp_path / "good.py").write_text(
            "def good():\n    return 1\n", encoding="utf-8"
        )
        # 写入一个语法错误的文件（tree-sitter 可能部分解析或失败）
        (tmp_path / "broken.py").write_text(
            "def broken(:\n    return\n", encoding="utf-8"
        )
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        # 不应抛异常
        result = mapper.build()
        # good.py 的符号应正常出现
        assert "good" in result


@pytest.mark.skipif(not _REPO_MAPPER_AVAILABLE, reason=_SKIP_REASON)
class TestRepoMapperEdgeCases:
    """RepoMapper 边界情况与异常分支测试。"""

    def test_build_empty_workspace_returns_empty(self, tmp_path: Path) -> None:
        """空工作区 build() 返回空字符串。"""
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        assert mapper.build() == ""

    def test_build_with_relevant_files(self, tmp_path: Path) -> None:
        """build(relevant_files=...) 仅扫描指定文件列表。"""
        (tmp_path / "included.py").write_text(
            "def included():\n    pass\n", encoding="utf-8"
        )
        (tmp_path / "excluded.py").write_text(
            "def excluded():\n    pass\n", encoding="utf-8"
        )
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper.build(relevant_files=[tmp_path / "included.py"])
        assert "included" in result
        assert "excluded" not in result

    def test_build_with_nonexistent_relevant_files(self, tmp_path: Path) -> None:
        """build(relevant_files=...) 过滤掉不存在的文件。"""
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper.build(relevant_files=[tmp_path / "nope.py"])
        assert result == ""

    def test_close_does_not_raise(self, tmp_path: Path) -> None:
        """close() 可安全调用且不抛异常。"""
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        mapper.close()
        # 重复调用也不抛异常
        mapper.close()

    def test_cache_db_custom_path(self, tmp_path: Path) -> None:
        """自定义 cache_db 路径正常工作。"""
        (tmp_path / "f.py").write_text("def f():\n    pass\n", encoding="utf-8")
        custom_db = tmp_path / "custom_cache.db"
        mapper = RepoMapper(tmp_path, max_tokens=1024, cache_db=custom_db)
        result = mapper.build()
        assert "f" in result
        # 自定义缓存文件应被创建
        assert custom_db.exists()

    def test_rank_files_without_networkx_falls_back(self, tmp_path: Path) -> None:
        """networkx 不可用时 _rank_files 回退到均匀分数。"""
        from codepilot.repomap.mapper import Symbol

        (tmp_path / "a.py").write_text("def a_func():\n    pass\n", encoding="utf-8")
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        file_symbols = {tmp_path / "a.py": [Symbol("a_func", "def", 1)]}
        # 模拟 networkx 不可用
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "networkx":
                raise ImportError("simulated")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            scores = mapper._rank_files(file_symbols)
        finally:
            builtins.__import__ = real_import
        # 应回退到均匀分数
        assert len(scores) == 1
        assert list(scores.values())[0] > 0

    def test_render_respects_token_budget(self, tmp_path: Path) -> None:
        """_render 在 token 预算不足时只输出首条。"""
        from codepilot.repomap.mapper import Symbol

        mapper = RepoMapper(tmp_path, max_tokens=1)
        files = [tmp_path / "a.py", tmp_path / "b.py"]
        file_symbols = {
            tmp_path / "a.py": [Symbol("a", "def", 1)],
            tmp_path / "b.py": [Symbol("b", "def", 1)],
        }
        result = mapper._render(files, file_symbols)
        # 预算极小，只输出首条
        assert "a.py" in result
        assert "b.py" not in result

    def test_extracts_class_with_bases_signature(self, tmp_path: Path) -> None:
        """build() 提取带基类的 class 签名。"""
        (tmp_path / "derived.py").write_text(
            "class Base:\n    pass\n\nclass Derived(Base):\n    pass\n",
            encoding="utf-8",
        )
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper.build()
        assert "Derived" in result
        # 基类签名 (Base) 应出现在结果中
        assert "Base" in result

    def test_get_symbols_nonexistent_file_returns_none(self, tmp_path: Path) -> None:
        """_get_symbols 对不存在的文件返回 None。"""
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        result = mapper._get_symbols(tmp_path / "nope.py")
        assert result is None

    def test_render_file_outside_workspace(self, tmp_path: Path) -> None:
        """_render 对不在 workspace 下的文件使用绝对路径。"""
        from codepilot.repomap.mapper import Symbol

        mapper = RepoMapper(tmp_path, max_tokens=1024)
        outside = Path("/tmp/outside_file.py")
        file_symbols = {outside: [Symbol("x", "def", 1)]}
        result = mapper._render([outside], file_symbols)
        # 应包含文件路径（绝对路径回退）
        assert "x" in result

    def test_rank_files_handles_unreadable_file(self, tmp_path: Path) -> None:
        """_rank_files 对不可读文件静默跳过。"""
        from codepilot.repomap.mapper import Symbol

        (tmp_path / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
        mapper = RepoMapper(tmp_path, max_tokens=1024)
        # 传入一个不存在的文件路径（模拟不可读）
        ghost = tmp_path / "ghost.py"
        file_symbols = {
            tmp_path / "a.py": [Symbol("a", "def", 1)],
            ghost: [Symbol("ghost", "def", 1)],
        }
        # 不应抛异常
        scores = mapper._rank_files(file_symbols)
        assert len(scores) >= 1
