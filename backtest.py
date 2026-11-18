"""
Backtest simple daily-chart strategies on the tracked indices.

Rules (applied identically to every strategy):
  * A signal is read on a session's CLOSE; the trade happens at the NEXT session's OPEN.
    No look-ahead: a strategy only ever sees bars up to and including the signal bar.
  * One position at a time, fully invested (1x, no leverage), long or short.
  * COST_PCT is charged on every entry and every exit (brokerage, STT, slippage).
  * While flat, money earns CASH_RATE_PCT a year (a liquid fund), for the calendar days held in cash.
  * Sharpe ratio is measured on returns above that same cash rate.
  * Index prices exclude dividends (about 1-1.5% a year on Nifty), which understates buy-and-hold
    and every long position against cash by roughly that much while invested.
  * Results are per index, not a portfolio.

Writes docs/data/backtest.json for the dashboard's Backtest tab.
Run:  python backtest.py            (fetches data from Yahoo Finance)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import sys

import indicators as I

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("PMD_DATA") or (ROOT / "docs" / "data"))
YEARS = 5
COST_PCT = 0.05          # per side, in percent
CASH_RATE_PCT = 6.5      # yearly return on idle cash (liquid fund), earned on days the strategy is flat
WARMUP = 220             # bars needed before the first trade (200-day EMA + buffer)

MARKETS = [("nifty50", "Nifty 50", "^NSEI"), ("banknifty", "Nifty Bank", "^NSEBANK")]


# ---------------------------------------------------------------- strategies
# Each returns a list of desired positions per bar: +1 long, -1 short, 0 flat.
# Position i is decided using bars[0..i] only and executed at bar i+1's open.
def strat_supertrend(b, short):
    _, d = I.supertrend([x["h"] for x in b], [x["l"] for x in b], [x["c"] for x in b], 10, 3)
    return [0 if v is None else (1 if v == 1 else (-1 if short else 0)) for v in d]


def strat_ema_cross(b, short):
    c = [x["c"] for x in b]
    f, s = I.ema(c, 20), I.ema(c, 50)
    return [0 if a is None or z is None else (1 if a > z else (-1 if short else 0)) for a, z in zip(f, s)]


def strat_rsi_pullback(b, short):
    """Buy a dip inside an uptrend: price above the 200-day EMA and RSI drops below 40.
    Exit when RSI recovers above 60 or price closes below the 200-day EMA."""
    c = [x["c"] for x in b]
    r, e = I.rsi(c, 14), I.ema(c, 200)
    pos, out = 0, []
    for i in range(len(c)):
        if r[i] is None or e[i] is None:
            out.append(0)
            continue
        if pos == 0 and c[i] > e[i] and r[i] < 40:
            pos = 1
        elif pos == 1 and (r[i] > 60 or c[i] < e[i]):
            pos = 0
        out.append(pos)
    return out


def strat_trend_filter(b, short):
    """Long only while price is above the 200-day EMA."""
    c = [x["c"] for x in b]
    e = I.ema(c, 200)
    return [0 if v is None else (1 if p > v else 0) for p, v in zip(c, e)]


STRATEGIES = [
    ("supertrend", "Supertrend (10, 3), long only", strat_supertrend, False,
     "Long while Supertrend points up, flat when it flips down."),
    ("supertrend_ls", "Supertrend (10, 3), long and short", strat_supertrend, True,
     "Always in the market: long in an uptrend, short in a downtrend (as with index futures)."),
    ("ema_cross", "EMA 20/50 crossover, long only", strat_ema_cross, False,
     "Long while the 20-day EMA is above the 50-day, flat otherwise."),
    ("rsi_pullback", "RSI pullback in uptrend", strat_rsi_pullback, False,
     "Buy when RSI(14) dips below 40 while price is above the 200-day EMA; sell when RSI tops 60 "
     "or price closes below the 200-day EMA."),
    ("trend_filter", "Above 200-day EMA", strat_trend_filter, False,
     "Long only while price is above its 200-day EMA, flat below it."),
]


# ---------------------------------------------------------------- engine
def run(bars: list[dict], target: list[int], start: int, cost_pct: float = COST_PCT,
        cash_rate_pct: float = CASH_RATE_PCT) -> dict:
    """Simulate next-open execution. Returns equity curve, trades and stats."""
    cost = cost_pct / 100
    eq, pos, entry_px, entry_d, entry_eq = 1.0, 0, None, None, None
    curve, trades = [], []
    days_in = 0

    def close_trade(px, d):
        nonlocal eq
        eq *= 1 - cost
        trades.append({"side": "long" if pos == 1 else "short", "entry": entry_d, "exit": d,
                       "entryPx": round(entry_px, 2), "exitPx": round(px, 2),
                       "pct": round((eq / entry_eq - 1) * 100, 2),
                       "days": (dt.date.fromisoformat(d) - dt.date.fromisoformat(entry_d)).days})

    for i in range(start, len(bars)):
        bar = bars[i]
        want = target[i - 1]   # decided on the previous close
        # 1) from the previous close to today's open, on what was held overnight:
        #    the market's gap if invested, interest for the calendar days if in cash
        if pos != 0:
            eq *= 1 + pos * (bar["o"] / bars[i - 1]["c"] - 1)
        elif cash_rate_pct:
            gap = (dt.date.fromisoformat(bar["d"]) - dt.date.fromisoformat(bars[i - 1]["d"])).days
            eq *= (1 + cash_rate_pct / 100) ** (gap / 365)
        # 2) change position at today's open
        if want != pos:
            if pos != 0:
                close_trade(bar["o"], bar["d"])
            pos = want
            if pos != 0:
                eq *= 1 - cost
                entry_px, entry_d, entry_eq = bar["o"], bar["d"], eq / (1 - cost)
        # 3) open to close move on whatever we now hold
        if pos != 0:
            eq *= 1 + pos * (bar["c"] / bar["o"] - 1)
            days_in += 1
        curve.append((bar["d"], eq))
    if pos != 0:   # mark the open trade to the last close
        trades.append({"side": "long" if pos == 1 else "short", "entry": entry_d, "exit": None,
                       "entryPx": round(entry_px, 2), "exitPx": round(bars[-1]["c"], 2),
                       "pct": round((eq / entry_eq - 1) * 100, 2), "open": True,
                       "days": (dt.date.fromisoformat(bars[-1]["d"]) - dt.date.fromisoformat(entry_d)).days})
    return {"curve": curve, "trades": trades,
            "stats": stats(curve, trades, pos, days_in / max(1, len(bars) - start),
                           [bars[start - 1]["d"]] + [d for d, _ in curve])}


def stats(curve, trades, final_pos=0, exposure=None, dates=None, rf_pct: float = CASH_RATE_PCT) -> dict:
    """Sharpe is on daily returns ABOVE the risk-free (liquid fund) rate, annualised."""
    eqs = [e for _, e in curve]
    days = (dt.date.fromisoformat(curve[-1][0]) - dt.date.fromisoformat(curve[0][0])).days or 1
    total = eqs[-1] / 1.0 - 1
    cagr = (eqs[-1]) ** (365.25 / days) - 1 if eqs[-1] > 0 else -1
    peak, mdd = 1.0, 0.0
    for e in eqs:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    full = [1.0] + eqs                     # equity before the first session, then each close
    ds = dates or [curve[0][0]] + [d for d, _ in curve]
    rets = []
    for i in range(1, len(full)):
        gap = (dt.date.fromisoformat(ds[i]) - dt.date.fromisoformat(ds[i - 1])).days
        rets.append(full[i] / full[i - 1] - 1 - ((1 + rf_pct / 100) ** (gap / 365) - 1))
    mean = sum(rets) / len(rets) if rets else 0
    sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5 if rets else 0
    closed = [t for t in trades if not t.get("open")]
    wins = [t["pct"] for t in closed if t["pct"] > 0]
    losses = [t["pct"] for t in closed if t["pct"] <= 0]
    in_mkt = exposure if exposure is not None else 1.0
    return {
        "totalPct": round(total * 100, 1), "cagrPct": round(cagr * 100, 1), "maxDdPct": round(mdd * 100, 1),
        "sharpe": round(mean / sd * 252 ** 0.5, 2) if sd > 1e-9 else None,
        "trades": len(closed), "winRatePct": round(len(wins) / len(closed) * 100, 0) if closed else None,
        "avgWinPct": round(sum(wins) / len(wins), 2) if wins else None,
        "avgLossPct": round(sum(losses) / len(losses), 2) if losses else None,
        "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "exposurePct": round(in_mkt * 100, 0), "openPosition": final_pos,
    }


def thin(curve, n=260):
    """Downsample an equity curve for the page, always keeping the last point."""
    step = max(1, len(curve) // n)
    pts = curve[::step]
    if pts[-1] != curve[-1]:
        pts.append(curve[-1])
    return [[d, round(e, 4)] for d, e in pts]


def backtest_market(bars: list[dict]) -> dict:
    start = max(WARMUP, len(bars) - YEARS * 248)
    hold = run(bars, [1] * len(bars), start, cost_pct=0)   # buy and hold, no trading costs
    cash = run(bars, [0] * len(bars), start)                 # liquid fund only
    out = {"from": bars[start]["d"], "to": bars[-1]["d"], "sessions": len(bars) - start,
           "buyHold": {"stats": hold["stats"], "curve": thin(hold["curve"])},
           "cash": {"stats": cash["stats"]}, "strategies": []}
    for sid, name, fn, short, desc in STRATEGIES:
        res = run(bars, fn(bars, short), start)
        out["strategies"].append({"id": sid, "name": name, "desc": desc, "stats": res["stats"],
                                  "curve": thin(res["curve"]), "trades": res["trades"][-15:][::-1]})
    return out


def main() -> int:
    import fetch   # reuse the Yahoo Finance loader
    now = dt.datetime.now(fetch.IST)
    result = {"generated": now.isoformat(timespec="minutes"), "costPct": COST_PCT, "cashRatePct": CASH_RATE_PCT,
              "years": YEARS, "markets": []}
    for mid, name, ticker in MARKETS:
        try:
            bars = fetch.completed_bars(fetch.yf_ohlc(ticker, period=f"{YEARS + 2}y"), now.date(), now)
            bars = [b for b in bars if b["o"] > 0 and b["h"] >= b["l"]]
            if len(bars) < WARMUP + 250:
                raise ValueError(f"only {len(bars)} bars")
            result["markets"].append({"id": mid, "name": name, **backtest_market(bars)})
            log.info("%s: backtested %d sessions", name, len(bars))
        except Exception as e:
            log.warning("%s failed: %s", name, e)
    if not result["markets"]:
        log.error("no market could be backtested")
        return 1
    (DATA / "backtest.json").write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
