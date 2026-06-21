"""hooks 模块单元测试。

覆盖：LintHook 文件 lint 检测、HookRegistry 触发顺序与首个重试优先、
AgentLoop 集成（lint 错误触发重试，修复后正常完成）。
使用 tmp_path 隔离文件系统，structlog.testing.capture_logs() 验证日志，
mock provider 模拟 LLM 响应。

遵循 TDD：本文件先于 src/codepilot/hooks/ 实现编写，运行时应因模块不存在而失败。
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from codepilot.agent.loop import AgentLoop
from codepilot.config import ContextConfig
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.hooks import (
    BaseHook,
    GitCommitHook,
    HookEvent,
    HookRegistry,
    HookResult,
    LintHook,
)
from codepilot.providers.base import Done, TextDelta, ToolCall
from codepilot.tools.file_write import WriteFileTool
from codepilot.tools.registry import ToolRegistry

# ============================================================================
# 辅助函数与 Mock
# ============================================================================


def _ruff_available() -> bool:
    """检测当前环境是否可运行 python -m ruff。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class _MockUI:
    """收集 UI 事件用于断言的最简 mock。"""

    def __init__(self) -> None:
        self.tool_results: list[tuple[str, str, bool]] = []

    async def on_text_delta(self, text: str) -> None:
        pass

    async def on_thinking_delta(self, text: str) -> None:
        pass

    async def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        pass

    async def on_tool_result(self, name: str, result: str, success: bool) -> None:
        self.tool_results.append((name, result, success))

    async def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        pass

    async def on_error(self, error: str) -> None:
        pass

    async def on_turn_end(self) -> None:
        pass


class _ScriptedProvider:
    """按预设序列返回 AgentEvent 的 mock provider。

    每次 chat() 调用按顺序消费 event_sequences 中的一个序列。
    """

    def __init__(self, event_sequences: list[list[Any]]) -> None:
        self.event_sequences = event_sequences
        self.call_count: int = 0

    async def chat(
        self,
        messages: list[Any],
        tools: list[dict[str, Any]] | None = None,
        system_prompt: str = "",
        stream: bool = True,
    ) -> AsyncIterator[Any]:
        idx = min(self.call_count, len(self.event_sequences) - 1)
        events = self.event_sequences[idx]
        self.call_count += 1
        for event in events:
            yield event

    def format_assistant_message(
        self,
        text: str,
        tool_calls: list[ToolCall],
    ) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in tool_calls
            ]
        return msg

    def format_tool_result(
        self,
        role: str,
        tool_call_id: str,
        content: str,
    ) -> dict[str, Any]:
        return {
            "role": role,
            "tool_call_id": tool_call_id,
            "content": content,
        }


def _make_context_manager() -> ContextManager:
    """创建测试用 ContextManager（短配置，无压缩器）。"""
    config = ContextConfig()
    token_counter = TokenCounter()
    return ContextManager(
        config=config,
        token_counter=token_counter,
        compressor=None,
        system_prompt="",
    )


# ============================================================================
# LintHook 测试
# ============================================================================


