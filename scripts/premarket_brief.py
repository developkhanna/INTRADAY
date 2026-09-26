"""Collect everything needed for a pre-market briefing, in one pass.

This script does no judging. It gathers facts — overnight gaps, pre-market
volume, volatility, distance to each hard exit, and the headlines behind the
moves — and writes them to JSON plus a readable Markdown table. The advisor
(see `.agents/skills/premarket-briefing/SKILL.md`) reads that pack and writes
the actual plan.

Usage:
    PYTHONPATH=. python3 scripts/premarket_brief.py
    PYTHONPATH=. python3 scripts/premarket_brief.py --symbols AAPL,META --no-news
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from intraday.config import (
    DATA_DIR,
    MARKET_TZ,
    AlpacaConfig,
    ensure_dirs,
    load_watchlist,
)
from intraday.positions import load_positions
from intraday.risk import load_risk

PREMARKET_OPEN_MIN = 4 * 60  # 04:00 ET, when the pre-market tape starts
SEEKING_ALPHA_FEED = "https://seekingalpha.com/api/sa/combined/{symbol}.xml"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) intraday-briefing/1.0"
NEWS_LOOKBACK_HOURS = 36
MARKET_PROXIES = ("SPY", "QQQ", "IWM")


@dataclass(frozen=True)
class Window:
    """The clock at the moment the brief is built."""

    now_utc: pd.Timestamp
    now_et: pd.Timestamp
    session_date: dt.date
    prior_session_date: dt.date | None
    minutes_to_open: float
    early_close_et: str | None


class Feed:
    """Thin Alpaca wrapper: free SIP history plus the 15-minute embargo dance."""

    def __init__(self, config: AlpacaConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "APCA-API-KEY-ID": config.key_id,
                "APCA-API-SECRET-KEY": config.secret_key,
            }
        )

    def get(self, path: str, params: dict | None = None, base: str | None = None) -> dict:
        url = f"{base or self.config.data_url}{path}"
        response = self.session.get(url, params=params or {}, timeout=60)
        response.raise_for_status()
        return response.json()

    def bars(
        self,
        symbols: list[str],
        timeframe: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        feed: str = "sip",
    ) -> dict[str, pd.DataFrame]:
        """Bars for many symbols, following pagination, indexed in ET."""
        out: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
        page_token = None
        while True:
            params = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
                "limit": 10000,
                "adjustment": "all",
                "feed": feed,
            }
            if page_token:
                params["page_token"] = page_token
            payload = self.get("/v2/stocks/bars", params)
            for symbol, rows in (payload.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(rows)
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        frames: dict[str, pd.DataFrame] = {}
        for symbol, rows in out.items():
            if not rows:
                continue
            frame = pd.DataFrame(rows).rename(
                columns={"t": "timestamp", "o": "open", "h": "high", "l": "low",
                         "c": "close", "v": "volume", "n": "trades", "vw": "vwap"}
            )
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(
                MARKET_TZ
            )
            frames[symbol] = frame.set_index("timestamp").sort_index()
        return frames

    def news(
        self, symbols: list[str], since: pd.Timestamp, limit: int = 50, max_pages: int = 20
    ) -> list[dict]:
        """Every article in the window, paginated.

        One page of 50 spread over a watchlist of forty names leaves most
        symbols with nothing, so follow the cursor to the end of the window.
        """
        articles: list[dict] = []
        page_token = None
        for _ in range(max_pages):
            params = {
                "symbols": ",".join(symbols),
                "start": since.isoformat().replace("+00:00", "Z"),
                "limit": limit,
                "sort": "desc",
            }
            if page_token:
                params["page_token"] = page_token
            payload = self.get("/v1beta1/news", params)
            articles.extend(payload.get("news") or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return articles

    def clock(self) -> dict:
        return self.get("/v2/clock", base=self.config.trading_url)

    def calendar(self, start: dt.date, end: dt.date) -> list[dict]:
        return self.get(
            "/v2/calendar",
            {"start": start.isoformat(), "end": end.isoformat()},
            base=self.config.trading_url,
        )


def build_window(feed: Feed) -> Window:
    now_utc = pd.Timestamp.now(tz="UTC")
    now_et = now_utc.tz_convert(MARKET_TZ)
    calendar = feed.calendar(
        (now_et - pd.Timedelta(days=10)).date(), (now_et + pd.Timedelta(days=3)).date()
    )
    days = [entry for entry in calendar if entry.get("date")]
    today = now_et.date().isoformat()
    session = next((d for d in days if d["date"] >= today), days[-1] if days else None)
    prior = [d for d in days if d["date"] < (session["date"] if session else today)]

    session_date = (
        dt.date.fromisoformat(session["date"]) if session else now_et.date()
    )
    prior_date = dt.date.fromisoformat(prior[-1]["date"]) if prior else None
    open_et = pd.Timestamp(
        f"{session_date.isoformat()} {session['open'] if session else '09:30'}",
        tz=MARKET_TZ,
    )
    close_time = session.get("close") if session else None
    return Window(
        now_utc=now_utc,
        now_et=now_et,
        session_date=session_date,
        prior_session_date=prior_date,
        minutes_to_open=(open_et - now_et).total_seconds() / 60.0,
        early_close_et=close_time if close_time and close_time < "16:00" else None,
    )


def seeking_alpha_headlines(symbol: str, since: pd.Timestamp) -> list[dict]:
    """Seeking Alpha's free per-symbol feed: headline, time, author.

    Personal, non-commercial use only, per their terms — we read headlines to
    explain a move, we do not republish the feed.
    """
    try:
        response = requests.get(
            SEEKING_ALPHA_FEED.format(symbol=symbol),
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError):
        return []

    items = []
    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        published = item.findtext("pubDate")
        if not title or not published:
            continue
        try:
            when = pd.Timestamp(published).tz_convert(MARKET_TZ)
        except (ValueError, TypeError):
            continue
        if when < since.tz_convert(MARKET_TZ):
            continue
        author = item.findtext("{https://seekingalpha.com/api/1.0}author_name") or ""
        items.append(
            {
                "symbol": symbol,
                "source": "Seeking Alpha",
                "headline": title,
                "published_et": when.isoformat(),
                "author": author.strip(),
                "url": (item.findtext("link") or "").strip(),
            }
        )
    return items[:8]


def daily_stats(daily: pd.DataFrame) -> dict:
    """Prior close, typical daily range and volume — the 'normal' to compare to."""
    if daily.empty:
        return {}
    recent = daily.tail(20)
    day_range_pct = ((recent["high"] - recent["low"]) / recent["close"]).mean()
    return {
        "prior_close": float(daily["close"].iloc[-1]),
        "prior_date": daily.index[-1].date().isoformat(),
        "prior_volume": float(daily["volume"].iloc[-1]),
        "avg_volume_20d": float(recent["volume"].mean()),
        "avg_day_range_pct": float(day_range_pct),
        "change_5d_pct": float(daily["close"].iloc[-1] / daily["close"].iloc[-6] - 1)
        if len(daily) > 6
        else None,
        "off_20d_high_pct": float(daily["close"].iloc[-1] / recent["high"].max() - 1),
    }


def minute_volatility(minutes: pd.DataFrame) -> float | None:
    """Average one-minute range as a fraction of price — the noise floor.

    This is what the working stop has to clear: a stop tighter than one normal
    minute of movement gets hit by nothing at all.
    """
    if minutes.empty:
        return None
    rth = minutes.between_time("09:30", "16:00")
    if rth.empty:
        return None
    return float(((rth["high"] - rth["low"]) / rth["close"]).mean())


def premarket_stats(minutes: pd.DataFrame, session_date: dt.date, prior_close: float) -> dict:
    """What has happened since 04:00 ET today."""
    if minutes.empty or not prior_close:
        return {}
    today = minutes[minutes.index.date == session_date]
    pre = today.between_time("04:00", "09:29")
    if pre.empty:
        return {}
    last = float(pre["close"].iloc[-1])
    return {
        "premarket_last": last,
        "premarket_high": float(pre["high"].max()),
        "premarket_low": float(pre["low"].min()),
        "premarket_volume": float(pre["volume"].sum()),
        "premarket_bars": int(len(pre)),
        "gap_pct": float(last / prior_close - 1),
        "premarket_range_pct": float((pre["high"].max() - pre["low"].min()) / prior_close),
        "as_of_et": pre.index[-1].isoformat(),
    }


def collect(symbols: list[str], feed: Feed, window: Window, with_news: bool) -> dict:
    end = window.now_utc - pd.Timedelta(minutes=feed.config.sip_delay_minutes)
    daily = feed.bars(symbols, "1Day", window.now_utc - pd.Timedelta(days=90), end)
    minutes = feed.bars(symbols, "1Min", window.now_utc - pd.Timedelta(days=5), end)

    news_by_symbol: dict[str, list[dict]] = {symbol: [] for symbol in symbols}
    if with_news:
        since = window.now_utc - pd.Timedelta(hours=NEWS_LOOKBACK_HOURS)
        try:
            for article in feed.news(symbols, since):
                for symbol in article.get("symbols", []):
                    if symbol in news_by_symbol:
                        news_by_symbol[symbol].append(
                            {
                                "symbol": symbol,
                                "source": article.get("source", "Alpaca"),
                                "headline": article.get("headline", ""),
                                "published_et": pd.Timestamp(article["created_at"])
                                .tz_convert(MARKET_TZ)
                                .isoformat(),
                                "author": article.get("author", ""),
                                "url": article.get("url", ""),
                            }
                        )
        except requests.RequestException as error:
            print(f"warning: Alpaca news unavailable ({error})", file=sys.stderr)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for items in pool.map(
                lambda s: seeking_alpha_headlines(s, since), symbols
            ):
                for item in items:
                    news_by_symbol[item["symbol"]].append(item)

    rows = {}
    for symbol in symbols:
        stats = daily_stats(daily.get(symbol, pd.DataFrame()))
        minute_frame = minutes.get(symbol, pd.DataFrame())
        pre = premarket_stats(
            minute_frame, window.session_date, stats.get("prior_close", 0.0)
        )
        rows[symbol] = {
            **stats,
            **pre,
            "minute_range_pct": minute_volatility(minute_frame),
            "news": sorted(
                news_by_symbol.get(symbol, []),
                key=lambda item: item["published_et"],
                reverse=True,
            )[:8],
        }
    return rows


def enrich_positions(rows: dict, positions, risk) -> list[dict]:
    """Each holding against the only line the user has committed to."""
    out = []
    for position in positions:
        data = rows.get(position.symbol, {})
        price = data.get("premarket_last") or data.get("prior_close")
        hard_stop = position.avg_price * (1 - risk.hard_stop_pct)
        entry = {
            "symbol": position.symbol,
            "quantity": position.quantity,
            "avg_price": position.avg_price,
            "reference_price": price,
            "hard_exit_price": round(hard_stop, 2),
            "cost_basis": round(position.cost_basis(), 2),
        }
        if price:
            entry.update(
                {
                    "unrealized_pct": round(price / position.avg_price - 1, 4),
                    "unrealized_dollars": round(
                        (price - position.avg_price) * position.quantity, 2
                    ),
                    "room_to_hard_exit_pct": round(price / hard_stop - 1, 4),
                    "gap_pct": data.get("gap_pct"),
                }
            )
        out.append(entry)
    return sorted(out, key=lambda e: e.get("room_to_hard_exit_pct", 99))


def candidate_levels(data: dict, risk) -> dict:
    """Mechanical entry maths, so the advisor argues about the idea, not arithmetic."""
    price = data.get("premarket_last") or data.get("prior_close")
    if not price:
        return {}
    minute_range = data.get("minute_range_pct") or 0.0
    stop_pct = risk.working_stop_pct(minute_range)
    shares = risk.shares_for(price)
    return {
        "reference_price": round(price, 2),
        "shares_for_position_size": shares,
        "working_stop_pct": round(stop_pct, 5),
        "working_stop_price": round(price * (1 - stop_pct), 2),
        "working_stop_dollars": round(price * stop_pct * shares, 2),
        "hard_exit_price": round(price * (1 - risk.hard_stop_pct), 2),
        "target_1r_price": round(price * (1 + stop_pct), 2),
        "target_2r_price": round(price * (1 + 2 * stop_pct), 2),
        "relative_volume_premarket": round(
            data["premarket_volume"] / data["avg_volume_20d"], 4
        )
        if data.get("premarket_volume") and data.get("avg_volume_20d")
        else None,
    }


def _pct(value: float | None) -> str:
    """A percentage, or an em dash when the number does not exist yet."""
    return f"{value * 100:+.2f}%" if value is not None else "—"


def _num(value: float | None, spec: str = ",.0f") -> str:
    return format(value, spec) if value is not None else "—"


def to_markdown(pack: dict) -> str:
    window, risk = pack["window"], pack["risk"]
    has_premarket = any(
        data.get("premarket_bars") for data in pack["symbols"].values()
    )
    lines = [
        f"# Pre-market data pack — {window['session_date']}",
        "",
        f"Built {window['now_et']} ET, {window['minutes_to_open']:.0f} minutes to the open."
        + (f" Early close at {window['early_close_et']} ET." if window["early_close_et"] else ""),
        "",
    ]
    if not has_premarket:
        lines += [
            "No pre-market tape yet: every gap, pre-market volume and relative-volume "
            "cell is empty, and prices shown are the prior close.",
            "",
        ]
    lines += [
        f"Equity ${risk['account_equity']:,.0f} · new trade ${risk['position_size']:,.0f} "
        f"· daily stop ${risk['daily_loss_limit_dollars']:,.0f} "
        f"· hard ceiling {risk['hard_stop_pct']:.0%} · max {risk['max_open_positions']} open",
        "",
        "## Market",
        "",
        "| Symbol | Prior close | Gap % | Pre-mkt vol |",
        "| --- | ---: | ---: | ---: |",
    ]
    for symbol in MARKET_PROXIES:
        data = pack["symbols"].get(symbol)
        if not data:
            continue
        lines.append(
            f"| {symbol} | {data.get('prior_close', float('nan')):.2f} | "
            f"{_pct(data.get('gap_pct'))} | "
            f"{_num(data.get('premarket_volume'))} |"
        )

    lines += [
        "",
        "## Holdings (closest to the hard exit first)",
        "",
        "| Symbol | Qty | Avg | Now | P/L % | Hard exit | Room | Gap % |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in pack["positions"]:
        lines.append(
            f"| {row['symbol']} | {row['quantity']:.4g} | {row['avg_price']:.2f} | "
            f"{_num(row.get('reference_price'), '.2f')} | "
            f"{_num((row.get('unrealized_pct') or 0) * 100, '+.1f')}% | "
            f"{row['hard_exit_price']:.2f} | "
            f"{_num((row.get('room_to_hard_exit_pct') or 0) * 100, '+.1f')}% | "
            f"{_pct(row.get('gap_pct'))} |"
        )

    lines += [
        "",
        "## Watchlist (biggest movers first)",
        "",
        "| Symbol | Prior close | Gap % | RelVol | 1-min range | Stop | 2R target | Shares |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for symbol, data in pack["candidates"]:
        levels = data["levels"]
        lines.append(
            f"| {symbol} | {data.get('prior_close', float('nan')):.2f} | "
            f"{_pct(data.get('gap_pct'))} | "
            f"{_num(levels.get('relative_volume_premarket'), '.3f')} | "
            f"{(data.get('minute_range_pct') or 0) * 100:.3f}% | "
            f"{levels.get('working_stop_price', 0):.2f} | "
            f"{levels.get('target_2r_price', 0):.2f} | "
            f"{levels.get('shares_for_position_size', 0)} |"
        )

    lines += ["", "## Headlines (last 36h)", ""]
    held_first = [row["symbol"] for row in pack["positions"]]
    news_symbols = held_first + [
        symbol for symbol, _ in pack["candidates"][:12] if symbol not in set(held_first)
    ]
    for symbol in news_symbols:
        data = pack["symbols"].get(symbol) or {}
        if not data.get("news"):
            continue
        lines.append(f"**{symbol}**")
        for item in data["news"][:4]:
            lines.append(
                f"- {item['published_et'][:16]} · {item['source']} — {item['headline']}"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="Comma-separated override of the watchlist")
    parser.add_argument("--no-news", action="store_true", help="Skip news fetching")
    parser.add_argument(
        "--out-dir", type=Path, default=DATA_DIR / "briefs", help="Where to write the pack"
    )
    args = parser.parse_args()

    ensure_dirs()
    risk = load_risk()
    positions = load_positions()
    watchlist = (
        tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
        if args.symbols
        else load_watchlist()
    )
    symbols = sorted(
        set(watchlist) | {p.symbol for p in positions} | set(MARKET_PROXIES)
    )

    feed = Feed(AlpacaConfig.from_env())
    window = build_window(feed)
    rows = collect(symbols, feed, window, with_news=not args.no_news)

    held = {p.symbol for p in positions}
    candidates = []
    for symbol in watchlist:
        data = rows.get(symbol)
        if not data or symbol in held:
            continue
        data = {**data, "levels": candidate_levels(data, risk)}
        rows[symbol] = data
        candidates.append((symbol, data))
    candidates.sort(key=lambda item: abs(item[1].get("gap_pct") or 0), reverse=True)

    pack = {
        "window": {
            "now_et": window.now_et.isoformat(timespec="minutes"),
            "session_date": window.session_date.isoformat(),
            "prior_session_date": window.prior_session_date.isoformat()
            if window.prior_session_date
            else None,
            "minutes_to_open": round(window.minutes_to_open, 1),
            "early_close_et": window.early_close_et,
            "data_embargo_minutes": feed.config.sip_delay_minutes,
        },
        "risk": {
            "account_equity": risk.account_equity,
            "position_size": risk.position_size,
            "hard_stop_pct": risk.hard_stop_pct,
            "daily_loss_limit_dollars": risk.daily_loss_limit_dollars,
            "max_open_positions": risk.max_open_positions,
            "honor_stop_on_model_trades": risk.honor_stop_on_model_trades,
        },
        "positions": enrich_positions(rows, positions, risk),
        "candidates": candidates,
        "symbols": rows,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = window.now_et.strftime("%Y-%m-%d-%H%M")
    json_path = args.out_dir / f"{stamp}.json"
    md_path = args.out_dir / f"{stamp}.md"
    json_path.write_text(json.dumps(pack, indent=2, default=str))
    markdown = to_markdown(pack)
    md_path.write_text(markdown)
    print(markdown)
    print(f"\nWrote {json_path} and {md_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
