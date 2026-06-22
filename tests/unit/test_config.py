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
    Config,
    ContextConfig,
    ProviderConfig,
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
        assert config.provider == "xunfei"
        assert "xunfei" in config.providers
        assert "deepseek" in config.providers
        assert (
            config.providers["xunfei"].base_url
            == "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
        )
        assert config.providers["deepseek"].base_url == "https://api.deepseek.com"
        assert config.providers["xunfei"].model == "astron-code-latest"
        assert config.providers["deepseek"].model == "deepseek-reasoner"

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
        assert isinstance(config.providers["xunfei"].api_key, SecretStr)
        assert isinstance(config.providers["deepseek"].api_key, SecretStr)

    def test_secretstr_not_in_repr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SecretStr 不在 repr 中暴露明文。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            providers={
                "test": ProviderConfig(api_key=SecretStr("sk-secret-key-12345")),
            }
        )
        repr_str = repr(config)
        assert "sk-secret-key-12345" not in repr_str

    def test_secretstr_not_in_str(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SecretStr 不在 str() 中暴露明文。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            providers={
                "test": ProviderConfig(api_key=SecretStr("sk-ant-secret-999")),
            }
        )
        str_str = str(config)
        assert "sk-ant-secret-999" not in str_str


class TestEnvVarOverride:
    """环境变量覆盖测试。"""

    def test_codepilot_provider_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CODEPILOT_PROVIDER 环境变量覆盖 provider。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_PROVIDER", "deepseek")
        config = Config()
        assert config.provider == "deepseek"

    def test_codepilot_api_key_convenience_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_API_KEY 便捷变量覆盖当前 provider 的 api_key。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-convenience")
        config = load_config()
        # 默认 provider 为 xunfei，CODEPILOT_API_KEY 应覆盖 xunfei 的 api_key
        assert config.providers["xunfei"].api_key.get_secret_value() == "sk-convenience"

    def test_codepilot_api_key_follows_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_API_KEY 跟随当前 provider 覆盖对应 api_key。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-follows")
        monkeypatch.setenv("CODEPILOT_PROVIDER", "deepseek")
        config = load_config()
        assert config.provider == "deepseek"
        assert config.providers["deepseek"].api_key.get_secret_value() == "sk-follows"
        # xunfei 的 api_key 不受影响
        assert config.providers["xunfei"].api_key.get_secret_value() == ""


