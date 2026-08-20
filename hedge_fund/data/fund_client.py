"""FundClient — 天天基金（东方财富）公开接口客户端，用于国内场外公募基金。

Unofficial, free, China-reachable endpoints (subject to change — every
endpoint lives behind `_get` and the JSON/HTML parsing sits in small pure
functions so a format change is a one-spot fix):

- 主题基金池:  rankhandler.aspx (fundranking page data feed)
- 净值/规模/经理:  pingzhongdata/{code}.js (page-embedded JS variables)
- 重仓股:       FundArchivesDatas.aspx?type=jjcc (F10 持仓明细页)
- 费率:         jjfl_{code}.html (F10 费率页)

Failure contract: infrastructure failures (connection, HTTP error) raise
FundClientError after retries — a broken download must never silently look
like "no data". Missing *fields* inside a successful response are tolerated
as None (the snapshot renders 数据缺失 and the analysis continues).
"""

from __future__ import annotations

import csv
import io
import json
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime

import requests

from hedge_fund.fund.snapshot import FundHolding, FundSnapshot

logger = logging.getLogger(__name__)

_RANK_URL = ("https://fund.eastmoney.com/data/rankhandler.aspx"
             "?op=ph&dt=kf&ft={ft}&rs=&gs=0&sc=gm&st=desc&pi={pi}&pn=50"
             "&dx=1&v={v}")
_PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
_JJCC_URL = ("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
             "?type=jjcc&code={code}&topline=10&year=&month=")
_JJFL_URL = "https://fundf10.eastmoney.com/jjfl_{code}.html"
# 沪深300 月K（市场基准，Alpha 回归用）— 腾讯行情公开接口（东财
# push2his 在部分网络环境下不可达，故用腾讯兜底）
_BENCH_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
              "?param=sh000300,month,,,240,qfq")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_RANK = re.compile(r'"([^"]*)"')
_TOTAL_ASSET = "总资产"
_DRAWDAYS = {365: "return_1y", 1096: "return_3y", 1827: "return_5y"}

_benchmark_cache: dict = {"series": None, "ts": 0.0}

_TYPES = {"gp": "股票型", "hh": "混合型", "zs": "指数型", "zq": "债券型"}

KEYWORDS = {
    "宽基指数": ("沪深300", "中证500", "中证1000", "中证800", "上证50", "上证180",
              "创业板", "科创50", "科创100", "国证2000", "深证100", "A500"),
    "消费": ("消费", "白酒", "食品", "饮料", "家电", "内需"),
    "医药": ("医药", "医疗", "健康", "生物", "创新药", "中药", "疫苗", "制药"),
    "科技": ("科技", "信息", "计算机", "半导体", "芯片", "电子", "软件", "人工智能",
           "通信", "云计算", "数字经济"),
    "新能源": ("新能源", "光伏", "电池", "储能", "碳中和", "锂电", "绿电", "智能汽车"),
    "红利": ("红利", "股息"),
    "债券": ("纯债", "中短债", "中短融", "利率债", "国债", "政金债", "信用债"),
}
_THEME_FT = {
    "宽基指数": ("zs",),
    "债券": ("zq",),
    "红利": ("gp", "hh", "zs"),
    "消费": ("gp", "hh"),
    "医药": ("gp", "hh"),
    "科技": ("gp", "hh"),
    "新能源": ("gp", "hh"),
}
THEMES = tuple(KEYWORDS)


