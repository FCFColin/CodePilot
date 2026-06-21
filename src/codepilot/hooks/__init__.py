"""Hooks 子包。

提供 Hook 事件系统：HookEvent 枚举、HookResult TypedDict、BaseHook 抽象基类、
HookRegistry 注册表，以及内置的 LintHook（自动 lint）和 GitCommitHook（自动提交）。
"""

from codepilot.hooks.builtin import GitCommitHook, LintHook
from codepilot.hooks.registry import BaseHook, HookEvent, HookRegistry, HookResult

__all__ = [
    "HookEvent",
    "HookResult",
    "BaseHook",
    "HookRegistry",
    "LintHook",
    "GitCommitHook",
]
