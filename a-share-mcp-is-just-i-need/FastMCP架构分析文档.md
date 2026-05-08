# A-Share MCP Server — FastMCP 架构分析文档

> 文档生成时间：2026-03-26  
> 项目路径：`a-share-mcp-is-just-i-need/`

---

## 目录

1. [整体框架脉络](#一整体框架脉络)
2. [FastMCP 核心工具与思想](#二fastmcp-核心工具与思想)
3. [面试高频问题汇总](#三面试高频问题汇总)

---

## 一、整体框架脉络

### 1.1 项目目录结构

```
a-share-mcp-is-just-i-need/
├── mcp_server.py                  # 入口：FastMCP App 初始化 & 工具注册
└── src/
    ├── data_source_interface.py   # 抽象接口层（ABC）
    ├── baostock_data_source.py    # Baostock 具体实现（依赖注入目标）
    ├── utils.py                   # 登录上下文 & 通用数据获取函数
    ├── formatting/
    │   └── markdown_formatter.py  # DataFrame → Markdown 格式化工具
    └── tools/                     # 各业务域工具模块（按职责拆分）
        ├── base.py                # 工具层公共辅助函数
        ├── stock_market.py        # 股票行情工具
        ├── financial_reports.py   # 财务报表工具
        ├── indices.py             # 指数工具
        ├── market_overview.py     # 市场概况工具
        ├── macroeconomic.py       # 宏观经济工具
        ├── date_utils.py          # 日期/交易日工具
        ├── analysis.py            # 综合分析报告工具
        └── news_crawler.py        # 新闻爬虫工具
```

---

### 1.2 分层架构图（数据流向）

```
┌────────────────────────────────────────────────────┐
│          MCP Host（如 Claude Desktop）              │
│         通过 stdio transport 调用工具               │
└───────────────────┬────────────────────────────────┘
                    │  MCP 协议（JSON-RPC over stdio）
┌───────────────────▼────────────────────────────────┐
│               mcp_server.py                        │
│  • 创建 FastMCP app 实例                            │
│  • 依赖注入：实例化 BaostockDataSource              │
│  • 调用各模块 register_xxx_tools(app, ds)           │
│  • app.run(transport='stdio') 启动服务              │
└──────┬────────────────────────────────┬─────────────┘
       │ @app.tool() 注册               │ 持有引用
┌──────▼────────────────────┐  ┌────────▼──────────────────────┐
│     src/tools/*.py        │  │  src/data_source_interface.py  │
│  register_xxx_tools()     │  │  FinancialDataSource (ABC)      │
│  闭包捕获 data_source     │  │  定义所有数据方法抽象签名       │
└──────┬────────────────────┘  └────────┬──────────────────────┘
       │ 调用 base.py 辅助函数            │ 继承实现
┌──────▼────────────────────┐  ┌────────▼──────────────────────┐
│     src/tools/base.py     │  │  src/baostock_data_source.py   │
│  safe_data_source_call()  │  │  BaostockDataSource             │
│  call_financial_data_tool │  │  委托 utils.py 中的通用函数    │
│  call_macro_data_tool()   │  │  执行实际 Baostock API 查询    │
└──────┬────────────────────┘  └────────┬──────────────────────┘
       └──────────────┬─────────────────┘
                      │ 依赖
┌─────────────────────▼──────────────────────────────┐
│               src/utils.py                         │
│  baostock_login_context()  上下文管理器             │
│  fetch_financial_data()    通用季度财务数据获取     │
│  fetch_index_constituent_data()  指数成分股获取     │
│  fetch_macro_data()        通用宏观数据获取         │
│  fetch_generic_data()      最通用数据获取函数       │
│  format_fields()           字段列表格式化           │
└─────────────────────┬──────────────────────────────┘
                      │ 返回 DataFrame
┌─────────────────────▼──────────────────────────────┐
│    src/formatting/markdown_formatter.py             │
│    format_df_to_markdown() 带截断保护的 Markdown    │
└────────────────────────────────────────────────────┘
```

---

### 1.3 启动流程（mcp_server.py）

**代码位置：**[`mcp_server.py`](mcp_server.py) — 第 1-67 行

```
第1步  setup_logging()          配置全局日志（INFO 级别）
第2步  BaostockDataSource()     实例化数据源（依赖注入准备）
第3步  FastMCP(...)             创建 MCP 应用实例
第4步  register_xxx_tools() ×8  向 app 注册全部工具（8个业务模块）
第5步  app.run('stdio')         阻塞式启动，通过 stdio 与 MCP Host 通信
```

---

### 1.4 工具注册机制（以 stock_market.py 为例）

**代码位置：**[`src/tools/stock_market.py`](src/tools/stock_market.py) — 第 48-200 行

每个业务模块暴露一个 `register_xxx_tools(app, active_data_source)` 函数，内部用 `@app.tool()` 装饰器注册工具函数。关键点是通过 **Python 闭包** 将 `active_data_source` 捕获进每个工具函数，实现依赖注入：

```python
def register_stock_market_tools(app: FastMCP, active_data_source: FinancialDataSource):

    @app.tool()                          # FastMCP 工具注册装饰器
    def get_historical_k_data(
        code: str,
        start_date: str,
        end_date: str,
        frequency: str = "d",
        adjust_flag: str = "3",
        fields: Optional[List[str]] = None,
    ) -> str:
        """ 函数 docstring = MCP 工具的描述（AI 依此决策是否调用） """
        # 闭包捕获外层 active_data_source
        return safe_data_fetch(
            "get_historical_k_data",
            active_data_source.get_historical_k_data,
            ...
        )
```

---

### 1.5 异常处理分层

**代码位置：**[`src/data_source_interface.py`](src/data_source_interface.py) — 第 1-18 行 | [`src/tools/base.py`](src/tools/base.py) — 第 1-110 行

项目定义了三级自定义异常，形成明确的语义层次：

```
Exception
└── DataSourceError          数据源通用错误（基类）
    ├── LoginError           Baostock 登录失败
    └── NoDataFoundError     查询结果为空
```

每个工具层的错误处理策略（`safe_data_source_call` 为统一入口）：

| 异常类型 | 日志级别 | 返回给 LLM 的内容 |
|---------|---------|------------------|
| `NoDataFoundError` | WARNING | 友好的无数据提示 |
| `LoginError` | ERROR | 连接失败提示 |
| `DataSourceError` | ERROR | 数据获取失败提示 |
| `ValueError` | WARNING | 参数错误提示 |
| `Exception`（兜底） | EXCEPTION | 未知错误提示 |

所有工具函数返回类型均为 `str`（Markdown 字符串），MCP 协议不感知内部异常，错误信息直接作为工具返回值传给 LLM。

---

### 1.6 数据源抽象层（依赖注入 + 策略模式）

**代码位置：**[`src/data_source_interface.py`](src/data_source_interface.py) | [`src/baostock_data_source.py`](src/baostock_data_source.py)

`FinancialDataSource` 是一个 Python ABC（抽象基类），定义了所有数据获取方法的签名，而不绑定具体实现。`BaostockDataSource` 是当前唯一实现，日后可无缝替换为 AKShare、Tushare 等其他数据源：

```
FinancialDataSource (ABC)        接口契约
└── BaostockDataSource           当前实现（使用 baostock 库）
    未来可扩展：
    ├── AKShareDataSource
    └── TushareDataSource
```

`mcp_server.py` 中一行代码即可切换数据源：

```python
# mcp_server.py 第30行
active_data_source: FinancialDataSource = BaostockDataSource()  # 只改这一行
```

---

### 1.7 Baostock 登录上下文管理器

**代码位置：**[`src/utils.py`](src/utils.py) — 第 24-72 行

`baostock_login_context()` 是一个 `@contextmanager`，解决了以下三个问题：

| 问题 | 解决方案 |
|------|----------|
| Baostock 每次查询前需登录、查询后需登出 | `with baostock_login_context():` 自动管理生命周期 |
| baostock 登录/登出会向 stdout 打印噪音信息 | `os.dup2` 临时将 stdout 重定向到 `/dev/null` |
| 登录失败需要语义化异常 | 抛出自定义 `LoginError` 而非通用异常 |

```python
# src/utils.py 第24-72行
@contextmanager
def baostock_login_context():
    # 1. 抑制 stdout（os.dup2 重定向到 /dev/null）
    # 2. bs.login() → 失败则抛 LoginError
    # 3. yield  ← 实际 API 调用在这里执行
    # 4. finally: bs.logout()（无论成功失败都保证登出）
```

---

### 1.8 全局工具清单

| 模块文件 | 注册函数 | 代表工具 |
|----------|----------|----------|
| [`stock_market.py`](src/tools/stock_market.py) | `register_stock_market_tools` | `get_historical_k_data`, `get_stock_basic_info`, `get_dividend_data`, `get_adjust_factor_data` |
| [`financial_reports.py`](src/tools/financial_reports.py) | `register_financial_report_tools` | `get_profit_data`, `get_balance_data`, `get_dupont_data`, `get_cash_flow_data`, `get_growth_data` |
| [`indices.py`](src/tools/indices.py) | `register_index_tools` | `get_hs300_stocks`, `get_sz50_stocks`, `get_zz500_stocks` |
| [`market_overview.py`](src/tools/market_overview.py) | `register_market_overview_tools` | `get_all_stock`, `get_stock_industry` |
| [`macroeconomic.py`](src/tools/macroeconomic.py) | `register_macroeconomic_tools` | `get_deposit_rate_data`, `get_money_supply_data_month`, `get_required_reserve_ratio_data` |
| [`date_utils.py`](src/tools/date_utils.py) | `register_date_utils_tools` | `get_latest_trading_date`, `get_market_analysis_timeframe` |
| [`analysis.py`](src/tools/analysis.py) | `register_analysis_tools` | `get_stock_analysis`（综合分析报告，聚合多接口） |
| [`news_crawler.py`](src/tools/news_crawler.py) | `register_news_crawler_tools` | `crawl_news`（百度新闻爬虫 + Qwen LoRA 情感/风险分析） |

---

## 二、FastMCP 核心工具与思想

### 2.1 FastMCP 是什么

FastMCP 是 `mcp` 官方 Python SDK 中的高级封装层（`mcp.server.fastmcp.FastMCP`），类比 FastAPI 之于 Starlette 的关系。它屏蔽了 MCP 协议底层的 JSON-RPC 报文处理、schema 生成、transport 管理等细节，让开发者只需关注业务函数本身。

**本项目使用方式：**

```python
# mcp_server.py 第5行
from mcp.server.fastmcp import FastMCP

# 第31-35行：创建实例
app = FastMCP()

# 各工具模块中：注册工具
@app.tool()
def my_tool(param: str) -> str:
    """工具描述"""
    ...

# 第61行：启动服务
app.run(transport='stdio')
```

---

### 2.2 @app.tool() 装饰器

**代码位置：**[`src/tools/stock_market.py`](src/tools/stock_market.py) — 第 49、79、101、134 行

`@app.tool()` 是 FastMCP 的核心注册机制，它做了以下三件事：

1. **自动解析函数签名** — 将参数名、类型注解、默认值转换为 MCP JSON Schema，LLM 据此知道如何传参
2. **自动生成工具描述** — 提取函数的 `docstring` 作为工具的自然语言描述，LLM 依此判断何时调用该工具
3. **自动注册到 MCP Server** — 将包装后的函数加入 FastMCP 内部的工具注册表，在 MCP Host 发起 `tools/list` 请求时返回

```python
@app.tool()                      # 无需额外配置，函数名即工具名
def get_historical_k_data(
    code: str,                   # 必填参数 → JSON Schema required
    start_date: str,             # 必填参数
    frequency: str = "d",        # 有默认值 → JSON Schema 标注 default
    fields: Optional[List[str]] = None,  # Optional → JSON Schema 允许 null
) -> str:                         # 返回类型固定为 str（Markdown 文本）
    """获取中国A股股票的历史K线（OHLCV）数据  ← 这段文字直接成为工具描述"""
    ...
```

---

### 2.3 stdio Transport

**代码位置：**[`mcp_server.py`](mcp_server.py) — 第 61 行

```python
app.run(transport='stdio')
```

MCP 协议支持多种 transport，本项目使用 `stdio`（标准输入/输出）：

| Transport | 适用场景 | 通信方式 |
|-----------|---------|----------|
| `stdio` | 本地集成（Claude Desktop、Cursor 等） | 进程间管道，JSON-RPC 报文 |
| `sse` | 远程 HTTP 服务 | Server-Sent Events |
| `streamable-http` | 远程 HTTP 服务（新版） | HTTP 流式响应 |

stdio 模式下，MCP Host 启动本服务器进程，通过 stdin 发送请求、从 stdout 读取响应，整个通信在进程内完成，无需网络端口。

---

### 2.4 依赖注入思想（通过闭包实现）

**代码位置：**[`mcp_server.py`](mcp_server.py) — 第 48-55 行 | [`src/tools/stock_market.py`](src/tools/stock_market.py) — 第 48 行

FastMCP 的 `@app.tool()` 注册的是普通 Python 函数，本项目利用 **Python 闭包** 将 `active_data_source` 注入到每个工具函数中，实现了与依赖注入框架等价的效果：

```
mcp_server.py
  active_data_source = BaostockDataSource()       ← 单例，全局只创建一次
  register_stock_market_tools(app, active_data_source)
                                 ↓
src/tools/stock_market.py
  def register_stock_market_tools(app, active_data_source):
      @app.tool()
      def get_historical_k_data(...):
          # active_data_source 被闭包捕获
          # 每次工具被调用时，都使用同一个 BaostockDataSource 实例
          active_data_source.get_historical_k_data(...)
```

这样做的好处：工具函数签名保持干净（只有业务参数），数据源实现对 MCP 协议层完全透明。

---

### 2.5 工具函数的 Docstring 即 Prompt 工程

**代码位置：**[`src/tools/stock_market.py`](src/tools/stock_market.py) — 第 67-88 行

FastMCP 直接将工具函数的 `docstring` 暴露给 LLM 作为工具描述。因此本项目的 docstring 写法本质上是在做 **Prompt 工程**，需要让 LLM 准确理解：
- 工具的功能是什么
- 每个参数的含义和合法值范围
- 返回值的格式

```python
@app.tool()
def get_historical_k_data(...) -> str:
    """
    获取中国A股股票的历史K线（OHLCV）数据

    参数:
        code: Baostock格式的股票代码（例如：'sh.600000', 'sz.000001'）
        frequency: 数据频率。有效选项：
                     'd': 日线  'w': 周线  'm': 月线
                     '5': 5分钟  '15': 15分钟  ...    ← 明确枚举合法值
        adjust_flag: '1':前复权  '2':后复权  '3':不复权  ← 枚举避免 LLM 乱传参
    返回:
        包含K线数据表的Markdown格式字符串，或错误消息
    """
```

---

### 2.6 通用辅助函数层（减少重复代码）

**代码位置：**[`src/tools/base.py`](src/tools/base.py) | [`src/utils.py`](src/utils.py)

项目在两个层次抽象了通用逻辑，避免每个工具重复写相同的异常处理和数据获取代码：

**工具层（`src/tools/base.py`）：**

| 函数 | 用途 |
|------|------|
| `safe_data_source_call()` | 最通用的数据获取 + 统一异常处理入口 |
| `call_financial_data_tool()` | 财务报表专用，含季度/年份参数校验 |
| `call_macro_data_tool()` | 宏观数据专用，处理 start_date/end_date |
| `call_index_constituent_tool()` | 指数成分股专用 |

**数据源层（`src/utils.py`）：**

| 函数 | 用途 |
|------|------|
| `fetch_financial_data()` | 所有季度财务数据的统一获取逻辑 |
| `fetch_index_constituent_data()` | 所有指数成分股的统一获取逻辑 |
| `fetch_macro_data()` | 所有宏观经济数据的统一获取逻辑 |
| `fetch_generic_data()` | 最通用，适配所有 Baostock API |

---

### 2.7 Markdown 格式化输出

**代码位置：**[`src/formatting/markdown_formatter.py`](src/formatting/markdown_formatter.py) — 第 1-75 行

所有工具函数返回 `str` 类型的 Markdown 文本，LLM 可直接渲染展示。`format_df_to_markdown()` 的设计要点：

- **行数截断保护**：`MAX_MARKDOWN_ROWS = 250`，防止大数据量撑爆上下文窗口
- **截断提示**：超过限制时在开头加 `Note: Data truncated (...)` 提示，让 LLM 知道数据不完整
- **统一出口**：所有工具的 DataFrame 结果都经过此函数，格式保持一致

```python
# src/formatting/markdown_formatter.py 第15行
MAX_MARKDOWN_ROWS = 250

def format_df_to_markdown(df: pd.DataFrame, max_rows: int = None) -> str:
    # 空 DataFrame 返回固定提示
    if df.empty:
        return "(No data available to display)"
    # 截断行数
    df_display = df.head(min(original_rows, max_rows))
    # 转为 Markdown 表格
    markdown_table = df_display.to_markdown(index=False)
    # 加截断说明
    if truncated:
        return f"Note: Data truncated ({notes}).\n\n{markdown_table}"
```

---

### 2.8 新闻爬虫 + LLM 分析的扩展思路

**代码位置：**[`src/baostock_data_source.py`](src/baostock_data_source.py) — `crawl_news` 方法

`crawl_news` 工具展示了 MCP 工具不限于数据 API，还可以集成：
- **Web 爬虫**（requests + BeautifulSoup 抓取百度新闻）
- **本地 LLM 推理**（Qwen + LoRA 微调模型，`_analyze_risk()` 和 `_analyze_sentiment()`）
- **Few-shot Prompt**（用3个示例引导模型输出 1-5 分的评分）

这体现了 MCP 工具的扩展性：任何 Python 逻辑都可以被包装为 MCP 工具。

---

## 三、面试高频问题汇总

### 3.1 基础概念类

**Q1：什么是 MCP？FastMCP 和 MCP 是什么关系？**

MCP（Model Context Protocol）是 Anthropic 提出的开放协议，定义了 LLM 应用与外部工具/数据源之间的标准通信接口，基于 JSON-RPC 2.0。FastMCP 是 `mcp` Python SDK 中的高级封装层，类似 FastAPI 之于 HTTP，屏蔽了协议底层细节（报文解析、schema 生成、transport 管理），让开发者只需用 `@app.tool()` 装饰器注册业务函数即可。

---

**Q2：项目中 FastMCP 的工具是如何注册的？LLM 是如何知道有哪些工具可以用的？**

通过 `@app.tool()` 装饰器注册。FastMCP 会自动解析函数签名（参数名、类型注解、默认值）生成 JSON Schema，并提取 `docstring` 作为工具描述。当 MCP Host（如 Claude Desktop）发起 `tools/list` 请求时，FastMCP 返回所有注册工具的 schema 和描述，LLM 依此决策在对话中调用哪个工具、传什么参数。

对应代码：[`src/tools/stock_market.py`](src/tools/stock_market.py) 第 49 行 `@app.tool()` 装饰器

---

**Q3：项目使用了 stdio transport，这是什么？为什么选它？**

stdio transport 是 MCP 协议的一种通信方式，MCP Host 通过启动服务器进程并与其 stdin/stdout 进行管道通信。选择原因：本地部署场景（Claude Desktop、Cursor）不需要额外的网络端口，进程间通信延迟低，配置简单，只需在 MCP Host 配置文件中指定启动命令即可。

对应代码：[`mcp_server.py`](mcp_server.py) 第 61 行 `app.run(transport='stdio')`

---

### 3.2 架构设计类

**Q4：项目中的数据源是如何组织的？如果要换一个数据源，需要改多少代码？**

数据源通过 ABC 抽象基类（`FinancialDataSource`）定义接口，`BaostockDataSource` 是当前实现。如果要换成 AKShare，只需：①新建 `AKShareDataSource` 继承 `FinancialDataSource` 并实现所有抽象方法；②修改 `mcp_server.py` 第 30 行的一行实例化代码。所有工具层代码（`src/tools/`）和 FastMCP 注册逻辑完全不需要改动，这是依赖注入 + 策略模式的典型收益。

对应代码：[`src/data_source_interface.py`](src/data_source_interface.py) | [`mcp_server.py`](mcp_server.py) 第 30 行

---

**Q5：项目中的工具是如何拆分模块的？为什么不把所有工具都写在 mcp_server.py 里？**

按业务域拆分为 8 个模块（stock_market、financial_reports、indices 等），每个模块暴露一个 `register_xxx_tools()` 函数。好处有三：①单一职责，每个文件只关注一个业务域，便于维护；②便于团队协作，不同成员可并行开发不同模块；③`mcp_server.py` 保持精简，只负责组装，符合「入口文件只做串联」的设计原则。

对应代码：[`mcp_server.py`](mcp_server.py) 第 48-55 行（8 次 register 调用）

---

**Q6：项目中的闭包是如何实现依赖注入的？为什么不用全局变量？**

`register_xxx_tools(app, active_data_source)` 函数接收 `active_data_source` 参数，内部的 `@app.tool()` 函数通过 Python 闭包自动捕获这个引用。相比全局变量：①闭包方式依赖关系显式，函数签名即文档；②便于测试，可以传入 mock 数据源；③多实例场景下不会互相干扰。

对应代码：[`src/tools/stock_market.py`](src/tools/stock_market.py) 第 48-60 行

---

**Q7：`baostock_login_context()` 为什么用上下文管理器而不是在每个方法里手动 login/logout？**

三个理由：①**保证配对**：`finally` 块确保无论 API 调用成功还是抛出异常，`logout()` 都会被执行，避免连接泄漏；②**消除重复**：所有数据获取函数只需 `with baostock_login_context():` 一行，不需要每处都写 login/logout 逻辑；③**副作用隔离**：`os.dup2` 对 stdout 的重定向也在上下文内完成，不会影响上下文外的正常输出。

对应代码：[`src/utils.py`](src/utils.py) 第 24-72 行

---

### 3.3 工程实践类

**Q8：工具函数的返回类型为什么都是 str？能不能返回 dict 或 DataFrame？**

MCP 协议规定工具返回内容（`content`）是文本或图片等媒体类型，不支持结构化对象直接传输。返回 `str`（Markdown 格式）有以下好处：①LLM 天然理解 Markdown 表格，可直接渲染给用户；②Markdown 是人类可读的，便于调试；③统一返回格式降低了工具层的复杂度。如果需要结构化数据，可以返回 JSON 字符串，但 Markdown 表格对 LLM 的理解效果通常更好。

---

**Q9：项目中异常处理的设计原则是什么？为什么不直接 raise 让调用方处理？**

MCP 工具函数是 LLM 调用的终点，没有「上层调用方」可以处理异常。如果工具抛出未捕获异常，MCP Host 会收到错误响应，LLM 无法从中获取有用信息。本项目的原则是：**所有异常在工具层被捕获并转换为有意义的字符串返回给 LLM**，让 LLM 能理解错误原因并向用户说明（如「未找到该股票数据」「请检查股票代码格式」）。数据源层（`utils.py`）则保留 raise，因为那一层的调用方（工具层）会统一处理。

对应代码：[`src/tools/base.py`](src/tools/base.py) 第 20-55 行（`safe_data_source_call`）

---

**Q10：`format_df_to_markdown` 中的 250 行截断是怎么考虑的？**

`MAX_MARKDOWN_ROWS = 250` 是权衡上下文窗口和数据完整性的结果。A 股历史 K 线数据一个股票一年约有 250 个交易日，所以 250 行大约对应一年的日线数据。超出时函数会在 Markdown 开头加 `Note: Data truncated` 提示，LLM 能感知数据被截断，从而建议用户缩小查询范围，而不是无声地丢失数据。

对应代码：[`src/formatting/markdown_formatter.py`](src/formatting/markdown_formatter.py) 第 15 行

---

**Q11：项目中如何防止 LLM 传入错误参数？**

两道防线：
1. **FastMCP 层**：`@app.tool()` 根据函数类型注解自动生成 JSON Schema，MCP Host 在调用前用 schema 验证参数格式。
2. **工具函数层**：对关键枚举参数（如 `frequency`、`adjust_flag`、`year_type`）进行显式白名单校验，不合法时立即返回清晰的错误提示字符串；`docstring` 中也明确列出所有合法值，从源头引导 LLM 传正确参数。

对应代码：[`src/tools/stock_market.py`](src/tools/stock_market.py) 第 94-104 行（频率/调整标志校验）

---

**Q12：这个项目和普通的 Python 脚本调用 API 有什么本质区别？**

普通脚本需要人写代码指定「调用哪个函数、传什么参数」，是硬编码的调用链。MCP 架构的核心区别是：**LLM 在运行时根据用户意图动态决定调用哪个工具、传什么参数**。用户只需说「帮我查一下贵州茅台最近6个月的K线」，LLM 自动选择 `get_historical_k_data`，自动推断股票代码 `sh.600519`、日期范围，然后调用工具、将结果整合成自然语言回复。这是从「代码驱动」到「意图驱动」的范式转变。

---

**Q13：如果要给这个 MCP Server 增加一个新工具，需要几步？**

最少 3 步：
1. 在对应的 `src/tools/xxx.py` 模块中，在 `register_xxx_tools()` 函数内用 `@app.tool()` 定义新函数，写好 docstring 和类型注解
2. 在 `FinancialDataSource` ABC 中添加对应的抽象方法（如果需要新数据接口）
3. 在 `BaostockDataSource` 中实现该抽象方法

无需修改 `mcp_server.py`（已有的 register 调用会自动包含新工具），无需修改任何 MCP 协议相关代码，体现了良好的开闭原则。

---

**Q14：news_crawler 模块中集成了本地 LLM（Qwen LoRA），这在 MCP 架构中意味着什么？**

这展示了 MCP 工具的核心特点：**工具内部逻辑对 LLM 完全透明**。对于调用方（Claude/GPT 等），`crawl_news` 只是一个接受 `query` 和 `top_k` 参数、返回字符串的工具。但工具内部实际上完成了：HTTP 爬虫 → HTML 解析 → 本地 Qwen 模型推理（风险评分 + 情感分析）→ 结果格式化。这种封装性使得 MCP 工具可以组合任意复杂的计算逻辑，而调用方无需关心实现细节。

对应代码：[`src/baostock_data_source.py`](src/baostock_data_source.py) — `crawl_news`、`_analyze_risk`、`_analyze_sentiment` 方法

---

## 附录：关键代码速查表

| 概念 | 文件 | 关键行 |
|------|------|--------|
| FastMCP App 创建 | [`mcp_server.py`](mcp_server.py) | 第 31 行 `app = FastMCP(...)` |
| 工具注册入口 | [`mcp_server.py`](mcp_server.py) | 第 48-55 行（8次 register 调用） |
| 服务启动 | [`mcp_server.py`](mcp_server.py) | 第 61 行 `app.run(transport='stdio')` |
| @app.tool() 装饰器示例 | [`src/tools/stock_market.py`](src/tools/stock_market.py) | 第 49 行 |
| 闭包捕获数据源 | [`src/tools/stock_market.py`](src/tools/stock_market.py) | 第 48-60 行 |
| 抽象基类定义 | [`src/data_source_interface.py`](src/data_source_interface.py) | 第 24-120 行 |
| 自定义异常层次 | [`src/data_source_interface.py`](src/data_source_interface.py) | 第 6-17 行 |
| 统一异常处理 | [`src/tools/base.py`](src/tools/base.py) | 第 15-55 行 `safe_data_source_call` |
| 登录上下文管理器 | [`src/utils.py`](src/utils.py) | 第 24-72 行 |
| Markdown 格式化 + 截断 | [`src/formatting/markdown_formatter.py`](src/formatting/markdown_formatter.py) | 第 15、28-65 行 |
| 新闻爬虫 + LLM 分析 | [`src/baostock_data_source.py`](src/baostock_data_source.py) | `crawl_news` 方法 |
