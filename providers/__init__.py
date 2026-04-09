"""
上游 API 类型处理模块

每种 API 类型对应一个子模块，需实现以下接口：
  - get_default_headers(provider: ProviderConfig) -> Dict[str, str]
  - async forward_request(client, provider, method, path, headers, body) -> Tuple[int, Dict, bytes, Dict]
"""
