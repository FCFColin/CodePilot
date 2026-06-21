"""Agent 核心循环。

实现 agentic tool-use 循环：用户输入 → LLM 流式响应 → 工具调用执行
→ 结果回传 → 继续生成，直到 LLM 不再请求工具调用。

支持 Ctrl+C 中断、单轮工具调用上限、UI 回调（UICallback Protocol 注入）。
所有 UI 回调方法为 async，通过 UICallback Protocol 定义，由 Phase 7 的 UI 层实现。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

import structlog

from codepilot.context.manager import ContextManager
from codepilot.exceptions import ProviderError
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.base import (
    BaseProvider,
    Done,
    Message,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    Usage,
)
from codepilot.tools.registry import (
    ApprovalProtocol,
    SandboxProtocol,
    ToolRegistry,
)

if TYPE_CHECKING:
    from codepilot.hooks.registry import HookRegistry
    from codepilot.repomap import RepoMapper
    from codepilot.session.manager import SessionManager

logger = structlog.get_logger(__name__)


# 单轮内 lint 重试上限（避免无限循环）
MAX_LINT_RETRIES = 3


# ============================================================================
# 默认系统提示词（PRD 第九节，完整复制）
# ============================================================================

DEFAULT_SYSTEM_PROMPT = """\
You are CodePilot, an expert AI coding assistant operating in a terminal \
environment.

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
1. ALWAYS use tools to inspect the codebase before making changes. Read \
relevant files first.
2. When editing files, prefer edit_file (surgical changes) over write_file \
(full replacement) for existing files.
3. After making changes, verify them by reading the file back or running tests.
4. When running commands, explain what the command does before executing.
5. If a tool call fails or is blocked by security, acknowledge it and try an \
alternative approach.
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


# ============================================================================
# UICallback Protocol
# ============================================================================


@runtime_checkable
class UICallback(Protocol):
    """UI 回调协议，由 Phase 7 的 UI 层实现。

    所有方法为 async，AgentLoop 在对应事件发生时调用。
    实现方应确保方法不抛出异常（AgentLoop 也会包裹 try/except 保护循环）。
    """

    async def on_text_delta(self, text: str) -> None:
        """流式文本片段。"""
        ...

    async def on_thinking_delta(self, text: str) -> None:
        """思考过程片段。"""
        ...

    async def on_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        """工具调用开始。"""
        ...

    async def on_tool_result(self, name: str, result: str, success: bool) -> None:
        """工具执行结果，success 为 False 表示工具返回错误。"""
        ...

    async def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        """token 用量。"""
        ...

    async def on_error(self, error: str) -> None:
        """错误通知。"""
        ...

    async def on_turn_end(self) -> None:
        """单次 run 调用结束（无论正常/中断/错误）。"""
        ...


