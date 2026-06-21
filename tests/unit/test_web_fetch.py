"""web_fetch 工具测试。

覆盖正常抓取、URL 校验、超时、HTTP 错误等路径。
使用 unittest.mock 模拟 httpx 请求，不依赖真实网络。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from codepilot.tools.web_fetch import WebFetchTool


@pytest.fixture
def tool() -> WebFetchTool:
    return WebFetchTool()


class TestWebFetchTool:
    """WebFetchTool 测试：正常/边界/错误路径。"""

    def test_name_and_description(self, tool: WebFetchTool) -> None:
        assert tool.name == "web_fetch"
        assert "URL" in tool.description

    async def test_missing_url(self, tool: WebFetchTool) -> None:
        result = await tool.execute({})
        assert "Error" in result

    async def test_invalid_url_scheme(self, tool: WebFetchTool) -> None:
        result = await tool.execute({"url": "ftp://example.com"})
        assert "Error" in result
        assert "http://" in result

    async def test_successful_fetch(self, tool: WebFetchTool) -> None:
        mock_response = AsyncMock()
        mock_response.text = "<html><body><h1>Hello</h1><p>World</p></body></html>"
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.url = "https://example.com"
        mock_response.raise_for_status = lambda: None

        with patch("codepilot.tools.web_fetch.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client())
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_response)

            result = await tool.execute({"url": "https://example.com"})
            assert "Hello" in result
            assert "200" in result

    async def test_timeout(self, tool: WebFetchTool) -> None:
        with patch("codepilot.tools.web_fetch.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client())
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(
                side_effect=httpx.TimeoutException("timeout")
            )

            result = await tool.execute({"url": "https://example.com"})
            assert "Error" in result
            assert "超时" in result

    async def test_http_error(self, tool: WebFetchTool) -> None:
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"
        error = httpx.HTTPStatusError(
            "404", request=AsyncMock(), response=mock_response
        )

        with patch("codepilot.tools.web_fetch.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client())
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(side_effect=error)

            result = await tool.execute({"url": "https://example.com/notfound"})
            assert "Error" in result
            assert "404" in result

    async def test_content_type_non_html(self, tool: WebFetchTool) -> None:
        """非 HTML Content-Type 直接返回原文。"""
        mock_response = AsyncMock()
        mock_response.text = '{"key": "value"}'
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.url = "https://api.example.com/data"
        mock_response.raise_for_status = lambda: None

        with patch("codepilot.tools.web_fetch.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client())
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_response)

            result = await tool.execute({"url": "https://api.example.com/data"})
            assert '{"key": "value"}' in result

    async def test_truncation(self, tool: WebFetchTool) -> None:
        """超过 50KB 的内容被截断。"""
        long_content = "<html><body>" + "x" * (60 * 1024) + "</body></html>"
        mock_response = AsyncMock()
        mock_response.text = long_content
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}
        mock_response.url = "https://example.com/long"
        mock_response.raise_for_status = lambda: None

        with patch("codepilot.tools.web_fetch.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client())
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_response)

            result = await tool.execute({"url": "https://example.com/long"})
            assert "截断" in result
