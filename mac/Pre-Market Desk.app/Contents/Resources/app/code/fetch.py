"""
Pre-Market Desk: morning data pipeline.

Runs before the NSE open (scheduled by GitHub Actions at ~08:15 IST) and writes:
  docs/data/indices.json            1-month day-wise history for each tracked index
  docs/data/briefs/<YYYY-MM-DD>.json the morning brief (global cues, flows, options, news)
  docs/data/briefs/index.json       list of available briefs, newest first

docs/index.html reads these files, so GitHub Pages serves the whole dashboard.

Every data source is optional: if one fails, the rest of the brief is still written
and the failure is recorded in the brief's "compiledNote".
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import re
import sys

import requests

import indicators

log = logging.getLogger("premarket")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
ROOT = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("PMD_DATA") or (ROOT / "docs" / "data"))
BRIEFS = DATA / "briefs"
KEEP_BRIEFS = 60          # how many past briefs the dropdown lists
WINDOW_SESSIONS = 21      # ~1 month of trading days in the day-wise table

# ---------------------------------------------------------------- configuration
# (id, order, display name, short label, kind, Yahoo Finance ticker; "A|B" = try A, then B)
INDICES = [
    ("nifty50",   1, "Nifty 50",                 "Nifty 50",  "index", "^NSEI"),
    ("banknifty", 2, "Nifty Bank",               "Bank Nifty", "index", "^NSEBANK"),
    ("finnifty",  3, "Nifty Financial Services", "FinNifty",  "index", "NIFTY_FIN_SERVICE.NS"),
    ("midcap",    4, "Nifty Midcap Select",      "Midcap Select", "index", "NIFTY_MID_SELECT.NS|^NSEMDCP50|NIFTY_MIDCAP_100.NS"),
    ("sensex",    5, "BSE Sensex",               "Sensex",    "index", "^BSESN"),
    ("indiavix",  6, "India VIX",                "India VIX", "vol",   "^INDIAVIX"),
]

# (group, display name, ticker, decimals)
GLOBAL = [
    ("US (last close)",    "Dow Jones",          "^DJI",      2),
    ("US (last close)",    "S&P 500",            "^GSPC",     2),
    ("US (last close)",    "Nasdaq Composite",   "^IXIC",     2),
    ("US (last close)",    "CBOE VIX",           "^VIX",      2),
    ("Asia (this morning)", "Nikkei 225",        "^N225",     0),
    ("Asia (this morning)", "Hang Seng",         "^HSI",      0),
    ("Asia (this morning)", "Kospi",             "^KS11",     0),
    ("Asia (this morning)", "Shanghai",          "000001.SS", 0),
    ("Asia (this morning)", "ASX 200",           "^AXJO",     0),
    ("Europe (last close)", "DAX",               "^GDAXI",    2),
    ("Europe (last close)", "FTSE 100",          "^FTSE",     2),
    ("Commodities & FX",   "Brent crude ($/bbl)", "BZ=F",     2),
    ("Commodities & FX",   "WTI crude ($/bbl)",  "CL=F",      2),
    ("Commodities & FX",   "Gold ($/oz)",        "GC=F",      0),
    ("Commodities & FX",   "US dollar index",    "DX-Y.NYB",  2),
    ("Commodities & FX",   "USD/INR",            "INR=X",     2),
    ("Commodities & FX",   "US 10Y yield (%)",   "^TNX",      3),
    ("Commodities & FX",   "Bitcoin ($)",        "BTC-USD",   0),
]

# Headline feeds used when no Anthropic API key is set. Replace with any feeds you prefer.
RSS_FEEDS = [
    ("Economic Times Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Business Standard Markets", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Moneycontrol Market Reports", "https://www.moneycontrol.com/rss/marketreports.xml"),
]

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

PROBLEMS: list[str] = []   # collected into the brief so the page shows what failed


# ---------------------------------------------------------------- pure helpers (unit-testable)
def build_series(closes: list[tuple[str, float]], today_ist: dt.date, now_ist: dt.datetime,
                 window: int = WINDOW_SESSIONS) -> dict | None:
    """closes: [(YYYY-MM-DD, close)] ascending. Drops today's bar if the session hasn't closed.
    Returns last/lastDate/dayPct/monthPct/series (with day % change per session)."""
    rows = [(d, float(c)) for d, c in closes if c is not None and c == c]  # drop NaN
    market_closed_today = now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 40)
    if rows and rows[-1][0] == today_ist.isoformat() and not market_closed_today:
        rows = rows[:-1]
    if len(rows) < 2:
        return None
    series = []
    for i in range(1, len(rows)):
        d, c = rows[i]
        prev = rows[i - 1][1]
        series.append({"d": d, "c": round(c, 4), "p": round((c / prev - 1) * 100, 2)})
    series = series[-window:]
    last_d, last_c = rows[-1]
    # 1-month change: last close vs the latest close on or before the same date a month earlier
    last_date = dt.date.fromisoformat(last_d)
    month_ago = (last_date.replace(day=1) - dt.timedelta(days=1))
    month_ago = month_ago.replace(day=min(last_date.day, month_ago.day))
    base = [c for d, c in rows if dt.date.fromisoformat(d) <= month_ago]
    month_pct = round((last_c / base[-1] - 1) * 100, 2) if base else None
    out = {"last": round(last_c, 4), "lastDate": last_d, "dayPct": series[-1]["p"], "series": series}
    if month_pct is not None:
        out["monthPct"] = month_pct
    return out


def summarize_chain(ce: dict[float, float], pe: dict[float, float]) -> dict:
    """Put-call ratio, max pain and the biggest call/put open-interest strikes."""
    tot_ce, tot_pe = sum(ce.values()), sum(pe.values())
    strikes = sorted(set(ce) | set(pe))
    if not strikes or tot_ce == 0:
        return {}

    def payout(k: float) -> float:   # what option writers owe if expiry settles at k
        calls = sum((k - s) * ce.get(s, 0) for s in strikes if s < k)
        puts = sum((s - k) * pe.get(s, 0) for s in strikes if s > k)
        return calls + puts

    return {
        "pcr": round(tot_pe / tot_ce, 2),
        "maxPain": min(strikes, key=payout),
        "callWall": max(ce, key=ce.get) if ce else None,
        "putWall": max(pe, key=pe.get) if pe else None,
    }


def rule_based_bias(glob: list[dict]) -> dict:
    """Fallback opening bias when no LLM summary is available."""
    pick = {g["name"]: g["chg"] for g in glob if g.get("chg") is not None}
    risk = [pick.get(k) for k in ("S&P 500", "Nasdaq Composite", "Nikkei 225", "Hang Seng", "Kospi")]
    risk = [x for x in risk if x is not None]
    avg = sum(risk) / len(risk) if risk else 0.0
    oil = pick.get("Brent crude ($/bbl)") or 0.0
    score = max(-1.0, min(1.0, avg / 2 - max(0.0, oil) / 10))
    label = ("Bullish tilt" if score > 0.35 else "Mildly positive" if score > 0.1 else
             "Bearish tilt" if score < -0.35 else "Mildly negative" if score < -0.1 else "Neutral")
    return {"score": round(score, 2), "label": label,
            "summary": f"Rule-based read: risk assets moved {avg:+.2f}% on average overnight and Brent "
                       f"moved {oil:+.2f}%. Add an Anthropic API key for a written analysis with news."}


# ---------------------------------------------------------------- fetchers
def yf_ohlc(ticker: str, period: str = "2y") -> list[dict]:
    import yfinance as yf
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"no data for {ticker}")
    df = df.dropna(subset=["Close"])
    return [{"d": ix.date().isoformat(), "o": float(r.Open), "h": float(r.High), "l": float(r.Low),
             "c": float(r.Close)} for ix, r in df.iterrows()]


def completed_bars(bars: list[dict], today: dt.date, now: dt.datetime) -> list[dict]:
    """Drop today's still-forming bar before the 3:30 PM close (plus a 10-minute buffer)."""
    if bars and bars[-1]["d"] == today.isoformat() and (now.hour, now.minute) < (15, 40):
        return bars[:-1]
    return bars