# ============================================================================
# AgentLoop 核心循环
# ============================================================================


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
        ui_callback: 可选的 UI 回调对象（Phase 7 注入）。
        max_tool_calls_per_turn: 单轮最大工具调用次数。
        system_prompt: 系统提示词。
        _cancelled: Ctrl+C 中断标志。
    """

    def __init__(
        self,
        provider: BaseProvider,
        context_manager: ContextManager,
        tool_registry: ToolRegistry,
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
        ui_callback: UICallback | None = None,
        max_tool_calls_per_turn: int = 25,
        system_prompt: str = "",
        session_manager: SessionManager | None = None,
        hook_registry: HookRegistry | None = None,
        max_lint_retries: int = MAX_LINT_RETRIES,
        repo_mapper: RepoMapper | None = None,
    ) -> None:
        self.provider = provider
        self.context_manager = context_manager
        self.tool_registry = tool_registry
        self.sandbox = sandbox
        self.approval = approval
        self.ui_callback = ui_callback
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.session_manager = session_manager
        self.hook_registry = hook_registry
        self.max_lint_retries = max_lint_retries
        self.repo_mapper = repo_mapper
        self._cancelled = False
        # 当前轮生效的系统提示（可能含 RepoMap 摘要）
        self._effective_system_prompt: str = self.system_prompt
        logger.debug(
            "AgentLoop 已初始化",
            max_tool_calls=max_tool_calls_per_turn,
            has_callback=ui_callback is not None,
            has_session=session_manager is not None,
            has_hooks=hook_registry is not None,
            max_lint_retries=max_lint_retries,
            has_repo_mapper=repo_mapper is not None,
        )

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
        self._cancelled = False
        try:
            return await self._run_loop(user_input)
        finally:
            # 无论正常结束、中断还是异常，都通知 UI 回调
            await self._emit_turn_end()
            # 保存会话记录（静默失败，不影响主流程）
            self._save_session()

    def _save_session(self) -> None:
        """保存会话记录到存储（静默失败）。"""
        if self.session_manager is None:
            return
        try:
            self.session_manager.save()
        except Exception as e:
            logger.warning("会话保存失败", error=str(e))

    def _record_session_message(self, role: str, content: str) -> None:
        """记录消息到 session（静默失败）。"""
        if self.session_manager is None:
            return
        try:
            self.session_manager.add_message(role, content)
        except Exception as e:
            logger.warning("session 记录消息失败", error=str(e))

    def _record_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        result: str,
        duration_ms: int,
    ) -> None:
        """记录工具调用到 session（静默失败）。"""
        if self.session_manager is None:
            return
        try:
            self.session_manager.record_tool_call(
                tool_name=name,
                arguments=arguments,
                result=result,
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.warning("session 记录工具调用失败", error=str(e))

    async def _run_loop(self, user_input: str) -> str:
        """实际循环逻辑（由 run 调用，run 负责 finally 中的 on_turn_end）。"""
        # 1. 将用户输入加入 context_manager
        await self.context_manager.add_message("user", user_input)
        self._record_session_message("user", user_input)
        logger.info("开始处理用户输入", input_length=len(user_input))

        # 1.5 生成 RepoMap 摘要并追加到系统提示（可选）
        self._effective_system_prompt = self._build_effective_system_prompt(user_input)

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
                logger.info("Agent 循环被中断")
                return final_text

            # a. 获取当前上下文（内部触发 maybe_compress，实现每次工具调用后检查压缩）
            context = await self.context_manager.get_context()

            # b. 调用 provider.chat，c. 流式处理 AgentEvent
            accumulated_text = ""
            accumulated_thinking = ""
            tool_calls: list[ToolCall] = []
            stop_reason = ""

            try:
                async for event in self.provider.chat(
                    cast(list[Message], context),
                    tools=tools,
                    system_prompt=self._effective_system_prompt,
                    stream=True,
                ):
                    if self._cancelled:
                        # 中断：返回已累积的文本
                        return final_text

                    if isinstance(event, TextDelta):
                        accumulated_text += event.text
                        await self._emit_text_delta(event.text)
                    elif isinstance(event, ThinkingDelta):
                        accumulated_thinking += event.text
                        await self._emit_thinking_delta(event.text)
                    elif isinstance(event, ToolCall):
                        tool_calls.append(event)
                    elif isinstance(event, Usage):
                        await self.context_manager.update_usage(
                            event.input_tokens, event.output_tokens
                        )
                        await self._emit_usage(event.input_tokens, event.output_tokens)
                    elif isinstance(event, Done):
                        stop_reason = event.stop_reason
                        # 结束本次 provider 调用
                        break
            except ProviderError as e:
                # Provider 调用失败：通知 UI，返回已累积文本 + 错误信息
                error_msg = f"[LLM 调用失败: {e}]"
                logger.error("Provider 调用失败", error=str(e))
                await self._emit_error(error_msg)
                return final_text + error_msg if final_text else error_msg

            # 检查 provider 错误（stop_reason 以 "error" 开头）
            if stop_reason.startswith("error"):
                error_msg = f"[LLM 调用失败: {stop_reason}]"
                logger.error("Provider 返回错误", stop_reason=stop_reason)
                await self._emit_error(error_msg)
                return final_text + error_msg if final_text else error_msg

            # d. 将 assistant 消息加入 context_manager
            # 使用 provider.format_assistant_message 构造 provider 原生格式 dict，
            # 将完整 dict 作为 content 存入 Message，provider 的 _convert_messages
            # 会检测并提取内部 dict（见 _convert_messages 中的嵌套 dict 处理）
            assistant_msg = self.provider.format_assistant_message(
                accumulated_text, tool_calls
            )
            await self.context_manager.add_message("assistant", assistant_msg)
            self._record_session_message("assistant", accumulated_text)

            # 记录 thinking 内容到 session
            if accumulated_thinking and self.session_manager is not None:
                try:
                    self.session_manager.add_thinking(accumulated_thinking)
                except Exception as e:
                    logger.warning("session 记录 thinking 失败", error=str(e))

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
                        logger.warning(
                            "达到工具调用上限",
                            limit=self.max_tool_calls_per_turn,
                        )
                        await self._emit_error(limit_msg.strip())
                        return final_text + limit_msg

                    # i. 显示工具调用信息
                    await self._emit_tool_call(tc.name, tc.arguments)

                    # ii. 执行工具（计时）
                    start_time = time.monotonic()
                    result = await self._execute_tool(tc)
                    duration_ms = int((time.monotonic() - start_time) * 1000)

                    # iii. 显示工具结果（success 由结果字符串前缀判断）
                    success = not result.startswith("Error")
                    await self._emit_tool_result(tc.name, result, success)

                    # 记录工具调用到 session
                    self._record_tool_call(tc.name, tc.arguments, result, duration_ms)

                    # iv. 触发 TOOL_CALL_AFTER Hook（如 LintHook）
                    # 若 Hook 返回 should_retry=True，将 retry_message 作为
                    # tool_result 追加到消息历史，触发新一轮 LLM 调用
                    # （最多重试 max_lint_retries 次，避免无限循环）
                    path = tc.arguments.get("path")
                    lint_retry_count = 0
                    if self.hook_registry is not None:
                        hook_result = self.hook_registry.trigger_tool_after(
                            tc.name, path, result
                        )
                        while (
                            hook_result is not None
                            and hook_result["should_retry"]
                            and lint_retry_count < self.max_lint_retries
                        ):
                            lint_retry_count += 1
                            retry_msg = hook_result.get("retry_message") or ""
                            logger.info(
                                "Lint 错误触发重试",
                                tool_name=tc.name,
                                path=path,
                                retry_count=lint_retry_count,
                                max_retries=self.max_lint_retries,
                            )
                            # 构造带 lint 重试计数的提示消息
                            lint_retry_text = (
                                f"[Lint 修复尝试 {lint_retry_count}/"
                                f"{self.max_lint_retries}] {retry_msg}"
                            )
                            # 通知 UI 显示 lint 重试提示
                            await self._emit_tool_result(
                                tc.name, lint_retry_text, success=False
                            )
                            # 将 retry_message 作为 tool_result 追加到消息历史
                            tool_result_role = self._get_tool_result_role()
                            retry_tool_result = self.provider.format_tool_result(
                                tool_result_role,
                                tc.id,
                                lint_retry_text,
                            )
                            await self.context_manager.add_message(
                                tool_result_role, retry_tool_result
                            )

                            # 触发新一轮 LLM 调用，期望模型修复 lint 错误
                            new_result = await self._call_llm_for_retry(tc)
                            if new_result is None:
                                # LLM 未返回工具调用或被中断：跳出重试循环
                                break
                            result = new_result
                            # 显示重试后的工具结果
                            success = not result.startswith("Error")
                            await self._emit_tool_result(tc.name, result, success)
                            # 再次触发 Hook 检查修复后的文件
                            hook_result = self.hook_registry.trigger_tool_after(
                                tc.name, path, result
                            )

                    # v. 将最终 tool_result 加入 context_manager
                    # 使用 provider.format_tool_result 构造 provider 原生格式 dict，
                    # 同样将完整 dict 作为 content 存入 Message
                    tool_result_role = self._get_tool_result_role()
                    tool_result_msg = self.provider.format_tool_result(
                        tool_result_role, tc.id, result
                    )
                    await self.context_manager.add_message(
                        tool_result_role, tool_result_msg
                    )

                # continue（继续循环让模型处理工具结果）
                continue

            # f. 若无 tool_calls：break，返回最终文本
            break

        logger.info("Agent 循环完成", tool_calls=tool_call_count)
        return final_text

    def _build_effective_system_prompt(self, user_input: str) -> str:
        """构造当前轮生效的系统提示。

        若 repo_mapper 可用，调用 build_for_query 生成仓库结构摘要并
        追加到系统提示末尾；否则返回原始系统提示。

        Args:
            user_input: 当前用户输入，用于驱动相关性排序。

        Returns:
            可能含 RepoMap 摘要的系统提示字符串。
        """
        if self.repo_mapper is None:
            return self.system_prompt
        try:
            map_text = self.repo_mapper.build_for_query(user_input)
        except Exception as e:
            logger.warning("RepoMap 生成失败", error=str(e))
            return self.system_prompt
        if not map_text:
            return self.system_prompt
        return f"{self.system_prompt}\n\n## 当前仓库结构摘要\n{map_text}"

    def cancel(self) -> None:
        """中断当前 LLM 调用。

        设置 _cancelled 标志，run 方法会在下次检查时退出并返回已累积的文本。
        """
        self._cancelled = True
        logger.info("收到中断请求")

    # ========================================================================
    # 内部辅助方法
    # ========================================================================

    async def _execute_tool(self, tool_call: ToolCall) -> str:
        """执行单个工具调用，返回结果字符串。

        工具不存在或执行异常时返回 "Error: ..." 格式的字符串。

        Args:
            tool_call: 工具调用请求。

        Returns:
            工具执行结果文本。出错时返回 "Error: ..." 格式。
        """
        tool = self.tool_registry.get(tool_call.name)
        if tool is None:
            logger.warning("未知工具", name=tool_call.name)
            return f"Error: unknown tool '{tool_call.name}'"

        try:
            result = await tool.execute(
                tool_call.arguments, self.sandbox, self.approval
            )
            return result
        except Exception as e:
            # 捕获 ToolError/SecurityError 及其他异常，优雅处理
            logger.error("工具执行异常", name=tool_call.name, error=str(e))
            return f"Error executing tool {tool_call.name}: {e}"

    async def _call_llm_for_retry(self, original_tool_call: ToolCall) -> str | None:
        """在 lint 重试循环中调用 LLM 获取修复后的工具调用并执行。

        获取当前上下文，调用 provider.chat，期望模型返回修复后的工具调用。
        若模型返回工具调用，则执行并返回结果字符串；若返回纯文本或被中断，返回 None。

        Args:
            original_tool_call: 原始触发 lint 错误的工具调用（用于日志）。

        Returns:
            修复后的工具执行结果字符串；LLM 未返回工具调用或被中断时返回 None。
        """
        if self._cancelled:
            return None

        context = await self.context_manager.get_context()
        tools = self._get_tools_format()
        retry_tool_calls: list[ToolCall] = []
        retry_text = ""

        try:
            async for event in self.provider.chat(
                cast(list[Message], context),
                tools=tools,
                system_prompt=self._effective_system_prompt,
                stream=True,
            ):
                if self._cancelled:
                    return None
                if isinstance(event, TextDelta):
                    retry_text += event.text
                    await self._emit_text_delta(event.text)
                elif isinstance(event, ThinkingDelta):
                    await self._emit_thinking_delta(event.text)
                elif isinstance(event, ToolCall):
                    retry_tool_calls.append(event)
                elif isinstance(event, Usage):
                    await self.context_manager.update_usage(
                        event.input_tokens, event.output_tokens
                    )
                    await self._emit_usage(event.input_tokens, event.output_tokens)
                elif isinstance(event, Done):
                    break
        except ProviderError as e:
            logger.warning(
                "Lint 重试时 LLM 调用失败",
                error=str(e),
                original_tool=original_tool_call.name,
            )
            await self._emit_error(f"[LLM 调用失败: {e}]")
            return None

        # 将 assistant 消息加入 context_manager
        assistant_msg = self.provider.format_assistant_message(
            retry_text, retry_tool_calls
        )
        await self.context_manager.add_message("assistant", assistant_msg)
        self._record_session_message("assistant", retry_text)

        if not retry_tool_calls:
            # LLM 未返回工具调用：返回 None，由调用方跳出重试循环
            return None

        # 执行第一个工具调用（修复后的 write_file/edit_file）
        retry_tc = retry_tool_calls[0]
        await self._emit_tool_call(retry_tc.name, retry_tc.arguments)
        start_time = time.monotonic()
        retry_result = await self._execute_tool(retry_tc)
        duration_ms = int((time.monotonic() - start_time) * 1000)
        self._record_tool_call(
            retry_tc.name, retry_tc.arguments, retry_result, duration_ms
        )

        # 将修复后的 tool_result 加入 context_manager
        tool_result_role = self._get_tool_result_role()
        retry_tool_result_msg = self.provider.format_tool_result(
            tool_result_role, retry_tc.id, retry_result
        )
        await self.context_manager.add_message(tool_result_role, retry_tool_result_msg)

        return retry_result

    def _get_tools_format(self) -> list[dict[str, Any]]:
        """根据 provider 类型返回对应格式的工具定义。

        - AnthropicProvider → to_anthropic_format()
        - 其他（DeepSeek 等 OpenAI 兼容）→ to_openai_format()

        Returns:
            工具定义列表（provider 原生格式）。
        """
        if isinstance(self.provider, AnthropicProvider):
            return cast(
                list[dict[str, Any]],
                self.tool_registry.to_anthropic_format(),
            )
        # 默认使用 OpenAI 格式（DeepSeek 及其他兼容 provider）
        return cast(
            list[dict[str, Any]],
            self.tool_registry.to_openai_format(),
        )

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
    # ui_callback 是可选对象，若有则调用其方法；为 None 时静默跳过。
    # 所有回调调用都包裹在 try/except 中，避免 UI 错误影响 Agent 循环。

    async def _emit_text_delta(self, text: str) -> None:
        """通知流式文本片段。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_text_delta(text)
            except Exception as e:
                logger.warning("on_text_delta 回调失败", error=str(e))

    async def _emit_thinking_delta(self, text: str) -> None:
        """通知思考过程片段。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_thinking_delta(text)
            except Exception as e:
                logger.warning("on_thinking_delta 回调失败", error=str(e))

    async def _emit_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        """通知工具调用开始。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_tool_call(name, arguments)
            except Exception as e:
                logger.warning("on_tool_call 回调失败", error=str(e))

    async def _emit_tool_result(self, name: str, result: str, success: bool) -> None:
        """通知工具执行结果。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_tool_result(name, result, success)
            except Exception as e:
                logger.warning("on_tool_result 回调失败", error=str(e))

    async def _emit_usage(self, input_tokens: int, output_tokens: int) -> None:
        """通知用量信息。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_usage(input_tokens, output_tokens)
            except Exception as e:
                logger.warning("on_usage 回调失败", error=str(e))

    async def _emit_error(self, error: str) -> None:
        """通知错误。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_error(error)
            except Exception as e:
                logger.warning("on_error 回调失败", error=str(e))

    async def _emit_turn_end(self) -> None:
        """通知单次 run 调用结束。"""
        if self.ui_callback is not None:
            try:
                await self.ui_callback.on_turn_end()
            except Exception as e:
                logger.warning("on_turn_end 回调失败", error=str(e))


__all__ = ["AgentLoop", "UICallback", "DEFAULT_SYSTEM_PROMPT"]
