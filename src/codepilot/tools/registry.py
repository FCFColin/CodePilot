"""工具注册表与基类定义。

定义 SandboxProtocol/ApprovalProtocol 协议接口（Phase 5 实现），
BaseTool 抽象基类，ToolRegistry 注册表，以及内置的 GetContextTool。

sandbox 和 approval 作为可选依赖注入，传入 None 时跳过校验。
所有 I/O 异常包装为 ToolError，由 execute 捕获并转为错误字符串。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol, TypedDict, runtime_checkable

import structlog

from codepilot.exceptions import ToolError

logger = structlog.get_logger(__name__)


# ============================================================================
# Provider 工具定义 TypedDict
# ============================================================================


class OpenAIFunctionDef(TypedDict):
    """OpenAI function 子结构。"""

    name: str
    description: str
    parameters: dict[str, Any]


class OpenAIToolDef(TypedDict):
    """OpenAI 工具定义（type=function 包装层）。"""

    type: str  # "function"
    function: OpenAIFunctionDef


class AnthropicToolDef(TypedDict):
    """Anthropic 工具定义（扁平结构）。"""

    name: str
    description: str
    input_schema: dict[str, Any]


# ============================================================================
# 安全协议接口（Phase 5 实现，此处仅定义协议）
# ============================================================================


@runtime_checkable
class SandboxProtocol(Protocol):
    """沙箱协议，由 Phase 5 的 security/sandbox.py 实现。"""

    def validate_path(self, path: str, operation: str = "read") -> tuple[bool, str]:
        """校验路径是否允许指定操作。

        Args:
            path: 待校验路径（相对或绝对）。
            operation: 操作类型（read/write）。

        Returns:
            (is_valid, error_message)。is_valid 为 True 时 error_message 为空。
        """
        ...

    def validate_command(self, command: str) -> tuple[bool, str]:
        """校验 shell 命令是否允许执行。

        Returns:
            (is_valid, error_message)。
        """
        ...


@runtime_checkable
class ApprovalProtocol(Protocol):
    """审批协议，由 Phase 5 的 security/approval.py 实现。"""

    async def request_approval(self, operation: str, details: dict[str, Any]) -> bool:
        """请求用户审批操作。

        Args:
            operation: 操作类型（file_write/file_edit/shell_exec）。
            details: 操作详情（path/content/diff/command 等）。

        Returns:
            True 表示批准，False 表示拒绝。
        """
        ...


# ============================================================================
# BaseTool 抽象基类
# ============================================================================


class BaseTool(ABC):
    """工具抽象基类。

    所有工具继承此类，实现 get_parameters 和 execute 方法。
    子类需设置 name 和 description 类属性。
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    def get_parameters(self) -> dict[str, Any]:
        """返回 JSON Schema 参数定义（OpenAI/Anthropic 通用格式）。"""
        ...

    @abstractmethod
    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行工具，返回结构化字符串结果给 LLM。

        Args:
            arguments: 工具参数字典（动态结构，无法用单一 TypedDict 描述）。
            sandbox: 可选的沙箱校验器，None 时跳过路径/命令校验。
            approval: 可选的审批器，None 时跳过审批。

        Returns:
            结构化字符串结果。出错时返回 "Error: ..." 格式的字符串。
        """
        ...

    def to_openai_format(self) -> OpenAIToolDef:
        """转换为 OpenAI function 格式。"""
        return OpenAIToolDef(
            type="function",
            function=OpenAIFunctionDef(
                name=self.name,
                description=self.description,
                parameters=self.get_parameters(),
            ),
        )

    def to_anthropic_format(self) -> AnthropicToolDef:
        """转换为 Anthropic 原生格式。"""
        return AnthropicToolDef(
            name=self.name,
            description=self.description,
            input_schema=self.get_parameters(),
        )


# ============================================================================
# GetContextTool 内置工具
# ============================================================================


class GetContextTool(BaseTool):
    """上下文统计工具，返回当前对话上下文使用情况。

    通过构造函数注入 context_manager 引用（可选）。
    若 context_manager 为 None，返回不可用提示。
    """

    name = "get_context"
    description = (
        "获取当前对话上下文的使用统计，包括总 token 数、最大 token 限制、"
        "占比、消息数量和压缩状态。无需参数。"
    )

    def __init__(self, context_manager: Any = None) -> None:
        self.context_manager = context_manager

    def get_parameters(self) -> dict[str, Any]:
        """无参数。"""
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """返回上下文统计信息。

        Raises:
            ToolError: context_manager 调用失败时抛出（由调用方捕获）。
        """
        if self.context_manager is None:
            return "Context manager not available"

        try:
            # 尝试调用 get_stats() 方法（Phase 6 的 ContextManager 实现）
            stats = self.context_manager.get_stats()
        except Exception as e:
            logger.error("获取上下文统计失败", error=str(e))
            raise ToolError(f"获取上下文统计失败: {e}") from e

        if isinstance(stats, dict):
            total = stats.get("total_tokens", 0)
            max_tokens = stats.get("max_tokens", 0)
            ratio = (total / max_tokens * 100) if max_tokens else 0.0
            msg_count = stats.get("message_count", 0)
            compressed = stats.get("compressed", False)
            logger.debug("返回上下文统计", total=total, max=max_tokens)
            return (
                f"Context stats:\n"
                f"  Total tokens: {total}\n"
                f"  Max tokens: {max_tokens}\n"
                f"  Usage: {ratio:.1f}%\n"
                f"  Messages: {msg_count}\n"
                f"  Compressed: {'yes' if compressed else 'no'}"
            )
        return f"Context stats: {stats}"


# ============================================================================
# ToolRegistry 注册表
# ============================================================================


class ToolRegistry:
    """工具注册表，管理所有可用工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册工具。同名工具会被覆盖。"""
        logger.debug("注册工具", name=tool.name)
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """按名获取工具，不存在返回 None。"""
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        """列出所有已注册工具。"""
        return list(self._tools.values())

    def to_openai_format(self) -> list[OpenAIToolDef]:
        """转换全部工具为 OpenAI function 格式列表。"""
        return [tool.to_openai_format() for tool in self._tools.values()]

    def to_anthropic_format(self) -> list[AnthropicToolDef]:
        """转换全部工具为 Anthropic 原生格式列表。"""
        return [tool.to_anthropic_format() for tool in self._tools.values()]

    @staticmethod
    def create_default_registry(
        context_manager: Any = None,
        workspace_root: str | None = None,
        require_approval_for: list[str] | None = None,
    ) -> ToolRegistry:
        """创建包含全部 10 个工具的默认注册表。

        Args:
            context_manager: 可选的上下文管理器，注入 GetContextTool。
            workspace_root: 工作区根目录，None 时使用当前目录 "."。
            require_approval_for: 需审批的操作类型列表，None 时使用默认值。

        Returns:
            包含 10 个工具的 ToolRegistry 实例。
        """
        # 延迟导入避免循环依赖
        from codepilot.tools.diagnose import DiagnoseTool
        from codepilot.tools.file_edit import EditFileTool
        from codepilot.tools.file_read import ReadFileTool
        from codepilot.tools.file_write import WriteFileTool
        from codepilot.tools.list_files import ListFilesTool
        from codepilot.tools.plan_tool import PlanTool
        from codepilot.tools.search_code import SearchCodeTool
        from codepilot.tools.shell_exec import ShellExecTool
        from codepilot.tools.web_fetch import WebFetchTool

        ws = workspace_root or "."
        rap = (
            require_approval_for
            if require_approval_for is not None
            else ["file_write", "file_edit", "shell_exec"]
        )

        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root=ws, require_approval_for=rap))
        registry.register(WriteFileTool(workspace_root=ws, require_approval_for=rap))
        registry.register(EditFileTool(workspace_root=ws, require_approval_for=rap))
        registry.register(ListFilesTool(workspace_root=ws, require_approval_for=rap))
        registry.register(ShellExecTool(workspace_root=ws, require_approval_for=rap))
        registry.register(SearchCodeTool(workspace_root=ws, require_approval_for=rap))
        registry.register(WebFetchTool())
        registry.register(GetContextTool(context_manager=context_manager))
        registry.register(DiagnoseTool())
        registry.register(PlanTool())
        logger.info("默认工具注册表已创建", count=len(registry._tools))
        return registry


__all__ = [
    "SandboxProtocol",
    "ApprovalProtocol",
    "BaseTool",
    "GetContextTool",
    "ToolRegistry",
    "OpenAIFunctionDef",
    "OpenAIToolDef",
    "AnthropicToolDef",
]
