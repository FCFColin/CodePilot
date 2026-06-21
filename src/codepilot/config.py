"""配置系统。

Phase 1 使用 Pydantic v2 BaseSettings 实现：
- 三级覆盖：命令行参数 > 环境变量 > .codepilot.yml > 默认值
- API Key 使用 SecretStr，不在日志/repr 中暴露明文
- 启动时 fail-fast 校验：缺少 API Key 立即抛出 ConfigError
- 支持 YAML 配置文件中的 ${ENV_VAR} 引用替换
- 使用 structlog 记录配置加载日志（API Key 不入日志）
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path
from typing import Any, Literal, cast

import structlog
import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from codepilot.exceptions import ConfigError

logger = structlog.get_logger(__name__)

# 环境变量引用正则：匹配 ${VAR_NAME} 形式
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 有效的 provider 值
_VALID_PROVIDERS = ("deepseek", "anthropic")


# ============================================================================
# 配置模型定义
# ============================================================================


class ThinkingConfig(BaseModel):
    """深度思考模式配置。"""

    enabled: bool = False
    reasoning_effort: str = "high"  # high | max


class ProviderConfig(BaseModel):
    """通用 Provider 配置（支持 OpenAI 兼容和 Anthropic 兼容端点）。"""

    type: Literal["openai", "anthropic"] = "openai"
    api_key: SecretStr = SecretStr("")
    base_url: str = ""
    model: str = ""
    max_tokens: int = 8192
    temperature: float = 0.7
    top_p: float = 1.0
    stream: bool = True
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)


class DeepSeekConfig(BaseModel):
    """DeepSeek Provider 配置（OpenAI 兼容端点）。"""

    api_key: SecretStr = SecretStr("")
    base_url: str = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
    model: str = "astron-code-latest"
    max_tokens: int = 8192
    temperature: float = 1.0
    top_p: float = 1.0
    stream: bool = True
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)


class AnthropicConfig(BaseModel):
    """Anthropic Provider 配置（Anthropic 兼容端点）。"""

    api_key: SecretStr = SecretStr("")
    base_url: str = "https://maas-coding-api.cn-huabei-1.xf-yun.com/anthropic"
    model: str = "astron-code-latest"
    max_tokens: int = 8192
    temperature: float = 0.7


class SecurityConfig(BaseModel):
    """安全配置。"""

    workspace_root: str = "."
    allowed_dirs: list[str] = Field(default_factory=list)
    blocked_paths: list[str] = Field(
        default_factory=lambda: [
            "/",
            "/etc",
            "/usr",
            "/var",
            "/sys",
            "/proc",
            "/boot",
            "/root",
            "~",
        ]
    )
    command_blacklist: list[str] = Field(
        default_factory=lambda: [
            "rm -rf /",
            "rm -rf ~",
            "rm -rf /*",
            "mkfs",
            "dd if=",
            ":(){:|:&};:",
            "chmod -R 777 /",
            "wget * | bash",
            "curl * | sh",
            "shutdown",
            "reboot",
            "init 0",
            "systemctl",
        ]
    )
    command_whitelist_mode: bool = False
    command_whitelist: list[str] = Field(
        default_factory=lambda: [
            "ls",
            "cat",
            "grep",
            "find",
            "echo",
            "python",
            "node",
            "npm",
            "pip",
            "git",
            "make",
            "cargo",
            "go",
        ]
    )
    require_approval_for: list[str] = Field(
        default_factory=lambda: ["file_write", "file_edit", "shell_exec"]
    )
    auto_approve_read: bool = True


class ContextConfig(BaseModel):
    """上下文管理配置。"""

    max_tokens: int = 120000
    compression_threshold: float = 0.70
    critical_threshold: float = 0.85
    preserve_recent_turns: int = 4
    preserve_system_prompt: bool = True
    compression_strategy: str = "summary"  # summary | truncate | hybrid
    save_full_history: bool = True
    history_file: str = ".codepilot_history.jsonl"


class UIConfig(BaseModel):
    """UI 显示配置。"""

    theme: str = "monokai"
    show_token_usage: bool = True
    show_cost_estimate: bool = True
    show_tool_calls: bool = True
    show_thinking: bool = True
    spinner_style: str = "dots"
    max_diff_lines: int = 50


class GitConfig(BaseModel):
    """Git 集成配置。"""

    auto_commit: bool = True
    commit_message_style: Literal["rules", "llm"] = "rules"


class HooksConfig(BaseModel):
    """Hooks 系统配置。

    控制内置 Hook 的启用与重试上限。
    """

    auto_lint: bool = True
    auto_git_commit: bool = True
    max_lint_retries: int = 3


class RepoMapConfig(BaseModel):
    """Repo Map 配置（可选功能）。

    控制仓库结构摘要的生成。需安装 repomap 可选依赖
    （tree-sitter-language-pack、networkx）才能实际生效；
    未安装时 RepoMapper.is_available() 返回 False，相关逻辑静默跳过。
    """

    enabled: bool = True
    max_tokens: int = 1024
    languages: list[str] = Field(default_factory=lambda: ["python"])


class Config(BaseSettings):
    """顶层配置结构。

    使用 Pydantic v2 BaseSettings 自动加载环境变量：
    - CODEPILOT_PROVIDER → provider
    - CODEPILOT_DEEPSEEK__API_KEY → deepseek.api_key（嵌套）
    - CODEPILOT_ANTHROPIC__API_KEY → anthropic.api_key（嵌套）
    - 其他 CODEPILOT_{SECTION}__{FIELD} 形式的环境变量

    注意：CODEPILOT_API_KEY 为便捷变量，需通过 load_config 手动应用。
    """

    model_config = SettingsConfigDict(
        env_prefix="CODEPILOT_",
        env_nested_delimiter="__",
        env_file=".codepilot.env",
        extra="ignore",
    )

    provider: str = "deepseek"
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    deepseek: DeepSeekConfig = Field(default_factory=DeepSeekConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    hooks: HooksConfig = Field(default_factory=HooksConfig)
    repomap: RepoMapConfig = Field(default_factory=RepoMapConfig)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        """验证 provider 值是否合法。

        如果 providers 非空，provider 必须是 providers 中的键；
        否则回退到旧逻辑（deepseek/anthropic 二选一）。
        注意：此验证器在 providers 字段赋值前执行，
        因此 providers 非空时的完整验证由 _validate_provider_after 改写。
        """
        if v not in _VALID_PROVIDERS:
            # 新 provider 名称可能在 providers 字典中，允许通过
            # 后续在 _validate_provider_after 中做完整校验
            pass
        return v


# ============================================================================
# 配置加载实现
# ============================================================================


def _substitute_env_vars(value: Any) -> Any:
    """递归替换配置值中的 ${ENV_VAR} 引用。

    对字符串进行环境变量替换；对字典和列表递归处理；其他类型原样返回。
    若引用的环境变量未设置，则替换为空字符串。
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


