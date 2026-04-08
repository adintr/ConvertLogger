#!/usr/bin/env python3
"""
Anthropic API代理终端
将终端划分为三个区域显示不同内容
"""

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header, Footer, Static, RichLog,
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
            "uptime": 0
        }

        # 当前选择的provider和model
        self.current_provider = None
        self.current_model = None

        # 选择模式状态
        self.select_mode = None  # None, 'provider', 'model'
        self.select_providers = []  # 缓存的provider列表
        self.select_models = []  # 缓存的model列表
        self.selected_provider = None  # 临时存储选择的provider

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
                    log = self.query_one("#api-log", RichLog)
                    log.write(f"[green]✅ WebSocket连接成功[/green]")
                elif event_data.get("status") == "shutting_down":
                    log = self.query_one("#api-log", RichLog)
                    log.write(f"[yellow]⚠️  服务器正在关闭...[/yellow]")

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
                log = self.query_one("#api-log", RichLog)

                provider = event_data.get("provider", "unknown")
                model = event_data.get("model", "unknown")
                status_code = event_data.get("status_code", 0)
                success = event_data.get("success", False)
                response_time = event_data.get("response_time", 0)
                tokens = event_data.get("total_tokens", 0)

                status_icon = "✅" if success else "❌"
                status_color = "green" if success else "red"

                timestamp = datetime.fromtimestamp(event_data.get("timestamp", time.time())).strftime("%H:%M:%S")

                log.write(f"[{status_color}]{status_icon} [{timestamp}] {provider}: {model} (HTTP {status_code}) - {response_time:.2f}s - {tokens} tokens[/{status_color}]")
                log.scroll_end(animate=False)

                # 在"请求/响应" Tab 写入详细流量信息
                traffic_log = self.query_one("#traffic-log", RichLog)
                self._write_traffic_log(traffic_log, event_data, timestamp, status_color, status_icon)

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

            elif event_type == "error":
                # 显示错误信息
                log = self.query_one("#api-log", RichLog)
                error_msg = event_data.get("message", "未知错误")
                log.write(f"[red]❌ 服务器错误: {error_msg}[/red]")

        except Exception as e:
            # 防止事件处理异常影响应用
            log = self.query_one("#api-log", RichLog)
            log.write(f"[yellow]⚠️  事件处理异常: {e}[/yellow]")

    async def _auto_start_server(self):
        """自动启动API服务器"""
        log = self.query_one("#api-log", RichLog)

        # 等待一小段时间让UI完全初始化
        await asyncio.sleep(1)

        log.write(f"[dim]正在自动启动API服务器...[/dim]")

        try:
            success = await self.server_manager.start_server()
            if success:
                log.write(f"[green]✅ API服务器启动成功[/green]")
                log.write(f"[dim]服务器PID: {self.server_manager.status.pid}[/dim]")
                log.write(f"[dim]WebSocket连接中...[/dim]")
                log.write("")
                log.write(f"[bold]💡 快速开始:[/bold]")
                log.write(f"  1. 输入 'test' 模拟API调用测试")
                log.write(f"  2. 观察右上角实时统计更新")
                log.write(f"  3. 输入 'help' 查看所有命令")
                log.write("")
                log.write(f"[dim]提示: 服务器已自动启动，您可以直接开始测试[/dim]")
            else:
                log.write(f"[red]❌ API服务器启动失败[/red]")
                log.write(f"[dim]您仍可以手动输入 'start-server' 尝试启动[/dim]")
        except Exception as e:
            log.write(f"[red]❌ 启动服务器时发生错误: {e}[/red]")

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
                        yield RichLog(id="api-log", wrap=True, highlight=True, markup=True)
                        yield Input(
                            placeholder="输入命令 (help查看帮助)...",
                            id="command-input"
                        )

                    # Tab 2: 请求/响应
                    with TabPane("请求/响应", id="tab-traffic"):
                        yield RichLog(id="traffic-log", wrap=True, highlight=True, markup=True)

                    # Tab 3: 详情
                    with TabPane("详情", id="tab-detail"):
                        yield RichLog(id="detail-log", wrap=True, highlight=True, markup=True)

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
        log = self.query_one("#api-log", RichLog)
        log.write(f"[bold cyan]🚀 Anthropic API代理终端 v2.0 已启动[/bold cyan]")
        log.write(f"[dim]当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
        log.write(f"[dim]集成功能: 实时API监控、服务器控制、WebSocket通信[/dim]")
        log.write(f"[dim]输入 'help' 查看可用命令[/dim]")
        log.write("")
        log.write(f"[bold]💡 自动启动API服务器中...[/bold]")

        # 初始化请求/响应 Tab
        traffic_log = self.query_one("#traffic-log", RichLog)
        traffic_log.write(f"[bold cyan]📡 请求/响应实时监控[/bold cyan]")
        traffic_log.write(f"[dim]等待服务器启动后，所有代理请求和响应将在此实时显示...[/dim]")
        traffic_log.write(f"[dim]包含完整的 HTTP Header 和 Body 内容[/dim]")

        # 初始化详情 Tab
        detail_log = self.query_one("#detail-log", RichLog)
        detail_log.write(f"[bold cyan]🔍 请求详情查看器[/bold cyan]")
        detail_log.write(f"[dim]点击右侧历史记录中的任意一条，将在此处显示该请求的完整详情[/dim]")
        detail_log.write(f"[dim]类似 Chrome DevTools → Network 面板[/dim]")

        # 设置焦点到命令输入框
        input_widget = self.query_one("#command-input", Input)
        input_widget.focus()

        # 异步启动服务器
        asyncio.create_task(self._auto_start_server())

    def _write_headers(self, log: RichLog, headers: Dict[str, Any], indent: str = "    ") -> None:
        """辅助：写入 headers，无内容时显示(无)"""
        if headers:
            for k, v in headers.items():
                log.write(f"{indent}[cyan]{k}[/cyan]: {v}")
        else:
            log.write(f"{indent}[dim](无)[/dim]")

    def _write_body(self, log: RichLog, body: Any, indent: str = "    ") -> None:
        """辅助：写入 body，JSON 格式化，不截断"""
        if body is None or body == "":
            log.write(f"{indent}[dim](无)[/dim]")
            return
        if isinstance(body, (dict, list)):
            try:
                body_str = json_module.dumps(body, ensure_ascii=False, indent=2)
            except Exception:
                body_str = str(body)
        else:
            body_str = str(body)
        for line in body_str.splitlines():
            log.write(f"{indent}{line}")

    def _write_traffic_log(self, log: RichLog, data: Dict[str, Any], timestamp: str, status_color: str, status_icon: str) -> None:
        """在请求/响应 Tab 写入详细流量信息"""
        provider = data.get("provider", "unknown")
        model = data.get("model", "unknown")
        method = data.get("method", "POST")
        url = data.get("url", "")
        status_code = data.get("status_code", 0)
        response_time = data.get("response_time", 0)
        tokens = data.get("total_tokens", 0)

        log.write(f"\n[bold {status_color}]{'─' * 60}[/bold {status_color}]")
        log.write(f"[bold {status_color}]{status_icon} [{timestamp}] {method} → {provider} | HTTP {status_code} | {response_time:.2f}s[/bold {status_color}]")

        incoming_url = data.get("incoming_url", "")

        # ── 1. 客户端 → 代理：原始请求 ──────────────────────────
        log.write(f"\n[bold yellow]▶ 客户端请求 (→ 代理)[/bold yellow]")
        log.write(f"  [dim]URL:[/dim]  {method} {incoming_url or '(未知)'}")
        log.write(f"  [dim]模型:[/dim] {model}")
        log.write(f"  [dim]Headers:[/dim]")
        self._write_headers(log, data.get("incoming_headers", {}))
        log.write(f"  [dim]Body:[/dim]")
        self._write_body(log, data.get("request_body"))

        # ── 2. 代理 → Provider：转发请求 ─────────────────────────
        log.write(f"\n[bold cyan]▶ 转发请求 (→ {provider})[/bold cyan]")
        log.write(f"  [dim]URL:[/dim]  {method} {url or '(未知)'}")
        log.write(f"  [dim]Headers:[/dim]")
        self._write_headers(log, data.get("forwarded_headers", {}))

        # ── 3. Provider → 代理：响应 ─────────────────────────────
        log.write(f"\n[bold green]◀ Provider 响应 (← {provider})[/bold green]")
        log.write(f"  [dim]状态:[/dim]  HTTP {status_code}")
        log.write(f"  [dim]Tokens:[/dim] {tokens}")
        log.write(f"  [dim]Headers:[/dim]")
        self._write_headers(log, data.get("response_headers", {}))
        log.write(f"  [dim]Body:[/dim]")
        self._write_body(log, data.get("response_body"))

        # ── 4. 代理 → 客户端：最终响应 ───────────────────────────
        log.write(f"\n[bold magenta]◀ 发送给客户端 (→ 原始调用方)[/bold magenta]")
        log.write(f"  [dim]状态:[/dim]  HTTP {status_code}")
        log.write(f"  [dim]Headers:[/dim]")
        self._write_headers(log, data.get("client_response_headers", {}))
        log.write(f"  [dim]Body:[/dim]")
        self._write_body(log, data.get("client_response_body"))

        error_msg = data.get("error_message")
        if error_msg:
            log.write(f"  [red]错误: {error_msg}[/red]")

        log.scroll_end(animate=False)

    def _write_detail_view(self, log: RichLog, item: Dict[str, Any]) -> None:
        """在详情 Tab 显示类似 Chrome DevTools Network 面板的请求详情"""
        log.clear()
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

        status_color = "green" if success else "red"
        status_icon = "✅" if success else "❌"

        # 总览
        log.write(f"[bold cyan]{'═' * 60}[/bold cyan]")
        log.write(f"[bold cyan]  {status_icon} 请求详情[/bold cyan]")
        log.write(f"[bold cyan]{'═' * 60}[/bold cyan]")
        log.write(f"")
        log.write(f"[bold]▍ 概览[/bold]")
        log.write(f"  时间:       {timestamp}")
        log.write(f"  Provider:   {provider}")
        log.write(f"  模型:       {model}")
        log.write(f"  状态:       [{status_color}]HTTP {status_code} ({'成功' if success else '失败'})[/{status_color}]")
        log.write(f"  耗时:       {response_time:.3f}s")
        log.write(f"  Tokens:     {tokens}")
        log.write(f"  方法:       {method}")
        incoming_url = data.get("incoming_url", "")
        if incoming_url:
            log.write(f"  客户端URL:  {incoming_url}")
        if url:
            log.write(f"  转发URL:    {url}")

        # ── 1. 客户端 → 代理：原始请求 ───────────────────────────
        log.write(f"")
        log.write(f"[bold yellow]▍ 1. 客户端请求 Headers (→ 代理)[/bold yellow]")
        self._write_headers(log, data.get("incoming_headers", {}), indent="  ")

        log.write(f"")
        log.write(f"[bold yellow]▍ 2. 客户端请求 Body[/bold yellow]")
        self._write_body(log, data.get("request_body"), indent="  ")

        # ── 2. 代理 → Provider：转发请求 ──────────────────────────
        log.write(f"")
        log.write(f"[bold cyan]▍ 3. 转发请求 Headers (→ {provider})[/bold cyan]")
        self._write_headers(log, data.get("forwarded_headers", {}), indent="  ")

        # ── 3. Provider → 代理：响应 ──────────────────────────────
        log.write(f"")
        log.write(f"[bold green]▍ 4. Provider 响应 Headers (← {provider})[/bold green]")
        self._write_headers(log, data.get("response_headers", {}), indent="  ")

        log.write(f"")
        log.write(f"[bold green]▍ 5. Provider 响应 Body[/bold green]")
        self._write_body(log, data.get("response_body"), indent="  ")

        # ── 4. 代理 → 客户端：最终响应 ────────────────────────────
        log.write(f"")
        log.write(f"[bold magenta]▍ 6. 发送给客户端 Headers[/bold magenta]")
        self._write_headers(log, data.get("client_response_headers", {}), indent="  ")

        log.write(f"")
        log.write(f"[bold magenta]▍ 7. 发送给客户端 Body[/bold magenta]")
        self._write_body(log, data.get("client_response_body"), indent="  ")

        # 错误信息
        error_msg = data.get("error_message")
        if error_msg:
            log.write(f"")
            log.write(f"[bold red]▍ 错误信息[/bold red]")
            log.write(f"  [red]{error_msg}[/red]")

        log.write(f"")
        log.write(f"[bold cyan]{'═' * 60}[/bold cyan]")
        log.scroll_begin(animate=False)

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

        content = (
            f"[bold]{server_icon} 服务器状态: {server_text}[/bold]\n"
            f"[bold]{ws_icon} WebSocket: {ws_text}[/bold]\n"
            f"[bold]⏱️  运行时间: {uptime_str}[/bold]\n"
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
        """更新标题栏显示代理入口地址和provider"""
        # 获取服务器地址
        host = self.server_manager.status.host
        port = self.server_manager.status.port
        address = f"{host}:{port}" if host and port else "未启动"

        # 优先使用手动选择的provider和model
        current_provider = "未选择"
        current_model = ""

        if self.current_provider:
            current_provider = self.current_provider
        else:
            # 从实时统计中获取活跃的provider
            providers = self.real_time_stats.get("providers", {})
            if providers:
                # 找出请求数最多的provider作为当前provider
                # providers 是 Dict[str, int]，值直接是请求数
                active_providers = [p for p, cnt in providers.items() if (cnt if isinstance(cnt, int) else cnt.get("requests", 0)) > 0]
                if active_providers:
                    # 按请求数排序，providers 值是 int
                    sorted_providers = sorted(
                        active_providers,
                        key=lambda p: providers[p] if isinstance(providers[p], int) else providers[p].get("requests", 0),
                        reverse=True
                    )
                    current_provider = sorted_providers[0]

        # 添加model信息
        if self.current_model:
            current_model = f" ({self.current_model})"
        elif self.current_provider and not self.current_model:
            current_model = " (默认模型)"

        # 设置标题和副标题
        self.title = f"Anthropic API代理终端 v2.0 | {address} -> {current_provider}{current_model}"
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
            detail_log = self.query_one("#detail-log", RichLog)
            self._write_detail_view(detail_log, history_item)

    @on(Input.Submitted, "#command-input")
    async def on_command_submitted(self, event: Input.Submitted) -> None:
        """处理用户命令输入"""
        command = event.value.strip()
        event.input.value = ""  # 清空输入框

        if not command:
            return

        log = self.query_one("#api-log", RichLog)

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
            log.write(f"[dim]正在退出程序...[/dim]")
            await asyncio.sleep(0.5)  # 短暂延迟让用户看到消息

            # 停止服务器进程（如果正在运行）
            if self.server_status["running"]:
                log.write(f"[dim]正在停止API服务器...[/dim]")
                try:
                    success = await self.server_manager.stop_server()
                    if success:
                        log.write(f"[green]✅ API服务器已停止[/green]")
                    else:
                        log.write(f"[yellow]⚠️  停止服务器失败，继续退出程序[/yellow]")
                except Exception as e:
                    log.write(f"[yellow]⚠️  停止服务器时发生错误: {e}，继续退出程序[/yellow]")
                await asyncio.sleep(0.3)  # 短暂延迟让用户看到消息

            self.exit()
        elif command.lower() == "select":
            await self._handle_select_command(log)
        elif command.lower() == "cancel":
            # 取消选择模式
            if self.select_mode is not None:
                self.select_mode = None
                self.select_providers = []
                self.select_models = []
                self.selected_provider = None
                input_widget = self.query_one("#command-input", Input)
                input_widget.placeholder = "输入命令 (help查看帮助)..."
                log.write(f"[dim]选择已取消[/dim]")
            else:
                log.write(f"[dim]当前不在选择模式[/dim]")

        # 服务器控制命令
        elif command.lower() == "start-server":
            await self._handle_start_server()
        elif command.lower() == "stop-server":
            await self._handle_stop_server()
        elif command.lower() == "restart-server":
            await self._handle_restart_server()
        elif command.lower() == "server-status":
            await self._handle_server_status()
        elif command.lower() == "server-info":
            await self._handle_server_info()

        # provider和model选择命令
        elif command.lower().startswith("select-provider "):
            provider_name = command[len("select-provider "):].strip()
            await self._handle_select_provider(provider_name)
        elif command.lower() == "list-providers":
            await self._handle_list_providers()
        elif command.lower().startswith("select-model "):
            model_name = command[len("select-model "):].strip()
            await self._handle_select_model(model_name)
        elif command.lower() == "list-models":
            await self._handle_list_models()
        elif command.lower() == "current-provider":
            await self._handle_current_provider()
        elif command.lower() == "current-model":
            await self._handle_current_model()
        elif command.lower() == "clear-selection":
            await self._handle_clear_selection()

        else:
            log.write(f"[yellow]❓ 未知命令: {command}[/yellow]")
            log.write(f"[dim]输入 'help' 查看可用命令[/dim]")

    async def _handle_select_command(self, log: RichLog) -> None:
        """处理select命令，显示provider菜单"""
        try:
            # 加载配置
            from config import load_config
            config = load_config(self.server_manager.config_path)
            enabled_providers = config.get_enabled_providers()

            if not enabled_providers:
                log.write(f"[yellow]⚠️  没有可用的providers[/yellow]")
                return

            # 进入选择模式
            self.select_mode = 'provider'
            self.select_providers = enabled_providers
            self.select_models = []
            self.selected_provider = None

            # 显示菜单
            log.write(f"\n[bold]🏢 请选择provider (输入数字):[/bold]")
            for i, provider in enumerate(enabled_providers, 1):
                status = "✅" if provider.enabled else "❌"
                proxy_info = " (使用代理)" if provider.proxy_enabled else ""
                log.write(f"  [bold]{i}. {provider.name}[/bold] {status}{proxy_info}")
                log.write(f"      类型: {provider.type}, 模型: {len(provider.models)}个")
                if provider.models:
                    models_preview = ', '.join(provider.models[:2])
                    if len(provider.models) > 2:
                        models_preview += f" ... (共{len(provider.models)}个)"
                    log.write(f"      示例: {models_preview}")

            log.write(f"\n[dim]输入 1-{len(enabled_providers)} 选择provider，或输入 'cancel' 取消[/dim]")

            # 更新输入框提示
            input_widget = self.query_one("#command-input", Input)
            input_widget.placeholder = f"输入数字 (1-{len(enabled_providers)})..."

        except Exception as e:
            log.write(f"[red]❌ 显示provider菜单时发生错误: {e}[/red]")
            self.select_mode = None
            self.select_providers = []

    async def _handle_selection_input(self, command: str, log: RichLog) -> None:
        """处理选择模式下的用户输入"""
        command_lower = command.lower()

        # 取消命令
        if command_lower == "cancel":
            self.select_mode = None
            self.select_providers = []
            self.select_models = []
            self.selected_provider = None
            input_widget = self.query_one("#command-input", Input)
            input_widget.placeholder = "输入命令 (help查看帮助)..."
            log.write(f"[dim]选择已取消[/dim]")
            return

        try:
            if self.select_mode == 'provider':
                # 验证输入是否为数字
                if not command.isdigit():
                    log.write(f"[yellow]⚠️  请输入数字 1-{len(self.select_providers)}[/yellow]")
                    return

                index = int(command)
                if index < 1 or index > len(self.select_providers):
                    log.write(f"[yellow]⚠️  请输入有效数字 1-{len(self.select_providers)}[/yellow]")
                    return

                # 选择provider
                provider = self.select_providers[index - 1]
                self.selected_provider = provider
                log.write(f"[green]✅ 已选择provider: {provider.name}[/green]")
                log.write(f"[dim]类型: {provider.type}, 基础URL: {provider.base_url}[/dim]")

                # 自动选择第一个model
                if provider.models:
                    # 进入model选择模式
                    self.select_mode = 'model'
                    self.select_models = provider.models

                    # 显示model菜单
                    log.write(f"\n[bold]🤖 请选择model (输入数字):[/bold]")
                    for i, model in enumerate(provider.models, 1):
                        log.write(f"  [bold]{i}. {model}[/bold]")

                    log.write(f"\n[dim]输入 1-{len(provider.models)} 选择model，或输入 'cancel' 取消[/dim]")
                    log.write(f"[dim]提示: 输入 'auto' 自动选择第一个model[/dim]")

                    # 更新输入框提示
                    input_widget = self.query_one("#command-input", Input)
                    input_widget.placeholder = f"输入数字 (1-{len(provider.models)}) 或 'auto'..."
                else:
                    log.write(f"[yellow]⚠️  该provider没有可用的models[/yellow]")
                    # 完成选择，设置默认model为None
                    await self._finalize_selection(provider.name, None, log)

            elif self.select_mode == 'model':
                # 处理model选择
                selected_model = None

                if command_lower == 'auto':
                    # 自动选择第一个model
                    if self.select_models:
                        selected_model = self.select_models[0]
                        log.write(f"[green]✅ 自动选择model: {selected_model}[/green]")
                    else:
                        log.write(f"[yellow]⚠️  没有可用的models[/yellow]")
                        selected_model = None
                elif command.isdigit():
                    index = int(command)
                    if index < 1 or index > len(self.select_models):
                        log.write(f"[yellow]⚠️  请输入有效数字 1-{len(self.select_models)}[/yellow]")
                        return
                    selected_model = self.select_models[index - 1]
                    log.write(f"[green]✅ 已选择model: {selected_model}[/green]")
                else:
                    log.write(f"[yellow]⚠️  请输入数字或 'auto'[/yellow]")
                    return

                # 完成选择
                await self._finalize_selection(self.selected_provider.name, selected_model, log)

        except Exception as e:
            log.write(f"[red]❌ 处理选择时发生错误: {e}[/red]")
            self.select_mode = None
            self.select_providers = []
            self.select_models = []
            self.selected_provider = None

    async def _finalize_selection(self, provider_name: str, model_name: Optional[str], log: RichLog) -> None:
        """完成选择，更新本地状态并发送到服务器"""
        try:
            # 更新本地状态
            self.current_provider = provider_name
            self.current_model = model_name

            # 更新标题
            self.update_title()

            # 发送到服务器
            if self.server_status["ws_connected"]:
                data = {"provider": provider_name}
                if model_name:
                    data["model"] = model_name

                # 通过server_manager发送命令
                cmd_id = await self.server_manager.send_command("set_default_provider", data)
                if cmd_id:
                    log.write(f"[dim]命令已发送到服务器 (ID: {cmd_id})[/dim]")
                else:
                    log.write(f"[yellow]⚠️  发送命令到服务器失败，WebSocket可能未连接[/yellow]")
            else:
                log.write(f"[yellow]⚠️  WebSocket未连接，无法更新服务器默认设置[/yellow]")
                log.write(f"[dim]本地选择已更新，但服务器将继续使用之前的默认设置[/dim]")

            # 显示确认信息
            model_info = f" ({model_name})" if model_name else ""
            log.write(f"[green]✅ 选择完成: {provider_name}{model_info}[/green]")
            log.write(f"[dim]现在将使用此provider和model处理后续请求[/dim]")

            # 重置选择模式
            self.select_mode = None
            self.select_providers = []
            self.select_models = []
            self.selected_provider = None

            # 恢复输入框提示
            input_widget = self.query_one("#command-input", Input)
            input_widget.placeholder = "输入命令 (help查看帮助)..."

        except Exception as e:
            log.write(f"[red]❌ 完成选择时发生错误: {e}[/red]")
            self.select_mode = None

    async def _handle_start_server(self):
        """处理启动服务器命令"""
        log = self.query_one("#api-log", RichLog)

        if self.server_status["running"]:
            log.write(f"[yellow]⚠️  服务器已经在运行中[/yellow]")
            return

        log.write(f"[dim]正在启动API服务器...[/dim]")

        try:
            success = await self.server_manager.start_server()
            if success:
                log.write(f"[green]✅ API服务器启动成功[/green]")
                log.write(f"[dim]服务器PID: {self.server_manager.status.pid}[/dim]")
                log.write(f"[dim]WebSocket连接中...[/dim]")
            else:
                log.write(f"[red]❌ API服务器启动失败[/red]")
        except Exception as e:
            log.write(f"[red]❌ 启动服务器时发生错误: {e}[/red]")

    async def _handle_stop_server(self):
        """处理停止服务器命令"""
        log = self.query_one("#api-log", RichLog)

        if not self.server_status["running"]:
            log.write(f"[yellow]⚠️  服务器未运行[/yellow]")
            return

        log.write(f"[dim]正在停止API服务器...[/dim]")

        try:
            success = await self.server_manager.stop_server()
            if success:
                log.write(f"[green]✅ API服务器已停止[/green]")
            else:
                log.write(f"[red]❌ 停止服务器失败[/red]")
        except Exception as e:
            log.write(f"[red]❌ 停止服务器时发生错误: {e}[/red]")

    async def _handle_restart_server(self):
        """处理重启服务器命令"""
        log = self.query_one("#api-log", RichLog)

        log.write(f"[dim]正在重启API服务器...[/dim]")

        try:
            success = await self.server_manager.restart_server()
            if success:
                log.write(f"[green]✅ API服务器重启成功[/green]")
            else:
                log.write(f"[red]❌ 重启服务器失败[/red]")
        except Exception as e:
            log.write(f"[red]❌ 重启服务器时发生错误: {e}[/red]")

    async def _handle_server_status(self):
        """处理服务器状态命令"""
        log = self.query_one("#api-log", RichLog)

        status = self.server_status
        manager_status = self.server_manager.status

        log.write(f"\n[bold]📡 服务器状态信息[/bold]")
        log.write(f"  运行状态: {'🟢 运行中' if status['running'] else '🔴 已停止'}")
        log.write(f"  WebSocket: {'📡 已连接' if status['ws_connected'] else '📴 未连接'}")

        if status['start_time'] > 0:
            uptime = time.time() - status['start_time']
            uptime_str = self._format_uptime(uptime)
            log.write(f"  运行时间: {uptime_str}")

        if manager_status.pid:
            log.write(f"  进程ID: {manager_status.pid}")

        if status['last_heartbeat'] > 0:
            time_since_heartbeat = time.time() - status['last_heartbeat']
            log.write(f"  上次心跳: {time_since_heartbeat:.1f}秒前")

        log.write(f"  配置路径: {self.server_manager.config_path}")

    async def _handle_server_info(self):
        """处理服务器详细信息命令"""
        log = self.query_one("#api-log", RichLog)

        try:
            info = await self.server_manager.get_server_info()
            log.write(f"\n[bold]📋 服务器详细信息[/bold]")

            for key, value in info.items():
                if isinstance(value, dict):
                    log.write(f"  {key}:")
                    for sub_key, sub_value in value.items():
                        log.write(f"    {sub_key}: {sub_value}")
                else:
                    log.write(f"  {key}: {value}")
        except Exception as e:
            log.write(f"[red]❌ 获取服务器信息失败: {e}[/red]")

    # provider和model选择相关方法
    async def _handle_select_provider(self, provider_name: str):
        """选择指定的provider"""
        log = self.query_one("#api-log", RichLog)

        try:
            # 加载配置
            config = load_config(self.server_manager.config_path)
            enabled_providers = config.get_enabled_providers()

            # 查找provider
            found_provider = None
            for provider in enabled_providers:
                if provider.name.lower() == provider_name.lower():
                    found_provider = provider
                    break

            if found_provider:
                self.current_provider = found_provider.name
                log.write(f"[green]✅ 已选择provider: {found_provider.name}[/green]")
                log.write(f"[dim]类型: {found_provider.type}[/dim]")
                log.write(f"[dim]基础URL: {found_provider.base_url}[/dim]")
                log.write(f"[dim]支持模型: {', '.join(found_provider.models[:5])}{'...' if len(found_provider.models) > 5 else ''}[/dim]")
                log.write(f"[dim]权重: {found_provider.weight}, 超时: {found_provider.timeout}秒[/dim]")

                # 更新标题
                self.update_title()
            else:
                log.write(f"[red]❌ 找不到provider: {provider_name}[/red]")
                log.write(f"[dim]可用的providers: {', '.join([p.name for p in enabled_providers])}[/dim]")

        except Exception as e:
            log.write(f"[red]❌ 选择provider时发生错误: {e}[/red]")

    async def _handle_list_providers(self):
        """列出所有可用的providers"""
        log = self.query_one("#api-log", RichLog)

        try:
            # 加载配置
            config = load_config(self.server_manager.config_path)
            enabled_providers = config.get_enabled_providers()

            if not enabled_providers:
                log.write(f"[yellow]⚠️  没有可用的providers[/yellow]")
                return

            log.write(f"\n[bold]🏢 可用providers (共{len(enabled_providers)}个):[/bold]")

            for i, provider in enumerate(enabled_providers, 1):
                status = "✅" if provider.enabled else "❌"
                proxy_info = " (使用代理)" if provider.proxy_enabled else ""
                current_marker = " 👈 当前选择" if self.current_provider == provider.name else ""
                log.write(f"\n  [bold]{i}. {provider.name}[/bold] {status}{proxy_info}{current_marker}")
                log.write(f"     类型: {provider.type}")
                log.write(f"     基础URL: {provider.base_url}")
                log.write(f"     模型数量: {len(provider.models)}")
                log.write(f"     权重: {provider.weight}, 超时: {provider.timeout}秒")
                if provider.models:
                    models_preview = ', '.join(provider.models[:3])
                    if len(provider.models) > 3:
                        models_preview += f" ... (共{len(provider.models)}个)"
                    log.write(f"     模型示例: {models_preview}")

            log.write(f"\n[dim]使用 'select-provider <name>' 选择provider[/dim]")

        except Exception as e:
            log.write(f"[red]❌ 列出providers时发生错误: {e}[/red]")

    async def _handle_select_model(self, model_name: str):
        """选择指定的model"""
        log = self.query_one("#api-log", RichLog)

        try:
            # 加载配置
            config = load_config(self.server_manager.config_path)
            all_models = config.get_all_supported_models()

            # 查找模型（支持通配符匹配）
            matched_models = []
            for model in all_models:
                if model_name.lower() in model.lower():
                    matched_models.append(model)

            if not matched_models:
                log.write(f"[red]❌ 找不到model: {model_name}[/red]")
                log.write(f"[dim]可用的models: {', '.join(all_models[:10])}{'...' if len(all_models) > 10 else ''}[/dim]")
                return

            if len(matched_models) == 1:
                selected_model = matched_models[0]
                self.current_model = selected_model
                log.write(f"[green]✅ 已选择model: {selected_model}[/green]")

                # 查找支持该model的providers
                supporting_providers = config.get_providers_for_model(selected_model)
                if supporting_providers:
                    provider_names = [p.name for p in supporting_providers]
                    log.write(f"[dim]支持该model的providers: {', '.join(provider_names)}[/dim]")

                    # 如果当前没有选择provider，建议选择第一个支持的provider
                    if not self.current_provider and supporting_providers:
                        suggested_provider = supporting_providers[0].name
                        log.write(f"[dim]建议: 使用 'select-provider {suggested_provider}' 选择provider[/dim]")
                else:
                    log.write(f"[yellow]⚠️  没有provider支持此model[/yellow]")

                # 更新标题
                self.update_title()
            else:
                log.write(f"[yellow]⚠️  找到多个匹配的models:[/yellow]")
                for i, model in enumerate(matched_models[:5], 1):
                    log.write(f"  {i}. {model}")
                if len(matched_models) > 5:
                    log.write(f"  ... (共{len(matched_models)}个)")
                log.write(f"[dim]请指定更精确的model名称[/dim]")

        except Exception as e:
            log.write(f"[red]❌ 选择model时发生错误: {e}[/red]")

    async def _handle_list_models(self):
        """列出所有支持的models"""
        log = self.query_one("#api-log", RichLog)

        try:
            # 加载配置
            config = load_config(self.server_manager.config_path)
            all_models = config.get_all_supported_models()

            if not all_models:
                log.write(f"[yellow]⚠️  没有可用的models[/yellow]")
                return

            log.write(f"\n[bold]🤖 可用models (共{len(all_models)}个):[/bold]")

            # 按provider分组显示
            enabled_providers = config.get_enabled_providers()
            for provider in enabled_providers:
                if provider.models:
                    current_marker = " 👈 当前选择" if self.current_provider == provider.name else ""
                    log.write(f"\n  [bold]{provider.name}[/bold]{current_marker}:")
                    for i, model in enumerate(provider.models[:10], 1):
                        model_current = " ✅" if self.current_model == model else ""
                        log.write(f"      {i}. {model}{model_current}")
                    if len(provider.models) > 10:
                        log.write(f"      ... 还有{len(provider.models) - 10}个模型")

            log.write(f"\n[dim]使用 'select-model <name>' 选择model[/dim]")
            log.write(f"[dim]当前选择的model: {self.current_model if self.current_model else '未选择'}[/dim]")

        except Exception as e:
            log.write(f"[red]❌ 列出models时发生错误: {e}[/red]")

    async def _handle_current_provider(self):
        """显示当前选择的provider"""
        log = self.query_one("#api-log", RichLog)

        if not self.current_provider:
            log.write(f"[yellow]⚠️  当前未选择provider[/yellow]")
            log.write(f"[dim]使用 'list-providers' 查看可用providers[/dim]")
            log.write(f"[dim]使用 'select-provider <name>' 选择provider[/dim]")
            return

        try:
            # 加载配置获取详细信息
            config = load_config(self.server_manager.config_path)
            provider_config = config.get_provider_by_name(self.current_provider)

            if not provider_config:
                log.write(f"[red]❌ 找不到当前选择的provider配置: {self.current_provider}[/red]")
                self.current_provider = None  # 重置
                self.update_title()
                return

            log.write(f"\n[bold]📋 当前选择的provider: {provider_config.name}[/bold]")
            log.write(f"  类型: {provider_config.type}")
            log.write(f"  基础URL: {provider_config.base_url}")
            log.write(f"  模型数量: {len(provider_config.models)}")
            log.write(f"  权重: {provider_config.weight}")
            log.write(f"  超时: {provider_config.timeout}秒")
            log.write(f"  代理: {'启用' if provider_config.proxy_enabled else '禁用'}")
            if provider_config.models:
                log.write(f"  模型列表: {', '.join(provider_config.models[:5])}{'...' if len(provider_config.models) > 5 else ''}")

        except Exception as e:
            log.write(f"[red]❌ 获取provider信息时发生错误: {e}[/red]")

    async def _handle_current_model(self):
        """显示当前选择的model"""
        log = self.query_one("#api-log", RichLog)

        if not self.current_model:
            log.write(f"[yellow]⚠️  当前未选择model[/yellow]")
            log.write(f"[dim]使用 'list-models' 查看可用models[/dim]")
            log.write(f"[dim]使用 'select-model <name>' 选择model[/dim]")
            return

        log.write(f"\n[bold]🤖 当前选择的model: {self.current_model}[/bold]")

        try:
            # 加载配置查找支持该model的providers
            config = load_config(self.server_manager.config_path)
            supporting_providers = config.get_providers_for_model(self.current_model)

            if supporting_providers:
                log.write(f"[dim]支持该model的providers (共{len(supporting_providers)}个):[/dim]")
                for provider in supporting_providers:
                    current_marker = " 👈 当前选择" if self.current_provider == provider.name else ""
                    log.write(f"  - {provider.name}{current_marker}")
            else:
                log.write(f"[yellow]⚠️  没有provider支持此model[/yellow]")

        except Exception as e:
            log.write(f"[dim]无法获取provider信息: {e}[/dim]")

    async def _handle_clear_selection(self):
        """清除provider和model选择"""
        log = self.query_one("#api-log", RichLog)

        if not self.current_provider and not self.current_model:
            log.write(f"[dim]当前没有选择任何provider或model[/dim]")
            return

        cleared_items = []
        if self.current_provider:
            cleared_items.append(f"provider: {self.current_provider}")
            self.current_provider = None
        if self.current_model:
            cleared_items.append(f"model: {self.current_model}")
            self.current_model = None

        log.write(f"[green]✅ 已清除选择: {', '.join(cleared_items)}[/green]")
        log.write(f"[dim]现在将使用自动选择策略[/dim]")

        # 更新标题
        self.update_title()

    def show_help(self) -> None:
        """显示帮助信息"""
        log = self.query_one("#api-log", RichLog)
        log.write("\n[bold]📋 可用命令:[/bold]")
        log.write("  [cyan]help[/cyan]         - 显示此帮助信息")
        log.write("  [cyan]clear[/cyan]        - 清空日志")
        log.write("  [cyan]stats[/cyan]        - 显示详细统计")
        log.write("  [cyan]test[/cyan]         - 模拟API调用")
        log.write("  [cyan]add[/cyan]          - 添加测试历史记录")
        log.write("  [cyan]exit/quit[/cyan]    - 退出程序")
        log.write("")
        log.write("[bold]🚀 服务器控制命令:[/bold]")
        log.write("  [cyan]start-server[/cyan]   - 启动API服务器")
        log.write("  [cyan]stop-server[/cyan]    - 停止API服务器")
        log.write("  [cyan]restart-server[/cyan] - 重启API服务器")
        log.write("  [cyan]server-status[/cyan]  - 显示服务器状态")
        log.write("  [cyan]server-info[/cyan]    - 显示服务器详细信息")
        log.write("")
        log.write("[bold]🔧 provider和model选择命令:[/bold]")
        log.write("  [cyan]select[/cyan]           - 交互式选择provider和model (菜单式)")
        log.write("  [cyan]cancel[/cyan]           - 取消当前选择模式")
        log.write("  [cyan]list-providers[/cyan]    - 列出所有可用的providers")
        log.write("  [cyan]list-models[/cyan]      - 列出所有支持的models")
        log.write("  [cyan]select-provider <name>[/cyan] - 选择指定的provider")
        log.write("  [cyan]select-model <name>[/cyan]   - 选择指定的model")
        log.write("  [cyan]current-provider[/cyan] - 显示当前选择的provider")
        log.write("  [cyan]current-model[/cyan]    - 显示当前选择的model")
        log.write("  [cyan]clear-selection[/cyan]  - 清除provider和model选择")
        log.write("")
        log.write("[bold]📊 界面说明:[/bold]")
        log.write("  左侧 [操作] Tab:     命令输入和摘要日志")
        log.write("  左侧 [请求/响应] Tab: 完整 HTTP 请求和响应实时流（含 Header + Body）")
        log.write("  左侧 [详情] Tab:     点击右下历史记录查看详细信息（类似 Chrome DevTools Network）")
        log.write("  右上:               服务器状态 + Token统计")
        log.write("  右下:               历史对话记录 (点击自动切换至详情 Tab)")
        log.write("")
        log.write("[dim]提示: 启动服务器后，API请求日志将实时显示在左侧区域[/dim]")

    def show_stats(self) -> None:
        """显示详细统计信息"""
        log = self.query_one("#api-log", RichLog)
        stats = self.token_stats
        real_time_stats = self.real_time_stats

        log.write("\n[bold]📈 详细统计信息[/bold]")
        log.write("────────────────")

        # 本地统计（向后兼容）
        log.write("[bold]📊 本地统计:[/bold]")
        log.write(f"  总API调用次数: {stats['total_calls']}")
        log.write(f"  总Tokens消耗: {stats['total_tokens']:,}")
        log.write(f"  成功调用: {stats['success_calls']}")
        log.write(f"  失败调用: {stats['failed_calls']}")

        if stats['total_calls'] > 0:
            success_rate = (stats['success_calls'] / stats['total_calls']) * 100
            log.write(f"  成功率: {success_rate:.1f}%")
            avg_tokens = stats['total_tokens'] / stats['total_calls']
            log.write(f"  平均Tokens/次: {avg_tokens:.0f}")

        # 实时统计（来自API服务器）
        log.write("")
        log.write("[bold]🚀 实时统计 (来自API服务器):[/bold]")
        log.write(f"  总请求数: {real_time_stats.get('total_requests', 0)}")
        log.write(f"  总Tokens: {real_time_stats.get('total_tokens', 0):,}")
        log.write(f"  成功请求: {real_time_stats.get('successful_requests', 0)}")
        log.write(f"  失败请求: {real_time_stats.get('failed_requests', 0)}")

        if real_time_stats.get('total_requests', 0) > 0:
            real_time_success_rate = (real_time_stats.get('successful_requests', 0) / real_time_stats.get('total_requests', 1)) * 100
            log.write(f"  实时成功率: {real_time_success_rate:.1f}%")
            real_time_avg_tokens = real_time_stats.get('total_tokens', 0) / real_time_stats.get('total_requests', 1)
            log.write(f"  实时平均Tokens/次: {real_time_avg_tokens:.0f}")

        # 提供商使用统计
        providers = real_time_stats.get('providers', {})
        if providers:
            log.write("")
            log.write("[bold]🏢 提供商使用统计:[/bold]")
            for provider_name, provider_stats in providers.items():
                # provider_stats 是 int（请求计数）
                requests = provider_stats if isinstance(provider_stats, int) else provider_stats.get('requests', 0)
                log.write(f"  {provider_name}: {requests} 请求")

        # 模型使用统计
        models = real_time_stats.get('models', {})
        if models:
            log.write("")
            log.write("[bold]🤖 模型使用统计:[/bold]")
            for model_name, model_stats in models.items():
                requests = model_stats.get('requests', 0)
                tokens = model_stats.get('tokens', 0)
                log.write(f"  {model_name}: {requests} 请求, {tokens:,} tokens")

        # 其他信息
        log.write("")
        log.write(f"[bold]📜 历史记录数量:[/bold] {len(self.history)}")

        last_update = real_time_stats.get('last_update', 0)
        if last_update > 0:
            time_since_update = time.time() - last_update
            update_time = datetime.fromtimestamp(last_update).strftime("%H:%M:%S")
            log.write(f"[bold]⏰ 最后更新:[/bold] {update_time} ({time_since_update:.1f}秒前)")

    async def simulate_api_call(self) -> None:
        """模拟API调用（测试用）"""
        log = self.query_one("#api-log", RichLog)

        log.write(f"\n[bold]🔧 模拟API调用...[/bold]")

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

        log.write(f"[green]✅ API调用成功! 消耗150 tokens[/green]")
        log.write(f"[dim]当前总Tokens: {self.token_stats['total_tokens']:,}[/dim]")

    def add_test_history(self) -> None:
        """添加测试历史记录"""
        test_records = [
            {"time": "10:30:15", "model": "claude-3-sonnet", "tokens": 85, "status": "success"},
            {"time": "11:45:22", "model": "claude-3-haiku", "tokens": 42, "status": "success"},
            {"time": "14:20:33", "model": "claude-3-opus", "tokens": 210, "status": "failed"},
            {"time": "15:55:47", "model": "claude-3-sonnet", "tokens": 120, "status": "success"},
        ]

        table = self.query_one("#history-table", DataTable)
        log = self.query_one("#api-log", RichLog)

        log.write(f"\n[bold]➕ 添加{len(test_records)}条测试历史记录[/bold]")

        for record in test_records:
            self.history.append(record)
            status_display = "✅ 成功" if record["status"] == "success" else "❌ 失败"
            table.add_row(
                record["time"],
                record["model"],
                str(record["tokens"]),
                status_display
            )

        log.write(f"[dim]历史记录总数: {len(self.history)}[/dim]")

    def action_refresh(self) -> None:
        """刷新显示"""
        self.update_token_display()
        log = self.query_one("#api-log", RichLog)
        log.write(f"[dim]🔄 已刷新 {datetime.now().strftime('%H:%M:%S')}[/dim]")

    def action_clear_log(self) -> None:
        """清空当前 Tab 的日志"""
        tabs = self.query_one("#main-tabs", TabbedContent)
        active = tabs.active
        if active == "tab-traffic":
            log = self.query_one("#traffic-log", RichLog)
        elif active == "tab-detail":
            log = self.query_one("#detail-log", RichLog)
        else:
            log = self.query_one("#api-log", RichLog)
        log.clear()
        log.write(f"[dim]🧹 日志已清空 {datetime.now().strftime('%H:%M:%S')}[/dim]")

    def action_quit(self) -> None:
        """退出程序"""
        # 创建异步任务来停止服务器并退出
        asyncio.create_task(self._async_quit())

    async def _async_quit(self) -> None:
        """异步退出程序，先停止服务器"""
        log = self.query_one("#api-log", RichLog)
        log.write(f"[dim]正在退出程序...[/dim]")

        # 停止服务器进程（如果正在运行）
        if self.server_status["running"]:
            log.write(f"[dim]正在停止API服务器...[/dim]")
            try:
                success = await self.server_manager.stop_server()
                if success:
                    log.write(f"[green]✅ API服务器已停止[/green]")
                else:
                    log.write(f"[yellow]⚠️  停止服务器失败，继续退出程序[/yellow]")
            except Exception as e:
                log.write(f"[yellow]⚠️  停止服务器时发生错误: {e}，继续退出程序[/yellow]")
            await asyncio.sleep(0.3)  # 短暂延迟让用户看到消息

        self.exit()


if __name__ == "__main__":
    app = APIProxyApp()
    app.run()