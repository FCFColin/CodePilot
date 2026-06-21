"""网页抓取工具 WebFetchTool。

抓取指定 URL 的网页内容，转为 Markdown 格式返回。
I/O 异常包装为 ToolError，由 execute 捕获并转为错误字符串。
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from markdownify import markdownify as md

from codepilot.tools.registry import ApprovalProtocol, BaseTool, SandboxProtocol

logger = structlog.get_logger(__name__)

# 最大内容大小（50KB）
_MAX_CONTENT_SIZE = 50 * 1024
# 请求超时（15秒）
_REQUEST_TIMEOUT = 15


class WebFetchTool(BaseTool):
    """抓取网页内容并转为 Markdown 格式。"""

    name = "web_fetch"
    description = (
        "抓取指定 URL 的网页内容，转为 Markdown 格式返回。"
        "适用于获取文档、API 说明、网页内容等。"
    )

    def get_parameters(self) -> dict[str, Any]:
        """返回参数 JSON Schema。"""
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": (
                        "要抓取的网页 URL（必须以 http:// 或 https:// 开头）"
                    ),
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    async def execute(
        self,
        arguments: dict[str, Any],
        sandbox: SandboxProtocol | None = None,
        approval: ApprovalProtocol | None = None,
    ) -> str:
        """执行网页抓取。

        Args:
            arguments: 工具参数，必须包含 url。
            sandbox: 可选沙箱校验器（URL 非文件路径，不使用）。
            approval: 可选审批器（只读操作，无需审批）。

        Returns:
            网页内容 Markdown 字符串；出错时返回 "Error: ..."。
        """
        url = arguments.get("url", "")
        if not url:
            return "Error: 缺少 url 参数"

        # URL 格式校验
        if not url.startswith(("http://", "https://")):
            return f"Error: URL 必须以 http:// 或 https:// 开头，收到: {url}"

        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT,
                follow_redirects=True,
                max_redirects=5,
            ) as client:
                response = await client.get(
                    url,
                    headers={"User-Agent": "CodePilot/0.2.0 (AI Coding Agent)"},
                )
                response.raise_for_status()

            # 检查 Content-Type
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type or "text/plain" in content_type:
                html = response.text
                # 转 Markdown
                result = md(html, heading_style="ATX", bullets="-")
            else:
                # 非 HTML 内容直接返回
                result = response.text

            # 截断
            if len(result) > _MAX_CONTENT_SIZE:
                result = result[:_MAX_CONTENT_SIZE] + "\n\n... (内容已截断，超过 50KB)"

            # 添加元信息
            status = response.status_code
            final_url = str(response.url)
            meta = (
                f"URL: {final_url}\n"
                f"Status: {status}\n"
                f"Content-Type: {content_type}\n"
                f"Size: {len(result)} chars\n\n---\n\n"
            )
            return meta + result

        except httpx.TimeoutException:
            return f"Error: 请求超时（{_REQUEST_TIMEOUT}秒），URL: {url}"
        except httpx.HTTPStatusError as e:
            return (
                f"Error: HTTP {e.response.status_code}"
                f" - {e.response.reason_phrase}, URL: {url}"
            )
        except httpx.InvalidURL:
            return f"Error: 无效的 URL: {url}"
        except Exception as e:
            return f"Error: 抓取失败 - {type(e).__name__}: {e}"


__all__ = ["WebFetchTool"]
