"""审批管理器 ApprovalManager。

对 file_write/file_edit/shell_exec 等操作请求用户审批，满足
tools/registry.py 中的 ApprovalProtocol 协议。支持四种选择：
y（本次批准）、n（拒绝）、a（本会话自动批准同类）、!（YOLO 模式）。

使用 rich.panel.Panel 渲染审批面板：file 操作显示 diff 预览，
shell_exec 高亮显示完整命令。
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


# 操作类型中文映射，用于面板显示
_OPERATION_LABELS: dict[str, str] = {
    "file_write": "文件写入",
    "file_edit": "文件编辑",
    "shell_exec": "Shell 执行",
}


class ApprovalManager:
    """用户审批管理器，满足 ApprovalProtocol 协议。"""

    def __init__(
        self,
        require_approval_for: list[str],
        auto_approve_read: bool = True,
    ):
        # 需要审批的操作类型集合
        self.require_approval_for: set[str] = set(require_approval_for or [])
        self.auto_approve_read: bool = bool(auto_approve_read)
        # 本会话自动批准的操作类型
        self._auto_approved: set[str] = set()
        # YOLO 模式：开启后所有操作自动批准
        self._yolo_mode: bool = False
        # rich console 实例（复用）
        self._console: Console = Console()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def request_approval(self, operation: str, details: dict) -> bool:
        """请求用户审批操作。

        Args:
            operation: 操作类型（file_write/file_edit/shell_exec）。
            details: 操作详情（path/content/diff/command 等）。

        Returns:
            True 表示批准，False 表示拒绝。
        """
        # 1. YOLO 模式：全部放行
        if self._yolo_mode:
            return True

        # 2. 不在需审批列表中：放行
        if operation not in self.require_approval_for:
            return True

        # 3. 本会话已自动批准同类操作：放行
        if operation in self._auto_approved:
            return True

        # 4. 显示面板并读取用户选择
        return self._prompt_user(operation, details)

    def enable_yolo_mode(self) -> None:
        """开启 YOLO 模式，后续所有操作自动批准。"""
        self._yolo_mode = True

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _prompt_user(self, operation: str, details: dict) -> bool:
        """显示审批面板并循环读取用户输入，直到获得有效选择。

        捕获 KeyboardInterrupt/EOFError 返回 False。
        """
        # 渲染审批面板
        self._render_panel(operation, details)

        # 循环读取输入
        while True:
            try:
                choice = input(
                    "批准? [Y]es / [N]o / [A]lways / [!]YOLO > "
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                # Ctrl+C / Ctrl+D 视为拒绝
                self._console.print("[yellow]已拒绝（中断）[/yellow]")
                return False

            if choice in ("y", "yes"):
                return True
            if choice in ("n", "no"):
                return False
            if choice == "a":
                self._auto_approved.add(operation)
                self._console.print(
                    f"[green]本会话后续 '{operation}' 操作将自动批准[/green]"
                )
                return True
            if choice == "!":
                self._yolo_mode = True
                self._console.print(
                    "[bold red]YOLO 模式已开启，后续所有操作自动批准[/bold red]"
                )
                return True
            # 无效输入，重新提示
            self._console.print(
                "[yellow]无效选择，请输入 y/n/a/! [/yellow]"
            )

    def _render_panel(self, operation: str, details: dict) -> None:
        """渲染审批面板。

        file_write/file_edit 显示路径与 diff 预览；
        shell_exec 高亮显示完整命令。
        """
        op_label = _OPERATION_LABELS.get(operation, operation)

        # 构建面板内容
        lines: list[Text] = []
        lines.append(Text(f"操作: {op_label} ({operation})", style="bold cyan"))

        if operation in ("file_write", "file_edit"):
            # 文件操作：显示路径与 diff
            path = details.get("path", "")
            if path:
                lines.append(Text(f"路径: {path}", style="cyan"))

            diff = details.get("diff")
            if diff:
                # diff 作为嵌套面板显示
                diff_text = Text(diff.rstrip("\n"))
                # 简单着色：+ 行绿色，- 行红色，其余默认
                colored = Text()
                for line in diff_text.split("\n"):
                    if line.startswith("+"):
                        colored.append(line + "\n", style="green")
                    elif line.startswith("-"):
                        colored.append(line + "\n", style="red")
                    elif line.startswith("@@"):
                        colored.append(line + "\n", style="cyan")
                    else:
                        colored.append(line + "\n")
                diff_panel = Panel(
                    colored,
                    title="预览",
                    border_style="blue",
                    padding=(0, 1),
                )
                self._console.print(
                    Panel(
                        self._compose_content(lines),
                        title="审批请求",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                self._console.print(diff_panel)
                return
            # 无 diff 时显示 content（file_write 新文件场景）
            content = details.get("content")
            if content:
                preview = content if len(content) <= 2000 else content[:2000] + "\n...[truncated]"
                content_panel = Panel(
                    Text(preview),
                    title="内容预览",
                    border_style="blue",
                    padding=(0, 1),
                )
                self._console.print(
                    Panel(
                        self._compose_content(lines),
                        title="审批请求",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                self._console.print(content_panel)
                return

        elif operation == "shell_exec":
            # shell_exec：高亮显示完整命令
            command = details.get("command", "")
            if command:
                lines.append(Text("命令:", style="cyan"))
                self._console.print(
                    Panel(
                        self._compose_content(lines),
                        title="审批请求",
                        border_style="yellow",
                        padding=(0, 1),
                    )
                )
                # 用 Syntax 高亮 shell 命令
                syntax = Syntax(command, "bash", theme="monokai", word_wrap=True)
                cmd_panel = Panel(
                    syntax,
                    title="命令",
                    border_style="red",
                    padding=(0, 1),
                )
                self._console.print(cmd_panel)
                return

        # 兜底：仅显示操作信息
        self._console.print(
            Panel(
                self._compose_content(lines),
                title="审批请求",
                border_style="yellow",
                padding=(0, 1),
            )
        )

    @staticmethod
    def _compose_content(lines: list[Text]) -> Text:
        """将多行 Text 合并为单个 Text（用换行连接）。"""
        content = Text()
        for i, line in enumerate(lines):
            content.append(line)
            if i < len(lines) - 1:
                content.append("\n")
        return content


__all__ = ["ApprovalManager"]
