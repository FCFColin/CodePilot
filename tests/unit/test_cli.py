"""cli 模块单元测试。

覆盖：parse_args 参数解析（各参数）、main 入口函数
（--version、缺少 API Key、正常配置加载、REPL/单次模式）。
使用 mock 隔离 load_config 和 create_app，避免真实网络/IO。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from codepilot.cli import main, parse_args
from codepilot.config import Config, ProviderConfig
from codepilot.exceptions import CodePilotError, ConfigError

# ============================================================================
# 辅助函数
# ============================================================================


def _make_mock_config() -> Config:
    """构造测试用 Config（带 dummy API Key）。"""
    return Config(
        provider="deepseek",
        providers={
            "deepseek": ProviderConfig(
                type="openai",
                api_key=SecretStr("sk-test-dummy"),
                base_url="https://api.deepseek.com",
                model="deepseek-reasoner",
            ),
        },
    )


# ============================================================================
# TestParseArgs
# ============================================================================


class TestParseArgs:
    """parse_args 参数解析测试。"""

    def test_no_args(self) -> None:
        """无参数时 prompt 为 None。"""
        args = parse_args([])
        assert args.prompt is None

    def test_with_prompt(self) -> None:
        """位置参数 prompt 被正确解析。"""
        args = parse_args(["hello world"])
        assert args.prompt == "hello world"

    def test_provider_arg(self) -> None:
        """--provider 参数被正确解析。"""
        args = parse_args(["--provider", "anthropic"])
        assert args.provider == "anthropic"

    def test_provider_deepseek(self) -> None:
        """--provider deepseek 被正确解析。"""
        args = parse_args(["--provider", "deepseek"])
        assert args.provider == "deepseek"

    def test_provider_custom_name(self) -> None:
        """--provider 接受任意字符串。"""
        args = parse_args(["--provider", "my-custom-provider"])
        assert args.provider == "my-custom-provider"

    def test_model_arg(self) -> None:
        """--model 参数被正确解析。"""
        args = parse_args(["--model", "gpt-4"])
        assert args.model == "gpt-4"

    def test_api_key_arg(self) -> None:
        """--api-key 参数被正确解析。"""
        args = parse_args(["--api-key", "sk-secret"])
        assert args.api_key == "sk-secret"

    def test_workspace_arg(self) -> None:
        """--workspace 参数被正确解析。"""
        args = parse_args(["--workspace", "/tmp/test"])
        assert args.workspace == "/tmp/test"

    def test_no_approve_flag(self) -> None:
        """--no-approve 标志为 True。"""
        args = parse_args(["--no-approve"])
        assert args.no_approve is True

    def test_no_approve_default_false(self) -> None:
        """未指定 --no-approve 时默认为 False。"""
        args = parse_args([])
        assert args.no_approve is False

    def test_config_arg(self) -> None:
        """--config 参数被正确解析。"""
        args = parse_args(["--config", "/path/to/config.yml"])
        assert args.config == "/path/to/config.yml"

    def test_verbose_flag(self) -> None:
        """--verbose 标志为 True。"""
        args = parse_args(["--verbose"])
        assert args.verbose is True

    def test_verbose_default_false(self) -> None:
        """未指定 --verbose 时默认为 False。"""
        args = parse_args([])
        assert args.verbose is False

    def test_combined_args(self) -> None:
        """多个参数组合解析。"""
        args = parse_args(
            [
                "test prompt",
                "--provider",
                "anthropic",
                "--model",
                "claude-3",
                "--api-key",
                "sk-key",
                "--verbose",
            ]
        )
        assert args.prompt == "test prompt"
        assert args.provider == "anthropic"
        assert args.model == "claude-3"
        assert args.api_key == "sk-key"
        assert args.verbose is True

    def test_continue_flag(self) -> None:
        """-c 标志解析为 continue_last=True。"""
        args = parse_args(["-c"])
        assert args.continue_last is True

    def test_continue_long_flag(self) -> None:
        """--continue 标志解析为 continue_last=True。"""
        args = parse_args(["--continue"])
        assert args.continue_last is True

    def test_continue_default_false(self) -> None:
        """未指定 -c 时 continue_last 默认为 False。"""
        args = parse_args([])
        assert args.continue_last is False

    def test_resume_flag(self) -> None:
        """-r SESSION_ID 解析为 resume_session。"""
        args = parse_args(["-r", "abc12345-1700000000"])
        assert args.resume_session == "abc12345-1700000000"

    def test_resume_long_flag(self) -> None:
        """--resume SESSION_ID 解析为 resume_session。"""
        args = parse_args(["--resume", "session-xyz"])
        assert args.resume_session == "session-xyz"

    def test_resume_default_none(self) -> None:
        """未指定 -r 时 resume_session 默认为 None。"""
        args = parse_args([])
        assert args.resume_session is None


# ============================================================================
# TestMain
# ============================================================================


class TestMain:
    """main 入口函数测试。"""

    def test_version_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--version 打印版本号并以 exit code 0 退出。"""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--version"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "CodePilot" in captured.out

    def test_missing_api_key_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺少 API Key 时 ConfigError 导致 exit 1。"""

        def _raise_config_error(args: Any) -> Config:
            raise ConfigError("缺少 API Key")

        monkeypatch.setattr("codepilot.cli.load_config", _raise_config_error)
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1

    def test_create_app_error_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_app 抛出 CodePilotError 时 exit 1。"""
        mock_config = _make_mock_config()
        monkeypatch.setattr("codepilot.cli.load_config", lambda args: mock_config)

        def _raise_error(config: Any) -> Any:
            raise CodePilotError("初始化失败")

        monkeypatch.setattr("codepilot.cli.create_app", _raise_error)
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1

    def test_normal_startup_repl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常启动无 prompt 时调用 run_repl。"""
        mock_config = _make_mock_config()
        monkeypatch.setattr("codepilot.cli.load_config", lambda args: mock_config)
        mock_app = MagicMock()
        mock_app.run_repl = AsyncMock()
        mock_app.run_single = AsyncMock()
        monkeypatch.setattr("codepilot.cli.create_app", lambda config: mock_app)
        main([])
        mock_app.run_repl.assert_called_once()
        mock_app.run_single.assert_not_called()

    def test_normal_startup_single(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """正常启动有 prompt 时调用 run_single。"""
        mock_config = _make_mock_config()
        monkeypatch.setattr("codepilot.cli.load_config", lambda args: mock_config)
        mock_app = MagicMock()
        mock_app.run_repl = AsyncMock()
        mock_app.run_single = AsyncMock()
        monkeypatch.setattr("codepilot.cli.create_app", lambda config: mock_app)
        main(["test prompt"])
        mock_app.run_single.assert_called_once_with("test prompt")
        mock_app.run_repl.assert_not_called()

    def test_keyboard_interrupt_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """KeyboardInterrupt 时 exit 0。"""
        mock_config = _make_mock_config()
        monkeypatch.setattr("codepilot.cli.load_config", lambda args: mock_config)
        mock_app = MagicMock()
        mock_app.run_repl = AsyncMock(side_effect=KeyboardInterrupt())
        monkeypatch.setattr("codepilot.cli.create_app", lambda config: mock_app)
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 0

    def test_codepilot_error_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """运行时 CodePilotError 导致 exit 1。"""
        mock_config = _make_mock_config()
        monkeypatch.setattr("codepilot.cli.load_config", lambda args: mock_config)
        mock_app = MagicMock()
        mock_app.run_repl = AsyncMock(side_effect=CodePilotError("runtime error"))
        monkeypatch.setattr("codepilot.cli.create_app", lambda config: mock_app)
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
