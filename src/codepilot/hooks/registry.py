"""Hook 注册表与抽象基类。

定义 HookEvent 枚举、HookResult TypedDict、BaseHook 抽象基类、
HookRegistry 注册表。

Hook 系统用于在工具调用前后、会话开始/结束、错误等事件点插入自定义逻辑
（如自动 lint、自动 git 提交）。所有 Hook 必须实现 on_event 方法，
禁止抛异常（异常由调用方包裹 try/except 保护）。
"""

from __future__ import annotations

import abc
import enum
from typing import Any, TypedDict

import structlog

logger = structlog.get_logger(__name__)


# ============================================================================
# HookEvent 枚举
# ============================================================================


class HookEvent(enum.Enum):
    """Hook 事件类型。"""

    TOOL_CALL_BEFORE = "tool_call_before"
    TOOL_CALL_AFTER = "tool_call_after"
    TURN_END = "turn_end"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    ERROR = "error"


# ============================================================================
# HookResult TypedDict
# ============================================================================


class HookResult(TypedDict):
    """Hook 执行结果。

    Attributes:
        success: 是否成功执行（不抛异常）。
        output: 输出信息（用于日志/调试）。
        should_retry: 是否应触发重试（仅 TOOL_CALL_AFTER 有效）。
        retry_message: 重试提示消息（应触发重试时非空）。
    """

    success: bool
    output: str
    should_retry: bool
    retry_message: str | None


# ============================================================================
# BaseHook 抽象基类
# ============================================================================


class BaseHook(abc.ABC):
    """Hook 抽象基类。

    所有 Hook 继承此类，实现 name 和 on_event 方法。
    on_event 禁止抛异常，异常情况应返回 success=False 的 HookResult。
    """

    @abc.abstractmethod
    def name(self) -> str:
        """返回 Hook 名称（用于日志与调试）。"""
        ...

    @abc.abstractmethod
    def on_event(self, event: HookEvent, context: dict[str, Any]) -> HookResult:
        """处理事件，返回 HookResult。

        Args:
            event: 事件类型。
            context: 事件上下文（含 tool_name、path、result 等字段）。

        Returns:
            HookResult。禁止抛异常，异常情况返回 success=False。
        """
        ...


# ============================================================================
# HookRegistry 注册表
# ============================================================================


class HookRegistry:
    """Hook 注册表，管理所有已注册的 Hook。

    按注册顺序调用所有 Hook 的 on_event 方法。
    trigger_tool_after 返回第一个 should_retry=True 的结果（首个重试优先）。
    """

    def __init__(self) -> None:
        self._hooks: list[BaseHook] = []

    def register(self, hook: BaseHook) -> None:
        """注册 Hook，追加到 _hooks 末尾。

        Args:
            hook: 待注册的 Hook 实例。
        """
        self._hooks.append(hook)
        logger.debug("Hook 已注册", name=hook.name())

    def trigger(
        self,
        event: HookEvent,
        context: dict[str, Any],
    ) -> list[HookResult]:
        """按注册顺序触发所有 Hook 的 on_event。

        单个 Hook 抛异常时记录 warning 并跳过，不影响其他 Hook。

        Args:
            event: 事件类型。
            context: 事件上下文。

        Returns:
            所有 Hook 的结果列表（按注册顺序）。
        """
        results: list[HookResult] = []
        for hook in self._hooks:
            try:
                result = hook.on_event(event, context)
                results.append(result)
            except Exception as e:
                logger.warning(
                    "Hook 执行异常",
                    hook_name=hook.name(),
                    error=str(e),
                )
                results.append(
                    HookResult(
                        success=False,
                        output="",
                        should_retry=False,
                        retry_message=None,
                    )
                )
        return results

    def trigger_tool_after(
        self,
        tool_name: str,
        path: str | None,
        result: str,
    ) -> HookResult | None:
        """触发 TOOL_CALL_AFTER 事件，返回第一个 should_retry=True 的结果。

        Args:
            tool_name: 工具名称。
            path: 工具操作的文件路径（可为 None）。
            result: 工具执行结果文本。

        Returns:
            第一个 should_retry=True 的 HookResult，无则返回 None。
        """
        context: dict[str, Any] = {
            "tool_name": tool_name,
            "path": path,
            "result": result,
        }
        results = self.trigger(HookEvent.TOOL_CALL_AFTER, context)
        for r in results:
            if r["should_retry"]:
                return r
        return None


__all__ = ["HookEvent", "HookResult", "BaseHook", "HookRegistry"]
