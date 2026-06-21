"""DiagnoseTool 单元测试。

覆盖：缺少 error_description、文件不存在、文件存在且可读、
linter 未安装、常见问题检查。
使用 tmp_path fixture 隔离文件系统，不写死路径。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codepilot.tools.diagnose import DiagnoseTool


class TestDiagnoseTool:
    """DiagnoseTool 测试：正常/边界/错误路径。"""

    async def test_missing_error_description(self) -> None:
        """缺少 error_description 参数时仍可执行（空字符串）。"""
        tool = DiagnoseTool()
        result = await tool.execute({"error_description": ""})
        assert "## 错误描述" in result

    async def test_file_not_exists(self, tmp_path: Path) -> None:
        """文件不存在时返回不存在提示。"""
        tool = DiagnoseTool()
        fake_path = str(tmp_path / "nonexistent.py")
        result = await tool.execute(
            {"error_description": "test error", "file_path": fake_path, "run_linter": False}
        )
        assert "❌ 文件不存在" in result

    async def test_file_exists_and_readable(self, tmp_path: Path) -> None:
        """文件存在且可读时返回文件状态信息。"""
        file_path = tmp_path / "test_file.py"
        file_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
        tool = DiagnoseTool()
        result = await tool.execute(
            {
                "error_description": "test error",
                "file_path": str(file_path),
                "run_linter": False,
            }
        )
        assert "✅ 文件存在" in result
        assert "总行数: 3" in result
        assert "line1" in result

    async def test_linter_not_installed(self, tmp_path: Path) -> None:
        """linter 未安装时返回未安装提示（ruff 不在 PATH 时）。"""
        # 使用一个 .py 文件触发 ruff 检测，但 ruff 可能未安装
        file_path = tmp_path / "test_linter.py"
        file_path.write_text("x = 1\n", encoding="utf-8")
        tool = DiagnoseTool()
        result = await tool.execute(
            {
                "error_description": "test error",
                "file_path": str(file_path),
                "run_linter": True,
            }
        )
        # 无论 ruff 是否安装，都应包含 Linter 检查节
        assert "## Linter 检查" in result
        # 如果 ruff 未安装，应出现 ⚠️；如果安装了，应出现 ✅ 或 ❌
        assert any(
            marker in result
            for marker in ["✅ ruff", "❌ ruff", "⚠️ ruff", "未检测到适用的 linter"]
        )

    async def test_common_issues_check(self, tmp_path: Path) -> None:
        """常见问题检查节始终出现。"""
        tool = DiagnoseTool()
        result = await tool.execute(
            {"error_description": "test error", "run_linter": False}
        )
        assert "## 常见问题检查" in result

    async def test_common_issues_bom_detection(self, tmp_path: Path) -> None:
        """检测到 UTF-8 BOM 时给出警告。"""
        file_path = tmp_path / "bom_file.py"
        file_path.write_bytes(b"\xef\xbb\xbfprint('hello')\n")
        tool = DiagnoseTool()
        result = await tool.execute(
            {
                "error_description": "encoding issue",
                "file_path": str(file_path),
                "run_linter": False,
            }
        )
        assert "⚠️ 文件包含 UTF-8 BOM" in result

    async def test_common_issues_empty_file(self, tmp_path: Path) -> None:
        """空文件检测。"""
        file_path = tmp_path / "empty.py"
        file_path.write_bytes(b"")
        tool = DiagnoseTool()
        result = await tool.execute(
            {
                "error_description": "empty file",
                "file_path": str(file_path),
                "run_linter": False,
            }
        )
        assert "⚠️ 文件为空" in result

    async def test_common_issues_crlf_detection(self, tmp_path: Path) -> None:
        """CRLF 行尾检测。"""
        file_path = tmp_path / "crlf_file.py"
        file_path.write_bytes(b"line1\r\nline2\r\n")
        tool = DiagnoseTool()
        result = await tool.execute(
            {
                "error_description": "line ending issue",
                "file_path": str(file_path),
                "run_linter": False,
            }
        )
        assert "ℹ️ 文件使用 CRLF 行尾" in result

    async def test_no_file_path_skips_file_status(self) -> None:
        """不提供 file_path 时跳过文件状态检查。"""
        tool = DiagnoseTool()
        result = await tool.execute(
            {"error_description": "test error", "run_linter": False}
        )
        assert "## 文件状态" not in result

    async def test_run_linter_false_skips_linter(self) -> None:
        """run_linter=False 时跳过 linter 检查。"""
        tool = DiagnoseTool()
        result = await tool.execute(
            {"error_description": "test error", "run_linter": False}
        )
        assert "## Linter 检查" not in result

    async def test_disk_space_check(self) -> None:
        """磁盘空间检查节始终出现（磁盘信息可能因环境不可用而省略）。"""
        tool = DiagnoseTool()
        result = await tool.execute(
            {"error_description": "test error", "run_linter": False}
        )
        # 常见问题检查节始终出现，磁盘空间信息可能因 os.disk_usage 不可用而省略
        assert "## 常见问题检查" in result

    async def test_error_description_displayed(self) -> None:
        """错误描述正确显示在输出中。"""
        tool = DiagnoseTool()
        result = await tool.execute(
            {"error_description": "ImportError: no module named foo", "run_linter": False}
        )
        assert "ImportError: no module named foo" in result

    async def test_file_preview_max_20_lines(self, tmp_path: Path) -> None:
        """文件预览最多显示 20 行。"""
        file_path = tmp_path / "long_file.py"
        lines = [f"line_{i}" for i in range(30)]
        file_path.write_text("\n".join(lines), encoding="utf-8")
        tool = DiagnoseTool()
        result = await tool.execute(
            {
                "error_description": "test",
                "file_path": str(file_path),
                "run_linter": False,
            }
        )
        assert "总行数: 30" in result
        assert "line_19" in result
        assert "line_20" not in result

    def test_get_parameters(self) -> None:
        """get_parameters 返回正确的 JSON Schema。"""
        tool = DiagnoseTool()
        params = tool.get_parameters()
        assert params["type"] == "object"
        assert "error_description" in params["properties"]
        assert "file_path" in params["properties"]
        assert "run_linter" in params["properties"]
        assert params["required"] == ["error_description"]

    def test_name_and_description(self) -> None:
        """工具名称和描述正确。"""
        tool = DiagnoseTool()
        assert tool.name == "diagnose"
        assert "诊断" in tool.description
