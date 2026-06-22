"""命令过滤器 CommandFilter。

对单条 shell 命令进行黑名单、交互式、白名单三重检查。
不含链式命令拆解（由 Sandbox 负责），仅检查单条命令。

黑名单匹配使用 fnmatch，支持 `*` 通配符（如 `wget * | bash`）。
白名单匹配检查命令第一个 token 是否在白名单列表中。
交互式命令检查命令第一个 token 是否在交互式命令集合中。
"""

from __future__ import annotations

import fnmatch
import re

import structlog

logger = structlog.get_logger(__name__)

# 交互式命令集合：第一个 token 匹配则视为交互式
_INTERACTIVE_COMMANDS: frozenset[str] = frozenset(
    {
        "vim",
        "vi",
        "nano",
        "emacs",
        "less",
        "more",
        "top",
        "htop",
        "man",
        "ssh",
        "telnet",
        "ftp",
    }
)

# 提权命令前缀：禁止以这些 token 开头
_PRIVILEGE_PREFIXES: frozenset[str] = frozenset({"sudo", "su"})

# 命令执行器前缀：带 -c/-e 参数时禁止（代码执行模式）
_COMMAND_EXECUTOR_PREFIXES: frozenset[str] = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
    }
)

# 无条件禁止的命令执行器（eval/exec 本身就是代码执行）
_UNCONDITIONAL_EXECUTOR_PREFIXES: frozenset[str] = frozenset({"eval", "exec"})


def _first_token(command: str) -> str:
    """提取命令第一个 token（原始大小写），用于前缀匹配。

    命令为空或仅空白时返回空字符串。
    """
    stripped = command.strip()
    if not stripped:
        return ""
    # 按空白拆分取第一个 token
    parts = re.split(r"\s+", stripped, maxsplit=1)
    return parts[0] if parts else ""


class CommandFilter:
    """单条命令过滤器。

    可被 Sandbox 内部使用，也可独立使用。check() 方法按
    黑名单 → 提权 → 交互式 → 白名单顺序检查。
    """

    def __init__(
        self,
        blacklist: list[str] | None = None,
        whitelist_mode: bool = False,
        whitelist: list[str] | None = None,
    ) -> None:
        # 黑名单列表（保留原始顺序与大小写，匹配时统一小写处理）
        self.blacklist: list[str] = list(blacklist or [])
        self.whitelist_mode: bool = bool(whitelist_mode)
        # 白名单按第一个 token 匹配，统一小写存储
        self.whitelist: set[str] = {w.lower() for w in (whitelist or [])}

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def check(self, command: str) -> tuple[bool, str]:
        """检查单条命令（不含链式拆解，由 Sandbox 负责）。

        Returns:
            (is_safe, reason)。is_safe 为 True 时 reason 为 "OK"。
        """
        if not command or not command.strip():
            return False, "empty command"

        # 0. 命令替换检测
        if "$(" in command:
            return False, "command substitution not allowed: $()"
        if "`" in command:
            return False, "command substitution not allowed: backticks"

        # 0.1 历史展开检测
        if re.search(r"![!$\w]", command):
            return False, "history expansion not allowed"

        # 0.2 Here-string 检测
        if "<<<" in command:
            return False, "here-string not allowed"

        # 0.3 进程替换检测
        if "<(" in command or ">(" in command:
            return False, "process substitution not allowed"

        # 1. 黑名单
        blocked, pattern = self.is_blacklisted(command)
        if blocked:
            logger.warning("命令命中黑名单", command=command, pattern=pattern)
            return False, f"blacklisted command matches pattern: {pattern}"

        # 2. 提权命令（sudo / su）
        first = _first_token(command)
        if first.lower() in _PRIVILEGE_PREFIXES:
            logger.warning("拒绝提权命令", command=command, prefix=first)
            return False, f"privilege escalation command not allowed: {first}"

        # 2.5 命令执行器检测
        if first.lower() in _UNCONDITIONAL_EXECUTOR_PREFIXES:
            logger.warning(
                "拒绝命令执行器", command=command, prefix=first
            )
            return False, f"command executor not allowed: {first}"
        if first.lower() in _COMMAND_EXECUTOR_PREFIXES:
            if re.search(r"-[ce]\b", command):
                logger.warning(
                    "拒绝命令执行器代码执行", command=command, prefix=first
                )
                return False, f"command executor with code execution not allowed: {first} -c/-e"

        # 3. 交互式命令
        if self.is_interactive(command):
            logger.warning("拒绝交互式命令", command=command, prefix=first)
            return False, f"interactive command not allowed: {first}"

        # 4. 白名单模式
        if self.whitelist_mode and not self.is_whitelisted(command):
            logger.warning("命令不在白名单", command=command, prefix=first)
            return False, f"command not in whitelist: {first}"

        logger.debug("命令校验通过", command=command)
        return True, "OK"

    def is_blacklisted(self, command: str) -> tuple[bool, str]:
        """检查命令是否命中黑名单。

        黑名单条目支持 `*` 通配符（fnmatch 匹配）；不含通配符的条目
        同时检查"以该条目开头"和"包含该条目"两种情况。

        Returns:
            (is_blocked, matched_pattern)。
        """
        if not command:
            return False, ""
        # 统一小写比较，避免 rm vs RM 绕过
        cmd_lower = command.lower()
        for pattern in self.blacklist:
            pat_lower = pattern.lower()
            if "*" in pat_lower:
                # 通配符模式：fnmatch 整条命令
                if fnmatch.fnmatch(cmd_lower, pat_lower):
                    return True, pattern
            else:
                # 非通配符：开头匹配 或 子串包含
                if cmd_lower.startswith(pat_lower) or pat_lower in cmd_lower:
                    return True, pattern
        return False, ""

    def is_whitelisted(self, command: str) -> bool:
        """检查命令第一个 token 是否在白名单中。

        白名单模式关闭时也返回 True（调用方应先判断 whitelist_mode）。
        """
        first = _first_token(command)
        if not first:
            return False
        return first.lower() in self.whitelist

    def is_interactive(self, command: str) -> bool:
        """检查命令是否为交互式命令（第一个 token 命中集合）。"""
        first = _first_token(command)
        if not first:
            return False
        return first.lower() in _INTERACTIVE_COMMANDS


__all__ = ["CommandFilter"]