def _migrate_legacy_config(yaml_data: dict[str, Any]) -> dict[str, Any]:
    """将旧格式 deepseek:/anthropic: 配置迁移为 providers: 格式。

    如果 YAML 中有 deepseek: 或 anthropic: 但没有 providers:，
    自动转换为 providers: 格式，并发出 deprecation 警告。

    Args:
        yaml_data: 原始 YAML 配置字典。

    Returns:
        可能包含 providers: 段的配置字典。
    """
    if "providers" in yaml_data:
        return yaml_data

    has_legacy = "deepseek" in yaml_data or "anthropic" in yaml_data
    if not has_legacy:
        return yaml_data

    providers: dict[str, Any] = {}

    if "deepseek" in yaml_data:
        deepseek_data = dict(yaml_data["deepseek"])
        deepseek_data["type"] = "openai"
        # 旧 DeepSeekConfig 默认 temperature=1.0，迁移时保留
        if "temperature" not in deepseek_data:
            deepseek_data["temperature"] = 1.0
        providers["deepseek"] = deepseek_data
        logger.warning(
            "deepseek: 配置段已弃用，请迁移到 providers: 格式",
        )

    if "anthropic" in yaml_data:
        anthropic_data = dict(yaml_data["anthropic"])
        anthropic_data["type"] = "anthropic"
        providers["anthropic"] = anthropic_data
        logger.warning(
            "anthropic: 配置段已弃用，请迁移到 providers: 格式",
        )

    if providers:
        yaml_data = dict(yaml_data)
        yaml_data["providers"] = providers

    return yaml_data