class TestYamlLoading:
    """YAML 配置加载测试。"""

    def test_load_yaml_config(self, tmp_path: Any) -> None:
        """YAML 配置文件加载。"""
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: deepseek\n"
            "providers:\n"
            "  deepseek:\n"
            "    model: custom-model\n"
            "    max_tokens: 4096\n",
            encoding="utf-8",
        )
        data = _load_yaml_config(str(yaml_file))
        assert data["provider"] == "deepseek"
        assert data["providers"]["deepseek"]["model"] == "custom-model"
        assert data["providers"]["deepseek"]["max_tokens"] == 4096

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
            'providers:\n  xunfei:\n    api_key: "${MY_TEST_API_KEY}"\n',
            encoding="utf-8",
        )
        data = _load_yaml_config(str(yaml_file))
        assert data["providers"]["xunfei"]["api_key"] == "sk-from-env-var"

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
            "providers:\n"
            "  xunfei:\n"
            '    model: "${TEST_MODEL}"\n'
            "    thinking:\n"
            '      reasoning_effort: "${TEST_MODEL}"\n',
            encoding="utf-8",
        )
        data = _load_yaml_config(str(yaml_file))
        assert data["providers"]["xunfei"]["model"] == "gpt-test"
        assert data["providers"]["xunfei"]["thinking"]["reasoning_effort"] == "gpt-test"

    def test_load_config_with_yaml(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """load_config 从 YAML 加载配置。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-test")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: xunfei\nproviders:\n  xunfei:\n    model: yaml-model\n    max_tokens: 2048\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(yaml_file))
        assert config.providers["xunfei"].model == "yaml-model"
        assert config.providers["xunfei"].max_tokens == 2048


class TestPriority:
    """优先级测试：CLI > env > YAML > 默认。"""

    def test_cli_args_override_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLI 参数覆盖环境变量。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_PROVIDER", "deepseek")
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-env")
        args = _make_args(provider="xunfei", api_key="sk-cli")
        config = load_config(args)
        assert config.provider == "xunfei"
        assert config.providers["xunfei"].api_key.get_secret_value() == "sk-cli"

    def test_env_override_yaml(self, tmp_path, monkeypatch):
        """环境变量覆盖 YAML。"""
        _clear_codepilot_env(monkeypatch)
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: xunfei\nproviders:\n  xunfei:\n    api_key: sk-yaml\n    model: yaml-model\n",
            encoding="utf-8",
        )
        # 环境变量覆盖 api_key
        monkeypatch.setenv("CODEPILOT_PROVIDERS__XUNFEI__API_KEY", "sk-env")
        config = load_config(config_path=str(yaml_file))
        assert config.providers["xunfei"].api_key.get_secret_value() == "sk-env"
        # YAML 的 model 应保留（环境变量未覆盖）
        assert config.providers["xunfei"].model == "yaml-model"

    def test_cli_args_override_yaml(self, tmp_path, monkeypatch):
        """CLI 参数覆盖 YAML。"""
        _clear_codepilot_env(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-base")
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: xunfei\nproviders:\n  xunfei:\n    model: yaml-model\n",
            encoding="utf-8",
        )
        args = _make_args(model="cli-model")
        config = load_config(args, config_path=str(yaml_file))
        assert config.providers["xunfei"].model == "cli-model"

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
        config = Config(
            providers={
                "xunfei": ProviderConfig(api_key=SecretStr("sk-test")),
            }
        )
        validate_config(config)  # 不抛异常

    def test_deepseek_missing_api_key_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """deepseek provider 缺少 API Key 时抛出 ConfigError。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(provider="deepseek")
        with pytest.raises(ConfigError):
            validate_config(config)

    def test_deepseek_with_api_key_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """deepseek provider 有 API Key 时验证通过。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            provider="deepseek",
            providers={
                "deepseek": ProviderConfig(api_key=SecretStr("sk-ds-test")),
            },
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

    def test_invalid_provider_in_providers_dict(self) -> None:
        """providers 非空时 provider 必须是 providers 中的键。"""
        config = Config(
            provider="nonexistent",
            providers={
                "myprovider": ProviderConfig(
                    api_key=SecretStr("sk-test"),
                    base_url="https://example.com/v1",
                    model="test-model",
                )
            },
        )
        with pytest.raises(ConfigError):
            validate_config(config)

    def test_invalid_max_tokens_type_raises(self) -> None:
        """无效 max_tokens 类型触发 ValidationError。"""
        with pytest.raises(ValidationError):
            ContextConfig(max_tokens="not-an-int")  # type: ignore[arg-type]


class TestProviderConfig:
    """ProviderConfig 模型测试。"""

    def test_provider_config_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ProviderConfig 默认值正确。"""
        _clear_codepilot_env(monkeypatch)
        config = ProviderConfig()
        assert config.type == "openai"
        assert config.base_url == ""
        assert config.model == ""
        assert config.max_tokens == 8192
        assert config.temperature == 0.7
        assert config.top_p == 1.0
        assert config.stream is True

    def test_provider_config_anthropic_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ProviderConfig type=anthropic 可正常创建。"""
        _clear_codepilot_env(monkeypatch)
        config = ProviderConfig(
            type="anthropic",
            api_key=SecretStr("sk-test"),
            base_url="https://api.anthropic.com",
            model="claude-3-opus",
        )
        assert config.type == "anthropic"
        assert config.model == "claude-3-opus"

    def test_config_providers_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config.providers 字典可正常使用。"""
        _clear_codepilot_env(monkeypatch)
        config = Config(
            provider="myprovider",
            providers={
                "myprovider": ProviderConfig(
                    api_key=SecretStr("sk-test"),
                    base_url="https://example.com/v1",
                    model="test-model",
                )
            },
        )
        assert "myprovider" in config.providers
        assert config.providers["myprovider"].model == "test-model"


class TestMultiProviderConfig:
    """多 Provider 配置加载测试。"""

    def test_load_providers_from_yaml(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """从 YAML 加载 providers 段。"""
        _clear_codepilot_env(monkeypatch)
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: myprovider\n"
            "providers:\n"
            "  myprovider:\n"
            "    type: openai\n"
            "    api_key: sk-test\n"
            "    base_url: https://example.com/v1\n"
            "    model: test-model\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(yaml_file))
        assert config.provider == "myprovider"
        assert "myprovider" in config.providers
        assert config.providers["myprovider"].type == "openai"
        assert config.providers["myprovider"].model == "test-model"

    def test_load_multiple_providers(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """加载多个 provider 配置。"""
        _clear_codepilot_env(monkeypatch)
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: openai_provider\n"
            "providers:\n"
            "  openai_provider:\n"
            "    type: openai\n"
            "    api_key: sk-openai\n"
            "    base_url: https://api.openai.com/v1\n"
            "    model: gpt-4\n"
            "  anthropic_provider:\n"
            "    type: anthropic\n"
            "    api_key: sk-anthropic\n"
            "    base_url: https://api.anthropic.com\n"
            "    model: claude-3\n",
            encoding="utf-8",
        )
        config = load_config(config_path=str(yaml_file))
        assert "openai_provider" in config.providers
        assert "anthropic_provider" in config.providers
        assert config.providers["openai_provider"].type == "openai"
        assert config.providers["anthropic_provider"].type == "anthropic"

    def test_validate_provider_in_providers_dict(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """providers 非空时 provider 必须在字典中。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        # 直接构造 Config 并验证
        config = Config(
            provider="nonexistent",
            providers={
                "myprovider": ProviderConfig(
                    api_key=SecretStr("sk-test"),
                    base_url="https://example.com/v1",
                    model="test-model",
                )
            },
        )
        with pytest.raises(ConfigError):
            validate_config(config)

    def test_codepilot_api_key_with_providers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CODEPILOT_API_KEY 覆盖 providers 中的 api_key。"""
        _clear_codepilot_env(monkeypatch)
        _mock_no_yaml(monkeypatch)
        monkeypatch.setenv("CODEPILOT_API_KEY", "sk-convenience")
        config = load_config()
        # 默认 provider 为 xunfei，CODEPILOT_API_KEY 应覆盖
        assert config.providers["xunfei"].api_key.get_secret_value() == "sk-convenience"

    def test_cli_provider_switch(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI --provider 切换到 providers 中的 provider。"""
        _clear_codepilot_env(monkeypatch)
        yaml_file = tmp_path / ".codepilot.yml"
        yaml_file.write_text(
            "provider: provider_a\n"
            "providers:\n"
            "  provider_a:\n"
            "    type: openai\n"
            "    api_key: sk-a\n"
            "    base_url: https://a.example.com/v1\n"
            "    model: model-a\n"
            "  provider_b:\n"
            "    type: anthropic\n"
            "    api_key: sk-b\n"
            "    base_url: https://b.example.com\n"
            "    model: model-b\n",
            encoding="utf-8",
        )
        args = _make_args(provider="provider_b")
        config = load_config(args, config_path=str(yaml_file))
        assert config.provider == "provider_b"
        assert config.providers["provider_b"].model == "model-b"
