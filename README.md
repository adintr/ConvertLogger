# Anthropic API代理终端

一个带终端UI的Anthropic API代理工具，将终端划分为三个区域显示不同内容：
- **主区域**: 显示API请求/响应日志，接收用户输入命令
- **右上角**: 实时Token使用统计
- **右下角**: 历史对话记录，点击可查看详情

## 功能特性

✅ **三区域终端UI** - 使用Textual库实现的现代化终端界面  
✅ **实时统计显示** - Token使用量、调用次数、成功率等  
✅ **历史记录管理** - 点击查看历史对话详情  
✅ **交互式命令** - 支持help、clear、stats等命令  
✅ **模拟API调用** - 用于测试和演示  

## 快速开始

### 安装依赖
```bash
pip install -r requirements.txt
```

### 运行应用
```bash
python api_proxy.py
```

### 常用命令
- `help` - 显示帮助信息
- `clear` - 清空日志
- `stats` - 显示详细统计
- `test` - 模拟API调用
- `add` - 添加测试历史记录

## 项目结构
```
├── api_proxy.py      # 主程序
├── app.css           # 终端UI样式
├── config.py         # 配置管理类
├── config.yaml       # 配置文件示例
├── requirements.txt  # Python依赖
├── README.md         # 项目说明
├── CLAUDE.md         # Claude开发指南
├── LICENSE           # Apache 2.0许可证
└── .gitignore        # Git忽略配置
```

## 配置说明

项目使用YAML格式配置文件管理多个API提供商，支持环境变量和复杂路由规则。

### 配置文件
- `config.yaml` - 主配置文件，包含服务器设置、提供商配置、路由规则等
- `.env` - 环境变量文件（可选），用于存储API密钥

### 主要配置项
1. **服务器配置** - 监听地址、端口、超时设置
2. **提供商配置** - 支持多个Anthropic API提供商，每个提供商可配置：
   - API密钥（支持环境变量 `${VAR_NAME}`）
   - 支持的模型列表
   - 请求超时和速率限制
   - 负载均衡权重
   - **HTTP代理支持** - 可配置代理服务器访问API
3. **HTTP代理配置** - 全局代理设置，支持认证和绕过规则
4. **路由规则** - 基于模型名称、请求参数、时间等的路由策略
5. **负载均衡** - 轮询、加权、最少连接等策略

### 快速配置示例
```yaml
# config.yaml 简化示例
server:
  port: 8000

# 全局HTTP代理配置
proxy:
  enabled: true
  url: "http://proxy.example.com:8080"
  auth: "${PROXY_USER}:${PROXY_PASS}"

providers:
  - name: "anthropic_official"
    enabled: true
    base_url: "https://api.anthropic.com"
    api_key: "${ANTHROPIC_API_KEY}"
    models:
      - "claude-3-opus-20240229"
      - "claude-3-sonnet-20240229"
    # 提供商特定代理配置（覆盖全局设置）
    proxy_enabled: true
    proxy_url: "http://custom-proxy.example.com:8888"
```

### 环境变量
```bash
# .env 文件
ANTHROPIC_API_KEY=sk-ant-xxx
AZURE_OPENAI_KEY=xxx
# HTTP代理认证
PROXY_USER=proxyuser
PROXY_PASS=proxypassword
```

查看 `config.yaml` 文件获取完整配置选项和说明。

## 手动Provider选择

API代理服务器支持手动选择provider，提供以下方式：

### 1. 通过请求头选择
在HTTP请求中添加 `X-Provider` 请求头指定provider名称：
```bash
curl -H "X-Provider: anthropic_official" \
     -H "Content-Type: application/json" \
     -X POST http://localhost:8000/v1/messages \
     -d '{"model": "claude-3-opus-20240229", "messages": [...]}'
```

### 2. 通过URL参数选择
在URL中添加 `provider` 查询参数：
```bash
curl -X POST "http://localhost:8000/v1/messages?provider=anthropic_official" \
     -H "Content-Type: application/json" \
     -d '{"model": "claude-3-opus-20240229", "messages": [...]}'
```

### 3. 自动选择逻辑
- 如果指定了provider但该provider不支持请求的模型，系统会返回错误
- 如果未指定provider，系统会选择支持该模型的第一个provider
- 如果未指定provider且没有provider支持该模型，系统会返回错误

