# 基金选购 Web 前端设计文档

日期：2026-08-19
状态：已批准（用户确认方案 B：独立 `hedge_fund/web/` 包）

## 背景与目标

现"基金选购"功能是 Textual TUI 内的一个屏幕（`hedge_fund/tui/fund_screen.py`）。用户希望先将其改造成 Web 前端：本地自己用，浏览器打开即用，无需构建链。本次只做基金选购，TUI 其余功能（运行基金、构建向导、回测）不动，TUI 的基金选购入口保留。

已确认的决策：
- 范围：仅基金选购功能 web 化
- 场景：本地使用（启动命令 + 自动开浏览器，http://localhost:8765）
- 技术栈：FastAPI + 原生 HTML/JS/CSS（无 node 构建链）
- 进度体验：逐只基金完成即实时上屏（SSE）
- 架构：独立 `hedge_fund/web/` 包，分析管线与 TUI 共享

## 架构

```
hedge_fund/web/                    （与 tui/ 平级的新包）
├── __init__.py
├── analysis.py      共享分析管线 run_fund_analysis()（从 fund_screen 抽出）
├── server.py        FastAPI：静态页 + API + SSE 流 + 任务管理
└── static/
    ├── index.html   单页应用
    ├── app.js       原生 JS（EventSource 消费 SSE）
    └── style.css    深色主题（沿用 TUI 色板 GREEN/CYAN/RED）
```

### analysis.py — 共享分析管线

从 `hedge_fund/tui/fund_screen.py` 提升为模块级共享逻辑：

- `run_fund_analysis(theme: str, on_event: Callable[[dict], None]) -> None`
  - 事件序列（dict 形式，`{"type": ...}`）：
    - `{"type": "pool", "pool": [FundInfo 序列化], "count": n}` — 候选池就绪
    - `{"type": "fund_done", "code", "name", "signal", "confidence", "reasoning", "snapshot": {...}}` — 每只完成即发
    - `{"type": "done", "total": n}` — 全部完成
  - 单只失败：发 `fund_done` 且 signal=neutral/confidence=0/reasoning="无法分析（异常类型）"，不中断（与 TUI 一致）
  - 池级失败：抛异常由调用方（server 或 TUI）处理
- `fuse(llm: FundVerdict, quant: FundQuantResult) -> FundVerdict` — 从 `fund_screen.py` 的 `_fuse` 平移，改公开名
- 并发：内部 `ThreadPoolExecutor(max_workers=3)` 逐只跑 `FundClient.fetch_snapshot` + `fetch_quant_input` + `FundQuantModel().score` + `FundAnalyst().analyze` + `fuse`（与 TUI `_one` 相同）

### server.py — FastAPI

- 依赖：仅新增 `fastapi`、`uvicorn`
- 端点：
  - `GET /` → 挂载 `static/` 的 index.html（`StaticFiles`，`html=True`）
  - `GET /api/themes` → `{"themes": ["宽基指数", ...]}`（来自 `fund_client.THEMES`）
  - `POST /api/analyze`，body `{"theme": "科技"}` → 校验主题（非法 400）；若已有运行中任务 → 409；否则创建任务（in-memory dict + 后台线程），返回 `{"task_id": "..."}`
  - `GET /api/stream/{task_id}` → `text/event-stream`：
    - 事件 `status`（开始/进行中计数）、`pool`、`fund_done`、`done`、`error`
    - 重连支持：任务完成后连接保留 5 分钟；新连接先补发已完成事件快照，再续推未完成
- 任务管理：模块级 `dict[task_id, TaskState]`；TaskState = 事件队列 + 已发快照 + 完成标志；分析线程 daemon
- 端口 8765；`main()`：`uvicorn.run(app, host="127.0.0.1", port=8765)`，启动前 `webbrowser.open("http://localhost:8765")`

### static/ — 原生前端

- 单页流程：主题卡片网格 → 点"开始分析" → 进度区逐只实时刷新（✓ 已完成/⏳ 分析中…）→ 全部完成后按推荐度排序（推荐买入/可观望/不建议，复用 `_sort_verdicts` 的排序字段）→ 点击基金展开详情
- 详情内容：量化综合分+成分、LLM 理由（【量化】/【LLM】双段）、快照（年收益/回撤/规模/费率/重仓 top10/经理）
- 深色主题，色板与 TUI 一致：GREEN `#2bd97c`、CYAN `#22d3ee`、RED `#f87171`、TEXT `#d9e6e0`、BRIGHT `#f2f7f4`、MUTED `#5f7268`
- SSE 用 `EventSource`；`fund_done` 事件逐条追加渲染；刷新页面后重连自动补发快照
- 无任何第三方前端库、无构建步骤

### TUI 联动

- `hedge_fund/tui/fund_screen.py` 的 `_pool`/`_one`/`_fuse` 删除，改为调用 `web.analysis` 的共享管线（线程 worker 里跑 `run_fund_analysis` + `app.call_from_thread` 上屏，行为不变）
- TUI 的 `FundPickerScreen` UI 结构与快捷键不变

## 数据流

```
POST /api/analyze {theme}
  → server 校验主题、创建 TaskState、起 daemon 线程
  → 线程跑 run_fund_analysis(theme, on_event=task.queue.put_nowait)
  → 每完成一只基金：事件入队
  → GET /api/stream/{task_id}：SSE 逐条推送（新连接先补快照）
  → 前端 EventSource 收到 fund_done → 进度列表更新 → done → 排序报告
```

## 错误处理

- 池拉取失败（`FundClientError` 等）→ `error` 事件，前端显示失败原因
- 单只基金失败 → `fund_done`（neutral/0，"无法分析（类型）"），不中断
- 主题非法 → 400；分析中再发起 → 409
- LLM 403/429/5xx 重试与弃权：沿用 `FundAnalyst._transient` 现有逻辑，web 不重复实现
- 服务关闭：分析线程 daemon，进程退出不阻塞

## 测试

- `hedge_fund/web/test_analysis.py`：假数据 fixture（不联网）——池事件→逐只 fund_done→done 事件序列；单只失败不中断；fuse 合成分支（同向/分歧/弃权）
- `hedge_fund/web/test_server.py`：FastAPI TestClient——`/api/themes`、`/api/analyze` 校验（非法 400、并发 409）、SSE 基本流（事件类型序列）、重连补发快照
- 回归：全量 `pytest -m "not live"` 保持 210 通过（新增测试计入增量）
- TUI 冒烟：`FundPickerScreen` import + 方法存在性（本环境 Textual 无法自动化渲染，沿用现有约束）

## 边界与不改动

- 不引入 node/构建链；前端零依赖
- 不改 `FundClient`/`FundAnalyst`/`FundQuantModel`/`_sort_verdicts`/`_fuse` 之外的现有分析逻辑
- 不做鉴权（本地单用户）；不做多任务队列（一次一个分析）
- 不迁移 TUI 其它屏幕
- 桌面快捷方式：新建 `AI对冲基金Web.lnk`（可选，用户决定）