"""Agent 核心循环。

实现 agentic tool-use 循环：用户输入 → LLM 流式响应 → 工具调用执行
→ 结果回传 → 继续生成，直到 LLM 不再请求工具调用。

支持 Ctrl+C 中断、单轮工具调用上限、UI 回调（Phase 7 注入）。
"""

from __future__ import annotations

from codepilot.context.manager import ContextManager
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.base import (
    BaseProvider,
    Done,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)
from codepilot.providers.deepseek import DeepSeekProvider
from codepilot.security.approval import ApprovalManager
from codepilot.security.sandbox import Sandbox
from codepilot.tools.registry import ToolRegistry


# ============================================================================
# 默认系统提示词（PRD 第九节，完整复制）
# ============================================================================

DEFAULT_SYSTEM_PROMPT = """You are CodePilot, an expert AI coding assistant operating in a terminal environment.

## Your Capabilities
You have access to the following tools to help users with coding tasks:
- read_file: Read file contents
- write_file: Create or overwrite files
- edit_file: Make targeted edits using search/replace
- list_files: List directory contents
- shell_exec: Execute terminal commands
- search_code: Search for patterns in code
- get_context: Check current context window usage

## Guidelines
1. ALWAYS use tools to inspect the codebase before making changes. Read relevant files first.
2. When editing files, prefer edit_file (surgical changes) over write_file (full replacement) for existing files.
3. After making changes, verify them by reading the file back or running tests.
4. When running commands, explain what the command does before executing.
5. If a tool call fails or is blocked by security, acknowledge it and try an alternative approach.
6. Keep your responses concise and focused on the task.
7. When creating new files, always show the full content in write_file.
8. For complex tasks, break them down into steps and execute them one by one.

## Constraints  
- You can ONLY access files within the designated workspace directory.
- Some operations require user approval before execution.
- Destructive commands (rm -rf, etc.) may be blocked by security policy.
- If you encounter a security restriction, do NOT try to bypass it.

## Response Style
- Be direct and actionable
- Use markdown formatting in explanations
- Show code changes clearly
- Provide brief explanations of what you're doing and why
"""


# ============================================================================
# 工具结果消息角色（不同 provider 要求的角色不同）
# ============================================================================

# DeepSeek/OpenAI 格式：工具结果消息角色为 "tool"
_TOOL_RESULT_ROLE_OPENAI = "tool"
# Anthropic 格式：工具结果消息角色为 "user"（content 为 tool_result blocks）
_TOOL_RESULT_ROLE_ANTHROPIC = "user"


