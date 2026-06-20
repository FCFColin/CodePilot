"""沙箱 Sandbox。

对文件路径与 shell 命令进行安全校验，满足 tools/registry.py 中的
SandboxProtocol 协议。路径校验阻止越界访问（路径遍历、符号链接逃逸、
敏感文件写入）；命令校验通过 CommandFilter 检查黑名单/白名单/交互式/提权。

Windows 兼容：路径比较统一使用 os.path.normcase 处理大小写与分隔符差异。

异常处理：路径解析等 I/O 操作的异常包装为 SecurityError 抛出；
正常校验拒绝通过 ValidationResult 返回（不抛异常）。
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

import structlog

from codepilot.exceptions import SecurityError
from codepilot.security.command_filter import CommandFilter

logger = structlog.get_logger(__name__)


# ============================================================================
# ValidationResult：校验结果 NamedTuple（替代裸 tuple）
# ============================================================================


class ValidationResult(NamedTuple):
    """路径/命令校验结果。

    Attributes:
        is_safe: 是否安全（允许操作）。
        reason: 拒绝原因；is_safe 为 True 时为 "OK"。
    """

    is_safe: bool
    reason: str


# ============================================================================
# 默认值（与 config.py 中 SecurityConfig 默认值保持一致）
# ============================================================================

_DEFAULT_BLOCKED_PATHS: list[str] = [
    "/",
    "/etc",
    "/usr",
    "/var",
    "/sys",
    "/proc",
    "/boot",
    "/root",
    "~",
]

_DEFAULT_COMMAND_BLACKLIST: list[str] = [
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "chmod -R 777 /",
    "wget * | bash",
    "curl * | sh",
    "shutdown",
    "reboot",
    "init 0",
    "systemctl",
]

_DEFAULT_COMMAND_WHITELIST: list[str] = [
    "ls",
    "cat",
    "grep",
    "find",
    "echo",
    "python",
    "node",
    "npm",
    "pip",
    "git",
    "make",
    "cargo",
    "go",
]

# write 操作禁止的敏感路径组件/文件名
_SENSITIVE_DIR_COMPONENTS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
    }
)
_SENSITIVE_FILENAMES: frozenset[str] = frozenset(
    {
        ".codepilot.yml",
        ".codepilot_history.jsonl",
    }
)

# 链式命令分隔符正则：|| 必须在 | 之前匹配
_CHAIN_SPLIT_PATTERN = re.compile(r"\|\||&&|\||;")


def _norm(path: str) -> str:
    """规范化路径用于比较（Windows 下小写化、统一分隔符）。"""
    return os.path.normcase(os.path.normpath(path))


def _is_subpath(child: str, parent: str) -> bool:
    """判断 child 是否等于 parent 或位于 parent 之内（前缀子路径）。

    使用 normcase + normpath 处理后比较，确保跨平台一致。
    """
    n_child = _norm(child)
    n_parent = _norm(parent)
    if n_child == n_parent:
        return True
    # 确保是路径前缀而非字符串前缀（如 /etc 不应匹配 /etcxyz）
    return n_child.startswith(n_parent + os.sep)


class Sandbox:
    """路径与命令沙箱校验器，满足 SandboxProtocol 协议。"""

    def __init__(
        self,
        workspace_root: str,
        allowed_dirs: list[str] | None = None,
        blocked_paths: list[str] | None = None,
        command_blacklist: list[str] | None = None,
        command_whitelist_mode: bool = False,
        command_whitelist: list[str] | None = None,
    ) -> None:
        # workspace_root 解析为绝对路径（realpath 展开符号链接）
        try:
            self.workspace_root: str = os.path.realpath(workspace_root)
        except OSError as e:
            raise SecurityError(f"解析 workspace_root 失败: {e}") from e

        # allowed_dirs 解析为绝对路径
        self.allowed_dirs: list[str] = []
        for d in allowed_dirs or []:
            try:
                self.allowed_dirs.append(os.path.realpath(d))
            except OSError as e:
                raise SecurityError(f"解析 allowed_dir 失败: {d}: {e}") from e

        # blocked_paths：展开 ~ 为 home 目录，realpath 解析符号链接
        raw_blocked = (
            blocked_paths if blocked_paths is not None else _DEFAULT_BLOCKED_PATHS
        )
        self.blocked_paths: list[str] = []
        for p in raw_blocked:
            try:
                expanded = os.path.expanduser(p)
                self.blocked_paths.append(os.path.realpath(expanded))
            except OSError as e:
                raise SecurityError(f"解析 blocked_path 失败: {p}: {e}") from e

        # 命令过滤器
        raw_blacklist = (
            command_blacklist
            if command_blacklist is not None
            else _DEFAULT_COMMAND_BLACKLIST
        )
        raw_whitelist = (
            command_whitelist
            if command_whitelist is not None
            else _DEFAULT_COMMAND_WHITELIST
        )
        self.command_filter: CommandFilter = CommandFilter(
            blacklist=raw_blacklist,
            whitelist_mode=command_whitelist_mode,
            whitelist=raw_whitelist,
        )

        logger.debug(
            "沙箱初始化完成",
            workspace_root=self.workspace_root,
            allowed_dirs_count=len(self.allowed_dirs),
            blocked_paths_count=len(self.blocked_paths),
        )

    # ------------------------------------------------------------------
    # validate_path
    # ------------------------------------------------------------------

    def validate_path(self, path: str, operation: str = "read") -> ValidationResult:
        """校验路径是否允许指定操作。

        Args:
            path: 待校验路径（相对或绝对）。相对路径相对 workspace_root。
            operation: 操作类型（read/write）。

        Returns:
            ValidationResult。is_safe 为 True 时 reason 为 "OK"。

        Raises:
            SecurityError: 路径解析发生 I/O 异常时抛出。
        """
        if not path:
            return ValidationResult(False, "empty path")

        logger.debug("路径校验开始", path=path, operation=operation)

        # 1. 解析为绝对路径（realpath 解析符号链接与 .. ）
        try:
            if os.path.isabs(path):
                resolved = os.path.realpath(path)
            else:
                # 相对路径相对 workspace_root 解析
                resolved = os.path.realpath(os.path.join(self.workspace_root, path))
        except OSError as e:
            logger.error("路径解析失败", path=path, error=str(e))
            raise SecurityError(f"路径解析失败: {path}: {e}") from e

        # 2. 路径遍历检查：realpath 已解析 .. ，
        #    若解析后逃逸 workspace 则后续 allowed 检查会拦截

        # 3. 检查 blocked_paths（精确匹配 + 前缀子路径匹配）
        for bp in self.blocked_paths:
            if _is_subpath(resolved, bp):
                logger.warning("路径被阻止", path=path, blocked=bp)
                return ValidationResult(False, f"path is blocked: {bp}")

        # 4. 检查是否在 workspace_root 或 allowed_dirs 内
        if not self._is_path_allowed(resolved):
            logger.warning(
                "路径逃逸工作区",
                path=path,
                resolved=resolved,
                workspace=self.workspace_root,
            )
            return ValidationResult(
                False,
                f"path escapes workspace root: {resolved} "
                f"(workspace={self.workspace_root})",
            )

        # 5. write 操作额外检查敏感文件
        if operation.lower() == "write":
            sensitive = self._check_sensitive_path(resolved)
            if sensitive:
                logger.warning("写入敏感路径被拒", path=path, sensitive=sensitive)
                return ValidationResult(
                    False,
                    f"writing to sensitive path is not allowed: {sensitive}",
                )

        logger.debug("路径校验通过", path=path, operation=operation)
        return ValidationResult(True, "OK")

    def _is_path_allowed(self, resolved: str) -> bool:
        """检查解析后的路径是否在 workspace_root 或 allowed_dirs 内。"""
        # workspace_root
        if _is_subpath(resolved, self.workspace_root):
            return True
        # allowed_dirs
        return any(_is_subpath(resolved, ad) for ad in self.allowed_dirs)

    def _check_sensitive_path(self, resolved: str) -> str:
        """检查 write 操作的目标是否为敏感路径。

        Returns:
            命中的敏感标识（如 ".git"），未命中返回空字符串。
        """
        # 拆分路径组件，检查敏感目录组件
        parts = resolved.split(os.sep)
        for part in parts:
            if part in _SENSITIVE_DIR_COMPONENTS:
                return part
        # 检查文件名
        basename = os.path.basename(resolved)
        if basename in _SENSITIVE_FILENAMES:
            return basename
        return ""

    # ------------------------------------------------------------------
    # validate_command
    # ------------------------------------------------------------------

    def validate_command(self, command: str) -> ValidationResult:
        """校验 shell 命令是否允许执行。

        拆解链式命令（|、&&、||、;），对每段分别用 CommandFilter 检查。

        Returns:
            ValidationResult。is_safe 为 True 时 reason 为 "OK"。
        """
        if not command or not command.strip():
            return ValidationResult(False, "empty command")

        logger.debug("命令校验开始", command=command)

        # 拆解链式命令
        segments = _CHAIN_SPLIT_PATTERN.split(command)
        for seg in segments:
            seg = seg.strip()
            if not seg:
                # 连续分隔符产生空段，跳过
                continue
            ok, msg = self.command_filter.check(seg)
            if not ok:
                logger.warning(
                    "链式命令段被拒", command=command, segment=seg, reason=msg
                )
                return ValidationResult(False, f"chain segment '{seg}' rejected: {msg}")

        logger.debug("命令校验通过", command=command)
        return ValidationResult(True, "OK")


__all__ = ["Sandbox", "ValidationResult"]
