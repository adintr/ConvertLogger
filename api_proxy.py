#!/usr/bin/env python3
"""
Anthropic API代理终端
将终端划分为三个区域显示不同内容
"""

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
import re as _re_markup
from textual.widgets import (
    Header, Footer, Static, TextArea,
    DataTable, Input, Label, TabbedContent, TabPane
)
from textual.binding import Binding
from datetime import datetime
import asyncio
import time
import json as json_module
from typing import Dict, Any, List, Optional

# 导入服务器管理和事件模块
from server_manager import ServerManager, ServerStatus

# 导入配置管理
from config import load_config


class APIProxyApp(App):
    """API代理终端应用 - 三区域布局"""

    CSS_PATH = "app.css"
    BINDINGS = [
        Binding("ctrl+c", "quit", "退出", show=True),
        Binding("ctrl+r", "refresh", "刷新", show=True),
        Binding("ctrl+l", "clear_log", "清空日志", show=True),
    ]

    def __init__(self):
        super().__init__()

        # 统计信息
        self.token_stats = {
            "total_calls": 0,
            "total_tokens": 0,
            "success_calls": 0,
            "failed_calls": 0,
        }
        self.history: List[Dict[str, Any]] = []

        # 服务器管理
        self.server_manager = ServerManager(config_path="config.yaml")
        self.server_manager.register_event_handler(self._handle_server_event)

        # 实时统计（来自API服务器）
        self.real_time_stats = {
            "total_requests": 0,
            "total_tokens": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "providers": {},
            "models": {},
            "last_update": 0
        }

        # 服务器状态
        self.server_status = {
            "running": False,
            "ws_connected": False,
            "last_heartbeat": 0,
            "start_time": 0,
            "uptime": 0,
            "active_clients": 0,
        }

        # 当前选择的转发方案
        self.current_scheme: Optional[str] = None

        # 方案交互选择模式状态
        self.select_mode = None  # None, 'scheme', 'error'
        self.select_schemes = []  # 缓存的方案列表

        # 上游错误暂停队列：error_id -> 事件摘要（用于用户决策）
        # key: error_id, value: dict with provider/model/status_code/error_message
        self.pending_error_ids: list = []  # 有序列表，方便用数字选择

        # 请求/响应面板：追踪当前"活跃"的 trace_id，用于检测中途插入
        # 当 forwarding/provider_response/client_response 到达时，
        # 若 _traffic_current_trace 不等于本 trace_id，说明期间有其它请求插入，
        # 需要新开一个"继续响应"记录头。
        self._traffic_current_trace: Optional[str] = None

    async def _handle_server_event(self, event: Dict[str, Any]):
        """处理服务器事件"""
        event_type = event.get("type")
        event_data = event.get("data", {})

        try:
            if event_type == "server_status":
                # 更新服务器状态
                self.server_status.update(event_data)
                if "running" in event_data:
                    self.server_status["running"] = event_data["running"]
                if "ws_connected" in event_data:
                    self.server_status["ws_connected"] = event_data["ws_connected"]
                if "last_heartbeat" in event_data:
                    self.server_status["last_heartbeat"] = event_data["last_heartbeat"]
                if "start_time" in event_data:
                    self.server_status["start_time"] = event_data["start_time"]

                # 更新UI显示
                self.update_token_display()
                self.update_title()

                # 在日志中显示状态变化
                if event_data.get("status") == "connected":
                    log = self.query_one("#api-log", TextArea)
                    self._append(log, f"[green]✅ WebSocket连接成功[/green]")
                elif event_data.get("status") == "shutting_down":
                    log = self.query_one("#api-log", TextArea)
                    self._append(log, f"[yellow]⚠️  服务器正在关闭...[/yellow]")

            elif event_type == "statistics_update":
                # 更新实时统计
                self.real_time_stats.update(event_data)
                self.real_time_stats["last_update"] = time.time()

                # 合并到本地token_stats（向后兼容）
                self.token_stats["total_calls"] = event_data.get("total_requests", 0)
                self.token_stats["total_tokens"] = event_data.get("total_tokens", 0)
                self.token_stats["success_calls"] = event_data.get("successful_requests", 0)
                self.token_stats["failed_calls"] = event_data.get("failed_requests", 0)

                # 更新UI显示
                self.update_token_display()
                self.update_title()

            elif event_type == "request_log":
                # 显示API请求日志（操作 Tab 摘要）
                log = self.query_one("#api-log", TextArea)

                provider = event_data.get("provider", "unknown")
                model = event_data.get("model", "unknown")
                status_code = event_data.get("status_code", 0)
                success = event_data.get("success", False)
                response_time = event_data.get("response_time", 0)
                tokens = event_data.get("total_tokens", 0)

                status_icon = "✅" if success else "❌"
                status_color = "green" if success else "red"

                timestamp = datetime.fromtimestamp(event_data.get("timestamp", time.time())).strftime("%H:%M:%S")

                self._append(log, f"[{status_color}]{status_icon} [{timestamp}] {provider}: {model} (HTTP {status_code}) - {response_time:.2f}s - {tokens} tokens[/{status_color}]")

                # 添加到历史记录（保留完整 event_data 供详情使用）
                history_item = {
                    "time": timestamp,
                    "model": model,
                    "tokens": tokens,
                    "status": "success" if success else "failed",
                    "provider": provider,
                    "_raw": event_data,  # 保存原始数据供详情 Tab 使用
                }
                self.history.append(history_item)

                # 更新历史表格
                table = self.query_one("#history-table", DataTable)
                status_display = "✅ 成功" if success else "❌ 失败"
                table.add_row(
                    timestamp,
                    f"{model} ({provider})",
                    str(tokens),
                    status_display
                )

            elif event_type == "traffic_chunk":
                # 实时显示请求/响应各阶段数据
                traffic_log = self.query_one("#traffic-log", TextArea)
                phase = event_data.get("phase", "")
                trace_id = event_data.get("trace_id", "")
                provider = event_data.get("provider", "unknown")
                model = event_data.get("model", "")
                timestamp = datetime.fromtimestamp(event_data.get("timestamp", time.time())).strftime("%H:%M:%S")

                if phase == "client_request":
                    method = event_data.get("method", "POST")
                    incoming_url = event_data.get("incoming_url", "")
                    # 新请求到来时更新当前活跃 trace
                    self._traffic_current_trace = trace_id
                    self._append(traffic_log, f"\n[bold yellow]{'─' * 60}[/bold yellow]")
                    self._append(traffic_log, f"[bold yellow]▶ [{timestamp}] 客户端请求 → 代理[/bold yellow]  [dim]model: {model}  provider: {provider}[/dim]")
                    self._append(traffic_log, f"  [dim]URL:[/dim]  {method} {incoming_url}")
                    self._append(traffic_log, f"  [dim]Headers:[/dim]")
                    self._write_headers(traffic_log, event_data.get("incoming_headers", {}))
                    self._append(traffic_log, f"  [dim]Body:[/dim]")
                    self._write_body(traffic_log, event_data.get("request_body"))

                elif phase == "forwarding":
                    method = event_data.get("method", "POST")
                    url = event_data.get("url", "")
                    # 若当前活跃 trace 已被其它请求抢占，新开一条记录
                    if trace_id and self._traffic_current_trace != trace_id:
                        self._append(traffic_log, f"\n[bold yellow]{'─' * 60}[/bold yellow]")
                        self._append(traffic_log, f"[bold yellow]↩ [{timestamp}] 继续响应[/bold yellow]  [dim]model: {model}  provider: {provider}[/dim]")
                        self._traffic_current_trace = trace_id
                    self._append(traffic_log, f"\n[bold cyan]▶ [{timestamp}] 转发请求 → {provider}[/bold cyan]")
                    self._append(traffic_log, f"  [dim]URL:[/dim]  {method} {url}")
                    self._append(traffic_log, f"  [dim cyan](等待响应...)[/dim cyan]")

                elif phase == "provider_response":
                    status_code = event_data.get("status_code", 0)
                    response_time = event_data.get("response_time", 0)
                    color = "green" if status_code < 400 else "red"
                    status_str = f"HTTP {status_code}" if status_code else "网络错误"
                    # 若当前活跃 trace 已被其它请求抢占，新开一条记录
                    if trace_id and self._traffic_current_trace != trace_id:
                        self._append(traffic_log, f"\n[bold yellow]{'─' * 60}[/bold yellow]")
                        self._append(traffic_log, f"[bold yellow]↩ [{timestamp}] 继续响应[/bold yellow]  [dim]model: {model}  provider: {provider}[/dim]")
                        self._traffic_current_trace = trace_id
                    # 补充显示转发请求的 headers（首次有数据时）
                    fwd_hdrs = event_data.get("forwarded_headers", {})
                    if fwd_hdrs:
                        self._append(traffic_log, f"  [dim]转发 Headers:[/dim]")
                        self._write_headers(traffic_log, fwd_hdrs)
                    self._append(traffic_log, f"\n[bold {color}]◀ [{timestamp}] Provider 响应 ← {provider}[/bold {color}]  [{color}]{status_str}[/{color}]  [dim]{response_time:.2f}s[/dim]")
                    self._append(traffic_log, f"  [dim]响应 Headers:[/dim]")
                    self._write_headers(traffic_log, event_data.get("response_headers", {}))
                    self._append(traffic_log, f"  [dim]Body:[/dim]")
                    self._write_body(traffic_log, event_data.get("response_body"))

                elif phase == "client_response":
                    status_code = event_data.get("status_code", 0)
                    response_time = event_data.get("response_time", 0)
                    color = "green" if status_code < 400 else "red"
                    # 若当前活跃 trace 已被其它请求抢占，新开一条记录
                    if trace_id and self._traffic_current_trace != trace_id:
                        self._append(traffic_log, f"\n[bold yellow]{'─' * 60}[/bold yellow]")
                        self._append(traffic_log, f"[bold yellow]↩ [{timestamp}] 继续响应[/bold yellow]  [dim]model: {model}  provider: {provider}[/dim]")
                        self._traffic_current_trace = trace_id
                    self._append(traffic_log, f"\n[bold magenta]◀ [{timestamp}] 发送给客户端[/bold magenta]  [{color}]HTTP {status_code}[/{color}]  [dim]{response_time:.2f}s[/dim]")
                    self._append(traffic_log, f"  [dim]Headers:[/dim]")
                    self._write_headers(traffic_log, event_data.get("client_response_headers", {}))
                    self._append(traffic_log, f"  [dim]Body:[/dim]")
                    self._write_sse_body(traffic_log, event_data.get("client_response_body", ""))

            elif event_type == "providers_info":
                # 显示 provider 详情列表
                log = self.query_one("#api-log", TextArea)
                providers = event_data.get("providers", [])
                total = event_data.get("total", len(providers))
                self._append(log, f"\n[bold]📋 Provider 列表 (共{total}个):[/bold]")
                for p in providers:
                    status_icon = "🟢" if p.get("enabled") else "🔴"
                    self._append(log, f"\n  {status_icon} [bold cyan]{p['name']}[/bold cyan]  (type: {p['type']})")
                    self._append(log, f"     base_url:  {p.get('base_url', 'N/A')}")
                    if p.get("proxy_enabled"):
                        self._append(log, f"     proxy:     {p.get('proxy_url', 'N/A')}")
                    self._append(log, f"     timeout:   {p.get('timeout', 'N/A')}s")
                    self._append(log, f"     请求次数:  {p.get('request_count', 0)}  错误次数: {p.get('error_count', 0)}")
                    models = p.get("models", [])
                    if models:
                        self._append(log, f"     模型列表 ({len(models)} 个):")
                        for m in models:
                            self._append(log, f"       - {m}")
                    else:
                        self._append(log, f"     模型列表: [dim](未配置)[/dim]")

            elif event_type == "command_response":
                action = event_data.get("action", "")
                if action == "reload":
                    log = self.query_one("#api-log", TextArea)
                    providers = event_data.get("providers", {})
                    schemes = event_data.get("schemes", {})
                    added = providers.get("added", [])
                    removed = providers.get("removed", [])
                    updated = providers.get("updated", [])
                    self._append(log, f"[green]✅ 热重载完成[/green]  providers总计: {providers.get('total', '?')}  schemes总计: {schemes.get('total', '?')} (默认: {schemes.get('default', 'N/A')})")
                    if added:
                        self._append(log, f"   [green]新增 provider:[/green] {', '.join(added)}")
                    if removed:
                        self._append(log, f"   [red]移除 provider:[/red] {', '.join(removed)}")
                    if updated:
                        self._append(log, f"   [cyan]更新 provider:[/cyan] {', '.join(updated)}")
                    if not added and not removed and not updated:
                        self._append(log, f"   [dim]配置无变化[/dim]")
                    fallback = event_data.get("scheme_fallback")
                    if fallback:
                        self._append(log, f"   [yellow]⚠️  当前方案 '{fallback}' 已失效，已回退到默认方案[/yellow]")

            elif event_type == "client_connection":
                # 客户端连接/断开事件
                event_action = event_data.get("event")
                client_ip = event_data.get("client_ip", "unknown")
                path = event_data.get("path", "")
                active_clients = event_data.get("active_clients", 0)
                self.server_status["active_clients"] = active_clients

                log = self.query_one("#api-log", TextArea)
                if event_action == "connected":
                    self._append(log, f"[cyan]🔌 客户端连接: {client_ip} → {path}  (活跃连接: {active_clients})[/cyan]")
                else:
                    self._append(log, f"[dim]🔌 客户端断开: {client_ip} ← {path}  (活跃连接: {active_clients})[/dim]")

                self.update_token_display()

            elif event_type == "config_reloaded":
                # 广播消息（其他 WebSocket 客户端收到），本客户端已由 command_response 处理，忽略
                pass

            elif event_type == "upstream_error":
                # 上游API错误，需要用户决策
                log = self.query_one("#api-log", TextArea)
                error_id = event_data.get("error_id", "")
                provider = event_data.get("provider", "unknown")
                model = event_data.get("model", "unknown")
                status_code = event_data.get("status_code", 0)
                error_type = event_data.get("error_type", "unknown")
                error_message = event_data.get("error_message", "")

                # 记录到待处理列表
                self.pending_error_ids.append({
                    "error_id": error_id,
                    "provider": provider,
                    "model": model,
                    "status_code": status_code,
                    "error_type": error_type,
                    "error_message": error_message,
                })
                idx = len(self.pending_error_ids)

                # 切换到操作 Tab 并醒目展示
                tabs = self.query_one("#main-tabs", TabbedContent)
                tabs.active = "tab-operation"

                self._append(log, f"")
                self._append(log, f"[bold red]{'─' * 60}[/bold red]")
                self._append(log, f"[bold red]⚠️  上游API错误 — 请求已暂停，等待您的处理[/bold red]")
                self._append(log, f"[bold red]{'─' * 60}[/bold red]")
                self._append(log, f"  [bold]Provider:[/bold] {provider}")
                self._append(log, f"  [bold]模型:[/bold]     {model}")
                self._append(log, f"  [bold]错误类型:[/bold] {error_type}")
                status_str = f"HTTP {status_code}" if status_code else "网络错误"
                self._append(log, f"  [bold]状态:[/bold]     {status_str}")
                self._append(log, f"  [bold]错误信息:[/bold] {error_message[:200]}")
                self._append(log, f"")
                self._append(log, f"  [bold yellow]编号 [{idx}]  可用操作:[/bold yellow]")
                self._append(log, f"    [cyan]1[/cyan] 将错误直接返回给客户端")
                self._append(log, f"")
                self._append(log, f"  [dim]输入 'resolve error {idx} 1' 执行对应操作[/dim]")
                self._append(log, f"[bold red]{'─' * 60}[/bold red]")

                # 进入错误决策选择模式
                self.select_mode = 'error'
                input_widget = self.query_one("#command-input", Input)
                input_widget.placeholder = f"输入 'resolve error {idx} 1' 处理错误，或 'resolve error {idx} <选项号>'..."

            elif event_type == "server_start_error":
                # 服务器进程意外退出，显示错误输出
                self.server_status["running"] = False
                self.update_token_display()
                self.update_title()
                log = self.query_one("#api-log", TextArea)
                output = event_data.get("stderr", "").strip()
                self._append(log, f"[red]❌ 服务器进程崩溃，错误输出:[/red]")
                if output:
                    for line in output.splitlines()[-30:]:
                        self._append(log, f"[red]  {line}[/red]")
                else:
                    self._append(log, f"[red]  （无错误输出，请检查 config.yaml）[/red]")

            elif event_type == "error":
                # 显示错误信息
                log = self.query_one("#api-log", TextArea)
                error_msg = event_data.get("message", "未知错误")
                self._append(log, f"[red]❌ 服务器错误: {error_msg}[/red]")

        except Exception as e:
            # 防止事件处理异常影响应用
            log = self.query_one("#api-log", TextArea)
            self._append(log, f"[yellow]⚠️  事件处理异常: {e}[/yellow]")

    def _show_server_stderr(self, log):
        """显示服务器进程的 stderr"""
        stderr = self.server_manager.last_start_error.strip()
        if stderr:
            self._append(log, f"[red]── 服务器错误输出 ──[/red]")
            for line in stderr.splitlines()[-30:]:
                self._append(log, f"[red]  {line}[/red]")

    async def _auto_start_server(self):
        """自动启动API服务器"""
        log = self.query_one("#api-log", TextArea)

        # 等待一小段时间让UI完全初始化
        await asyncio.sleep(1)

        self._append(log, f"[dim]正在自动启动API服务器...[/dim]")

        try:
            success = await self.server_manager.start_server()
            if success:
                self._append(log, f"[green]✅ API服务器启动成功[/green]")
                self._append(log, f"[dim]服务器PID: {self.server_manager.status.pid}[/dim]")
                self._append(log, f"[dim]WebSocket连接中...[/dim]")
                self._append(log, "")
                self._append(log, f"[bold]💡 快速开始:[/bold]")
                self._append(log, f"  1. 输入 'test' 模拟API调用测试")
                self._append(log, f"  2. 观察右上角实时统计更新")
                self._append(log, f"  3. 输入 'help' 查看所有命令")
                self._append(log, "")
                self._append(log, f"[dim]提示: 服务器已自动启动，您可以直接开始测试[/dim]")
            else:
                self._append(log, f"[red]❌ API服务器启动失败[/red]")
                self._show_server_stderr(log)
                self._append(log, f"[dim]您仍可以手动输入 'start server' 尝试启动[/dim]")
        except Exception as e:
            self._append(log, f"[red]❌ 启动服务器时发生错误: {e}[/red]")

    def compose(self) -> ComposeResult:
        """创建UI布局"""
        yield Header()

        # 主容器：左右分割
        with Horizontal(id="main-container"):
            # 左侧：主区域 (75%宽度) - Tab切换
            with Container(id="left-panel"):
                with TabbedContent(id="main-tabs"):
                    # Tab 1: 操作
                    with TabPane("操作", id="tab-operation"):
                        yield TextArea(id="api-log", read_only=True, show_line_numbers=False, soft_wrap=True)
                        yield Input(
                            placeholder="输入命令 (help查看帮助)...",
                            id="command-input"
                        )

                    # Tab 2: 请求/响应
                    with TabPane("请求/响应", id="tab-traffic"):
                        yield TextArea(id="traffic-log", read_only=True, show_line_numbers=False, soft_wrap=True)

                    # Tab 3: 详情
                    with TabPane("详情", id="tab-detail"):
                        yield TextArea(id="detail-log", read_only=True, show_line_numbers=False, soft_wrap=True)

            # 右侧：辅助区域 (25%宽度)
            with Vertical(id="right-panel"):
                # 右上角：token统计 (30%高度)
                with Container(id="token-stats-panel", classes="panel"):
                    yield Label("📊 Token统计", classes="panel-title")
                    yield Static(id="token-display")

                # 右下角：历史摘要 (70%高度)
                with Container(id="history-panel", classes="panel"):
                    yield Label("📜 历史对话", classes="panel-title")
                    yield DataTable(id="history-table", cursor_type="row")

        yield Footer()

    def on_mount(self) -> None:
        """应用启动时初始化"""
        # 初始化标题
        self.update_title()

        # 初始化历史表格
        table = self.query_one("#history-table", DataTable)
        table.add_columns("时间", "模型", "Tokens", "状态")
        table.add_rows([])

        # 初始化token显示
        self.update_token_display()

        # 显示欢迎信息
        log = self.query_one("#api-log", TextArea)
        self._append(log, f"[bold cyan]🚀 Anthropic API代理终端 v2.0 已启动[/bold cyan]")
        self._append(log, f"[dim]当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
        self._append(log, f"[dim]集成功能: 实时API监控、服务器控制、WebSocket通信[/dim]")
        self._append(log, f"[dim]输入 'help' 查看可用命令[/dim]")
        self._append(log, "")
        self._append(log, f"[bold]💡 自动启动API服务器中...[/bold]")

        # 初始化请求/响应 Tab
        traffic_log = self.query_one("#traffic-log", TextArea)
        self._append(traffic_log, f"[bold cyan]📡 请求/响应实时监控[/bold cyan]")
        self._append(traffic_log, f"[dim]等待服务器启动后，所有代理请求和响应将在此实时显示...[/dim]")
        self._append(traffic_log, f"[dim]包含完整的 HTTP Header 和 Body 内容[/dim]")

        # 初始化详情 Tab
        detail_log = self.query_one("#detail-log", TextArea)
        self._append(detail_log, f"[bold cyan]🔍 请求详情查看器[/bold cyan]")
        self._append(detail_log, f"[dim]点击右侧历史记录中的任意一条，将在此处显示该请求的完整详情[/dim]")
        self._append(detail_log, f"[dim]类似 Chrome DevTools → Network 面板[/dim]")

        # 设置焦点到命令输入框
        input_widget = self.query_one("#command-input", Input)
        input_widget.focus()

        # 异步启动服务器
        asyncio.create_task(self._auto_start_server())

    # ── TextArea 辅助方法 ────────────────────────────────────────

    @staticmethod
    def _strip_markup(text: str) -> str:
        """去掉 Rich markup 标签，保留纯文字"""
        return _re_markup.sub(r'\[/?[^\[\]]*\]', '', str(text))

    def _append(self, widget: TextArea, line: str) -> None:
        """向只读 TextArea 末尾追加一行文字（去除 Rich markup）"""
        plain = self._strip_markup(line)
        end = widget.document.end
        widget.insert(plain + "\n", location=end)
        widget.scroll_end(animate=False)

    def _clear_log(self, widget: TextArea) -> None:
        """清空 TextArea 内容"""
        widget.load_text("")

    # ── 写入辅助方法 ─────────────────────────────────────────────

    def _write_headers(self, log: TextArea, headers: Dict[str, Any], indent: str = "    ") -> None:
        """辅助：写入 headers，无内容时显示(无)"""
        if headers:
            for k, v in headers.items():
                self._append(log, f"{indent}{k}: {v}")
        else:
            self._append(log, f"{indent}(无)")

    def _write_body(self, log: TextArea, body: Any, indent: str = "    ") -> None:
        """辅助：写入 body，JSON 格式化，不截断"""
        if body is None or body == "":
            self._append(log, f"{indent}(无)")
            return
        if isinstance(body, (dict, list)):
            try:
                body_str = json_module.dumps(body, ensure_ascii=False, indent=2)
            except Exception:
                body_str = str(body)
        else:
            body_str = str(body)
        for line in body_str.splitlines():
            self._append(log, f"{indent}{line}")

    def _write_sse_body(self, log: TextArea, body: Any, indent: str = "    ") -> None:
        """辅助：写入 SSE body，连续 ping 事件折叠为 ping ×N 显示，其余完整输出"""
        if body is None or body == "":
            self._append(log, f"{indent}(无)")
            return
        if isinstance(body, (dict, list)):
            # 非 SSE 格式，直接用 _write_body 处理
            self._write_body(log, body, indent)
            return

        body_str = str(body)
        # 将 SSE 文本按事件块（双换行）分割
        # 每个 SSE 事件以 \n\n 结尾；分割后过滤空块
        events = [e for e in _re_markup.split(r'\n\n', body_str) if e.strip()]

        PING_LINE = "event: ping"
        pending_ping_count = 0

        def flush_pings():
            nonlocal pending_ping_count
            if pending_ping_count > 0:
                label = f"ping ×{pending_ping_count}" if pending_ping_count > 1 else "ping"
                self._append(log, f"{indent}{label}")
                pending_ping_count = 0

        for event in events:
            lines = event.splitlines()
            is_ping = any(ln.strip() == PING_LINE for ln in lines)
            if is_ping:
                pending_ping_count += 1
            else:
                flush_pings()
                for ln in lines:
                    self._append(log, f"{indent}{ln}")

        flush_pings()

    def _write_traffic_log(self, log: TextArea, data: Dict[str, Any], timestamp: str, status_color: str, status_icon: str) -> None:
        """在请求/响应 Tab 写入详细流量信息"""
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")
        method = data.get("method", "POST")
        url = data.get("url", "")
        status_code = data.get("status_code", 0)
        response_time = data.get("response_time", 0)
        tokens = data.get("total_tokens", 0)

        self._append(log, "")
        self._append(log, f"{'─' * 60}")
        self._append(log, f"{status_icon} [{timestamp}] {method} → {provider} | HTTP {status_code} | {response_time:.2f}s")

        incoming_url = data.get("incoming_url", "")

        # ── 1. 客户端 → 代理：原始请求 ──────────────────────────
        self._append(log, "")
        self._append(log, f"▶ 客户端请求 (→ 代理)")
        self._append(log, f"  URL:    {method} {incoming_url or '(未知)'}")
        self._append(log, f"  模型:   {model}")
        self._append(log, f"  Headers:")
        self._write_headers(log, data.get("incoming_headers", {}))
        self._append(log, f"  Body:")
        self._write_body(log, data.get("request_body"))

        # ── 2. 代理 → Provider：转发请求 ─────────────────────────
        self._append(log, "")
        self._append(log, f"▶ 转发请求 (→ {provider})")
        self._append(log, f"  URL:    {method} {url or '(未知)'}")
        self._append(log, f"  Headers:")
        self._write_headers(log, data.get("forwarded_headers", {}))

        # ── 3. Provider → 代理：响应 ─────────────────────────────
        self._append(log, "")
        self._append(log, f"◀ Provider 响应 (← {provider})")
        self._append(log, f"  状态:   HTTP {status_code}")
        self._append(log, f"  Tokens: {tokens}")
        self._append(log, f"  Headers:")
        self._write_headers(log, data.get("response_headers", {}))
        self._append(log, f"  Body:")
        self._write_body(log, data.get("response_body"))

        # ── 4. 代理 → 客户端：最终响应 ───────────────────────────
        self._append(log, "")
        self._append(log, f"◀ 发送给客户端 (→ 原始调用方)")
        self._append(log, f"  状态:   HTTP {status_code}")
        self._append(log, f"  Headers:")
        self._write_headers(log, data.get("client_response_headers", {}))
        self._append(log, f"  Body:")
        self._write_body(log, data.get("client_response_body"))

        error_msg = data.get("error_message")
        if error_msg:
            self._append(log, f"  错误: {error_msg}")

    def _write_detail_view(self, log: TextArea, item: Dict[str, Any]) -> None:
        """在详情 Tab 显示类似 Chrome DevTools Network 面板的请求详情"""
        self._clear_log(log)
        data = item.get("_raw", {})
        timestamp = item.get("time", "N/A")
        provider = item.get("provider", "unknown")
        model = item.get("model", "unknown")
        status = item.get("status", "unknown")
        tokens = item.get("tokens", 0)

        method = data.get("method", "POST")
        url = data.get("url", "")
        status_code = data.get("status_code", 0)
        response_time = data.get("response_time", 0)
        success = data.get("success", status == "success")

        status_icon = "✅" if success else "❌"

        # 总览
        self._append(log, f"{'═' * 60}")
        self._append(log, f"  {status_icon} 请求详情")
        self._append(log, f"{'═' * 60}")
        self._append(log, "")
        self._append(log, "▍ 概览")
        self._append(log, f"  时间:       {timestamp}")
        self._append(log, f"  Provider:   {provider}")
        self._append(log, f"  模型:       {model}")
        self._append(log, f"  状态:       HTTP {status_code} ({'成功' if success else '失败'})")
        self._append(log, f"  耗时:       {response_time:.3f}s")
        self._append(log, f"  Tokens:     {tokens}")
        self._append(log, f"  方法:       {method}")
        incoming_url = data.get("incoming_url", "")
        if incoming_url:
            self._append(log, f"  客户端URL:  {incoming_url}")
        if url:
            self._append(log, f"  转发URL:    {url}")

        # ── 1. 客户端 → 代理：原始请求 ───────────────────────────
        self._append(log, "")
        self._append(log, "▍ 1. 客户端请求 Headers (→ 代理)")
        self._write_headers(log, data.get("incoming_headers", {}), indent="  ")

        self._append(log, "")
        self._append(log, "▍ 2. 客户端请求 Body")
        self._write_body(log, data.get("request_body"), indent="  ")

        # ── 2. 代理 → Provider：转发请求 ──────────────────────────
        self._append(log, "")
        self._append(log, f"▍ 3. 转发请求 Headers (→ {provider})")
        self._write_headers(log, data.get("forwarded_headers", {}), indent="  ")

        # ── 3. Provider → 代理：响应 ──────────────────────────────
        self._append(log, "")
        self._append(log, f"▍ 4. Provider 响应 Headers (← {provider})")
        self._write_headers(log, data.get("response_headers", {}), indent="  ")

        self._append(log, "")
        self._append(log, "▍ 5. Provider 响应 Body")
        self._write_body(log, data.get("response_body"), indent="  ")

        # ── 4. 代理 → 客户端：最终响应 ────────────────────────────
        self._append(log, "")
        self._append(log, "▍ 6. 发送给客户端 Headers")
        self._write_headers(log, data.get("client_response_headers", {}), indent="  ")

        self._append(log, "")
        self._append(log, "▍ 7. 发送给客户端 Body")
        self._write_body(log, data.get("client_response_body"), indent="  ")

        # 错误信息
        error_msg = data.get("error_message")
        if error_msg:
            self._append(log, "")
            self._append(log, "▍ 错误信息")
            self._append(log, f"  {error_msg}")

        self._append(log, "")
        self._append(log, f"{'═' * 60}")
        log.move_cursor((0, 0))

    def update_token_display(self) -> None:
        """更新token统计显示和服务器状态"""
        stats = self.token_stats
        display = self.query_one("#token-display", Static)

        # 服务器状态指示器
        server_status = self.server_status
        server_icon = "🟢" if server_status["running"] else "🔴"
        server_text = "运行中" if server_status["running"] else "已停止"

        ws_icon = "📡" if server_status["ws_connected"] else "📴"
        ws_text = "已连接" if server_status["ws_connected"] else "未连接"

        # 计算运行时间
        uptime = 0
        if server_status["start_time"] > 0:
            uptime = time.time() - server_status["start_time"]
            uptime_str = self._format_uptime(uptime)
        else:
            uptime_str = "N/A"

        active_clients = server_status.get("active_clients", 0)
        clients_color = "green" if active_clients > 0 else "dim"

        content = (
            f"[bold]{server_icon} 服务器状态: {server_text}[/bold]\n"
            f"[bold]{ws_icon} WebSocket: {ws_text}[/bold]\n"
            f"[bold]⏱️  运行时间: {uptime_str}[/bold]\n"
            f"[bold {clients_color}]🔌 活跃连接: {active_clients}[/bold {clients_color}]\n"
            f"────────────────\n"
            f"[bold]📊 统计信息:[/bold]\n"
            f"[bold]总请求:[/bold] {stats['total_calls']}\n"
            f"[bold]总Tokens:[/bold] {stats['total_tokens']:,}\n"
            f"[bold green]成功:[/bold green] {stats['success_calls']}\n"
            f"[bold red]失败:[/bold red] {stats['failed_calls']}\n"
            f"[dim]更新时间: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )
        display.update(content)

    def _format_uptime(self, seconds: float) -> str:
        """格式化运行时间"""
        if seconds < 60:
            return f"{seconds:.0f}秒"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.0f}分"
        elif seconds < 86400:
            hours = seconds / 3600
            return f"{hours:.1f}小时"
        else:
            days = seconds / 86400
            return f"{days:.1f}天"

    def update_title(self) -> None:
        """更新标题栏显示代理入口地址和当前方案"""
        # 获取服务器地址
        host = self.server_manager.status.host
        port = self.server_manager.status.port
        address = f"{host}:{port}" if host and port else "未启动"

        # 显示当前方案
        if self.current_scheme:
            scheme_display = self.current_scheme
        else:
            try:
                config = load_config(self.server_manager.config_path)
                default = config.get_default_scheme()
                scheme_display = f"{default.name} (默认)" if default else "无方案"
            except Exception:
                scheme_display = "无方案"

        self.title = f"Anthropic API代理终端 v2.0 | {address} | 方案: {scheme_display}"
        self.sub_title = ""

    @on(DataTable.RowSelected, "#history-table")
    def on_history_row_selected(self, event: DataTable.RowSelected) -> None:
        """处理历史记录点击事件 - 在详情 Tab 展示完整请求/响应信息"""
        if event.row_index is not None and event.row_index < len(self.history):
            history_item = self.history[event.row_index]

            # 切换到详情 Tab
            tabs = self.query_one("#main-tabs", TabbedContent)
            tabs.active = "tab-detail"

            # 在详情 Tab 展示 Chrome DevTools 风格的详情
            detail_log = self.query_one("#detail-log", TextArea)
            self._write_detail_view(detail_log, history_item)

    @on(Input.Submitted, "#command-input")
    async def on_command_submitted(self, event: Input.Submitted) -> None:
        """处理用户命令输入"""
        command = event.value.strip()
        event.input.value = ""  # 清空输入框

        if not command:
            return

        log = self.query_one("#api-log", TextArea)

        # 检查是否处于选择模式
        if self.select_mode is not None:
            await self._handle_selection_input(command, log)
            return

        # 处理命令
        if command.lower() == "help":
            self.show_help()
        elif command.lower() == "clear":
            self.action_clear_log()
        elif command.lower() == "stats":
            self.show_stats()
        elif command.lower() == "test":
            await self.simulate_api_call()
        elif command.lower() == "add":
            self.add_test_history()
        elif command.lower() in ["exit", "quit"]:
            self._append(log, f"[dim]正在退出程序...[/dim]")
            await asyncio.sleep(0.5)  # 短暂延迟让用户看到消息

            # 停止服务器进程（如果正在运行）
            if self.server_status["running"]:
                self._append(log, f"[dim]正在停止API服务器...[/dim]")
                try:
                    success = await self.server_manager.stop_server()
                    if success:
                        self._append(log, f"[green]✅ API服务器已停止[/green]")
                    else:
                        self._append(log, f"[yellow]⚠️  停止服务器失败，继续退出程序[/yellow]")
                except Exception as e:
                    self._append(log, f"[yellow]⚠️  停止服务器时发生错误: {e}，继续退出程序[/yellow]")
                await asyncio.sleep(0.3)  # 短暂延迟让用户看到消息

            self.exit()
        elif command.lower() == "cancel":
            # 取消选择模式
            if self.select_mode is not None:
                self.select_mode = None
                self.select_schemes = []
                input_widget = self.query_one("#command-input", Input)
                input_widget.placeholder = "输入命令 (help查看帮助)..."
                self._append(log, f"[dim]选择已取消[/dim]")
            else:
                self._append(log, f"[dim]当前不在选择模式[/dim]")

        # 服务器控制命令
        elif command.lower() == "start server":
            await self._handle_start_server()
        elif command.lower() == "stop server":
            await self._handle_stop_server()
        elif command.lower() == "restart server":
            await self._handle_restart_server()
        elif command.lower() == "server status":
            await self._handle_server_status()
        elif command.lower() == "server info":
            await self._handle_server_info()
        elif command.lower() == "reload":
            await self._handle_reload(log)

        # 方案选择命令
        elif command.lower() == "list schemes":
            await self._handle_list_schemes(log)
        elif command.lower() == "current scheme":
            await self._handle_current_scheme(log)
        elif command.lower() == "select scheme":
            await self._handle_select_scheme_interactive(log)
        elif command.lower().startswith("select scheme "):
            scheme_name = command[len("select scheme "):].strip()
            await self._handle_select_scheme(scheme_name, log)

        # provider 命令
        elif command.lower() == "list providers":
            await self._handle_list_providers(log)

        # 模型更新命令
        elif command.lower().startswith("update models "):
            provider_name = command[len("update models "):].strip()
            await self._handle_update_models(provider_name, log)
        elif command.lower() == "update models":
            self._append(log, f"[yellow]⚠️  请指定 provider 名称，例如: update models anthropic_official[/yellow]")

        # 上游错误决策命令
        # 格式: resolve error <编号> <选项号>
        #  编号: pending_error_ids 列表中的位置（1-based）
        #  选项号: 1=将错误返回客户端
        elif command.lower().startswith("resolve error "):
            await self._handle_resolve_error(command[len("resolve error "):].strip(), log)
        elif command.lower() == "pending errors":
            await self._handle_pending_errors(log)

        else:
            self._append(log, f"[yellow]❓ 未知命令: {command}[/yellow]")
            self._append(log, f"[dim]输入 'help' 查看可用命令[/dim]")

    async def _handle_list_schemes(self, log: TextArea) -> None:
        """列出所有可用方案"""
        try:
            config = load_config(self.server_manager.config_path)
            if not config.schemes:
                self._append(log, f"[yellow]⚠️  配置文件中没有定义任何方案[/yellow]")
                return

            # 确定当前方案名
            current_name = self.current_scheme or (config.get_default_scheme().name if config.get_default_scheme() else None)

            self._append(log, f"\n[bold]📋 方案列表 (共{len(config.schemes)}个):[/bold]")
            for scheme in config.schemes:
                marker = "[bold green]*[/bold green] " if scheme.name == current_name else "  "
                self._append(log, f"  {marker}[bold]{scheme.name}[/bold]  {scheme.description}")

            self._append(log, f"\n[dim]使用 'select scheme <name>' 或 'select scheme' 切换方案[/dim]")
        except Exception as e:
            self._append(log, f"[red]❌ 列出方案时发生错误: {e}[/red]")

    async def _handle_current_scheme(self, log: TextArea) -> None:
        """显示当前方案及规则"""
        try:
            config = load_config(self.server_manager.config_path)
            scheme = None
            if self.current_scheme:
                scheme = config.get_scheme_by_name(self.current_scheme)
            if not scheme:
                scheme = config.get_default_scheme()

            if not scheme:
                self._append(log, f"[yellow]⚠️  当前没有可用方案[/yellow]")
                return

            is_default = not self.current_scheme or self.current_scheme == scheme.name
            suffix = " (默认)" if is_default and not self.current_scheme else ""
            self._append(log, f"\n[bold]当前方案: {scheme.name}{suffix}[/bold]")
            self._append(log, f"描述: {scheme.description}")
            self._append(log, f"规则 ({len(scheme.rules)} 条):")
            for i, rule in enumerate(scheme.rules, 1):
                self._append(log, f"  {i}. [cyan]{rule.model_pattern}[/cyan] -> [green]{rule.provider}[/green]:{rule.target_model}")
        except Exception as e:
            self._append(log, f"[red]❌ 获取方案信息时发生错误: {e}[/red]")

    async def _handle_select_scheme_interactive(self, log: TextArea) -> None:
        """不带参数的 select scheme：交互式列表选择"""
        try:
            config = load_config(self.server_manager.config_path)
            if not config.schemes:
                self._append(log, f"[yellow]⚠️  配置文件中没有定义任何方案[/yellow]")
                return

            self.select_mode = 'scheme'
            self.select_schemes = config.schemes

            self._append(log, f"\n[bold]📋 请选择方案 (输入序号):[/bold]")
            current_name = self.current_scheme or (config.get_default_scheme().name if config.get_default_scheme() else None)
            for i, scheme in enumerate(config.schemes, 1):
                marker = "[green]*[/green] " if scheme.name == current_name else "  "
                self._append(log, f"  {marker}[bold][{i}][/bold] {scheme.name}  {scheme.description}")

            self._append(log, f"\n[dim]输入 1-{len(config.schemes)} 选择，或输入 'cancel' 取消[/dim]")  # cancel 无连字符，保持不变
            input_widget = self.query_one("#command-input", Input)
            input_widget.placeholder = f"输入序号 (1-{len(config.schemes)})..."
        except Exception as e:
            self._append(log, f"[red]❌ 显示方案列表时发生错误: {e}[/red]")
            self.select_mode = None
            self.select_schemes = []

    async def _handle_select_scheme(self, scheme_name: str, log: TextArea) -> None:
        """切换到指定方案"""
        try:
            config = load_config(self.server_manager.config_path)
            scheme = config.get_scheme_by_name(scheme_name)
            if not scheme:
                available = [s.name for s in config.schemes]
                self._append(log, f"[red]❌ 找不到方案: {scheme_name}[/red]")
                self._append(log, f"[dim]可用方案: {', '.join(available)}[/dim]")
                return

            # 更新本地状态
            self.current_scheme = scheme.name
            self.update_title()

            # 同步到服务器
            if self.server_status["ws_connected"]:
                cmd_id = await self.server_manager.send_command("set scheme", {"scheme": scheme.name})
                if cmd_id:
                    self._append(log, f"[dim]命令已发送到服务器 (ID: {cmd_id})[/dim]")
                else:
                    self._append(log, f"[yellow]⚠️  发送命令到服务器失败，WebSocket可能未连接[/yellow]")
            else:
                self._append(log, f"[yellow]⚠️  WebSocket未连接，方案切换仅在本地生效[/yellow]")

            self._append(log, f"[green]✅ 已切换到方案: {scheme.name}[/green]")
            self._append(log, f"规则 ({len(scheme.rules)} 条):")
            for i, rule in enumerate(scheme.rules, 1):
                self._append(log, f"  {i}. [cyan]{rule.model_pattern}[/cyan] -> [green]{rule.provider}[/green]:{rule.target_model}")
        except Exception as e:
            self._append(log, f"[red]❌ 切换方案时发生错误: {e}[/red]")

    async def _handle_reload(self, log: TextArea) -> None:
        """热重载 providers 和 schemes"""
        if not self.server_status["ws_connected"]:
            self._append(log, f"[yellow]⚠️  WebSocket未连接，无法发送 reload 命令[/yellow]")
            return
        self._append(log, f"[dim]正在热重载配置...[/dim]")
        cmd_id = await self.server_manager.send_command("reload")
        if cmd_id:
            self._append(log, f"[dim]reload 命令已发送 (ID: {cmd_id})[/dim]")
        else:
            self._append(log, f"[red]❌ 发送 reload 命令失败[/red]")

    async def _handle_list_providers(self, log: TextArea) -> None:
        """列出所有 provider 的详细信息"""
        if not self.server_status["ws_connected"]:
            self._append(log, f"[yellow]⚠️  WebSocket未连接，无法获取 provider 信息[/yellow]")
            return
        self._append(log, f"[dim]正在获取 provider 列表...[/dim]")
        cmd_id = await self.server_manager.send_command("list providers")
        if not cmd_id:
            self._append(log, f"[red]❌ 发送 list providers 命令失败[/red]")

    async def _handle_update_models(self, provider_name: str, log: TextArea) -> None:
        """向 provider 查询可用模型列表并同步到配置"""
        if not provider_name:
            self._append(log, f"[yellow]⚠️  请指定 provider 名称，例如: update models anthropic_official[/yellow]")
            return
        if not self.server_status["ws_connected"]:
            self._append(log, f"[yellow]⚠️  WebSocket未连接，无法发送 update models 命令[/yellow]")
            return
        self._append(log, f"[dim]正在查询 provider '{provider_name}' 的模型列表...[/dim]")
        cmd_id = await self.server_manager.send_command("update models", {"provider": provider_name})
        if cmd_id:
            self._append(log, f"[dim]update models 命令已发送 (ID: {cmd_id})[/dim]")
        else:
            self._append(log, f"[red]❌ 发送 update models 命令失败[/red]")

    async def _handle_selection_input(self, command: str, log: TextArea) -> None:
        """处理选择模式下的用户输入（方案选择 / 错误决策）"""
        if command.lower() == "cancel":
            self.select_mode = None
            self.select_schemes = []
            input_widget = self.query_one("#command-input", Input)
            input_widget.placeholder = "输入命令 (help查看帮助)..."
            self._append(log, f"[dim]选择已取消[/dim]")
            return

        if self.select_mode == 'scheme':
            if not command.isdigit():
                self._append(log, f"[yellow]⚠️  请输入数字 1-{len(self.select_schemes)}[/yellow]")
                return
            index = int(command)
            if index < 1 or index > len(self.select_schemes):
                self._append(log, f"[yellow]⚠️  请输入有效数字 1-{len(self.select_schemes)}[/yellow]")
                return
            scheme = self.select_schemes[index - 1]
            self.select_mode = None
            self.select_schemes = []
            input_widget = self.query_one("#command-input", Input)
            input_widget.placeholder = "输入命令 (help查看帮助)..."
            await self._handle_select_scheme(scheme.name, log)

        elif self.select_mode == 'error':
            # 错误决策模式：支持完整的 resolve error 命令或简写（"<编号> <选项>"）
            cmd_lower = command.lower()
            if cmd_lower.startswith("resolve error "):
                await self._handle_resolve_error(command[len("resolve error "):].strip(), log)
            else:
                # 允许简写：直接输入 "<编号> <选项号>"
                await self._handle_resolve_error(command.strip(), log)

    async def _handle_start_server(self):
        """处理启动服务器命令"""
        log = self.query_one("#api-log", TextArea)

        if self.server_status["running"]:
            self._append(log, f"[yellow]⚠️  服务器已经在运行中[/yellow]")
            return

        self._append(log, f"[dim]正在启动API服务器...[/dim]")

        try:
            success = await self.server_manager.start_server()
            if success:
                self._append(log, f"[green]✅ API服务器启动成功[/green]")
                self._append(log, f"[dim]服务器PID: {self.server_manager.status.pid}[/dim]")
                self._append(log, f"[dim]WebSocket连接中...[/dim]")
            else:
                self._append(log, f"[red]❌ API服务器启动失败[/red]")
                self._show_server_stderr(log)
        except Exception as e:
            self._append(log, f"[red]❌ 启动服务器时发生错误: {e}[/red]")

    async def _handle_stop_server(self):
        """处理停止服务器命令"""
        log = self.query_one("#api-log", TextArea)

        if not self.server_status["running"]:
            self._append(log, f"[yellow]⚠️  服务器未运行[/yellow]")
            return

        self._append(log, f"[dim]正在停止API服务器...[/dim]")

        try:
            success = await self.server_manager.stop_server()
            if success:
                self._append(log, f"[green]✅ API服务器已停止[/green]")
            else:
                self._append(log, f"[red]❌ 停止服务器失败[/red]")
        except Exception as e:
            self._append(log, f"[red]❌ 停止服务器时发生错误: {e}[/red]")

    async def _handle_restart_server(self):
        """处理重启服务器命令"""
        log = self.query_one("#api-log", TextArea)

        self._append(log, f"[dim]正在重启API服务器...[/dim]")

        try:
            success = await self.server_manager.restart_server()
            if success:
                self._append(log, f"[green]✅ API服务器重启成功[/green]")
            else:
                self._append(log, f"[red]❌ 重启服务器失败[/red]")
        except Exception as e:
            self._append(log, f"[red]❌ 重启服务器时发生错误: {e}[/red]")

    async def _handle_server_status(self):
        """处理服务器状态命令"""
        log = self.query_one("#api-log", TextArea)

        status = self.server_status
        manager_status = self.server_manager.status

        self._append(log, f"\n[bold]📡 服务器状态信息[/bold]")
        self._append(log, f"  运行状态: {'🟢 运行中' if status['running'] else '🔴 已停止'}")
        self._append(log, f"  WebSocket: {'📡 已连接' if status['ws_connected'] else '📴 未连接'}")

        if status['start_time'] > 0:
            uptime = time.time() - status['start_time']
            uptime_str = self._format_uptime(uptime)
            self._append(log, f"  运行时间: {uptime_str}")

        if manager_status.pid:
            self._append(log, f"  进程ID: {manager_status.pid}")

        if status['last_heartbeat'] > 0:
            time_since_heartbeat = time.time() - status['last_heartbeat']
            self._append(log, f"  上次心跳: {time_since_heartbeat:.1f}秒前")

        self._append(log, f"  配置路径: {self.server_manager.config_path}")

    async def _handle_server_info(self):
        """处理服务器详细信息命令"""
        log = self.query_one("#api-log", TextArea)

        try:
            info = await self.server_manager.get_server_info()
            self._append(log, f"\n[bold]📋 服务器详细信息[/bold]")

            for key, value in info.items():
                if isinstance(value, dict):
                    self._append(log, f"  {key}:")
                    for sub_key, sub_value in value.items():
                        self._append(log, f"    {sub_key}: {sub_value}")
                else:
                    self._append(log, f"  {key}: {value}")
        except Exception as e:
            self._append(log, f"[red]❌ 获取服务器信息失败: {e}[/red]")


    async def _handle_pending_errors(self, log: TextArea) -> None:
        """列出所有待处理的上游错误"""
        pending = self.pending_error_ids
        if not pending:
            self._append(log, f"[dim]当前没有待处理的上游错误[/dim]")
            return

        self._append(log, f"\n[bold]⚠️  待处理上游错误 (共 {len(pending)} 个):[/bold]")
        for i, err in enumerate(pending, 1):
            self._append(log, 
                f"  [{i}] {err['provider']} | {err['model']} | "
                f"HTTP {err['status_code'] or '网络错误'} | {err['error_message'][:80]}"
            )
        self._append(log, f"\n[dim]使用 'resolve error <编号> 1' 将错误返回客户端[/dim]")

    async def _handle_resolve_error(self, args: str, log: TextArea) -> None:
        """
        处理 'resolve error <编号> <选项号>' 命令。

        选项号对应的操作（当前仅支持第 1 号）：
          1 — 将上游错误直接返回给客户端
        """
        parts = args.split()
        if len(parts) < 2:
            self._append(log, f"[yellow]⚠️  用法: resolve error <编号> <选项号>[/yellow]")
            self._append(log, f"  例如: resolve error 1 1  （将第 1 个错误返回给客户端）")
            self._append(log, f"  输入 'pending errors' 查看当前待处理错误列表")
            return

        try:
            idx = int(parts[0])
            option = int(parts[1])
        except ValueError:
            self._append(log, f"[red]❌ 编号和选项号必须为整数[/red]")
            return

        if idx < 1 or idx > len(self.pending_error_ids):
            self._append(log, f"[red]❌ 编号 {idx} 不存在，当前有 {len(self.pending_error_ids)} 个待处理错误[/red]")
            return

        # 目前仅支持选项 1: "return_error"
        action_map = {1: "return_error"}
        if option not in action_map:
            self._append(log, f"[yellow]⚠️  无效选项 {option}，当前支持: 1=将错误返回客户端[/yellow]")
            return

        action = action_map[option]
        err_info = self.pending_error_ids[idx - 1]

        # 通过 WebSocket 发送决策给服务器
        try:
            await self.server_manager.ws_client.send_message("command", {
                "action": "resolve_error",
                "error_id": err_info["error_id"],
                "decision": action,
            })
            # 从本地列表移除（服务器处理后不会再推送）
            self.pending_error_ids.pop(idx - 1)

            action_desc = {
                "return_error": "将错误返回给客户端",
            }
            self._append(log, f"[green]✅ 已处理错误 [{idx}]：{action_desc[action]}[/green]")

            # 如果没有更多待处理错误，退出错误模式
            if not self.pending_error_ids:
                self.select_mode = None
                input_widget = self.query_one("#command-input", Input)
                input_widget.placeholder = "输入命令 (help查看帮助)..."
            else:
                input_widget = self.query_one("#command-input", Input)
                input_widget.placeholder = f"还有 {len(self.pending_error_ids)} 个待处理错误，输入 'pending errors' 查看..."

        except Exception as e:
            self._append(log, f"[red]❌ 发送决策失败: {e}[/red]")

    def show_help(self) -> None:
        """显示帮助信息"""
        log = self.query_one("#api-log", TextArea)
        self._append(log, "\n[bold]📋 可用命令:[/bold]")
        self._append(log, "  [cyan]help[/cyan]         - 显示此帮助信息")
        self._append(log, "  [cyan]clear[/cyan]        - 清空日志")
        self._append(log, "  [cyan]stats[/cyan]        - 显示详细统计")
        self._append(log, "  [cyan]test[/cyan]         - 模拟API调用")
        self._append(log, "  [cyan]add[/cyan]          - 添加测试历史记录")
        self._append(log, "  [cyan]exit/quit[/cyan]    - 退出程序")
        self._append(log, "")
        self._append(log, "[bold]🚀 服务器控制命令:[/bold]")
        self._append(log, "  [cyan]start server[/cyan]    - 启动API服务器")
        self._append(log, "  [cyan]stop server[/cyan]     - 停止API服务器")
        self._append(log, "  [cyan]restart server[/cyan]  - 重启API服务器")
        self._append(log, "  [cyan]server status[/cyan]   - 显示服务器状态")
        self._append(log, "  [cyan]server info[/cyan]     - 显示服务器详细信息")
        self._append(log, "  [cyan]reload[/cyan]          - 热重载 providers 和 schemes（不重启服务器）")
        self._append(log, "")
        self._append(log, "[bold]🔧 转发方案命令:[/bold]")
        self._append(log, "  [cyan]list schemes[/cyan]              - 列出所有可用方案")
        self._append(log, "  [cyan]current scheme[/cyan]            - 显示当前方案及规则")
        self._append(log, "  [cyan]select scheme[/cyan]             - 交互式选择方案 (菜单)")
        self._append(log, "  [cyan]select scheme <name>[/cyan]      - 直接切换到指定方案")
        self._append(log, "  [cyan]cancel[/cyan]                    - 取消当前选择模式")
        self._append(log, "")
        self._append(log, "[bold]⚙️  Provider 命令:[/bold]")
        self._append(log, "  [cyan]list providers[/cyan]            - 列出所有 provider 的详细信息（不含 API Key）")
        self._append(log, "  [cyan]update models <provider>[/cyan]  - 向 provider 查询可用模型并同步到配置")
        self._append(log, "")
        self._append(log, "[bold]🚨 上游错误处理命令:[/bold]")
        self._append(log, "  [cyan]pending errors[/cyan]              - 列出所有待处理的上游错误")
        self._append(log, "  [cyan]resolve error <编号> <选项>[/cyan] - 处理指定上游错误")
        self._append(log, "    选项: [bold]1[/bold]=将错误直接返回给客户端")
        self._append(log, "")
        self._append(log, "[bold]📊 界面说明:[/bold]")
        self._append(log, "  左侧 [操作] Tab:     命令输入和摘要日志")
        self._append(log, "  左侧 [请求/响应] Tab: 完整 HTTP 请求和响应实时流（含 Header + Body）")
        self._append(log, "  左侧 [详情] Tab:     点击右下历史记录查看详细信息（类似 Chrome DevTools Network）")
        self._append(log, "  右上:               服务器状态 + Token统计")
        self._append(log, "  右下:               历史对话记录 (点击自动切换至详情 Tab)")
        self._append(log, "")
        self._append(log, "[dim]提示: 启动服务器后，API请求日志将实时显示在左侧区域[/dim]")

    def show_stats(self) -> None:
        """显示详细统计信息"""
        log = self.query_one("#api-log", TextArea)
        stats = self.token_stats
        real_time_stats = self.real_time_stats

        self._append(log, "\n[bold]📈 详细统计信息[/bold]")
        self._append(log, "────────────────")

        # 本地统计（向后兼容）
        self._append(log, "[bold]📊 本地统计:[/bold]")
        self._append(log, f"  总API调用次数: {stats['total_calls']}")
        self._append(log, f"  总Tokens消耗: {stats['total_tokens']:,}")
        self._append(log, f"  成功调用: {stats['success_calls']}")
        self._append(log, f"  失败调用: {stats['failed_calls']}")

        if stats['total_calls'] > 0:
            success_rate = (stats['success_calls'] / stats['total_calls']) * 100
            self._append(log, f"  成功率: {success_rate:.1f}%")
            avg_tokens = stats['total_tokens'] / stats['total_calls']
            self._append(log, f"  平均Tokens/次: {avg_tokens:.0f}")

        # 实时统计（来自API服务器）
        self._append(log, "")
        self._append(log, "[bold]🚀 实时统计 (来自API服务器):[/bold]")
        self._append(log, f"  总请求数: {real_time_stats.get('total_requests', 0)}")
        self._append(log, f"  总Tokens: {real_time_stats.get('total_tokens', 0):,}")
        self._append(log, f"  成功请求: {real_time_stats.get('successful_requests', 0)}")
        self._append(log, f"  失败请求: {real_time_stats.get('failed_requests', 0)}")

        if real_time_stats.get('total_requests', 0) > 0:
            real_time_success_rate = (real_time_stats.get('successful_requests', 0) / real_time_stats.get('total_requests', 1)) * 100
            self._append(log, f"  实时成功率: {real_time_success_rate:.1f}%")
            real_time_avg_tokens = real_time_stats.get('total_tokens', 0) / real_time_stats.get('total_requests', 1)
            self._append(log, f"  实时平均Tokens/次: {real_time_avg_tokens:.0f}")

        # 提供商使用统计
        providers = real_time_stats.get('providers', {})
        if providers:
            self._append(log, "")
            self._append(log, "[bold]🏢 提供商使用统计:[/bold]")
            for provider_name, provider_stats in providers.items():
                # provider_stats 是 int（请求计数）
                requests = provider_stats if isinstance(provider_stats, int) else provider_stats.get('requests', 0)
                self._append(log, f"  {provider_name}: {requests} 请求")

        # 模型使用统计
        models = real_time_stats.get('models', {})
        if models:
            self._append(log, "")
            self._append(log, "[bold]🤖 模型使用统计:[/bold]")
            for model_name, model_stats in models.items():
                requests = model_stats.get('requests', 0)
                tokens = model_stats.get('tokens', 0)
                self._append(log, f"  {model_name}: {requests} 请求, {tokens:,} tokens")

        # 其他信息
        self._append(log, "")
        self._append(log, f"[bold]📜 历史记录数量:[/bold] {len(self.history)}")

        last_update = real_time_stats.get('last_update', 0)
        if last_update > 0:
            time_since_update = time.time() - last_update
            update_time = datetime.fromtimestamp(last_update).strftime("%H:%M:%S")
            self._append(log, f"[bold]⏰ 最后更新:[/bold] {update_time} ({time_since_update:.1f}秒前)")

    async def simulate_api_call(self) -> None:
        """模拟API调用（测试用）"""
        log = self.query_one("#api-log", TextArea)

        self._append(log, f"\n[bold]🔧 模拟API调用...[/bold]")

        # 模拟处理延迟
        await asyncio.sleep(0.5)

        # 更新统计
        self.token_stats["total_calls"] += 1
        self.token_stats["success_calls"] += 1
        self.token_stats["total_tokens"] += 150

        # 添加历史记录
        new_history = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "model": "claude-3-opus-20240229",
            "tokens": 150,
            "status": "success"
        }
        self.history.append(new_history)

        # 更新UI
        self.update_token_display()

        # 更新历史表格
        table = self.query_one("#history-table", DataTable)
        table.add_row(
            new_history["time"],
            new_history["model"],
            str(new_history["tokens"]),
            "✅ 成功"
        )

        self._append(log, f"[green]✅ API调用成功! 消耗150 tokens[/green]")
        self._append(log, f"[dim]当前总Tokens: {self.token_stats['total_tokens']:,}[/dim]")

    def add_test_history(self) -> None:
        """添加测试历史记录"""
        test_records = [
            {"time": "10:30:15", "model": "claude-3-sonnet", "tokens": 85, "status": "success"},
            {"time": "11:45:22", "model": "claude-3-haiku", "tokens": 42, "status": "success"},
            {"time": "14:20:33", "model": "claude-3-opus", "tokens": 210, "status": "failed"},
            {"time": "15:55:47", "model": "claude-3-sonnet", "tokens": 120, "status": "success"},
        ]

        table = self.query_one("#history-table", DataTable)
        log = self.query_one("#api-log", TextArea)

        self._append(log, f"\n[bold]➕ 添加{len(test_records)}条测试历史记录[/bold]")

        for record in test_records:
            self.history.append(record)
            status_display = "✅ 成功" if record["status"] == "success" else "❌ 失败"
            table.add_row(
                record["time"],
                record["model"],
                str(record["tokens"]),
                status_display
            )

        self._append(log, f"[dim]历史记录总数: {len(self.history)}[/dim]")

    def action_refresh(self) -> None:
        """刷新显示"""
        self.update_token_display()
        log = self.query_one("#api-log", TextArea)
        self._append(log, f"[dim]🔄 已刷新 {datetime.now().strftime('%H:%M:%S')}[/dim]")

    def action_clear_log(self) -> None:
        """清空当前 Tab 的日志"""
        tabs = self.query_one("#main-tabs", TabbedContent)
        active = tabs.active
        if active == "tab-traffic":
            log = self.query_one("#traffic-log", TextArea)
        elif active == "tab-detail":
            log = self.query_one("#detail-log", TextArea)
        else:
            log = self.query_one("#api-log", TextArea)
        self._clear_log(log)
        self._append(log, f"[dim]🧹 日志已清空 {datetime.now().strftime('%H:%M:%S')}[/dim]")

    def action_quit(self) -> None:
        """退出程序"""
        # 创建异步任务来停止服务器并退出
        asyncio.create_task(self._async_quit())

    async def _async_quit(self) -> None:
        """异步退出程序，先停止服务器"""
        log = self.query_one("#api-log", TextArea)
        self._append(log, f"[dim]正在退出程序...[/dim]")

        # 停止服务器进程（如果正在运行）
        if self.server_status["running"]:
            self._append(log, f"[dim]正在停止API服务器...[/dim]")
            try:
                success = await self.server_manager.stop_server()
                if success:
                    self._append(log, f"[green]✅ API服务器已停止[/green]")
                else:
                    self._append(log, f"[yellow]⚠️  停止服务器失败，继续退出程序[/yellow]")
            except Exception as e:
                self._append(log, f"[yellow]⚠️  停止服务器时发生错误: {e}，继续退出程序[/yellow]")
            await asyncio.sleep(0.3)  # 短暂延迟让用户看到消息

        self.exit()


if __name__ == "__main__":
    app = APIProxyApp()
    app.run()