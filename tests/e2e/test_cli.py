"""端到端 CLI 测试。

全部通过 subprocess.run 调用已安装的 codepilot 命令，不 import 内部模块。
验证 --version、--help、管道 /quit 退出、无 API Key 失败、python -m 一致性。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

# 标记本模块所有测试为 e2e
pytestmark = pytest.mark.e2e

# 用于通过配置校验的虚拟 API Key
_DUMMY_API_KEY = "test-dummy-key-for-e2e"


def _env_with_api_key(api_key: str | None) -> dict[str, str]:
    """构造带指定 API Key 的环境变量字典。

    Args:
        api_key: API Key 值，None 表示清除。

    Returns:
        环境变量字典。
    """
    env = dict(os.environ)
    # 清除所有可能的 API Key 环境变量
    for key in (
        "CODEPILOT_API_KEY",
        "CODEPILOT_DEEPSEEK__API_KEY",
        "CODEPILOT_ANTHROPIC__API_KEY",
        "CODEPILOT_PROVIDERS__XUNFEI__API_KEY",
        "CODEPILOT_PROVIDERS__DEEPSEEK__API_KEY",
    ):
        env.pop(key, None)
    if api_key is not None:
        env["CODEPILOT_API_KEY"] = api_key
    return env


class TestCLI:
    """端到端 CLI 测试。"""

    def test_version(self) -> None:
        """codepilot --version 返回码 0 且输出包含版本号。"""
        result = subprocess.run(
            ["codepilot", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "0.2.0" in result.stdout

    def test_help(self) -> None:
        """codepilot --help 返回码 0 且输出包含关键参数。"""
        result = subprocess.run(
            ["codepilot", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--provider" in result.stdout
        assert "--model" in result.stdout
        assert "--api-key" in result.stdout

    def test_pipe_quit(self) -> None:
        """管道输入 /quit 正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot"],
            input="/quit\n",
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0

    def test_no_api_key_fails(self) -> None:
        """无 API Key 且无配置文件时启动给出清晰错误信息并以非零返回码退出。

        在临时目录中运行，避免读取项目目录下的 .codepilot.yml。
        """
        env = _env_with_api_key(None)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["codepilot"],
                input="/quit\n",
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
                cwd=tmpdir,
            )
            assert result.returncode != 0
            stderr_lower = result.stderr.lower()
            stdout_lower = result.stdout.lower()
            assert "api" in stderr_lower or "key" in stderr_lower or "api" in stdout_lower or "key" in stdout_lower

    def test_python_m_consistency(self) -> None:
        """python -m codepilot --version 与 codepilot --version 输出一致。"""
        r1 = subprocess.run(
            ["codepilot", "--version"],
            capture_output=True,
            text=True,
        )
        r2 = subprocess.run(
            [sys.executable, "-m", "codepilot", "--version"],
            capture_output=True,
            text=True,
        )
        assert r1.stdout == r2.stdout

    def test_pipe_help_command(self) -> None:
        """管道输入 /help 后 /quit 能正常显示帮助并退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot"],
            input="/help\n/quit\n",
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        assert result.returncode == 0
        assert "可用命令" in result.stdout or "Help" in result.stdout
