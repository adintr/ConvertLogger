#!/usr/bin/env python3
"""
Anthropic API 聊天客户端 (HTTPX版本)

使用 httpx 直接调用 Anthropic API 的简单聊天程序。
无需安装 anthropic SDK，仅依赖 httpx。

使用方法:
1. 设置环境变量 ANTHROPIC_API_KEY
2. 运行: python chat_httpx.py
3. 输入消息开始对话，输入 'quit' 或 'exit' 退出

可选参数:
  --model MODEL     指定模型 (默认: claude-3-haiku-20240307)
  --system SYSTEM  设置系统提示词
  --temperature T  设置温度参数 (0.0-1.0)
  --max-tokens N   设置最大生成token数
  --no-stream      禁用流式输出
"""

import os
import sys
import json
import asyncio
from typing import Optional, List, Dict, Any
import argparse

try:
    import httpx
except ImportError:
    print("错误: 需要安装 httpx 包")
    print("请运行: pip install httpx")
    sys.exit(1)


class AnthropicClient:
    """Anthropic API 客户端 (HTTPX版本)"""

    BASE_URL = "http://localhost:11434"
    BASE_URL = "http://localhost:8000"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: int = 60,
        proxy: Optional[str] = None
    ):
        """初始化客户端

        Args:
            api_key: Anthropic API 密钥，如果为 None 则从环境变量读取
            timeout: 请求超时时间(秒)
            proxy: HTTP代理服务器地址 (可选)
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "未提供 API 密钥。请设置环境变量 ANTHROPIC_API_KEY "
                "或通过参数提供 api_key"
            )

        # 创建HTTP客户端
        client_kwargs = {
            "timeout": timeout,
            "headers": {
                "x-api-key": self.api_key,
                "anthropic-version": self.API_VERSION,
                "content-type": "application/json"
            }
        }

        if proxy:
            client_kwargs["proxies"] = {"http": proxy, "https": proxy}

        self.client = httpx.AsyncClient(**client_kwargs)

    async def close(self):
        """关闭HTTP客户端"""
        await self.client.aclose()

    async def create_message(
        self,
        model: str = "claude-3-haiku-20240307",
        messages: List[Dict[str, str]] = None,
        system: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        stream: bool = True
    ) -> Dict[str, Any]:
        """创建消息 (调用 /v1/messages 端点)

        Args:
            model: 模型名称
            messages: 消息列表
            system: 系统提示词
            max_tokens: 最大生成token数
            temperature: 温度参数
            stream: 是否使用流式输出

        Returns:
            API响应数据
        """
        url = f"{self.BASE_URL}/v1/messages"

        data = {
            "model": model,
            "messages": messages or [],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system:
            data["system"] = system

        if stream:
            data["stream"] = True
            return await self._stream_response(url, data)
        else:
            return await self._complete_response(url, data)

    async def _complete_response(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """获取完整响应（非流式）"""
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def _stream_response(self, url: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """处理流式响应"""
        async with self.client.stream("POST", url, json=data) as response:
            response.raise_for_status()

            full_response = ""
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue

                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break

                    try:
                        event_data = json.loads(data_str)
                        if "delta" in event_data and "text" in event_data["delta"]:
                            text = event_data["delta"]["text"]
                            print(text, end="", flush=True)
                            full_response += text
                        elif "content_block" in event_data:
                            # 处理内容块
                            pass
                    except json.JSONDecodeError:
                        continue

            print()  # 换行
            return {"content": full_response}

    async def test_connection(self) -> bool:
        """测试API连接"""
        try:
            # 尝试不同的端点以兼容不同服务
            # 对于 Anthropic: /v1/models
            # 对于 Ollama: /api/tags 或 /api/version
            endpoints = ["/v1/models", "/api/tags", "/api/version", "/"]

            for endpoint in endpoints:
                try:
                    response = await self.client.get(f"{self.BASE_URL}{endpoint}", timeout=5.0)
                    if response.status_code == 200:
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False


class ChatClient:
    """聊天客户端"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-3-haiku-20240307",
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        stream: bool = True,
        proxy: Optional[str] = None
    ):
        """初始化聊天客户端

        Args:
            api_key: Anthropic API 密钥
            model: 使用的模型名称
            system_prompt: 系统提示词
            temperature: 温度参数
            max_tokens: 最大生成token数
            stream: 是否使用流式输出
            proxy: HTTP代理服务器地址
        """
        self.client = AnthropicClient(api_key=api_key, proxy=proxy)
        self.model = model
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.stream = stream

        # 对话历史
        self.history: List[Dict[str, Any]] = []

        print(f"初始化聊天客户端，模型: {self.model}")
        if self.system_prompt:
            print(f"系统提示词: {self.system_prompt[:50]}...")

        # 对话历史
        self.history: List[Dict[str, Any]] = []

        print(f"初始化聊天客户端，模型: {self.model}")
        if self.system_prompt:
            print(f"系统提示词: {self.system_prompt[:50]}...")

    async def test_connection(self) -> bool:
        """测试连接并显示结果"""
        print("测试API连接...", end="", flush=True)
        connected = await self.client.test_connection()
        if connected:
            print("成功")
        else:
            print("失败")
            print("警告: API连接测试失败，可能无法正常工作")
        return connected

    def add_message(self, role: str, content: str) -> None:
        """添加消息到历史"""
        self.history.append({"role": role, "content": content})

    def format_history(self) -> List[Dict[str, str]]:
        """格式化对话历史以供 API 使用"""
        formatted = []
        for msg in self.history:
            formatted.append({
                "role": msg["role"],
                "content": msg["content"]
            })
        return formatted

    async def get_response(self, user_input: str) -> str:
        """获取响应

        Args:
            user_input: 用户输入

        Returns:
            AI 响应内容
        """
        # 添加用户消息到历史
        self.add_message("user", user_input)

        try:
            messages = self.format_history()

            response = await self.client.create_message(
                model=self.model,
                messages=messages,
                system=self.system_prompt,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                stream=self.stream
            )

            if self.stream:
                # 流式响应已打印，直接返回文本
                full_response = response.get("content", "")
            else:
                # 非流式响应
                full_response = ""
                if "content" in response and len(response["content"]) > 0:
                    for content_block in response["content"]:
                        if content_block["type"] == "text":
                            full_response = content_block["text"]
                            print(f"\nClaude: {full_response}")
                            break

            # 添加助手响应到历史
            if full_response:
                self.add_message("assistant", full_response)

            return full_response

        except httpx.HTTPStatusError as e:
            error_msg = f"API 错误 (状态码 {e.response.status_code}): {e.response.text}"
            print(f"错误: {error_msg}")
            # 从历史中移除失败的用户消息
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
            raise Exception(error_msg)
        except Exception as e:
            print(f"错误: {e}")
            # 从历史中移除失败的用户消息
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
            raise e

    def clear_history(self) -> None:
        """清空对话历史"""
        self.history.clear()
        print("对话历史已清空")

    def show_history(self) -> None:
        """显示对话历史"""
        if not self.history:
            print("对话历史为空")
            return

        print("\n=== 对话历史 ===")
        for i, msg in enumerate(self.history, 1):
            role_display = "用户" if msg["role"] == "user" else "Claude"
            # 截断长消息以便显示
            content = msg["content"]
            if len(content) > 100:
                content = content[:97] + "..."
            print(f"{i}. {role_display}: {content}")
        print("================\n")

    async def close(self):
        """关闭客户端"""
        await self.client.close()