def _load_yaml_config(path: str) -> dict[str, Any]:
    """加载 YAML 配置文件并做 ${ENV_VAR} 环境变量替换。

    文件不存在时返回空字典；解析失败记录日志并返回空字典。

    Args:
        path: YAML 文件路径。

    Returns:
        解析后的配置字典，文件不存在或解析失败时返回空字典。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        with file_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("加载 YAML 配置失败", path=path, error=str(e))
        return {}
    if not isinstance(raw, dict):
        logger.warning("YAML 配置根节点非字典", path=path)
        return {}
    return cast(dict[str, Any], _substitute_env_vars(raw))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并两个字典，override 中的值覆盖 base 中的同名键。

    对嵌套字典递归合并；非字典值直接覆盖。
    """
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _is_env_var_set(field_path: list[str]) -> bool:
    """检查给定字段路径对应的环境变量是否已设置。

    field_path 如 ["deepseek", "api_key"] 对应 CODEPILOT_DEEPSEEK__API_KEY。

    Args:
        field_path: 字段路径组件列表。

    Returns:
        对应环境变量已设置返回 True，否则 False。
    """
    env_var = "CODEPILOT_" + "__".join(p.upper() for p in field_path)
    return env_var in os.environ


def _merge_yaml_dict(
    base: dict[str, Any], yaml_data: dict[str, Any], path: list[str]
) -> None:
    """递归合并 YAML 数据到 base 字典，跳过环境变量已覆盖的字段。

    优先级：环境变量 > YAML > 默认值。
    对于每个 YAML 字段，若对应的环境变量已设置，则跳过（环境变量优先）。
    CODEPILOT_API_KEY 作为便捷变量，会覆盖当前 provider 的 api_key，
    因此也跳过 YAML 中的 api_key 字段。

    Args:
        base: 基础字典（会被原地修改）。
        yaml_data: YAML 配置字典。
        path: 当前字段路径组件列表。
    """
    for key, value in yaml_data.items():
        current_path = path + [key]
        # 检查对应环境变量是否已设置
        if _is_env_var_set(current_path):
            continue
        # 特殊处理：api_key 可被 CODEPILOT_API_KEY 便捷变量覆盖
        if key == "api_key" and "CODEPILOT_API_KEY" in os.environ:
            continue

        if isinstance(value, dict) and key in base and isinstance(base[key], dict):
            _merge_yaml_dict(base[key], value, current_path)
        else:
            base[key] = value


def _merge_yaml_into_config(config: Config, yaml_data: dict[str, Any]) -> Config:
    """将 YAML 数据合并到配置中，跳过环境变量已覆盖的字段。

    策略：将各 section 转为 dict，递归合并 YAML 值（跳过 env 已覆盖字段），
    然后用 model_validate 重建 section（section 是 BaseModel，不会加载 env）。
    最后用 model_copy 更新顶层 Config。

    Args:
        config: 当前配置（已包含环境变量和默认值）。
        yaml_data: YAML 配置字典。

    Returns:
        合并后的新 Config 对象。
    """
    if not yaml_data:
        return config

    updates: dict[str, Any] = {}

    # 顶层 provider
    if "provider" in yaml_data and not _is_env_var_set(["provider"]):
        updates["provider"] = yaml_data["provider"]

    # 嵌套 section
    section_names = [
        "deepseek",
        "anthropic",
        "security",
        "context",
        "ui",
        "git",
        "hooks",
        "repomap",
    ]
    for section_name in section_names:
        if section_name not in yaml_data:
            continue
        section_yaml = yaml_data[section_name]
        if not isinstance(section_yaml, dict):
            continue

        section_config = getattr(config, section_name)
        # 转为 dict 以便深度合并
        section_dict = section_config.model_dump()
        _merge_yaml_dict(section_dict, section_yaml, [section_name])
        # 重建 section（BaseModel.model_validate 不会加载环境变量）
        section_type = type(section_config)
        new_section = section_type.model_validate(section_dict)
        updates[section_name] = new_section

    # providers 段（新格式）
    if "providers" in yaml_data and isinstance(yaml_data["providers"], dict):
        providers_yaml = yaml_data["providers"]
        providers_updates: dict[str, ProviderConfig] = {}
        for prov_name, prov_data in providers_yaml.items():
            if not isinstance(prov_data, dict):
                continue
            if prov_name in config.providers:
                # 合并到已有 ProviderConfig
                existing = config.providers[prov_name]
                prov_dict = existing.model_dump()
                _merge_yaml_dict(prov_dict, prov_data, ["providers", prov_name])
                providers_updates[prov_name] = ProviderConfig.model_validate(prov_dict)
            else:
                # 新增 provider
                _merge_yaml_dict({}, prov_data, ["providers", prov_name])
                providers_updates[prov_name] = ProviderConfig.model_validate(prov_data)
        if providers_updates:
            merged_providers = dict(config.providers)
            merged_providers.update(providers_updates)
            updates["providers"] = merged_providers

    return config.model_copy(update=updates)


