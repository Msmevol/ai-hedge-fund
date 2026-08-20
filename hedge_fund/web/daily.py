"""每日推荐引擎 — 每天自动分析一个主题，选出今日推荐基金。

结果存 ~/.hedge-fund/cache/daily/YYYY-MM-DD.json，前端读取后置顶横幅展示。

设计：
- 每天 08:00 自动运行（后台线程，与 FastAPI 同进程）
- 主题按日期轮转（确保覆盖所有 7 个主题）
- 分析结果存磁盘（JSON），前端 GET /api/daily 读取
- 手动触发 /api/daily/refresh 可强制刷新
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from hedge_fund.data.fund_client import THEMES, FundClient
from hedge_fund.signals.fund_analyst import FundAnalyst
from hedge_fund.signals.fund_quant import FundQuantModel
from hedge_fund.web.analysis import fuse, _snapshot_dict

logger = logging.getLogger(__name__)

_DAILY_DIR = Path.home() / ".hedge-fund" / "cache" / "daily"
_DAILY_HOUR = 8  # 每天几点运行（本地时间）
_THEMES = list(THEMES)


@dataclass
class DailyPick:
    date: str
    theme: str
    code: str
    name: str
    signal: str
    confidence: float
    label: str
    reasoning: str
    quant_total: float | None
    snapshot: dict | None

    def to_dict(self) -> dict:
        return asdict(self)


def _today_key() -> str:
    return date.today().isoformat()


def _daily_path(d: str | None = None) -> Path:
    return _DAILY_DIR / f"{d or _today_key()}.json"


def get_today_recommendation() -> DailyPick | None:
    """读取今日推荐（磁盘缓存）。没有则返回 None。"""
    p = _daily_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return DailyPick(**data)
    except Exception:
        return None


def _save_daily(pick: DailyPick) -> None:
    _DAILY_DIR.mkdir(parents=True, exist_ok=True)
    _daily_path(pick.date).write_text(
        json.dumps(pick.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _pick_theme_for_today() -> str:
    """按日期轮转选主题。"""
    today = date.today()
    idx = today.toordinal() % len(_THEMES)
    return _THEMES[idx]


def run_daily_analysis() -> DailyPick:
    """执行每日推荐分析（同步，在后台线程调用）。"""
    theme = _pick_theme_for_today()
    logger.info("每日推荐：开始分析主题 %s", theme)

    client = FundClient()
    try:
        pool = client.list_funds(theme, min_scale_billion=5.0,
                                 min_years=3, limit=10)
        if not pool:
            raise RuntimeError(f"主题 {theme} 无候选基金")

        analyst = FundAnalyst()
        quant_model = FundQuantModel()

        best_verdict = None
        best_snapshot = None
        best_quant_total = None

        for info in pool:
            try:
                snapshot = client.fetch_snapshot(info)
                qi = client.fetch_quant_input(info.code)
                quant = quant_model.score(snapshot, qi.fund_monthly,
                                          qi.bench_monthly)
                verdict = analyst.analyze(snapshot)
                fused = fuse(verdict, quant)
                if best_verdict is None or fused.score > best_verdict.score:
                    best_verdict = fused
                    best_snapshot = snapshot
                    best_quant_total = quant.total
            except Exception as exc:
                logger.warning("每日推荐分析 %s 失败: %s", info.code, exc)
                continue

        if best_verdict is None:
            raise RuntimeError(f"主题 {theme} 所有基金分析失败")

        pick = DailyPick(
            date=_today_key(),
            theme=theme,
            code=best_verdict.code,
            name=best_verdict.name,
            signal=best_verdict.signal,
            confidence=best_verdict.confidence,
            label=best_verdict.label,
            reasoning=best_verdict.reasoning,
            quant_total=best_quant_total,
            snapshot=_snapshot_dict(best_snapshot),
        )
        _save_daily(pick)
        logger.info("每日推荐：%s %s (%s, %.0f%%)",
                     pick.code, pick.name, pick.signal, pick.confidence)
        return pick
    finally:
        client.close()


def _daily_loop(stop_event: threading.Event) -> None:
    """后台线程主循环：等到每天 _DAILY_HOUR 点，运行分析。"""
    while not stop_event.is_set():
        now = datetime.now()
        target = now.replace(hour=_DAILY_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            from datetime import timedelta
            target += timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        stop_event.wait(timeout=min(wait_sec, 60))
        if stop_event.is_set():
            break
        now = datetime.now()
        if now.hour == _DAILY_HOUR and now.minute < 5:
            try:
                run_daily_analysis()
            except Exception as exc:
                logger.error("每日推荐失败: %s", exc)
            stop_event.wait(timeout=300)  # 避免同一分钟重复运行


_daily_stop = threading.Event()
_daily_thread: threading.Thread | None = None


def start_daily_scheduler() -> None:
    """启动每日推荐后台线程（FastAPI 启动时调用）。"""
    global _daily_thread
    if _daily_thread is not None and _daily_thread.is_alive():
        return
    _daily_stop.clear()
    _daily_thread = threading.Thread(target=_daily_loop, args=(_daily_stop,),
                                     daemon=True, name="daily-recommend")
    _daily_thread.start()
    logger.info("每日推荐调度器已启动（每天 %d:00）", _DAILY_HOUR)


def stop_daily_scheduler() -> None:
    _daily_stop.set()
