"""应用组合根 - 依赖注入。

组装所有组件：provider / sandbox / approval / compressor /
context_manager / tool_registry / display / agent_loop。
实现 REPL 主循环与 slash 命令处理。

注意：本模块不直接 print()，所有输出通过 DisplayManager（ui/ 子包）完成。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory

from codepilot.agent.loop import DEFAULT_SYSTEM_PROMPT, AgentLoop
from codepilot.config import Config
from codepilot.context.compressor import CompressionStrategy, ContextCompressor
from codepilot.context.manager import ContextManager
from codepilot.context.token_counter import TokenCounter
from codepilot.exceptions import CodePilotError
from codepilot.git import CommitMessageGenerator, GitManager
from codepilot.hooks import GitCommitHook, HookRegistry, LintHook
from codepilot.providers.anthropic import AnthropicProvider
from codepilot.providers.base import BaseProvider
from codepilot.providers.deepseek import DeepSeekProvider
from codepilot.repomap import RepoMapper
from codepilot.security.approval import ApprovalManager
from codepilot.security.sandbox import Sandbox
from codepilot.session import SessionExporter, SessionManager, SessionStorage
from codepilot.tools.registry import (
    ApprovalProtocol,
    BaseTool,
    SandboxProtocol,
    ToolRegistry,
)
from codepilot.ui.banner import show_banner
from codepilot.ui.display import DisplayManager

# REPL 输入提示符
_PROMPT_TEXT = "You › "

# 输入历史文件路径（用户目录下）
_HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".codepilot_history")

# 默认需审批的操作类型（/approve 关闭 YOLO 时恢复）
_DEFAULT_APPROVAL_OPS: list[str] = ["file_write", "file_edit", "shell_exec"]


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

    def _read_file(self, abs_path: str) -> str | None:
        """读取文件内容，不存在返回 None。

        Args:
            abs_path: 文件绝对路径。

        Returns:
            文件内容，不存在时返回 None。
        """
        if not os.path.isfile(abs_path):
            return None
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None


# ============================================================================
# 工具包装器（装饰器模式）
# ============================================================================


class TrackedToolWrapper(BaseTool):
    """工具包装器，为文件操作工具添加撤销追踪与 Git 自动提交功能。

    通过装饰器模式包装原有工具，在执行前记录文件原内容，
    支持撤销操作，而不修改原始工具类的实现。
    执行成功后若启用 Git 自动提交，则调用 GitManager.auto_commit。
    """

    def __init__(
        self,
        tool: BaseTool,
        undo_tracker: UndoTracker,
        workspace_root: str,
        git_manager: GitManager | None = None,
        auto_commit_enabled: bool = False,
    ) -> None:
        """初始化工具包装器。

        Args:
            tool: 被包装的原始工具（WriteFileTool 或 EditFileTool）。
            undo_tracker: 撤销追踪器实例。
            workspace_root: 工作区根目录，用于解析相对路径。
            git_manager: 可选的 GitManager 实例，用于自动提交。
            auto_commit_enabled: 是否启用 Git 自动提交。
        """
        self._tool = tool
        self._undo_tracker = undo_tracker
        self._workspace_root = workspace_root
        self._git_manager = git_manager
        self._auto_commit_enabled = auto_commit_enabled
        self._commit_generator = CommitMessageGenerator()
        # 覆盖类属性，使 name/description 与原始工具一致
        self.name = tool.name
        self.description = tool.description

    def get_parameters(self) -> dict[str, Any]:
        """返回工具参数定义，直接委托给原始工具。"""
        return self._tool.get_parameters()

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行工具操作，在执行前记录文件原内容，成功后自动提交。

        Args:
            arguments: 工具参数。
            sandbox: 可选沙箱校验器。
            approval: 可选审批器。

        Returns:
            工具执行结果。
        """
        path = arguments.get("path", "")
        abs_path = ""
        is_new_file = False
        if path:
            # 解析绝对路径
            if os.path.isabs(path):
                abs_path = os.path.realpath(path)
            else:
                abs_path = os.path.realpath(os.path.join(self._workspace_root, path))
            # 记录原内容到撤销追踪器
            old_content = self._undo_tracker._read_file(abs_path)
            is_new_file = old_content is None
            self._undo_tracker._stack.append((abs_path, old_content))

        # 委托给原始工具执行
        result = await self._tool.execute(arguments, sandbox, approval)

        # 执行成功后自动提交到 Git
        if (
            path
            and self._auto_commit_enabled
            and self._git_manager is not None
            and self._git_manager.is_git_repo()
            and not result.startswith("Error")
        ):
            # 使用规则生成提交信息
            action = "add" if is_new_file else "update"
            diff_summary = f"{action} {path}"
            commit_message = self._commit_generator.generate(diff_summary)
            self._git_manager.auto_commit(commit_message, [Path(abs_path)])

        return result

    def to_openai_format(self) -> Any:
        """转换为 OpenAI 格式，委托给原始工具。"""
        return self._tool.to_openai_format()

    def to_anthropic_format(self) -> Any:
        """转换为 Anthropic 格式，委托给原始工具。"""
        return self._tool.to_anthropic_format()