def yf_closes(ticker: str, period: str = "3mo") -> list[tuple[str, float]]:
    import yfinance as yf  # imported lazily so helpers above can be tested without it
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"no data for {ticker}")
    return [(ix.date().isoformat(), float(v)) for ix, v in df["Close"].dropna().items()]


def fetch_indices(today: dt.date, now: dt.datetime) -> list[dict]:
    old = {}
    try:
        old = {i["id"]: i for i in json.loads((DATA / "indices.json").read_text())}
    except Exception:
        pass
    out = []
    for iid, order, name, short, kind, tickers in INDICES:
        doc = {"id": iid, "order": order, "name": name, "short": short, "kind": kind}
        errors = []
        for ticker in tickers.split("|"):
            try:
                bars = completed_bars(yf_ohlc(ticker), today, now)
                built = build_series([(b["d"], b["c"]) for b in bars], today, now)
                if not built or len(bars) < 40:
                    raise ValueError("not enough history")
                doc.update(built)
                if ticker == "NIFTY_MIDCAP_100.NS":
                    doc.update(name="Nifty Midcap 100", short="Midcap 100")
                elif ticker == "^NSEMDCP50":
                    doc.update(name="Nifty Midcap 50", short="Midcap 50")
                doc["tech"] = indicators.analyze(bars, kind)
                doc["note"] = f"Source: Yahoo Finance ({ticker}). 1-month change vs the close a month earlier."
                break
            except Exception as e:
                errors.append(f"{ticker}: {e}")
        else:  # every ticker failed: keep yesterday's data rather than blanking the index
            PROBLEMS.append(f"{name}: {'; '.join(errors)}")
            log.warning("index %s failed: %s", name, errors)
            if iid in old:
                doc = old[iid]
                doc["note"] = (doc.get("note", "").replace(" (Not refreshed this morning.)", "")
                               + " (Not refreshed this morning.)").strip()
            else:
                continue
        out.append(doc)
    return out


