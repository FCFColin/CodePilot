"""Diff 着色显示模块。

使用 difflib 生成 unified diff，并用 rich 渲染着色面板：
- 新增行：绿色 + 前缀
- 删除行：红色 - 前缀
- 上下文行：默认色 空格前缀
- hunk 头：青色 @@ 前缀
- 文件头（---/+++）：黄色
"""

from __future__ import annotations

import difflib

from rich.panel import Panel
from rich.text import Text


def _colorize_diff_line(line: str) -> Text:
    """为单行 diff 文本着色。

    根据行首前缀选择颜色：
    - '+' 开头（非 '+++'）：绿色
    - '-' 开头（非 '---'）：红色
    - '@@' 开头：青色（hunk 头）
    - '+++' 或 '---' 开头：黄色（文件头）
    - 其他：默认色
    """
    if line.startswith("+++") or line.startswith("---"):
        return Text(line, style="yellow")
    if line.startswith("@@"):
        return Text(line, style="cyan")
    if line.startswith("+"):
        return Text(line, style="green")
    if line.startswith("-"):
        return Text(line, style="red")
    return Text(line)


def _build_diff_text(diff_lines: list[str], max_lines: int) -> Text:
    """将 diff 行列表构建为着色的 Text 对象，截断到 max_lines 行。"""
    content = Text()
    truncated = len(diff_lines) > max_lines
    visible_lines = diff_lines[:max_lines]

    for i, line in enumerate(visible_lines):
        content.append(_colorize_diff_line(line))
        if i < len(visible_lines) - 1:
            content.append("\n")

    if truncated:
        omitted = len(diff_lines) - max_lines
        content.append("\n")
        content.append(
            f"... [{omitted} more lines omitted]",
            style="dim italic",
        )

    return content


def render_diff(old_text: str, new_text: str, max_lines: int = 50) -> Panel:
    """生成 unified diff 并用 rich 渲染着色面板。

    Args:
        old_text: 原始文本。
        new_text: 新文本。
        max_lines: 最多显示的 diff 行数（超出截断）。

    Returns:
        rich.panel.Panel 包裹的着色 diff。
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        lineterm="",
    )
    # difflib 在 keepends=True 时，行尾已含换行符；lineterm="" 避免重复添加
    # 但 splitlines(keepends=True) 保留的换行会导致 diff 行带尾部换行
    # 这里统一去除行尾换行，由 _build_diff_text 自行添加
    diff_lines = [line.rstrip("\n") for line in diff]

    if not diff_lines:
        content = Text("(无差异)", style="dim italic")
    else:
        content = _build_diff_text(diff_lines, max_lines)

    return Panel(
        content,
        title="Diff",
        border_style="blue",
        padding=(0, 1),
    )


def render_new_file(content: str, max_lines: int = 50) -> Panel:
    """渲染新文件内容（全部为新增行，绿色 + 前缀）。

    Args:
        content: 新文件内容。
        max_lines: 最多显示的行数（超出截断）。

    Returns:
        rich.panel.Panel 包裹的着色内容。
    """
    lines = content.splitlines()
    truncated = len(lines) > max_lines
    visible_lines = lines[:max_lines]

    text = Text()
    for i, line in enumerate(visible_lines):
        text.append(Text(f"+ {line}", style="green"))
        if i < len(visible_lines) - 1:
            text.append("\n")

    if truncated:
        omitted = len(lines) - max_lines
        text.append("\n")
        text.append(
            f"... [{omitted} more lines omitted]",
            style="dim italic",
        )

    return Panel(
        text,
        title="New File",
        border_style="green",
        padding=(0, 1),
    )


__all__ = ["render_diff", "render_new_file"]
