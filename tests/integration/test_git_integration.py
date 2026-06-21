"""Git 集成测试。

验证 TrackedToolWrapper 与 GitManager 的端到端集成：
通过 write_file 工具写入文件后，git log 中存在对应的 [codepilot] 提交。
使用 tmp_path 初始化真实 git 仓库，不 mock git 命令。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from codepilot.app import App
from codepilot.config import (
    AnthropicConfig,
    Config,
    DeepSeekConfig,
    SecurityConfig,
)

# ============================================================================
# 辅助函数
# ============================================================================


def _init_git_repo(repo_path: Path) -> None:
    """在指定路径初始化真实 git 仓库并配置 user.name/user.email。"""
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )


def _clear_codepilot_env() -> None:
    """清除所有 CODEPILOT_ 前缀的环境变量，避免污染测试。"""
    for key in list(os.environ.keys()):
        if key.startswith("CODEPILOT_"):
            del os.environ[key]


def _make_config(tmp_path: Path) -> Config:
    """构造测试用 Config，workspace_root 指向 tmp_path。"""
    _clear_codepilot_env()
    return Config(
        provider="deepseek",
        deepseek=DeepSeekConfig(api_key=SecretStr("sk-test-deepseek")),
        anthropic=AnthropicConfig(api_key=SecretStr("sk-test-anthropic")),
        security=SecurityConfig(
            workspace_root=str(tmp_path),
            blocked_paths=[],
        ),
    )


class _AutoApproveApproval:
    """测试用审批器，自动批准所有操作。"""

    async def request_approval(self, operation: str, details: dict[str, Any]) -> bool:
        return True


# ============================================================================
# 集成测试
# ============================================================================


class TestGitIntegration:
    """Git 与 TrackedToolWrapper 端到端集成测试。"""

    async def test_tracked_tool_auto_commit(self, tmp_path: Path) -> None:
        """通过 write_file 工具写入文件后，git log 中存在对应的 [codepilot] 提交。"""
        _init_git_repo(tmp_path)
        config = _make_config(tmp_path)
        app = App(config)

        # 获取 write_file 工具（已被 TrackedToolWrapper 包装）
        write_tool = app.tool_registry.get("write_file")
        assert write_tool is not None

        # 使用自动批准的审批器执行写入
        approval = _AutoApproveApproval()
        result = await write_tool.execute(
            {"path": "integration_test.py", "content": "print('integration')\n"},
            sandbox=app.sandbox,
            approval=approval,
        )
        assert "File written" in result

        # 验证 git log 中存在 [codepilot] 提交
        log_result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "[codepilot]" in log_result.stdout

        # 验证提交内容包含该文件
        show_result = subprocess.run(
            ["git", "show", "--stat", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "integration_test.py" in show_result.stdout
