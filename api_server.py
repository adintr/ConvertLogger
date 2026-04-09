#!/usr/bin/env python3
"""
Anthropic API代理服务器
实现HTTP服务器，接收请求并转发到不同的provider
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp
from aiohttp import web
import httpx

from config import load_config, Config, ProviderConfig, SchemeConfig


@dataclass
class RequestStatistics:
    """请求统计信息"""
    timestamp: float
    provider_name: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    response_time: float = 0.0
    status_code: int = 0
    success: bool = False
    error_message: str = ""


class StatisticsCollector:
    """统计收集器"""

    def __init__(self):
        self.total_requests = 0
        self.total_tokens = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.requests_by_provider: Dict[str, int] = {}
        self.requests_by_model: Dict[str, int] = {}
        self.recent_requests: List[RequestStatistics] = []
        self.max_recent_requests = 100

    def add_request(self, stats: RequestStatistics) -> None:
        """添加请求统计"""
        self.total_requests += 1
        self.total_tokens += stats.total_tokens

        if stats.success:
            self.successful_requests += 1
        else:
            self.failed_requests += 1

        # 按provider统计
        self.requests_by_provider[stats.provider_name] = \
            self.requests_by_provider.get(stats.provider_name, 0) + 1

        # 按model统计
        self.requests_by_model[stats.model] = \
            self.requests_by_model.get(stats.model, 0) + 1

        # 保存最近请求
        self.recent_requests.append(stats)
        if len(self.recent_requests) > self.max_recent_requests:
            self.recent_requests.pop(0)

    def get_summary(self) -> Dict[str, Any]:
        """获取统计摘要"""
        return {
            "total_requests": self.total_requests,
            "total_tokens": self.total_tokens,
            "successful_requests": self.successful_requests,
            "failed_requests": self.failed_requests,
            "success_rate": (self.successful_requests / self.total_requests * 100
                           if self.total_requests > 0 else 0),
            "providers": self.requests_by_provider,
            "models": self.requests_by_model,
            "recent_requests_count": len(self.recent_requests)
        }


class ProviderClient:
    """Provider客户端，负责与后端API通信"""

    def __init__(self, provider: ProviderConfig, config: Config):
        self.provider = provider
        self.config = config
        self.client: Optional[httpx.AsyncClient] = None
        self.last_used = 0.0
        self.request_count = 0
        self.error_count = 0

    async def initialize(self) -> None:
        """初始化HTTP客户端"""
        # 获取代理配置
        proxy_url = self.config.get_provider_proxy_url(self.provider)

        # 构建headers
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Anthropic-API-Proxy/1.0",
            **self.provider.get_auth_header()
        }

        # 添加provider特定headers
        if self.provider.headers:
            headers.update(self.provider.headers)

        # 如果是Azure OpenAI，添加api-version
        if self.provider.api_version:
            headers["api-version"] = self.provider.api_version

        # 创建HTTP客户端
        client_kwargs = {
            "headers": headers,
            "timeout": httpx.Timeout(self.provider.timeout),
        }

        # 添加代理配置
        if proxy_url:
            client_kwargs["proxies"] = proxy_url

        self.client = httpx.AsyncClient(**client_kwargs)

    async def close(self) -> None:
        """关闭HTTP客户端"""
        if self.client:
            await self.client.aclose()

    async def forward_request(self,
                             method: str,
                             path: str,
                             headers: Dict[str, str],
                             body: Optional[bytes] = None) -> Tuple[int, Dict[str, str], bytes, Dict[str, str]]:
        """
        转发请求到provider

        Returns:
            Tuple[int, Dict[str, str], bytes, Dict[str, str]]: (状态码, 响应头, 响应体, 实际发出的请求头)
        """
        if not self.client:
            await self.initialize()

        self.last_used = time.time()
        self.request_count += 1

        try:
            # 构建完整URL
            url = f"{self.provider.base_url.rstrip('/')}/{path.lstrip('/')}"

            # 构建实际发出的请求头（合并client默认headers + 传入headers）
            merged_headers = dict(self.client.headers)
            merged_headers.update(headers)

            # 转发请求
            response = await self.client.request(
                method=method,
                url=url,
                headers=headers,
                content=body
            )

            # 提取响应信息
            response_headers = dict(response.headers)
            response_body = response.content

            # 检查是否成功
            if response.status_code < 400:
                self.error_count = 0  # 重置错误计数
            else:
                self.error_count += 1

            return response.status_code, response_headers, response_body, merged_headers

        except Exception as e:
            self.error_count += 1
            logging.error(f"请求转发失败 {self.provider.name}: {e}")

            # 返回错误响应
            error_response = {
                "error": {
                    "type": "proxy_error",
                    "message": f"Failed to forward request to provider: {str(e)}"
                }
            }

            return 502, {"Content-Type": "application/json"}, json.dumps(error_response).encode(), {}

    def is_healthy(self) -> bool:
        """检查provider是否健康"""
        # 简单健康检查：最近错误次数过多则认为不健康
        return self.error_count < 5

    def get_weight(self) -> int:
        """获取当前权重（考虑健康状态）"""
        base_weight = self.provider.weight
        if not self.is_healthy():
            return max(1, base_weight // 2)  # 不健康时降低权重
        return base_weight


class APIServer:
    """API代理服务器"""

    class EnhancedStatisticsCollector(StatisticsCollector):
        """增强的统计收集器，支持事件广播"""

        def __init__(self, server_instance):
            super().__init__()
            self.server = server_instance
            self.last_broadcast_time = 0.0
            self.broadcast_interval = 1.0  # 广播间隔(秒)

        def add_request(self, stats: RequestStatistics):
            """添加请求统计，并检查是否需要广播"""
            super().add_request(stats)

            # 检查是否需要广播更新
            current_time = time.time()
            if current_time - self.last_broadcast_time >= self.broadcast_interval:
                asyncio.create_task(self._broadcast_update())
                self.last_broadcast_time = current_time

        async def _broadcast_update(self):
            """广播统计更新"""
            try:
                summary = self.get_summary()
                await self.server._broadcast_statistics_update()
            except Exception as e:
                logging.error(f"广播统计更新失败: {e}")

    def __init__(self, config: Config):
        self.config = config
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

        # 初始化组件
        self.statistics = self.EnhancedStatisticsCollector(self)
        self.provider_clients: Dict[str, ProviderClient] = {}

        # WebSocket支持
        self.websocket_clients = set()  # 连接的WebSocket客户端
        self.heartbeat_interval = 30.0  # 心跳间隔(秒)
        self.last_heartbeat_time = 0.0
        self.start_time = time.time()  # 服务器启动时间

        # 当前转发方案（None 表示使用配置文件中的 default_scheme）
        self.current_scheme_name: Optional[str] = None

        # 设置路由
        self.setup_routes()

    # hop-by-hop headers 不能原样转发，会导致 aiohttp 构造响应失败
    _HOP_BY_HOP_HEADERS = frozenset([
        "transfer-encoding", "connection", "keep-alive", "proxy-authenticate",
        "proxy-authorization", "te", "trailers", "upgrade",
        "content-encoding",   # httpx 已自动解压，body 是明文，不能再声明压缩
        "content-length",     # body 长度由 aiohttp 重新计算
    ])

    def _safe_response_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """过滤掉不能转发给客户端的 hop-by-hop headers"""
        return {k: v for k, v in headers.items() if k.lower() not in self._HOP_BY_HOP_HEADERS}

    def setup_routes(self) -> None:
        """设置HTTP路由"""
        # Anthropic API兼容端点
        self.app.router.add_route("*", "/v1/messages", self.handle_anthropic_request)
        self.app.router.add_route("*", "/v1/completions", self.handle_anthropic_request)

        # 通用代理端点
        self.app.router.add_route("*", "/{path:.*}", self.handle_proxy_request)

        # 监控端点
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/stats", self.handle_stats)
        self.app.router.add_get("/config", self.handle_config)

        # WebSocket端点
        self.app.router.add_get("/ws", self.handle_websocket)

    async def initialize(self) -> None:
        """初始化服务器"""
        # 创建provider客户端
        for provider_config in self.config.get_enabled_providers():
            client = ProviderClient(provider_config, self.config)
            await client.initialize()
            self.provider_clients[provider_config.name] = client

        logging.info(f"初始化了 {len(self.provider_clients)} 个provider客户端")

    def select_provider_by_request(self, request: web.Request) -> Optional[ProviderClient]:
        """
        根据请求选择provider

        优先级:
        1. X-Provider请求头
        2. provider查询参数
        3. 默认provider
        4. 第一个启用的provider
        """
        if not self.provider_clients:
            return None

        # 从请求头获取
        provider_name = request.headers.get("X-Provider")

        # 从查询参数获取
        if not provider_name:
            provider_name = request.query.get("provider")

        # 如果指定了provider名称，尝试获取
        if provider_name:
            provider_client = self.provider_clients.get(provider_name)
            if provider_client:
                return provider_client
            else:
                logging.warning(f"请求指定的provider不存在: {provider_name}")

        # 返回第一个可用的provider
        first_provider_name = next(iter(self.provider_clients.keys()), None)
        if first_provider_name:
            return self.provider_clients[first_provider_name]

        return None

    def get_current_scheme(self) -> Optional[SchemeConfig]:
        """获取当前生效的转发方案"""
        name = self.current_scheme_name
        if name:
            scheme = self.config.get_scheme_by_name(name)
            if scheme:
                return scheme
        return self.config.get_default_scheme()

    def select_provider_by_scheme(self, model_name: str) -> Tuple[Optional[ProviderClient], str]:
        """
        用当前方案匹配模型，返回 (provider_client, target_model)。
        若无方案或无匹配规则，回退到第一个可用 provider，target_model 不变。
        """
        target_model = model_name
        scheme = self.get_current_scheme()
        if scheme:
            rule = scheme.match(model_name)
            if rule:
                client = self.provider_clients.get(rule.provider)
                if client:
                    return client, rule.target_model
                else:
                    logging.warning(f"方案规则引用的 provider 不存在: {rule.provider}")

        # 无方案 / 无匹配 / provider 不存在 → 使用第一个可用 provider
        first = next(iter(self.provider_clients.values()), None)
        return first, target_model

    async def start(self) -> None:
        """启动服务器"""
        await self.initialize()

        # 创建app runner
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        # 启动TCP站点
        self.site = web.TCPSite(
            self.runner,
            host=self.config.server.host,
            port=self.config.server.port
        )

        await self.site.start()

        logging.info(f"API代理服务器启动在 http://{self.config.server.host}:{self.config.server.port}")

        # 启动心跳任务
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logging.info("心跳任务已启动")

    async def stop(self) -> None:
        """停止服务器"""
        # 关闭provider客户端
        for client in self.provider_clients.values():
            await client.close()

        # 关闭服务器
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()

        # 停止心跳任务
        if hasattr(self, '_heartbeat_task') and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            logging.info("心跳任务已停止")

        logging.info("API代理服务器已停止")

    async def handle_health(self, request: web.Request) -> web.Response:
        """健康检查端点"""
        health_status = {
            "status": "healthy",
            "timestamp": datetime.now().isoformat(),
            "providers": len(self.provider_clients),
            "requests": self.statistics.total_requests
        }

        return web.json_response(health_status)

    async def handle_stats(self, request: web.Request) -> web.Response:
        """统计信息端点"""
        stats = self.statistics.get_summary()

        # 添加provider详情
        provider_details = {}
        for name, client in self.provider_clients.items():
            provider_details[name] = {
                "request_count": client.request_count,
                "error_count": client.error_count,
                "healthy": client.is_healthy(),
                "weight": client.get_weight()
            }

        stats["providers_detail"] = provider_details

        return web.json_response(stats)

    async def handle_config(self, request: web.Request) -> web.Response:
        """配置信息端点"""
        config_summary = self.config.get_config_summary()
        return web.json_response(config_summary)

    @staticmethod
    def _build_sse_from_message(msg: dict) -> bytes:
        """
        将 Anthropic 非流式响应 JSON 转换为 SSE 事件流字节串。

        Anthropic streaming 事件顺序：
          message_start → content_block_start(×n) → ping →
          content_block_delta(×n) → content_block_stop(×n) →
          message_delta → message_stop
        """
        lines: list[str] = []

        def sse(event: str, data: dict) -> None:
            lines.append(f"event: {event}")
            lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
            lines.append("")  # 空行分隔

        msg_id      = msg.get("id", "")
        model_name  = msg.get("model", "")
        usage       = msg.get("usage", {})
        stop_reason = msg.get("stop_reason", "end_turn")
        content     = msg.get("content", [])

        # 1. message_start
        sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model_name,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": usage.get("input_tokens", 0), "output_tokens": 1},
            }
        })

        # 2. content_block_start + delta + stop 逐块
        for idx, block in enumerate(content):
            block_type = block.get("type", "text")

            sse("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": block_type, "text": ""},
            })
            sse("ping", {"type": "ping"})

            if block_type == "text":
                sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": block.get("text", "")},
                })
            elif block_type == "tool_use":
                # tool_use 块用 input_json_delta 传输 JSON 字符串
                sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })

            sse("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

        # 3. message_delta
        sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage.get("output_tokens", 0)},
        })

        # 4. message_stop
        sse("message_stop", {"type": "message_stop"})

        return "\n".join(lines).encode("utf-8")

    async def handle_anthropic_request(self, request: web.Request) -> web.Response:
        """处理Anthropic API请求"""
        start_time = time.time()

        try:
            # 读取请求体
            body = await request.read()

            # 解析JSON获取模型信息
            request_data = json.loads(body) if body else {}
            model = request_data.get("model", "unknown")

            # 检测原始请求是否为流式，并记录，用于响应时还原格式
            stream_value = request_data.get("stream")
            client_requested_stream = (
                stream_value is True
                or (isinstance(stream_value, str) and stream_value.lower() == "true")
            )

            # 用当前方案匹配 model，得到目标 provider 和目标 model
            provider_client, target_model = self.select_provider_by_scheme(model)
            if not provider_client:
                error_response = {
                    "error": {
                        "type": "no_provider",
                        "message": f"No available provider for model: {model}. "
                                  f"请配置 schemes 并确认 provider 存在。"
                    }
                }
                return web.json_response(error_response, status=503)

            # 替换 model 字段为方案中的目标模型
            if target_model != model:
                request_data = request_data.copy()
                request_data["model"] = target_model
                logging.info(f"方案路由: {model} -> {provider_client.provider.name}:{target_model}")

            # 如果客户端要求流式，把请求改为非流式再转发给 provider
            modified_body = json.dumps(request_data).encode() if target_model != model else body
            if client_requested_stream:
                modified_request_data = request_data.copy()
                modified_request_data["stream"] = False
                modified_body = json.dumps(modified_request_data).encode()
                logging.info(f"检测到流式请求，改为非流式转发 provider，响应将重新封装为 SSE (model: {target_model})")

            # 构建转发headers，移除客户端认证和连接相关headers
            # 认证header由ProviderClient初始化时配置，不应被客户端header覆盖
            headers_to_remove = [
                "host", "content-length", "connection",
                "authorization", "x-api-key", "api-key",
                "x-provider", "provider"
            ]
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in headers_to_remove
            }

            # 转发请求
            status_code, response_headers, response_body, forwarded_headers = await provider_client.forward_request(
                method=request.method,
                path=request.path,
                headers=headers,
                body=modified_body
            )

            # 若客户端原本请求流式，将 provider 的非流式 JSON 重新封装为 SSE 格式
            client_body = response_body
            client_headers = self._safe_response_headers(response_headers)
            if client_requested_stream and status_code < 400:
                try:
                    msg_json = json.loads(response_body)
                    client_body = self._build_sse_from_message(msg_json)
                    client_headers = dict(client_headers)
                    client_headers["content-type"] = "text/event-stream; charset=utf-8"
                    client_headers["cache-control"] = "no-cache"
                    client_headers["x-accel-buffering"] = "no"
                    logging.info(f"已将非流式响应重新封装为 SSE 格式，SSE 大小: {len(client_body)} 字节")
                except Exception as e:
                    logging.warning(f"SSE 封装失败，回退为原始响应: {e}")

            # 收集统计信息（含完整请求/响应数据）
            forwarded_url = f"{provider_client.provider.base_url.rstrip('/')}/{request.path.lstrip('/')}"
            incoming_url = str(request.url)
            self._collect_statistics(
                provider=provider_client.provider.name,
                model=model,
                method=request.method,
                incoming_url=incoming_url,
                request_data=request_data,
                incoming_headers=dict(request.headers),
                forwarded_headers=forwarded_headers,
                forwarded_url=forwarded_url,
                response_data=response_body,
                response_headers=response_headers,
                status_code=status_code,
                response_time=time.time() - start_time,
                client_response_headers=client_headers,
                client_response_data=client_body,
            )

            logging.info(
                f"发送响应给客户端 - 状态码: {status_code}, SSE重封装: {client_requested_stream}, "
                f"响应体大小: {len(client_body)} 字节, 路径: {request.path}, 模型: {model}"
            )

            return web.Response(
                status=status_code,
                headers=client_headers,
                body=client_body
            )

        except json.JSONDecodeError:
            error_response = {
                "error": {
                    "type": "invalid_json",
                    "message": "Invalid JSON in request body"
                }
            }
            logging.info(
                f"发送错误响应给客户端 (JSON解析错误) - 状态码: 400, "
                f"路径: {request.path}, 客户端: {request.remote}"
            )
            return web.json_response(error_response, status=400)

        except Exception as e:
            logging.error(f"处理请求失败: {e}")
            error_response = {
                "error": {
                    "type": "server_error",
                    "message": f"Internal server error: {str(e)}"
                }
            }
            logging.info(
                f"发送错误响应给客户端 (服务器错误) - 状态码: 500, "
                f"路径: {request.path}, 客户端: {request.remote}, 错误: {e}"
            )
            return web.json_response(error_response, status=500)

    async def handle_proxy_request(self, request: web.Request) -> web.Response:
        """通用代理请求处理"""
        start_time = time.time()

        try:
            # 读取请求体
            body = await request.read()

            # 检查并强制关闭流式响应
            modified_body = body
            try:
                if body:
                    request_data = json.loads(body)
                    stream_value = request_data.get("stream")
                    if stream_value is True or (isinstance(stream_value, str) and stream_value.lower() == "true"):
                        # 复制请求数据，将stream设置为false
                        modified_request_data = request_data.copy()
                        modified_request_data["stream"] = False
                        # 重新编码请求体
                        modified_body = json.dumps(modified_request_data).encode()
                        logging.info(f"检测到流式请求（通用代理），已强制关闭流式响应")
            except (json.JSONDecodeError, UnicodeDecodeError):
                # 非JSON请求，忽略
                pass

            # 选择provider（无特定模型）
            provider_client = self.select_provider_by_request(request)
            if not provider_client:
                error_response = {
                    "error": {
                        "type": "no_provider",
                        "message": "No available provider. "
                                  f"请通过X-Provider请求头或provider查询参数指定provider。"
                    }
                }
                return web.json_response(error_response, status=503)

            # 构建转发headers，移除客户端认证和连接相关headers
            headers_to_remove = [
                "host", "content-length", "connection",
                "authorization", "x-api-key", "api-key",
                "x-provider", "provider"
            ]
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in headers_to_remove
            }

            # 转发请求
            status_code, response_headers, response_body, forwarded_headers = await provider_client.forward_request(
                method=request.method,
                path=request.path_qs,
                headers=headers,
                body=modified_body
            )

            # 统计
            forwarded_url = f"{provider_client.provider.base_url.rstrip('/')}/{request.path_qs.lstrip('/')}"
            try:
                request_body = json.loads(body) if body else {}
            except Exception:
                request_body = body.decode("utf-8", errors="replace") if body else ""
            safe_headers = self._safe_response_headers(response_headers)
            self._collect_statistics(
                provider=provider_client.provider.name,
                model="unknown",
                method=request.method,
                incoming_url=str(request.url),
                request_data=request_body if isinstance(request_body, dict) else {},
                incoming_headers=dict(request.headers),
                forwarded_headers=forwarded_headers,
                forwarded_url=forwarded_url,
                response_data=response_body,
                response_headers=response_headers,
                status_code=status_code,
                response_time=time.time() - start_time,
                client_response_headers=safe_headers,
            )

            # 记录发送给客户端的响应
            logging.info(
                f"发送响应给客户端 (通用代理) - 状态码: {status_code}, "
                f"响应头数量: {len(safe_headers)}, "
                f"响应体大小: {len(response_body) if response_body else 0} 字节, "
                f"路径: {request.path}, 方法: {request.method}"
            )
            if len(response_body) < 1000:  # 只记录较小的响应体
                try:
                    response_text = response_body.decode('utf-8', errors='replace')
                    if response_text:
                        logging.debug(f"响应体内容: {response_text[:500]}")
                except Exception:
                    pass

            # 返回响应
            return web.Response(
                status=status_code,
                headers=safe_headers,
                body=response_body
            )

        except Exception as e:
            logging.error(f"代理请求失败: {e}")
            error_response = {
                "error": {
                    "type": "server_error",
                    "message": f"Internal server error: {str(e)}"
                }
            }
            logging.info(
                f"发送错误响应给客户端 (通用代理错误) - 状态码: 500, "
                f"路径: {request.path}, 客户端: {request.remote}, 错误: {e}"
            )
            return web.json_response(error_response, status=500)

    def _collect_statistics(self,
                           provider: str,
                           model: str,
                           method: str,
                           incoming_url: str,
                           request_data: Dict[str, Any],
                           incoming_headers: Dict[str, str],
                           forwarded_headers: Dict[str, str],
                           forwarded_url: str,
                           response_data: bytes,
                           response_headers: Dict[str, str],
                           status_code: int,
                           response_time: float,
                           client_response_headers: Optional[Dict[str, str]] = None,
                           client_response_data: Optional[bytes] = None) -> None:
        """收集请求统计信息并广播完整流量数据"""
        try:
            # 从响应中提取token数量
            prompt_tokens = 0
            completion_tokens = 0
            response_json = None

            if response_data:
                try:
                    response_json = json.loads(response_data)
                    if "usage" in response_json:
                        usage = response_json["usage"]
                        prompt_tokens = usage.get("input_tokens", 0)
                        completion_tokens = usage.get("output_tokens", 0)
                    elif "choices" in response_json and len(response_json["choices"]) > 0:
                        choice = response_json["choices"][0]
                        if "message" in choice:
                            message_text = str(choice["message"].get("content", ""))
                            completion_tokens = len(message_text) // 4
                except Exception:
                    pass

            # 从请求中估算（无法从响应获取时）
            if prompt_tokens == 0:
                if "messages" in request_data:
                    prompt_tokens = len(request_data["messages"]) * 10
                elif "prompt" in request_data:
                    prompt_tokens = len(str(request_data["prompt"])) // 4

            total_tokens = prompt_tokens + completion_tokens

            stats = RequestStatistics(
                timestamp=time.time() - response_time,
                provider_name=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                response_time=response_time,
                status_code=status_code,
                success=status_code < 400
            )

            self.statistics.add_request(stats)

            # 脱敏处理：隐藏认证相关header的值
            sensitive_keys = {"authorization", "x-api-key", "api-key"}

            def mask_headers(hdrs: Dict[str, str]) -> Dict[str, str]:
                return {
                    k: ("***" if k.lower() in sensitive_keys else v)
                    for k, v in hdrs.items()
                }

            # 广播完整流量数据
            try:
                log_data = {
                    "provider": provider,
                    "model": model,
                    "method": method,
                    "incoming_url": incoming_url,
                    "url": forwarded_url,
                    "status_code": status_code,
                    "success": status_code < 400,
                    "response_time": response_time,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                    "timestamp": time.time(),
                    # 客户端发来的原始请求
                    "incoming_headers": mask_headers(incoming_headers),
                    "request_body": request_data,
                    # 转发给 provider 的请求
                    "forwarded_headers": mask_headers(forwarded_headers),
                    # provider 返回的响应
                    "response_headers": dict(response_headers),
                    "response_body": response_json if response_json is not None else response_data.decode("utf-8", errors="replace"),
                    # 发送给客户端的响应（过滤后的 headers，SSE 重封装时内容不同于 provider 响应）
                    "client_response_headers": dict(client_response_headers) if client_response_headers else {},
                    "client_response_body": (
                        client_response_data.decode("utf-8", errors="replace")
                        if client_response_data is not None
                        else (response_json if response_json is not None else response_data.decode("utf-8", errors="replace"))
                    ),
                }
                asyncio.create_task(self._broadcast_request_log(log_data))
            except Exception as log_error:
                logging.warning(f"广播请求日志失败: {log_error}")

        except Exception as e:
            logging.warning(f"收集统计信息失败: {e}")

    # WebSocket处理方法
    async def handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """处理WebSocket连接"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # 注册客户端
        self.websocket_clients.add(ws)
        logging.info(f"WebSocket客户端已连接，当前客户端数: {len(self.websocket_clients)}")

        # 发送欢迎消息
        welcome_msg = {
            "type": "server_status",
            "timestamp": datetime.now().isoformat(),
            "data": {
                "status": "connected",
                "clients": len(self.websocket_clients),
                "server_time": datetime.now().isoformat()
            }
        }
        await ws.send_str(json.dumps(welcome_msg))

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_ws_message(ws, msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logging.error(f"WebSocket错误: {ws.exception()}")
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                 aiohttp.WSMsgType.CLOSING,
                                 aiohttp.WSMsgType.CLOSED):
                    logging.info("WebSocket连接关闭")
                    break
        finally:
            # 清理客户端
            self.websocket_clients.remove(ws)
            logging.info(f"WebSocket客户端已断开，剩余客户端数: {len(self.websocket_clients)}")

        return ws

    async def _handle_ws_message(self, ws: web.WebSocketResponse, raw_message: str):
        """处理WebSocket消息"""
        try:
            message = json.loads(raw_message)
            msg_type = message.get("type")
            msg_data = message.get("data", {})

            logging.debug(f"收到WebSocket消息: {msg_type}")

            if msg_type == "command":
                await self._handle_ws_command(ws, msg_data)
            elif msg_type == "ping":
                # 心跳响应
                await ws.send_str(json.dumps({
                    "type": "pong",
                    "timestamp": datetime.now().isoformat(),
                    "data": {"server_time": datetime.now().isoformat()}
                }))

        except json.JSONDecodeError:
            logging.warning(f"无效的JSON消息: {raw_message[:100]}")

    async def _handle_ws_command(self, ws: web.WebSocketResponse, data: dict):
        """处理WebSocket命令"""
        action = data.get("action")
        logging.info(f"处理WebSocket命令: {action}")
        logging.debug(f"命令数据: {data}")

        if action == "shutdown":
            # 优雅关闭命令
            await self._broadcast_ws_message("server_status", {
                "status": "shutting_down",
                "message": "服务器正在关闭"
            })
            # 在实际实现中，这里应该触发服务器关闭流程
            logging.info("收到关闭命令")

        elif action == "get_stats":
            # 获取统计信息
            stats = self.statistics.get_summary()
            await self._send_ws_message(ws, "statistics_update", stats)

        elif action == "get_config":
            # 获取配置信息
            config_summary = self.config.get_config_summary()
            await self._send_ws_message(ws, "config_info", config_summary)

        elif action == "set_scheme":
            # 切换当前转发方案
            scheme_name = data.get("scheme")
            if scheme_name:
                scheme = self.config.get_scheme_by_name(scheme_name)
                if scheme:
                    self.current_scheme_name = scheme_name
                    logging.info(f"切换转发方案为: {scheme_name}")
                    await self._send_ws_message(ws, "command_response", {
                        "action": "set_scheme",
                        "success": True,
                        "scheme": scheme_name,
                        "message": f"转发方案已切换为 {scheme_name}"
                    })
                else:
                    await self._send_ws_message(ws, "error", {
                        "message": f"方案不存在: {scheme_name}",
                        "available_schemes": [s.name for s in self.config.schemes]
                    })
            else:
                await self._send_ws_message(ws, "error", {
                    "message": "缺少 scheme 参数"
                })

        else:
            await self._send_ws_message(ws, "error", {
                "message": f"未知命令: {action}",
                "available_commands": ["shutdown", "get_stats", "get_config", "set_scheme"]
            })

    async def _send_ws_message(self, ws: web.WebSocketResponse, msg_type: str, data: dict):
        """发送消息到指定WebSocket客户端"""
        message = {
            "type": msg_type,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        try:
            await ws.send_str(json.dumps(message))
        except Exception as e:
            logging.error(f"发送WebSocket消息失败: {e}")

    async def _broadcast_ws_message(self, msg_type: str, data: dict):
        """广播消息到所有WebSocket客户端"""
        if not self.websocket_clients:
            return

        message = {
            "type": msg_type,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }
        message_json = json.dumps(message)

        disconnected_clients = []
        for ws in list(self.websocket_clients):
            try:
                await ws.send_str(message_json)
            except Exception as e:
                logging.error(f"广播消息失败，客户端可能已断开: {e}")
                disconnected_clients.append(ws)

        # 清理断开连接的客户端
        for ws in disconnected_clients:
            self.websocket_clients.remove(ws)

    async def _broadcast_statistics_update(self):
        """广播统计更新"""
        if not self.websocket_clients:
            return

        stats = self.statistics.get_summary()
        await self._broadcast_ws_message("statistics_update", stats)

    async def _broadcast_request_log(self, request_data: dict):
        """广播请求日志"""
        if not self.websocket_clients:
            return

        await self._broadcast_ws_message("request_log", request_data)

    async def _send_heartbeat(self):
        """发送心跳"""
        if not self.websocket_clients:
            return

        current_time = time.time()
        if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
            await self._broadcast_ws_message("server_status", {
                "status": "running",
                "heartbeat": current_time,
                "clients": len(self.websocket_clients),
                "requests": self.statistics.total_requests,
                "uptime": current_time - (self.start_time if hasattr(self, 'start_time') else current_time)
            })
            self.last_heartbeat_time = current_time

    async def _heartbeat_loop(self):
        """心跳循环"""
        try:
            while True:
                await asyncio.sleep(10)  # 每10秒检查一次
                await self._send_heartbeat()
        except asyncio.CancelledError:
            logging.info("心跳循环被取消")
        except Exception as e:
            logging.error(f"心跳循环异常: {e}")


async def run_server(config_path: str = "config.yaml") -> None:
    """运行API服务器"""
    # 加载配置
    config = load_config(config_path)

    # 验证配置
    errors = config.validate()
    if errors:
        print("配置错误:")
        for error in errors:
            print(f"  - {error}")
        return

    # 创建服务器
    server = APIServer(config)

    try:
        await server.start()

        # 保持运行
        print(f"服务器运行中...")
        print(f"访问 http://{config.server.host}:{config.server.port}/health 检查健康状态")
        print(f"访问 http://{config.server.host}:{config.server.port}/stats 查看统计")

        # 等待终止信号
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\n正在停止服务器...")
    finally:
        await server.stop()


if __name__ == "__main__":
    # 设置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # 运行服务器
    asyncio.run(run_server())