# 02Finance — A 股多 Agent 投研分析系统

基于 **LangGraph** 编排、**ReAct Agent** 与 **MCP（Model Context Protocol）** 的 A 股投资研究辅助工具：并行完成基本面、技术面、估值与新闻分析，汇总为结构化 Markdown 报告，并记录全链路执行日志便于复盘与评测。

> **声明**：本系统输出仅供研究参考，不构成投资建议。数据来源于公开接口与网络检索，请以交易所及公司公告为准。

---

## 项目简介

| 模块 | 说明 |
|------|------|
| **Financial-MCP-Agent** | 主程序：LangGraph 工作流、四个分析 Agent + 总结 Agent、执行日志、报告落盘 |
| **a-share-mcp-is-just-i-need** | MCP 服务端（FastMCP）：将 Baostock 行情/财报等封装为工具；新闻能力含检索与解析（实现见数据源层） |

**典型用户输入**：自然语言描述标的与分析诉求（建议同时给出 **股票代码** 以提高解析准确度）。  
**典型输出**：`reports/` 下的 Markdown 报告；`logs/<execution_id>/` 下的 Agent 与 LLM 交互日志。

---

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     Financial-MCP-Agent (Host)                   │
│  ┌──────────┐                                                    │
│  │ main.py  │ 解析 query → 初始化 AgentState → 编译 LangGraph      │
│  └────┬─────┘                                                    │
│       │ fan-out                                                  │
│       ▼                                                          │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐               │
│  │基本面    │ │技术面    │ │估值      │ │新闻      │  ReAct + MCP │
│  │Agent    │ │Agent    │ │Agent    │ │Agent    │  Tools       │
│  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘               │
│       └──────────┴───────────┴───────────┘                       │
│                         │ fan-in                                 │
│                         ▼                                        │
│                  ┌─────────────┐                                 │
│                  │ Summary      │ → final_report.md             │
│                  │ Agent        │                                 │
│                  └─────────────┘                                 │
│  ExecutionLogger → logs/<id>/agents | llm_interactions | reports │
└───────────────────────────┬─────────────────────────────────────┘
                            │ stdio MCP
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│           a-share-mcp-is-just-i-need (MCP Server, FastMCP)       │
│  BaostockDataSource → K线 / 财报 / 宏观 / 指数 / 新闻爬取等工具    │
└─────────────────────────────────────────────────────────────────┘
```

**状态（AgentState）**：`messages`（对话拼接）、`data`（业务字段合并）、`metadata`（执行元信息）。

---

## 环境准备

- Python 3.10+（推荐；部分依赖需与本地一致）
- 已安装 **[uv](https://github.com/astral-sh/uv)**（用于按 `mcp_config.py` 启动 MCP 子进程）
- 可访问的 **OpenAI 兼容 API**（用于各 Agent 与总结）

### 依赖安装（仓库根目录）

```bash
pip install -r requirements.txt
```

根目录 `requirements.txt` 已包含 LangGraph、LangChain、MCP 适配器、Baostock、Transformers 等；**MCP 子项目**若单独用 `uv run`，请在该目录按项目约定安装依赖（通常含 `baostock`、`mcp`、`requests`、`beautifulsoup4` 等，以子项目为准）。

---

## 配置

### 1. Agent 侧环境变量

复制并编辑：

```bash
cp Financial-MCP-Agent/.env.example Financial-MCP-Agent/.env
```

| 变量 | 含义 |
|------|------|
| `OPENAI_COMPATIBLE_API_KEY` | API Key |
| `OPENAI_COMPATIBLE_BASE_URL` | 兼容 OpenAI 的 Base URL |
| `OPENAI_COMPATIBLE_MODEL` | 模型名 |
| `USE_LOCAL_MODEL` | 总结阶段：`api`（默认）或 `local`（本地 FinR1 等，需自行配置路径与算力） |

### 2. MCP 启动路径（必改）

编辑 `Financial-MCP-Agent/src/tools/mcp_config.py`，将 `--directory` 改为本机 **a-share-mcp-is-just-i-need** 的**绝对路径**（Windows 示例）：

```python
r"D:\path\to\02Finance\a-share-mcp-is-just-i-need"
```

确保系统 PATH 中可执行 `uv`。

---

## 启动方式

在 **`Financial-MCP-Agent`** 目录下以模块方式运行（`main.py` 会把本目录加入 `sys.path`，从而正确解析 `from src...` 导入）。

**方式 A：交互式（推荐本地调试）**

```bash
cd Financial-MCP-Agent
python -m src.main
```

**方式 B：非交互，单次查询**

```bash
cd Financial-MCP-Agent
python -m src.main --command "分析一下比亚迪(002594)的投资价值与风险"
```

成功后在控制台提示报告路径；执行日志在 `Financial-MCP-Agent/logs/<execution_id>/`，Markdown 报告在 `Financial-MCP-Agent/reports/`（与 `summary_agent` 中路径逻辑一致）。

**单独验证 MCP 工具加载（可选）**

```bash
cd Financial-MCP-Agent
python -m src.tools.mcp_client
```

---

## 「API」说明与示例（CLI 契约）

当前版本 **未提供独立 HTTP/gRPC 服务**；对外契约等价于 **命令行参数 + 环境变量**。

### 输入

- **主参数**：`--command` 为用户自然语言查询；省略则进入交互， stdin 输入一行查询。
- **解析规则**：从文本中提取公司名称、A 股代码（`sh.*` / `sz.*` 规则在 `main.py` 中处理）。

### 输出

- **终端**：进度提示与报告保存路径。
- **文件**：
  - `reports/report_<公司>_<代码>_<时间戳>.md`（或基于 query 的文件名）
  - `logs/<execution_id>/execution_info.json`、`agents/*_execution.json`、`llm_interactions/*.json`（及 `.txt` 摘要）

### 示例调用

```bash
python -m src.main --command "请帮我分析一下贵州茅台(600519)的基本面与估值"
```

```bash
python -m src.main --command "603871 这个股票最近表现怎么样"
```

---

## 评测方式

### 1. 工程可观测（内置）

每次运行生成唯一 **`execution_id`**，便于：

- 统计 **单次运行的 Agent 数量、LLM 交互次数、工具调用次数**（见 `logs/.../execution_info.json` 与 `EXECUTION_SUMMARY.md`）。
- 复盘 **单次 ReAct / 总结** 的输入输出（`llm_interactions/`）。

### 2. 业务与质量指标（建议在文档/实验中定义口径）

在离线或灰度环境中可自建评测集，例如：

| 维度 | 说明 |
|------|------|
| **工具调用成功率** | MCP 工具返回非错误、数据非空的调用占比 |
| **端到端时延** | 从提交 query 到 `final_report` 落盘耗时（P50/P95） |
| **关键字段回源核对** | 从报告中抽取数值字段，与同源工具二次查询比对（需额外脚本） |
| **人工评分** | 报告结构完整性、风险提示、与用户需求相关性（Likert 或 rubric） |

新闻与爬虫路径受网络与反爬影响较大，建议单独统计成功率与时延，不与 Baostock 结构化数据混为一谈。

### 3. 单元 / 集成测试

- Baostock 数据源：`a-share-mcp-is-just-i-need/test_baostock.py`（若存在）可用于验证本地环境与接口可用性。
- Agent 单测：各 `*_agent.py` 文件末尾的 `if __name__ == "__main__"` 块可作为冒烟入口。

---

## 仓库结构（概要）

```
02Finance/
├── README.md                 # 本说明
├── requirements.txt          # Python 依赖（根目录）
├── Financial-MCP-Agent/      # LangGraph Host + Agents
│   ├── src/
│   │   ├── main.py           # 入口
│   │   ├── agents/           # fundamental / technical / value / news / summary
│   │   ├── tools/            # mcp_client, mcp_config
│   │   └── utils/            # state, execution_logger, ...
│   ├── logs/                 # 运行日志（本地生成）
│   └── reports/              # Markdown 报告（本地生成）
└── a-share-mcp-is-just-i-need/
    ├── mcp_server.py         # FastMCP 入口
    └── src/                  # 数据源与工具注册
```

---

## 常见问题

1. **MCP 拉不到工具**：检查 `mcp_config.py` 路径、`uv` 是否可用、子项目依赖是否完整；查看 MCP 子进程日志。
2. **Baostock 登录失败**：检查网络与 Baostock 服务状态；新闻检索若遇验证码，属于爬虫侧限制，需降级或换源。
3. **Windows 路径**：务必使用合法绝对路径或正确转义的字符串配置 MCP `args` 中的 `--directory`。

---

## 许可证与致谢

使用 **Baostock** 等第三方数据时请遵守其用户协议；LLM 调用费用由所用 API 提供商计费策略决定。