class AgentLoop:
    """核心 Agent 循环。

    实现 agentic tool-use 循环：
    1. 用户输入加入上下文
    2. 调用 provider.chat 获取流式响应
    3. 累积文本与工具调用
    4. 若有工具调用：执行工具，结果回传，继续循环
    5. 若无工具调用：返回最终文本

    Attributes:
        provider: LLM 后端（DeepSeek/Anthropic）。
        context_manager: 上下文管理器，维护对话历史。
        tool_registry: 工具注册表。
        sandbox: 可选的沙箱校验器。
        approval: 可选的审批管理器。
        ui_display: 可选的 UI 回调对象（Phase 7 注入）。
        max_tool_calls_per_turn: 单轮最大工具调用次数。
        system_prompt: 系统提示词。
        _cancelled: Ctrl+C 中断标志。
    """

    def __init__(
        self,
        provider: BaseProvider,
        context_manager: ContextManager,
        tool_registry: ToolRegistry,
        sandbox: Sandbox | None = None,
        approval: ApprovalManager | None = None,
        ui_display=None,  # UI 回调，Phase 7 注入
        max_tool_calls_per_turn: int = 25,
        system_prompt: str = "",
    ):
        self.provider = provider
        self.context_manager = context_manager
        self.tool_registry = tool_registry
        self.sandbox = sandbox
        self.approval = approval
        self.ui_display = ui_display  # 可选，有则调用其方法显示
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self._cancelled = False  # Ctrl+C 中断标志

    async def run(self, user_input: str) -> str:
        """处理一次用户输入，返回 assistant 最终文本回复。

        实现 agentic tool-use 循环：
        1. 将用户输入加入 context_manager
        2. while True:
            a. context = await context_manager.get_context()
            b. 调用 provider.chat(context, tools, system_prompt, stream=True)
            c. 流式处理 AgentEvent
            d. 将 assistant 消息（文本 + tool_calls）加入 context_manager
            e. 若有 tool_calls：执行工具，结果回传，continue
            f. 若无 tool_calls：break，返回累积文本

        Args:
            user_input: 用户输入文本。

        Returns:
            assistant 最终文本回复。被中断时返回已累积的文本。
        """
        # 重置中断标志
        self._cancelled = False

        # 显示用户输入
        self._emit_user_input(user_input)

        # 1. 将用户输入加入 context_manager
        await self.context_manager.add_message("user", user_input)

        # 获取工具定义（provider 原生格式）
        tools = self._get_tools_format()

        # 本轮累计工具调用次数
        tool_call_count = 0
        # 累积的最终文本（最后一次无工具调用的回复）
        final_text = ""

        # 2. agentic tool-use 循环
        while True:
            # 检查中断
            if self._cancelled:
                return final_text

            # a. 获取当前上下文
            context = await self.context_manager.get_context()

            # b. 调用 provider.chat，c. 流式处理 AgentEvent
            accumulated_text = ""
            tool_calls: list[ToolCall] = []
            stop_reason = ""

            async for event in self.provider.chat(
                context,
                tools=tools,
                system_prompt=self.system_prompt,
                stream=True,
            ):
                if self._cancelled:
                    # 中断：返回已累积的文本
                    return final_text

                if isinstance(event, TextDelta):
                    accumulated_text += event.text
                    self._emit_text_delta(event.text)
                elif isinstance(event, ThinkingDelta):
                    self._emit_thinking_delta(event.text)
                elif isinstance(event, ToolCall):
                    tool_calls.append(event)
                elif isinstance(event, Usage):
                    await self.context_manager.update_usage(
                        event.input_tokens, event.output_tokens
                    )
                    self._emit_usage(event.input_tokens, event.output_tokens)
                elif isinstance(event, Done):
                    stop_reason = event.stop_reason
                    # 结束本次 provider 调用
                    break

            # 检查 provider 错误（stop_reason 以 "error" 开头）
            if stop_reason.startswith("error"):
                error_msg = f"[LLM 调用失败: {stop_reason}]"
                self._emit_text_delta(error_msg)
                return final_text + error_msg if final_text else error_msg

            # d. 将 assistant 消息加入 context_manager
            # 使用 provider.format_assistant_message 构造 provider 原生格式 dict，
            # 将完整 dict 作为 content 存入 Message，provider 的 _convert_messages
            # 会检测并提取内部 dict（见 _convert_messages 中的嵌套 dict 处理）
            assistant_msg = self.provider.format_assistant_message(
                accumulated_text, tool_calls
            )
            await self.context_manager.add_message(
                assistant_msg["role"], assistant_msg
            )

            # 保存当前文本作为可能的最终回复
            if accumulated_text:
                final_text = accumulated_text

            # e. 若有 tool_calls：执行并继续循环
            if tool_calls:
                for tc in tool_calls:
                    if self._cancelled:
                        return final_text

                    tool_call_count += 1
                    if tool_call_count > self.max_tool_calls_per_turn:
                        limit_msg = (
                            f"\n[已达到单轮工具调用上限 "
                            f"({self.max_tool_calls_per_turn})，停止执行]"
                        )
                        self._emit_text_delta(limit_msg)
                        return final_text + limit_msg

                    # i. 显示工具调用信息
                    self._emit_tool_call(tc.name, tc.arguments)

                    # ii. 执行工具
                    result = await self._execute_tool(tc)

                    # iii. 显示工具结果
                    self._emit_tool_result(tc.name, result)

                    # iv. 将 tool_result 加入 context_manager
                    # 使用 provider.format_tool_result 构造 provider 原生格式 dict，
                    # 同样将完整 dict 作为 content 存入 Message
                    tool_result_role = self._get_tool_result_role()
                    tool_result_msg = self.provider.format_tool_result(
                        tool_result_role, tc.id, result
                    )
                    await self.context_manager.add_message(
                        tool_result_msg["role"], tool_result_msg
                    )

                # continue（继续循环让模型处理工具结果）
                continue

            # f. 若无 tool_calls：break，返回最终文本
            break

        return final_text

    def cancel(self) -> None:
        """中断当前 LLM 调用。

        设置 _cancelled 标志，run 方法会在下次检查时退出并返回已累积的文本。
        """
        self._cancelled = True

    # ========================================================================
    # 内部辅助方法
    # ========================================================================

    async def _execute_tool(self, tool_call: ToolCall) -> str:
        """执行单个工具调用，返回结果字符串。

        工具不存在或执行异常时返回 "Error: ..." 格式的字符串。

        Args:
            tool_call: 工具调用请求。

        Returns:
            工具执行结果文本。
        """
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            return f"Error: unknown tool '{tool_call.name}'"

        try:
            result = await tool.execute(
                tool_call.arguments, self.sandbox, self.approval
            )
            return result
        except Exception as e:
            return f"Error executing tool {tool_call.name}: {e}"

    def _get_tools_format(self) -> list[dict]:
        """根据 provider 类型返回对应格式的工具定义。

        - AnthropicProvider → to_anthropic_format()
        - 其他（DeepSeek 等 OpenAI 兼容）→ to_openai_format()

        Returns:
            工具定义列表（provider 原生格式）。
        """
        if isinstance(self.provider, AnthropicProvider):
            return self.tool_registry.to_anthropic_format()
        # 默认使用 OpenAI 格式（DeepSeek 及其他兼容 provider）
        return self.tool_registry.to_openai_format()

    def _get_tool_result_role(self) -> str:
        """根据 provider 类型返回工具结果消息的角色。

        - AnthropicProvider → "user"（tool_result 作为 user 消息的 content block）
        - 其他（DeepSeek 等 OpenAI 兼容）→ "tool"

        Returns:
            工具结果消息角色字符串。
        """
        if isinstance(self.provider, AnthropicProvider):
            return _TOOL_RESULT_ROLE_ANTHROPIC
        return _TOOL_RESULT_ROLE_OPENAI

    # ========================================================================
    # UI 回调辅助方法
    # ========================================================================
    # ui_display 是可选对象，若有则调用其方法显示；为 None 时用 print 简单输出。
    # 所有回调调用都包裹在 try/except 中，避免 UI 错误影响 Agent 循环。
    # Phase 7 会注入完整的 UI 回调对象。

    def _emit_user_input(self, text: str) -> None:
        """显示用户输入。"""
        if self.ui_display is not None:
            try:
                self.ui_display.on_user_input(text)
            except Exception:
                pass
        else:
            print(f"\nYou > {text}")

    def _emit_text_delta(self, text: str) -> None:
        """显示流式文本片段。"""
        if self.ui_display is not None:
            try:
                self.ui_display.on_text_delta(text)
            except Exception:
                pass
        else:
            print(text, end="", flush=True)

    def _emit_thinking_delta(self, text: str) -> None:
        """显示思考过程片段。"""
        if self.ui_display is not None:
            try:
                self.ui_display.on_thinking_delta(text)
            except Exception:
                pass
        # 默认不显示思考过程（避免干扰主输出）

    def _emit_tool_call(self, name: str, arguments: dict) -> None:
        """显示工具调用开始。"""
        if self.ui_display is not None:
            try:
                self.ui_display.on_tool_call(name, arguments)
            except Exception:
                pass
        else:
            print(f"\n[工具调用] {name}: {arguments}")

    def _emit_tool_result(self, name: str, result: str) -> None:
        """显示工具结果。"""
        if self.ui_display is not None:
            try:
                self.ui_display.on_tool_result(name, result)
            except Exception:
                pass
        else:
            # 截断过长的结果用于显示
            preview = (
                result if len(result) <= 500 else result[:500] + "...[truncated]"
            )
            print(f"[工具结果] {name}: {preview}")

    def _emit_usage(self, input_tokens: int, output_tokens: int) -> None:
        """显示用量信息。"""
        if self.ui_display is not None:
            try:
                self.ui_display.on_usage(input_tokens, output_tokens)
            except Exception:
                pass
        # 默认不显示用量（避免干扰主输出）


__all__ = ["AgentLoop", "DEFAULT_SYSTEM_PROMPT"]