def _apply_codepilot_api_key(config: Config) -> Config:
    """应用 CODEPILOT_API_KEY 便捷环境变量。

    CODEPILOT_API_KEY 覆盖当前选中 provider 的 api_key。
    这是 BaseSettings 无法自动处理的便捷变量。

    Args:
        config: 当前配置。

    Returns:
        更新 api_key 后的新 Config 对象（若 CODEPILOT_API_KEY 未设置则原样返回）。
    """
    api_key = os.environ.get("CODEPILOT_API_KEY")
    if not api_key:
        return config

    provider = config.provider
    logger.debug("应用 CODEPILOT_API_KEY", provider=provider)

    updates: dict[str, Any] = {}

    # 新格式：providers 字典
    if config.providers and provider in config.providers:
        new_prov = config.providers[provider].model_copy(
            update={"api_key": SecretStr(api_key)}
        )
        new_providers = dict(config.providers)
        new_providers[provider] = new_prov
        updates["providers"] = new_providers

    # 旧格式：deepseek/anthropic 字段
    if provider == "deepseek":
        new_deepseek = config.deepseek.model_copy(
            update={"api_key": SecretStr(api_key)}
        )
        updates["deepseek"] = new_deepseek
    elif provider == "anthropic":
        new_anthropic = config.anthropic.model_copy(
            update={"api_key": SecretStr(api_key)}
        )
        updates["anthropic"] = new_anthropic

    if not updates:
        return config
    return config.model_copy(update=updates)


def _apply_cli_args(config: Config, args: argparse.Namespace) -> Config:
    """应用命令行参数（最高优先级）。

    仅覆盖命令行中显式提供的值，None 值不覆盖。

    Args:
        config: 当前配置。
        args: 命令行参数 Namespace。

    Returns:
        更新后的新 Config 对象。
    """
    updates: dict[str, Any] = {}

    provider = getattr(args, "provider", None)
    if provider:
        updates["provider"] = provider

    # 获取当前 provider（可能已被上面的 provider 覆盖）
    current_provider = updates.get("provider", config.provider)

    model = getattr(args, "model", None)
    if model:
        # 新格式：更新 providers 中的活跃 provider
        if config.providers and current_provider in config.providers:
            new_prov = config.providers[current_provider].model_copy(
                update={"model": model}
            )
            new_providers = dict(config.providers)
            new_providers[current_provider] = new_prov
            updates["providers"] = new_providers
        # 旧格式
        if current_provider == "anthropic":
            new_anthropic = config.anthropic.model_copy(update={"model": model})
            updates["anthropic"] = new_anthropic
        else:
            new_deepseek = config.deepseek.model_copy(update={"model": model})
            updates["deepseek"] = new_deepseek

    api_key = getattr(args, "api_key", None)
    if api_key:
        # 新格式：更新 providers 中的活跃 provider
        if config.providers and current_provider in config.providers:
            new_prov = config.providers[current_provider].model_copy(
                update={"api_key": SecretStr(api_key)}
            )
            new_providers = dict(config.providers)
            new_providers[current_provider] = new_prov
            updates["providers"] = new_providers
        # 旧格式
        if current_provider == "anthropic":
            new_anthropic = config.anthropic.model_copy(
                update={"api_key": SecretStr(api_key)}
            )
            updates["anthropic"] = new_anthropic
        else:
            new_deepseek = config.deepseek.model_copy(
                update={"api_key": SecretStr(api_key)}
            )
            updates["deepseek"] = new_deepseek

    workspace = getattr(args, "workspace", None)
    if workspace:
        new_security = config.security.model_copy(update={"workspace_root": workspace})
        updates["security"] = new_security

    no_approve = getattr(args, "no_approve", False)
    if no_approve:
        # YOLO 模式：清空需审批列表
        new_security = config.security.model_copy(update={"require_approval_for": []})
        updates["security"] = new_security

    no_auto_commit = getattr(args, "no_auto_commit", False)
    if no_auto_commit:
        new_git = config.git.model_copy(update={"auto_commit": False})
        updates["git"] = new_git

    return config.model_copy(update=updates)


def _resolve_workspace_root(config: Config) -> Config:
    """将 workspace_root 解析为绝对路径。

    使用 os.path.realpath() 解析，确保符号链接被展开。

    Args:
        config: 当前配置。

    Returns:
        workspace_root 已解析为绝对路径的新 Config 对象。
    """
    raw = config.security.workspace_root or "."
    resolved = os.path.realpath(raw)
    if resolved != config.security.workspace_root:
        new_security = config.security.model_copy(update={"workspace_root": resolved})
        return config.model_copy(update={"security": new_security})
    return config


