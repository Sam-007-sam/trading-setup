"""
Paper trading: forward-test the daily strategies with fake money.

On its first run this records a start date. From then on, every run replays the
same rules as backtest.py (signal on the close, fill at the next open, costs,
interest on idle cash) over only the sessions since that date, so the record is
genuinely out-of-sample: none of these days were seen when the rules were chosen.

Each run also works out the order every strategy would place at the next open.
No real orders are sent anywhere.

Writes docs/data/paper.json.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import sys

import backtest as B

log = logging.getLogger("paper")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

ROOT = pathlib.Path(__file__).resolve().parent
DATA = pathlib.Path(os.environ.get("PMD_DATA") or (ROOT / "docs" / "data"))
CAPITAL = 1_000_000          # rupees per strategy account


def action(current: int, target: int) -> str:
    names = {1: "long", -1: "short", 0: "flat"}
    if current == target:
        return {1: "Hold long", -1: "Hold short", 0: "Stay flat"}[current]
    if target == 0:
        return "Exit " + names[current] + " at open"
    if current == 0:
        return ("Buy" if target == 1 else "Sell short") + " at open"
    return f"Reverse to {names[target]} at open"


def paper_market(bars: list[dict], started: str) -> list[dict]:
    """Run every strategy from the first session on or after `started`."""
    start = next((i for i, b in enumerate(bars) if b["d"] >= started), len(bars))
    out = []
    for sid, name, fn, short, desc in B.STRATEGIES:
        target = fn(bars, short)
        if start >= len(bars):   # no session since the start date yet
            res = {"curve": [], "trades": [], "stats": None}
            current = 0
        else:
            res = B.run(bars, target, start)
            current = res["stats"]["openPosition"]
        eq = res["curve"][-1][1] if res["curve"] else 1.0
        open_trade = next((t for t in res["trades"] if t.get("open")), None)
        out.append({
            "id": sid, "name": name, "desc": desc,
            "equity": round(CAPITAL * eq), "pnl": round(CAPITAL * (eq - 1)), "pnlPct": round((eq - 1) * 100, 2),
            "position": current, "openTrade": open_trade,
            "next": {"target": target[-1], "action": action(current, target[-1]), "basedOn": bars[-1]["d"]},
            "trades": [t for t in res["trades"] if not t.get("open")][::-1][:20],
            "closedTrades": sum(1 for t in res["trades"] if not t.get("open")),
            "curve": [[d, round(CAPITAL * e)] for d, e in res["curve"]],
        })
    return out


def main() -> int:
    import fetch
    now = dt.datetime.now(fetch.IST)
    path = DATA / "paper.json"
    try:
        started = json.loads(path.read_text())["started"]
    except Exception:
        # First run: start from the next session that hasn't opened yet, so no fill is decided after the fact.
        first = now.date() if (now.hour, now.minute) < (9, 15) else now.date() + dt.timedelta(days=1)
        while first.weekday() >= 5:
            first += dt.timedelta(days=1)
        started = first.isoformat()
        log.info("starting paper trading on %s", started)
    result = {"started": started, "capital": CAPITAL, "generated": now.isoformat(timespec="minutes"),
              "costPct": B.COST_PCT, "cashRatePct": B.CASH_RATE_PCT, "markets": []}
    for mid, name, ticker in B.MARKETS:
        try:
            bars = fetch.completed_bars(fetch.yf_ohlc(ticker, period="2y"), now.date(), now)
            result["markets"].append({"id": mid, "name": name, "lastClose": round(bars[-1]["c"], 2),
                                      "lastDate": bars[-1]["d"], "accounts": paper_market(bars, started)})
        except Exception as e:
            log.warning("%s failed: %s", name, e)
    if not result["markets"]:
        return 1
    path.write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
