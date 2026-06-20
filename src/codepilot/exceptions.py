"""自定义异常体系。

所有 CodePilot 内部错误 SHALL 派生自 CodePilotError，
便于上层统一捕获与处理。禁止裸 except，禁止 except Exception pass。
"""


class CodePilotError(Exception):
    """所有 CodePilot 异常的基类。"""


class ConfigError(CodePilotError):
    """配置错误。"""


class ProviderError(CodePilotError):
    """Provider 调用错误。"""


class ToolError(CodePilotError):
    """工具执行错误。"""


class SecurityError(CodePilotError):
    """安全校验错误。"""
