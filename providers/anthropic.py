"""
Anthropic API 类型处理

适用于 Anthropic 官方 API 及兼容 Anthropic 协议的上游服务（如 Ollama）。
"""
import json
import logging
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from config import ProviderConfig


def get_default_headers(provider: "ProviderConfig") -> Dict[str, str]:
    """返回 Anthropic API 所需的默认请求头"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Anthropic-API-Proxy/1.0",
        "anthropic-version": "2023-06-01",
        **provider.get_auth_header(),
    }
    if provider.headers:
        headers.update(provider.headers)
    return headers


async def list_models(provider: "ProviderConfig") -> List[str]:
    """查询 Anthropic 可用模型列表"""
    import httpx as _httpx
    url = f"{provider.base_url.rstrip('/')}/v1/models"
    headers = {
        "x-api-key": provider.api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        async with _httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            # {"data": [{"id": "claude-...", ...}, ...]}
            return [m["id"] for m in data.get("data", []) if "id" in m]
    except Exception as e:
        logging.error(f"Anthropic 查询模型列表失败 (provider={provider.name}): {e}")
        raise


async def forward_request(
    client: httpx.AsyncClient,
    provider: "ProviderConfig",
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes],
) -> Tuple[int, Dict[str, str], bytes, Dict[str, str]]:
    """
    转发请求到 Anthropic 兼容上游 API。

    Returns:
        (status_code, response_headers, response_body, merged_request_headers)
    """
    url = f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"

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
        logging.error(f"Anthropic 请求转发失败 {provider.name}: {e}")
        error_body = json.dumps({
            "error": {
                "type": "proxy_error",
                "message": f"Failed to forward request to Anthropic provider: {str(e)}",
            }
        }).encode()
        return 502, {"Content-Type": "application/json"}, error_body, {}
