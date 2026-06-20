"""配置加载模块。

按优先级合并配置：命令行参数 > 环境变量 > 当前目录 .codepilot.yml > 用户目录 config.yml > 默认值。
支持 ${ENV_VAR} 形式的环境变量引用替换。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import yaml


# 环境变量引用正则：匹配 ${VAR_NAME} 形式
_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ============================================================================
# 配置数据结构定义
# ============================================================================

@dataclass
class ThinkingConfig:
    """DeepSeek 深度思考模式配置。"""
    enabled: bool = False
    reasoning_effort: str = "high"  # high | max


@dataclass
class DeepSeekConfig:
    """DeepSeek Provider 配置。"""
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    max_tokens: int = 8192
    temperature: float = 1.0
    top_p: float = 1.0
    stream: bool = True
    thinking: ThinkingConfig = field(default_factory=ThinkingConfig)


@dataclass
class AnthropicConfig:
    """Anthropic Claude Provider 配置。"""
    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 8192
    temperature: float = 0.7


@dataclass
class SecurityConfig:
    """安全配置。"""
    workspace_root: str = "."
    allowed_dirs: list[str] = field(default_factory=list)
    blocked_paths: list[str] = field(default_factory=lambda: [
        "/", "/etc", "/usr", "/var", "/sys", "/proc", "/boot", "/root", "~",
    ])
    command_blacklist: list[str] = field(default_factory=lambda: [
        "rm -rf /", "rm -rf ~", "rm -rf /*", "mkfs", "dd if=",
        ":(){:|:&};:", "chmod -R 777 /", "wget * | bash", "curl * | sh",
        "shutdown", "reboot", "init 0", "systemctl",
    ])
    command_whitelist_mode: bool = False
    command_whitelist: list[str] = field(default_factory=lambda: [
        "ls", "cat", "grep", "find", "echo", "python", "node", "npm",
        "pip", "git", "make", "cargo", "go",
    ])
    require_approval_for: list[str] = field(default_factory=lambda: [
        "file_write", "file_edit", "shell_exec",
    ])
    auto_approve_read: bool = True


@dataclass
class ContextConfig:
    """上下文管理配置。"""
    max_tokens: int = 120000
    compression_threshold: float = 0.70
    critical_threshold: float = 0.85
    preserve_recent_turns: int = 4
    preserve_system_prompt: bool = True
    compression_strategy: str = "summary"  # summary | truncate | hybrid
    save_full_history: bool = True
    history_file: str = ".codepilot_history.jsonl"


@dataclass
class UIConfig:
    """UI 显示配置。"""
    theme: str = "monokai"
    show_token_usage: bool = True
    show_cost_estimate: bool = True
    show_tool_calls: bool = True
    show_thinking: bool = True
    spinner_style: str = "dots"
    max_diff_lines: int = 50


@dataclass
class Config:
    """顶层配置结构。"""
    provider: str = "deepseek"
    deepseek: DeepSeekConfig = field(default_factory=DeepSeekConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    ui: UIConfig = field(default_factory=UIConfig)


# 程序内置默认配置常量
DEFAULT_CONFIG = Config()


# ============================================================================
# 配置加载实现
# ============================================================================

def _substitute_env_vars(value: Any) -> Any:
    """递归替换配置值中的 ${ENV_VAR} 引用。

    对字符串进行环境变量替换；对字典和列表递归处理；其他类型原样返回。
    若引用的环境变量未设置，则替换为空字符串。
    """
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    if isinstance(value, dict):
        return {k: _substitute_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env_vars(item) for item in value]
    return value


def _deep_update(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 中的值覆盖 base 中的同名键。

    对嵌套字典递归合并；非字典值直接覆盖。
    """
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_update(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_file(path: str) -> dict:
    """加载 YAML 配置文件并做环境变量替换。

    文件不存在或解析失败时返回空字典。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    try:
        with file_path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return _substitute_env_vars(raw)


def _dict_to_config(data: dict) -> Config:
    """将字典数据转换为 Config 数据类实例。

    仅识别已定义的字段，忽略未知字段。嵌套结构按字段类型递归构造。
    """
    config = Config()
    if "provider" in data and isinstance(data["provider"], str):
        config.provider = data["provider"]

    if "deepseek" in data and isinstance(data["deepseek"], dict):
        config.deepseek = _build_deepseek(data["deepseek"])

    if "anthropic" in data and isinstance(data["anthropic"], dict):
        config.anthropic = _build_anthropic(data["anthropic"])

    if "security" in data and isinstance(data["security"], dict):
        config.security = _build_security(data["security"])

    if "context" in data and isinstance(data["context"], dict):
        config.context = _build_context(data["context"])

    if "ui" in data and isinstance(data["ui"], dict):
        config.ui = _build_ui(data["ui"])

    return config


def _build_deepseek(data: dict) -> DeepSeekConfig:
    """从字典构造 DeepSeekConfig。"""
    cfg = DeepSeekConfig()
    if "api_key" in data and isinstance(data["api_key"], str):
        cfg.api_key = data["api_key"]
    if "base_url" in data and isinstance(data["base_url"], str):
        cfg.base_url = data["base_url"]
    if "model" in data and isinstance(data["model"], str):
        cfg.model = data["model"]
    if "max_tokens" in data and isinstance(data["max_tokens"], int):
        cfg.max_tokens = data["max_tokens"]
    if "temperature" in data and isinstance(data["temperature"], (int, float)):
        cfg.temperature = float(data["temperature"])
    if "top_p" in data and isinstance(data["top_p"], (int, float)):
        cfg.top_p = float(data["top_p"])
    if "stream" in data and isinstance(data["stream"], bool):
        cfg.stream = data["stream"]
    if "thinking" in data and isinstance(data["thinking"], dict):
        t = data["thinking"]
        if "enabled" in t and isinstance(t["enabled"], bool):
            cfg.thinking.enabled = t["enabled"]
        if "reasoning_effort" in t and isinstance(t["reasoning_effort"], str):
            cfg.thinking.reasoning_effort = t["reasoning_effort"]
    return cfg


def _build_anthropic(data: dict) -> AnthropicConfig:
    """从字典构造 AnthropicConfig。"""
    cfg = AnthropicConfig()
    if "api_key" in data and isinstance(data["api_key"], str):
        cfg.api_key = data["api_key"]
    if "base_url" in data and isinstance(data["base_url"], str):
        cfg.base_url = data["base_url"]
    if "model" in data and isinstance(data["model"], str):
        cfg.model = data["model"]
    if "max_tokens" in data and isinstance(data["max_tokens"], int):
        cfg.max_tokens = data["max_tokens"]
    if "temperature" in data and isinstance(data["temperature"], (int, float)):
        cfg.temperature = float(data["temperature"])
    return cfg


def _build_security(data: dict) -> SecurityConfig:
    """从字典构造 SecurityConfig。"""
    cfg = SecurityConfig()
    if "workspace_root" in data and isinstance(data["workspace_root"], str):
        cfg.workspace_root = data["workspace_root"]
    if "allowed_dirs" in data and isinstance(data["allowed_dirs"], list):
        cfg.allowed_dirs = [str(x) for x in data["allowed_dirs"]]
    if "blocked_paths" in data and isinstance(data["blocked_paths"], list):
        cfg.blocked_paths = [str(x) for x in data["blocked_paths"]]
    if "command_blacklist" in data and isinstance(data["command_blacklist"], list):
        cfg.command_blacklist = [str(x) for x in data["command_blacklist"]]
    if "command_whitelist_mode" in data and isinstance(data["command_whitelist_mode"], bool):
        cfg.command_whitelist_mode = data["command_whitelist_mode"]
    if "command_whitelist" in data and isinstance(data["command_whitelist"], list):
        cfg.command_whitelist = [str(x) for x in data["command_whitelist"]]
    if "require_approval_for" in data and isinstance(data["require_approval_for"], list):
        cfg.require_approval_for = [str(x) for x in data["require_approval_for"]]
    if "auto_approve_read" in data and isinstance(data["auto_approve_read"], bool):
        cfg.auto_approve_read = data["auto_approve_read"]
    return cfg


def _build_context(data: dict) -> ContextConfig:
    """从字典构造 ContextConfig。"""
    cfg = ContextConfig()
    if "max_tokens" in data and isinstance(data["max_tokens"], int):
        cfg.max_tokens = data["max_tokens"]
    if "compression_threshold" in data and isinstance(data["compression_threshold"], (int, float)):
        cfg.compression_threshold = float(data["compression_threshold"])
    if "critical_threshold" in data and isinstance(data["critical_threshold"], (int, float)):
        cfg.critical_threshold = float(data["critical_threshold"])
    if "preserve_recent_turns" in data and isinstance(data["preserve_recent_turns"], int):
        cfg.preserve_recent_turns = data["preserve_recent_turns"]
    if "preserve_system_prompt" in data and isinstance(data["preserve_system_prompt"], bool):
        cfg.preserve_system_prompt = data["preserve_system_prompt"]
    if "compression_strategy" in data and isinstance(data["compression_strategy"], str):
        cfg.compression_strategy = data["compression_strategy"]
    if "save_full_history" in data and isinstance(data["save_full_history"], bool):
        cfg.save_full_history = data["save_full_history"]
    if "history_file" in data and isinstance(data["history_file"], str):
        cfg.history_file = data["history_file"]
    return cfg


def _build_ui(data: dict) -> UIConfig:
    """从字典构造 UIConfig。"""
    cfg = UIConfig()
    if "theme" in data and isinstance(data["theme"], str):
        cfg.theme = data["theme"]
    if "show_token_usage" in data and isinstance(data["show_token_usage"], bool):
        cfg.show_token_usage = data["show_token_usage"]
    if "show_cost_estimate" in data and isinstance(data["show_cost_estimate"], bool):
        cfg.show_cost_estimate = data["show_cost_estimate"]
    if "show_tool_calls" in data and isinstance(data["show_tool_calls"], bool):
        cfg.show_tool_calls = data["show_tool_calls"]
    if "show_thinking" in data and isinstance(data["show_thinking"], bool):
        cfg.show_thinking = data["show_thinking"]
    if "spinner_style" in data and isinstance(data["spinner_style"], str):
        cfg.spinner_style = data["spinner_style"]
    if "max_diff_lines" in data and isinstance(data["max_diff_lines"], int):
        cfg.max_diff_lines = data["max_diff_lines"]
    return cfg


def _config_to_dict(config: Config) -> dict:
    """将 Config 数据类转为字典（用于深度合并）。"""
    return asdict(config)


def _apply_cli_args(config: Config, args: Any) -> Config:
    """应用命令行参数（最高优先级）。

    仅覆盖命令行中显式提供的值，None 值不覆盖。
    """
    provider = getattr(args, "provider", None)
    if provider:
        config.provider = provider

    model = getattr(args, "model", None)
    if model:
        if config.provider == "anthropic":
            config.anthropic.model = model
        else:
            config.deepseek.model = model

    api_key = getattr(args, "api_key", None)
    if api_key:
        if config.provider == "anthropic":
            config.anthropic.api_key = api_key
        else:
            config.deepseek.api_key = api_key

    workspace = getattr(args, "workspace", None)
    if workspace:
        config.security.workspace_root = workspace

    no_approve = getattr(args, "no_approve", False)
    if no_approve:
        # YOLO 模式：清空需审批列表
        config.security.require_approval_for = []

    return config


def _apply_env_vars(config: Config) -> Config:
    """应用环境变量覆盖（第二优先级）。

    CODEPILOT_PROVIDER 覆盖 provider；
    DEEPSEEK_API_KEY / ANTHROPIC_API_KEY 仅在对应 api_key 为空时填充。
    """
    env_provider = os.environ.get("CODEPILOT_PROVIDER")
    if env_provider and env_provider in ("deepseek", "anthropic"):
        config.provider = env_provider

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if deepseek_key and not config.deepseek.api_key:
        config.deepseek.api_key = deepseek_key

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key and not config.anthropic.api_key:
        config.anthropic.api_key = anthropic_key

    return config


def _resolve_workspace_root(config: Config) -> Config:
    """将 workspace_root 解析为绝对路径。

    使用 os.path.realpath() 解析，确保符号链接被展开。
    """
    raw = config.security.workspace_root or "."
    config.security.workspace_root = os.path.realpath(raw)
    return config


def load_config(args: Any = None) -> Config:
    """加载并合并配置。

    优先级从高到低：
      1. 命令行参数（args.provider/model/api_key/workspace/no_approve）
      2. 环境变量（CODEPILOT_PROVIDER, DEEPSEEK_API_KEY, ANTHROPIC_API_KEY）
      3. 当前目录 .codepilot.yml
      4. 用户目录 ~/.config/codepilot/config.yml
      5. 程序内置默认值（DEFAULT_CONFIG）

    args 可为 None（无命令行参数），此时跳过 CLI 覆盖。
    """
    # 从默认值开始
    merged_dict = _config_to_dict(DEFAULT_CONFIG)

    # 第4优先级：用户目录配置
    user_config_path = os.path.join(
        os.path.expanduser("~"), ".config", "codepilot", "config.yml"
    )
    user_data = _load_yaml_file(user_config_path)
    merged_dict = _deep_update(merged_dict, user_data)

    # 第3优先级：当前目录 .codepilot.yml（或 --config 指定路径）
    cli_config_path = getattr(args, "config", None) if args else None
    if cli_config_path:
        local_data = _load_yaml_file(cli_config_path)
    else:
        local_data = _load_yaml_file(".codepilot.yml")
    merged_dict = _deep_update(merged_dict, local_data)

    # 转为 Config 对象
    config = _dict_to_config(merged_dict)

    # 第2优先级：环境变量
    config = _apply_env_vars(config)

    # 第1优先级：命令行参数
    if args is not None:
        config = _apply_cli_args(config, args)

    # 解析 workspace_root 为绝对路径
    config = _resolve_workspace_root(config)

    return config