async def interactive_chat(args):
    """交互式聊天会话"""
    try:
        client = ChatClient(
            model=args.model,
            system_prompt=args.system,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            stream=not args.no_stream,
            proxy=args.proxy,
            api_key="hello"
        )
    except ValueError as e:
        print(f"错误: {e}")
        return

    # 测试连接
    connected = await client.test_connection()
    if not connected:
        print("继续运行，但API连接可能有问题")

    print("\n" + "="*50)
    print("Anthropic API 聊天客户端 (HTTPX版本)")
    print("输入消息开始对话，输入以下命令:")
    print("  /help     - 显示帮助")
    print("  /history  - 显示对话历史")
    print("  /clear    - 清空对话历史")
    print("  /exit     - 退出程序")
    print("="*50 + "\n")

    while True:
        try:
            user_input = input("你: ").strip()

            if not user_input:
                continue

            # 处理命令
            if user_input.lower() in ["/exit", "/quit", "exit", "quit"]:
                print("再见!")
                break
            elif user_input.lower() == "/help":
                print("\n可用命令:")
                print("  /help     - 显示此帮助")
                print("  /history  - 显示对话历史")
                print("  /clear    - 清空对话历史")
                print("  /exit     - 退出程序")
                print("  其他输入将作为消息发送给 Claude\n")
                continue
            elif user_input.lower() == "/history":
                client.show_history()
                continue
            elif user_input.lower() == "/clear":
                client.clear_history()
                continue

            # 发送消息
            print("\nClaude: ", end="", flush=True)
            try:
                await client.get_response(user_input)
            except Exception:
                # 错误已在上层处理
                pass

        except KeyboardInterrupt:
            print("\n\n程序被中断")
            break
        except EOFError:
            print("\n\n程序结束")
            break

    # 关闭客户端
    await client.close()


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Anthropic API 聊天客户端 (HTTPX版本)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--model",
        default="deepseek-coder:6.7b",
        help="模型名称 (默认: deepseek-coder:6.7b)"
    )
    parser.add_argument(
        "--system",
        help="系统提示词"
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="温度参数 (0.0-1.0, 默认: 0.7)"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=1000,
        help="最大生成token数 (默认: 1000)"
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="禁用流式输出"
    )
    parser.add_argument(
        "--proxy",
        help="HTTP代理服务器地址 (例如: http://proxy.example.com:8080)"
    )

    args = parser.parse_args()

    # 运行交互式聊天
    asyncio.run(interactive_chat(args))


if __name__ == "__main__":
    main()