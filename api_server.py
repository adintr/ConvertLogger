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

from config import load_config, Config, ProviderConfig


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
                             body: Optional[bytes] = None) -> Tuple[int, Dict[str, str], bytes]:
        """
        转发请求到provider

        Returns:
            Tuple[int, Dict[str, str], bytes]: (状态码, 响应头, 响应体)
        """
        if not self.client:
            await self.initialize()

        self.last_used = time.time()
        self.request_count += 1

        try:
            # 构建完整URL
            url = f"{self.provider.base_url.rstrip('/')}/{path.lstrip('/')}"

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

            return response.status_code, response_headers, response_body

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

            return 502, {"Content-Type": "application/json"}, json.dumps(error_response).encode()

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

    def __init__(self, config: Config):
        self.config = config
        self.app = web.Application()
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None

        # 初始化组件
        self.statistics = StatisticsCollector()
        self.provider_clients: Dict[str, ProviderClient] = {}

        # 设置路由
        self.setup_routes()

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
        3. 第一个启用的provider
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

    def select_provider_by_model(self, model_name: str, request: web.Request) -> Optional[ProviderClient]:
        """
        根据模型名称选择provider

        首先尝试使用请求指定的provider，然后检查该provider是否支持该模型。
        如果不支持或未指定provider，则查找支持该模型的provider。
        """
        if not self.provider_clients:
            return None

        # 首先尝试请求指定的provider
        provider_client = self.select_provider_by_request(request)
        if provider_client:
            # 检查该provider是否支持该模型
            if provider_client.provider.is_model_supported(model_name):
                return provider_client
            else:
                logging.warning(f"请求指定的provider不支持模型 {model_name}: {provider_client.provider.name}")

        # 查找支持该模型的provider
        for client in self.provider_clients.values():
            if client.provider.is_model_supported(model_name):
                return client

        # 没有找到支持该模型的provider
        return None

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

    async def handle_anthropic_request(self, request: web.Request) -> web.Response:
        """处理Anthropic API请求"""
        start_time = time.time()

        try:
            # 读取请求体
            body = await request.read()

            # 解析JSON获取模型信息
            request_data = json.loads(body) if body else {}
            model = request_data.get("model", "unknown")

            # 选择provider
            provider_client = self.select_provider_by_model(model, request)
            if not provider_client:
                error_response = {
                    "error": {
                        "type": "no_provider",
                        "message": f"No available provider for model: {model}. "
                                  f"请通过X-Provider请求头或provider查询参数指定provider，"
                                  f"或确认配置的provider支持该模型。"
                    }
                }
                return web.json_response(error_response, status=503)

            # 构建转发headers
            headers = dict(request.headers)

            # 移除不需要转发的headers
            headers_to_remove = ["host", "content-length", "connection"]
            for header in headers_to_remove:
                headers.pop(header, None)

            # 转发请求
            status_code, response_headers, response_body = await provider_client.forward_request(
                method=request.method,
                path=request.path,
                headers=headers,
                body=body
            )

            # 收集统计信息
            self._collect_statistics(
                provider=provider_client.provider.name,
                model=model,
                request_data=request_data,
                response_data=response_body,
                status_code=status_code,
                response_time=time.time() - start_time
            )

            # 返回响应
            return web.Response(
                status=status_code,
                headers=response_headers,
                body=response_body
            )

        except json.JSONDecodeError:
            error_response = {
                "error": {
                    "type": "invalid_json",
                    "message": "Invalid JSON in request body"
                }
            }
            return web.json_response(error_response, status=400)

        except Exception as e:
            logging.error(f"处理请求失败: {e}")
            error_response = {
                "error": {
                    "type": "server_error",
                    "message": f"Internal server error: {str(e)}"
                }
            }
            return web.json_response(error_response, status=500)

    async def handle_proxy_request(self, request: web.Request) -> web.Response:
        """通用代理请求处理"""
        start_time = time.time()

        try:
            # 读取请求体
            body = await request.read()

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

            # 构建转发headers
            headers = dict(request.headers)

            # 移除不需要转发的headers
            headers_to_remove = ["host", "content-length", "connection"]
            for header in headers_to_remove:
                headers.pop(header, None)

            # 转发请求
            status_code, response_headers, response_body = await provider_client.forward_request(
                method=request.method,
                path=request.path_qs,
                headers=headers,
                body=body
            )

            # 简单统计（无法解析具体模型）
            stats = RequestStatistics(
                timestamp=start_time,
                provider_name=provider_client.provider.name,
                model="unknown",
                response_time=time.time() - start_time,
                status_code=status_code,
                success=status_code < 400
            )
            self.statistics.add_request(stats)

            # 返回响应
            return web.Response(
                status=status_code,
                headers=response_headers,
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
            return web.json_response(error_response, status=500)

    def _collect_statistics(self,
                           provider: str,
                           model: str,
                           request_data: Dict[str, Any],
                           response_data: bytes,
                           status_code: int,
                           response_time: float) -> None:
        """收集请求统计信息"""
        try:
            # 尝试从请求中提取token数量
            prompt_tokens = 0
            completion_tokens = 0

            # 从Anthropic请求中提取
            if "messages" in request_data:
                # 简单估算：每个消息10个token
                prompt_tokens = len(request_data["messages"]) * 10
            elif "prompt" in request_data:
                # 简单估算：每4个字符约1个token
                prompt_text = str(request_data["prompt"])
                prompt_tokens = len(prompt_text) // 4

            # 尝试从响应中提取token数量
            if response_data:
                try:
                    response_json = json.loads(response_data)

                    # Anthropic响应格式
                    if "usage" in response_json:
                        usage = response_json["usage"]
                        prompt_tokens = usage.get("input_tokens", prompt_tokens)
                        completion_tokens = usage.get("output_tokens", 0)

                    # OpenAI兼容格式
                    elif "choices" in response_json and len(response_json["choices"]) > 0:
                        choice = response_json["choices"][0]
                        if "message" in choice:
                            # 简单估算响应token
                            message_text = str(choice["message"].get("content", ""))
                            completion_tokens = len(message_text) // 4
                except:
                    pass

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

        except Exception as e:
            logging.warning(f"收集统计信息失败: {e}")


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
        print(f"服务器运行中... 按 Ctrl+C 停止")
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