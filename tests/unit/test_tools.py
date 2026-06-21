"""工具系统单元测试。

覆盖 7 个核心工具的正常/边界/错误路径，以及注册表的格式转换。
使用 tmp_path fixture 隔离文件系统，不写死路径。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from codepilot.tools.file_edit import EditFileTool
from codepilot.tools.file_read import ReadFileTool
from codepilot.tools.file_write import WriteFileTool
from codepilot.tools.list_files import ListFilesTool
from codepilot.tools.registry import GetContextTool, ToolRegistry
from codepilot.tools.search_code import SearchCodeTool
from codepilot.tools.shell_exec import ShellExecTool

# ============================================================================
# 辅助类：mock sandbox 与 approval
# ============================================================================


class _MockSandbox:
    """模拟沙箱，按路径/命令黑名单拒绝。"""

    def __init__(
        self,
        reject_path_patterns: list[str] | None = None,
        reject_command_patterns: list[str] | None = None,
    ) -> None:
        self.reject_path_patterns = reject_path_patterns or []
        self.reject_command_patterns = reject_command_patterns or []

    def validate_path(self, path: str, operation: str = "read") -> tuple[bool, str]:
        for pattern in self.reject_path_patterns:
            if pattern in path:
                return False, f"path matches blocked pattern: {pattern}"
        return True, ""

    def validate_command(self, command: str) -> tuple[bool, str]:
        for pattern in self.reject_command_patterns:
            if pattern in command:
                return False, f"command matches blocked pattern: {pattern}"
        return True, ""


class _MockApproval:
    """模拟审批器，记录调用并按预设返回审批结果。"""

    def __init__(self, approved: bool = True) -> None:
        self.approved = approved
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request_approval(self, operation: str, details: dict[str, Any]) -> bool:
        self.calls.append((operation, details))
        return self.approved


def _count_numbered_lines(output: str) -> int:
    """统计工具输出中带行号的行数（含 '→' 的行）。"""
    return sum(1 for line in output.splitlines() if "→" in line)


# ============================================================================
# ReadFileTool 测试
# ============================================================================


class TestReadFileTool:
    """ReadFileTool 测试：正常/边界/错误路径。"""

    @pytest.mark.parametrize(
        "content,expected_lines",
        [
            ("line1\nline2\n", 3),  # 末尾换行产生空行
            ("single line", 1),
            ("", 1),  # 空文件仍输出 1 个空行号
            ("a\nb\nc", 3),  # 无末尾换行
        ],
    )
    async def test_read_normal_file(
        self, tmp_path: Path, content: str, expected_lines: int
    ) -> None:
        """正常读取文件，验证行号格式与行数。"""
        file_path = tmp_path / "test.txt"
        file_path.write_text(content, encoding="utf-8")
        tool = ReadFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "test.txt"})
        assert result.startswith("Read file: test.txt")
        assert _count_numbered_lines(result) == expected_lines

    async def test_read_binary_file_skipped(self, tmp_path: Path) -> None:
        """二进制文件（含 \\x00）被跳过，返回错误。"""
        file_path = tmp_path / "binary.bin"
        file_path.write_bytes(b"hello\x00world")
        tool = ReadFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "binary.bin"})
        assert "Error" in result
        assert "binary" in result.lower()

    async def test_read_large_file_truncated(self, tmp_path: Path) -> None:
        """超过 100KB 的文件被截断。"""
        file_path = tmp_path / "large.txt"
        # 写入 101KB 内容
        content = "a" * (101 * 1024)
        file_path.write_text(content, encoding="utf-8")
        tool = ReadFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "large.txt"})
        assert "truncated" in result

    async def test_read_nonexistent_file(self, tmp_path: Path) -> None:
        """读取不存在的文件返回错误。"""
        tool = ReadFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "nonexistent.txt"})
        assert "Error" in result
        assert "not found" in result.lower()

    async def test_read_missing_path_param(self, tmp_path: Path) -> None:
        """缺少 path 参数返回错误。"""
        tool = ReadFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({})
        assert "Error" in result
        assert "path" in result.lower()

    async def test_read_sandbox_rejected(self, tmp_path: Path) -> None:
        """sandbox 拒绝路径时返回错误。"""
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello", encoding="utf-8")
        tool = ReadFileTool(workspace_root=str(tmp_path))
        sandbox = _MockSandbox(reject_path_patterns=["test.txt"])
        result = await tool.execute({"path": "test.txt"}, sandbox=sandbox)
        assert "Error" in result
        assert "validation failed" in result


# ============================================================================
# WriteFileTool 测试
# ============================================================================


class TestWriteFileTool:
    """WriteFileTool 测试：正常/边界/错误路径。"""

    async def test_write_normal(self, tmp_path: Path) -> None:
        """正常写入文件。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "out.txt", "content": "hello\nworld\n"})
        assert "File written" in result
        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello\nworld\n"

    async def test_write_creates_parent_dir(self, tmp_path: Path) -> None:
        """写入时自动创建父目录。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "sub/dir/out.txt", "content": "nested"})
        assert "File written" in result
        nested_path = tmp_path / "sub" / "dir" / "out.txt"
        assert nested_path.read_text(encoding="utf-8") == "nested"

    async def test_write_path_escape_rejected(self, tmp_path: Path) -> None:
        """sandbox 拒绝路径逃逸（.. 路径）。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        sandbox = _MockSandbox(reject_path_patterns=[".."])
        result = await tool.execute(
            {"path": "../escape.txt", "content": "evil"},
            sandbox=sandbox,
        )
        assert "Error" in result
        assert "validation failed" in result
        # 确保文件未被写入
        assert not (tmp_path.parent / "escape.txt").exists()

    async def test_write_approval_rejected(self, tmp_path: Path) -> None:
        """审批被拒绝时返回错误且不写入。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        approval = _MockApproval(approved=False)
        result = await tool.execute(
            {"path": "out.txt", "content": "data"},
            approval=approval,
        )
        assert "Error" in result
        assert "not approved" in result
        assert not (tmp_path / "out.txt").exists()

    async def test_write_approval_approved(self, tmp_path: Path) -> None:
        """审批通过后正常写入。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        approval = _MockApproval(approved=True)
        result = await tool.execute(
            {"path": "out.txt", "content": "data"},
            approval=approval,
        )
        assert "File written" in result
        assert len(approval.calls) == 1
        assert approval.calls[0][0] == "file_write"

    async def test_write_missing_path_param(self, tmp_path: Path) -> None:
        """缺少 path 参数返回错误。"""
        tool = WriteFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"content": "data"})
        assert "Error" in result


