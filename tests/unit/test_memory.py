"""跨会话持久记忆系统单元测试。

覆盖：保存/加载项目记忆、保存/加载全局记忆、删除记忆、
格式化输出、空记忆。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codepilot.memory.manager import MemoryManager


class TestSaveAndLoadProjectMemory:
    """项目级记忆保存与加载测试。"""

    def test_save_project_memory(self, tmp_path: Any) -> None:
        """保存项目级记忆到文件。"""
        manager = MemoryManager(str(tmp_path))
        manager.save_project_memory("使用 pytest 进行测试")
        # 验证文件已创建
        mem_file = tmp_path / ".codepilot" / "memories.md"
        assert mem_file.is_file()
        content = mem_file.read_text(encoding="utf-8")
        assert "使用 pytest 进行测试" in content

    def test_load_project_memory_on_init(self, tmp_path: Any) -> None:
        """初始化时加载已有项目记忆。"""
        # 先创建记忆文件
        mem_file = tmp_path / ".codepilot" / "memories.md"
        mem_file.parent.mkdir(parents=True, exist_ok=True)
        mem_file.write_text(
            "# Project Memories\n\n- 记忆1\n- 记忆2",
            encoding="utf-8",
        )
        # 重新创建 manager
        manager = MemoryManager(str(tmp_path))
        assert "记忆1" in manager._project_memories
        assert "记忆2" in manager._project_memories

    def test_save_multiple_project_memories(self, tmp_path: Any) -> None:
        """保存多条项目记忆。"""
        manager = MemoryManager(str(tmp_path))
        manager.save_project_memory("第一条")
        manager.save_project_memory("第二条")
        assert len(manager._project_memories) == 2
        # 验证持久化
        mem_file = tmp_path / ".codepilot" / "memories.md"
        content = mem_file.read_text(encoding="utf-8")
        assert "第一条" in content
        assert "第二条" in content


class TestSaveAndLoadGlobalMemory:
    """用户级记忆保存与加载测试。"""

    def test_save_global_memory(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """保存用户级记忆到文件。"""
        # 将全局记忆路径重定向到临时目录
        global_dir = tmp_path / "home" / ".codepilot"
        global_dir.mkdir(parents=True, exist_ok=True)
        global_file = global_dir / "memories.json"

        manager = MemoryManager(str(tmp_path))
        manager._global_memories_path = global_file

        manager.save_global_memory("偏好设置1", scope="preferences")
        assert global_file.is_file()
        data = json.loads(global_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["text"] == "偏好设置1"
        assert data[0]["scope"] == "preferences"
        assert "created_at" in data[0]

    def test_load_global_memory_on_init(self, tmp_path: Any) -> None:
        """初始化时加载已有全局记忆。"""
        global_dir = tmp_path / "home" / ".codepilot"
        global_dir.mkdir(parents=True, exist_ok=True)
        global_file = global_dir / "memories.json"
        global_file.write_text(
            json.dumps([
                {"text": "全局记忆1", "scope": "general", "created_at": "2024-01-01"},
            ]),
            encoding="utf-8",
        )

        manager = MemoryManager(str(tmp_path))
        manager._global_memories_path = global_file
        manager._global_memories = []
        manager._load_memories()
        assert len(manager._global_memories) == 1
        assert manager._global_memories[0]["text"] == "全局记忆1"


class TestDeleteMemory:
    """记忆删除测试。"""

    def test_delete_project_memory_valid_index(self, tmp_path: Any) -> None:
        """删除有效的项目记忆索引。"""
        manager = MemoryManager(str(tmp_path))
        manager.save_project_memory("保留")
        manager.save_project_memory("删除")
        result = manager.delete_project_memory(1)
        assert result is True
        assert len(manager._project_memories) == 1
        assert manager._project_memories[0] == "保留"

    def test_delete_project_memory_invalid_index(self, tmp_path: Any) -> None:
        """删除无效的项目记忆索引返回 False。"""
        manager = MemoryManager(str(tmp_path))
        manager.save_project_memory("唯一")
        result = manager.delete_project_memory(5)
        assert result is False
        assert len(manager._project_memories) == 1

    def test_delete_global_memory_valid_index(self, tmp_path: Any) -> None:
        """删除有效的全局记忆索引。"""
        global_dir = tmp_path / "home" / ".codepilot"
        global_dir.mkdir(parents=True, exist_ok=True)
        global_file = global_dir / "memories.json"

        manager = MemoryManager(str(tmp_path))
        manager._global_memories_path = global_file
        manager.save_global_memory("保留")
        manager.save_global_memory("删除")
        result = manager.delete_global_memory(1)
        assert result is True
        assert len(manager._global_memories) == 1

    def test_delete_global_memory_invalid_index(self, tmp_path: Any) -> None:
        """删除无效的全局记忆索引返回 False。"""
        global_dir = tmp_path / "home" / ".codepilot"
        global_dir.mkdir(parents=True, exist_ok=True)
        global_file = global_dir / "memories.json"

        manager = MemoryManager(str(tmp_path))
        manager._global_memories_path = global_file
        manager.save_global_memory("唯一")
        result = manager.delete_global_memory(-1)
        assert result is False

    def test_delete_negative_index(self, tmp_path: Any) -> None:
        """负索引删除返回 False。"""
        manager = MemoryManager(str(tmp_path))
        result = manager.delete_project_memory(-1)
        assert result is False


class TestGetAllMemoriesText:
    """格式化记忆文本测试。"""

    def test_with_project_memories(self, tmp_path: Any) -> None:
        """包含项目记忆的格式化输出。"""
        manager = MemoryManager(str(tmp_path))
        manager.save_project_memory("项目记忆1")
        text = manager.get_all_memories_text()
        assert "[Project Memory]" in text
        assert "项目记忆1" in text

    def test_with_global_memories(self, tmp_path: Any) -> None:
        """包含全局记忆的格式化输出。"""
        global_dir = tmp_path / "home" / ".codepilot"
        global_dir.mkdir(parents=True, exist_ok=True)
        global_file = global_dir / "memories.json"

        manager = MemoryManager(str(tmp_path))
        manager._global_memories_path = global_file
        manager.save_global_memory("全局记忆1")
        text = manager.get_all_memories_text()
        assert "[Global Memory]" in text
        assert "全局记忆1" in text

    def test_with_both_memories(self, tmp_path: Any) -> None:
        """同时包含项目和全局记忆。"""
        global_dir = tmp_path / "home" / ".codepilot"
        global_dir.mkdir(parents=True, exist_ok=True)
        global_file = global_dir / "memories.json"

        manager = MemoryManager(str(tmp_path))
        manager._global_memories_path = global_file
        manager.save_project_memory("项目记忆")
        manager.save_global_memory("全局记忆")
        text = manager.get_all_memories_text()
        assert "[Project Memory]" in text
        assert "[Global Memory]" in text


class TestEmptyMemories:
    """空记忆测试。"""

    def test_empty_memories_text(self, tmp_path: Any) -> None:
        """无记忆时格式化输出为空字符串。"""
        manager = MemoryManager(str(tmp_path))
        text = manager.get_all_memories_text()
        assert text == ""

    def test_empty_list_memories(self, tmp_path: Any) -> None:
        """无记忆时 list_memories 返回空列表。"""
        manager = MemoryManager(str(tmp_path))
        result = manager.list_memories()
        assert result["project"] == []
        assert result["global"] == []

    def test_load_nonexistent_files(self, tmp_path: Any) -> None:
        """记忆文件不存在时不报错。"""
        manager = MemoryManager(str(tmp_path))
        assert manager._project_memories == []
        assert manager._global_memories == []
