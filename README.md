# DuckAgent — Android 逆向 Multi-Agent 系统

多 Agent 协作系统，用于 Android 逆向工程。Agent 平权——通过 `@agent_id` 互相点名，不设中心路由。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置 LLM（.env）
cp .env.example .env
# 编辑 .env 填入 API key 和模型

# 3. 启动（tmux 多进程模式，默认）
uv run duck run

# 4. 单进程调试模式
uv run duck run --local
```

## 架构

### 多进程模式（`duck run`，默认）

```
bus-server (FastAPI + SQLite + WebSocket)   :8720
    ↑
    ├── main_agent   ── HTTP POST + WebSocket
    ├── trace_agent  ── 同上
    └── ida_jadx_agent ── 同上

tmux session: duckagent
    ├─ Win0: main      双窗格：上(agent stdout + rich 格式化) + 下(input)
    ├─ Win1: trace     双窗格：上(agent stdout + rich 格式化) + 下(input)
    ├─ Win2: ida       双窗格：上(agent stdout + rich 格式化) + 下(input)
    ├─ Win3: messages  bus monitor（WebSocket observer）
    └─ Win4: status    agent 状态仪表盘
```

每个 agent 窗口上下两个 pane：上 pane 是 agent 进程 stdout（rich 格式化成聊天日志），下 pane 是 1 行 prompt_toolkit 输入框。鼠标点击 status bar 切换窗口。

### MCP 工具连接（Agent 是纯客户端）

```
ida_jadx_agent (MCP client)
    ├─ ida-pro-mcp (Streamable HTTP)  → IDA Pro
    └─ jadx-mcp (stdio subprocess)    → JADX-GUI

trace_agent (MCP client)
    └─ trace (stdio subprocess)       → 内置 trace MCP server (ak_search)
```

Agent 不管理 MCP server 生命周期。连接是惰性的——第一次调工具时才连。连不上优雅降级，不崩溃。

### 单进程模式（`duck run --local`）

所有 agent 在同一进程内通过 LocalMessageBus 通信，prompt_toolkit 单行输入。不需要 tmux 和 bus server。

### 角色

| 角色 | agent_id | 职责 | MCP Servers |
|------|----------|------|-------------|
| MainAgent | main_agent | 协调、拆解任务、综合结论 | file（内置） |
| TraceAgent | trace_agent | 执行流分析、算法还原 | trace + file |
| IdaJadxAgent | ida_jadx_agent | 静态分析、反汇编、反编译 | ida-pro-mcp + jadx-mcp + file |
| 人（Leader） | human | 终审、路径决策 | tmux input pane / --local prompt_toolkit |

## 通信机制

### @mention 路由（Agent 平权）

```
Human: "@trace_agent 分析签名"                → trace_agent
trace_agent: "发现 HMAC, @ida_jadx_agent 确认" → ida_jadx_agent
ida_jadx_agent: "@trace_agent 确认了"           → trace_agent
trace_agent: "结论: HMAC-SHA256"                → human
```

路由规则：
- `to_agent` + `mentions` 双重路由，取并集
- `mentions` 从 content 中的 `@agent_id` 自动解析
- 发件人永远不收自己的消息
- 无显式收件人时广播

### 消息类型语义

| type | 含义 | 是否触发 agent 动作 |
|------|------|-------------------|
| request | 请求执行任务 | ✅ 是 |
| question | 需要回答的问题 | ✅ 是 |
| conclusion | 结论/报告/信息 | ❌ 否（CC 而已） |
| decision | 决策/判定 | ❌ 否 |
| status | Agent 状态广播 | ❌ 否，纯内存不持久化 |

## 使用

```bash
# === 默认模式（tmux 多进程，一键全开） ===
uv run duck run

# === 单进程模式（开发/调试） ===
uv run duck run --local

# === 连接到已有 bus server ===
uv run duck run --connect http://127.0.0.1:8720

# === 手动分步启动 ===
uv run duck server                      # 终端 1: 总线服务
uv run duck agent main_agent            # 终端 2
uv run duck agent trace_agent           # 终端 3
uv run duck agent ida_jadx_agent        # 终端 4

# === 命令行 ===
uv run duck send "@trace_agent 分析签名"
uv run duck log --from trace_agent --limit 10

# === curl 调试 ===
curl http://127.0.0.1:8720/api/v1/history
curl -X POST http://127.0.0.1:8720/api/v1/publish \
  -H "Content-Type: application/json" \
  -d '{"from_agent":"human","to_agent":"main_agent","mentions":[],"type":"request","content":"hello","evidence":[],"confidence":"high"}'
```

## Bus Server API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/publish` | POST | 发布消息，非 status 持久化 |
| `/api/v1/history` | GET | 查询历史（支持 from/type/limit 过滤） |
| `/api/v1/health` | GET | 健康检查 |
| `/ws?agent_id=<id>` | WS | Agent 连接，服务端过滤推送 |
| `/ws?role=observer` | WS | Observer 连接，推送全部消息 |
| `/ws?role=status` | WS | Status 连接，仅推送状态消息 |

