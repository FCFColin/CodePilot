"""项目指令文件加载器单元测试。

覆盖：从项目根目录加载、从全局目录加载、从覆盖目录加载、
无指令文件、多指令文件合并。
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codepilot.context.project_instructions import (
    get_loaded_instruction_files,
    load_project_instructions,
)


def test_load_from_project_root(tmp_path: Path):
    """项目根目录的 CODEPILOT.md 应被加载。"""
    codepilot_md = tmp_path / "CODEPILOT.md"
    codepilot_md.write_text("Use Python 3.11 for this project.", encoding="utf-8")

    result = load_project_instructions(str(tmp_path))
    assert "Project Instructions" in result
    assert "Use Python 3.11" in result


def test_load_from_global(tmp_path: Path):
    """全局 ~/.codepilot/CODEPILOT.md 应被加载。"""
    global_dir = tmp_path / "home" / ".codepilot"
    global_dir.mkdir(parents=True)
    global_md = global_dir / "CODEPILOT.md"
    global_md.write_text("Always write tests.", encoding="utf-8")

    with patch.object(Path, "home", return_value=tmp_path / "home"):
        result = load_project_instructions(str(tmp_path / "workspace"))

    assert "Global Instructions" in result
    assert "Always write tests." in result


def test_load_from_override(tmp_path: Path):
    """项目级 .codepilot/CODEPILOT.md 覆盖应被加载。"""
    override_dir = tmp_path / ".codepilot"
    override_dir.mkdir()
    override_md = override_dir / "CODEPILOT.md"
    override_md.write_text("Use strict linting rules.", encoding="utf-8")

    result = load_project_instructions(str(tmp_path))
    assert "Project Override Instructions" in result
    assert "Use strict linting rules." in result


def test_no_instructions_file(tmp_path: Path):
    """无指令文件时应返回空字符串。"""
    # 确保全局路径也不存在
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch.object(Path, "home", return_value=fake_home):
        result = load_project_instructions(str(tmp_path / "workspace"))

    assert result == ""


def test_multiple_instruction_files(tmp_path: Path):
    """多个指令文件应按优先级合并。"""
    # 全局指令
    global_dir = tmp_path / "home" / ".codepilot"
    global_dir.mkdir(parents=True)
    (global_dir / "CODEPILOT.md").write_text("Global rule.", encoding="utf-8")

    # 项目根目录指令
    (tmp_path / "CODEPILOT.md").write_text("Project rule.", encoding="utf-8")

    # 项目级覆盖
    override_dir = tmp_path / ".codepilot"
    override_dir.mkdir()
    (override_dir / "CODEPILOT.md").write_text("Override rule.", encoding="utf-8")

    with patch.object(Path, "home", return_value=tmp_path / "home"):
        result = load_project_instructions(str(tmp_path))

    assert "Global Instructions" in result
    assert "Global rule." in result
    assert "Project Instructions" in result
    assert "Project rule." in result
    assert "Project Override Instructions" in result
    assert "Override rule." in result

    # 验证顺序：全局 → 项目 → 覆盖
    assert result.index("Global") < result.index("Project Instructions")
    assert result.index("Project Instructions") < result.index("Project Override")


def test_get_loaded_instruction_files(tmp_path: Path):
    """get_loaded_instruction_files 应返回存在的文件路径列表。"""
    # 只创建项目根目录的 CODEPILOT.md
    (tmp_path / "CODEPILOT.md").write_text("Test", encoding="utf-8")

    fake_home = tmp_path / "home"
    fake_home.mkdir()

    with patch.object(Path, "home", return_value=fake_home):
        files = get_loaded_instruction_files(str(tmp_path))

    assert len(files) == 1
    assert "CODEPILOT.md" in files[0]


def test_empty_codepilot_md_ignored(tmp_path: Path):
    """空的 CODEPILOT.md 文件应被忽略。"""
    (tmp_path / "CODEPILOT.md").write_text("   \n  \n", encoding="utf-8")

    result = load_project_instructions(str(tmp_path))
    # 空白内容被 strip 后为空，不应包含 Project Instructions
    assert "Project Instructions" not in result
