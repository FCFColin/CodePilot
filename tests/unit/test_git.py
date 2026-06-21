"""git 模块单元测试。

覆盖：GitManager 仓库检测、自动提交、撤销、脏文件检测；
CommitMessageGenerator 规则生成与长度限制。
全部使用 tmp_path fixture 和 subprocess 初始化真实 git 仓库，禁止 mock git 命令。
"""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from codepilot.git import CommitMessageGenerator, GitManager
from codepilot.providers.base import BaseProvider, Message, TextDelta

# ============================================================================
# 辅助函数
# ============================================================================


def _init_git_repo(repo_path: Path) -> None:
    """在指定路径初始化真实 git 仓库并配置 user.name/user.email。

    Args:
        repo_path: 仓库根目录路径。
    """
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )


def _git_log_oneline(repo_path: Path) -> str:
    """返回 git log --oneline 的输出文本。"""
    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# ============================================================================
# GitManager 仓库检测测试
# ============================================================================


class TestGitManagerDetect:
    """GitManager 仓库检测测试。"""

    def test_detect_git_repo(self, tmp_path: Path) -> None:
        """在 git init 的目录中 is_git_repo 返回 True。"""
        _init_git_repo(tmp_path)
        manager = GitManager(tmp_path)
        assert manager.is_git_repo() is True

    def test_detect_non_git_dir(self, tmp_path: Path) -> None:
        """在空目录中 is_git_repo 返回 False。"""
        manager = GitManager(tmp_path)
        assert manager.is_git_repo() is False


# ============================================================================
# GitManager 自动提交测试
# ============================================================================


