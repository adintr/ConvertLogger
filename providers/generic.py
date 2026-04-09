"""
通用 provider 类型处理

适用于任何标准 HTTP API，直接透传请求和响应。
当没有匹配的专用模块时作为兜底使用。
"""
import json
import logging
from typing import Dict, Optional, Tuple, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from config import ProviderConfig


def get_default_headers(provider: "ProviderConfig") -> Dict[str, str]:
    """返回通用默认请求头"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Anthropic-API-Proxy/1.0",
        **provider.get_auth_header(),
    }
    if provider.headers:
        headers.update(provider.headers)
    if provider.api_version:
        headers["api-version"] = provider.api_version
    return headers


async def forward_request(
    client: httpx.AsyncClient,
    provider: "ProviderConfig",
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes],
) -> Tuple[int, Dict[str, str], bytes, Dict[str, str]]:
    """
    直接透传请求到上游 API。

    Returns:
        (status_code, response_headers, response_body, merged_request_headers)
    """
    url = f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"

    # 合并 client 默认 headers + 传入 headers
    merged_headers = dict(client.headers)
    merged_headers.update(headers)

    try:
        response = await client.request(
            method=method,
            url=url,
            headers=headers,
            content=body,
        )
        return response.status_code, dict(response.headers), response.content, merged_headers

    except Exception as e:
        logging.error(f"请求转发失败 {provider.name}: {e}")
        error_body = json.dumps({
            "error": {
                "type": "proxy_error",
                "message": f"Failed to forward request to provider: {str(e)}",
            }
        }).encode()
        return 502, {"Content-Type": "application/json"}, error_body, {}
