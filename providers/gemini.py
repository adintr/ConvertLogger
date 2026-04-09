"""
Gemini API 类型处理

使用 google-genai SDK 调用 Gemini，并将 Anthropic 格式的请求/响应双向转换，
使上层代码对 provider 类型完全无感知。

请求转换（Anthropic → Gemini）：
  messages[].role: "user"/"assistant" → "user"/"model"
  messages[].content: str | list[block] → list[types.Part]
  system: str → config.system_instruction
  tool_use / tool_result blocks → FunctionCall / FunctionResponse parts
  tools[].input_schema → FunctionDeclaration.parameters_json_schema

响应转换（Gemini → Anthropic）：
  candidates[0].content.parts → content[]
  text part → {"type":"text","text":...}
  function_call part → {"type":"tool_use","id":...,"name":...,"input":...}
  usage_metadata → usage{input_tokens, output_tokens}
  finish_reason → stop_reason
"""
import json
import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from config import ProviderConfig

# google-genai SDK
from google import genai
from google.genai import types

# finish_reason 映射：Gemini → Anthropic
_FINISH_REASON_MAP = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "stop_sequence",
    "RECITATION": "stop_sequence",
    "OTHER": "end_turn",
}


# ── 格式转换工具函数 ───────────────────────────────────────────────────────────

def _anthropic_content_to_parts(content) -> List[types.Part]:
    """将单条 Anthropic message 的 content 字段转换为 Gemini Part 列表"""
    if isinstance(content, str):
        return [types.Part.from_text(text=content)]

    parts: List[types.Part] = []
    for block in content:
        btype = block.get("type", "")
        if btype == "text":
            parts.append(types.Part.from_text(text=block.get("text", "")))
        elif btype == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                import base64
                data = base64.b64decode(src["data"])
                parts.append(types.Part.from_bytes(data=data, mime_type=src.get("media_type", "image/jpeg")))
            elif src.get("type") == "url":
                parts.append(types.Part.from_uri(file_uri=src["url"], mime_type=src.get("media_type", "image/jpeg")))
        elif btype == "tool_use":
            parts.append(types.Part.from_function_call(
                name=block["name"],
                args=block.get("input", {}),
            ))
        elif btype == "tool_result":
            # tool_result 的 content 可能是字符串或列表
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_text = " ".join(
                    b.get("text", "") for b in result_content if b.get("type") == "text"
                )
            else:
                result_text = str(result_content)
            parts.append(types.Part.from_function_response(
                name=block.get("tool_use_id", "unknown"),
                response={"result": result_text},
            ))
    return parts


def _build_gemini_contents(messages: List[Dict]) -> List[types.Content]:
    """将 Anthropic messages 列表转换为 Gemini Content 列表"""
    contents: List[types.Content] = []
    for msg in messages:
        role = msg.get("role", "user")
        gemini_role = "model" if role == "assistant" else "user"
        parts = _anthropic_content_to_parts(msg.get("content", ""))
        if parts:
            contents.append(types.Content(role=gemini_role, parts=parts))
    return contents


def _build_tools(tools: List[Dict]) -> Optional[List[types.Tool]]:
    """将 Anthropic tools 列表转换为 Gemini Tool"""
    if not tools:
        return None
    declarations = []
    for tool in tools:
        declarations.append(types.FunctionDeclaration(
            name=tool["name"],
            description=tool.get("description", ""),
            parameters_json_schema=tool.get("input_schema"),
        ))
    return [types.Tool(function_declarations=declarations)]


def _gemini_parts_to_anthropic_content(parts) -> List[Dict]:
    """将 Gemini response Part 列表转换为 Anthropic content block 列表"""
    blocks = []
    for part in parts:
        if part.text is not None:
            blocks.append({"type": "text", "text": part.text})
        elif part.function_call is not None:
            fc = part.function_call
            blocks.append({
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:16]}",
                "name": fc.name,
                "input": dict(fc.args) if fc.args else {},
            })
    return blocks


def _gemini_response_to_anthropic(response, model: str, msg_id: str) -> Dict:
    """将 Gemini GenerateContentResponse 转换为 Anthropic Messages 响应格式"""
    candidate = response.candidates[0] if response.candidates else None

    # content blocks
    content = []
    if candidate and candidate.content and candidate.content.parts:
        content = _gemini_parts_to_anthropic_content(candidate.content.parts)

    # stop_reason
    stop_reason = "end_turn"
    if candidate and candidate.finish_reason:
        reason_name = candidate.finish_reason.name if hasattr(candidate.finish_reason, "name") else str(candidate.finish_reason)
        stop_reason = _FINISH_REASON_MAP.get(reason_name, "end_turn")

    # usage
    usage = response.usage_metadata
    input_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0

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
    """Gemini 使用 SDK 调用，不需要自定义 HTTP 请求头"""
    return {
        "Content-Type": "application/json",
        "User-Agent": "Anthropic-API-Proxy/1.0",
    }


async def forward_request(
    client: httpx.AsyncClient,
    provider: "ProviderConfig",
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes],
) -> Tuple[int, Dict[str, str], bytes, Dict[str, str]]:
    """
    将 Anthropic 格式请求转换后通过 google-genai SDK 调用 Gemini，
    再将响应转换回 Anthropic 格式返回。
    """
    if not body:
        error = {"error": {"type": "invalid_request", "message": "Empty request body"}}
        return 400, {"Content-Type": "application/json"}, json.dumps(error).encode(), {}

    try:
        request_data: Dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as e:
        error = {"error": {"type": "invalid_request", "message": f"Invalid JSON: {e}"}}
        return 400, {"Content-Type": "application/json"}, json.dumps(error).encode(), {}

    model = request_data.get("model", "gemini-2.0-flash")
    messages: List[Dict] = request_data.get("messages", [])
    system_text: Optional[str] = request_data.get("system")
    tools_raw: List[Dict] = request_data.get("tools", [])
    max_tokens: Optional[int] = request_data.get("max_tokens")
    temperature: Optional[float] = request_data.get("temperature")

    # 构建 SDK 参数
    contents = _build_gemini_contents(messages)
    tools = _build_tools(tools_raw)

    config_kwargs: Dict[str, Any] = {}
    if system_text:
        config_kwargs["system_instruction"] = system_text
    if max_tokens:
        config_kwargs["max_output_tokens"] = max_tokens
    if temperature is not None:
        config_kwargs["temperature"] = temperature
    if tools:
        config_kwargs["tools"] = tools
    gen_config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None

    # 初始化 Gemini 客户端（每次使用 provider 的 api_key）
    gemini_client = genai.Client(api_key=provider.api_key)

    msg_id = f"msg_{uuid.uuid4().hex[:24]}"

    try:
        if gen_config:
            response = await gemini_client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=gen_config,
            )
        else:
            response = await gemini_client.aio.models.generate_content(
                model=model,
                contents=contents,
            )

        anthropic_response = _gemini_response_to_anthropic(response, model, msg_id)
        response_body = json.dumps(anthropic_response, ensure_ascii=False).encode()
        response_headers = {"Content-Type": "application/json"}
        return 200, response_headers, response_body, {}

    except Exception as e:
        logging.error(f"Gemini 请求失败 (provider={provider.name}, model={model}): {e}")
        # 尽量从异常中提取 HTTP 状态码
        status = 502
        if hasattr(e, "status_code"):
            status = e.status_code
        elif hasattr(e, "code"):
            status = e.code
        error = {
            "error": {
                "type": "gemini_error",
                "message": str(e),
            }
        }
        return status, {"Content-Type": "application/json"}, json.dumps(error).encode(), {}