## 配置

通过 `.env` 文件（不提交到 git）：

```bash
# === LLM ===
OPENAI_API_KEY=sk-xxx
OPENAI_API_BASE=https://api.deepseek.com/v1
DUCKAGENT_LITELLM_MODEL=openai/deepseek-chat

# === 消息总线 ===
DUCKAGENT_BUS_TRANSPORT=local           # local | http
DUCKAGENT_BUS_SERVER_HOST=127.0.0.1
DUCKAGENT_BUS_SERVER_PORT=8720

# === 数据库 ===
DUCKAGENT_DB_DIR=.duckagent

# === Trace 文件 ===
DUCKAGENT_TRACE_CODE_FILE=/path/to/code.log
DUCKAGENT_TRACE_RW_FILE=/path/to/rw.log
DUCKAGENT_TRACE_BL_FILE=/path/to/bl.log

# === MCP 服务器映射 ===
DUCKAGENT_TRACE_AGENT_MCP_SERVERS=trace,file
DUCKAGENT_JADX_AGENT_MCP_SERVERS=ida-pro-mcp,jadx-mcp,file
DUCKAGENT_MAIN_AGENT_MCP_SERVERS=file
DUCKAGENT_MCP_JSON_PATHS=~/.claude/.mcp.json

# === 日志级别 ===
DUCKAGENT_LOG_LEVEL=WARNING             # DEBUG | INFO | WARNING | ERROR
```

MCP server 配置在 `~/.claude/.mcp.json`（Claude Code 兼容格式）：

```json
{
    "mcpServers": {
        "ida-pro-mcp": {
            "type": "http",
            "url": "http://127.0.0.1:13337/mcp"
        },
        "jadx-mcp": {
            "command": "uv",
            "args": ["run", "/path/to/jadx_mcp_server.py"]
        }
    }
}
```

内置 MCP servers（无需配置）：
- **trace** — trace 文件搜索（FastMCP + ak_search C daemon）
- **file** — 通用文件读写（file_read / file_write / file_list / file_append）

## 运行测试

```bash
uv run pytest tests/ -v
```

## 项目结构

```
src/duckagent/
├── bus/
│   ├── interface.py       # MessageBus ABC
│   ├── models.py          # Message 数据模型（含 @mentions）
│   ├── store.py           # LocalMessageBus: SQLite + Queue 分发
│   ├── http_client.py     # HttpMessageBus: HTTP POST + WebSocket 接收
│   └── _db.py             # 共享 SQLite schema
├── server/
│   ├── app.py             # FastAPI app + lifespan
│   ├── routes.py          # REST + WebSocket 端点
│   ├── ws_manager.py      # ConnectionManager（agent/observer/status）
│   ├── db.py              # SQLite 持久层
│   └── dispatcher.py      # 纯函数路由逻辑
├── agents/
│   ├── base.py            # BaseAgent: 生命周期、think()、MCP + 本地工具 calling
│   ├── main_agent.py      # MainAgent: @mention 路由、JSON 清理
│   ├── trace_agent.py     # TraceAgent: trace 分析
│   └── ida_jadx_agent.py  # IdaJadxAgent: IDA + JADX 静态分析
├── mcp/
│   ├── client_manager.py  # McpClientManager: 惰性连接、工具路由
│   ├── schema_converter.py # MCP Tool → OpenAI function-calling 格式
│   └── servers/
│       ├── trace_server.py  # 内置 trace MCP server (FastMCP + ak_search)
│       ├── file_server.py   # 内置 file MCP server
│       └── jadx_server.py   # 内置 JADX MCP server wrapper
├── tools/
│   ├── trace_executor.py  # LocalTraceToolExecutor（被 trace MCP server 复用）
│   └── jadx_executor.py   # JadxToolExecutor（被 jadx MCP server 复用）
├── processes/
│   └── agent_process.py   # Agent 进程入口（工厂 + 信号处理）
├── tmux/
│   ├── session.py         # TmuxSession: libtmux 会话管理 + 双窗格布局
│   ├── console.py         # AgentConsole: rich 格式化 agent stdout 输出
│   ├── input_pane.py      # prompt_toolkit 输入进程（下 pane）
│   ├── local_app.py       # 单进程 prompt_toolkit chat（--local 模式）
│   ├── bus_monitor.py     # 消息总线监控（win3）
│   └── status_dashboard.py # Agent 状态仪表盘（win4）
├── cli/
│   └── app.py             # typer CLI: run/log/send/server/agent
├── launcher.py            # 多进程启动器
└── config.py              # pydantic-settings 配置 + MCP 注册表
tools/search/              # ak_search C 源码 + 编译产物
prompts/                   # agent system prompts
```

## 技术栈

Python 3.12+ · litellm · FastAPI · uvicorn · httpx · websockets · libtmux · prompt_toolkit · rich · SQLite · aiosqlite · typer · structlog · Pydantic v2 · MCP (Model Context Protocol) · uv