def _load_yaml_config_from_paths(
    args: argparse.Namespace | None, config_path: str | None
) -> dict[str, Any]:
    """从多个路径加载并合并 YAML 配置。

    优先级（高到低）：
      1. --config 指定路径 或 当前目录 .codepilot.yml
      2. 用户目录 ~/.config/codepilot/config.yml

    Args:
        args: 命令行参数（用于读取 args.config）。
        config_path: 显式指定的配置文件路径。

    Returns:
        合并后的配置字典。
    """
    merged: dict[str, Any] = {}

    # 用户目录配置（最低优先级）
    user_config_path = os.path.join(
        os.path.expanduser("~"), ".config", "codepilot", "config.yml"
    )
    user_data = _load_yaml_config(user_config_path)
    merged = _deep_merge(merged, user_data)

    # 当前目录或 --config 指定路径（更高优先级）
    if config_path:
        local_data = _load_yaml_config(config_path)
        merged = _deep_merge(merged, local_data)
    elif args is not None and getattr(args, "config", None):
        local_data = _load_yaml_config(args.config)
        merged = _deep_merge(merged, local_data)
    else:
        local_data = _load_yaml_config(".codepilot.yml")
        merged = _deep_merge(merged, local_data)

    return merged


def load_config(
    args: argparse.Namespace | None = None,
    config_path: str | None = None,
) -> Config:
    """加载并合并配置。

    优先级从高到低：
      1. 命令行参数（args.provider/model/api_key/workspace/no_approve）
      2. 环境变量（CODEPILOT_API_KEY, CODEPILOT_PROVIDER,
         CODEPILOT_DEEPSEEK__*, CODEPILOT_ANTHROPIC__* 等）
      3. YAML 配置文件（--config > .codepilot.yml > ~/.config/codepilot/config.yml）
      4. 程序内置默认值

    Args:
        args: 命令行参数 Namespace，可为 None。
        config_path: 显式指定的配置文件路径，可为 None。

    Returns:
        合并后的 Config 对象。

    Raises:
        ConfigError: 当前 provider 缺少 API Key 时抛出。
    """
    logger.debug("开始加载配置")

    # 1. 加载 YAML 配置
    yaml_data = _load_yaml_config_from_paths(args, config_path)
    if yaml_data:
        logger.debug("已加载 YAML 配置")

    # 1.5 迁移旧格式配置
    yaml_data = _migrate_legacy_config(yaml_data)

    # 2. 创建 Config() - BaseSettings 自动加载环境变量
    config = Config()

    # 3. 合并 YAML 值（跳过环境变量已覆盖的字段）
    config = _merge_yaml_into_config(config, yaml_data)

    # 4. 应用 CODEPILOT_API_KEY 便捷变量
    config = _apply_codepilot_api_key(config)

    # 5. 应用命令行参数（最高优先级）
    if args is not None:
        config = _apply_cli_args(config, args)

    # 6. 解析 workspace_root 为绝对路径
    config = _resolve_workspace_root(config)

    # 6.5 验证 provider 名称（providers 非空时必须在字典中）
    if (
        config.providers
        and config.provider not in config.providers
        and config.provider not in _VALID_PROVIDERS
    ):
        raise ConfigError(
            f"无效的 provider: {config.provider}，"
            f"可选值: {', '.join(config.providers.keys())}"
        )

    # 7. fail-fast 验证
    validate_config(config)

    logger.debug("配置加载完成", provider=config.provider)
    return config


def validate_config(config: Config) -> None:
    """fail-fast 验证配置。

    检查当前 provider 对应的 api_key 是否非空，为空则抛出 ConfigError。

    Args:
        config: 待验证的配置对象。

    Raises:
        ConfigError: 当前 provider 缺少 API Key 或 provider 未知时抛出。
    """
    provider = config.provider

    # 新格式：providers 字典
    if config.providers and provider in config.providers:
        api_key = config.providers[provider].api_key.get_secret_value()
        if not api_key:
            raise ConfigError(
                f"Provider '{provider}' 缺少 API Key，"
                f"请设置 CODEPILOT_API_KEY 环境变量或在配置文件中配置"
            )
        return

    # 旧格式：deepseek/anthropic 字段
    if provider == "deepseek":
        api_key = config.deepseek.api_key.get_secret_value()
    elif provider == "anthropic":
        api_key = config.anthropic.api_key.get_secret_value()
    else:
        raise ConfigError(f"未知的 provider: {provider}")

    if not api_key:
        raise ConfigError(
            f"Provider '{provider}' 缺少 API Key，"
            f"请设置 CODEPILOT_API_KEY 环境变量或在配置文件中配置"
        )
