"""UI 显示模块。

实现 UIDisplay 类，提供 AgentLoop 需要的所有回调方法，
使用 rich 库渲染用户输入面板、工具调用面板、assistant 流式回复、
token 用量状态栏、安全拒绝面板、压缩通知等。

参考 PRD 8.2 节的显示格式设计。
"""

from __future__ import annotations

import json

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from codepilot.config import UIConfig


# ============================================================================
# 费用估算（每百万 token 美元价格）
# ============================================================================

# DeepSeek 定价（参考官方公开价格）
_DEEPSEEK_PRICING = {"input": 0.14, "output": 0.28}
# Anthropic Claude 定价（参考官方公开价格）
_ANTHROPIC_PRICING = {"input": 3.0, "output": 15.0}


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    provider_name: str,
) -> float:
    """根据 provider 类型估算费用（美元）。

    Args:
        input_tokens: 输入 token 数。
        output_tokens: 输出 token 数。
        provider_name: provider 名称（deepseek/anthropic）。

    Returns:
        估算费用（美元）。
    """
    pricing = _ANTHROPIC_PRICING if provider_name == "anthropic" else _DEEPSEEK_PRICING
    cost = (
        input_tokens * pricing["input"] / 1_000_000
        + output_tokens * pricing["output"] / 1_000_000
    )
    return cost


def _format_arguments(arguments: dict) -> str:
    """将工具参数字典格式化为紧凑字符串。

    Args:
        arguments: 参数字典。

    Returns:
        格式化后的参数字符串，如 `path="main.py", content="..."`。
    """
    if not arguments:
        return ""
    parts: list[str] = []
    for key, value in arguments.items():
        if isinstance(value, str):
            # 字符串值截断到 80 字符
            display = value if len(value) <= 80 else value[:77] + "..."
            parts.append(f'{key}="{display}"')
        elif isinstance(value, (int, float, bool)):
            parts.append(f"{key}={value}")
        elif value is None:
            parts.append(f"{key}=None")
        else:
            # dict/list 用 JSON 表示，截断到 100 字符
            json_str = json.dumps(value, ensure_ascii=False)
            if len(json_str) > 100:
                json_str = json_str[:97] + "..."
            parts.append(f"{key}={json_str}")
    return ", ".join(parts)


