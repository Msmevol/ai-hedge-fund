# 基金选购子系统设计（国内场外公募基金）

日期：2026-08-18
状态：已确认（用户逐节审阅通过）

## 目标

在现有 ai-hedge-fund（Windows 本地版，DeepSeek 驱动，中文 TUI）中新增"基金选购"子系统：从内置主题池拉取国内场外公募基金，用 LLM 智能体逐只分析，输出"推荐买入 / 可观望 / 不建议"评分排序报告，指导用户买入基金。

不改变现有股票分析、回测、交易模拟功能。

## 架构与组件

```
hedge_fund/
├── data/fund_client.py        (新增) 天天基金数据客户端
├── fund/
│   ├── spec.py                (已有, 不动)
│   └── snapshot.py            (新增) FundSnapshot 构建
├── signals/
│   ├── llm_agent.py           (已有, 不动)
│   └── fund_analyst.py        (新增) 基金分析智能体
├── tui/
│   ├── app.py                 (修改) 基金模式入口 + 主题选择
│   ├── fund_screen.py         (新增) 基金分析报告屏
│   └── shared.py              (修改) 主题双语名称
└── run.py                     (不动)
```

职责边界：

- **FundClient**（`hedge_fund/data/fund_client.py`）：所有天天基金 HTTP 请求与解析。方法：
  - `list_funds(theme) -> list[FundInfo]`：按主题拉基金池（代码、名称、类型）
  - `fetch_snapshot(code) -> FundSnapshot`：构建单只基金快照
- **FundSnapshot**（`hedge_fund/fund/snapshot.py`）：纯数据类 + 停用构造逻辑，字段见下。
- **FundAnalyst**（`hedge_fund/signals/fund_analyst.py`）：LLM 智能体，复用现有 DeepSeek 模型加载配置（api_models.json / hedge_fund.llm 模块），输出 `AgentOutput`（signal: bullish/bearish/neutral + confidence 0-100 + reasoning 中文）。提示词为基金视角，区别于股票大师提示词。
- **fund_screen.py**：报告屏，只读展示，不参与逻辑。

现有股票分析师、回测引擎、SimBroker、资金管理逻辑一律不动，基金逻辑全部隔离。

## 数据流

1. TUI 主界面输入 `funds` 进入基金模式 → 主题选择（消费/医药/科技/新能源/红利/宽基指数/债券）
2. `FundClient.list_funds(theme)` 拉取基金池 → 预筛（规模 ≥ 5 亿元 且 成立 ≥ 3 年）→ 取前 10 只（超出的按现有排序截断，实现时确定排序字段）
3. 逐只 `fetch_snapshot(code)` 构建 FundSnapshot
4. 并行调用 FundAnalyst 分析（信号 + 置信度 + 中文理由）
5. 汇总排序：按 信号强弱（bullish > neutral > bearish）再按置信度降序
6. 报告屏展示，每行可展开完整理由

## FundSnapshot 字段

- 基本信息：代码、名称、类型、成立日期、当前规模（亿元）
- 历史业绩：近 1/3/5 年年化收益率、最大回撤（基于日净值，取可得窗口）
- 费率：管理费、托管费、申购费
- 前十大重仓股（最新季报）：股票名、占净值比、所属行业
- 行业分布（最新季报，可得时）
- 基金经理：现任经理、任职起始、任职期年化（可得时）
- 任何字段缺失记 `None`，报告标注"数据缺失"

## 数据源（天天基金公开接口）

候选端点（实现时逐一实测，接口变动时仅改 FundClient 内部）：

| 数据 | 端点 |
|---|---|
| 主题基金池 | 天天基金主题页列表接口（`fund.eastmoney.com/data/rankhandler.aspx` 系列，主题分类筛选） |
| 净值/规模/费率 | `https://fund.eastmoney.com/pingzhongdata/{code}.js`（JS 变量含日净值、累计净值、规模、费率） |
| 季报重仓股 | `https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={code}` |
| 基本信息 | 同上或 pingzhongdata 内 `Data_BaseInfo` |

主题→东财分类参数映射做成配置表（放 FundClient 内），便于调整。

缓解措施：请求带浏览器 UA；超时 10s；失败重试 2 次（指数退避）；单基金失败跳过并标注；字段缺失记 None。

## 错误处理

- 网络：超时 10s、重试 2 次退避；主题池拉取失败 → 显示错误并返回主界面
- 单基金数据拉取失败 → 跳过，报告该行标"数据缺失/失败"
- 单基金 LLM 分析失败 → 标"分析失败"，继续下一只
- 基金数为 0 → 提示"该主题下无符合条件的基金"

## TUI 交互

- 主界面输入 `funds` 进入基金模式；帮助中提示
- 主题选择弹窗（中英双语：消费/医药/科技/新能源/红利/宽基指数/债券）
- 运行中状态页："拉取基金池… / 分析第 3/10 只…"
- 报告屏表格：排名 | 代码 | 名称 | 信号徽章 | 置信度 | 一句话结论；回车展开详情（历史业绩、回撤、重仓股、经理任期、分析理由全文）
- 完成后返回主界面，两种模式互不干扰

## 测试

- 单元测试（不联网）：
  - `FundSnapshot` 字段解析与缺失容忍（固定 fixture JSON）
  - 主题→池列表解析、预筛逻辑、排序逻辑
  - FundAnalyst 输出解析（mock LLM 返回）
- 集成测试打 `@pytest.mark.live`，默认不跑
- 不新增第三方依赖（请求库复用项目既有，实现时确认 pyproject）

## 范围外（YAGNI）

真实下单、场外申赎费用计算、回测引擎适配基金、实时估值、定投计划、历史报告持久化、海外基金、主题自定义关键词。