# ============================================================================
# EditFileTool 测试
# ============================================================================


class TestEditFileTool:
    """EditFileTool 测试：正常/边界/错误路径。"""

    async def test_unique_match_replace(self, tmp_path: Path) -> None:
        """唯一匹配时成功替换。"""
        file_path = tmp_path / "edit.txt"
        file_path.write_text("foo bar baz", encoding="utf-8")
        tool = EditFileTool(workspace_root=str(tmp_path))
        result = await tool.execute(
            {"path": "edit.txt", "old_string": "bar", "new_string": "qux"}
        )
        assert "File edited" in result
        assert file_path.read_text(encoding="utf-8") == "foo qux baz"

    async def test_zero_match_error(self, tmp_path: Path) -> None:
        """零匹配时返回错误。"""
        file_path = tmp_path / "edit.txt"
        file_path.write_text("foo bar baz", encoding="utf-8")
        tool = EditFileTool(workspace_root=str(tmp_path))
        result = await tool.execute(
            {"path": "edit.txt", "old_string": "xyz", "new_string": "qux"}
        )
        assert "Error" in result
        assert "not found" in result.lower()
        # 文件未被修改
        assert file_path.read_text(encoding="utf-8") == "foo bar baz"

    async def test_multiple_match_error(self, tmp_path: Path) -> None:
        """多次匹配时返回错误。"""
        file_path = tmp_path / "edit.txt"
        file_path.write_text("foo foo foo", encoding="utf-8")
        tool = EditFileTool(workspace_root=str(tmp_path))
        result = await tool.execute(
            {"path": "edit.txt", "old_string": "foo", "new_string": "bar"}
        )
        assert "Error" in result
        assert "3 times" in result or "multiple" in result.lower()

    async def test_edit_nonexistent_file(self, tmp_path: Path) -> None:
        """编辑不存在的文件返回错误。"""
        tool = EditFileTool(workspace_root=str(tmp_path))
        result = await tool.execute(
            {"path": "nope.txt", "old_string": "a", "new_string": "b"}
        )
        assert "Error" in result
        assert "not found" in result.lower()

    async def test_edit_missing_old_string(self, tmp_path: Path) -> None:
        """缺少 old_string 参数返回错误。"""
        tool = EditFileTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "x.txt", "new_string": "b"})
        assert "Error" in result
        assert "old_string" in result

    async def test_edit_approval_rejected(self, tmp_path: Path) -> None:
        """编辑审批被拒绝时返回错误。"""
        file_path = tmp_path / "edit.txt"
        file_path.write_text("foo bar", encoding="utf-8")
        tool = EditFileTool(workspace_root=str(tmp_path))
        approval = _MockApproval(approved=False)
        result = await tool.execute(
            {"path": "edit.txt", "old_string": "foo", "new_string": "baz"},
            approval=approval,
        )
        assert "Error" in result
        assert "not approved" in result
        # 文件未被修改
        assert file_path.read_text(encoding="utf-8") == "foo bar"