def fetch_global() -> list[dict]:
    rows = []
    for group, name, ticker, dec in GLOBAL:
        try:
            closes = yf_closes(ticker, period="7d")
            last, prev = closes[-1][1], closes[-2][1]
            if ticker == "^TNX" and last > 20:   # some feeds quote yield x10
                last, prev = last / 10, prev / 10
            rows.append({"group": group, "name": name, "value": f"{last:,.{dec}f}",
                         "chg": round((last / prev - 1) * 100, 2), "raw": last})
        except Exception as e:
            PROBLEMS.append(f"{name}: {e}")
            log.warning("global %s failed: %s", name, e)
    return rows


def nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    s.get("https://www.nseindia.com/option-chain", timeout=15)  # sets the cookies the API needs
    return s


def _nse_json(s: requests.Session, url: str) -> dict:
    r = s.get(url, timeout=20)
    if not r.ok:
        moved = f" (redirected to {r.url})" if r.history else ""
        raise ValueError(f"HTTP {r.status_code}{moved}; NSE often blocks cloud servers, "
                         "so this usually works when run from home")
    return r.json()


def fetch_options(s: requests.Session, symbol: str) -> dict | None:
    """Nearest-expiry option chain. Uses NSE's current v3 API, falling back to the older one."""
    try:
        info = _nse_json(s, f"https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}")
        expiry = info["expiryDates"][0]
        data = _nse_json(s, "https://www.nseindia.com/api/option-chain-v3?type=Indices"
                            f"&symbol={symbol}&expiry={expiry}")
        rows = data["records"]["data"]
    except Exception as v3_err:
        try:
            rec = _nse_json(s, f"https://www.nseindia.com/api/option-chain-indices?symbol={symbol}")["records"]
            expiry = rec["expiryDates"][0]
            rows = [d for d in rec["data"] if d.get("expiryDate") == expiry]
        except Exception:
            raise v3_err
    ce = {d["strikePrice"]: d["CE"]["openInterest"] for d in rows if d.get("CE")}
    pe = {d["strikePrice"]: d["PE"]["openInterest"] for d in rows if d.get("PE")}
    out = summarize_chain(ce, pe)
    if not out:
        raise ValueError("option chain came back empty")
    out["expiry"] = expiry
    return out


def fetch_flows(s: requests.Session) -> dict | None:
    out = {}
    for row in _nse_json(s, "https://www.nseindia.com/api/fiidiiTradeReact"):
        cat = row.get("category", "").upper()
        val = float(str(row.get("netValue", "0")).replace(",", ""))
        if "FII" in cat or "FPI" in cat:
            out["fii"], out["date"] = val, row.get("date")
        elif "DII" in cat:
            out["dii"] = val
    return out or None


def fetch_rss(limit: int = 12) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []
    items = []
    for src, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:5]:
                body = re.sub("<[^>]+>", "", e.get("summary", ""))[:220]
                items.append({"cat": src, "impact": "neu", "title": e.get("title", ""), "body": body})
        except Exception as e:
            PROBLEMS.append(f"RSS {src}: {e}")
    return items[:limit]


