"""
Historical candles from Upstox's public market-data API (v3).

Gives 15-minute index history back to January 2022, against Yahoo Finance's 60 days.
If the environment variable UPSTOX_ACCESS_TOKEN is set it is sent as a bearer token;
otherwise the request goes without one (Upstox has served historical candles that way).

Upstox's data is for your own use. Redistributing it in a paid product needs a
licensed data source; see SELLING.md.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import urllib.parse

import requests

BASE = "https://api.upstox.com/v3/historical-candle"
KEYS = {"^NSEI": "NSE_INDEX|Nifty 50", "^NSEBANK": "NSE_INDEX|Nifty Bank"}


def _headers() -> dict:
    h = {"Accept": "application/json"}
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def today_candles(instrument_key: str, unit: str, interval: int) -> list[dict]:
    """The current session's candles (the historical endpoint stops at the previous day)."""
    key = urllib.parse.quote(instrument_key, safe="")
    r = requests.get(f"{BASE}/intraday/{key}/{unit}/{interval}", headers=_headers(), timeout=30)
    r.raise_for_status()
    out = []
    for row in r.json().get("data", {}).get("candles", []):
        ts = dt.datetime.fromisoformat(str(row[0]))
        out.append({"d": ts.date().isoformat(), "t": ts.strftime("%H:%M"),
                    "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])})
    return sorted(out, key=lambda x: x["t"])


def candles(instrument_key: str, unit: str, interval: int, start: dt.date, end: dt.date,
            chunk_days: int = 28) -> list[dict]:
    """[{d, t, o, h, l, c}] in time order, IST. Fetches in chunks under Upstox's 1-month limit.
    If `end` is today (IST), today's candles so far are added from the intraday endpoint."""
    key = urllib.parse.quote(instrument_key, safe="")
    out, seen = [], set()
    to = end
    while to >= start:
        frm = max(start, to - dt.timedelta(days=chunk_days))
        url = f"{BASE}/{key}/{unit}/{interval}/{to.isoformat()}/{frm.isoformat()}"
        for attempt in range(3):
            r = requests.get(url, headers=_headers(), timeout=30)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            break
        if r.status_code in (401, 403):
            raise PermissionError(f"Upstox refused the request ({r.status_code}); set UPSTOX_ACCESS_TOKEN")
        r.raise_for_status()
        for row in r.json().get("data", {}).get("candles", []):
            ts = dt.datetime.fromisoformat(str(row[0]))
            stamp = ts.isoformat()
            if stamp in seen:
                continue
            seen.add(stamp)
            out.append({"d": ts.date().isoformat(), "t": ts.strftime("%H:%M"),
                        "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4])})
        to = frm - dt.timedelta(days=1)
        time.sleep(0.3)   # stay well inside the rate limit
    if end >= dt.datetime.now(IST).date():
        try:
            out += [c for c in today_candles(instrument_key, unit, interval) if c["d"] not in {x["d"] for x in out}]
        except Exception:
            pass                       # history is still usable without today
    out.sort(key=lambda x: (x["d"], x["t"]))
    if not out:
        raise ValueError(f"Upstox returned no candles for {instrument_key}")
    return out
