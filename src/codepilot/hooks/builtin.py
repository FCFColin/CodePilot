"""内置 Hook 实现。

提供 LintHook（自动 lint 检测）和 GitCommitHook（自动 git 提交）。

LintHook 在 TOOL_CALL_AFTER 事件中对 write_file/edit_file 写入的文件运行
ruff/eslint/gofmt，发现 lint 错误时返回 should_retry=True 触发重试。
所有异常静默处理，禁止传播到 agent loop。

GitCommitHook 在 TOOL_CALL_AFTER 事件中对 write_file/edit_file 操作
调用 GitManager.auto_commit，所有异常静默处理。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog

from codepilot.git.manager import GitManager
from codepilot.hooks.registry import BaseHook, HookEvent, HookResult

logger = structlog.get_logger(__name__)


# ============================================================================
# LintHook
# ============================================================================


# 触发 lint 的工具名
_LINT_TOOL_NAMES: frozenset[str] = frozenset({"write_file", "edit_file"})

# Python 文件扩展名
_PY_EXTENSIONS: frozenset[str] = frozenset({".py"})
# JS/TS 文件扩展名
_JS_TS_EXTENSIONS: frozenset[str] = frozenset({".js", ".ts", ".jsx", ".tsx"})
# Go 文件扩展名
_GO_EXTENSIONS: frozenset[str] = frozenset({".go"})


class LintHook(BaseHook):
    """自动 lint 检测 Hook。

    在 TOOL_CALL_AFTER 事件中对 write_file/edit_file 写入的文件运行 lint：
    - .py 文件：python -m ruff check --output-format=json
    - .js/.ts 文件：npx eslint（若有 npx）
    - .go 文件：gofmt（若有 gofmt）

    发现 lint 错误时返回 should_retry=True，retry_message 包含行号和错误码。
    所有异常静默处理，禁止传播。
    """

    def name(self) -> str:
        return "auto_lint"

    def on_event(self, event: HookEvent, context: dict[str, Any]) -> HookResult:
        """处理事件，对文件操作工具的结果运行 lint。

        Args:
            event: 事件类型，仅处理 TOOL_CALL_AFTER。
            context: 事件上下文，需含 tool_name、path、result。

        Returns:
            HookResult。lint 通过或非目标文件时 success=True, should_retry=False；
            lint 发现错误时 should_retry=True, retry_message 含错误列表；
            异常时 success=False, should_retry=False。
        """
        # 仅处理 TOOL_CALL_AFTER 事件
        if event != HookEvent.TOOL_CALL_AFTER:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        tool_name = context.get("tool_name", "")
        path = context.get("path")
        result = context.get("result", "")

        # 仅对文件写入/编辑工具触发
        if tool_name not in _LINT_TOOL_NAMES:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # path 为 None 时跳过
        if path is None:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 工具执行失败（结果以 Error 开头）时跳过 lint
        if isinstance(result, str) and result.startswith("Error"):
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 文件不存在时跳过
        if not os.path.isfile(path):
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 按扩展名分发
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext in _PY_EXTENSIONS:
                return self._lint_python(path)
            if ext in _JS_TS_EXTENSIONS:
                return self._lint_js_ts(path)
            if ext in _GO_EXTENSIONS:
                return self._lint_go(path)
        except Exception as e:
            logger.warning(
                "LintHook 异常",
                path=path,
                error=str(e),
                tool_name=tool_name,
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 非支持的文件类型：跳过
        return HookResult(
            success=True,
            output="",
            should_retry=False,
            retry_message=None,
        )

    def _lint_python(self, path: str) -> HookResult:
        """对 Python 文件运行 ruff check。

        Args:
            path: 文件路径。

        Returns:
            HookResult。无错误时 success=True, should_retry=False；
            有错误时 should_retry=True, retry_message 含错误列表。
        """
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "ruff", "check", "--output-format=json", path],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "ruff 调用失败",
                path=path,
                error=str(e),
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # ruff 找不到模块时 stderr 含 "No module named ruff"
        if proc.returncode != 0 and not proc.stdout.strip():
            # 可能 ruff 未安装
            logger.warning(
                "ruff 不可用",
                path=path,
                stderr=proc.stderr.strip(),
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 解析 JSON 输出
        try:
            lint_data = json.loads(proc.stdout) if proc.stdout.strip() else []
        except json.JSONDecodeError as e:
            logger.warning(
                "ruff JSON 解析失败",
                path=path,
                error=str(e),
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # ruff JSON 输出为列表，每个元素含 filename、message、code、location 等
        if not lint_data:
            # 无错误
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 构造错误列表
        error_lines: list[str] = []
        for item in lint_data:
            line_no = item.get("location", {}).get("row", 0)
            message = item.get("message", "")
            code = item.get("code", "")
            error_lines.append(f"第{line_no}行：{message}（{code}）")

        retry_message = "以下 lint 错误需要修复：\n" + "\n".join(error_lines)
        logger.info(
            "LintHook 检测到 lint 错误",
            path=path,
            error_count=len(error_lines),
        )
        return HookResult(
            success=True,
            output="",
            should_retry=True,
            retry_message=retry_message,
        )

    def _lint_js_ts(self, path: str) -> HookResult:
        """对 JS/TS 文件运行 eslint（若有 npx）。

        Args:
            path: 文件路径。

        Returns:
            HookResult。无 npx 时跳过返回 success=True。
        """
        npx_path = shutil.which("npx")
        if npx_path is None:
            # 无 npx：跳过
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )
        try:
            proc = subprocess.run(
                [npx_path, "eslint", "--format=json", path],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "eslint 调用失败",
                path=path,
                error=str(e),
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )

        try:
            lint_data = json.loads(proc.stdout) if proc.stdout.strip() else []
        except json.JSONDecodeError:
            # eslint 输出无法解析：跳过
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # eslint JSON 输出为列表，每个元素含 messages
        error_lines: list[str] = []
        for file_result in lint_data:
            for msg in file_result.get("messages", []):
                line_no = msg.get("line", 0)
                message = msg.get("message", "")
                rule_id = msg.get("ruleId", "unknown")
                error_lines.append(f"第{line_no}行：{message}（{rule_id}）")

        if not error_lines:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        retry_message = "以下 lint 错误需要修复：\n" + "\n".join(error_lines)
        return HookResult(
            success=True,
            output="",
            should_retry=True,
            retry_message=retry_message,
        )

    def _lint_go(self, path: str) -> HookResult:
        """对 Go 文件运行 gofmt（若有 gofmt）。

        Args:
            path: 文件路径。

        Returns:
            HookResult。无 gofmt 时跳过返回 success=True。
        """
        gofmt_path = shutil.which("gofmt")
        if gofmt_path is None:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )
        try:
            proc = subprocess.run(
                [gofmt_path, "-l", path],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(
                "gofmt 调用失败",
                path=path,
                error=str(e),
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # gofmt -l 输出需要格式化的文件名（每行一个）
        if proc.stdout.strip():
            # 文件需要格式化
            retry_message = "以下 lint 错误需要修复：\n第1行：gofmt 格式不规范（gofmt）"
            return HookResult(
                success=True,
                output="",
                should_retry=True,
                retry_message=retry_message,
            )

        return HookResult(
            success=True,
            output="",
            should_retry=False,
            retry_message=None,
        )


# ============================================================================
# GitCommitHook
# ============================================================================


class GitCommitHook(BaseHook):
    """自动 Git 提交 Hook。

    在 TOOL_CALL_AFTER 事件中对 write_file/edit_file 操作调用
    GitManager.auto_commit。所有异常静默处理。
    """

    def __init__(self, git_manager: GitManager) -> None:
        """初始化 GitCommitHook。

        Args:
            git_manager: GitManager 实例。
        """
        self._git_manager = git_manager

    def name(self) -> str:
        return "auto_git_commit"

    def on_event(self, event: HookEvent, context: dict[str, Any]) -> HookResult:
        """处理事件，对文件操作工具调用 auto_commit。

        Args:
            event: 事件类型，仅处理 TOOL_CALL_AFTER。
            context: 事件上下文，需含 tool_name、path、result。

        Returns:
            HookResult。auto_commit 成功返回 success=True；
            异常或非目标事件返回 success=False, should_retry=False。
        """
        if event != HookEvent.TOOL_CALL_AFTER:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        tool_name = context.get("tool_name", "")
        path = context.get("path")
        result = context.get("result", "")

        if tool_name not in _LINT_TOOL_NAMES:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        if path is None:
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        # 工具执行失败时跳过提交
        if isinstance(result, str) and result.startswith("Error"):
            return HookResult(
                success=True,
                output="",
                should_retry=False,
                retry_message=None,
            )

        try:
            commit_hash = self._git_manager.auto_commit(
                f"auto commit by hook: {tool_name} {path}",
                [Path(path)],
            )
            if commit_hash is not None:
                return HookResult(
                    success=True,
                    output=commit_hash,
                    should_retry=False,
                    retry_message=None,
                )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )
        except Exception as e:
            logger.warning(
                "GitCommitHook 异常",
                path=path,
                error=str(e),
                tool_name=tool_name,
            )
            return HookResult(
                success=False,
                output="",
                should_retry=False,
                retry_message=None,
            )


__all__ = ["LintHook", "GitCommitHook"]