def llm_brief(context: dict) -> dict | None:
    """Ask Claude (with web search) for the written parts of the brief. Returns None without a key."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    today = context["date"]
    prompt = f"""You are preparing a pre-market brief for an Indian index F&O trader. Today is {today}.
The brief covers the previous NSE close (3:30 PM IST) up to 9:00 AM IST today.

Numbers already fetched (trust these, don't re-derive them):
{json.dumps(context, ensure_ascii=False)[:12000]}

Use web search for: GIFT Nifty level/change this morning; yesterday's Indian close recap (sector moves,
top Nifty gainers/losers, breadth); provisional FII/DII cash figures if missing above; the most important
global and Indian news since yesterday's close; stocks in focus today; analyst Nifty support/resistance;
today's events (expiries, data releases, listings, Fed/RBI speakers); news on these watchlist stocks:
{", ".join(context.get("watchlist", [])) or "none"}.

Reply with ONLY a JSON object, no markdown fences, with exactly these keys:
{{"bias":{{"score":-1..1,"label":"short verdict","summary":"3-4 sentences"}},
 "gift":{{"value":"e.g. ~22,815","change":"e.g. -22 pts","note":"implication"}},
 "india":{{"recap":"2-3 sentences","sectors":[{{"name":"IT","chg":-0.26}}],"gainers":[{{"name":"","chg":0}}],"losers":[{{"name":"","chg":0}}]}},
 "flows":{{"date":"","fii":0,"dii":0,"fiiMtd":null,"note":""}},
 "levels":{{"support":[0,0],"resistance":[0,0],"note":""}},
 "news":[{{"cat":"Global and macro|India","impact":"pos|neg|neu","title":"","body":"1-2 sentences, own words"}}],
 "watch":[{{"name":"","why":""}}],
 "watchNotes":{{"SYMBOL":"one line, only if there is real news"}},
 "events":["..."],
 "sources":[{{"name":"","url":""}}]}}
