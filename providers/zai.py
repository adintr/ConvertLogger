"""
Z.AI API 类型处理

使用 zai-sdk 调用 Z.AI，并将 Anthropic 格式的请求/响应双向转换。
Z.AI 使用 OpenAI 兼容协议（/chat/completions 端点）。

请求转换（Anthropic → OpenAI）：
  messages[].role: 保持 "user"/"assistant" 不变
  messages[].content: list[block] → str 或 list[dict]（OpenAI 多模态格式）
  system: str → {"role": "system", "content": str} 消息前置
  tool_use / tool_result blocks → OpenAI tool_calls / tool 消息
  tools[].input_schema → function.parameters

响应转换（OpenAI → Anthropic）：
  choices[0].message.content → content[{"type":"text","text":...}]
  choices[0].message.tool_calls → content[{"type":"tool_use",...}]
  usage → usage{input_tokens, output_tokens}
  finish_reason → stop_reason
"""
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from config import ProviderConfig

# finish_reason 映射：OpenAI → Anthropic
_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "stop_sequence",
}


# ── 格式转换工具函数 ───────────────────────────────────────────────────────────

def _anthropic_content_to_openai(content) -> Any:
    """将单条 Anthropic message 的 content 字段转换为 OpenAI 格式"""
    if isinstance(content, str):
        return content

    # 收集 text 部分和 tool_use 部分
    text_parts = []
    tool_calls = []

    for block in content:
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "image":
            # OpenAI 多模态格式
            src = block.get("source", {})
            if src.get("type") == "base64":
                img_content = {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{src.get('media_type', 'image/jpeg')};base64,{src['data']}"
                    },
                }
                text_parts.append(img_content)  # 作为 content list 元素
            elif src.get("type") == "url":
                text_parts.append({
                    "type": "image_url",
                    "image_url": {"url": src["url"]},
                })
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:16]}"),
                "type": "function",
                "function": {
                    "name": block["name"],
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

    # 如果有 tool_calls，返回 (text, tool_calls) 供调用方处理
    # 简单情况：只有文本
    if not tool_calls:
        if len(text_parts) == 1 and isinstance(text_parts[0], str):
            return text_parts[0]
        elif text_parts:
            # 多模态 content list
            result = []
            for p in text_parts:
                if isinstance(p, str):
                    result.append({"type": "text", "text": p})
                else:
                    result.append(p)
            return result
        return ""

    # 有 tool_calls：返回特殊标记供 _build_openai_messages 处理
    return {"_text": " ".join(t for t in text_parts if isinstance(t, str)), "_tool_calls": tool_calls}


def _build_openai_messages(
    messages: List[Dict],
    system_text: Optional[str],
) -> List[Dict]:
    """将 Anthropic messages + system 转换为 OpenAI messages 列表"""
    result = []

    if system_text:
        result.append({"role": "system", "content": system_text})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # tool_result → OpenAI tool 角色消息
        if isinstance(content, list) and any(b.get("type") == "tool_result" for b in content):
            for block in content:
                if block.get("type") == "tool_result":
                    tool_content = block.get("content", "")
                    if isinstance(tool_content, list):
                        tool_text = " ".join(
                            b.get("text", "") for b in tool_content if b.get("type") == "text"
                        )
                    else:
                        tool_text = str(tool_content)
                    result.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": tool_text,
                    })
            continue

        converted = _anthropic_content_to_openai(content)

        if isinstance(converted, dict) and "_tool_calls" in converted:
            # assistant 消息带 tool_calls
            oai_msg: Dict[str, Any] = {"role": "assistant"}
            if converted["_text"]:
                oai_msg["content"] = converted["_text"]
            else:
                oai_msg["content"] = None
            oai_msg["tool_calls"] = converted["_tool_calls"]
            result.append(oai_msg)
        else:
            result.append({"role": role, "content": converted})

    return result


def _build_openai_tools(tools: List[Dict]) -> Optional[List[Dict]]:
    """将 Anthropic tools 转换为 OpenAI function tools 格式"""
    if not tools:
        return None
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


