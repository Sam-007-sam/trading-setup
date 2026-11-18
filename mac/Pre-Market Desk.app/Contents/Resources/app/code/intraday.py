"""
Intraday strategy backtests on 15-minute bars.

Yahoo Finance only serves 15-minute history for the last ~60 days, so this is a
short sample: read the results as "how did this behave recently", not proof.
A broker data API (Upstox, Zerodha Kite) can supply years of intraday history.

Rules applied to every strategy:
  * Signals are read on a 15-minute candle's CLOSE and filled at the NEXT candle's OPEN.
  * Stop-losses fill at the stop price, or at the candle's open if it gapped past the stop.
  * Every position is squared off at the open of the 3:15 PM candle, and no new trade starts after
    3:00 PM. Nothing is held overnight.
  * COST_PCT is charged per side (futures brokerage, STT, exchange fees and slippage).
  * The index itself is traded at 1x. NSE index data has no volume, so "VWAP" here is the
    session's running average price (a time-weighted stand-in).

Writes docs/data/intraday.json for the dashboard.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import sys

import indicators as I

log = logging.getLogger("intraday")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("PMD_DATA") or (ROOT / "docs" / "data"))
COST_PCT = 0.03
SQUARE_OFF = "15:15"
LAST_ENTRY = "15:00"     # no new positions in the final candle before square-off
NARROW_CPR_PCT = 0.25
MARKETS = [("nifty50", "Nifty 50", "^NSEI"), ("banknifty", "Nifty Bank", "^NSEBANK")]


# ---------------------------------------------------------------- per-day simulation
def simulate_day(bars: list[dict], signal_fn, ctx: dict, cost_pct: float = COST_PCT) -> list[dict]:
    """bars: one session's candles [{t:'HH:MM', o,h,l,c}] in time order.
    signal_fn(i, bars, state, ctx) -> None or {"side": +1/-1, "stop": price or None}
    or {"exit": True}. Called on each candle close while trading is allowed."""
    cost = cost_pct / 100
    trades, pos, state = [], None, {"n": 0}

    def close(px, t, why):
        nonlocal pos
        ret = pos["side"] * (px / pos["px"] - 1) - 2 * cost
        trades.append({"side": "long" if pos["side"] > 0 else "short", "in": pos["t"], "out": t,
                       "entry": round(pos["px"], 2), "exit": round(px, 2), "pct": round(ret * 100, 3), "why": why})
        pos = None

    pending = None
    for i, b in enumerate(bars):
        # fill orders decided on the previous close
        if pending is not None:
            if pos and (pending.get("exit") or pending.get("side") != pos["side"]):
                close(b["o"], b["t"], "signal")
            if pending.get("side") and b["t"] < LAST_ENTRY and pos is None:
                pos = {"side": pending["side"], "px": b["o"], "t": b["t"], "stop": pending.get("stop")}
                state["n"] += 1
            pending = None
        if b["t"] >= SQUARE_OFF:
            if pos:
                close(b["o"], b["t"], "square-off")
            break
        # stop-loss inside this candle
        if pos and pos["stop"] is not None:
            st = pos["stop"]
            if pos["side"] > 0 and b["l"] <= st:
                close(min(b["o"], st), b["t"], "stop")
            elif pos["side"] < 0 and b["h"] >= st:
                close(max(b["o"], st), b["t"], "stop")
        state["pos"] = pos["side"] if pos else 0
        pending = signal_fn(i, bars, state, ctx)
    else:
        if pos:   # session data ended early: exit at the last close
            close(bars[-1]["c"], bars[-1]["t"], "end of data")
    return trades


# ---------------------------------------------------------------- strategies
def s_orb(i, bars, state, ctx):
    """Opening range breakout: trade the first close outside the 9:15-9:30 range, stop at the other side."""
    if i == 0 or state["n"] >= 1 or state["pos"]:
        return None
    hi, lo = bars[0]["h"], bars[0]["l"]
    c = bars[i]["c"]
    if c > hi:
        return {"side": 1, "stop": lo}
    if c < lo:
        return {"side": -1, "stop": hi}
    return None


def s_vwap(i, bars, state, ctx):
    """Trade crosses of the session average price after 9:45, max 4 entries a day; a cross the other way flips."""
    if i < 2 or state["n"] >= 4:
        return {"exit": True} if state["pos"] and state["n"] >= 4 and _avg_side(i, bars) != state["pos"] else None
    side = _avg_side(i, bars)
    if side and side != state["pos"] and _avg_side(i - 1, bars) != side:
        return {"side": side, "stop": None}
    return None


def _avg_side(i, bars):
    avg = sum((b["h"] + b["l"] + b["c"]) / 3 for b in bars[:i + 1]) / (i + 1)
    c = bars[i]["c"]
    return 1 if c > avg else -1 if c < avg else 0


def s_cpr(i, bars, state, ctx, narrow_only=False):
    """If the first 15-minute candle closes above the CPR, go long (stop at the pivot);
    below it, go short. Optionally only on narrow-CPR days."""
    cpr = ctx.get("cpr")
    if i != 0 or not cpr or (narrow_only and cpr["cprWidthPct"] > NARROW_CPR_PCT):
        return None
    c = bars[0]["c"]
    if c > cpr["cprTop"]:
        return {"side": 1, "stop": cpr["P"]}
    if c < cpr["cprBottom"]:
        return {"side": -1, "stop": cpr["P"]}
    return None


def s_long_day(i, bars, state, ctx):
    """Baseline: buy at 9:30 every day, sell at 3:15."""
    return {"side": 1, "stop": None} if i == 0 else None


STRATEGIES = [
    ("orb", "Opening range breakout (15 min)", s_orb,
     "Wait for the first 15-minute candle. Go long when a later candle closes above its high, short below its low. "
     "Stop at the other end of the range; square off at 3:15."),
    ("vwap", "Session average (VWAP) cross", s_vwap,
     "From 9:45, go long when price closes above the session's running average price and short when it closes below. "
     "Flips on each cross, up to 4 entries a day."),
    ("cpr", "CPR open, every day", lambda i, b, s, c: s_cpr(i, b, s, c, False),
     "If the 9:15 candle closes above the Central Pivot Range go long, below it go short. Stop at the pivot."),
    ("cpr_narrow", f"CPR open, narrow-CPR days only", lambda i, b, s, c: s_cpr(i, b, s, c, True),
     f"Same as above, but only on days when the CPR is narrower than {NARROW_CPR_PCT}% of the pivot, "
     "which traders read as a likely trending day."),
]
BASELINE = ("long_day", "Baseline: long 9:30 to 3:15 daily", s_long_day,
            "Buy at 9:30 and sell at 3:15 every day, for comparison.")


# ---------------------------------------------------------------- run over many days
def group_sessions(candles: list[dict]) -> list[tuple[str, list[dict]]]:
    days: dict[str, list[dict]] = {}
    for c in candles:
        days.setdefault(c["d"], []).append(c)
    return [(d, sorted(v, key=lambda x: x["t"])) for d, v in sorted(days.items())
            if any(x["t"] == "09:15" for x in v)]


def run_strategy(sessions, fn, cost_pct=COST_PCT) -> dict:
    eq, peak, mdd, all_trades, curve = 1.0, 1.0, 0.0, [], []
    for k in range(1, len(sessions)):
        d, bars = sessions[k]
        prev = sessions[k - 1][1]
        cpr = I.pivots(max(b["h"] for b in prev), min(b["l"] for b in prev), prev[-1]["c"])
        tr = simulate_day(bars, fn, {"cpr": cpr}, cost_pct)
        for t in tr:
            t["d"] = d
            eq *= 1 + t["pct"] / 100
        all_trades += tr
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        curve.append([d, round(eq, 5)])
    wins = [t["pct"] for t in all_trades if t["pct"] > 0]
    losses = [t["pct"] for t in all_trades if t["pct"] <= 0]
    days = max(1, len(sessions) - 1)
    return {
        "stats": {
            "totalPct": round((eq - 1) * 100, 2), "maxDdPct": round(mdd * 100, 2),
            "trades": len(all_trades), "tradesPerDay": round(len(all_trades) / days, 2),
            "daysTraded": len({t["d"] for t in all_trades}),
            "winRatePct": round(len(wins) / len(all_trades) * 100) if all_trades else None,
            "avgWinPct": round(sum(wins) / len(wins), 3) if wins else None,
            "avgLossPct": round(sum(losses) / len(losses), 3) if losses else None,
            "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "stopsHit": sum(1 for t in all_trades if t["why"] == "stop"),
        },
        "curve": curve, "trades": all_trades[-12:][::-1],
    }


def backtest_market(candles: list[dict]) -> dict:
    sessions = group_sessions(candles)
    if len(sessions) < 10:
        raise ValueError(f"only {len(sessions)} full sessions")
    out = {"from": sessions[1][0], "to": sessions[-1][0], "sessions": len(sessions) - 1, "strategies": []}
    for sid, name, fn, desc in STRATEGIES + [BASELINE]:
        out["strategies"].append({"id": sid, "name": name, "desc": desc, "baseline": sid == BASELINE[0],
                                  **run_strategy(sessions, fn)})
    return out


def yf_15m(ticker: str) -> list[dict]:
    import yfinance as yf
    df = yf.Ticker(ticker).history(period="60d", interval="15m", auto_adjust=False)
    if df is None or df.empty:
        raise ValueError(f"no intraday data for {ticker}")
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata")
    return [{"d": ix.date().isoformat(), "t": ix.strftime("%H:%M"), "o": float(r.Open), "h": float(r.High),
             "l": float(r.Low), "c": float(r.Close)} for ix, r in df.dropna(subset=["Close"]).iterrows()]


def main() -> int:
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now = dt.datetime.now(ist)
    result = {"generated": now.isoformat(timespec="minutes"), "costPct": COST_PCT, "interval": "15m", "markets": []}
    for mid, name, ticker in MARKETS:
        try:
            candles = yf_15m(ticker)
            # leave out today's session if it hasn't finished
            if (now.hour, now.minute) < (15, 30):
                candles = [c for c in candles if c["d"] != now.date().isoformat()]
            result["markets"].append({"id": mid, "name": name, **backtest_market(candles)})
            log.info("%s: %d sessions", name, result["markets"][-1]["sessions"])
        except Exception as e:
            log.warning("%s failed: %s", name, e)
    if not result["markets"]:
        return 1
    (DATA / "intraday.json").write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