class FundClientError(Exception):
    """Infrastructure failure — connection, HTTP error, or the pool feed
    refusing us. Never raised for a missing field inside a response."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


@dataclass(frozen=True)
class FundInfo:
    """One row from the ranked pool — enough to pre-filter and fetch."""

    code: str
    name: str
    fund_type: str  # 股票型/混合型/指数型/债券型
    inception: str  # YYYY-MM-DD
    scale_billion: float | None
    purchase_fee: float | None


@dataclass(frozen=True)
class QuantInput:
    """Time series the quant signals need: the fund's monthly returns and
    the 沪深300 monthly benchmark returns, newest last, aligned by index."""

    fund_monthly: tuple[float, ...]
    bench_monthly: tuple[float, ...]


class FundClient:
    def __init__(self, timeout: float = 15.0, retries: int = 2) -> None:
        self._timeout = timeout
        self._retries = retries
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Pool
    # ------------------------------------------------------------------

    def list_funds(self, theme: str, *, min_scale_billion: float = 5.0,
                   min_years: int = 3, limit: int = 10) -> list[FundInfo]:
        """Theme-ranked candidate pool: name-keyword filtered, pre-filtered
        on scale and age, sorted by scale descending, capped at `limit`."""
        if theme not in KEYWORDS:
            raise ValueError(f"未知主题: {theme!r}（可选: {', '.join(THEMES)}）")
        keywords = KEYWORDS[theme]
        today = _date.today()
        cutoff = today.replace(year=today.year - min_years)
        found: list[FundInfo] = []
        for ft in _THEME_FT[theme]:
            for page in range(1, 21):  # 50/page; pool needs only a few pages
                rows = self._pool_page(ft, page)
                if not rows:
                    break
                for row in rows:
                    name = row[1]
                    if not any(k in name for k in keywords):
                        continue
                    inception = row[16] or ""
                    try:
                        age_ok = _date.fromisoformat(inception) <= cutoff
                    except ValueError:
                        age_ok = False
                    scale = _to_float(row[24])
                    if not age_ok or scale is None or scale < min_scale_billion:
                        continue
                    found.append(FundInfo(
                        code=row[0],
                        name=name,
                        fund_type=_TYPES.get(ft, ft),
                        inception=inception,
                        scale_billion=scale,
                        purchase_fee=_to_float(row[19]),
                    ))
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        found.sort(key=lambda f: (f.scale_billion or 0.0), reverse=True)
        return found[:limit]

    def _pool_page(self, ft: str, page: int) -> list[list[str]]:
        """One ranked page as raw CSV rows (rank by scale)."""
        if ft not in _TYPES:
            raise ValueError(f"未知类型: {ft!r}")
        url = _RANK_URL.format(ft=ft, pi=page, v=time.time())
        text = self._get(url, referer="https://fund.eastmoney.com/fundranking.html")
        m = re.search(r"datas:\s*\[(.*?)\],\s*allRecords", text, re.S)
        if m is None:
            raise FundClientError(f"池子数据解析失败: {ft} 第{page}页", url=url)
        return [_rank_row(csv_data) for csv_data in _RANK.findall(m.group(1))]

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def fetch_snapshot(self, info: FundInfo) -> FundSnapshot:
        """Assemble the full snapshot: pingzhongdata net-worth/manager/scale,
        F10 quarterly holdings, F10 fee table. A source that fails entirely
        contributes None fields (except the name/code basics)."""
        pz = self._pingzhong(info.code)
        acw = pz.get("Data_ACWorthTrend") or []
        nav = pz.get("Data_netWorthTrend") or []
        pz1y, pz3y, pz5y, pzytd = _window_returns(acw)
        allocation = _parse_allocation(pz.get("Data_assetAllocation"))
        manager = _parse_manager(pz.get("Data_currentFundManager"))

        holdings: list[FundHolding] = []
        try:
            holdings = self._holdings(info.code)
        except FundClientError as exc:
            logger.warning("重仓获取失败 %s: %s", info.code, exc)

        mgmt_fee = custody_fee = None
        try:
            mgmt_fee, custody_fee = self._fees(info.code)
        except FundClientError as exc:
            logger.warning("费率页获取失败 %s: %s", info.code, exc)

        return FundSnapshot(
            code=info.code,
            name=info.name,
            fund_type=info.fund_type,
            inception=info.inception or None,
            scale_billion=allocation or info.scale_billion,
            purchase_fee=info.purchase_fee,  # pool feed carries it
            mgmt_fee=mgmt_fee,
            custody_fee=custody_fee,
            return_1y=pz1y, return_3y=pz3y, return_5y=pz5y, ytd=pzytd,
            max_drawdown=_max_drawdown(nav),
            holdings=tuple(holdings),
            manager=manager[0] if manager else None,
            manager_tenure=manager[1] if manager else None,
        )

    def _pingzhong(self, code: str) -> dict:
        text = self._get(_PINGZHONG_URL.format(code=code),
                         referer=f"https://fund.eastmoney.com/{code}.html")
        return _parse_pz_vars(text)

    def _holdings(self, code: str) -> list[FundHolding]:
        text = self._get(_JJCC_URL.format(code=code),
                         referer=f"https://fundf10.eastmoney.com/ccmx_{code}.html")
        return _parse_holdings(text)

    def _fees(self, code: str) -> tuple[float | None, float | None]:
        text = self._get(_JJFL_URL.format(code=code),
                         referer=f"https://fundf10.eastmoney.com/jjfl_{code}.html")
        return _parse_fees(text)

    # ------------------------------------------------------------------
    # Quant time series
    # ------------------------------------------------------------------

    def fetch_quant_input(self, code: str) -> QuantInput:
        """Monthly fund returns (from cumulative NAV) + 沪深300 monthly
        benchmark returns, both newest last. The benchmark is fetched once
        per process and cached."""
        pz = self._pingzhong(code)
        fund_monthly = _monthly_returns(pz.get("Data_ACWorthTrend") or [])
        bench_monthly = self._benchmark_monthly()
        n = min(len(fund_monthly), len(bench_monthly))
        return QuantInput(fund_monthly=fund_monthly[-n:],
                          bench_monthly=bench_monthly[-n:])

    def _benchmark_monthly(self) -> tuple[float, ...]:
        cached = _benchmark_cache.get("series")
        if cached is not None and time.time() - _benchmark_cache["ts"] < 86400:
            return cached
        text = self._get(_BENCH_URL,
                         referer="https://quote.eastmoney.com/")
        series = _parse_benchmark(text)
        _benchmark_cache["series"] = series
        _benchmark_cache["ts"] = time.time()
        return series

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _get(self, url: str, *, referer: str | None) -> str:
        headers = {"User-Agent": _UA}
        if referer:
            headers["Referer"] = referer
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = self._session.get(url, headers=headers,
                                         timeout=self._timeout)
                if resp.status_code != 200:
                    raise FundClientError(
                        f"HTTP {resp.status_code} 获取失败", url=url,
                        status_code=resp.status_code)
                return _decode(resp)
            except FundClientError:
                raise
            except Exception as exc:  # connection errors, timeouts
                last = exc
                time.sleep(2 ** attempt)
        raise FundClientError(f"网络错误: {last}", url=url)

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Single fund lookup
    # ------------------------------------------------------------------

    def get_fund_info(self, code: str) -> FundInfo:
        """Fetch fund metadata by code from pingzhongdata, returning a
        FundInfo suitable for fetch_snapshot / fetch_quant_input."""
        pz = self._pingzhong(code)
        name = pz.get("fS_name", "")
        fund_type = _TYPES.get(pz.get("fund_sourceRate", ""), "")
        inception = pz.get("fSRDate", "") or ""
        if isinstance(inception, (int, float)):
            inception = datetime.fromtimestamp(inception / 1000).strftime("%Y-%m-%d")
        acw = pz.get("Data_ACWorthTrend") or []
        scale = None
        if acw:
            scale = acw[-1][1] if acw[-1] else None
        return FundInfo(
            code=code,
            name=name,
            fund_type=fund_type or "股票型",
            inception=inception,
            scale_billion=scale,
            purchase_fee=None,
        )


# ---------------------------------------------------------------------------
# Pure parsers (unit-testable without the network)
# ---------------------------------------------------------------------------

def _decode(resp: requests.Response) -> str:
    """requests' charset sniffing is right most of the time; eastmoney feeds
    sometimes lie. A UTF-8-strict decode never loses Chinese."""
    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "latin-1"):
        return resp.content.decode("utf-8", errors="ignore")
    return resp.text


def _to_float(raw: str) -> float | None:
    m = re.search(r"[\d.]+", raw or "")
    return float(m.group(0)) if m else None


def _rank_row(csv_data: str) -> list[str]:
    # A comma-joined CSV row inside the feed's JS string. 25 fields; trailing
    # empties possible, so read leniently.
    fields = list(next(csv.reader(io.StringIO(csv_data))))
    return fields + [""] * max(0, 25 - len(fields))


def _parse_pz_vars(text: str) -> dict:
    """Extract the `var X = <json>;` assignments from pingzhongdata JS.

    The lookahead never consumes the following `var`/comment, so
    `var a=...;var b=...` on one line yields both assignments.
    """
    out: dict = {}
    pattern = r"var\s+(\w+)\s*=\s*(.*?)(?=;\s*(?:var\s+|/\*|$))"
    for m in re.finditer(pattern, text, re.S):
        name, body = m.group(1), m.group(2).strip()
        if not body:
            continue
        try:
            out[name] = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            pass
    return out


def _window_returns(acw: list) -> tuple[float | None, float | None,
                                        float | None, float | None]:
    """Point-to-point cumulative returns of the cumulative-NAV series:
    (1y, 3y, 5y, ytd) as multiples (+0.10 = +10%). Empty handling: a younger
    fund returns None for the windows it cannot span."""
    if not acw or len(acw) < 2:
        return None, None, None, None
    days = [datetime.fromtimestamp(ms / 1000).date() for ms, _ in acw]
    values = [float(v) for _, v in acw]
    last_date = days[-1]

    def gain(span_days: int) -> float | None:
        target = last_date.toordinal() - span_days
        start = None
        for i, d in enumerate(days):
            if d.toordinal() <= target:
                start = i
            else:
                break
        if start is None or start == len(days) - 1:
            return None
        return values[-1] / values[start] - 1.0

    ytd_start = None
    for i, d in enumerate(days):
        if d.year < last_date.year:
            ytd_start = i
        else:
            break
    ytd = (values[-1] / values[ytd_start] - 1.0) if ytd_start is not None else None
    return gain(365), gain(1096), gain(1827), ytd


def _max_drawdown(nav: list) -> float | None:
    """Depth of the deepest drawdown in the unit-NAV history (negative)."""
    if not nav:
        return None
    peak = -float("inf")
    mdd = 0.0
    for row in nav:
        # pingzhongdata ships the unit-NAV series as [{x,y,...}, ...] dicts;
        # tolerate the pair-list shape too.
        if isinstance(row, dict):
            v = float(row["y"])
        elif isinstance(row, (tuple, list)) and len(row) >= 2:
            v = float(row[1])
        else:
            continue
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd if mdd < 0 else None


def _parse_allocation(data) -> float | None:
    """Latest 总资产 (亿元) from Data_assetAllocation, else None."""
    if not isinstance(data, dict):
        return None
    for series in data.get("series") or []:
        if (series or {}).get("name") == _TOTAL_ASSET:
            values = series.get("data") or []
            return float(values[-1]) if values else None
    return None


def _parse_manager(data) -> tuple[str, str] | None:
    """(name, tenure-desc) of the current lead manager, else None."""
    if not isinstance(data, list) or not data:
        return None
    first = data[0] or {}
    name = first.get("name")
    if not name:
        return None
    return name, first.get("workTime") or ""


def _parse_holdings(text: str) -> list[FundHolding]:
    """F10 持仓明细 HTML table → top-10 holdings with 占净值比例.

    Column layout shifts between funds (dynamic quote cells come and go), so
    the NAV ratio is taken from the row's only percentage — every other
    percentage-shaped cell in a row is a quote injected client-side.
    """
    out: list[FundHolding] = []
    tables = re.findall(r"<table[^>]*>(.*?)</table>", text, re.S)
    table = tables[0] if tables else text  # newest quarter's table comes first
    for tr in re.findall(r"<tr>(.*?)</tr>", table, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 3:
            continue
        plain = [re.sub(r"<[^>]+>", "", td).strip() for td in tds]
        code, name = plain[1], plain[2]
        if not code or not name:
            continue
        m = re.search(r">([\d.]+)%<", tr)
        out.append(FundHolding(
            code=code, name=name,
            percent=float(m.group(1)) if m else None))
    return out


def _parse_fees(text: str) -> tuple[float | None, float | None]:
    """(管理费率, 托管费率) 每年, from the F10 fee page."""
    mgmt = custody = None
    mgmt_m = re.search(r"管理费率</td><td[^>]*>([^<]*)%", text)
    if mgmt_m:
        mgmt = float(mgmt_m.group(1))
    custody_m = re.search(r"托管费率</td><td[^>]*>([^<]*)%", text)
    if custody_m:
        custody = float(custody_m.group(1))
    return mgmt, custody


def _monthly_returns(acw: list) -> tuple[float, ...]:
    """Month-end cumulative-NAV levels → monthly returns, newest last.
    A month without an observation repeats the previous month's value."""
    if not acw:
        return ()
    levels: dict[tuple[int, int], float] = {}
    for row in acw:
        ms = row[0] if isinstance(row, (tuple, list)) else row.get("x")
        v = row[1] if isinstance(row, (tuple, list)) else row.get("y")
        d = datetime.fromtimestamp(ms / 1000)
        levels[(d.year, d.month)] = float(v)
    months = sorted(levels)
    out = []
    for i in range(1, len(months)):
        prev, cur = months[i - 1], months[i]
        v0, v1 = levels[prev], levels[cur]
        if v0 > 0:
            out.append(v1 / v0 - 1.0)
    return tuple(out)


def _parse_benchmark(text: str) -> tuple[float, ...]:
    """沪深300 monthly closes (newest last) → monthly returns.

    Tencent format: {"data":{"sh000300":{"month":[[date,open,close,
    high,low,vol], ...]}}} — close at index 2."""
    try:
        payload = json.loads(text)
        rows = payload["data"]["sh000300"]["month"]
    except (ValueError, KeyError, TypeError):
        raise FundClientError("沪深300 行情解析失败")
    closes = [float(row[2]) for row in rows if len(row) >= 3]
    out = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            out.append(closes[i] / closes[i - 1] - 1.0)
    return tuple(out)