def _openai_response_to_anthropic(data: Dict, model: str, msg_id: str) -> Dict:
    """将 OpenAI chat completions 响应转换为 Anthropic Messages 响应格式"""
    choice = data.get("choices", [{}])[0]
    message = choice.get("message", {})

    content = []

    # 文本内容
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # tool_calls → tool_use blocks
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {})
        try:
            input_data = json.loads(fn.get("arguments", "{}"))
        except json.JSONDecodeError:
            input_data = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:16]}"),
            "name": fn.get("name", ""),
            "input": input_data,
        })

    # stop_reason
    finish_reason = choice.get("finish_reason", "stop")
    stop_reason = _FINISH_REASON_MAP.get(finish_reason, "end_turn")

    # usage
    usage = data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# ── Provider 接口实现 ──────────────────────────────────────────────────────────

def get_default_headers(provider: "ProviderConfig") -> Dict[str, str]:
    """返回 Z.AI API 所需的默认请求头"""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Anthropic-API-Proxy/1.0",
        **provider.get_auth_header(),
    }
    if provider.headers:
        headers.update(provider.headers)
    return headers


async def list_models(provider: "ProviderConfig") -> List[str]:
    """查询 Z.AI 可用模型列表"""
    base = (provider.base_url or "https://api.z.ai/api/paas/v4/").rstrip("/")
    url = f"{base}/models"
    auth_headers = provider.get_auth_header()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=auth_headers)
            resp.raise_for_status()
            data = resp.json()
            return [m["id"] for m in data.get("data", []) if "id" in m]
    except Exception as e:
        logging.error(f"Z.AI 查询模型列表失败 (provider={provider.name}): {e}")
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
    将 Anthropic 格式请求转换为 OpenAI 格式，发往 Z.AI，再转回 Anthropic 格式。

    Returns:
        (status_code, response_headers, response_body, merged_request_headers)
    """
    if not body:
        error = {"error": {"type": "invalid_request", "message": "Empty request body"}}
        return 400, {"Content-Type": "application/json"}, json.dumps(error).encode(), {}

    try:
        request_data: Dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as e:
        error = {"error": {"type": "invalid_request", "message": f"Invalid JSON: {e}"}}
        return 400, {"Content-Type": "application/json"}, json.dumps(error).encode(), {}

    model = request_data.get("model", "glm-5.1")
    messages: List[Dict] = request_data.get("messages", [])
    system_text: Optional[str] = request_data.get("system")
    tools_raw: List[Dict] = request_data.get("tools", [])
    max_tokens: Optional[int] = request_data.get("max_tokens")
    temperature: Optional[float] = request_data.get("temperature")

    # 构建 OpenAI 格式请求体
    oai_messages = _build_openai_messages(messages, system_text)
    oai_tools = _build_openai_tools(tools_raw)

    oai_body: Dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
    }
    if max_tokens:
        oai_body["max_tokens"] = max_tokens
    if temperature is not None:
        oai_body["temperature"] = temperature
    if oai_tools:
        oai_body["tools"] = oai_tools

    base = (provider.base_url or "https://api.z.ai/api/paas/v4/").rstrip("/")
    url = f"{base}/chat/completions"

    merged_headers = dict(client.headers)
    merged_headers.update(headers)

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    try:
        response = await client.post(
            url,
            content=json.dumps(oai_body, ensure_ascii=False).encode(),
        )

        if response.status_code != 200:
            # 原样返回错误体
            logging.error(
                f"Z.AI 上游错误 (provider={provider.name}, status={response.status_code}): "
                f"{response.text[:200]}"
            )
            return response.status_code, dict(response.headers), response.content, merged_headers

        oai_data = response.json()
        anthropic_response = _openai_response_to_anthropic(oai_data, model, msg_id)
        response_body = json.dumps(anthropic_response, ensure_ascii=False).encode()
        return 200, {"Content-Type": "application/json"}, response_body, merged_headers

    except Exception as e:
        logging.error(f"Z.AI 请求失败 (provider={provider.name}, model={model}): {e}")
        status = 502
        if hasattr(e, "status_code"):
            status = e.status_code
        error = {
            "error": {
                "type": "zai_error",
                "message": str(e),
            }
        }
        return status, {"Content-Type": "application/json"}, json.dumps(error).encode(), {}