# ============================================================================
# 应用容器
# ============================================================================


class App:
    """应用容器，组装所有组件并提供 REPL/单次执行入口。"""

    def __init__(self, config: Config) -> None:
        self.config = config

        # 1. 创建 Provider
        self.provider: BaseProvider = self._create_provider(config)

        # 2. 创建 TokenCounter
        self.token_counter = TokenCounter()

        # 3. 创建 Sandbox（内部含 CommandFilter）
        self.sandbox = Sandbox(
            workspace_root=config.security.workspace_root,
            allowed_dirs=config.security.allowed_dirs,
            blocked_paths=config.security.blocked_paths,
            command_blacklist=config.security.command_blacklist,
            command_whitelist_mode=config.security.command_whitelist_mode,
            command_whitelist=config.security.command_whitelist,
        )

        # 4. 创建 ApprovalManager
        self.approval = ApprovalManager(
            require_approval_for=config.security.require_approval_for,
            auto_approve_read=config.security.auto_approve_read,
        )

        # 5. 创建 ContextCompressor
        self.compressor = ContextCompressor(
            provider=self.provider,
            token_counter=self.token_counter,
            strategy=cast(CompressionStrategy, config.context.compression_strategy),
            save_full_history=config.context.save_full_history,
            history_file=config.context.history_file,
        )

        # 6. 创建 ContextManager
        self.context_manager = ContextManager(
            config=config.context,
            token_counter=self.token_counter,
            compressor=self.compressor,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
        )

        # 7. 创建撤销追踪器
        self.undo_tracker = UndoTracker()

        # 7.5 创建 GitManager（用于自动提交与撤销）
        self.git_manager = GitManager(Path(config.security.workspace_root))

        # 8. 创建工具注册表并包装需要追踪的工具
        self.tool_registry = self._create_tool_registry(config)

        # 9. 创建 DisplayManager（实现 UICallback Protocol）
        self.display = DisplayManager(
            config=config.ui,
            provider_name=config.provider,
            context_manager=self.context_manager,
        )

        # 10. 创建 SessionManager（会话持久化）
        self.session_storage = SessionStorage()
        self.session_manager = SessionManager(
            storage=self.session_storage,
            provider=config.provider,
            model=(
                config.anthropic.model
                if config.provider == "anthropic"
                else config.deepseek.model
            ),
            workspace_root=Path(config.security.workspace_root),
        )
        self.session_exporter = SessionExporter()
        # 开始新会话
        self.session_manager.start_session()

        # 11. 创建 HookRegistry（Lint 反馈循环与自动 Git 提交）
        self.hook_registry = self._create_hook_registry(config)

        # 11.5 创建 RepoMapper（可选；tree-sitter 不可用时为 None）
        self.repo_mapper = self._create_repo_mapper(config)

        # 12. 创建 AgentLoop
        self.agent_loop = AgentLoop(
            provider=self.provider,
            context_manager=self.context_manager,
            tool_registry=self.tool_registry,
            sandbox=self.sandbox,
            approval=self.approval,
            ui_callback=self.display,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            session_manager=self.session_manager,
            hook_registry=self.hook_registry,
            max_lint_retries=config.hooks.max_lint_retries,
            repo_mapper=self.repo_mapper,
        )

    @staticmethod
    def _create_provider(config: Config) -> BaseProvider:
        """根据 config.provider 创建对应的 provider 实例。"""
        if config.provider == "anthropic":
            return AnthropicProvider(config.anthropic)
        return DeepSeekProvider(config.deepseek)

    def _create_tool_registry(self, config: Config) -> ToolRegistry:
        """创建工具注册表，对文件操作工具添加撤销追踪与 Git 自动提交。

        Args:
            config: 配置对象。

        Returns:
            配置好的工具注册表。
        """
        # 创建基础工具注册表
        registry = ToolRegistry.create_default_registry(
            context_manager=self.context_manager,
            workspace_root=config.security.workspace_root,
            require_approval_for=config.security.require_approval_for,
        )

        # 包装需要撤销追踪的工具
        workspace_root = config.security.workspace_root
        auto_commit_enabled = config.git.auto_commit
        for tool_name in ("write_file", "edit_file"):
            original_tool = registry.get(tool_name)
            if original_tool is not None:
                wrapped_tool = TrackedToolWrapper(
                    original_tool,
                    self.undo_tracker,
                    workspace_root,
                    git_manager=self.git_manager,
                    auto_commit_enabled=auto_commit_enabled,
                )
                registry.register(wrapped_tool)

        return registry

    def _create_hook_registry(self, config: Config) -> HookRegistry:
        """创建 HookRegistry，根据 config.hooks 注册内置钩子。

        - auto_lint 为 True：注册 LintHook
        - auto_git_commit 为 True 且 git.auto_commit 为 True：注册 GitCommitHook

        Args:
            config: 配置对象。

        Returns:
            配置好的 HookRegistry 实例。
        """
        registry = HookRegistry()
        if config.hooks.auto_lint:
            registry.register(LintHook())
        if config.hooks.auto_git_commit and config.git.auto_commit:
            registry.register(GitCommitHook(self.git_manager))
        return registry

    def _create_repo_mapper(self, config: Config) -> RepoMapper | None:
        """创建 RepoMapper（可选功能）。

        tree-sitter 不可用或 repomap.enabled 为 False 时返回 None。

        Args:
            config: 配置对象。

        Returns:
            RepoMapper 实例；不可用时返回 None。
        """
        if not config.repomap.enabled:
            return None
        try:
            mapper = RepoMapper(
                workspace_root=Path(config.security.workspace_root),
                max_tokens=config.repomap.max_tokens,
            )
            if not mapper.is_available():
                return None
            return mapper
        except Exception:
            return None

    def _recreate_provider_and_loop(self) -> None:
        """重新创建 provider 和 agent_loop（/model 和 /provider 命令使用）。

        更新 self.provider、self.agent_loop 引用，以及 compressor 的 provider。
        context_manager 和 tool_registry 保持不变。
        """
        new_provider = self._create_provider(self.config)
        self.provider = new_provider
        # 更新 compressor 的 provider 引用（用于 summary 策略）
        if self.context_manager.compressor is not None:
            self.context_manager.compressor.provider = new_provider
        # 重新创建 agent_loop
        self.agent_loop = AgentLoop(
            provider=new_provider,
            context_manager=self.context_manager,
            tool_registry=self.tool_registry,
            sandbox=self.sandbox,
            approval=self.approval,
            ui_callback=self.display,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            session_manager=self.session_manager,
            hook_registry=self.hook_registry,
            max_lint_retries=self.config.hooks.max_lint_retries,
            repo_mapper=self.repo_mapper,
        )

    # ------------------------------------------------------------------
    # REPL 与单次执行
    # ------------------------------------------------------------------

    async def run_repl(self) -> None:
        """运行交互式 REPL 主循环。

        TTY 模式使用 prompt_toolkit 的 PromptSession 异步读取输入，支持历史记录；
        非 TTY 模式（管道/重定向）回退到 sys.stdin.readline，避免 prompt_toolkit
        在非交互式 stdin 上挂起。
        捕获 KeyboardInterrupt（Ctrl+C）中断当前 agent_loop，回到提示符；
        EOFError（Ctrl+D）退出。
        """
        # 显示启动 banner
        show_banner(self.config, self.display.console)

        # 检测 stdin 是否为 TTY
        is_tty = sys.stdin.isatty()

        # TTY 模式下初始化带历史记录的 PromptSession
        session: PromptSession[str] | None = None
        if is_tty:
            try:
                session = PromptSession(history=FileHistory(_HISTORY_FILE))
            except OSError:
                # 历史文件目录不可写时回退到无历史模式
                session = PromptSession()

        while True:
            # 读取用户输入
            if session is not None:
                # TTY 模式：使用 prompt_toolkit
                try:
                    user_input = await session.prompt_async(_PROMPT_TEXT)
                except KeyboardInterrupt:
                    # Ctrl+C：中断当前操作，回到提示符
                    self.display.console.print("[dim]^C[/dim]")
                    continue
                except EOFError:
                    # Ctrl+D：退出
                    self.display.console.print("[dim]再见！[/dim]")
                    break
            else:
                # 非 TTY 模式：使用 readline（在线程中执行避免阻塞事件循环）
                try:
                    line = await asyncio.to_thread(sys.stdin.readline)
                except (KeyboardInterrupt, OSError):
                    self.display.console.print("[dim]再见！[/dim]")
                    break
                # readline 返回空串表示 EOF
                if not line:
                    self.display.console.print("[dim]再见！[/dim]")
                    break
                # 去除行尾换行符
                user_input = line.rstrip("\r\n")

            # 空输入跳过
            if not user_input.strip():
                continue

            # slash 命令处理
            if user_input.startswith("/"):
                try:
                    should_exit = await self._handle_slash_command(user_input)
                except CodePilotError as e:
                    self.display.console.print(f"[bold red]命令错误: {e}[/bold red]")
                    continue
                if should_exit:
                    break
                continue

            # Agent 循环
            try:
                await self.agent_loop.run(user_input)
            except KeyboardInterrupt:
                # Ctrl+C 中断 agent 循环
                self.agent_loop.cancel()
                await self.display.on_error("操作已中断")
            except CodePilotError as e:
                await self.display.on_error(f"错误: {e}")

    async def run_single(self, prompt: str) -> None:
        """单次执行模式：处理完 prompt 后退出。

        Args:
            prompt: 用户提示词。
        """
        try:
            await self.agent_loop.run(prompt)
        except KeyboardInterrupt:
            self.agent_loop.cancel()
            await self.display.on_error("操作已中断")
        except CodePilotError as e:
            await self.display.on_error(f"错误: {e}")

    # ------------------------------------------------------------------
    # Slash 命令处理
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, command: str) -> bool:
        """处理 slash 命令。

        Args:
            command: 用户输入的 slash 命令（含 /）。

        Returns:
            True 表示应退出 REPL，False 表示继续。
        """
        parts = command.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        display = self.display

        if cmd in ("/quit", "/exit"):
            display.console.print("[dim]再见！[/dim]")
            return True

        if cmd == "/help":
            display.show_help()
            return False

        if cmd == "/config":
            display.show_config(self.config)
            return False

        if cmd == "/stats":
            stats = self.context_manager.get_stats()
            display.show_stats(stats)
            return False

        if cmd == "/clear":
            await self.context_manager.clear()
            display.console.print("[green]对话历史已清空[/green]")
            return False

        if cmd == "/compact":
            display.console.print("[dim]正在压缩上下文...[/dim]")
            try:
                comp_stats = await self.context_manager.force_compress()
                display.on_compression(dict(comp_stats))
            except CodePilotError as e:
                await display.on_error(f"压缩失败: {e}")
            return False

        if cmd == "/history":
            display.show_history(self.context_manager.messages)
            return False

        if cmd == "/model":
            return self._handle_model_command(arg)

        if cmd == "/provider":
            return self._handle_provider_command(arg)

        if cmd == "/approve":
            return self._handle_approve_command()

        if cmd == "/undo":
            # 优先尝试 Git 撤销最近一次 codepilot 提交
            if self.git_manager.is_git_repo():
                git_success, git_message = self.git_manager.undo_last_commit()
                if git_success:
                    display.console.print(
                        f"[green]已撤销 Git 提交: {git_message}[/green]"
                    )
                    return False
                # Git 撤销失败（非 codepilot 提交或无提交），回退到内存撤销
            # 回退到内存 UndoTracker.undo()
            success, message = self.undo_tracker.undo()
            if success:
                display.console.print(f"[green]{message}[/green]")
            else:
                display.console.print(f"[yellow]{message}[/yellow]")
            return False

        if cmd == "/sessions":
            sessions = self.session_storage.list_sessions(limit=10)
            display.show_sessions(cast(list[dict[str, Any]], sessions))
            return False

        if cmd == "/export":
            return self._handle_export_command(arg)

        # 未知命令
        await display.on_error(f"未知命令: {cmd}（输入 /help 查看可用命令）")
        return False

    def _handle_model_command(self, arg: str) -> bool:
        """处理 /model 命令。"""
        display = self.display
        if not arg:
            # 显示当前模型
            if self.config.provider == "anthropic":
                current = self.config.anthropic.model
            else:
                current = self.config.deepseek.model
            display.console.print(f"[cyan]当前模型: {current}[/cyan]")
            display.console.print("[dim]用法: /model <model_name>[/dim]")
            return False
        # 切换模型
        if self.config.provider == "anthropic":
            self.config.anthropic.model = arg
        else:
            self.config.deepseek.model = arg
        # 重新创建 provider 和 agent_loop
        self._recreate_provider_and_loop()
        display.console.print(f"[green]已切换模型到: {arg}[/green]")
        return False

    def _handle_provider_command(self, arg: str) -> bool:
        """处理 /provider 命令。"""
        display = self.display
        if not arg:
            display.console.print(f"[cyan]当前 provider: {self.config.provider}[/cyan]")
            display.console.print("[dim]用法: /provider <deepseek|anthropic>[/dim]")
            return False
        if arg not in ("deepseek", "anthropic"):
            display.console.print(
                f"[bold red]不支持的 provider: {arg}"
                f"（可选: deepseek / anthropic）[/bold red]"
            )
            return False
        self.config.provider = arg
        self._recreate_provider_and_loop()
        # 更新 DisplayManager 的 provider_name
        self.display.provider_name = arg
        display.console.print(f"[green]已切换 provider 到: {arg}[/green]")
        return False

    def _handle_approve_command(self) -> bool:
        """处理 /approve 命令，切换 YOLO 模式。"""
        display = self.display
        if self.approval._yolo_mode:
            # 关闭 YOLO：恢复需审批列表
            self.approval._yolo_mode = False
            self.approval._auto_approved.clear()
            # 恢复默认需审批列表（若为空）
            if not self.approval.require_approval_for:
                self.approval.require_approval_for = set(_DEFAULT_APPROVAL_OPS)
            display.console.print("[yellow]YOLO 模式已关闭，恢复审批[/yellow]")
        else:
            self.approval.enable_yolo_mode()
            display.console.print(
                "[bold red]YOLO 模式已开启，所有操作自动批准[/bold red]"
            )
        return False

    def _handle_export_command(self, arg: str) -> bool:
        """处理 /export 命令，导出当前会话到文件。

        Args:
            arg: 导出格式（markdown 或 json），默认 markdown。
        """
        display = self.display
        fmt = arg.strip().lower() if arg.strip() else "markdown"
        if fmt not in ("markdown", "json"):
            display.console.print(
                f"[bold red]不支持的格式: {fmt}（可选: markdown / json）[/bold red]"
            )
            return False

        try:
            record = self.session_manager.get_record()
        except Exception as e:
            display.console.print(f"[bold red]获取会话记录失败: {e}[/bold red]")
            return False

        ext = "md" if fmt == "markdown" else "json"
        session_id = record.get("session_id", "unknown")
        file_name = f"codepilot-session-{session_id}.{ext}"
        file_path = Path.cwd() / file_name

        try:
            if fmt == "markdown":
                content = self.session_exporter.to_markdown(record)
            else:
                content = self.session_exporter.to_json(record)
            file_path.write_text(content, encoding="utf-8")
            display.console.print(f"[green]已导出会话到: {file_path}[/green]")
        except OSError as e:
            display.console.print(f"[bold red]导出失败: {e}[/bold red]")
        return False

    async def resume_from_history(self, session_id: str | None = None) -> bool:
        """加载历史会话消息注入 context_manager，实现断点续跑。

        Args:
            session_id: 指定会话 ID。为 None 时加载最近一个会话。

        Returns:
            True 表示成功加载历史，False 表示无可用历史。
        """
        if session_id is None:
            latest = self.session_storage.get_latest()
            if latest is None:
                self.display.console.print("[yellow]没有可恢复的历史会话[/yellow]")
                return False
            session_id = latest["session_id"]
        else:
            try:
                latest = self.session_storage.load(session_id)
            except Exception as e:
                self.display.console.print(f"[bold red]加载会话失败: {e}[/bold red]")
                return False

        messages = latest.get("messages", [])
        if not messages:
            self.display.console.print(f"[yellow]会话 {session_id} 无历史消息[/yellow]")
            return False

        # 注入历史消息到 context_manager
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            await self.context_manager.add_message(role, content)

        self.display.console.print(
            f"[green]已恢复会话 {session_id}（{len(messages)} 条消息）[/green]"
        )
        return True


# ============================================================================
# 工厂函数
# ============================================================================


def create_app(config: Config) -> App:
    """工厂函数，根据 config 创建 App。

    Args:
        config: 已加载并验证的 Config 对象。

    Returns:
        初始化好的 App 实例。
    """
    return App(config)


__all__ = ["App", "create_app", "UndoTracker", "TrackedToolWrapper"]