# ============================================================================
# ListFilesTool 测试
# ============================================================================


class TestListFilesTool:
    """ListFilesTool 测试：正常/边界/错误路径。"""

    async def test_tree_structure(self, tmp_path: Path) -> None:
        """正常目录树结构输出。"""
        (tmp_path / "file1.txt").write_text("a", encoding="utf-8")
        (tmp_path / "file2.py").write_text("b", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "nested.txt").write_text("c", encoding="utf-8")
        tool = ListFilesTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "."})
        assert "Listed" in result
        assert "file1.txt" in result
        assert "file2.py" in result
        assert "subdir/" in result
        assert "nested.txt" in result

    async def test_depth_limit(self, tmp_path: Path) -> None:
        """深度限制生效，深层文件不显示。"""
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "b").mkdir()
        (tmp_path / "a" / "b" / "deep.txt").write_text("x", encoding="utf-8")
        tool = ListFilesTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": ".", "max_depth": 1})
        assert "a/" in result
        # depth=1 时不应显示 deep.txt
        assert "deep.txt" not in result

    async def test_empty_dir(self, tmp_path: Path) -> None:
        """空目录返回空提示。"""
        tool = ListFilesTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "."})
        assert "0 entries" in result
        assert "empty" in result.lower()

    async def test_ignore_dirs(self, tmp_path: Path) -> None:
        """忽略目录被过滤。"""
        (tmp_path / "normal.txt").write_text("a", encoding="utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cache.pyc").write_text("x", encoding="utf-8")
        tool = ListFilesTool(workspace_root=str(tmp_path))
        result = await tool.execute({"path": "."})
        assert "normal.txt" in result
        assert "__pycache__" not in result


# ============================================================================
# ShellExecTool 测试
# ============================================================================


