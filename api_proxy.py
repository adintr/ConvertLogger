#!/usr/bin/env python3
"""
Anthropic API代理终端
将终端划分为三个区域显示不同内容
"""

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Header, Footer, Static, RichLog,
    DataTable, Input, Label
)
from textual.binding import Binding
from datetime import datetime
import asyncio
from typing import Dict, Any, List


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
        self.token_stats = {
            "total_calls": 0,
            "total_tokens": 0,
            "success_calls": 0,
            "failed_calls": 0,
        }
        self.history: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        """创建UI布局"""
        yield Header()

        # 主容器：左右分割
        with Horizontal(id="main-container"):
            # 左侧：主区域 (75%宽度)
            with Container(id="left-panel"):
                yield Label("API请求/响应日志", classes="panel-title")
                yield RichLog(id="api-log", wrap=True, highlight=True)
                yield Input(
                    placeholder="输入命令 (help查看帮助)...",
                    id="command-input"
                )

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
        self.title = "Anthropic API代理终端"
        self.sub_title = "按 Ctrl+C 退出 | 按 help 查看命令"

        # 初始化历史表格
        table = self.query_one("#history-table", DataTable)
        table.add_columns("时间", "模型", "Tokens", "状态")
        table.add_rows([])

        # 初始化token显示
        self.update_token_display()

        # 显示欢迎信息
        log = self.query_one("#api-log", RichLog)
        log.write(f"[bold cyan]🚀 Anthropic API代理终端已启动[/bold cyan]")
        log.write(f"[dim]当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
        log.write(f"[dim]输入 'help' 查看可用命令[/dim]")
        log.write("")

    def update_token_display(self) -> None:
        """更新token统计显示"""
        stats = self.token_stats
        display = self.query_one("#token-display", Static)

        content = (
            f"[bold]总调用次数:[/bold] {stats['total_calls']}\n"
            f"[bold]总Tokens:[/bold] {stats['total_tokens']:,}\n"
            f"[bold green]成功:[/bold green] {stats['success_calls']}\n"
            f"[bold red]失败:[/bold red] {stats['failed_calls']}\n"
            f"[dim]更新时间: {datetime.now().strftime('%H:%M:%S')}[/dim]"
        )
        display.update(content)

    @on(DataTable.RowSelected, "#history-table")
    def on_history_row_selected(self, event: DataTable.RowSelected) -> None:
        """处理历史记录点击事件"""
        if event.row_index is not None and event.row_index < len(self.history):
            history_item = self.history[event.row_index]
            log = self.query_one("#api-log", RichLog)
            log.write(f"\n[bold]📖 显示历史记录 #{event.row_index + 1}[/bold]")
            log.write(f"时间: {history_item.get('time', 'N/A')}")
            log.write(f"模型: {history_item.get('model', 'N/A')}")
            log.write(f"Tokens: {history_item.get('tokens', 0)}")
            log.write(f"状态: {history_item.get('status', 'unknown')}")

            # 滚动到日志底部
            log.scroll_end(animate=False)

    @on(Input.Submitted, "#command-input")
    async def on_command_submitted(self, event: Input.Submitted) -> None:
        """处理用户命令输入"""
        command = event.value.strip()
        event.input.value = ""  # 清空输入框

        if not command:
            return

        log = self.query_one("#api-log", RichLog)

        # 处理命令
        if command.lower() == "help":
            self.show_help()
        elif command.lower() == "clear":
            log.clear()
            log.write("[dim]日志已清空[/dim]")
        elif command.lower() == "stats":
            self.show_stats()
        elif command.lower() == "test":
            await self.simulate_api_call()
        elif command.lower() == "add":
            self.add_test_history()
        elif command.lower() in ["exit", "quit"]:
            log.write(f"[dim]正在退出程序...[/dim]")
            await asyncio.sleep(0.5)  # 短暂延迟让用户看到消息
            self.action_quit()
        else:
            log.write(f"[yellow]❓ 未知命令: {command}[/yellow]")
            log.write(f"[dim]输入 'help' 查看可用命令[/dim]")

    def show_help(self) -> None:
        """显示帮助信息"""
        log = self.query_one("#api-log", RichLog)
        log.write("\n[bold]📋 可用命令:[/bold]")
        log.write("  [cyan]help[/cyan]    - 显示此帮助信息")
        log.write("  [cyan]clear[/cyan]   - 清空日志")
        log.write("  [cyan]stats[/cyan]   - 显示详细统计")
        log.write("  [cyan]test[/cyan]    - 模拟API调用")
        log.write("  [cyan]add[/cyan]     - 添加测试历史记录")
        log.write("  [cyan]exit/quit[/cyan] - 退出程序")
        log.write("")
        log.write("[bold]📊 界面说明:[/bold]")
        log.write("  左侧: API请求/响应日志区域")
        log.write("  右上: Token使用统计")
        log.write("  右下: 历史对话记录 (点击查看详情)")
        log.write("")

    def show_stats(self) -> None:
        """显示详细统计信息"""
        log = self.query_one("#api-log", RichLog)
        stats = self.token_stats

        log.write("\n[bold]📈 详细统计信息:[/bold]")
        log.write(f"  总API调用次数: {stats['total_calls']}")
        log.write(f"  总Tokens消耗: {stats['total_tokens']:,}")
        log.write(f"  成功调用: {stats['success_calls']}")
        log.write(f"  失败调用: {stats['failed_calls']}")

        if stats['total_calls'] > 0:
            success_rate = (stats['success_calls'] / stats['total_calls']) * 100
            log.write(f"  成功率: {success_rate:.1f}%")
            avg_tokens = stats['total_tokens'] / stats['total_calls']
            log.write(f"  平均Tokens/次: {avg_tokens:.0f}")

        log.write(f"  历史记录数量: {len(self.history)}")

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
            "[green]成功[/green]"
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
            status_display = "[green]成功[/green]" if record["status"] == "success" else "[red]失败[/red]"
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
        """清空日志"""
        log = self.query_one("#api-log", RichLog)
        log.clear()
        log.write(f"[dim]🧹 日志已清空 {datetime.now().strftime('%H:%M:%S')}[/dim]")


if __name__ == "__main__":
    app = APIProxyApp()
    app.run()