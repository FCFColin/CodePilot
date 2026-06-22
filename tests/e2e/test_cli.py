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

# E2E 测试超时（秒）— tiktoken 加载需要约8秒
E2E_TIMEOUT = 30


def _env_with_api_key(api_key: str | None) -> dict[str, str]:
    """构造带指定 API Key 的环境变量字典。"""
    env = dict(os.environ)
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
            ["codepilot", "--no-approve"],
            input="/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_no_api_key_fails(self) -> None:
        """无 API Key 且无配置文件时启动给出清晰错误信息。"""
        env = _env_with_api_key(None)
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["codepilot", "--no-approve"],
                input="/quit\n",
                capture_output=True,
                text=True,
                timeout=E2E_TIMEOUT,
                env=env,
                cwd=tmpdir,
            )
            # 内置默认 provider 有空 key，validate_config 应报错
            assert result.returncode != 0
            combined = (result.stderr + result.stdout).lower()
            assert "api" in combined or "key" in combined

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
        assert r1.stdout.strip().split("\n")[-1] == r2.stdout.strip().split("\n")[-1]

    def test_pipe_help_command(self) -> None:
        """管道输入 /help 后 /quit 能正常显示帮助并退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/help\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_slash_commands(self) -> None:
        """管道输入多个 slash 命令后 /quit 正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/config\n/stats\n/providers\n/plan\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_model_command(self) -> None:
        """管道输入 /model 后正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/model\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_provider_command(self) -> None:
        """管道输入 /provider 后正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/provider\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_approve_command(self) -> None:
        """管道输入 /approve 后正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/approve\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_rollback_no_arg(self) -> None:
        """管道输入 /rollback（无参数）正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/rollback\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_undo_no_ops(self) -> None:
        """管道输入 /undo（无操作可撤销）正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/undo\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_export_command(self) -> None:
        """管道输入 /export markdown 后 /quit 正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/export markdown\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_clear_command(self) -> None:
        """管道输入 /clear 后 /quit 正常退出。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/clear\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0

    def test_pipe_unknown_command(self) -> None:
        """管道输入 /unknown 后 /quit 输出包含"未知命令"。"""
        env = _env_with_api_key(_DUMMY_API_KEY)
        result = subprocess.run(
            ["codepilot", "--no-approve"],
            input="/unknown\n/quit\n",
            capture_output=True,
            text=True,
            timeout=E2E_TIMEOUT,
            env=env,
        )
        assert result.returncode == 0
        assert "未知命令" in result.stdout