class TestShellExecTool:
    """ShellExecTool 测试：正常/边界/错误路径。"""

    async def test_normal_command(self, tmp_path: Path) -> None:
        """正常命令执行并捕获输出。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        # echo 在 Windows cmd 和 Unix shell 都可用
        result = await tool.execute({"command": "echo hello_codepilot"})
        assert "Exit code: 0" in result
        assert "hello_codepilot" in result

    async def test_timeout(self, tmp_path: Path) -> None:
        """超时命令被终止并返回超时错误。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        # 跨平台睡眠命令
        result = await tool.execute(
            {
                "command": f'"{sys.executable}" -c "import time; time.sleep(10)"',
                "timeout": 1,
            }
        )
        assert "timed out" in result

    async def test_interactive_rejected(self, tmp_path: Path) -> None:
        """交互式命令被拒绝。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        result = await tool.execute({"command": "vim somefile"})
        assert "Error" in result
        assert "interactive" in result.lower()

    async def test_chain_blacklist(self, tmp_path: Path) -> None:
        """sandbox 拒绝含黑名单模式的链式命令。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        sandbox = _MockSandbox(reject_command_patterns=["rm -rf"])
        result = await tool.execute(
            {"command": "ls && rm -rf /"},
            sandbox=sandbox,
        )
        assert "Error" in result
        assert "validation failed" in result

    async def test_missing_command_param(self, tmp_path: Path) -> None:
        """缺少 command 参数返回错误。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        result = await tool.execute({})
        assert "Error" in result
        assert "command" in result.lower()

    async def test_approval_rejected(self, tmp_path: Path) -> None:
        """命令审批被拒绝时返回错误。"""
        tool = ShellExecTool(workspace_root=str(tmp_path))
        approval = _MockApproval(approved=False)
        result = await tool.execute(
            {"command": "echo hi"},
            approval=approval,
        )
        assert "Error" in result
        assert "not approved" in result


# ============================================================================
# SearchCodeTool 测试
# ============================================================================


class TestSearchCodeTool:
    """SearchCodeTool 测试：正常/边界/错误路径。"""

    async def test_regex_match(self, tmp_path: Path) -> None:
        """正则匹配返回正确结果。"""
        (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("def bar():\n    return 1\n", encoding="utf-8")
        tool = SearchCodeTool(workspace_root=str(tmp_path))
        result = await tool.execute({"pattern": "def \\w+"})
        assert "Found" in result
        assert "a.py" in result
        assert "b.py" in result
        assert "def foo" in result
        assert "def bar" in result

    async def test_fnmatch_filter(self, tmp_path: Path) -> None:
        """include glob 过滤文件类型。"""
        (tmp_path / "a.py").write_text("target_line\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("target_line\n", encoding="utf-8")
        tool = SearchCodeTool(workspace_root=str(tmp_path))
        result = await tool.execute({"pattern": "target", "include": "*.py"})
        assert "a.py" in result
        # b.txt 被 include 过滤掉
        assert "b.txt" not in result

    async def test_zero_results(self, tmp_path: Path) -> None:
        """无匹配时返回 0 结果。"""
        (tmp_path / "a.py").write_text("hello world\n", encoding="utf-8")
        tool = SearchCodeTool(workspace_root=str(tmp_path))
        result = await tool.execute({"pattern": "nonexistent_pattern_xyz"})
        assert "0 matches" in result

    async def test_invalid_regex(self, tmp_path: Path) -> None:
        """无效正则返回错误。"""
        tool = SearchCodeTool(workspace_root=str(tmp_path))
        result = await tool.execute({"pattern": "[invalid"})
        assert "Error" in result
        assert "regex" in result.lower()

    async def test_missing_pattern_param(self, tmp_path: Path) -> None:
        """缺少 pattern 参数返回错误。"""
        tool = SearchCodeTool(workspace_root=str(tmp_path))
        result = await tool.execute({})
        assert "Error" in result
        assert "pattern" in result.lower()

    async def test_search_skips_binary(self, tmp_path: Path) -> None:
        """搜索跳过二进制文件。"""
        (tmp_path / "a.py").write_text("target_line\n", encoding="utf-8")
        (tmp_path / "b.bin").write_bytes(b"target\x00binary")
        tool = SearchCodeTool(workspace_root=str(tmp_path))
        result = await tool.execute({"pattern": "target"})
        assert "a.py" in result
        assert "b.bin" not in result


# ============================================================================
# GetContextTool 测试
# ============================================================================


class TestGetContextTool:
    """GetContextTool 测试。"""

    async def test_no_context_manager(self) -> None:
        """无 context_manager 时返回不可用提示。"""
        tool = GetContextTool()
        result = await tool.execute({})
        assert "not available" in result

    async def test_with_context_manager(self) -> None:
        """有 context_manager 时返回统计信息。"""

        class _FakeContextManager:
            def get_stats(self) -> dict[str, Any]:
                return {
                    "total_tokens": 1000,
                    "max_tokens": 10000,
                    "message_count": 5,
                    "compressed": False,
                }

        tool = GetContextTool(context_manager=_FakeContextManager())
        result = await tool.execute({})
        assert "Total tokens: 1000" in result
        assert "Max tokens: 10000" in result
        assert "Messages: 5" in result
        assert "Compressed: no" in result


# ============================================================================
# ToolRegistry 测试
# ============================================================================


class TestToolRegistry:
    """ToolRegistry 测试：注册/获取/格式转换。"""

    def test_register_and_get(self) -> None:
        """注册工具后可按名获取。"""
        registry = ToolRegistry()
        tool = ReadFileTool(workspace_root=".")
        registry.register(tool)
        assert registry.get("read_file") is tool
        assert registry.get("nonexistent") is None

    def test_list_tools(self) -> None:
        """list_tools 返回所有已注册工具。"""
        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root="."))
        registry.register(WriteFileTool(workspace_root="."))
        tools = registry.list_tools()
        assert len(tools) == 2

    def test_to_openai_format(self) -> None:
        """to_openai_format 返回 OpenAI 工具定义。"""
        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root="."))
        formats = registry.to_openai_format()
        assert len(formats) == 1
        fmt = formats[0]
        assert fmt["type"] == "function"
        assert fmt["function"]["name"] == "read_file"
        assert "description" in fmt["function"]
        assert "parameters" in fmt["function"]

    def test_to_anthropic_format(self) -> None:
        """to_anthropic_format 返回 Anthropic 工具定义。"""
        registry = ToolRegistry()
        registry.register(ReadFileTool(workspace_root="."))
        formats = registry.to_anthropic_format()
        assert len(formats) == 1
        fmt = formats[0]
        assert fmt["name"] == "read_file"
        assert "description" in fmt
        assert "input_schema" in fmt

    def test_create_default_registry(self, tmp_path: Path) -> None:
        """create_default_registry 创建包含 10 个工具的注册表。"""
        registry = ToolRegistry.create_default_registry(workspace_root=str(tmp_path))
        tools = registry.list_tools()
        assert len(tools) == 10
        names = {t.name for t in tools}
        expected = {
            "read_file",
            "write_file",
            "edit_file",
            "list_files",
            "shell_exec",
            "search_code",
            "web_fetch",
            "get_context",
            "diagnose",
            "plan",
        }
        assert names == expected

    def test_tool_format_typeddict_compliance(self) -> None:
        """工具格式输出符合 TypedDict 键约束。"""
        tool = ReadFileTool(workspace_root=".")
        openai_fmt = tool.to_openai_format()
        # OpenAIToolDef 必须含 type 和 function
        assert set(openai_fmt.keys()) == {"type", "function"}
        # function 子结构必须含 name/description/parameters
        assert set(openai_fmt["function"].keys()) == {
            "name",
            "description",
            "parameters",
        }
        anthropic_fmt = tool.to_anthropic_format()
        # AnthropicToolDef 必须含 name/description/input_schema
        assert set(anthropic_fmt.keys()) == {"name", "description", "input_schema"}
