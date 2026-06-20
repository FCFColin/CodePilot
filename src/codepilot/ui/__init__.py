"""UI 显示子包。

包含启动 banner、Rich Console 显示管理器、diff 着色显示。
注意：ui/ 是 src/ 中唯一允许通过 rich Console 输出的子包。
"""

from __future__ import annotations

from codepilot.ui.banner import show_banner
from codepilot.ui.diff_view import render_diff, render_new_file
from codepilot.ui.display import DisplayManager

__all__ = [
    "DisplayManager",
    "show_banner",
    "render_diff",
    "render_new_file",
]
