# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Anthropic API代理终端是一个带终端UI的API代理工具，使用Textual库实现三区域终端界面：
- 主区域：显示API请求/响应日志，接收用户输入
- 右上角：实时Token使用统计
- 右下角：历史对话记录，支持点击查看详情

项目目前处于开发初期，已完成基础UI布局，接下来需要实现API代理服务器功能。

## Development Environment

### Python Setup
This appears to be a Python project based on the `.gitignore` file patterns. While no dependency files exist yet, typical setup would include:

1. Create a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

2. Install development dependencies when they're added (likely in `requirements.txt`, `pyproject.toml`, or similar)

### Project Structure
Since this is a new repository, the structure hasn't been established. Future development might include:
- `src/` or `convertlogger/` for the main package
- `tests/` for test files
- `examples/` for usage examples
- Configuration files for logging, API keys, etc.

## Key Architectural Considerations

Based on the project description, these components are likely needed:

1. **Logging System**: Structured logging for conversation history
2. **API Client**: Wrapper around external API calls with retry logic
3. **Error Handler**: Mechanism to pause execution and await manual intervention on API failures
4. **State Management**: Persistence of conversation logs and recovery state

## Current Project Structure

The project now includes a terminal UI built with Textual:

### Core Files
- `api_proxy.py` - Main application with three-panel layout
- `app.css` - Styling for terminal UI components
- `requirements.txt` - Dependencies (textual, httpx, etc.)

### UI Layout
1. **Left Panel (75%)**: API log display + command input
2. **Right Panel (25%)**: Split vertically into:
   - Top-right (30%): Token statistics
   - Bottom-right (70%): History table (clickable)

### Development Commands
```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python api_proxy.py
# 或使用指定Python版本 (如 D:\conda\PyEnv\study\python.exe)
# "D:/conda/PyEnv/study/python.exe" api_proxy.py

# Test commands within the app
help      # Show help
clear     # Clear log
stats     # Show statistics
test      # Simulate API call
add       # Add test history
```

## Configuration

The project uses YAML-based configuration for managing multiple API providers:

### Configuration Files
- `config.yaml` - Main configuration file with providers and server settings
- `.env` - Environment variables for API keys (optional, referenced in config.yaml)

### Key Configuration Sections
1. **Server Configuration** - Host, port, timeout settings
2. **Provider Configuration** - Multiple API providers with authentication and models
3. **Per-Provider HTTP Proxy** - Proxy settings per provider with authentication support
4. **Forwarding Schemes** - Model-to-provider routing via named schemes
5. **Manual Provider Selection** - Manual provider selection via headers or URL parameters

### Provider Configuration Example
```yaml
providers:
  - name: "anthropic_official"
    enabled: true
    type: "anthropic"
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"  # Environment variable
    models:
      - "claude-3-opus-20240229"
      - "claude-3-sonnet-20240229"
    timeout: 60
```

### Proxy Configuration Example
```yaml
# Provider-specific proxy
providers:
  - name: "third_party_proxy"
    proxy_enabled: true
    proxy_url: "http://custom-proxy.example.com:8888"
    proxy_auth: "${PROXY_USER}:${PROXY_PASS}"
```

### Environment Variables
API keys should be stored in environment variables or `.env` file:
```bash
# .env file example
ANTHROPIC_API_KEY=sk-ant-xxx
AZURE_OPENAI_KEY=xxx
# HTTP proxy authentication
PROXY_USER=proxyuser
PROXY_PASS=proxypassword
```

### Configuration Management
Use the `config.py` module to load and validate configuration:
```python
from config import load_config

config = load_config("config.yaml")
enabled_providers = config.get_enabled_providers()
```

## API Server (api_server.py)

The API server (`api_server.py`) is an aiohttp-based proxy server that forwards requests to configured providers:

### Core Components
1. **APIServer** - Main server class managing provider clients and request handling
2. **ProviderClient** - HTTP client for communicating with individual providers
3. **StatisticsCollector** - Tracks request metrics and token usage

### Manual Provider Selection
Instead of load balancing, the system supports manual provider selection:

1. **Via HTTP Header**: Add `X-Provider: provider_name` to request headers
2. **Via URL Parameter**: Add `?provider=provider_name` to request URL
3. **Automatic Fallback**: If no provider specified, uses first provider supporting the requested model

### WebSocket Commands
Connect to `ws://<host>:<port>/ws` and send JSON messages in the format:
```json
{"type": "command", "data": {"action": "<action>", ...}}
```

| action | 参数 | 说明 |
|--------|------|------|
| `shutdown` | — | 触发优雅关闭 |
| `get stats` | — | 返回请求统计摘要 |
| `get config` | — | 返回配置摘要 |
| `set scheme` | `scheme` (string) | 切换当前转发方案 |
| `reload` | — | 热重载 providers 和 schemes，不停止监听端口 |
| `update models` | `provider` (string) | 向指定 provider 查询可用模型列表并同步到 config.yaml |

**`update models` 说明：**
- 调用各 provider 类型模块的 `list_models()` 函数向上游 API 获取模型列表
- 获取成功后同时更新内存配置和 config.yaml 文件中对应 provider 的 `models` 字段
- Anthropic 类型调用 `/v1/models`；OpenAI 兼容类型调用 `/v1/models`；Gemini 类型使用 `google-genai` SDK 的 `models.list()`
- 查询结果为空时不更新配置

```json
// 示例：更新 anthropic_official 的模型列表
{"type": "command", "data": {"action": "update models", "provider": "anthropic_official"}}
```

### Starting the Server
```bash
python api_server.py
# Or with specific config:
python api_server.py --config custom_config.yaml
```

## Common Development Tasks

When code is added, typical tasks might include:

- **Running tests**: `pytest` (if pytest is adopted)
- **Code formatting**: `black` or similar formatter
- **Linting**: `ruff` or `flake8` 
- **Type checking**: `mypy` or `pyright`

## Notes for Future Development

1. The `.gitignore` already includes patterns for Python development, suggesting this will be a Python project
2. Consider adding a `pyproject.toml` for modern Python project configuration
3. API keys and sensitive configuration should be kept out of version control (use `.env` files or similar)
4. The project's focus on "pausing for manual handling" suggests interactive or semi-automated workflows

## Repository Status

Current implementation includes:

### Completed
1. Terminal UI with three-panel layout (Textual)
2. YAML-based configuration system with environment variable support
3. Per-provider HTTP proxy support
4. API proxy server with scheme-based routing and manual provider selection
5. Statistics collection

### Pending Integration
1. Connect terminal UI with API server for real-time logging
2. Display real-time statistics in terminal UI
3. Persist conversation history and recovery state
4. End-to-end testing of the proxy system