class UIDisplay:
    """UI 显示回调类，供 AgentLoop 调用。

    使用 rich 库渲染所有 UI 元素：
    - 用户输入面板（青色边框）
    - assistant 流式回复（Live 实时更新，最终面板包裹）
    - 工具调用与结果面板
    - token 用量底部状态栏
    - 安全拒绝、压缩通知、错误等面板
    """

    def __init__(
        self,
        config: UIConfig,
        provider_name: str = "deepseek",
        context_manager=None,
    ) -> None:
        self.config = config
        self.provider_name = provider_name
        self.context_manager = context_manager
        self.console = Console()

        # 流式文本累积状态
        self._live: Live | None = None
        self._current_text: str = ""

    # ------------------------------------------------------------------
    # 流式文本管理
    # ------------------------------------------------------------------

    def _start_live(self) -> None:
        """启动 Live 实时显示 assistant 回复面板。"""
        if self._live is not None:
            return
        self._current_text = ""
        self._live = Live(
            self._build_assistant_panel(),
            console=self.console,
            refresh_per_second=15,
            transient=False,
        )
        self._live.start()

    def _stop_live(self) -> None:
        """停止 Live，将最终面板输出到控制台。"""
        if self._live is not None:
            try:
                self._live.update(self._build_assistant_panel())
                self._live.stop()
            except Exception:
                # Live 停止失败时尝试强制停止
                try:
                    self._live.stop()
                except Exception:
                    pass
            self._live = None
            self._current_text = ""

    def _build_assistant_panel(self) -> Panel:
        """构建 assistant 回复面板。"""
        content = Text(self._current_text) if self._current_text else Text("...", style="dim italic")
        return Panel(
            content,
            title="Assistant",
            border_style="green",
            padding=(0, 1),
        )

    def finalize(self) -> None:
        """结束当前流式显示（main.py 在 agent_loop.run() 后调用）。"""
        self._stop_live()

    # ------------------------------------------------------------------
    # AgentLoop 回调方法
    # ------------------------------------------------------------------

    def on_user_input(self, text: str) -> None:
        """显示用户输入面板（标题"用户输入"，青色边框）。"""
        self._stop_live()
        content = Text(f"You › {text}", style="cyan")
        panel = Panel(
            content,
            title="用户输入",
            border_style="cyan",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_text_delta(self, text: str) -> None:
        """流式输出文本。用 rich.live.Live 实时更新 assistant 面板。"""
        if self._live is None:
            self._start_live()
        self._current_text += text
        if self._live is not None:
            try:
                self._live.update(self._build_assistant_panel())
            except Exception:
                pass

    def on_thinking_delta(self, text: str) -> None:
        """显示思考过程（灰色/暗色，斜体）。"""
        # 思考过程不进入 Live，直接打印
        self._stop_live()
        if not self.config.show_thinking:
            return
        content = Text(text, style="dim italic")
        panel = Panel(
            content,
            title="Thinking",
            border_style="dim",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_tool_call(self, name: str, arguments: dict) -> None:
        """显示工具调用面板（标题"工具调用"，显示工具名和参数）。"""
        self._stop_live()
        if not self.config.show_tool_calls:
            return
        args_str = _format_arguments(arguments)
        content = Text()
        content.append(Text(f"🔧 {name}", style="bold yellow"))
        if args_str:
            content.append(Text(f"({args_str})", style="yellow"))
        panel = Panel(
            content,
            title="工具调用",
            border_style="yellow",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_tool_result(self, name: str, result: str) -> None:
        """显示工具结果（截断到 max_diff_lines 行）。"""
        self._stop_live()
        if not self.config.show_tool_calls:
            return
        max_lines = self.config.max_diff_lines
        lines = result.splitlines()
        truncated = len(lines) > max_lines
        visible_lines = lines[:max_lines]

        content = Text()
        content.append(Text(f"📋 {name} 结果:\n", style="bold blue"))
        for i, line in enumerate(visible_lines):
            content.append(Text(line))
            if i < len(visible_lines) - 1:
                content.append("\n")

        if truncated:
            omitted = len(lines) - max_lines
            content.append("\n")
            content.append(
                f"... [{omitted} more lines omitted]",
                style="dim italic",
            )

        panel = Panel(
            content,
            title="工具结果",
            border_style="blue",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_usage(self, input_tokens: int, output_tokens: int) -> None:
        """显示 token 用量底部状态栏。"""
        self._stop_live()
        if not self.config.show_token_usage:
            return

        total_tokens = input_tokens + output_tokens

        # 从 context_manager 获取累计用量与上下文占比
        cumulative_input = input_tokens
        cumulative_output = output_tokens
        context_tokens = total_tokens
        max_tokens = 0
        usage_percent = 0.0
        if self.context_manager is not None:
            try:
                stats = self.context_manager.get_stats()
                cumulative_input = stats.get("input_tokens", input_tokens)
                cumulative_output = stats.get("output_tokens", output_tokens)
                context_tokens = stats.get("total_tokens", total_tokens)
                max_tokens = stats.get("max_tokens", 0)
                usage_percent = stats.get("usage_percent", 0.0) * 100
            except Exception:
                pass

        cumulative_total = cumulative_input + cumulative_output

        # 费用估算
        cost = 0.0
        if self.config.show_cost_estimate:
            cost = _estimate_cost(
                cumulative_input, cumulative_output, self.provider_name
            )

        # 构建状态栏文本
        content = Text()
        content.append(
            f"   Input: {cumulative_input:,} | Output: {cumulative_output:,} | Total: {cumulative_total:,}\n",
            style="bold",
        )
        if max_tokens > 0:
            content.append(
                f"   Context: {context_tokens:,} / {max_tokens:,} ({usage_percent:.1f}%)",
                style="cyan",
            )
        else:
            content.append(
                f"   Context: {context_tokens:,}",
                style="cyan",
            )
        if self.config.show_cost_estimate:
            content.append(
                f" | Est. Cost: ${cost:.4f}",
                style="green",
            )

        panel = Panel(
            content,
            title="Token Usage",
            border_style="dim",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_security_block(self, operation: str, reason: str) -> None:
        """显示安全拒绝面板（SECURITY BLOCK，红色）。"""
        self._stop_live()
        content = Text()
        content.append(Text("🛑 ", style="bold red"))
        content.append(Text(f"Operation: {operation}\n", style="bold"))
        content.append(Text(f"Reason:    {reason}", style="red"))
        panel = Panel(
            content,
            title="🛑 SECURITY BLOCK",
            border_style="red",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_compression(self, stats: dict) -> None:
        """显示上下文压缩通知面板。"""
        self._stop_live()
        before = stats.get("before_tokens", 0)
        after = stats.get("after_tokens", 0)
        strategy = stats.get("strategy", "unknown")
        messages_compressed = stats.get("messages_compressed", 0)

        # 计算缩减比例
        reduction = 0.0
        if before > 0:
            reduction = (1 - after / before) * 100

        content = Text()
        content.append(Text(f"Strategy:   {strategy}\n", style="cyan"))
        content.append(Text(f"Compressed: {messages_compressed} messages\n", style="cyan"))
        content.append(Text(f"Before:     {before:,} tokens\n", style="yellow"))
        content.append(Text(f"After:      {after:,} tokens ", style="green"))
        content.append(Text(f"({reduction:.1f}% reduction)", style="bold green"))

        panel = Panel(
            content,
            title="📦 Context Compression",
            border_style="magenta",
            padding=(0, 1),
        )
        self.console.print(panel)

    def on_error(self, message: str) -> None:
        """显示错误信息（红色）。"""
        self._stop_live()
        content = Text(f"❌ {message}", style="bold red")
        panel = Panel(
            content,
            title="Error",
            border_style="red",
            padding=(0, 1),
        )
        self.console.print(panel)

    # ------------------------------------------------------------------
    # Slash 命令显示方法
    # ------------------------------------------------------------------

    def show_stats(self, stats: dict) -> None:
        """显示详细统计（/stats 命令）。

        Args:
            stats: 统计字典，含 total_tokens/max_tokens/usage_percent/
                   message_count/compressed/input_tokens/output_tokens。
        """
        self._stop_live()
        total = stats.get("total_tokens", 0)
        max_tokens = stats.get("max_tokens", 0)
        usage_percent = stats.get("usage_percent", 0.0) * 100
        message_count = stats.get("message_count", 0)
        compressed = stats.get("compressed", False)
        input_tokens = stats.get("input_tokens", 0)
        output_tokens = stats.get("output_tokens", 0)

        cost = _estimate_cost(
            input_tokens, output_tokens, self.provider_name
        )

        content = Text()
        content.append(Text("=== 会话统计 ===\n\n", style="bold cyan"))
        content.append(Text("Token 用量:\n", style="bold yellow"))
        content.append(Text(f"  累计 Input:  {input_tokens:,}\n"))
        content.append(Text(f"  累计 Output: {output_tokens:,}\n"))
        content.append(Text(f"  总计:        {input_tokens + output_tokens:,}\n\n"))

        content.append(Text("上下文:\n", style="bold yellow"))
        content.append(Text(f"  当前 token:  {total:,}\n"))
        content.append(Text(f"  最大 token:  {max_tokens:,}\n"))
        content.append(Text(f"  使用率:      {usage_percent:.1f}%\n"))
        content.append(Text(f"  消息数:      {message_count}\n"))
        content.append(Text(f"  已压缩:      {'是' if compressed else '否'}\n\n"))

        content.append(Text("费用:\n", style="bold yellow"))
        content.append(Text(f"  估算费用:    ${cost:.4f}\n"))

        panel = Panel(
            content,
            title="Stats",
            border_style="cyan",
            padding=(0, 1),
        )
        self.console.print(panel)

    def show_config(self, config) -> None:
        """显示当前配置（/config 命令）。

        Args:
            config: Config 对象。
        """
        self._stop_live()

        content = Text()
        content.append(Text("=== 当前配置 ===\n\n", style="bold cyan"))

        content.append(Text("Provider:\n", style="bold yellow"))
        content.append(Text(f"  provider: {config.provider}\n"))
        if config.provider == "anthropic":
            content.append(Text(f"  model:    {config.anthropic.model}\n"))
            content.append(Text(f"  base_url: {config.anthropic.base_url}\n"))
            content.append(Text(f"  api_key:  {'***' + config.anthropic.api_key[-4:] if config.anthropic.api_key else '(未设置)'}\n"))
            content.append(Text(f"  max_tokens: {config.anthropic.max_tokens}\n"))
            content.append(Text(f"  temperature: {config.anthropic.temperature}\n"))
        else:
            content.append(Text(f"  model:    {config.deepseek.model}\n"))
            content.append(Text(f"  base_url: {config.deepseek.base_url}\n"))
            content.append(Text(f"  api_key:  {'***' + config.deepseek.api_key[-4:] if config.deepseek.api_key else '(未设置)'}\n"))
            content.append(Text(f"  max_tokens: {config.deepseek.max_tokens}\n"))
            content.append(Text(f"  temperature: {config.deepseek.temperature}\n"))
            content.append(Text(f"  thinking: enabled={config.deepseek.thinking.enabled}\n"))
        content.append(Text("\n"))

        content.append(Text("Security:\n", style="bold yellow"))
        content.append(Text(f"  workspace_root: {config.security.workspace_root}\n"))
        content.append(Text(f"  allowed_dirs:   {config.security.allowed_dirs}\n"))
        content.append(Text(f"  require_approval_for: {config.security.require_approval_for}\n"))
        content.append(Text(f"  auto_approve_read: {config.security.auto_approve_read}\n"))
        content.append(Text(f"  command_whitelist_mode: {config.security.command_whitelist_mode}\n"))
        content.append(Text("\n"))

        content.append(Text("Context:\n", style="bold yellow"))
        content.append(Text(f"  max_tokens:            {config.context.max_tokens}\n"))
        content.append(Text(f"  compression_threshold: {config.context.compression_threshold}\n"))
        content.append(Text(f"  critical_threshold:    {config.context.critical_threshold}\n"))
        content.append(Text(f"  preserve_recent_turns: {config.context.preserve_recent_turns}\n"))
        content.append(Text(f"  compression_strategy:  {config.context.compression_strategy}\n"))
        content.append(Text(f"  save_full_history:     {config.context.save_full_history}\n"))
        content.append(Text("\n"))

        content.append(Text("UI:\n", style="bold yellow"))
        content.append(Text(f"  theme:            {config.ui.theme}\n"))
        content.append(Text(f"  show_token_usage: {config.ui.show_token_usage}\n"))
        content.append(Text(f"  show_cost_estimate: {config.ui.show_cost_estimate}\n"))
        content.append(Text(f"  show_tool_calls:  {config.ui.show_tool_calls}\n"))
        content.append(Text(f"  show_thinking:    {config.ui.show_thinking}\n"))
        content.append(Text(f"  max_diff_lines:   {config.ui.max_diff_lines}\n"))

        panel = Panel(
            content,
            title="Config",
            border_style="cyan",
            padding=(0, 1),
        )
        self.console.print(panel)

    def show_history(self, messages: list) -> None:
        """显示对话历史概要（/history 命令）。

        Args:
            messages: Message 对象列表（context_manager.messages）。
        """
        self._stop_live()

        if not messages:
            self.console.print(
                Panel(
                    Text("(无对话历史)", style="dim italic"),
                    title="History",
                    border_style="cyan",
                    padding=(0, 1),
                )
            )
            return

        content = Text()
        content.append(Text(f"=== 对话历史 ({len(messages)} 条消息) ===\n\n", style="bold cyan"))

        for i, msg in enumerate(messages, 1):
            # 提取 role 和 content
            if isinstance(msg, dict):
                role = msg.get("role", "unknown")
                msg_content = msg.get("content", "")
            else:
                role = getattr(msg, "role", "unknown")
                msg_content = getattr(msg, "content", "")

            # content 转为文本
            if isinstance(msg_content, str):
                text = msg_content
            elif isinstance(msg_content, list):
                # Anthropic content blocks
                parts: list[str] = []
                for block in msg_content:
                    if isinstance(block, dict):
                        block_type = block.get("type", "")
                        if block_type == "text":
                            parts.append(block.get("text", ""))
                        elif block_type == "tool_use":
                            parts.append(f"[tool_use: {block.get('name', '')}]")
                        elif block_type == "tool_result":
                            parts.append("[tool_result]")
                        else:
                            parts.append(f"[{block_type}]")
                    elif isinstance(block, str):
                        parts.append(block)
                text = " ".join(parts)
            elif isinstance(msg_content, dict):
                text = json.dumps(msg_content, ensure_ascii=False)[:200]
            else:
                text = str(msg_content)

            # 截断到 100 字符
            if len(text) > 100:
                text = text[:97] + "..."

            # role 颜色
            role_style = "cyan"
            if role == "user":
                role_style = "bold cyan"
            elif role == "assistant":
                role_style = "bold green"
            elif role == "tool":
                role_style = "yellow"
            elif role == "system":
                role_style = "dim"

            content.append(Text(f"{i}. [{role}] ", style=role_style))
            content.append(Text(text))
            if i < len(messages):
                content.append("\n")

        panel = Panel(
            content,
            title="History",
            border_style="cyan",
            padding=(0, 1),
        )
        self.console.print(panel)

    def show_help(self) -> None:
        """显示帮助信息（/help 命令）。"""
        self._stop_live()
        content = Text()
        content.append(Text("=== 可用命令 ===\n\n", style="bold cyan"))
        commands = [
            ("/help", "显示此帮助信息"),
            ("/config", "显示当前配置"),
            ("/stats", "显示详细统计（token、工具调用、费用）"),
            ("/clear", "清空对话历史"),
            ("/compact", "手动触发上下文压缩"),
            ("/history", "显示对话历史概要"),
            ("/model <name>", "切换模型"),
            ("/provider <p>", "切换 provider（deepseek/anthropic）"),
            ("/approve", "切换自动批准模式（YOLO 模式）"),
            ("/undo", "撤销最近的文件操作"),
            ("/quit 或 /exit", "退出"),
        ]
        for cmd, desc in commands:
            content.append(Text(f"  {cmd:<20}", style="bold yellow"))
            content.append(Text(f" {desc}\n"))

        panel = Panel(
            content,
            title="Help",
            border_style="cyan",
            padding=(0, 1),
        )
        self.console.print(panel)


__all__ = ["UIDisplay"]