class TestLintHook:
    """LintHook 单元测试。"""

    def test_lint_hook_clean_file(self, tmp_path: Path) -> None:
        """无错误的 Python 文件 LintHook 返回 should_retry=False。"""
        if not _ruff_available():
            pytest.skip("ruff 不可用，跳过")

        clean_file = tmp_path / "clean.py"
        clean_file.write_text("x = 1\nprint(x)\n", encoding="utf-8")

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(clean_file),
            "result": "File written",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False
        assert result["retry_message"] is None

    def test_lint_hook_error_file(self, tmp_path: Path) -> None:
        """有 ruff 错误的 Python 文件 LintHook 返回 should_retry=True。

        retry_message 应包含行号和错误码。
        """
        if not _ruff_available():
            pytest.skip("ruff 不可用，跳过")

        # 未使用的 import 会触发 F401；未定义变量会触发 F821
        bad_file = tmp_path / "bad.py"
        bad_file.write_text(
            "import os\n"  # 未使用的 import → F401
            "undefined_var = nonexistent_name\n",  # 未定义变量 → F821
            encoding="utf-8",
        )

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(bad_file),
            "result": "File written",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["should_retry"] is True
        assert result["retry_message"] is not None
        # retry_message 应包含行号和错误码
        assert "第" in result["retry_message"]
        assert "行" in result["retry_message"]
        # ruff 错误码格式为 F401/F821 等
        assert "（F" in result["retry_message"] or "(F" in result["retry_message"]

    def test_lint_hook_non_python_file(self, tmp_path: Path) -> None:
        """非 Python 文件 LintHook 直接返回 success=True，should_retry=False。"""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("hello world\n", encoding="utf-8")

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(txt_file),
            "result": "File written",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_ruff_not_found_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ruff 不可用时 LintHook 不抛异常，返回 should_retry=False，并 log warning。"""
        py_file = tmp_path / "sample.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        # 通过覆盖 sys.executable 让 python -m ruff 找不到模块
        # 使用一个不存在的 python 解释器路径触发 FileNotFoundError
        original_executable = sys.executable

        def _fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise FileNotFoundError("ruff not found")

        monkeypatch.setattr("codepilot.hooks.builtin.subprocess.run", _fake_run)

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(py_file),
            "result": "File written",
        }

        with structlog.testing.capture_logs() as cap_logs:
            result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        # ruff 不可用：不应触发重试
        assert result["should_retry"] is False
        # 应有 warning 级别日志
        warnings = [e for e in cap_logs if e["log_level"] == "warning"]
        assert len(warnings) >= 1

        # 恢复（monkeypatch 会自动恢复，这里只是显式记录）
        assert original_executable


# ============================================================================
# HookRegistry 测试
# ============================================================================


class _SideEffectHook(BaseHook):
    """记录调用顺序的测试钩子。"""

    def __init__(self, name_str: str, call_log: list[str], retry: bool = False) -> None:
        self._name = name_str
        self._call_log = call_log
        self._retry = retry

    def name(self) -> str:
        return self._name

    def on_event(self, event: HookEvent, context: dict[str, Any]) -> HookResult:
        self._call_log.append(self._name)
        if self._retry:
            return HookResult(
                success=True,
                output="",
                should_retry=True,
                retry_message=f"{self._name} retry",
            )
        return HookResult(
            success=True,
            output="",
            should_retry=False,
            retry_message=None,
        )


class TestHookRegistry:
    """HookRegistry 触发顺序与首个重试优先测试。"""

    def test_hook_registry_trigger_order(self) -> None:
        """注册两个钩子，trigger 按注册顺序调用两个。"""
        call_log: list[str] = []
        hook1 = _SideEffectHook("hook1", call_log)
        hook2 = _SideEffectHook("hook2", call_log)

        registry = HookRegistry()
        registry.register(hook1)
        registry.register(hook2)

        results = registry.trigger(HookEvent.TOOL_CALL_AFTER, {})

        assert len(results) == 2
        assert call_log == ["hook1", "hook2"]

    def test_hook_registry_first_retry_wins(self) -> None:
        """两个钩子都返回 should_retry=True，trigger_tool_after 返回第一个。"""
        call_log: list[str] = []
        hook1 = _SideEffectHook("hook1", call_log, retry=True)
        hook2 = _SideEffectHook("hook2", call_log, retry=True)

        registry = HookRegistry()
        registry.register(hook1)
        registry.register(hook2)

        result = registry.trigger_tool_after("write_file", "/tmp/x.py", "ok")

        assert result is not None
        assert isinstance(result, dict)
        assert result["should_retry"] is True
        assert result["retry_message"] == "hook1 retry"
        # 两个钩子都被调用
        assert call_log == ["hook1", "hook2"]

    def test_hook_registry_no_retry_returns_none(self) -> None:
        """所有钩子都不重试时 trigger_tool_after 返回 None。"""
        call_log: list[str] = []
        hook1 = _SideEffectHook("hook1", call_log, retry=False)

        registry = HookRegistry()
        registry.register(hook1)

        result = registry.trigger_tool_after("write_file", "/tmp/x.py", "ok")

        assert result is None
        assert call_log == ["hook1"]

    def test_hook_registry_trigger_other_event(self) -> None:
        """trigger 对非 TOOL_CALL_AFTER 事件也正常调用钩子。"""
        call_log: list[str] = []
        hook1 = _SideEffectHook("hook1", call_log)

        registry = HookRegistry()
        registry.register(hook1)

        results = registry.trigger(HookEvent.SESSION_START, {})

        assert len(results) == 1
        assert call_log == ["hook1"]

    def test_hook_registry_exception_isolated(self) -> None:
        """单个钩子抛异常时记录 warning 并跳过，不影响其他钩子。"""
        call_log: list[str] = []

        class _ErrorHook(BaseHook):
            def __init__(self, name_str: str, log: list[str]) -> None:
                self._name = name_str
                self._log = log

            def name(self) -> str:
                return self._name

            def on_event(self, event: HookEvent, context: dict[str, Any]) -> HookResult:
                self._log.append(self._name)
                raise RuntimeError("hook error")

        hook1 = _ErrorHook("error_hook", call_log)
        hook2 = _SideEffectHook("normal_hook", call_log)

        registry = HookRegistry()
        registry.register(hook1)
        registry.register(hook2)

        results = registry.trigger(HookEvent.TOOL_CALL_AFTER, {})

        # 两个钩子都被调用，异常被捕获
        assert len(results) == 2
        assert results[0]["success"] is False
        assert results[0]["should_retry"] is False
        assert results[1]["success"] is True
        assert call_log == ["error_hook", "normal_hook"]


# ============================================================================
# LintHook 边界情况测试
# ============================================================================


class TestLintHookEdgeCases:
    """LintHook 边界情况测试。"""

    def test_lint_hook_non_after_event(self) -> None:
        """非 TOOL_CALL_AFTER 事件 LintHook 直接返回 success=True。"""
        hook = LintHook()
        for event in (
            HookEvent.TOOL_CALL_BEFORE,
            HookEvent.TURN_END,
            HookEvent.SESSION_START,
            HookEvent.SESSION_END,
            HookEvent.ERROR,
        ):
            result = hook.on_event(event, {})
            assert isinstance(result, dict)
            assert result["success"] is True
            assert result["should_retry"] is False

    def test_lint_hook_non_lint_tool(self, tmp_path: Path) -> None:
        """非 write_file/edit_file 工具 LintHook 跳过。"""
        py_file = tmp_path / "x.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "read_file",
            "path": str(py_file),
            "result": "content",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_path_none(self) -> None:
        """path 为 None 时 LintHook 跳过。"""
        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": None,
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_error_result_skipped(self, tmp_path: Path) -> None:
        """工具结果以 Error 开头时 LintHook 跳过。"""
        py_file = tmp_path / "err.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(py_file),
            "result": "Error: write failed",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_nonexistent_file(self, tmp_path: Path) -> None:
        """文件不存在时 LintHook 跳过。"""
        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(tmp_path / "nonexistent.py"),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_unsupported_extension(self, tmp_path: Path) -> None:
        """不支持的文件扩展名 LintHook 跳过。"""
        md_file = tmp_path / "notes.md"
        md_file.write_text("# hello\n", encoding="utf-8")

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(md_file),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_js_without_npx(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JS 文件但无 npx 时 LintHook 跳过返回 success=True。"""
        js_file = tmp_path / "app.js"
        js_file.write_text("var x = 1\n", encoding="utf-8")

        # 模拟无 npx
        monkeypatch.setattr("codepilot.hooks.builtin.shutil.which", lambda _: None)

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(js_file),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_go_without_gofmt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Go 文件但无 gofmt 时 LintHook 跳过返回 success=True。"""
        go_file = tmp_path / "main.go"
        go_file.write_text("package main\n", encoding="utf-8")

        # 模拟无 gofmt
        monkeypatch.setattr("codepilot.hooks.builtin.shutil.which", lambda _: None)

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(go_file),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_lint_hook_ruff_json_parse_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ruff 输出非 JSON 时 LintHook 返回 success=False。"""
        py_file = tmp_path / "parse_fail.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        class _FakeCompletedProcess:
            def __init__(self) -> None:
                self.returncode = 1
                self.stdout = "not valid json {{{"
                self.stderr = ""

        def _fake_run(*args: Any, **kwargs: Any) -> Any:
            return _FakeCompletedProcess()

        monkeypatch.setattr("codepilot.hooks.builtin.subprocess.run", _fake_run)

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(py_file),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["should_retry"] is False

    def test_lint_hook_ruff_no_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ruff 模块未安装（returncode != 0 且 stdout 为空）时返回 success=False。"""
        py_file = tmp_path / "no_ruff.py"
        py_file.write_text("x = 1\n", encoding="utf-8")

        class _FakeCompletedProcess:
            def __init__(self) -> None:
                self.returncode = 1
                self.stdout = ""
                self.stderr = "No module named ruff"

        def _fake_run(*args: Any, **kwargs: Any) -> Any:
            return _FakeCompletedProcess()

        monkeypatch.setattr("codepilot.hooks.builtin.subprocess.run", _fake_run)

        hook = LintHook()
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(py_file),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["should_retry"] is False

    def test_lint_hook_name(self) -> None:
        """LintHook.name 返回 'auto_lint'。"""
        hook = LintHook()
        assert hook.name() == "auto_lint"


# ============================================================================
# GitCommitHook 测试
# ============================================================================


class TestGitCommitHook:
    """GitCommitHook 单元测试。"""

    def test_git_commit_hook_name(self, tmp_path: Path) -> None:
        """GitCommitHook.name 返回 'auto_git_commit'。"""
        from codepilot.git import GitManager

        hook = GitCommitHook(GitManager(tmp_path))
        assert hook.name() == "auto_git_commit"

    def test_git_commit_hook_non_after_event(self, tmp_path: Path) -> None:
        """非 TOOL_CALL_AFTER 事件 GitCommitHook 直接返回 success=True。"""
        from codepilot.git import GitManager

        hook = GitCommitHook(GitManager(tmp_path))
        for event in (
            HookEvent.TOOL_CALL_BEFORE,
            HookEvent.TURN_END,
            HookEvent.SESSION_START,
        ):
            result = hook.on_event(event, {})
            assert isinstance(result, dict)
            assert result["success"] is True
            assert result["should_retry"] is False

    def test_git_commit_hook_non_write_tool(self, tmp_path: Path) -> None:
        """非 write_file/edit_file 工具 GitCommitHook 跳过。"""
        from codepilot.git import GitManager

        hook = GitCommitHook(GitManager(tmp_path))
        context: dict[str, Any] = {
            "tool_name": "read_file",
            "path": str(tmp_path / "x.py"),
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_git_commit_hook_path_none(self, tmp_path: Path) -> None:
        """path 为 None 时 GitCommitHook 跳过。"""
        from codepilot.git import GitManager

        hook = GitCommitHook(GitManager(tmp_path))
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": None,
            "result": "ok",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_git_commit_hook_error_result_skipped(self, tmp_path: Path) -> None:
        """工具结果以 Error 开头时 GitCommitHook 跳过。"""
        from codepilot.git import GitManager

        hook = GitCommitHook(GitManager(tmp_path))
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(tmp_path / "x.py"),
            "result": "Error: write failed",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False

    def test_git_commit_hook_non_git_repo(self, tmp_path: Path) -> None:
        """非 git 仓库中 GitCommitHook 返回 success=False。"""
        from codepilot.git import GitManager

        # tmp_path 不是 git 仓库
        file_path = tmp_path / "x.py"
        file_path.write_text("x = 1\n", encoding="utf-8")

        hook = GitCommitHook(GitManager(tmp_path))
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(file_path),
            "result": "File written",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        # 非 git 仓库 auto_commit 返回 None，success=False
        assert result["success"] is False
        assert result["should_retry"] is False

    def test_git_commit_hook_success(self, tmp_path: Path) -> None:
        """git 仓库中 GitCommitHook 成功提交返回 success=True。"""
        import subprocess as sp

        from codepilot.git import GitManager

        # 初始化 git 仓库
        sp.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        sp.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )
        sp.run(
            ["git", "config", "user.name", "test"],
            cwd=tmp_path,
            capture_output=True,
            check=True,
        )

        file_path = tmp_path / "committed.py"
        file_path.write_text("x = 1\n", encoding="utf-8")

        hook = GitCommitHook(GitManager(tmp_path))
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(file_path),
            "result": "File written",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is True
        assert result["should_retry"] is False
        # output 应为 commit hash
        assert isinstance(result["output"], str)
        assert len(result["output"]) == 8

    def test_git_commit_hook_exception_silent(self, tmp_path: Path) -> None:
        """GitManager.auto_commit 抛异常时 GitCommitHook 静默处理。"""
        from unittest.mock import MagicMock

        from codepilot.git import GitManager

        # 用 MagicMock 模拟 GitManager
        mock_git = MagicMock(spec=GitManager)
        mock_git.auto_commit.side_effect = RuntimeError("git error")

        file_path = tmp_path / "x.py"
        file_path.write_text("x = 1\n", encoding="utf-8")

        hook = GitCommitHook(mock_git)
        context: dict[str, Any] = {
            "tool_name": "write_file",
            "path": str(file_path),
            "result": "File written",
        }
        result = hook.on_event(HookEvent.TOOL_CALL_AFTER, context)

        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["should_retry"] is False


# ============================================================================
# AgentLoop 集成测试：lint 重试循环
# ============================================================================


class TestLintRetryLoopInAgent:
    """AgentLoop 与 LintHook 集成测试：lint 错误触发重试。"""

    async def test_lint_retry_loop_in_agent(self, tmp_path: Path) -> None:
        """mock provider 先返回带 ruff 错误的代码，LintHook 触发重试，
        provider 第二次返回修复后的代码，验证最终写入文件无 lint 错误。
        """
        if not _ruff_available():
            pytest.skip("ruff 不可用，跳过")

        target_file = tmp_path / "retry_target.py"

        # 第一次：写入未使用 import 的代码（触发 F401）
        # 第二次：写入修复后的代码（空文件或 pass）
        provider = _ScriptedProvider(
            event_sequences=[
                [
                    ToolCall(
                        id="call_1",
                        name="write_file",
                        arguments={
                            "path": "retry_target.py",
                            "content": "import os\n",  # 未使用 import → F401
                        },
                    ),
                    Done(stop_reason="end_turn"),
                ],
                [
                    ToolCall(
                        id="call_2",
                        name="write_file",
                        arguments={
                            "path": "retry_target.py",
                            "content": "pass\n",  # 修复后无 lint 错误
                        },
                    ),
                    Done(stop_reason="end_turn"),
                ],
                [
                    TextDelta(text="已修复 lint 错误"),
                    Done(stop_reason="end_turn"),
                ],
            ]
        )

        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace_root=str(tmp_path)))

        cm = _make_context_manager()
        ui = _MockUI()

        hook_registry = HookRegistry()
        hook_registry.register(LintHook())

        loop = AgentLoop(
            provider=provider,
            context_manager=cm,
            tool_registry=registry,
            ui_callback=ui,
            system_prompt="test",
            hook_registry=hook_registry,
            max_lint_retries=3,
        )

        await loop.run("写入文件并修复 lint 错误")

        # 验证最终写入的文件无 lint 错误
        assert target_file.exists()
        final_content = target_file.read_text(encoding="utf-8")
        assert "import os" not in final_content
        # 最终内容应为修复后的代码
        assert "pass" in final_content or final_content.strip() == ""

        # 验证 provider 被调用至少 3 次（第一次写错、第二次写对、第三次总结）
        assert provider.call_count >= 3
