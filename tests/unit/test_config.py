"""config 模块单元测试。

覆盖：默认值、SecretStr、环境变量覆盖、YAML 加载、${ENV_VAR} 替换、
优先级（CLI > env > YAML > 默认）、fail-fast、无效配置。
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from codepilot.config import (
    AnthropicConfig,
    Config,
    ContextConfig,
    DeepSeekConfig,
    _load_yaml_config,
    _substitute_env_vars,
    load_config,
    validate_config,
)
from codepilot.exceptions import ConfigError

# ============================================================================
# 辅助函数
# ============================================================================


def _make_args(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    workspace: str | None = None,
    no_approve: bool = False,
    config: str | None = None,
) -> argparse.Namespace:
    """构造命令行参数 Namespace 用于测试。"""
    return argparse.Namespace(
        prompt=None,
        provider=provider,
        model=model,
        api_key=api_key,
        workspace=workspace,
        no_approve=no_approve,
        config=config,
        verbose=False,
    )


def _clear_codepilot_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清除所有 CODEPILOT_ 前缀的环境变量，避免污染测试。"""
    for key in list(os.environ.keys()):
        if key.startswith("CODEPILOT_"):
            monkeypatch.delenv(key, raising=False)


def _mock_no_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟无 YAML 配置文件加载。"""
    monkeypatch.setattr("codepilot.config._load_yaml_config", lambda path: {})


# ============================================================================
# 测试类
# ============================================================================


class TestConfigModel:
    """配置模型默认值与类型测试。"""

    def test_default_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认配置值正确。"""
        _clear_codepilot_env(monkeypatch)
        config = Config()
        assert config.provider == "deepseek"
        assert (
            config.deepseek.base_url
            == "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
        )
        assert (
            config.anthropic.base_url
            == "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
        )
        assert config.deepseek.model == "astron-code-latest"
        assert config.anthropic.model == "astron-code-latest"

    def test_default_security_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """安全配置默认值正确。"""
        _clear_codepilot_env(monkeypatch)
        config = Config()
        assert config.security.workspace_root == "."
        assert config.security.auto_approve_read is True
        assert "/" in config.security.blocked_paths
        assert "rm -rf /" in config.security.command_blacklist
        assert "file_write" in config.security.require_approval_for

    def test_default_context_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上下文配置默认值正确。"""
        _clear_codepilot_env(monkeypatch)
        config = Config()
        assert config.context.max_tokens == 120000
        assert config.context.compression_threshold == 0.70
        assert config.context.critical_threshold == 0.85
        assert config.context.preserve_recent_turns == 4

    def test_api_key_is_secretstr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """API Key 使用 SecretStr 类型。"""
        _clear_codepilot_env(monkeypatch)
        config = Config()
        assert isinstance(config.deepseek.api_key, SecretStr)
        assert isinstance(config.anthropic.api_key, SecretStr)

    def test_secretstr_not_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SecretStr 不在 repr 中暴露明文。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            deepseek=DeepSeekConfig(api_key=SecretStr("sk-secret-key-12345"))
        )
        repr_str = repr(config)
        assert "sk-secret-key-12345" not in repr_str

    def test_secretstr_not_in_str(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SecretStr 不在 str() 中暴露明文。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            anthropic=AnthropicConfig(api_key=SecretStr("sk-ant-secret-999"))
        )
        str_str = str(config)
        assert "sk-ant-secret-999" not in str_str


class TestEnvVarOverride:
    """环境变量覆盖测试。"""

    def test_codepilot_provider_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CODEPILOT_PROVIDER 环境变量覆盖 provider。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_PROVIDER", "anthropic")
        config = Config()
        assert config.provider == "anthropic"

    def test_codepilot_deepseek_api_key_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_DEEPSEEK__API_KEY 嵌套环境变量覆盖。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_DEEPSEEK__API_KEY", "sk-deepseek-env")
        config = Config()
        assert config.deepseek.api_key.get_secret_value() == "sk-deepseek-env"

    def test_codepilot_anthropic_api_key_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_ANTHROPIC__API_KEY 嵌套环境变量覆盖。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_ANTHROPIC__API_KEY", "sk-anthropic-env")
        config = Config()
        assert config.anthropic.api_key.get_secret_value() == "sk-anthropic-env"

    def test_codepilot_api_key_convenience_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_API_KEY 便捷变量覆盖当前 provider 的 api_key。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-convenience")
        config = load_config()
        # 默认 provider 为 deepseek，CODEPILOT_API_KEY 应覆盖 deepseek.api_key
        assert config.deepseek.api_key.get_secret_value() == "sk-convenience"

    def test_codepilot_api_key_follows_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_API_KEY 跟随当前 provider 覆盖对应 api_key。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-follows")
        monkeypatch.setenv("CODEPILOT_PROVIDER", "anthropic")
        config = load_config()
        assert config.provider == "anthropic"
        assert config.anthropic.api_key.get_secret_value() == "sk-follows"
        # deepseek.api_key 不受影响
        assert config.deepseek.api_key.get_secret_value() == ""


class TestYamlLoading:
    """YAML 配置加载测试。"""

    def test_load_yaml_config(self, tmp_path: Any) -> None:
        """YAML 配置文件加载。"""
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: anthropic\n"
            "deepseek:\n"
            "  model: custom-model\n"
            "  max_tokens: 4096\n",
            encoding="utf-8",
        )
        data = _load_yaml_config(str(yaml_file))
        assert data["provider"] == "anthropic"
        assert data["deepseek"]["model"] == "custom-model"
        assert data["deepseek"]["max_tokens"] == 4096

    def test_load_yaml_config_not_found(self) -> None:
        """文件不存在时返回空字典。"""
        data = _load_yaml_config("/nonexistent/path/.codepilot.yml")
        assert data == {}

    def test_env_var_substitution_in_yaml(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """YAML 中 ${ENV_VAR} 替换。"""
        monkeypatch.setenv("MY_TEST_API_KEY", "sk-from-env-var")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            'deepseek:\n  api_key: "${MY_TEST_API_KEY}"\n',
            encoding="utf-8",
        )
        data = _load_yaml_config(str(yaml_file))
        assert data["deepseek"]["api_key"] == "sk-from-env-var"

    def test_env_var_substitution_unset(self) -> None:
        """未设置的环境变量替换为空字符串。"""
        result = _substitute_env_vars("${UNSET_VAR_FOR_TEST_12345}")
        assert result == ""

    def test_env_var_substitution_in_nested(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """嵌套结构中的 ${ENV_VAR} 替换。"""
        monkeypatch.setenv("TEST_MODEL", "gpt-test")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "deepseek:\n"
            '  model: "${TEST_MODEL}"\n'
            "  thinking:\n"
            '    reasoning_effort: "${TEST_MODEL}"\n',
            encoding="utf-8",
        )
        data = _load_yaml_config(str(yaml_file))
        assert data["deepseek"]["model"] == "gpt-test"
        assert data["deepseek"]["thinking"]["reasoning_effort"] == "gpt-test"

    def test_load_config_with_yaml(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_config 从 YAML 加载配置。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-test")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "deepseek:\n  model: yaml-model\n  max_tokens: 2048\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(yaml_file))
        assert config.deepseek.model == "yaml-model"
        assert config.deepseek.max_tokens == 2048


class TestPriority:
    """优先级测试：CLI > env > YAML > 默认。"""

    def test_cli_args_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI 参数覆盖环境变量。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_PROVIDER", "anthropic")
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-env")
        args = _make_args(provider="deepseek", api_key="sk-cli")
        config = load_config(args)
        assert config.provider == "deepseek"
        assert config.deepseek.api_key.get_secret_value() == "sk-cli"

    def test_env_override_yaml(self, tmp_path, monkeypatch):
        """环境变量覆盖 YAML。"""
        _clear_codepilot_env(monkeypatch)
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: deepseek\ndeepseek:\n  api_key: sk-yaml\n  model: yaml-model\n",
            encoding="utf-8",
        )
        # 环境变量覆盖 api_key
        monkeypatch.setenv("CODEPILOT_DEEPSEEK__API_KEY", "sk-env")
        config = load_config(config_path=str(yaml_file))
        assert config.deepseek.api_key.get_secret_value() == "sk-env"
        # YAML 的 model 应保留（环境变量未覆盖）
        assert config.deepseek.model == "yaml-model"

    def test_cli_args_override_yaml(self, tmp_path, monkeypatch):
        """CLI 参数覆盖 YAML。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-base")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "deepseek:\n  model: yaml-model\n",
            encoding="utf-8",
        )
        args = _make_args(model="cli-model")
        config = load_config(args, config_path=str(yaml_file))
        assert config.deepseek.model == "cli-model"

    def test_yaml_override_defaults(self, tmp_path, monkeypatch):
        """YAML 覆盖默认值。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-test")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "context:\n  max_tokens: 60000\n  preserve_recent_turns: 8\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(yaml_file))
        assert config.context.max_tokens == 60000
        assert config.context.preserve_recent_turns == 8

    def test_cli_no_approve_clears_approval_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--no-approve 清空需审批列表。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-test")
        args = _make_args(no_approve=True)
        config = load_config(args)
        assert config.security.require_approval_for == []

    def test_cli_workspace_overrides(self, tmp_path, monkeypatch):
        """--workspace 覆盖工作目录。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-test")
        _mock_no_yaml(monkeypatch)
        args = _make_args(workspace=str(tmp_path))
        config = load_config(args)
        assert config.security.workspace_root == os.path.realpath(str(tmp_path))


