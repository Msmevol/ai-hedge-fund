"""Run the AI hedge fund.

Usage::

    aihf
        No arguments: the interactive app — a Textual TUI (the same app as
        `aihf` with no arguments). Build a fund — pick stocks, strategies, rebalance
        cadence — or backtest a saved fund and watch its equity curve draw
        against its benchmark.

    aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT
        With a mandate: run one cycle non-interactively. The full CycleRecord
        prints to stdout as JSON (pipe it anywhere); a short human summary
        goes to stderr. Add --out record.json to also write it to a file.

    aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT --backtest
        Backtest the mandate: run_cycle looped over history at the mandate's
        rebalance cadence; the full result JSON prints to stdout.

A mandate is the desk — strategies, staff, risk, capital, cadence — and never
names tickers; --tickers says what to point it at for this run.

Both paths run the same engine underneath. The interactive app is a thin
client: it only *composes a FundSpec* — the same machine-facing YAML this
CLI reads. Humans click, machines write, the engine reads one thing.
"""

from __future__ import annotations

import argparse
import os
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from rich.console import Console

from hedge_fund.backtesting import backtest_fund
from hedge_fund.brokers import SimBroker
from hedge_fund.data import CachedDataClient, FDClient
from hedge_fund.fund import Fund, load_spec, normalize_universe
from hedge_fund.paths import ensure_mandates_dir
from hedge_fund.pipeline import run_cycle
from hedge_fund.tui.keys import apply_credentials
from hedge_fund.tui.shared import _BACKTEST_WEEKS, _strategy_title, CADENCE_LABELS


def main() -> None:
    apply_credentials()
    ensure_mandates_dir()
    parser = argparse.ArgumentParser(
        prog="aihf",
        description="运行 AI 对冲基金。无参数：启动交互式应用。"
        "带基金定义 YAML：运行一个周期并输出结果。",
    )
    parser.add_argument("mandate", nargs="?",
                        help="基金定义 YAML 路径，例如 "
                        "~/.hedge-fund/mandates/example.yaml "
                        "（省略则启动交互式应用）")
    parser.add_argument(
        "--tickers",
        help="本次运行的交易标的，逗号或空格分隔，例如 "
        "AAPL,MSFT,NVDA —— 带基金定义时必填（基金本身不带自选列表）",
    )
    parser.add_argument(
        "--date",
        default=_date.today().isoformat(),
        help="数据截止日期 YYYY-MM-DD（默认：今天）；模型只能看到该日期前已归档的数据",
    )
    parser.add_argument(
        "--backtest", action="store_true",
        help="改为回测而非运行单周期：从 --start 到 --date 每个再平衡日运行一个周期，"
        "完整结果 JSON 输出到 stdout",
    )
    parser.add_argument(
        "--start",
        help=f"回测开始日期 YYYY-MM-DD（默认：--date 前 {_BACKTEST_WEEKS} 周）",
    )
    parser.add_argument(
        "--model",
        help="投资智能体的推理模型，例如 claude-opus-5 "
        "（默认：HEDGE_FUND_LLM_MODEL 环境变量，否则内置默认）；量化模型忽略此项",
    )
    parser.add_argument("--out", help="同时将结果 JSON 写入此文件")
    args = parser.parse_args()

    if args.model:
        os.environ["HEDGE_FUND_LLM_MODEL"] = args.model

    if args.mandate is None:
        # The interactive experience is the Textual app. Import it lazily so
        # the non-interactive path never pays to load Textual.
        from hedge_fund.tui.app import HedgeFundApp

        HedgeFundApp().run()
        return

    if not args.tickers:
        parser.error("--tickers 是必需的，例如 --tickers AAPL,MSFT")
    universe = normalize_universe(args.tickers.replace(",", " ").split())

    console = Console(stderr=True)  # status + summary on stderr; stdout stays pure JSON
    spec = load_spec(args.mandate)
    fund = Fund(spec)

    if args.backtest:
        start = args.start or (
            _date.fromisoformat(args.date) - timedelta(weeks=_BACKTEST_WEEKS)
        ).isoformat()
        with FDClient() as raw:
            fd = CachedDataClient(raw)
            with console.status(
                f"[cyan]{spec.name}：回测 {start} → {args.date} "
                f"（{CADENCE_LABELS.get(spec.rebalance, spec.rebalance)}再平衡"
                f"，基准 {spec.benchmark}）"
                f"股票池 {', '.join(universe)}…",
                spinner="dots",
            ):
                result = backtest_fund(fund, start, args.date, fd, universe)
        print(result.model_dump_json(indent=2))
        if args.out:
            Path(args.out).write_text(result.model_dump_json(indent=2))
        m = result.metrics
        console.print(
            f"[bold]{spec.name}[/] {result.start} → {result.end}  ·  "
            f"{m.n_cycles} 个周期  ·  回报 {m.total_return_pct:+.1%} "
            f"对比 {spec.benchmark} {m.benchmark_return_pct:+.1%}  ·  "
            f"夏普 {m.sharpe_ratio:.2f}  ·  最大回撤 {m.max_drawdown_pct:.1%}"
        )
        return

    broker = SimBroker(cash=spec.capital)

    with FDClient() as raw:
            fd = CachedDataClient(raw)
            n_models = sum(len(staff) for _, staff in fund.strategies)
            with console.status(
                f"[cyan]{spec.name}：按 {args.date} 运行一个周期——"
                f"{len(universe)} 只股票 × {n_models} 个模型，"
                f"覆盖 {len(fund.strategies)} 个策略…",
                spinner="dots",
            ):
                record = run_cycle(fund, args.date, broker, fd, universe)

    print(record.model_dump_json(indent=2))
    if args.out:
        Path(args.out).write_text(record.model_dump_json(indent=2))

    for sr in record.strategies:
        abstained = sum(1 for s in sr.signals if s.metadata.get("abstained") is True)
        specs = {s.name: s for s in record.spec.strategies}
        title = (_strategy_title(specs[sr.name])
                 if sr.name in specs else sr.name)
        console.print(
            f"[dim]  {title}（资金 {sr.slice:.0%}）："
            f"{len(sr.signals)} 个信号（{abstained} 个弃权）[/]"
        )
    n_signals = sum(len(sr.signals) for sr in record.strategies)
    console.print(
        f"[bold]{spec.name}[/] @ {record.as_of}  ·  "
        f"{len(record.strategies)} 个策略  ·  {n_signals} 个信号  ·  "
        f"{len(record.clamps)} 条风控限制  ·  "
        f"{len(record.orders)} 个订单  ·  NAV ${record.nav:,.2f}"
    )
    if record.skipped:
        console.print(f"[dim]跳过：{', '.join(s.ticker for s in record.skipped)}[/]")


if __name__ == "__main__":
    main()
