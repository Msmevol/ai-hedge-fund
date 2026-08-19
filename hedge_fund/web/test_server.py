"""Fund picker web server tests — TestClient against the real app with the
pipeline monkeypatched to emit canned events (no network, no LLM)."""

import pytest
from fastapi.testclient import TestClient

from hedge_fund.web import server


@pytest.fixture
def client(monkeypatch):
    server._tasks.clear()
    server._active_task = None

    def fake_run(theme, on_event):
        on_event({"type": "pool", "theme": theme, "count": 2,
                  "funds": [{"code": "010013", "name": "科技基金A"},
                            {"code": "010236", "name": "科技基金B"}]})
        on_event({"type": "fund_done", "done": 1, "total": 2,
                  "code": "010013", "name": "科技基金A",
                  "signal": "bullish", "confidence": 80.0,
                  "reasoning": "理由", "quant": None,
                  "snapshot": None})
        on_event({"type": "fund_done", "done": 2, "total": 2,
                  "code": "010236", "name": "科技基金B",
                  "signal": "bearish", "confidence": 70.0,
                  "reasoning": "理由", "quant": None,
                  "snapshot": None})
        on_event({"type": "done", "total": 2,
                  "order": [{"code": "010013", "rank": 1,
                             "signal": "bullish", "label": "推荐买入"},
                            {"code": "010236", "rank": 2,
                             "signal": "bearish", "label": "不建议"}]})

    monkeypatch.setattr(server, "run_fund_analysis", fake_run)
    return TestClient(server.app)


def _sse_events(resp):
    """Parse `data: {...}` frames out of a streamed response."""
    events = []
    for line in resp.iter_lines():
        if line.startswith("data: "):
            import json
            events.append(json.loads(line[6:]))
    return events


def test_themes(client):
    r = client.get("/api/themes")
    assert r.status_code == 200
    themes = r.json()["themes"]
    assert "科技" in themes and "债券" in themes


def test_analyze_bad_theme(client):
    r = client.post("/api/analyze", json={"theme": "火星"})
    assert r.status_code == 400


def test_analyze_then_conflict(client, monkeypatch):
    import time

    def slow_run(theme, on_event):
        on_event({"type": "pool", "theme": theme, "count": 0, "funds": []})
        time.sleep(0.5)
        on_event({"type": "done", "total": 0, "order": []})

    monkeypatch.setattr(server, "run_fund_analysis", slow_run)
    r = client.post("/api/analyze", json={"theme": "科技"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    # second analysis while the first is still running → 409
    r2 = client.post("/api/analyze", json={"theme": "医药"})
    assert r2.status_code == 409
    # unknown task id
    assert client.get("/api/stream/nope").status_code == 404
    # the running task streams its events
    with client.stream("GET", f"/api/stream/{task_id}") as resp:
        assert resp.status_code == 200
        events = _sse_events(resp)
    kinds = [e["type"] for e in events]
    assert kinds == ["pool", "done", "done_ack"]


def test_replay_on_reconnect(client):
    r = client.post("/api/analyze", json={"theme": "科技"})
    task_id = r.json()["task_id"]
    # first client drains the stream
    with client.stream("GET", f"/api/stream/{task_id}") as resp:
        first = _sse_events(resp)
    # second client reconnects → replay of all events + done_ack
    with client.stream("GET", f"/api/stream/{task_id}") as resp:
        second = _sse_events(resp)
    assert [e["type"] for e in first] == [e["type"] for e in second]
    assert first[-1]["type"] == "done_ack"


def test_task_error_event(client, monkeypatch):
    def failing_run(theme, on_event):
        raise RuntimeError("池拉取失败")
    monkeypatch.setattr(server, "run_fund_analysis", failing_run)
    r = client.post("/api/analyze", json={"theme": "科技"})
    task_id = r.json()["task_id"]
    with client.stream("GET", f"/api/stream/{task_id}") as resp:
        events = _sse_events(resp)
    assert events[-1]["type"] == "error"
    assert "RuntimeError" in events[-1]["message"]


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "基金选购" in r.text