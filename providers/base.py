"""
Provider 类型模块加载器及接口规范

====================================================================
如何新增一种上游 API 类型
====================================================================

1. 在 providers/ 目录下新建文件，文件名 = type 值（将 '-' 替换为 '_'）。
   例如：type = "gemini" → 文件名 providers/gemini.py

2. 文件中必须实现以下两个函数（函数签名不可改动）：

   ------------------------------------------------------------
   def get_default_headers(provider: ProviderConfig) -> Dict[str, str]:
   ------------------------------------------------------------
   返回发往上游时需要附加的默认 HTTP 请求头（认证、内容类型、版本等）。

   参数：
     provider  从 config.yaml 解析出的 ProviderConfig 对象，可读取：
               provider.api_key      API 密钥（已替换环境变量）
               provider.api_version  可选版本字符串
               provider.headers      配置中的自定义额外头
               provider.auth_type    "bearer" | "api_key"（由 get_auth_header() 封装）

   返回值：Dict[str, str] 请求头字典。

   ------------------------------------------------------------
   async def forward_request(
       client: httpx.AsyncClient,
       provider: ProviderConfig,
       method: str,
       path: str,
       headers: Dict[str, str],
       body: Optional[bytes],
   ) -> Tuple[int, Dict[str, str], bytes, Dict[str, str]]:
   ------------------------------------------------------------
   执行实际 HTTP 转发，返回四元组：
     (status_code, response_headers, response_body, merged_request_headers)

   参数：
     client    已由 ProviderClient 初始化的 httpx.AsyncClient，
               其默认 headers 已包含 get_default_headers() 的返回值。
     provider  同上。
     method    HTTP 方法字符串，如 "POST"。
     path      请求路径，如 "/v1/messages"（不含 query string）。
     headers   来自客户端的透传头（已去除认证相关字段）。
     body      原始请求体字节串，可能为 None。

   返回值：
     status_code          int，上游返回的 HTTP 状态码。
     response_headers     Dict[str, str]，上游返回的响应头。
     response_body        bytes，上游返回的响应体（已解压）。
     merged_request_headers  Dict[str, str]，实际发出的完整请求头（用于日志）。

   出错时应返回 (502, {"Content-Type": "application/json"}, error_json_bytes, {})
   而不是抛出异常，以保持调用方行为一致。

3. 无需注册：ProviderClient 将根据 provider.type 自动动态导入对应模块。
   若模块不存在，自动回退到 providers.generic（透传所有请求）。

====================================================================
快速模板（复制后修改标注 TODO 的部分）
====================================================================

    from providers.generic import forward_request  # 可复用通用实现
    from typing import Dict, TYPE_CHECKING
    if TYPE_CHECKING:
        from config import ProviderConfig

    def get_default_headers(provider: "ProviderConfig") -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Anthropic-API-Proxy/1.0",
            # TODO: 添加此 API 类型特有的头，如认证头、版本头等
            **provider.get_auth_header(),
        }
        if provider.headers:
            headers.update(provider.headers)
        return headers

    # 如果只需要自定义请求头，转发逻辑可以直接复用通用实现：
    # from providers.generic import forward_request  # noqa: F401

====================================================================
"""
import importlib
import logging
from typing import Dict

# 已加载模块缓存，避免重复导入
_module_cache: Dict[str, object] = {}


def load_provider_module(provider_type: str):
    """
    根据 provider type 动态加载对应模块。
    模块名称规则：providers.<type>（type 中的 '-' 替换为 '_'）。
    若对应模块不存在，回退到 providers.generic。
    """
    if provider_type in _module_cache:
        return _module_cache[provider_type]

    module_name = f"providers.{provider_type.replace('-', '_')}"
    try:
        module = importlib.import_module(module_name)
        _module_cache[provider_type] = module
        logging.debug(f"已加载 provider 模块: {module_name}")
        return module
    except ModuleNotFoundError:
        logging.warning(
            f"未找到 provider 类型模块 '{module_name}'，回退到 providers.generic"
        )
        if "generic" not in _module_cache:
            _module_cache["generic"] = importlib.import_module("providers.generic")
        _module_cache[provider_type] = _module_cache["generic"]
        return _module_cache["generic"]
