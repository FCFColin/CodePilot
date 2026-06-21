"""提交信息生成器。

提供两种生成策略：
- generate: 纯规则生成（不调用 LLM），根据 diff 摘要提取操作类型和文件名
- generate_from_llm: 调用 LLM provider 生成提交信息
"""

from __future__ import annotations

import re

import structlog

from codepilot.providers.base import BaseProvider

logger = structlog.get_logger(__name__)

# codepilot 提交信息前缀
_CODEPILOT_PREFIX = "[codepilot]"

# 操作类型关键词映射（按优先级排序）
_ACTION_KEYWORDS: list[tuple[str, str]] = [
    ("add", "add"),
    ("create", "create"),
    ("new", "add"),
    ("modify", "modify"),
    ("update", "update"),
    ("change", "modify"),
    ("delete", "delete"),
    ("remove", "delete"),
    ("fix", "fix"),
    ("refactor", "refactor"),
]

# 文件名提取正则：匹配常见文件名模式（含扩展名）
_FILE_PATTERN = re.compile(r"[\w\-./\\]+\.\w+")


class CommitMessageGenerator:
    """提交信息生成器。

    支持纯规则生成和 LLM 生成两种策略。
    生成的提交信息固定以 [codepilot] 前缀开头。
    """

    def generate(self, diff_summary: str, max_length: int = 72) -> str:
        """根据 diff 摘要纯规则生成提交信息。

        规则：
        1. 通过关键词识别操作类型（add/modify/delete/update/create 等）
        2. 提取 diff 摘要中的文件名
        3. 生成格式: "[codepilot] <action>: <file1>, <file2>"
        4. 超过 max_length 则截断并加 "..."

        Args:
            diff_summary: diff 摘要文本。
            max_length: 提交信息最大长度，默认 72。

        Returns:
            以 [codepilot] 开头的提交信息字符串。
        """
        summary_lower = diff_summary.lower()

        # 识别操作类型
        action = "update"  # 默认操作类型
        for keyword, mapped in _ACTION_KEYWORDS:
            if keyword in summary_lower:
                action = mapped
                break

        # 提取文件名
        files = _FILE_PATTERN.findall(diff_summary)
        # 去重并保留顺序
        seen: set[str] = set()
        unique_files: list[str] = []
        for f in files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        # 构造提交信息
        if unique_files:
            file_list = ", ".join(unique_files)
            message = f"{_CODEPILOT_PREFIX} {action}: {file_list}"
        else:
            # 无文件名时使用摘要本身（截断）
            message = f"{_CODEPILOT_PREFIX} {diff_summary.strip()}"

        # 截断到 max_length
        if len(message) > max_length:
            # 保留前缀和 "..."，截断中间内容
            truncate_len = max_length - 3  # 留 3 字符给 "..."
            message = message[:truncate_len] + "..."

        logger.debug("生成提交信息", message=message, length=len(message))
        return message

    async def generate_from_llm(self, provider: BaseProvider, diff_summary: str) -> str:
        """调用 LLM provider 生成提交信息。

        系统提示要求生成不超过 72 字符的 git 提交信息，
        前缀固定为 [codepilot]，禁止 Markdown 格式。

        Args:
            provider: LLM provider 实例。
            diff_summary: diff 摘要文本。

        Returns:
            以 [codepilot] 开头的提交信息字符串。
        """
        from codepilot.providers.base import Message

        system_prompt = (
            "You are a commit message generator. "
            "Generate a concise git commit message based on the given diff summary. "
            "Rules:\n"
            "1. The message MUST start with '[codepilot] ' prefix.\n"
            "2. The total message length MUST NOT exceed 72 characters.\n"
            "3. Do NOT use any Markdown formatting.\n"
            "4. Output ONLY the commit message, nothing else.\n"
            "5. Use English, describe the change concisely."
        )

        user_content = f"Diff summary:\n{diff_summary}\n\nGenerate a commit message:"

        messages: list[Message] = [
            Message(role="user", content=user_content),
        ]

        accumulated = ""
        try:
            async for event in provider.chat(
                messages,
                tools=None,
                system_prompt=system_prompt,
                stream=True,
            ):
                # 仅累积文本事件
                from codepilot.providers.base import TextDelta

                if isinstance(event, TextDelta):
                    accumulated += event.text
        except Exception as e:
            logger.warning("LLM 生成提交信息失败，回退到规则生成", error=str(e))
            return self.generate(diff_summary)

        # 清理输出：去除首尾空白和可能的 Markdown 标记
        message = accumulated.strip()
        # 若 LLM 未添加前缀则自动添加
        if not message.startswith(_CODEPILOT_PREFIX):
            message = f"{_CODEPILOT_PREFIX} {message}"

        # 截断到 72 字符
        if len(message) > 72:
            message = message[:69] + "..."

        logger.debug("LLM 生成提交信息", message=message, length=len(message))
        return message


__all__ = ["CommitMessageGenerator"]
