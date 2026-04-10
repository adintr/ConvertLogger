#!/usr/bin/env python3
"""
服务器管理器 - 管理API服务器进程和WebSocket通信

负责：
1. 启动/停止/重启API服务器子进程
2. 通过WebSocket与服务器实时通信
3. 转发事件到Textual UI
4. 监控服务器健康状态
"""

import asyncio
import aiohttp
import json
import time
import subprocess
import uuid
from typing import Dict, Any, Optional, Callable, List, Set
from dataclasses import dataclass, asdict
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class ServerStatus:
    """服务器状态信息"""
    running: bool = False
    pid: Optional[int] = None
    start_time: Optional[float] = None
    ws_connected: bool = False
    last_heartbeat: Optional[float] = None
    host: str = "localhost"
    port: int = 8000

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "running": self.running,
            "pid": self.pid,
            "start_time": self.start_time,
            "ws_connected": self.ws_connected,
            "last_heartbeat": self.last_heartbeat,
            "host": self.host,
            "port": self.port
        }


class WebSocketClient:
    """WebSocket客户端，管理与服务器的实时连接"""

    def __init__(self, host: str = "localhost", port: int = 8000):
        self.url = f"ws://{host}:{port}/ws"
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self.connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 2.0  # 重连延迟(秒)
        self.message_handlers: List[Callable] = []
        self._receive_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        """连接到WebSocket服务器"""
        if self.connected:
            return True

        try:
            self.session = aiohttp.ClientSession()
            self.ws = await self.session.ws_connect(self.url)
            self.connected = True
            self.reconnect_attempts = 0

            # 启动接收任务
            self._receive_task = asyncio.create_task(self._receive_messages())
            logger.info(f"WebSocket连接到 {self.url} 成功")
            return True

        except Exception as e:
            logger.error(f"WebSocket连接失败 {self.url}: {e}")
            await self._handle_connection_error(e)
            return False

    async def send_message(self, msg_type: str, data: Dict[str, Any]) -> Optional[str]:
        """发送消息到服务器"""
        if not self.connected or not self.ws:
            logger.warning(f"WebSocket未连接，无法发送消息: {msg_type}")
            return None

        message_id = str(uuid.uuid4())
        message = {
            "type": msg_type,
            "id": message_id,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }

        try:
            await self.ws.send_str(json.dumps(message))
            logger.debug(f"发送消息: {msg_type} (id: {message_id})")
            return message_id
        except Exception as e:
            logger.error(f"发送消息失败 {msg_type}: {e}")
            self.connected = False
            return None

    async def _receive_messages(self):
        """接收服务器消息"""
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error(f"WebSocket错误: {self.ws.exception()}")
                    await self._handle_disconnect()
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                 aiohttp.WSMsgType.CLOSING,
                                 aiohttp.WSMsgType.CLOSED):
                    logger.info("WebSocket连接关闭")
                    await self._handle_disconnect()
                    break
        except Exception as e:
            logger.error(f"接收消息异常: {e}")
            await self._handle_disconnect()

    async def _handle_message(self, raw_message: str):
        """处理收到的消息"""
        try:
            message = json.loads(raw_message)
            msg_type = message.get("type")
            logger.debug(f"收到消息: {msg_type}")

            # 调用所有注册的处理器
            for handler in self.message_handlers:
                try:
                    await handler(message)
                except Exception as e:
                    logger.error(f"消息处理器异常: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"消息解析失败: {e}, 原始消息: {raw_message[:100]}")

    def register_handler(self, handler: Callable):
        """注册消息处理器"""
        self.message_handlers.append(handler)
        logger.debug(f"注册消息处理器，总数: {len(self.message_handlers)}")

    async def _handle_connection_error(self, error: Exception):
        """处理连接错误"""
        self.reconnect_attempts += 1
        if self.reconnect_attempts <= self.max_reconnect_attempts:
            logger.info(f"等待 {self.reconnect_delay} 秒后重连 (尝试 {self.reconnect_attempts}/{self.max_reconnect_attempts})")
            await asyncio.sleep(self.reconnect_delay)
            await self.connect()
        else:
            logger.error(f"达到最大重连次数 {self.max_reconnect_attempts}，停止重连")

    async def _handle_disconnect(self):
        """处理断开连接"""
        self.connected = False
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()

        # 尝试重连
        await self._handle_connection_error(Exception("连接断开"))

    async def disconnect(self):
        """断开连接"""
        logger.info("断开WebSocket连接")
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()

        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()

        self.connected = False
        self.ws = None
        self.session = None


class ServerManager:
    """管理API服务器进程和WebSocket通信"""

    def __init__(self, config_path: str = "config.yaml", host: str = "localhost", port: int = 8000):
        self.config_path = config_path
        self.host = host
        self.port = port

        # 服务器状态
        self.status = ServerStatus(host=host, port=port)

        # 子进程
        self.process: Optional[subprocess.Popen] = None

        # WebSocket客户端
        self.ws_client = WebSocketClient(host, port)
        self.ws_client.register_handler(self._handle_ws_message)

        # 事件处理器
        self.event_handlers: List[Callable] = []

        # 心跳监控
        self.heartbeat_timeout = 35.0  # 心跳超时时间(秒)
        self._heartbeat_check_task: Optional[asyncio.Task] = None

        logger.info(f"ServerManager初始化完成，服务器地址: {host}:{port}")

    def register_event_handler(self, handler: Callable):
        """注册事件处理器"""
        self.event_handlers.append(handler)
        logger.debug(f"注册事件处理器，总数: {len(self.event_handlers)}")

    async def _emit_event(self, event_type: str, data: Dict[str, Any]):
        """发射事件到所有处理器"""
        event = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "data": data
        }

        for handler in self.event_handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.error(f"事件处理器异常: {e}")

    async def start_server(self) -> bool:
        """启动API服务器子进程"""
        if self.status.running:
            logger.warning("服务器已经在运行")
            return False

        try:
            # 启动服务器进程
            self.process = subprocess.Popen(
                ["python", "api_server.py", "--config", self.config_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            self.status.running = True
            self.status.pid = self.process.pid
            self.status.start_time = time.time()

            logger.info(f"服务器进程已启动，PID: {self.process.pid}")

            # 等待服务器初始化
            logger.info("等待服务器初始化...")
            await asyncio.sleep(3)

            # 连接WebSocket
            logger.info("连接WebSocket...")
            ws_connected = await self.ws_client.connect()
            if not ws_connected:
                logger.warning("WebSocket连接失败，但服务器进程已启动")

            # 启动心跳监控
            self._start_heartbeat_monitor()

            # 发送服务器状态事件
            await self._emit_event("server_status", self.status.to_dict())

            return True

        except Exception as e:
            logger.error(f"启动服务器失败: {e}")
            self.status.running = False
            return False

    async def stop_server(self) -> bool:
        """停止API服务器"""
        if not self.status.running:
            logger.warning("服务器未运行")
            return True

        logger.info("停止服务器...")

        # 停止心跳监控
        if self._heartbeat_check_task and not self._heartbeat_check_task.done():
            self._heartbeat_check_task.cancel()

        # 通过WebSocket发送优雅停止命令
        if self.status.ws_connected:
            await self.ws_client.send_message("command", {"action": "shutdown"})
            await asyncio.sleep(2)  # 等待服务器处理

        # 终止进程
        if self.process:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                    logger.info("服务器进程已终止")
                except subprocess.TimeoutExpired:
                    logger.warning("进程终止超时，强制杀死")
                    self.process.kill()
                    self.process.wait()
            except Exception as e:
                logger.error(f"终止进程失败: {e}")
            finally:
                self.process = None

        # 断开WebSocket
        await self.ws_client.disconnect()

        # 更新状态
        self.status.running = False
        self.status.pid = None
        self.status.ws_connected = False

        # 发送服务器状态事件
        await self._emit_event("server_status", self.status.to_dict())

        logger.info("服务器已停止")
        return True

    async def restart_server(self) -> bool:
        """重启API服务器"""
        logger.info("重启服务器...")

        if self.status.running:
            success = await self.stop_server()
            if not success:
                return False
            await asyncio.sleep(1)  # 等待清理

        return await self.start_server()

    async def send_command(self, command: str, data: Dict[str, Any] = None) -> Optional[str]:
        """发送控制命令到服务器"""
        if data is None:
            data = {}
        data["action"] = command
        logger.debug(f"发送命令: {command}, 数据: {data}")

        if not self.status.ws_connected:
            logger.warning(f"WebSocket未连接，无法发送命令: {command}")
            return None

        return await self.ws_client.send_message("command", data)

    async def _handle_ws_message(self, message: Dict[str, Any]):
        """处理WebSocket消息"""
        msg_type = message.get("type")
        msg_data = message.get("data", {})

        logger.debug(f"处理WebSocket消息: {msg_type}")

        if msg_type == "server_status":
            # 心跳更新
            self.status.ws_connected = True
            self.status.last_heartbeat = time.time()

            # 合并服务器状态数据
            if isinstance(msg_data, dict):
                for key, value in msg_data.items():
                    if hasattr(self.status, key):
                        setattr(self.status, key, value)

            # 转发事件到UI
            await self._emit_event("server_status", self.status.to_dict())

        elif msg_type == "statistics_update":
            # 统计更新
            await self._emit_event("statistics_update", msg_data)

        elif msg_type == "request_log":
            # 请求日志
            await self._emit_event("request_log", msg_data)

        elif msg_type == "providers_info":
            # provider 列表信息
            await self._emit_event("providers_info", msg_data)

        elif msg_type == "command_response":
            # 命令响应
            await self._emit_event("command_response", msg_data)

        elif msg_type == "config_reloaded":
            # 广播：配置已重载
            await self._emit_event("config_reloaded", msg_data)

        elif msg_type == "upstream_error":
            # 上游API错误暂停事件，需要转发给UI让用户决策
            await self._emit_event("upstream_error", msg_data)

        elif msg_type == "traffic_chunk":
            # 实时流量分阶段数据
            await self._emit_event("traffic_chunk", msg_data)

        elif msg_type == "client_connection":
            # 客户端连接/断开事件
            await self._emit_event("client_connection", msg_data)

        elif msg_type == "error":
            # 错误消息
            await self._emit_event("error", msg_data)

    def _start_heartbeat_monitor(self):
        """启动心跳监控"""
        async def _check_heartbeat():
            while self.status.running:
                await asyncio.sleep(10)  # 每10秒检查一次

                if self.status.ws_connected and self.status.last_heartbeat:
                    time_since_heartbeat = time.time() - self.status.last_heartbeat
                    if time_since_heartbeat > self.heartbeat_timeout:
                        logger.warning(f"心跳超时: {time_since_heartbeat:.1f}秒")
                        self.status.ws_connected = False
                        await self._emit_event("server_status", self.status.to_dict())

                        # 尝试重连
                        if self.status.running:
                            logger.info("尝试重新连接WebSocket...")
                            await self.ws_client.connect()

        self._heartbeat_check_task = asyncio.create_task(_check_heartbeat())

    async def get_server_info(self) -> Dict[str, Any]:
        """获取服务器信息"""
        return {
            "status": self.status.to_dict(),
            "config_path": self.config_path,
            "ws_connected": self.status.ws_connected,
            "uptime": time.time() - self.status.start_time if self.status.start_time else 0
        }


if __name__ == "__main__":
    # 测试ServerManager
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    async def test_manager():
        manager = ServerManager()

        def print_event(event):
            print(f"事件: {event['type']}")
            if event['type'] == 'server_status':
                print(f"  状态: {event['data']}")

        manager.register_event_handler(print_event)

        print("1. 启动服务器...")
        if await manager.start_server():
            print("服务器启动成功")

            # 等待一些事件
            await asyncio.sleep(5)

            print("\n2. 发送测试命令...")
            cmd_id = await manager.send_command("get stats")
            print(f"命令发送，ID: {cmd_id}")

            await asyncio.sleep(3)

            print("\n3. 停止服务器...")
            await manager.stop_server()
            print("服务器已停止")
        else:
            print("服务器启动失败")

        await asyncio.sleep(1)

    try:
        asyncio.run(test_manager())
    except KeyboardInterrupt:
        print("\n测试中断")
        sys.exit(0)