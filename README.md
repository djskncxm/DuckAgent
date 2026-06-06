# DuckAgent — Android 逆向 Multi-Agent 系统

多 Agent 协作系统，用于 Android 逆向工程。Agent 平权——通过 `@agent_id` 互相点名，不设中心路由。

## 快速开始

```bash
# 1. 安装依赖
uv sync

# 2. 配置 LLM（.env）
cp .env.example .env
# 编辑 .env 填入 API key 和模型

# 3. 启动（一键全开，端口默认从 .env 读取）
uv run duck run

# 4. 单进程调试模式
uv run duck run --local
```

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  多进程模式                                                  │
│                                                             │
│  bus-server (FastAPI :8720)  ←── HTTP + WebSocket           │
│       ↑                                                     │
│       ├── main-agent 进程  (纯推理、协调)                    │
│       ├── trace-agent 进程 (ARM64 执行流分析)                │
│       ├── ida-jadx-agent 进程 (APK 静态分析)                │
│       └── tui 进程 (Textual 终端界面)                       │
│                                                             │
│  单进程模式 (duck run) —— 所有 agent 在同一 asyncio 进程内    │
│  进程内通过 asyncio.Queue 通信，适合开发调试。               │
└─────────────────────────────────────────────────────────────┘
```

| Agent | 职责 | 工具 |
|-------|------|------|
| **MainAgent** | 协调、拆解任务、综合结论 | 无（纯推理） |
| **TraceAgent** | ARM64 执行流分析、算法还原 | ak_search (mmap 行索引) |
| **IdaJadxAgent** | APK 静态代码分析 | JADX HTTP API (11 个工具) |

### 通信机制

Agent 通过 `@agent_id` 互相点名，平权路由：

```
Human: "@trace_agent 分析签名"          → trace_agent
trace_agent: "发现 HMAC, @ida_jadx_agent" → ida_jadx_agent
ida_jadx_agent: "@trace_agent 确认了"     → trace_agent
trace_agent: "结论: HMAC-SHA256"          → human
```

- `request` / `question` 触发 Agent 动作
- `conclusion` / `decision` 仅通知，不触发
- `status` 不持久化，仅 UI 状态更新

## 使用

```bash
# ── TUI 交互 ──
uv run duck run                         # 一键启动（多进程，默认）
uv run duck run --local                 # 单进程调试模式
uv run duck run --connect http://127.0.0.1:8720  # 仅 TUI，连接已有 bus

# ── 多进程管理 ──
uv run duck run                         # 一键启动全部进程
uv run duck run --port 9000             # 指定端口
uv run duck server                      # 仅启动消息总线
uv run duck agent trace_agent           # 单个 Agent（URL 从配置读）

# ── 命令行 ──
uv run duck send "@trace_agent 分析签名算法"
uv run duck log --from trace_agent --limit 10
uv run duck log --type conclusion

# ── curl 调试 ──
curl http://127.0.0.1:8720/api/v1/history
curl http://127.0.0.1:8720/api/v1/health
```

## 配置

通过 `.env` 配置（所有变量带 `DUCKAGENT_` 前缀）：

```bash
# LLM
OPENAI_API_KEY=sk-xxx
DUCKAGENT_LITELLM_MODEL=openai/deepseek-chat

# 总线
DUCKAGENT_BUS_TRANSPORT=local           # local | http
DUCKAGENT_BUS_SERVER_PORT=8720

# Trace 文件
DUCKAGENT_TRACE_CODE_FILE=/path/to/code.log
DUCKAGENT_TRACE_RW_FILE=/path/to/rw.log
DUCKAGENT_TRACE_BL_FILE=/path/to/bl.log

# JADX
DUCKAGENT_JADX_HOST=127.0.0.1
DUCKAGENT_JADX_PORT=8650
```

## Bus Server API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/v1/publish` | POST | 发布消息 |
| `/api/v1/history` | GET | 查询历史 (`?from=&type=&limit=`) |
| `/api/v1/health` | GET | 健康检查 |
| `/ws?agent_id=<id>` | WS | Agent 连接 |
| `/ws?role=observer` | WS | 观察者（TUI） |

## 运行测试

```bash
uv run pytest tests/ -v                 # 全部 85 个测试
uv run pytest tests/test_bus.py -v      # 消息总线
uv run pytest tests/test_server.py -v   # FastAPI 服务端
```

## 项目结构

```
src/duckagent/
├── bus/            # 消息总线（ABC + Local + HTTP 三种实现）
├── server/         # FastAPI 总线服务端
├── agents/         # Agent 实现（Main/Trace/IdaJadx）
├── processes/      # 多进程入口
├── tools/          # 工具执行器（ak_search / JADX HTTP）
├── cli/            # typer CLI + Textual TUI
├── launcher.py     # 多进程启动器
└── config.py       # pydantic-settings 配置
```

## 技术栈

Python 3.12+ · litellm · FastAPI · uvicorn · httpx · websockets · Textual · SQLite · aiosqlite · typer · structlog · Pydantic v2 · uv

## 作者疑问
不知道为啥Claude code 4.6的版本总是会先说一句"我先检查一下ida和jadx的状态"，然后就寄了
但是DeepSeek就会正常的去干活，看看后续怎么做吧，我听大佬说是因为cc的多思考，DeepSeek思考比较浅
