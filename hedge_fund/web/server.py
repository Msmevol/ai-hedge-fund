"""FastAPI server for the fund picker web front end.

One analysis task at a time (in-memory), streamed to the page over SSE.
A finished task stays replayable for the lifetime of the process, so a
browser refresh reconnects and gets the buffered event replay first —
finished funds are never lost to a refresh.

Endpoints:
    GET  /                      the single-page app (static/)
    GET  /api/themes            the theme list
    POST /api/analyze           start an analysis ({theme: ...})
    GET  /api/stream/{task_id}  Server-Sent Events of the task
    GET  /api/daily             today's recommendation
    POST /api/analyze-fund      analyze a single fund by code (SSE)
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from hedge_fund.data.fund_client import THEMES
from hedge_fund.web.analysis import run_fund_analysis, run_single_fund_analysis

_HOST = "127.0.0.1"
_PORT = 8765
_STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class TaskState:
    theme: str | None  # None for single-fund analysis
    events: queue.Queue = field(default_factory=queue.Queue)
    sent: list[dict] = field(default_factory=list)  # replay buffer
    done: bool = False
    failed: str | None = None  # error message once the task has failed


_tasks: dict[str, TaskState] = {}
_active_task: TaskState | None = None
_lock = threading.Lock()

app = FastAPI(title="基金选购")


@app.get("/api/themes")
def api_themes() -> dict:
    return {"themes": list(THEMES)}


@app.post("/api/analyze")
def api_analyze(body: dict) -> dict:
    global _active_task
    theme = (body or {}).get("theme")
    if not theme or theme not in THEMES:
        raise HTTPException(status_code=400, detail=f"未知主题: {theme!r}")
    with _lock:
        if _active_task is not None and not _active_task.done:
            raise HTTPException(status_code=409,
                                detail="已有分析在进行中，请等待完成")
        task = TaskState(theme=theme)
        task_id = uuid.uuid4().hex[:12]
        _tasks[task_id] = task
        _active_task = task
    threading.Thread(target=_run, args=(task_id, task), daemon=True).start()
    return {"task_id": task_id}


@app.post("/api/analyze-fund")
def api_analyze_fund(body: dict) -> dict:
    """Analyze a single fund by code. Returns SSE stream."""
    global _active_task
    code = (body or {}).get("code", "").strip()
    if not code or not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400,
                            detail=f"无效基金代码: {code!r}（需6位数字）")
    with _lock:
        if _active_task is not None and not _active_task.done:
            raise HTTPException(status_code=409,
                                detail="已有分析在进行中，请等待完成")
        task = TaskState(theme=None)
        task_id = uuid.uuid4().hex[:12]
        _tasks[task_id] = task
        _active_task = task
    threading.Thread(target=_run_single, args=(task_id, task, code),
                     daemon=True).start()
    return {"task_id": task_id}


@app.get("/api/daily")
def api_daily() -> dict:
    """Today's recommendation (from disk cache or live analysis)."""
    from hedge_fund.web.daily import get_today_recommendation
    pick = get_today_recommendation()
    if pick is None:
        return {"pick": None}
    return {"pick": pick.to_dict()}


@app.post("/api/daily/refresh")
def api_daily_refresh() -> dict:
    """Force-refresh today's recommendation (runs in background)."""
    from hedge_fund.web.daily import get_today_recommendation
    # If already cached, return it immediately
    existing = get_today_recommendation()
    if existing is not None:
        return {"pick": existing.to_dict(), "status": "cached"}
    # Otherwise kick off background analysis
    threading.Thread(target=_run_daily_bg, daemon=True).start()
    return {"pick": None, "status": "running"}


def _run_daily_bg() -> None:
    from hedge_fund.web.daily import run_daily_analysis
    try:
        run_daily_analysis()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("每日推荐失败: %s", exc)


@app.get("/api/stream/{task_id}")
def api_stream(task_id: str) -> StreamingResponse:
    task = _tasks.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return StreamingResponse(_sse(task), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _run(task_id: str, task: TaskState) -> None:
    """Background: run the shared pipeline, queue every event for SSE."""
    try:
        run_fund_analysis(task.theme, task.events.put_nowait)
    except Exception as exc:
        task.failed = f"{type(exc).__name__}: {exc}"
    finally:
        task.done = True


def _run_single(task_id: str, task: TaskState, code: str) -> None:
    """Background: analyze a single fund by code."""
    try:
        run_single_fund_analysis(code, task.events.put_nowait)
    except Exception as exc:
        task.failed = f"{type(exc).__name__}: {exc}"
    finally:
        task.done = True


def _sse(task: TaskState):
    """Yield SSE frames; replay `sent` first for late/reconnecting clients."""
    for event in task.sent:
        yield _frame(event)
    while True:
        try:
            event = task.events.get(timeout=1.0)
        except queue.Empty:
            if task.done:
                if task.failed is not None:
                    yield _frame({"type": "error", "message": task.failed})
                else:
                    yield _frame({"type": "done_ack"})
                return
            continue
        task.sent.append(event)
        yield _frame(event)
        if event.get("type") == "done":
            yield _frame({"type": "done_ack"})
            return


def _frame(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def main() -> None:
    import uvicorn
    from uvicorn import Config, Server

    from hedge_fund.tui.keys import apply_credentials

    apply_credentials()
    # Start daily recommendation scheduler
    from hedge_fund.web.daily import start_daily_scheduler
    start_daily_scheduler()
    webbrowser.open(f"http://localhost:{_PORT}")
    Server(Config(app, host=_HOST, port=_PORT, log_level="info")).run()


# Mounted last so /api/* routes win over the catch-all static mount. Must sit
# BEFORE the `if __name__` block: `python -m` runs main() first and blocks in
# Server.run(), which would leave this line unexecuted and the static app
# serving nothing but 404s.
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True),
          name="static")


if __name__ == "__main__":
    main()