class TestGitManagerAutoCommit:
    """GitManager auto_commit 测试。"""

    def test_auto_commit_success(self, tmp_path: Path) -> None:
        """auto_commit 成功后 git log 存在以 [codepilot] 开头的提交，返回 8 位 hash。"""
        _init_git_repo(tmp_path)
        file_path = tmp_path / "foo.py"
        file_path.write_text("print('hello')\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        commit_hash = manager.auto_commit("add foo.py", [file_path])

        assert commit_hash is not None
        assert len(commit_hash) == 8
        log_output = _git_log_oneline(tmp_path)
        assert "[codepilot]" in log_output

    def test_auto_commit_adds_prefix(self, tmp_path: Path) -> None:
        """传入不带前缀的 message，实际提交信息自动加 [codepilot] 前缀。"""
        _init_git_repo(tmp_path)
        file_path = tmp_path / "bar.py"
        file_path.write_text("x = 1\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        manager.auto_commit("add bar.py", [file_path])

        log_output = _git_log_oneline(tmp_path)
        # log --oneline 格式: "<hash> <message>"
        # 提取提交信息部分
        message_line = log_output.strip().split("\n", 1)[0]
        # 去掉 hash 部分（前 8 字符 + 空格）
        commit_message = message_line.split(" ", 1)[1] if " " in message_line else ""
        assert commit_message.startswith("[codepilot]")

    def test_auto_commit_no_repo(self, tmp_path: Path) -> None:
        """非 git 目录中 auto_commit 返回 None 且不抛异常。"""
        file_path = tmp_path / "baz.py"
        file_path.write_text("y = 2\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        result = manager.auto_commit("add baz.py", [file_path])
        assert result is None


# ============================================================================
# GitManager 撤销提交测试
# ============================================================================


class TestGitManagerUndo:
    """GitManager undo_last_commit 测试。"""

    def test_undo_codepilot_commit(self, tmp_path: Path) -> None:
        """auto_commit 后 undo_last_commit 返回 (True, msg)，git log 验证已回滚。"""
        _init_git_repo(tmp_path)
        file_path = tmp_path / "undo_test.py"
        file_path.write_text("z = 3\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        manager.auto_commit("add undo_test.py", [file_path])

        # 撤销前存在 1 条提交
        log_before = _git_log_oneline(tmp_path)
        assert "[codepilot]" in log_before

        success, message = manager.undo_last_commit()
        assert success is True
        assert isinstance(message, str)

        # 撤销后 git log 应为空（无提交）
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        # 无提交时 git log 返回非零退出码且 stderr 包含提示
        assert result.returncode != 0 or "[codepilot]" not in result.stdout

    def test_undo_non_codepilot_commit(self, tmp_path: Path) -> None:
        """手动执行非 [codepilot] 提交，undo_last_commit 返回 (False, ...)。"""
        _init_git_repo(tmp_path)
        file_path = tmp_path / "manual.py"
        file_path.write_text("manual = True\n", encoding="utf-8")

        # 手动提交（不带 [codepilot] 前缀）
        subprocess.run(
            ["git", "add", "manual.py"], cwd=tmp_path, capture_output=True, check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "manual commit by user"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        manager = GitManager(tmp_path)
        success, message = manager.undo_last_commit()
        assert success is False
        assert isinstance(message, str)


# ============================================================================
# GitManager 脏文件检测测试
# ============================================================================


class TestGitManagerDirtyFiles:
    """GitManager get_dirty_files 测试。"""

    def test_get_dirty_files(self, tmp_path: Path) -> None:
        """创建未提交文件后 get_dirty_files 包含该文件路径。"""
        _init_git_repo(tmp_path)
        dirty_file = tmp_path / "dirty.py"
        dirty_file.write_text("dirty = True\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        dirty_files = manager.get_dirty_files()

        # 应包含刚创建的文件（路径可能是相对或绝对）
        dirty_names = [Path(f).name for f in dirty_files]
        assert "dirty.py" in dirty_names


# ============================================================================
# CommitMessageGenerator 测试
# ============================================================================


class TestCommitMessageGenerator:
    """CommitMessageGenerator 规则生成测试。"""

    def test_commit_message_generator_rules(self) -> None:
        """generate('add foo.py') 返回以 [codepilot] 开头的字符串。"""
        generator = CommitMessageGenerator()
        message = generator.generate("add foo.py")
        assert isinstance(message, str)
        assert message.startswith("[codepilot]")

    def test_commit_message_length(self) -> None:
        """超长 diff 摘要生成的提交信息不超过 72 字符。"""
        generator = CommitMessageGenerator()
        # 构造超长 diff 摘要
        long_files = [f"very_long_file_name_{i}.py" for i in range(20)]
        long_summary = "add " + ", ".join(long_files)
        message = generator.generate(long_summary, max_length=72)
        assert isinstance(message, str)
        assert len(message) <= 72

    def test_commit_message_modify_action(self) -> None:
        """generate 识别 modify 关键词。"""
        generator = CommitMessageGenerator()
        message = generator.generate("modify config.py")
        assert message.startswith("[codepilot]")
        assert "modify" in message

    def test_commit_message_delete_action(self) -> None:
        """generate 识别 delete 关键词。"""
        generator = CommitMessageGenerator()
        message = generator.generate("delete old_file.py")
        assert message.startswith("[codepilot]")
        assert "delete" in message

    def test_commit_message_no_files(self) -> None:
        """generate 无文件名时使用摘要本身。"""
        generator = CommitMessageGenerator()
        message = generator.generate("update something")
        assert message.startswith("[codepilot]")
        assert "update something" in message


# ============================================================================
# CommitMessageGenerator LLM 生成测试
# ============================================================================


class _MockProvider(BaseProvider):
    """测试用 mock provider，返回固定文本。"""

    def __init__(self, response_text: str = "[codepilot] add test.py") -> None:
        self._response_text = response_text

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[Any]:
        yield TextDelta(text=self._response_text)

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> Any:
        return {"role": role, "tool_call_id": tool_call_id, "content": content}

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[Any],
    ) -> Any:
        return {"role": "assistant", "content": text}


class _ErrorProvider(BaseProvider):
    """测试用 mock provider，抛出异常。"""

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[Any]:
        raise RuntimeError("provider error")
        yield  # 让 Python 识别为 async generator

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> Any:
        return {"role": role, "tool_call_id": tool_call_id, "content": content}

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[Any],
    ) -> Any:
        return {"role": "assistant", "content": text}


class TestCommitMessageGeneratorLLM:
    """CommitMessageGenerator LLM 生成测试。"""

    async def test_generate_from_llm_success(self) -> None:
        """generate_from_llm 成功时返回 LLM 生成的信息。"""
        generator = CommitMessageGenerator()
        provider = _MockProvider("[codepilot] add test.py")
        message = await generator.generate_from_llm(provider, "add test.py")
        assert message.startswith("[codepilot]")

    async def test_generate_from_llm_no_prefix(self) -> None:
        """LLM 输出无前缀时自动添加。"""
        generator = CommitMessageGenerator()
        provider = _MockProvider("add new feature")
        message = await generator.generate_from_llm(provider, "add new feature")
        assert message.startswith("[codepilot]")

    async def test_generate_from_llm_too_long(self) -> None:
        """LLM 输出超长时截断到 72 字符。"""
        generator = CommitMessageGenerator()
        long_text = "[codepilot] " + "x" * 100
        provider = _MockProvider(long_text)
        message = await generator.generate_from_llm(provider, "long change")
        assert len(message) <= 72

    async def test_generate_from_llm_error_fallback(self) -> None:
        """LLM 调用失败时回退到规则生成。"""
        generator = CommitMessageGenerator()
        provider = _ErrorProvider()
        message = await generator.generate_from_llm(provider, "add test.py")
        assert message.startswith("[codepilot]")


# ============================================================================
# GitManager 边界测试
# ============================================================================


class TestGitManagerEdgeCases:
    """GitManager 边界情况测试。"""

    def test_auto_commit_empty_paths(self, tmp_path: Path) -> None:
        """auto_commit 传入空路径列表返回 None。"""
        _init_git_repo(tmp_path)
        manager = GitManager(tmp_path)
        result = manager.auto_commit("add nothing", [])
        assert result is None

    def test_auto_commit_with_prefix(self, tmp_path: Path) -> None:
        """auto_commit 传入已带前缀的 message 不重复添加。"""
        _init_git_repo(tmp_path)
        file_path = tmp_path / "prefixed.py"
        file_path.write_text("x = 1\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        manager.auto_commit("[codepilot] add prefixed.py", [file_path])

        log_output = _git_log_oneline(tmp_path)
        # 不应出现重复前缀
        assert "[codepilot] [codepilot]" not in log_output
        assert "[codepilot]" in log_output

    def test_undo_no_repo(self, tmp_path: Path) -> None:
        """非 git 仓库中 undo_last_commit 返回 (False, ...)。"""
        manager = GitManager(tmp_path)
        success, message = manager.undo_last_commit()
        assert success is False
        assert isinstance(message, str)

    def test_get_dirty_files_no_repo(self, tmp_path: Path) -> None:
        """非 git 仓库中 get_dirty_files 返回空列表。"""
        manager = GitManager(tmp_path)
        dirty_files = manager.get_dirty_files()
        assert dirty_files == []

    def test_undo_no_commits(self, tmp_path: Path) -> None:
        """git 仓库无提交时 undo_last_commit 返回 (False, ...)。"""
        _init_git_repo(tmp_path)
        manager = GitManager(tmp_path)
        success, message = manager.undo_last_commit()
        assert success is False
        assert isinstance(message, str)

    def test_auto_commit_no_changes(self, tmp_path: Path) -> None:
        """auto_commit 对无变更的文件返回 None（git commit 失败）。"""
        _init_git_repo(tmp_path)
        file_path = tmp_path / "nochange.py"
        file_path.write_text("x = 1\n", encoding="utf-8")

        manager = GitManager(tmp_path)
        # 第一次提交成功
        first = manager.auto_commit("add nochange.py", [file_path])
        assert first is not None
        # 第二次提交相同内容（无变更）应返回 None
        second = manager.auto_commit("add nochange.py again", [file_path])
        assert second is None
