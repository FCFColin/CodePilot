"""CodePilot CLI 入口文件。

集成所有组件：配置加载、Provider、上下文管理、工具系统、安全沙箱、
审批管理、Agent 循环、UI 显示，实现完整的交互式 REPL 与单次执行模式。

支持 slash 命令、Ctrl+C 中断、Ctrl+D 退出、错误优雅处理。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

# 确保以 `python codepilot/main.py` 方式运行时也能正确导入 codepilot 包
# 将项目根目录（codepilot 的父目录）加入 sys.path
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console

from codepilot.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop
from codepilot.config import Config, load_config
from codepilot.context.compressor import ContextCompressor
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.base import BaseProvider
from codepilot.providers.deepseek import DeepSeekProvider
from codepilot.security.approval import ApprovalManager
from codepilot.security.sandbox import Sandbox
from codepilot.tools.registry import ToolRegistry
from codepilot.ui.banner import VERSION, show_banner
from codepilot.ui.display import UIDisplay


# REPL 输入提示符
_PROMPT_TEXT = "You › "

# 输入历史文件路径（用户目录下）
_HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".codepilot_history")


# ============================================================================
# 撤销追踪器
# ============================================================================

class UndoTracker:
    """文件操作撤销追踪器。

    包装 WriteFileTool 和 EditFileTool 的 execute 方法，
    在执行前记录文件原内容，支持 /undo 命令恢复。
    """

    def __init__(self) -> None:
        # 操作栈：[(abs_path, old_content_or_None), ...]
        # old_content 为 None 表示文件原本不存在（新建）
        self._stack: list[tuple[str, str | None]] = []

    def wrap_tool(self, tool: Any, workspace_root: str) -> None:
        """包装工具的 execute 方法，在执行前记录原内容。

        Args:
            tool: 要包装的工具对象（WriteFileTool 或 EditFileTool）。
            workspace_root: 工作区根目录（用于解析相对路径）。
        """
        original_execute = tool.execute

        async def wrapped_execute(arguments: dict, sandbox=None, approval=None) -> str:
            path = arguments.get("path", "")
            if path:
                # 解析绝对路径
                if os.path.isabs(path):
                    abs_path = os.path.realpath(path)
                else:
                    abs_path = os.path.realpath(os.path.join(workspace_root, path))
                # 记录原内容（不存在则为 None）
                old_content = self._read_file(abs_path)
                self._stack.append((abs_path, old_content))
            return await original_execute(arguments, sandbox, approval)

        tool.execute = wrapped_execute

    def undo(self) -> tuple[bool, str]:
        """撤销最近一次文件操作。

        Returns:
            (success, message) 元组。
        """
        if not self._stack:
            return False, "没有可撤销的文件操作"
        abs_path, old_content = self._stack.pop()
        try:
            if old_content is None:
                # 文件原本不存在：删除
                if os.path.isfile(abs_path):
                    os.remove(abs_path)
                    return True, f"已删除新建的文件: {abs_path}"
                return True, f"文件已不存在: {abs_path}"
            else:
                # 恢复原内容
                parent = os.path.dirname(abs_path)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write(old_content)
                return True, f"已恢复文件: {abs_path}"
        except OSError as e:
            return False, f"撤销失败: {e}"

    @staticmethod
    def _read_file(abs_path: str) -> str | None:
        """读取文件内容，不存在返回 None。"""
        if not os.path.isfile(abs_path):
            return None
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None


# ============================================================================
# 会话状态
# ============================================================================

class SessionState:
    """会话状态，持有所有可变组件引用。

    /provider 和 /model 命令需要重新创建 provider 和 agent_loop，
    通过 SessionState 在 REPL 循环中传递更新后的引用。
    """

    def __init__(
        self,
        config: Config,
        provider: BaseProvider,
        context_manager: ContextManager,
        tool_registry: ToolRegistry,
        sandbox: Sandbox,
        approval: ApprovalManager,
        agent_loop: AgentLoop,
        ui: UIDisplay,
        undo_tracker: UndoTracker,
    ) -> None:
        self.config = config
        self.provider = provider
        self.context_manager = context_manager
        self.tool_registry = tool_registry
        self.sandbox = sandbox
        self.approval = approval
        self.agent_loop = agent_loop
        self.ui = ui
        self.undo_tracker = undo_tracker


# ============================================================================
# 组件创建
# ============================================================================

def create_provider(config: Config) -> BaseProvider:
    """根据 config.provider 创建对应的 provider 实例。

    Args:
        config: 顶层 Config 对象。

    Returns:
        DeepSeekProvider 或 AnthropicProvider 实例。
    """
    if config.provider == "anthropic":
        return AnthropicProvider(config.anthropic)
    return DeepSeekProvider(config.deepseek)


def create_session(args: Any) -> SessionState:
    """创建完整会话状态，集成所有组件。

    Args:
        args: 命令行参数 Namespace。

    Returns:
        初始化好的 SessionState 对象。
    """
    # 1. 加载配置
    config = load_config(args)

    # 2. 创建组件
    token_counter = TokenCounter()
    provider = create_provider(config)
    sandbox = Sandbox(
        workspace_root=config.security.workspace_root,
        allowed_dirs=config.security.allowed_dirs,
        blocked_paths=config.security.blocked_paths,
        command_blacklist=config.security.command_blacklist,
        command_whitelist_mode=config.security.command_whitelist_mode,
        command_whitelist=config.security.command_whitelist,
    )
    approval = ApprovalManager(
        require_approval_for=config.security.require_approval_for,
        auto_approve_read=config.security.auto_approve_read,
    )
    compressor = ContextCompressor(
        provider=provider,
        token_counter=token_counter,
        strategy=config.context.compression_strategy,
        save_full_history=config.context.save_full_history,
        history_file=config.context.history_file,
    )
    context_manager = ContextManager(
        config=config.context,
        token_counter=token_counter,
        compressor=compressor,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )
    tool_registry = ToolRegistry.create_default_registry(
        context_manager=context_manager,
        workspace_root=config.security.workspace_root,
        require_approval_for=config.security.require_approval_for,
    )

    # 撤销追踪：包装 write_file 和 edit_file
    undo_tracker = UndoTracker()
    write_tool = tool_registry.get("write_file")
    edit_tool = tool_registry.get("edit_file")
    if write_tool is not None:
        undo_tracker.wrap_tool(write_tool, config.security.workspace_root)
    if edit_tool is not None:
        undo_tracker.wrap_tool(edit_tool, config.security.workspace_root)

    ui = UIDisplay(
        config.ui,
        provider_name=config.provider,
        context_manager=context_manager,
    )

    agent_loop = AgentLoop(
        provider=provider,
        context_manager=context_manager,
        tool_registry=tool_registry,
        sandbox=sandbox,
        approval=approval,
        ui_display=ui,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )

    return SessionState(
        config=config,
        provider=provider,
        context_manager=context_manager,
        tool_registry=tool_registry,
        sandbox=sandbox,
        approval=approval,
        agent_loop=agent_loop,
        ui=ui,
        undo_tracker=undo_tracker,
    )


# ============================================================================
# Slash 命令处理
# ============================================================================

async def handle_slash_command(command: str, session: SessionState) -> bool:
    """处理 slash 命令。

    Args:
        command: 用户输入的 slash 命令（含 /）。
        session: 当前会话状态。

    Returns:
        True 表示应退出 REPL，False 表示继续。
    """
    parts = command.strip().split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    ui = session.ui

    if cmd in ("/quit", "/exit"):
        ui.console.print("[dim]再见！[/dim]")
        return True

    if cmd == "/help":
        ui.show_help()
        return False

    if cmd == "/config":
        ui.show_config(session.config)
        return False

    if cmd == "/stats":
        stats = session.context_manager.get_stats()
        ui.show_stats(stats)
        return False

    if cmd == "/clear":
        await session.context_manager.clear()
        ui.console.print("[green]对话历史已清空[/green]")
        return False

    if cmd == "/compact":
        ui.console.print("[dim]正在压缩上下文...[/dim]")
        try:
            stats = await session.context_manager.force_compress()
            ui.on_compression(stats)
        except Exception as e:
            ui.on_error(f"压缩失败: {e}")
        return False

    if cmd == "/history":
        ui.show_history(session.context_manager.messages)
        return False

    if cmd == "/model":
        if not arg:
            # 显示当前模型
            if session.config.provider == "anthropic":
                current = session.config.anthropic.model
            else:
                current = session.config.deepseek.model
            ui.console.print(f"[cyan]当前模型: {current}[/cyan]")
            ui.console.print("[dim]用法: /model <model_name>[/dim]")
            return False
        # 切换模型
        if session.config.provider == "anthropic":
            session.config.anthropic.model = arg
        else:
            session.config.deepseek.model = arg
        # 重新创建 provider 和 agent_loop
        _recreate_provider_and_loop(session)
        ui.console.print(f"[green]已切换模型到: {arg}[/green]")
        return False

    if cmd == "/provider":
        if not arg:
            ui.console.print(f"[cyan]当前 provider: {session.config.provider}[/cyan]")
            ui.console.print("[dim]用法: /provider <deepseek|anthropic>[/dim]")
            return False
        if arg not in ("deepseek", "anthropic"):
            ui.on_error(f"不支持的 provider: {arg}（可选: deepseek / anthropic）")
            return False
        session.config.provider = arg
        _recreate_provider_and_loop(session)
        # 更新 UI 的 provider_name
        session.ui.provider_name = arg
        ui.console.print(f"[green]已切换 provider 到: {arg}[/green]")
        return False

    if cmd == "/approve":
        # 切换 YOLO 模式
        if session.approval._yolo_mode:
            # 关闭 YOLO：恢复需审批列表
            session.approval._yolo_mode = False
            session.approval._auto_approved.clear()
            # 恢复默认需审批列表
            if not session.config.security.require_approval_for:
                session.config.security.require_approval_for = [
                    "file_write", "file_edit", "shell_exec",
                ]
            ui.console.print("[yellow]YOLO 模式已关闭，恢复审批[/yellow]")
        else:
            session.approval.enable_yolo_mode()
            ui.console.print("[bold red]YOLO 模式已开启，所有操作自动批准[/bold red]")
        return False

    if cmd == "/undo":
        success, message = session.undo_tracker.undo()
        if success:
            ui.console.print(f"[green]{message}[/green]")
        else:
            ui.console.print(f"[yellow]{message}[/yellow]")
        return False

    # 未知命令
    ui.on_error(f"未知命令: {cmd}（输入 /help 查看可用命令）")
    return False


def _recreate_provider_and_loop(session: SessionState) -> None:
    """重新创建 provider 和 agent_loop（/model 和 /provider 命令使用）。

    更新 session 中的 provider、agent_loop 引用。
    context_manager 和 tool_registry 保持不变（它们不依赖 provider 实例）。
    """
    new_provider = create_provider(session.config)
    session.provider = new_provider
    # 更新 compressor 的 provider 引用（用于 summary 策略）
    if session.context_manager.compressor is not None:
        session.context_manager.compressor.provider = new_provider
    # 重新创建 agent_loop
    session.agent_loop = AgentLoop(
        provider=new_provider,
        context_manager=session.context_manager,
        tool_registry=session.tool_registry,
        sandbox=session.sandbox,
        approval=session.approval,
        ui_display=session.ui,
        system_prompt=DEFAULT_SYSTEM_PROMPT,
    )


# ============================================================================
# REPL 主循环
# ============================================================================

async def run_repl(session: SessionState) -> None:
    """运行交互式 REPL 主循环。

    使用 prompt_toolkit 的 PromptSession 异步读取输入，支持历史记录。
    捕获 KeyboardInterrupt（Ctrl+C）中断当前 agent_loop，回到提示符；
    EOFError（Ctrl+D）退出。
    """
    # 初始化带历史记录的 PromptSession
    try:
        session_obj: PromptSession = PromptSession(
            history=FileHistory(_HISTORY_FILE)
        )
    except OSError:
        # 历史文件目录不可写时回退到无历史模式
        session_obj = PromptSession()

    ui = session.ui

    while True:
        try:
            user_input = await session_obj.prompt_async(_PROMPT_TEXT)
        except KeyboardInterrupt:
            # Ctrl+C：中断当前操作，回到提示符
            ui.console.print("[dim]^C[/dim]")
            continue
        except EOFError:
            # Ctrl+D：退出
            ui.console.print("[dim]再见！[/dim]")
            break

        # 空输入跳过
        if not user_input.strip():
            continue

        # slash 命令处理
        if user_input.startswith("/"):
            try:
                should_exit = await handle_slash_command(user_input, session)
            except Exception as e:
                ui.on_error(f"命令执行错误: {e}")
                continue
            if should_exit:
                break
            continue

        # Agent 循环
        try:
            await session.agent_loop.run(user_input)
        except KeyboardInterrupt:
            # Ctrl+C 中断 agent 循环
            session.agent_loop.cancel()
            ui.on_error("操作已中断")
        except Exception as e:
            ui.on_error(f"错误: {e}")
        finally:
            # 确保 Live 流式显示已结束
            ui.finalize()


async def run_single(prompt: str, session: SessionState) -> None:
    """单次执行模式：处理完 prompt 后退出。

    Args:
        prompt: 用户提示词。
        session: 会话状态。
    """
    ui = session.ui
    try:
        await session.agent_loop.run(prompt)
    except KeyboardInterrupt:
        session.agent_loop.cancel()
        ui.on_error("操作已中断")
    except Exception as e:
        ui.on_error(f"错误: {e}")
    finally:
        ui.finalize()


# ============================================================================
# 命令行参数解析
# ============================================================================

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。

    支持交互模式（无 prompt）与单次执行模式（提供 prompt）。
    """
    parser = argparse.ArgumentParser(
        prog="codepilot",
        description="CodePilot - AI 编码智能体 CLI 工具",
    )
    # 位置参数：可选的单次执行 prompt
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="单次执行模式的提示词（不提供则进入交互模式）",
    )
    parser.add_argument(
        "--provider",
        choices=["deepseek", "anthropic"],
        default=None,
        help="指定 LLM Provider（deepseek 或 anthropic）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="指定模型名称",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="直接传入 API Key",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="指定工作区根目录",
    )
    parser.add_argument(
        "--no-approve",
        action="store_true",
        default=False,
        help="禁用人工审批（YOLO 模式，自动批准所有操作）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="指定配置文件路径",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="启用详细日志输出",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"CodePilot v{VERSION}",
        help="显示版本号并退出",
    )
    return parser.parse_args(argv)


# ============================================================================
# 主入口
# ============================================================================

async def main_async(args: argparse.Namespace) -> int:
    """异步主入口：创建会话、显示 banner、进入 REPL 或单次模式。"""
    console = Console()

    # 创建会话（集成所有组件）
    try:
        session = create_session(args)
    except Exception as e:
        console.print(f"[bold red]初始化失败: {e}[/bold red]")
        return 1

    # 显示 banner
    show_banner(session.config, console)

    # 根据是否提供 prompt 决定模式
    if args.prompt:
        await run_single(args.prompt, session)
    else:
        await run_repl(session)

    return 0


def main(argv: list[str] | None = None) -> int:
    """主入口函数。

    解析参数、创建会话、显示 banner，根据是否有 prompt 参数决定
    进入单次执行模式或交互 REPL 模式。
    """
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
