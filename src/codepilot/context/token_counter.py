"""Token 计数器。

使用 tiktoken 精确统计 token 数；tiktoken 不可用时回退到字符数估算。
提供文本、单条消息、消息列表的 token 计数能力，并带 LRU 缓存。
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    import tiktoken

    from codepilot.providers.base import Message

logger = structlog.get_logger(__name__)

# 缓存上限：避免内存爆炸
_CACHE_MAX_SIZE = 1000

# tiktoken 不可用时的字符→token 估算系数（经验值：英文约 4 字符/token，混合中文更高）
_FALLBACK_CHARS_PER_TOKEN = 3.5

# 单条消息 role 等元数据开销（OpenAI 经验值约 4 token）
_ROLE_OVERHEAD_TOKENS = 4


class TokenCounter:
    """Token 计数器。

    优先使用 tiktoken（cl100k_base 编码）精确计数；不可用时回退到字符数 / 3.5 估算。
    内置 LRU 缓存（最多 1000 条），缓存键为文本内容的 sha256 hash。
    """

    def __init__(self) -> None:
        self._encoder: tiktoken.Encoding | None = None
        try:
            import tiktoken

            self._encoder = tiktoken.get_encoding("cl100k_base")
            logger.debug("tiktoken 编码器已加载", encoding="cl100k_base")
        except Exception as exc:
            # tiktoken 未安装或编码不可用，回退到估算
            self._encoder = None
            logger.warning(
                "tiktoken 不可用，回退到字符数估算",
                error=str(exc),
            )
        # LRU 缓存：hash(text) -> token 数
        self._cache: OrderedDict[str, int] = OrderedDict()

    def count_text(self, text: str) -> int:
        """统计文本 token 数。

        tiktoken 可用时精确计数；否则用字符数 / 3.5 估算（向上取整）。
        结果带 LRU 缓存，缓存键为文本 sha256 hash。
        """
        if not isinstance(text, str):
            # 非 str 类型先转字符串
            text = str(text)
        if not text:
            return 0

        cache_key = self._cache_key(text)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if self._encoder is not None:
            try:
                count = len(self._encoder.encode(text))
            except Exception as exc:
                # 编码失败时回退到估算
                logger.debug(
                    "tiktoken 编码失败，回退到估算",
                    error=str(exc),
                    text_len=len(text),
                )
                count = self._estimate(text)
        else:
            count = self._estimate(text)

        self._cache_put(cache_key, count)
        return count

    def count_message(self, message: Message | dict[str, Any]) -> int:
        """统计单条消息 token 数（含 role 开销约 4 token）。

        message 可为 Message 对象或 dict。
        content 为 str 时直接计数；为 list 时对每个 block 的 text 字段计数求和。
        """
        # 提取 role 与 content
        if isinstance(message, dict):
            role = message.get("role", "")
            content = message.get("content", "")
        else:
            # Message dataclass 或其他对象
            role = getattr(message, "role", "")
            content = getattr(message, "content", "")

        total = _ROLE_OVERHEAD_TOKENS

        # role 本身也占少量 token
        if role:
            total += self.count_text(role)

        # content 为字符串
        if isinstance(content, str):
            total += self.count_text(content)
        elif isinstance(content, list):
            # Anthropic content blocks：[{"type": "text", "text": "..."}, ...]
            for block in content:
                total += self._count_block(block)
        elif content is None:
            pass
        else:
            # 其他类型转字符串计数
            total += self.count_text(str(content))

        return total

    def count_messages(self, messages: list[Message] | list[dict[str, Any]]) -> int:
        """统计消息列表总 token 数。"""
        total = 0
        for msg in messages:
            total += self.count_message(msg)
        return total

    def count_tokens(self, content: str | list[Any] | dict[str, Any]) -> int:
        """统计内容的 token 数，支持 str、list、dict 多种内容类型。

        - str: 直接计数文本
        - dict: 视为单条消息计数（含 role 开销）
        - list: 元素为消息 dict（含 role 键）时按消息计数，
          否则视为 content block 列表逐项计数
        """
        if isinstance(content, str):
            return self.count_text(content)
        if isinstance(content, dict):
            return self.count_message(content)
        if isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, str):
                    total += self.count_text(item)
                elif isinstance(item, dict):
                    # 含 role 键视为消息，否则视为 content block
                    if "role" in item:
                        total += self.count_message(item)
                    else:
                        total += self._count_block(item)
                elif isinstance(item, list):
                    total += self.count_tokens(item)
                else:
                    total += self.count_text(str(item))
            return total
        # 其他类型转字符串计数
        return self.count_text(str(content))

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(text: str) -> str:
        """生成缓存键：文本内容的 sha256 hash。"""
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

    @staticmethod
    def _estimate(text: str) -> int:
        """字符数估算 token 数（向上取整）。"""
        if not text:
            return 0
        return max(1, int(len(text) / _FALLBACK_CHARS_PER_TOKEN + 0.5))

    def _cache_get(self, key: str) -> int | None:
        """从 LRU 缓存读取，命中时移到末尾（最近使用）。"""
        if key not in self._cache:
            return None
        value = self._cache.pop(key)
        self._cache[key] = value
        return value

    def _cache_put(self, key: str, value: int) -> None:
        """写入 LRU 缓存，超过上限时淘汰最旧条目。"""
        if key in self._cache:
            # 已存在则先删除再插入，刷新顺序
            self._cache.pop(key)
        self._cache[key] = value
        # LRU 淘汰：超过上限时删除最旧（头部）
        while len(self._cache) > _CACHE_MAX_SIZE:
            self._cache.popitem(last=False)

    def _count_block(self, block: Any) -> int:
        """统计单个 content block 的 token 数。

        支持 Anthropic content blocks（dict 含 text 字段）和字符串。
        """
        if block is None:
            return 0
        if isinstance(block, str):
            return self.count_text(block)
        if isinstance(block, dict):
            # 优先取 text 字段；其次取 content 字段
            text = block.get("text") or block.get("content")
            if isinstance(text, str):
                return self.count_text(text)
            # 嵌套 list（如 tool_result 的 content）
            if isinstance(text, list):
                total = 0
                for sub in text:
                    total += self._count_block(sub)
                return total
            # 其他类型：取 type 字段做最小标记
            block_type = block.get("type", "")
            return self.count_text(str(block_type)) if block_type else 0
        # 其他类型转字符串
        return self.count_text(str(block))


__all__ = ["TokenCounter"]