Keep 6-10 news items and 6-10 stocks in focus. Paraphrase; never copy article text. Use null where unknown."""
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 8000,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 10}],
        "messages": [{"role": "user", "content": prompt}],
    }
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=300, json=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    r.raise_for_status()
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("model reply had no JSON")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- assemble
def fmt_signed(x: float | None, suffix: str = "%") -> str:
    return "—" if x is None else f"{x:+.2f}{suffix}"


def pivot_levels(tech: dict | None) -> dict:
    """Fallback Nifty levels from classic pivots when no AI summary is available."""
    pv = (tech or {}).get("pivots")
    if not pv:
        return {}
    return {"support": [round(pv["S1"]), round(pv["S2"])], "resistance": [round(pv["R1"]), round(pv["R2"])],
            "note": f"Classic pivot levels from the last session (pivot {pv['P']:,.0f}). "
                    "See the Indicators tab for the full picture."}


def main() -> int:
    now = dt.datetime.now(IST)
    today = now.date()
    BRIEFS.mkdir(parents=True, exist_ok=True)

    indices = fetch_indices(today, now)
    (DATA / "indices.json").write_text(json.dumps(indices, indent=1))
    glob = fetch_global()

    options, flows = {}, None
    try:
        s = nse_session()
        for sym, key in (("NIFTY", "nifty"), ("BANKNIFTY", "banknifty")):
            try:
                options[key] = fetch_options(s, sym)
            except Exception as e:
                PROBLEMS.append(f"NSE option chain {sym}: {e}")
        try:
            flows = fetch_flows(s)
        except Exception as e:
            PROBLEMS.append(f"NSE FII/DII: {e}")
    except Exception as e:
        PROBLEMS.append(f"NSE site unreachable: {e}")

    try:
        watch = [w["sym"] for w in json.loads((DATA / "watchlist.json").read_text()).get("items", [])]
    except Exception:
        watch = []

    ix = {i["id"]: i for i in indices}
    context = {
        "date": today.isoformat(),
        "indices": {k: {"last": v.get("last"), "dayPct": v.get("dayPct"), "monthPct": v.get("monthPct"),
                        "lastDate": v.get("lastDate"),
                        "technicals": {t: (v.get("tech") or {}).get(t) for t in ("verdict", "pivots", "atr")}
                        | {"readings": [f"{r['name']}: {r['reading']}" for r in (v.get("tech") or {}).get("rows", [])]}}
                    for k, v in ix.items()},
        "global": [{k: g[k] for k in ("group", "name", "value", "chg")} for g in glob],
        "options": options, "flows": flows, "watchlist": watch,
    }
    llm = None
    try:
        llm = llm_brief(context)
    except Exception as e:
        PROBLEMS.append(f"AI summary: {e}")
        log.warning("llm failed: %s", e)

    g = {x["name"]: x for x in glob}
    n, bn, vix = ix.get("nifty50", {}), ix.get("banknifty", {}), ix.get("indiavix", {})
    fl = {**(llm or {}).get("flows", {}), **{k: v for k, v in (flows or {}).items() if v is not None}}
    gift = (llm or {}).get("gift") or {}

    def stat(label, value, change=None, direction=None, note=None):
        return {k: v for k, v in dict(label=label, value=value, change=change, dir=direction, note=note).items() if v}

    def d(x, invert=False):
        if x is None:
            return "flat"
        x = -x if invert else x
        return "up" if x > 0 else "down" if x < 0 else "flat"

    key_stats = [
        stat("GIFT Nifty (pre-open)", gift.get("value") or "—", gift.get("change"), "flat", gift.get("note")),
        stat("Nifty 50 close", f"{n.get('last', 0):,.2f}", fmt_signed(n.get("dayPct")), d(n.get("dayPct"))),
        stat("Bank Nifty close", f"{bn.get('last', 0):,.2f}", fmt_signed(bn.get("dayPct")), d(bn.get("dayPct"))),
        stat("India VIX", f"{vix.get('last', 0):.2f}", fmt_signed(vix.get("dayPct")), d(vix.get("dayPct"), True)),
    ]
    if fl.get("fii") is not None:
        key_stats.append(stat(f"FII net ({fl.get('date', '')})", f"₹{fl['fii']:,.0f} cr",
                              f"DII ₹{fl.get('dii', 0):+,.0f} cr" if fl.get("dii") is not None else None,
                              d(fl["fii"])))
    for name, label, inv in (("USD/INR", "USD/INR", True), ("Brent crude ($/bbl)", "Brent crude", True),
                             ("US 10Y yield (%)", "US 10Y yield", True)):
        if name in g:
            key_stats.append(stat(label, g[name]["value"], fmt_signed(g[name]["chg"]), d(g[name]["chg"], inv)))

    brief = {
        "date": today.isoformat(),
        "windowLabel": (f"{today:%a %d %b %Y}, refreshed {now:%I:%M %p} IST. Includes today's close"
                        if (now.hour, now.minute) >= (15, 40) else
                        f"{today:%a %d %b %Y}, refreshed {now:%I:%M %p} IST. Covers the previous close to now"),
        "bias": (llm or {}).get("bias") or rule_based_bias(glob),
        "keyStats": key_stats,
        "global": [{k: x[k] for k in ("group", "name", "value", "chg")} for x in glob],
        "globalNote": "Asia levels are early-morning readings and keep moving until their close.",
        "india": (llm or {}).get("india") or {"recap": "", "sectors": [], "gainers": [], "losers": []},
        "flows": fl,
        "levels": (llm or {}).get("levels") or pivot_levels(n.get("tech")),
        "options": ({"asOf": f"{now:%I:%M %p} IST, {today:%d %b}", **options} if options else None),
        "news": (llm or {}).get("news") or fetch_rss(),
        "watch": (llm or {}).get("watch") or [],
        "watchNotes": {k.upper(): v for k, v in ((llm or {}).get("watchNotes") or {}).items() if v},
        "events": (llm or {}).get("events") or [],
        "sources": (llm or {}).get("sources") or [{"name": "Yahoo Finance", "url": "https://finance.yahoo.com"},
                                                   {"name": "NSE India", "url": "https://www.nseindia.com"}],
        "compiledNote": f"Generated automatically at {now:%I:%M %p} IST."
                        + (f" Not refreshed: {'; '.join(PROBLEMS)}." if PROBLEMS else ""),
    }
    (BRIEFS / f"{today.isoformat()}.json").write_text(json.dumps(brief, ensure_ascii=False, indent=1))

    dates = sorted((p.stem for p in BRIEFS.glob("????-??-??.json")), reverse=True)[:KEEP_BRIEFS]
    (BRIEFS / "index.json").write_text(json.dumps({"dates": dates}))
    log.info("wrote brief %s (%d problems)", today, len(PROBLEMS))
    for p in PROBLEMS:
        log.warning(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