### 4. Provider健康检查
每个provider都有健康状态检查，不健康的provider不会被自动选择，但可以通过手动指定强制使用。

## 技术栈
- **Textual** - 现代化终端UI框架
- **Rich** - 终端富文本显示
- **Python 3.8+** - 后端逻辑
- **PyYAML** - YAML配置文件解析

## 界面布局
```
┌──────────────────────┬─────────────┐
│                      │  右上角     │
│       主区域         │ token统计   │
│  (发送/接收文本 +    │             │
│     用户输入)        ├─────────────┤
│                      │  右下角     │
│                      │  历史摘要    │
│                      │ (可点击)    │
└──────────────────────┴─────────────┘
```

## 项目状态

### 已完成的核心功能

1. **终端UI框架**
   - 使用Textual库实现三区域终端界面
   - 主区域：API请求/响应日志显示 + 用户命令输入
   - 右上角：Token使用统计显示区域
   - 右下角：历史对话摘要表格（支持点击查看详情）
   - 支持help、clear、stats、test、add、exit等交互命令

2. **配置管理系统**
   - YAML格式配置文件 (`config.yaml`)
   - 环境变量替换支持 (${VAR_NAME} 格式)
   - 支持多个API提供商配置
   - 每个提供商可单独配置HTTP代理、认证、模型支持等
   - 配置文件验证和错误检查

3. **HTTP代理支持**
   - 全局HTTP代理配置
   - 每个提供商可独立配置代理
   - 支持代理认证 (username:password)
   - 绕过本地地址和特定域名的配置

4. **API代理服务器**
   - 基于aiohttp的异步HTTP服务器
   - 支持Anthropic API兼容端点 (`/v1/messages`, `/v1/completions`)
   - 通用代理端点 (`/{path:.*}`)
   - 监控端点 (`/health`, `/stats`, `/config`)

5. **手动Provider选择机制**
   - 移除了自动负载均衡器
   - 支持通过 `X-Provider` 请求头手动选择provider
   - 支持通过 `provider` 查询参数手动选择
   - 自动回退：如果未指定provider，使用支持请求模型的第一个provider
   - Provider健康检查机制 (错误计数监控)

6. **统计与监控**
   - 请求统计收集 (成功率、响应时间、token使用量)
   - Provider级别的请求计数和错误计数
   - 实时健康状态检查

### 技术架构
- **前端**: Textual (终端UI框架), Rich (富文本显示)
- **后端**: aiohttp (HTTP服务器), httpx (HTTP客户端)
- **配置**: PyYAML (YAML解析), python-dotenv (环境变量)
- **异步**: asyncio (异步编程)
- **数据模型**: dataclasses (配置类)

## 开发计划
- [x] 基础终端UI布局
- [x] 配置文件支持 (YAML格式)
- [x] API代理服务器实现（基础功能）
- [x] 手动Provider选择功能
- [ ] 终端UI与API服务器集成
- [ ] 持久化存储
- [ ] 实时统计数据显示

## 下一步开发重点

### 1. 终端UI与API服务器集成
**目标**: 将Textual终端UI与aiohttp API服务器集成，实现实时日志显示和交互控制
- 在终端UI中启动和管理API服务器进程
- 实时显示API请求/响应日志到主区域
- 将统计信息实时更新到右上角区域
- 支持通过终端命令控制服务器（启动、停止、重启）

### 2. 持久化存储
**目标**: 实现对话历史、配置状态和统计数据的持久化存储
- SQLite数据库存储对话历史记录
- 配置文件版本管理和备份
- 统计数据的定期保存和查询
- 异常恢复机制：服务器崩溃后能恢复状态

### 3. 实时统计数据显示
**目标**: 在终端UI中实时显示详细的统计信息
- 右上角区域显示实时Token使用统计
- 请求成功率、响应时间趋势图
- 各Provider的使用情况和健康状态
- 可配置的统计刷新频率

### 4. 高级功能规划
- **Provider自动切换**: 基于错误率、响应时间等指标自动切换provider
- **API密钥轮换**: 支持多个API密钥的自动轮换使用
- **请求队列管理**: 优先处理重要请求，限制并发数
- **Webhook通知**: 关键事件（如provider故障）的实时通知
- **CLI工具**: 提供命令行工具进行配置管理和监控
