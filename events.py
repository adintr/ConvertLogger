#!/usr/bin/env python3
"""
事件系统定义
定义标准事件格式和类型
"""

from enum import Enum
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, Optional


class EventType(Enum):
    """事件类型枚举"""
    SERVER_STATUS = "server_status"
    STATISTICS_UPDATE = "statistics_update"
    REQUEST_LOG = "request_log"
    ERROR = "error"
    COMMAND = "command"
    HEARTBEAT = "heartbeat"


@dataclass
class Event:
    """事件基类"""
    type: EventType
    data: Dict[str, Any]
    timestamp: datetime = None
    source: str = "system"

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "source": self.source
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Event':
        """从字典创建事件"""
        return cls(
            type=EventType(data.get("type", "unknown")),
            data=data.get("data", {}),
            timestamp=datetime.fromisoformat(data.get("timestamp"))
                     if data.get("timestamp") else None,
            source=data.get("source", "unknown")
        )


@dataclass
class ServerStatusEvent(Event):
    """服务器状态事件"""
    def __init__(self, status_data: Dict[str, Any]):
        super().__init__(
            type=EventType.SERVER_STATUS,
            data=status_data,
            source="server"
        )


@dataclass
class StatisticsUpdateEvent(Event):
    """统计更新事件"""
    def __init__(self, stats_data: Dict[str, Any]):
        super().__init__(
            type=EventType.STATISTICS_UPDATE,
            data=stats_data,
            source="statistics_collector"
        )


@dataclass
class RequestLogEvent(Event):
    """请求日志事件"""
    def __init__(self, log_data: Dict[str, Any]):
        super().__init__(
            type=EventType.REQUEST_LOG,
            data=log_data,
            source="request_handler"
        )


@dataclass
class ErrorEvent(Event):
    """错误事件"""
    def __init__(self, error_data: Dict[str, Any]):
        super().__init__(
            type=EventType.ERROR,
            data=error_data,
            source="error_handler"
        )


class EventHandler:
    """事件处理器基类"""

    async def handle_event(self, event: Event):
        """处理事件"""
        raise NotImplementedError("子类必须实现handle_event方法")


class EventEmitter:
    """事件发射器"""

    def __init__(self):
        self.handlers: Dict[EventType, list] = {}

    def register_handler(self, event_type: EventType, handler: EventHandler):
        """注册事件处理器"""
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        self.handlers[event_type].append(handler)

    async def emit(self, event: Event):
        """发射事件"""
        event_type = event.type
        if event_type in self.handlers:
            for handler in self.handlers[event_type]:
                try:
                    await handler.handle_event(event)
                except Exception as e:
                    print(f"事件处理器异常: {e}")


if __name__ == "__main__":
    # 测试事件系统
    import asyncio

    class TestHandler(EventHandler):
        async def handle_event(self, event: Event):
            print(f"处理事件: {event.type.value}")
            print(f"数据: {event.data}")

    async def test():
        emitter = EventEmitter()
        handler = TestHandler()

        emitter.register_handler(EventType.SERVER_STATUS, handler)

        # 创建并发射事件
        status_event = ServerStatusEvent({"running": True, "pid": 12345})
        await emitter.emit(status_event)

        stats_event = StatisticsUpdateEvent({"requests": 100, "tokens": 5000})
        await emitter.emit(stats_event)

    asyncio.run(test())