"""启动 banner 显示模块。

使用 rich 库打印启动 banner，包含 ASCII art、版本号、Provider 信息、
Workspace 路径、安全状态、上下文配置和可用 slash 命令列表。
主题色采用 monokai 风格（绿色/青色为主）。
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from codepilot.config import Config


# 版本号常量
VERSION = "0.1.0"

# CodePilot ASCII art（参考 PRD 8.1 节样式，手写实现）
_BANNER_ASCII = r"""
   ██████╗ ██████╗ ██████╗ ███████╗██████╗ ██╗██╗
  ██╔════╝██╔═══██╗██╔══██╗██╔════╝██╔══██╗██║██║
  ██║     ██║   ██║██║  ██║█████╗  ██████╔╝██║██║
  ██║     ██║   ██║██║  ██║██╔══╝  ██╔═══╝ ██║██║
  ╚██████╗╚██████╔╝██████╔╝███████╗██║     ██║███████╗
   ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝     ╚═╝╚══════╝
"""

# 可用 slash 命令列表
_SLASH_COMMANDS = "/help  /config  /clear  /compact  /stats  /undo  /history  /quit"


def _get_provider_display(config: Config) -> str:
    """获取 Provider 显示文本，如 "DeepSeek (deepseek-chat)"。"""
    if config.provider == "anthropic":
        return f"Anthropic ({config.anthropic.model})"
    return f"DeepSeek ({config.deepseek.model})"


def _get_security_display(config: Config) -> str:
    """获取安全状态显示文本。

    Sandbox 始终为 ON；Approval 状态取决于 require_approval_for 是否为空
    （--no_approve 会清空该列表，进入 YOLO 模式）。
    """
    sandbox_status = "ON"
    approval_status = "ON" if config.security.require_approval_for else "OFF"
    return f"Sandbox {sandbox_status} | Approval {approval_status}"


def _get_context_display(config: Config) -> str:
    """获取上下文配置显示文本，如 "120K tokens max | Auto-compress at 70%"。"""
    max_tokens_k = config.context.max_tokens // 1000
    compress_pct = int(config.context.compression_threshold * 100)
    return f"{max_tokens_k}K tokens max | Auto-compress at {compress_pct}%"


def show_banner(config: Config, console: Console | None = None) -> None:
    """打印启动 banner。

    使用 rich.panel.Panel 包裹所有内容，主题色采用 monokai 风格
    （绿色/青色为主）。ASCII art 用青色，标题行用绿色，其余信息用默认色。
    """
    if console is None:
        console = Console()

    # 构建 banner 内容
    lines: list[Text] = []

    # ASCII art（青色）
    ascii_text = Text(_BANNER_ASCII.rstrip("\n"), style="cyan")
    lines.append(ascii_text)
    lines.append(Text(""))

    # 版本标题行（绿色加粗）
    title_text = Text(f"AI Coding Agent CLI v{VERSION}", style="bold green")
    lines.append(title_text)
    lines.append(Text(""))

    # Provider 信息
    lines.append(Text(f"Provider:  {_get_provider_display(config)}", style="green"))
    # Workspace 路径
    lines.append(Text(f"Workspace: {config.security.workspace_root}", style="green"))
    # 安全状态
    lines.append(Text(f"Security:  {_get_security_display(config)}", style="green"))
    # 上下文配置
    lines.append(Text(f"Context:   {_get_context_display(config)}", style="green"))
    lines.append(Text(""))

    # slash 命令列表
    lines.append(Text("Commands:", style="yellow"))
    lines.append(Text(f"  {_SLASH_COMMANDS}", style="yellow"))

    # 合并为单个 Text
    content = Text()
    for i, line in enumerate(lines):
        content.append(line)
        if i < len(lines) - 1:
            content.append("\n")

    # 用 Panel 包裹，边框使用绿色（monokai 风格）
    panel = Panel(
        Align.center(content),
        border_style="green",
        padding=(1, 2),
        title=None,
    )
    console.print(panel)
