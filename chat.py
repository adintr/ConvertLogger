#!/usr/bin/env python3
"""
Anthropic API 聊天客户端

一个简单的命令行聊天程序，使用 Anthropic Claude API 进行对话。
支持流式输出和对话历史管理。

使用方法:
1. 设置环境变量 ANTHROPIC_API_KEY
2. 运行: python chat.py
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
import asyncio
from typing import Optional, List, Dict, Any
import argparse

try:
    import anthropic
except ImportError:
    print("错误: 需要安装 anthropic 包")
    print("请运行: pip install anthropic")
    sys.exit(1)


class ChatClient:
    """Anthropic API 聊天客户端"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-3-haiku-20240307",
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 1000,
        stream: bool = True
    ):
        """初始化聊天客户端

        Args:
            api_key: Anthropic API 密钥，如果为 None 则从环境变量读取
            model: 使用的模型名称
            system_prompt: 系统提示词
            temperature: 温度参数 (0.0-1.0)
            max_tokens: 最大生成token数
            stream: 是否使用流式输出
        """
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "未提供 API 密钥。请设置环境变量 ANTHROPIC_API_KEY "
                "或通过参数提供 api_key"
            )

        self.client = anthropic.Anthropic(api_key=self.api_key)
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

    def add_message(self, role: str, content: str) -> None:
        """添加消息到历史

        Args:
            role: 角色 ('user' 或 'assistant')
            content: 消息内容
        """
        self.history.append({"role": role, "content": content})

    def format_history(self) -> List[Dict[str, str]]:
        """格式化对话历史以供 API 使用"""
        formatted = []
        for msg in self.history:
            # Anthropic API 使用 'user' 和 'assistant' 角色
            formatted.append({
                "role": msg["role"],
                "content": msg["content"]
            })
        return formatted

    async def get_response_async(self, user_input: str) -> str:
        """异步获取响应

        Args:
            user_input: 用户输入

        Returns:
            AI 响应内容
        """
        # 添加用户消息到历史
        self.add_message("user", user_input)

        try:
            if self.stream:
                return await self._get_streaming_response()
            else:
                return await self._get_complete_response()
        except Exception as e:
            # 从历史中移除失败的用户消息
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
            raise e

    async def _get_streaming_response(self) -> str:
        """获取流式响应"""
        print("\nClaude: ", end="", flush=True)

        full_response = ""
        messages = self.format_history()

        # 流式调用
        with self.client.messages.stream(
            model=self.model,
            messages=messages,
            system=self.system_prompt,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                full_response += text

        print()  # 换行
        return full_response

    async def _get_complete_response(self) -> str:
        """获取完整响应（非流式）"""
        messages = self.format_history()

        response = self.client.messages.create(
            model=self.model,
            messages=messages,
            system=self.system_prompt,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        full_response = response.content[0].text
        print(f"\nClaude: {full_response}")
        return full_response

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


async def interactive_chat(args):
    """交互式聊天会话"""
    try:
        client = ChatClient(
            model=args.model,
            system_prompt=args.system,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            stream=not args.no_stream
        )
    except ValueError as e:
        print(f"错误: {e}")
        return

    print("\n" + "="*50)
    print("Anthropic API 聊天客户端")
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
            try:
                response = await client.get_response_async(user_input)
                client.add_message("assistant", response)
            except anthropic.APIConnectionError as e:
                print(f"网络连接错误: {e}")
            except anthropic.APIStatusError as e:
                print(f"API 错误 (状态码 {e.status_code}): {e}")
            except Exception as e:
                print(f"未知错误: {e}")

        except KeyboardInterrupt:
            print("\n\n程序被中断")
            break
        except EOFError:
            print("\n\n程序结束")
            break


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="Anthropic API 聊天客户端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument(
        "--model",
        default="claude-3-haiku-20240307",
        help="模型名称 (默认: claude-3-haiku-20240307)"
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

    args = parser.parse_args()

    # 运行交互式聊天
    asyncio.run(interactive_chat(args))


if __name__ == "__main__":
    main()