"""工具执行集成测试。

在真实临时目录中执行工具，验证端到端的文件 I/O 和命令执行行为。
使用 tmp_path fixture 隔离文件系统，不写死路径。
"""

from __future__ import annotations

from pathlib import Path

from codepilot.tools.file_edit import EditFileTool
from codepilot.tools.file_read import ReadFileTool
from codepilot.tools.file_write import WriteFileTool
from codepilot.tools.list_files import ListFilesTool
from codepilot.tools.shell_exec import ShellExecTool


class TestToolExecution:
    """工具执行集成测试：在真实临时目录中验证工具行为。"""

    async def test_write_then_read_consistency(self, tmp_path: Path) -> None:
        """write_file 然后 read_file 验证内容一致。"""
        content = "line1\nline2\nline3\n"
        write_tool = WriteFileTool(workspace_root=str(tmp_path))
        read_tool = ReadFileTool(workspace_root=str(tmp_path))

        # 写入文件
        write_result = await write_tool.execute(
            {"path": "data.txt", "content": content}
        )
        assert "File written" in write_result

        # 读取文件
        read_result = await read_tool.execute({"path": "data.txt"})
        assert "Read file: data.txt" in read_result
        # 验证内容一致（带行号显示）
        assert "line1" in read_result
        assert "line2" in read_result
        assert "line3" in read_result

        # 验证磁盘内容
        assert (tmp_path / "data.txt").read_text(encoding="utf-8") == content

    async def test_edit_file_replacement(self, tmp_path: Path) -> None:
        """执行 edit_file 验证替换结果。"""
        file_path = tmp_path / "edit_target.txt"
        file_path.write_text("foo bar baz\nsecond line", encoding="utf-8")

        edit_tool = EditFileTool(workspace_root=str(tmp_path))
        result = await edit_tool.execute(
            {
                "path": "edit_target.txt",
                "old_string": "bar",
                "new_string": "qux",
            }
        )

        assert "File edited" in result
        assert "1 replacement" in result
        # 验证替换结果
        updated = file_path.read_text(encoding="utf-8")
        assert "foo qux baz" in updated
        assert "bar" not in updated
        assert "second line" in updated

    async def test_list_files_tree(self, tmp_path: Path) -> None:
        """执行 list_files 验证目录树结构。"""
        # 创建目录结构
        (tmp_path / "file1.txt").write_text("a", encoding="utf-8")
        (tmp_path / "file2.py").write_text("b", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        (tmp_path / "subdir" / "nested.txt").write_text("c", encoding="utf-8")

        list_tool = ListFilesTool(workspace_root=str(tmp_path))
        result = await list_tool.execute({"path": ".", "max_depth": 3})

        assert "Listed" in result
        # 验证文件和目录出现在树中
        assert "file1.txt" in result
        assert "file2.py" in result
        assert "subdir/" in result
        assert "nested.txt" in result
        # 验证树形连接符
        assert "├──" in result or "└──" in result

    async def test_shell_exec_output(self, tmp_path: Path) -> None:
        """执行 shell_exec 验证命令输出。"""
        shell_tool = ShellExecTool(workspace_root=str(tmp_path))
        # echo 在 Windows cmd 和 Unix shell 都可用
        result = await shell_tool.execute(
            {"command": "echo codepilot_integration_test"}
        )

        assert "Exit code: 0" in result
        assert "codepilot_integration_test" in result
        assert "--- stdout ---" in result

    async def test_write_creates_nested_dirs(self, tmp_path: Path) -> None:
        """write_file 自动创建嵌套父目录。"""
        write_tool = WriteFileTool(workspace_root=str(tmp_path))

        result = await write_tool.execute(
            {"path": "deep/nested/dir/file.txt", "content": "nested content"}
        )

        assert "File written" in result
        nested_file = tmp_path / "deep" / "nested" / "dir" / "file.txt"
        assert nested_file.exists()
        assert nested_file.read_text(encoding="utf-8") == "nested content"

    async def test_edit_then_read_verification(self, tmp_path: Path) -> None:
        """edit_file 后 read_file 验证修改生效。"""
        file_path = tmp_path / "config.txt"
        file_path.write_text("debug=false\nport=8080\nhost=localhost", encoding="utf-8")

        edit_tool = EditFileTool(workspace_root=str(tmp_path))
        read_tool = ReadFileTool(workspace_root=str(tmp_path))

        # 编辑：将 debug=false 改为 debug=true
        edit_result = await edit_tool.execute(
            {
                "path": "config.txt",
                "old_string": "debug=false",
                "new_string": "debug=true",
            }
        )
        assert "File edited" in edit_result

        # 读取验证
        read_result = await read_tool.execute({"path": "config.txt"})
        assert "debug=true" in read_result
        assert "debug=false" not in read_result
        assert "port=8080" in read_result
