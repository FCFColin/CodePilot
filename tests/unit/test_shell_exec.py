"""Shell 执行与文件原子写入增强测试。

覆盖原子写入、ANSI 转义过滤、输出大小限制等功能。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codepilot.tools.file_write import WriteFileTool
from codepilot.tools.shell_exec import ShellExecTool, _MAX_OUTPUT_BYTES


# ============================================================================
# 原子写入测试
# ============================================================================


class TestAtomicWrite:
    """原子写入测试：新文件、覆写、失败清理。"""

    async def test_atomic_write_new_file(self, tmp_path: Path) -> None:
        """新文件写入成功。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "new_file.txt", "content": "hello"})
        assert "File written" in result
        assert (tmp_path / "new_file.txt").read_text(encoding="utf-8") == "hello"

    async def test_atomic_write_existing_file(self, tmp_path: Path) -> None:
        """覆写已有文件成功。"""
        file_path = tmp_path / "existing.txt"
        file_path.write_text("old content", encoding="utf-8")
        tool = WriteFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "existing.txt", "content": "new content"})
        assert "File written" in result
        assert file_path.read_text(encoding="utf-8") == "new content"

    async def test_atomic_write_failure_cleanup(self, tmp_path: Path) -> None:
        """写入失败时不留下半写文件，原文件保持不变。"""
        file_path = tmp_path / "target.txt"
        file_path.write_text("original", encoding="utf-8")

        tool = WriteFileTool(workspace_root=str(tmp_path))

        # 模拟 os.replace 失败，验证临时文件被清理
        with patch("os.replace", side_effect=OSError("mock replace failure")):
            result = await tool.execute({"path": "target.txt", "content": "broken"})

        assert "Error" in result
        # 原文件内容应保持不变
        assert file_path.read_text(encoding="utf-8") == "original"
        # 不应有 .tmp 残留文件
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


# ============================================================================
# ANSI 转义序列过滤测试
# ============================================================================


class TestAnsiStrip:
    """ANSI 转义序列过滤测试。"""

    def test_ansi_escape_stripped(self) -> None:
        """输出中的 ANSI 转义序列被移除。"""
        tool = ShellExecTool()
        # 常见 ANSI 转义序列
        input_text = "\x1b[31mRed Text\x1b[0m normal \x1b[1;32mBold Green\x1b[0m"
        result = tool._strip_ansi(input_text)
        assert result == "Red Text normal Bold Green"

    def test_ansi_escape_no_ansi(self) -> None:
        """无 ANSI 转义序列的文本保持不变。"""
        tool = ShellExecTool()
        input_text = "plain text without escapes"
        result = tool._strip_ansi(input_text)
        assert result == "plain text without escapes"

    def test_ansi_escape_complex(self) -> None:
        """复杂 ANSI 转义序列被正确移除。"""
        tool = ShellExecTool()
        # 包含 OSC 序列
        input_text = "\x1b]0;window title\x07prompt$ "
        result = tool._strip_ansi(input_text)
        assert result == "prompt$ "


# ============================================================================
# 输出大小限制测试
# ============================================================================


class TestLargeOutputTruncation:
    """超大输出截断测试。"""

    async def test_large_output_truncated(self, tmp_path: Path) -> None:
        """超大输出被截断到 1MB 限制内。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        # 生成超过 1MB 的输出
        big_size = _MAX_OUTPUT_BYTES + 1024
        result = await tool.execute(
            {
                "command": f'"{os.sys.executable}" -c "import sys; sys.stdout.write(\'A\' * {big_size})"',
                "timeout": 30,
            }
        )
        assert "Exit code: 0" in result
        # 输出应被截断，不应包含完整的 big_size 个 A
        # 截断后的 stdout 部分不应超过限制
        assert "A" in result
        # 验证输出文本长度不超过 _MAX_OUTPUT_BYTES 对应的字符数
        # （因为 UTF-8 单字节字符，字节数 ≈ 字符数）
        stdout_section = result.split("--- stdout ---\n")[1].split("\n--- stderr ---")[0]
        assert len(stdout_section.encode("utf-8")) <= _MAX_OUTPUT_BYTES + 1000  # 允许额外格式开销