class TestFailFast:
    """fail-fast 验证测试。"""

    def test_missing_api_key_raises_config_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """缺少 API Key 时 fail-fast 抛出 ConfigError。"""
        _clear_codepilot_env(monkeypatch)
        config = Config()
        with pytest.raises(ConfigError) as exc_info:
            validate_config(config)
        assert "缺少 API Key" in str(exc_info.value)

    def test_with_api_key_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """有 API Key 时验证通过。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(deepseek=DeepSeekConfig(api_key=SecretStr("sk-test")))
        validate_config(config)  # 不抛异常

    def test_anthropic_missing_api_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """anthropic provider 缺少 API Key 时抛出 ConfigError。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(provider="anthropic")
        with pytest.raises(ConfigError):
            validate_config(config)

    def test_anthropic_with_api_key_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """anthropic provider 有 API Key 时验证通过。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            provider="anthropic",
            anthropic=AnthropicConfig(api_key=SecretStr("sk-ant-test")),
        )
        validate_config(config)  # 不抛异常

    def test_load_config_raises_on_missing_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_config 在缺少 API Key 时抛出 ConfigError。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        with pytest.raises(ConfigError):
            load_config()


class TestInvalidConfig:
    """无效配置测试。"""

    def test_invalid_provider_raises(self) -> None:
        """无效 provider 值触发 ValidationError。"""
        with pytest.raises(ValidationError):
            Config(provider="invalid")

    def test_invalid_temperature_type_raises(self) -> None:
        """无效 temperature 类型触发 ValidationError。"""
        with pytest.raises(ValidationError):
            DeepSeekConfig(temperature="not-a-number")  # type: ignore[arg-type]

    def test_invalid_max_tokens_type_raises(self) -> None:
        """无效 max_tokens 类型触发 ValidationError。"""
        with pytest.raises(ValidationError):
            ContextConfig(max_tokens="not-an-int")  # type: ignore[arg-type]
