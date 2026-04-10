#!/usr/bin/env python3
"""
Anthropic API代理服务器
实现HTTP服务器，接收请求并转发到不同的provider
"""

import asyncio
import json
import logging
import sys
import time
import uuid
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp
from aiohttp import web
import httpx

from config import load_config, Config, ProviderConfig, SchemeConfig
from providers.base import load_provider_module


@dataclass
class PendingErrorRequest:
    """
    持有一个因上游错误而暂停的客户端请求。

    字段说明
    --------
    error_id         : 唯一标识符，也是 pending_errors 字典的键。
    provider         : 出错的 provider 名称。
    model            : 请求的模型名称。
    status_code      : 上游返回的 HTTP 状态码（网络错误时为 0）。
    error_type       : "http_error" | "network_error" | "api_error"
    error_message    : 错误详情文字。
    response_body    : 上游原始响应体（可能为空）。
    response_headers : 上游原始响应头（可能为空）。
    is_stream        : 客户端是否请求了 SSE 流式响应。
    timestamp        : 错误发生时间（unix timestamp）。
    decision         : 用户决策结果，初始为 None。
    fake_body        : 用户提供的伪造响应体（fake_response 决策时使用）。
    retry_headers    : 准备重试时发送给 provider 的 HTTP 头（用户可修改）。
    retry_body       : 准备重试时发送给 provider 的请求体（用户可修改）。
    retry_path       : 重试时转发的路径（含查询参数）。
    retry_method     : 重试时的 HTTP 方法。
    provider_client  : 用于重试的 ProviderClient 实例（不序列化，仅内存使用）。
    event            : asyncio.Event，用于通知等待协程用户已做出决策。
    """
    error_id: str
    provider: str
    model: str
    status_code: int
    error_type: str       # "http_error" | "network_error" | "api_error"
    error_message: str
    response_body: bytes
    response_headers: Dict[str, str]
    is_stream: bool
    timestamp: float
    decision: Optional[str] = None  # "return_error" | "fake_response" | "retry"
    fake_body: Optional[bytes] = None
    # 重试上下文（由 handle_*_request 在构造时填入，用户可通过 WS 命令修改）
    retry_headers: Dict[str, str] = field(default_factory=dict)
    retry_body: bytes = b""
    retry_path: str = ""
    retry_method: str = "POST"
    provider_client: Any = field(default=None, repr=False)
    event: asyncio.Event = field(default_factory=asyncio.Event)


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
        # 根据 provider.type 动态加载对应的处理模块
        self._type_module = load_provider_module(provider.type)

    async def initialize(self) -> None:
        """初始化HTTP客户端"""
        proxy_url = self.config.get_provider_proxy_url(self.provider)

        # 由各类型模块提供默认请求头
        headers = self._type_module.get_default_headers(self.provider)

        client_kwargs = {
            "headers": headers,
            "timeout": httpx.Timeout(self.provider.timeout),
        }

        if proxy_url:
            client_kwargs["proxy"] = proxy_url

        self.client = httpx.AsyncClient(**client_kwargs)
        logging.info(f"已初始化 provider '{self.provider.name}' (type={self.provider.type})")

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
        转发请求到provider，实际转发逻辑由各类型模块实现。

        Returns:
            Tuple[int, Dict[str, str], bytes, Dict[str, str]]: (状态码, 响应头, 响应体, 实际发出的请求头)
        """
        if not self.client:
            await self.initialize()

        self.last_used = time.time()
        self.request_count += 1

        status_code, response_headers, response_body, merged_headers = \
            await self._type_module.forward_request(
                self.client, self.provider, method, path, headers, body
            )

        if status_code < 400:
            self.error_count = 0
        else:
            self.error_count += 1

        return status_code, response_headers, response_body, merged_headers


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

        # 活跃客户端连接数（当前正在处理的请求数）
        self.active_clients = 0

        # 当前转发方案（None 表示使用配置文件中的 default_scheme）
        self.current_scheme_name: Optional[str] = None

        # 上游错误暂停队列：error_id -> PendingErrorRequest
        # 当上游出错时，请求协程在此等待用户决策后再继续
        self.pending_errors: Dict[str, PendingErrorRequest] = {}

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
        # 监控端点（必须在通配路由之前注册）
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/stats", self.handle_stats)
        self.app.router.add_get("/config", self.handle_config)

        # WebSocket端点（必须在通配路由之前注册）
        self.app.router.add_get("/ws", self.handle_websocket)

        # Anthropic API兼容端点
        self.app.router.add_route("*", "/v1/messages", self.handle_anthropic_request)
        self.app.router.add_route("*", "/v1/completions", self.handle_anthropic_request)

        # 通用代理端点（通配路由放最后）
        self.app.router.add_route("*", "/{path:.*}", self.handle_proxy_request)

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

        # 无显式指定时：优先使用当前方案第一条规则对应的 provider，
        # 确保 /v1/models 等非模型请求也走与方案一致的 provider，
        # 而不是取 dict 的任意第一个。
        scheme = self.get_current_scheme()
        if scheme and scheme.rules:
            scheme_provider = self.provider_clients.get(scheme.rules[0].provider)
            if scheme_provider:
                return scheme_provider

        # 最终兜底：字典第一个
        return next(iter(self.provider_clients.values()), None)

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

        # 无方案 / 无匹配 / provider 不存在 → 使用方案第一条规则的 provider，
        # 避免因 dict 插入顺序导致路由到意外的 provider。
        if scheme and scheme.rules:
            fallback = self.provider_clients.get(scheme.rules[0].provider)
            if fallback:
                return fallback, target_model

        # 最终兜底
        return next(iter(self.provider_clients.values()), None), target_model

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

    async def _update_provider_models(self, provider_name: str) -> Dict[str, Any]:
        """
        向指定 provider 查询可用模型列表，并将结果同步到内存配置和 config.yaml。

        Returns:
            {"success": True, "models": [...]} 或 {"success": False, "error": "..."}
        """
        client = self.provider_clients.get(provider_name)
        if not client:
            return {"success": False, "error": f"provider '{provider_name}' 不存在或未启用"}

        type_module = client._type_module
        if not hasattr(type_module, "list_models"):
            return {"success": False, "error": f"provider 类型 '{client.provider.type}' 不支持查询模型列表"}

        p = client.provider
        provider_type = p.type
        start_time = time.time()

        # 构建用于 traffic log 的请求信息
        if provider_type == "gemini":
            # Gemini 使用 SDK，无直接 HTTP URL
            request_url = f"[Gemini SDK] models.list (api_key=***)"
            request_headers: Dict[str, str] = {}
            request_body: Dict[str, Any] = {"sdk": "google-genai", "action": "models.list"}
        else:
            # anthropic / openai 兼容型
            request_url = f"{p.base_url.rstrip('/')}/v1/models"
            request_headers = {"x-api-key": "***", "anthropic-version": "2023-06-01"} \
                if provider_type == "anthropic" else {"Authorization": "Bearer ***"}
            request_body = {}

        trace_id = str(uuid.uuid4())

        # ── traffic_chunk 阶段1：内部发起（类比 client_request）──────────
        await self._broadcast_ws_message("traffic_chunk", {
            "trace_id": trace_id,
            "phase": "client_request",
            "provider": provider_name,
            "model": "list_models",
            "method": "GET",
            "incoming_url": f"[内部] update models {provider_name}",
            "incoming_headers": {},
            "request_body": request_body,
            "timestamp": time.time(),
        })

        # ── traffic_chunk 阶段2：转发给 provider ─────────────────────────
        await self._broadcast_ws_message("traffic_chunk", {
            "trace_id": trace_id,
            "phase": "forwarding",
            "provider": provider_name,
            "model": "list_models",
            "method": "GET",
            "url": request_url,
            "timestamp": time.time(),
        })

        try:
            models = await type_module.list_models(p)
            elapsed = time.time() - start_time
            status_code = 200
            error_msg = ""
        except (TimeoutError, asyncio.TimeoutError) as e:
            elapsed = time.time() - start_time
            status_code = 0
            error_msg = str(e) or f"请求超时（>{getattr(p, 'timeout', 60)}s）"
            # ── traffic_chunk 阶段3：超时错误 ─────────────────────────────
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "provider_response",
                "provider": provider_name,
                "status_code": 0,
                "response_time": elapsed,
                "response_headers": {},
                "response_body": {"error": f"网络超时: {error_msg}"},
                "timestamp": time.time(),
            })
            log_data = {
                "provider": provider_name,
                "model": "list_models",
                "method": "GET",
                "incoming_url": f"[内部] update models {provider_name}",
                "url": request_url,
                "status_code": 0,
                "success": False,
                "response_time": elapsed,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "timestamp": time.time(),
                "incoming_headers": {},
                "request_body": request_body,
                "forwarded_headers": request_headers,
                "response_headers": {},
                "response_body": {"error": f"网络超时: {error_msg}"},
                "client_response_headers": {},
                "client_response_body": {"error": f"网络超时: {error_msg}"},
                "error_message": f"网络超时: {error_msg}",
            }
            asyncio.create_task(self._broadcast_request_log(log_data))
            return {"success": False, "error": f"网络超时: {error_msg}"}
        except Exception as e:
            elapsed = time.time() - start_time
            status_code = 502
            error_msg = str(e)
            # ── traffic_chunk 阶段3：失败响应 ─────────────────────────────
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "provider_response",
                "provider": provider_name,
                "status_code": status_code,
                "response_time": elapsed,
                "response_headers": {},
                "response_body": {"error": error_msg},
                "timestamp": time.time(),
            })
            log_data = {
                "provider": provider_name,
                "model": "list_models",
                "method": "GET",
                "incoming_url": f"[内部] update models {provider_name}",
                "url": request_url,
                "status_code": status_code,
                "success": False,
                "response_time": elapsed,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "timestamp": time.time(),
                "incoming_headers": {},
                "request_body": request_body,
                "forwarded_headers": request_headers,
                "response_headers": {},
                "response_body": {"error": error_msg},
                "client_response_headers": {},
                "client_response_body": {"error": error_msg},
                "error_message": error_msg,
            }
            asyncio.create_task(self._broadcast_request_log(log_data))
            return {"success": False, "error": error_msg}

        # ── traffic_chunk 阶段3：成功响应 ─────────────────────────────────
        response_body_data: Any = {"models": models, "count": len(models)}
        await self._broadcast_ws_message("traffic_chunk", {
            "trace_id": trace_id,
            "phase": "provider_response",
            "provider": provider_name,
            "status_code": 200,
            "response_time": elapsed,
            "response_headers": {"content-type": "application/json"},
            "response_body": response_body_data,
            "timestamp": time.time(),
        })

        # 广播成功的 request_log（操作 Tab 摘要）
        log_data = {
            "provider": provider_name,
            "model": "list_models",
            "method": "GET",
            "incoming_url": f"[内部] update models {provider_name}",
            "url": request_url,
            "status_code": status_code,
            "success": True,
            "response_time": elapsed,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "timestamp": time.time(),
            "incoming_headers": {},
            "request_body": request_body,
            "forwarded_headers": request_headers,
            "response_headers": {"content-type": "application/json"},
            "response_body": response_body_data,
            "client_response_headers": {},
            "client_response_body": response_body_data,
        }
        asyncio.create_task(self._broadcast_request_log(log_data))

        if not models:
            return {"success": False, "error": "查询结果为空，未更新配置"}

        try:
            self.config.update_provider_models(provider_name, models)
        except Exception as e:
            return {"success": False, "error": f"写入配置失败: {e}"}

        logging.info(f"已更新 provider '{provider_name}' 模型列表: {models}")
        return {"success": True, "models": models}

    async def reload_config(self) -> Dict[str, Any]:
        """
        热重载配置文件中的 providers 和 schemes 部分。
        不停止监听端口，不断开任何已连接的客户端。
        返回重载结果摘要。
        """
        try:
            new_config = load_config(self.config.config_path)
        except Exception as e:
            logging.error(f"重载配置失败: {e}")
            return {"success": False, "error": str(e)}

        # ---- providers ----
        new_enabled = {p.name: p for p in new_config.get_enabled_providers()}
        old_names = set(self.provider_clients.keys())
        new_names = set(new_enabled.keys())

        # 关闭已删除/禁用的 provider 客户端
        removed = old_names - new_names
        for name in removed:
            await self.provider_clients[name].close()
            del self.provider_clients[name]
            logging.info(f"热重载: 移除 provider '{name}'")

        # 新增的 provider 客户端
        added = new_names - old_names
        for name in added:
            client = ProviderClient(new_enabled[name], new_config)
            await client.initialize()
            self.provider_clients[name] = client
            logging.info(f"热重载: 添加 provider '{name}'")

        # 更新已存在的 provider 配置（重建客户端以刷新认证/代理等）
        updated = old_names & new_names
        for name in updated:
            old_client = self.provider_clients[name]
            await old_client.close()
            client = ProviderClient(new_enabled[name], new_config)
            await client.initialize()
            # 保留统计计数
            client.request_count = old_client.request_count
            client.error_count = old_client.error_count
            self.provider_clients[name] = client
            logging.info(f"热重载: 更新 provider '{name}'")

        # ---- schemes ----
        new_config.providers = list(new_config.providers)  # 保持引用完整
        self.config.schemes = new_config.schemes
        self.config.default_scheme = new_config.default_scheme

        # 更新 config 中的 providers（供 select_provider_by_scheme 使用的 config.get_default_scheme）
        self.config.providers = new_config.providers
        self.config.raw_config = new_config.raw_config

        # 检查当前选中的方案是否仍存在
        if self.current_scheme_name:
            if not any(s.name == self.current_scheme_name for s in self.config.schemes):
                old_scheme = self.current_scheme_name
                self.current_scheme_name = None  # 回退到 default_scheme
                logging.warning(
                    f"热重载: 当前方案 '{old_scheme}' 不再存在，"
                    f"已回退到默认方案 '{self.config.default_scheme}'"
                )
                scheme_fallback = old_scheme
            else:
                scheme_fallback = None
        else:
            scheme_fallback = None

        result = {
            "success": True,
            "providers": {
                "added": list(added),
                "removed": list(removed),
                "updated": list(updated),
                "total": len(self.provider_clients),
            },
            "schemes": {
                "total": len(self.config.schemes),
                "default": self.config.default_scheme,
                "current": self.current_scheme_name,
            },
        }
        if scheme_fallback:
            result["scheme_fallback"] = scheme_fallback

        logging.info(f"热重载完成: {result}")
        return result

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

        # 客户端连接
        self.active_clients += 1
        client_ip = request.remote or "unknown"
        logging.info(f"客户端连接: {client_ip} [{request.method} {request.path}], 当前活跃连接数: {self.active_clients}")
        asyncio.create_task(self._broadcast_ws_message("client_connection", {
            "event": "connected",
            "client_ip": client_ip,
            "path": request.path,
            "method": request.method,
            "active_clients": self.active_clients,
        }))

        try:
            stream_resp: Optional[web.StreamResponse] = None  # 重试路径复用的响应通道
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

            # 转发请求（捕获网络级错误，统一封装为上游错误）
            forwarded_url = f"{provider_client.provider.base_url.rstrip('/')}/{request.path.lstrip('/')}"
            incoming_url = str(request.url)
            upstream_error: Optional[PendingErrorRequest] = None

            # 生成本次请求的唯一 trace_id，用于将各阶段 chunk 关联到同一条记录
            trace_id = str(uuid.uuid4())

            # ── 阶段1：收到客户端请求 ──────────────────────────────────
            sensitive_keys_set = {"authorization", "x-api-key", "api-key"}
            masked_incoming_headers = {
                k: ("***" if k.lower() in sensitive_keys_set else v)
                for k, v in request.headers.items()
            }
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "client_request",
                "provider": provider_client.provider.name,
                "model": model,
                "method": request.method,
                "incoming_url": incoming_url,
                "timestamp": time.time(),
                "incoming_headers": masked_incoming_headers,
                "request_body": request_data,
            })

            # ── 阶段2：向 Provider 转发请求 ────────────────────────────
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "forwarding",
                "provider": provider_client.provider.name,
                "model": target_model,
                "method": request.method,
                "url": forwarded_url,
                "timestamp": time.time(),
            })

            try:
                status_code, response_headers, response_body, forwarded_headers = \
                    await provider_client.forward_request(
                        method=request.method,
                        path=request.path,
                        headers=headers,
                        body=modified_body
                    )

                # ── 阶段3：收到 Provider 响应 ─────────────────────────
                try:
                    _resp_json_preview = json.loads(response_body)
                except Exception:
                    _resp_json_preview = response_body.decode("utf-8", errors="replace")
                _sensitive = {"authorization", "x-api-key", "api-key"}
                _masked_fwd_headers = {
                    k: ("***" if k.lower() in _sensitive else v)
                    for k, v in forwarded_headers.items()
                }
                await self._broadcast_ws_message("traffic_chunk", {
                    "trace_id": trace_id,
                    "phase": "provider_response",
                    "provider": provider_client.provider.name,
                    "status_code": status_code,
                    "response_time": time.time() - start_time,
                    "forwarded_headers": _masked_fwd_headers,
                    "response_headers": dict(response_headers),
                    "response_body": _resp_json_preview,
                    "timestamp": time.time(),
                })

                # 检测 HTTP 错误（4xx / 5xx）
                if status_code >= 400:
                    # 尝试从响应体里找 API 级错误描述
                    try:
                        err_json = json.loads(response_body)
                        err_msg = (
                            err_json.get("error", {}).get("message")
                            or err_json.get("message")
                            or response_body.decode("utf-8", errors="replace")
                        )
                        error_type = "api_error"
                    except Exception:
                        err_msg = response_body.decode("utf-8", errors="replace")
                        error_type = "http_error"

                    upstream_error = PendingErrorRequest(
                        error_id=str(uuid.uuid4()),
                        provider=provider_client.provider.name,
                        model=model,
                        status_code=status_code,
                        error_type=error_type,
                        error_message=err_msg,
                        response_body=response_body,
                        response_headers=dict(response_headers),
                        is_stream=client_requested_stream,
                        timestamp=time.time(),
                        retry_headers=dict(headers),
                        retry_body=modified_body,
                        retry_path=request.path,
                        retry_method=request.method,
                        provider_client=provider_client,
                    )

            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.NetworkError, httpx.RemoteProtocolError) as net_err:
                upstream_error = PendingErrorRequest(
                    error_id=str(uuid.uuid4()),
                    provider=provider_client.provider.name,
                    model=model,
                    status_code=0,
                    error_type="network_error",
                    error_message=str(net_err),
                    response_body=b"",
                    response_headers={},
                    is_stream=client_requested_stream,
                    timestamp=time.time(),
                    retry_headers=dict(headers),
                    retry_body=modified_body,
                    retry_path=request.path,
                    retry_method=request.method,
                    provider_client=provider_client,
                )
                # 为后续统计提供默认值
                status_code = 502
                response_headers = {}
                response_body = b""
                forwarded_headers = {}
                # ── 阶段3（网络错误）：广播错误响应 ──────────────────────
                await self._broadcast_ws_message("traffic_chunk", {
                    "trace_id": trace_id,
                    "phase": "provider_response",
                    "provider": provider_client.provider.name,
                    "status_code": 0,
                    "response_time": time.time() - start_time,
                    "response_headers": {},
                    "response_body": {"error": str(net_err)},
                    "timestamp": time.time(),
                })

            # ── 上游发生错误：挂起请求，等待用户决策（支持多次重试）──────
            if upstream_error is not None:
                # 建立流式响应通道（用于保活），在整个决策循环期间保持连接
                if client_requested_stream:
                    stream_resp = web.StreamResponse(status=200, headers={
                        "content-type": "text/event-stream; charset=utf-8",
                        "cache-control": "no-cache",
                        "x-accel-buffering": "no",
                    })
                else:
                    stream_resp = web.StreamResponse(status=200, headers={
                        "content-type": "application/json",
                    })
                await stream_resp.prepare(request)

            while upstream_error is not None:
                # 记录此次错误的统计（每次错误/重试均记录一条）
                early_err_body = (
                    upstream_error.response_body
                    if upstream_error.response_body
                    else json.dumps({"error": {"type": upstream_error.error_type,
                                               "message": upstream_error.error_message}}).encode()
                )
                self._collect_statistics(
                    provider=provider_client.provider.name,
                    model=model,
                    method=request.method,
                    incoming_url=incoming_url,
                    request_data=request_data,
                    incoming_headers=dict(request.headers),
                    forwarded_headers=forwarded_headers,
                    forwarded_url=forwarded_url,
                    response_data=early_err_body,
                    response_headers=upstream_error.response_headers,
                    status_code=upstream_error.status_code or 502,
                    response_time=time.time() - start_time,
                    client_response_data=b"[pending user decision]",
                )

                # 启动保活协程
                if client_requested_stream:
                    keepalive = asyncio.create_task(self._keepalive_sse(stream_resp))
                else:
                    keepalive = asyncio.create_task(self._keepalive_chunked(stream_resp))

                # 等待用户决策
                decision = await self._wait_for_error_decision(upstream_error, keepalive)

                # ── 处理决策 ─────────────────────────────────────────────
                if decision == "retry":
                    # 用用户（可能已修改过的）headers/body 重新发送请求
                    # 若 reload 已重建了 provider 客户端，使用最新实例（旧实例已关闭）
                    retry_client = self.provider_clients.get(upstream_error.provider, upstream_error.provider_client)
                    upstream_error.provider_client = retry_client
                    retry_err: Optional[PendingErrorRequest] = None
                    retry_fwd_headers: Dict[str, str] = {}
                    await self._broadcast_ws_message("traffic_chunk", {
                        "trace_id": trace_id,
                        "phase": "forwarding",
                        "provider": upstream_error.provider,
                        "model": upstream_error.model,
                        "method": upstream_error.retry_method,
                        "url": f"{retry_client.provider.base_url.rstrip('/')}/{upstream_error.retry_path.lstrip('/')}",
                        "timestamp": time.time(),
                        "note": "retry",
                    })
                    try:
                        status_code, response_headers, response_body, retry_fwd_headers = \
                            await retry_client.forward_request(
                                method=upstream_error.retry_method,
                                path=upstream_error.retry_path,
                                headers=upstream_error.retry_headers,
                                body=upstream_error.retry_body,
                            )
                        try:
                            _retry_resp_preview = json.loads(response_body)
                        except Exception:
                            _retry_resp_preview = response_body.decode("utf-8", errors="replace")
                        await self._broadcast_ws_message("traffic_chunk", {
                            "trace_id": trace_id,
                            "phase": "provider_response",
                            "provider": upstream_error.provider,
                            "status_code": status_code,
                            "response_time": time.time() - start_time,
                            "response_headers": dict(response_headers),
                            "response_body": _retry_resp_preview,
                            "timestamp": time.time(),
                            "note": "retry",
                        })
                        if status_code >= 400:
                            try:
                                err_json2 = json.loads(response_body)
                                err_msg2 = (
                                    err_json2.get("error", {}).get("message")
                                    or err_json2.get("message")
                                    or response_body.decode("utf-8", errors="replace")
                                )
                                error_type2 = "api_error"
                            except Exception:
                                err_msg2 = response_body.decode("utf-8", errors="replace")
                                error_type2 = "http_error"
                            retry_err = PendingErrorRequest(
                                error_id=str(uuid.uuid4()),
                                provider=upstream_error.provider,
                                model=upstream_error.model,
                                status_code=status_code,
                                error_type=error_type2,
                                error_message=err_msg2,
                                response_body=response_body,
                                response_headers=dict(response_headers),
                                is_stream=client_requested_stream,
                                timestamp=time.time(),
                                retry_headers=dict(upstream_error.retry_headers),
                                retry_body=upstream_error.retry_body,
                                retry_path=upstream_error.retry_path,
                                retry_method=upstream_error.retry_method,
                                provider_client=upstream_error.provider_client,
                            )
                    except (httpx.ConnectError, httpx.TimeoutException,
                            httpx.NetworkError, httpx.RemoteProtocolError) as net_err2:
                        await self._broadcast_ws_message("traffic_chunk", {
                            "trace_id": trace_id,
                            "phase": "provider_response",
                            "provider": upstream_error.provider,
                            "status_code": 0,
                            "response_time": time.time() - start_time,
                            "response_headers": {},
                            "response_body": {"error": str(net_err2)},
                            "timestamp": time.time(),
                            "note": "retry",
                        })
                        retry_err = PendingErrorRequest(
                            error_id=str(uuid.uuid4()),
                            provider=upstream_error.provider,
                            model=upstream_error.model,
                            status_code=0,
                            error_type="network_error",
                            error_message=str(net_err2),
                            response_body=b"",
                            response_headers={},
                            is_stream=client_requested_stream,
                            timestamp=time.time(),
                            retry_headers=dict(upstream_error.retry_headers),
                            retry_body=upstream_error.retry_body,
                            retry_path=upstream_error.retry_path,
                            retry_method=upstream_error.retry_method,
                            provider_client=upstream_error.provider_client,
                        )
                        status_code = 502
                        response_headers = {}
                        response_body = b""
                        retry_fwd_headers = {}
                    forwarded_headers = retry_fwd_headers
                    upstream_error = retry_err  # None 表示重试成功，退出循环
                    if upstream_error is None:
                        # 重试成功，response_body/response_headers/status_code 已更新，跳出循环走正常路径
                        break
                    else:
                        # 重试仍然失败，继续循环等待下一次决策
                        continue

                # return_error / fake_response 分支：写入响应体后退出
                _err_client_body_preview = ""
                if decision == "return_error":
                    if client_requested_stream:
                        err_payload = json.dumps({
                            "type": "error",
                            "error": {
                                "type": upstream_error.error_type,
                                "message": upstream_error.error_message,
                            },
                        }, ensure_ascii=False)
                        err_sse = (
                            f"event: error\ndata: {err_payload}\n\n"
                            "event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
                        ).encode("utf-8")
                        await stream_resp.write(err_sse)
                        _err_client_body_preview = err_sse.decode("utf-8", errors="replace")
                    else:
                        if upstream_error.response_body:
                            await stream_resp.write(upstream_error.response_body)
                            _err_client_body_preview = upstream_error.response_body.decode("utf-8", errors="replace")
                        else:
                            err_json = json.dumps({
                                "error": {
                                    "type": upstream_error.error_type,
                                    "message": upstream_error.error_message,
                                }
                            }, ensure_ascii=False).encode("utf-8")
                            await stream_resp.write(err_json)
                            _err_client_body_preview = err_json.decode("utf-8", errors="replace")

                elif decision == "fake_response":
                    fake_data = upstream_error.fake_body or b""
                    await stream_resp.write(fake_data)
                    _err_client_body_preview = fake_data.decode("utf-8", errors="replace")

                await self._broadcast_ws_message("traffic_chunk", {
                    "trace_id": trace_id,
                    "phase": "client_response",
                    "provider": upstream_error.provider,
                    "model": upstream_error.model,
                    "status_code": upstream_error.status_code or 502,
                    "response_time": time.time() - start_time,
                    "client_response_headers": {},
                    "client_response_body": _err_client_body_preview,
                    "timestamp": time.time(),
                })
                await stream_resp.write_eof()
                return stream_resp

            # ── 正常路径（上游返回成功响应 / 重试成功）───────────────────

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

            # ── 阶段4：发送给客户端 ─────────────────────────────────
            try:
                _client_body_preview = client_body.decode("utf-8", errors="replace")
            except Exception:
                _client_body_preview = ""
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "client_response",
                "provider": provider_client.provider.name,
                "model": model,
                "status_code": status_code,
                "response_time": time.time() - start_time,
                "client_response_headers": dict(client_headers),
                "client_response_body": _client_body_preview,
                "timestamp": time.time(),
            })

            # 若是重试成功（stream_resp 已经 prepare），通过 StreamResponse 写入响应体
            if stream_resp is not None and stream_resp.prepared:
                await stream_resp.write(client_body)
                await stream_resp.write_eof()
                return stream_resp
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

        finally:
            # 客户端断开（请求完成）
            self.active_clients = max(0, self.active_clients - 1)
            logging.info(f"客户端断开: {client_ip} [{request.method} {request.path}], 当前活跃连接数: {self.active_clients}")
            asyncio.create_task(self._broadcast_ws_message("client_connection", {
                "event": "disconnected",
                "client_ip": client_ip,
                "path": request.path,
                "method": request.method,
                "active_clients": self.active_clients,
            }))

    async def handle_proxy_request(self, request: web.Request) -> web.Response:
        """通用代理请求处理"""
        start_time = time.time()

        # 客户端连接
        self.active_clients += 1
        client_ip = request.remote or "unknown"
        logging.info(f"客户端连接: {client_ip} [{request.method} {request.path}], 当前活跃连接数: {self.active_clients}")
        asyncio.create_task(self._broadcast_ws_message("client_connection", {
            "event": "connected",
            "client_ip": client_ip,
            "path": request.path,
            "method": request.method,
            "active_clients": self.active_clients,
        }))

        try:
            stream_resp: Optional[web.StreamResponse] = None  # 重试路径复用的响应通道
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

            forwarded_url = f"{provider_client.provider.base_url.rstrip('/')}/{request.path_qs.lstrip('/')}"
            incoming_url = str(request.url)
            try:
                request_body = json.loads(body) if body else {}
            except Exception:
                request_body = body.decode("utf-8", errors="replace") if body else ""

            trace_id = str(uuid.uuid4())
            _sensitive_proxy = {"authorization", "x-api-key", "api-key"}

            # ── 阶段1：收到客户端请求 ──────────────────────────────────
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "client_request",
                "provider": provider_client.provider.name,
                "model": "unknown",
                "method": request.method,
                "incoming_url": incoming_url,
                "timestamp": time.time(),
                "incoming_headers": {
                    k: ("***" if k.lower() in _sensitive_proxy else v)
                    for k, v in request.headers.items()
                },
                "request_body": request_body if isinstance(request_body, dict) else {},
            })

            # ── 阶段2：向 Provider 转发请求 ────────────────────────────
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "forwarding",
                "provider": provider_client.provider.name,
                "model": "unknown",
                "method": request.method,
                "url": forwarded_url,
                "timestamp": time.time(),
            })

            # 转发请求
            upstream_error: Optional[PendingErrorRequest] = None
            try:
                status_code, response_headers, response_body, forwarded_headers = await provider_client.forward_request(
                    method=request.method,
                    path=request.path_qs,
                    headers=headers,
                    body=modified_body
                )

                # ── 阶段3：收到 Provider 响应 ─────────────────────────
                try:
                    _proxy_resp_json = json.loads(response_body)
                except Exception:
                    _proxy_resp_json = response_body.decode("utf-8", errors="replace")
                await self._broadcast_ws_message("traffic_chunk", {
                    "trace_id": trace_id,
                    "phase": "provider_response",
                    "provider": provider_client.provider.name,
                    "status_code": status_code,
                    "response_time": time.time() - start_time,
                    "forwarded_headers": {
                        k: ("***" if k.lower() in _sensitive_proxy else v)
                        for k, v in forwarded_headers.items()
                    },
                    "response_headers": dict(response_headers),
                    "response_body": _proxy_resp_json,
                    "timestamp": time.time(),
                })

                # 检测 HTTP 错误（4xx / 5xx）
                if status_code >= 400:
                    try:
                        err_json = json.loads(response_body)
                        err_msg = (
                            err_json.get("error", {}).get("message")
                            or err_json.get("message")
                            or response_body.decode("utf-8", errors="replace")
                        )
                        error_type = "api_error"
                    except Exception:
                        err_msg = response_body.decode("utf-8", errors="replace")
                        error_type = "http_error"

                    upstream_error = PendingErrorRequest(
                        error_id=str(uuid.uuid4()),
                        provider=provider_client.provider.name,
                        model="unknown",
                        status_code=status_code,
                        error_type=error_type,
                        error_message=err_msg,
                        response_body=response_body,
                        response_headers=dict(response_headers),
                        is_stream=False,
                        timestamp=time.time(),
                        retry_headers=dict(headers),
                        retry_body=modified_body,
                        retry_path=request.path_qs,
                        retry_method=request.method,
                        provider_client=provider_client,
                    )

            except (httpx.ConnectError, httpx.TimeoutException,
                    httpx.NetworkError, httpx.RemoteProtocolError) as net_err:
                upstream_error = PendingErrorRequest(
                    error_id=str(uuid.uuid4()),
                    provider=provider_client.provider.name,
                    model="unknown",
                    status_code=0,
                    error_type="network_error",
                    error_message=str(net_err),
                    response_body=b"",
                    response_headers={},
                    is_stream=False,
                    timestamp=time.time(),
                    retry_headers=dict(headers),
                    retry_body=modified_body,
                    retry_path=request.path_qs,
                    retry_method=request.method,
                    provider_client=provider_client,
                )
                status_code = 502
                response_headers = {}
                response_body = b""
                forwarded_headers = {}
                await self._broadcast_ws_message("traffic_chunk", {
                    "trace_id": trace_id,
                    "phase": "provider_response",
                    "provider": provider_client.provider.name,
                    "status_code": 0,
                    "response_time": time.time() - start_time,
                    "response_headers": {},
                    "response_body": {"error": str(net_err)},
                    "timestamp": time.time(),
                })

            # ── 上游发生错误：挂起请求，等待用户决策（支持多次重试）──────
            if upstream_error is not None:
                stream_resp = web.StreamResponse(status=200, headers={
                    "content-type": "application/json",
                })
                await stream_resp.prepare(request)

            while upstream_error is not None:
                self._collect_statistics(
                    provider=provider_client.provider.name,
                    model="unknown",
                    method=request.method,
                    incoming_url=incoming_url,
                    request_data=request_body if isinstance(request_body, dict) else {},
                    incoming_headers=dict(request.headers),
                    forwarded_headers=forwarded_headers,
                    forwarded_url=forwarded_url,
                    response_data=upstream_error.response_body or json.dumps({
                        "error": {"type": upstream_error.error_type, "message": upstream_error.error_message}
                    }).encode(),
                    response_headers=upstream_error.response_headers,
                    status_code=upstream_error.status_code or 502,
                    response_time=time.time() - start_time,
                    client_response_data=b"[pending user decision]",
                )

                keepalive = asyncio.create_task(self._keepalive_chunked(stream_resp))
                decision = await self._wait_for_error_decision(upstream_error, keepalive)

                if decision == "retry":
                    # 若 reload 已重建了 provider 客户端，使用最新实例（旧实例已关闭）
                    retry_client2 = self.provider_clients.get(upstream_error.provider, upstream_error.provider_client)
                    upstream_error.provider_client = retry_client2
                    retry_err2: Optional[PendingErrorRequest] = None
                    retry_fwd2: Dict[str, str] = {}
                    await self._broadcast_ws_message("traffic_chunk", {
                        "trace_id": trace_id,
                        "phase": "forwarding",
                        "provider": upstream_error.provider,
                        "model": "unknown",
                        "method": upstream_error.retry_method,
                        "url": f"{retry_client2.provider.base_url.rstrip('/')}/{upstream_error.retry_path.lstrip('/')}",
                        "timestamp": time.time(),
                        "note": "retry",
                    })
                    try:
                        status_code, response_headers, response_body, retry_fwd2 = \
                            await retry_client2.forward_request(
                                method=upstream_error.retry_method,
                                path=upstream_error.retry_path,
                                headers=upstream_error.retry_headers,
                                body=upstream_error.retry_body,
                            )
                        try:
                            _r2_preview = json.loads(response_body)
                        except Exception:
                            _r2_preview = response_body.decode("utf-8", errors="replace")
                        await self._broadcast_ws_message("traffic_chunk", {
                            "trace_id": trace_id,
                            "phase": "provider_response",
                            "provider": upstream_error.provider,
                            "status_code": status_code,
                            "response_time": time.time() - start_time,
                            "response_headers": dict(response_headers),
                            "response_body": _r2_preview,
                            "timestamp": time.time(),
                            "note": "retry",
                        })
                        if status_code >= 400:
                            try:
                                _ej2 = json.loads(response_body)
                                _em2 = _ej2.get("error", {}).get("message") or _ej2.get("message") or response_body.decode("utf-8", errors="replace")
                                _et2 = "api_error"
                            except Exception:
                                _em2 = response_body.decode("utf-8", errors="replace")
                                _et2 = "http_error"
                            retry_err2 = PendingErrorRequest(
                                error_id=str(uuid.uuid4()),
                                provider=upstream_error.provider,
                                model="unknown",
                                status_code=status_code,
                                error_type=_et2,
                                error_message=_em2,
                                response_body=response_body,
                                response_headers=dict(response_headers),
                                is_stream=False,
                                timestamp=time.time(),
                                retry_headers=dict(upstream_error.retry_headers),
                                retry_body=upstream_error.retry_body,
                                retry_path=upstream_error.retry_path,
                                retry_method=upstream_error.retry_method,
                                provider_client=upstream_error.provider_client,
                            )
                    except (httpx.ConnectError, httpx.TimeoutException,
                            httpx.NetworkError, httpx.RemoteProtocolError) as _ne2:
                        await self._broadcast_ws_message("traffic_chunk", {
                            "trace_id": trace_id,
                            "phase": "provider_response",
                            "provider": upstream_error.provider,
                            "status_code": 0,
                            "response_time": time.time() - start_time,
                            "response_headers": {},
                            "response_body": {"error": str(_ne2)},
                            "timestamp": time.time(),
                            "note": "retry",
                        })
                        retry_err2 = PendingErrorRequest(
                            error_id=str(uuid.uuid4()),
                            provider=upstream_error.provider,
                            model="unknown",
                            status_code=0,
                            error_type="network_error",
                            error_message=str(_ne2),
                            response_body=b"",
                            response_headers={},
                            is_stream=False,
                            timestamp=time.time(),
                            retry_headers=dict(upstream_error.retry_headers),
                            retry_body=upstream_error.retry_body,
                            retry_path=upstream_error.retry_path,
                            retry_method=upstream_error.retry_method,
                            provider_client=upstream_error.provider_client,
                        )
                        status_code = 502
                        response_headers = {}
                        response_body = b""
                        retry_fwd2 = {}
                    forwarded_headers = retry_fwd2
                    upstream_error = retry_err2
                    if upstream_error is None:
                        break  # 重试成功，走正常路径
                    else:
                        continue  # 重试失败，继续等待决策

                # return_error / fake_response
                _err_proxy_preview = ""
                if decision == "return_error":
                    if upstream_error.response_body:
                        await stream_resp.write(upstream_error.response_body)
                        _err_proxy_preview = upstream_error.response_body.decode("utf-8", errors="replace")
                    else:
                        err_json_b = json.dumps({
                            "error": {
                                "type": upstream_error.error_type,
                                "message": upstream_error.error_message,
                            }
                        }, ensure_ascii=False).encode("utf-8")
                        await stream_resp.write(err_json_b)
                        _err_proxy_preview = err_json_b.decode("utf-8", errors="replace")

                elif decision == "fake_response":
                    fake_data = upstream_error.fake_body or b""
                    await stream_resp.write(fake_data)
                    _err_proxy_preview = fake_data.decode("utf-8", errors="replace")

                await self._broadcast_ws_message("traffic_chunk", {
                    "trace_id": trace_id,
                    "phase": "client_response",
                    "provider": upstream_error.provider,
                    "model": "unknown",
                    "status_code": upstream_error.status_code or 502,
                    "response_time": time.time() - start_time,
                    "client_response_headers": {},
                    "client_response_body": _err_proxy_preview,
                    "timestamp": time.time(),
                })
                await stream_resp.write_eof()
                return stream_resp

            # ── 正常路径 ──────────────────────────────────────────────
            safe_headers = self._safe_response_headers(response_headers)
            self._collect_statistics(
                provider=provider_client.provider.name,
                model="unknown",
                method=request.method,
                incoming_url=incoming_url,
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

            logging.info(
                f"发送响应给客户端 (通用代理) - 状态码: {status_code}, "
                f"响应体大小: {len(response_body) if response_body else 0} 字节, "
                f"路径: {request.path}, 方法: {request.method}"
            )

            # ── 阶段4：发送给客户端 ─────────────────────────────────
            try:
                _proxy_client_preview = response_body.decode("utf-8", errors="replace")
            except Exception:
                _proxy_client_preview = ""
            await self._broadcast_ws_message("traffic_chunk", {
                "trace_id": trace_id,
                "phase": "client_response",
                "provider": provider_client.provider.name,
                "model": "unknown",
                "status_code": status_code,
                "response_time": time.time() - start_time,
                "client_response_headers": dict(safe_headers),
                "client_response_body": _proxy_client_preview,
                "timestamp": time.time(),
            })

            if stream_resp is not None and stream_resp.prepared:
                await stream_resp.write(response_body or b"")
                await stream_resp.write_eof()
                return stream_resp
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

        finally:
            # 客户端断开（请求完成）
            self.active_clients = max(0, self.active_clients - 1)
            logging.info(f"客户端断开: {client_ip} [{request.method} {request.path}], 当前活跃连接数: {self.active_clients}")
            asyncio.create_task(self._broadcast_ws_message("client_connection", {
                "event": "disconnected",
                "client_ip": client_ip,
                "path": request.path,
                "method": request.method,
                "active_clients": self.active_clients,
            }))

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

        elif action == "get stats":
            # 获取统计信息
            stats = self.statistics.get_summary()
            await self._send_ws_message(ws, "statistics_update", stats)

        elif action == "get config":
            # 获取配置信息
            config_summary = self.config.get_config_summary()
            await self._send_ws_message(ws, "config_info", config_summary)

        elif action == "reload":
            # 热重载 providers 和 schemes
            result = await self.reload_config()
            if result["success"]:
                await self._send_ws_message(ws, "command_response", {
                    "action": "reload",
                    "success": True,
                    "message": "配置已热重载",
                    **result,
                })
                # 广播通知所有客户端配置已变更
                await self._broadcast_ws_message("config_reloaded", {
                    "providers": result["providers"],
                    "schemes": result["schemes"],
                })
            else:
                await self._send_ws_message(ws, "error", {
                    "message": f"热重载失败: {result.get('error')}",
                    "action": "reload",
                })

        elif action == "set scheme":
            # 切换当前转发方案
            scheme_name = data.get("scheme")
            if scheme_name:
                scheme = self.config.get_scheme_by_name(scheme_name)
                if scheme:
                    self.current_scheme_name = scheme_name
                    logging.info(f"切换转发方案为: {scheme_name}")
                    await self._send_ws_message(ws, "command_response", {
                        "action": "set scheme",
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

        elif action == "list providers":
            # 返回所有 provider 的详细信息（不含 api_key）
            providers_info = []
            for name, client in self.provider_clients.items():
                p = client.provider
                providers_info.append({
                    "name": p.name,
                    "type": p.type,
                    "enabled": p.enabled,
                    "base_url": p.base_url,
                    "models": p.models,
                    "timeout": p.timeout,
                    "proxy_enabled": p.proxy_enabled,
                    "proxy_url": p.proxy_url if p.proxy_enabled else None,
                    "request_count": client.request_count,
                    "error_count": client.error_count,
                })
            await self._send_ws_message(ws, "providers_info", {
                "providers": providers_info,
                "total": len(providers_info),
            })

        elif action == "update models":
            # 向 provider 查询可用模型列表，并同步到配置
            provider_name = data.get("provider")
            if not provider_name:
                await self._send_ws_message(ws, "error", {
                    "message": "缺少 provider 参数",
                    "action": "update models",
                })
            else:
                result = await self._update_provider_models(provider_name)
                if result["success"]:
                    await self._send_ws_message(ws, "command_response", {
                        "action": "update models",
                        "success": True,
                        "provider": provider_name,
                        "models": result["models"],
                        "message": f"已更新 provider '{provider_name}' 的模型列表，共 {len(result['models'])} 个模型",
                    })
                else:
                    await self._send_ws_message(ws, "error", {
                        "action": "update models",
                        "message": result.get("error", "未知错误"),
                        "provider": provider_name,
                    })

        elif action == "show_pending_request":
            # 查看某个暂停请求准备发送给 provider 的详细数据
            # data: {"action": "show_pending_request", "error_id": str}
            error_id = data.get("error_id")
            if not error_id or error_id not in self.pending_errors:
                await self._send_ws_message(ws, "error", {"message": f"错误请求不存在: {error_id}", "action": action})
            else:
                pending = self.pending_errors[error_id]
                _sensitive = {"authorization", "x-api-key", "api-key"}
                masked_headers = {
                    k: ("***" if k.lower() in _sensitive else v)
                    for k, v in pending.retry_headers.items()
                }
                try:
                    body_preview = json.loads(pending.retry_body)
                except Exception:
                    body_preview = pending.retry_body.decode("utf-8", errors="replace")
                await self._send_ws_message(ws, "command_response", {
                    "action": "show_pending_request",
                    "error_id": error_id,
                    "method": pending.retry_method,
                    "path": pending.retry_path,
                    "provider": pending.provider,
                    "base_url": pending.provider_client.provider.base_url if pending.provider_client else "",
                    "headers": masked_headers,
                    "body": body_preview,
                })

        elif action == "set_request_header":
            # 添加或修改待重试请求的 HTTP 头
            # data: {"action": "set_request_header", "error_id": str, "key": str, "value": str}
            error_id = data.get("error_id")
            key = data.get("key", "").strip()
            value = data.get("value", "")
            if not error_id or error_id not in self.pending_errors:
                await self._send_ws_message(ws, "error", {"message": f"错误请求不存在: {error_id}", "action": action})
            elif not key:
                await self._send_ws_message(ws, "error", {"message": "缺少 key 参数", "action": action})
            else:
                self.pending_errors[error_id].retry_headers[key] = value
                await self._send_ws_message(ws, "command_response", {
                    "action": "set_request_header",
                    "error_id": error_id,
                    "key": key,
                    "value": value if key.lower() not in ("authorization", "x-api-key", "api-key") else "***",
                    "message": f"已设置请求头 {key}",
                })
                logging.info(f"set_request_header: error_id={error_id} key={key}")

        elif action == "delete_request_header":
            # 删除待重试请求的某个 HTTP 头
            # data: {"action": "delete_request_header", "error_id": str, "key": str}
            error_id = data.get("error_id")
            key = data.get("key", "").strip()
            if not error_id or error_id not in self.pending_errors:
                await self._send_ws_message(ws, "error", {"message": f"错误请求不存在: {error_id}", "action": action})
            elif not key:
                await self._send_ws_message(ws, "error", {"message": "缺少 key 参数", "action": action})
            else:
                removed = self.pending_errors[error_id].retry_headers.pop(key, None)
                if removed is None:
                    # 大小写不敏感地查找
                    for k in list(self.pending_errors[error_id].retry_headers):
                        if k.lower() == key.lower():
                            del self.pending_errors[error_id].retry_headers[k]
                            key = k
                            removed = True
                            break
                await self._send_ws_message(ws, "command_response", {
                    "action": "delete_request_header",
                    "error_id": error_id,
                    "key": key,
                    "found": removed is not None,
                    "message": f"已删除请求头 {key}" if removed is not None else f"请求头 {key} 不存在",
                })
                logging.info(f"delete_request_header: error_id={error_id} key={key}")

        elif action == "set_request_body":
            # 替换待重试请求的 body
            # data: {"action": "set_request_body", "error_id": str, "body": str}
            error_id = data.get("error_id")
            new_body = data.get("body", "")
            if not error_id or error_id not in self.pending_errors:
                await self._send_ws_message(ws, "error", {"message": f"错误请求不存在: {error_id}", "action": action})
            else:
                self.pending_errors[error_id].retry_body = new_body.encode("utf-8") if isinstance(new_body, str) else new_body
                await self._send_ws_message(ws, "command_response", {
                    "action": "set_request_body",
                    "error_id": error_id,
                    "body_length": len(self.pending_errors[error_id].retry_body),
                    "message": f"已更新请求体，长度 {len(self.pending_errors[error_id].retry_body)} 字节",
                })
                logging.info(f"set_request_body: error_id={error_id} length={len(self.pending_errors[error_id].retry_body)}")

        elif action == "resolve_error":
            # 用户对暂停的上游错误请求做出决策
            # data: {"action": "resolve_error", "error_id": str, "decision": "return_error"|"fake_response"|"retry",
            #        "fake_body": str (原始body字符串，可选), "fake_text": str (纯文本，可选)}
            error_id = data.get("error_id")
            user_action = data.get("decision", "return_error")

            if not error_id:
                await self._send_ws_message(ws, "error", {
                    "message": "缺少 error_id 参数",
                    "action": "resolve_error",
                })
            elif error_id not in self.pending_errors:
                await self._send_ws_message(ws, "error", {
                    "message": f"错误请求不存在或已处理: {error_id}",
                    "action": "resolve_error",
                })
            else:
                pending = self.pending_errors[error_id]
                pending.decision = user_action

                # 解析伪造响应内容
                if user_action == "fake_response":
                    if "fake_body" in data:
                        # 用户提供了原始 HTTP body 字符串，直接编码使用
                        pending.fake_body = data["fake_body"].encode("utf-8")
                    elif "fake_text" in data:
                        # 用户只提供了文本，由服务器组装成合适格式
                        pending.fake_body = self._build_fake_body(data["fake_text"], pending.is_stream, pending.model)

                pending.event.set()  # 唤醒等待的请求协程

                await self._send_ws_message(ws, "command_response", {
                    "action": "resolve_error",
                    "success": True,
                    "error_id": error_id,
                    "decision": user_action,
                    "message": f"已处理错误请求 {error_id}，决策: {user_action}",
                })
                logging.info(f"WS resolve_error: id={error_id} action={user_action}")

        else:
            await self._send_ws_message(ws, "error", {
                "message": f"未知命令: {action}",
                "available_commands": ["shutdown", "get stats", "get config", "set scheme", "reload", "update models", "list providers", "resolve_error", "show_pending_request", "set_request_header", "delete_request_header", "set_request_body"]
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

    # ------------------------------------------------------------------
    # 上游错误暂停 / 用户决策机制
    # ------------------------------------------------------------------

    async def _wait_for_error_decision(
        self,
        pending: PendingErrorRequest,
        keepalive_task: asyncio.Task,
    ) -> str:
        """
        挂起当前请求协程，广播 upstream_error 给 UI，然后等待用户通过
        WS 命令 resolve_error 做出决策。

        返回用户选择的 decision 字符串（例如 "return_error"）。
        keepalive_task 在此函数返回前会被取消。
        """
        self.pending_errors[pending.error_id] = pending

        # 广播给操作界面
        await self._broadcast_ws_message("upstream_error", {
            "error_id": pending.error_id,
            "provider": pending.provider,
            "model": pending.model,
            "status_code": pending.status_code,
            "error_type": pending.error_type,
            "error_message": pending.error_message,
            "timestamp": pending.timestamp,
            "is_stream": pending.is_stream,
            "available_actions": ["return_error", "fake_response"],
        })
        logging.info(
            f"上游错误已暂停，等待用户决策 [id={pending.error_id}] "
            f"provider={pending.provider} status={pending.status_code}"
        )

        # 等待用户决策（Event 被 _handle_ws_command 的 resolve_error 分支 set）
        await pending.event.wait()

        # 停止保活
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass

        self.pending_errors.pop(pending.error_id, None)
        decision = pending.decision or "return_error"
        logging.info(f"用户决策 [id={pending.error_id}]: {decision}")
        return decision

    @staticmethod
    def _build_fake_body(text: str, is_stream: bool, model: str) -> bytes:
        """
        将用户提供的纯文本组装为符合 Anthropic API 格式的响应体。

        is_stream=True  → SSE 格式（模拟完整流式响应）
        is_stream=False → JSON 格式（模拟非流式 Message 响应）
        """
        if is_stream:
            # 构造最小化的 SSE 流：message_start → content_block_start → content_block_delta → content_block_stop → message_stop
            msg_id = f"msg_fake_{uuid.uuid4().hex[:16]}"
            events = [
                {"type": "message_start", "message": {
                    "id": msg_id, "type": "message", "role": "assistant",
                    "content": [], "model": model, "stop_reason": None,
                    "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0},
                }},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "ping"},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 "usage": {"output_tokens": len(text.split())}},
                {"type": "message_stop"},
            ]
            sse_parts = []
            for ev in events:
                sse_parts.append(f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n")
            return "".join(sse_parts).encode("utf-8")
        else:
            # 构造非流式 Message 响应
            msg_id = f"msg_fake_{uuid.uuid4().hex[:16]}"
            body = {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": model,
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": len(text.split())},
            }
            return json.dumps(body, ensure_ascii=False).encode("utf-8")

    @staticmethod
    async def _keepalive_chunked(response: web.StreamResponse, interval: float = 10.0):
        """
        非流式客户端保活协程：定期向 StreamResponse 写入空 chunk，
        使 Transfer-Encoding: chunked 保持连接不被客户端超时断开。
        必须在 response.prepare() 之后，response.write_eof() 之前调用。
        """
        try:
            while True:
                await asyncio.sleep(interval)
                await response.write(b"")   # 空 chunk，不携带任何数据
        except (asyncio.CancelledError, ConnectionResetError):
            pass

    @staticmethod
    async def _keepalive_sse(response: web.StreamResponse, interval: float = 15.0):
        """
        SSE 流式客户端保活协程：定期发送 Anthropic SSE ping 事件，
        使客户端在等待用户决策期间不会超时断开。
        """
        ping_bytes = b"event: ping\ndata: {\"type\": \"ping\"}\n\n"
        try:
            while True:
                await asyncio.sleep(interval)
                await response.write(ping_bytes)
        except (asyncio.CancelledError, ConnectionResetError):
            pass

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
                "active_clients": self.active_clients,
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
        print("配置错误:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        sys.exit(1)

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