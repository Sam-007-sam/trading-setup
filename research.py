"""
Intraday rule research with a proper out-of-sample test.

  python research.py            (needs network: Upstox public candles)

Protocol, fixed BEFORE looking at any results:
  * Data: Upstox 15-minute candles for Nifty 50 and Nifty Bank, Jan 2022 to the last close.
  * TRAIN = 2022-01-01 .. 2024-12-31. Every variant is scored here only.
  * TEST  = 2025-01-01 .. latest. The top variants by TRAIN score are judged here once.
  * Score = profit factor after costs, needing at least 150 trades per index in TRAIN.
    A variant must be profitable on BOTH indices to count.
  * Luck check: the share of ALL variants that are profitable in TEST on both indices. If that
    share is high, the market simply favoured the family in the test years and a "winner"
    proves little; if it is low and the TRAIN-chosen variants still pass, that means more.
  * Costs and fills: identical to intraday.py (next-candle fills, stops before targets,
    3:15 PM square-off, intraday.COST_PCT per side).

Writes docs/data/research.json and research/RESULTS.md.
"""
from __future__ import annotations

import datetime as dt
import itertools
import json
import pathlib
import sys

import indicators as I
import intraday as X

ROOT = pathlib.Path(__file__).resolve().parent
TRAIN_END = "2024-12-31"
MIN_TRADES = 150
TOP_N = 5


# ---------------------------------------------------------------- a family of rules
def make_orb(range_bars, direction, target_r, trail, last_entry, stop_mode, skip_gap):
    """Opening-range breakout with options.
    direction: none | trend | cpr | both (trend and CPR agree)
    stop_mode: range (other side of the range) | mid (middle of the range)
    skip_gap: skip days opening more than this % away from the previous close (None = never)."""
    def rule(i, bars, state, ctx):
        if i < range_bars or state["n"] >= 1 or state["pos"] or bars[i]["t"] >= last_entry:
            return None
        hi = max(b["h"] for b in bars[:range_bars])
        lo = min(b["l"] for b in bars[:range_bars])
        c = bars[i]["c"]
        if (hi - lo) / c * 100 > X.ORB_SKIP_RANGE_PCT:
            return None
        if skip_gap is not None and ctx.get("prevClose") and \
                abs(bars[0]["o"] / ctx["prevClose"] - 1) * 100 > skip_gap:
            return None
        cpr = ctx.get("cpr") or {}
        cpr_side = 1 if bars[0]["o"] > cpr.get("cprTop", 9e9) else -1 if bars[0]["o"] < cpr.get("cprBottom", -1) else 0
        allowed = {"none": {1, -1}, "trend": {ctx.get("trend", 0)}, "cpr": {cpr_side},
                   "both": {ctx.get("trend", 0)} & {cpr_side}}[direction] - {0}
        side = 1 if c > hi else -1 if c < lo else 0
        if not side or side not in allowed:
            return None
        stop = (lo if side > 0 else hi) if stop_mode == "range" else (hi + lo) / 2
        return {"side": side, "stop": stop, "targetR": target_r, "trail": trail}
    return rule


GRID = {
    "range_bars": [1, 2],                     # 15 or 30-minute opening range
    "direction": ["none", "trend", "cpr", "both"],
    "target_r": [1, 1.5, 2, None],            # None = hold to the stop or 3:15
    "trail": [False, True],
    "last_entry": ["11:00", "13:00"],
    "stop_mode": ["range", "mid"],
    "skip_gap": [None, 0.5],
}


def variants():
    keys = list(GRID)
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, combo))
        yield p, make_orb(**p)


def label(p):
    return (f"ORB {15 * p['range_bars']}m, dir={p['direction']}, target={p['target_r'] or 'none'}R, "
            f"trail={'BE' if p['trail'] else 'no'}, entries<{p['last_entry']}, stop={p['stop_mode']}, "
            f"gap-skip={p['skip_gap'] or 'no'}")


# ---------------------------------------------------------------- evaluation
def contexts(sessions):
    """Per-session context from earlier sessions only: trend, CPR, previous close."""
    out = []
    for k in range(len(sessions)):
        if k == 0:
            out.append(None)
            continue
        prev = sessions[k - 1][1]
        out.append({"cpr": I.pivots(max(b["h"] for b in prev), min(b["l"] for b in prev), prev[-1]["c"]),
                    "trend": X.daily_trend([s[1][-1]["c"] for s in sessions[max(0, k - 20):k]]),
                    "prevClose": prev[-1]["c"]})
    return out


def score(sessions, ctxs, fn, period):
    pnl, wins, losses, n, eq, peak, mdd = [], 0.0, 0.0, 0, 1.0, 1.0, 0.0
    for (d, bars), ctx in zip(sessions, ctxs):
        if ctx is None or (period == "train" and d > TRAIN_END) or (period == "test" and d <= TRAIN_END):
            continue
        for t in X.simulate_day(bars, fn, ctx):
            r = t["pct"]
            n += 1
            wins += max(r, 0)
            losses += -min(r, 0)
            eq *= 1 + r / 100
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
    return {"trades": n, "pf": round(wins / losses, 3) if losses else None,
            "totalPct": round((eq - 1) * 100, 2), "maxDdPct": round(mdd * 100, 2)}


def research(data: dict) -> dict:
    """data: {market: candles}. Returns every variant's TRAIN and TEST scores per market."""
    prepared = {}
    for m, candles in data.items():
        ss = X.group_sessions(candles)
        prepared[m] = (ss, contexts(ss))
    rows = []
    for p, fn in variants():
        row = {"params": p, "label": label(p), "train": {}, "test": {}}
        for m, (ss, cx) in prepared.items():
            row["train"][m] = score(ss, cx, fn, "train")
            row["test"][m] = score(ss, cx, fn, "test")
        rows.append(row)

    def eligible(r):
        return all(r["train"][m]["trades"] >= MIN_TRADES and (r["train"][m]["pf"] or 0) > 0 for m in prepared)

    def train_key(r):          # the weaker index decides: a rule must work on both
        return min(r["train"][m]["pf"] or 0 for m in prepared)

    def passes(r, period):
        return all((r[period][m]["pf"] or 0) > 1 for m in prepared)

    chosen = sorted([r for r in rows if eligible(r)], key=train_key, reverse=True)[:TOP_N]
    luck = sum(passes(r, "test") for r in rows) / len(rows)
    survivors = [r for r in chosen if passes(r, "train") and passes(r, "test")]
    spans = {m: {"from": ss[1][0], "to": ss[-1][0], "sessions": len(ss) - 1,
                 "trainSessions": sum(1 for d, _ in ss[1:] if d <= TRAIN_END),
                 "testSessions": sum(1 for d, _ in ss[1:] if d > TRAIN_END)} for m, (ss, _) in prepared.items()}
    return {"variants": len(rows), "trainEnd": TRAIN_END, "minTrades": MIN_TRADES, "spans": spans,
            "costPct": X.COST_PCT, "chosen": chosen, "survivors": survivors,
            "shareAllPassingTest": round(luck, 3),
            "shareAllPassingTrain": round(sum(passes(r, "train") for r in rows) / len(rows), 3),
            "testPfAll": sorted(min(r["test"][m]["pf"] or 0 for m in prepared) for r in rows)}


def write_report(res: dict, path: pathlib.Path) -> None:
    lines = ["# Intraday rule research", "",
             f"{res['variants']} variants of the opening-range breakout. Chosen on data up to {res['trainEnd']}, "
             f"tested once on later data. Costs {res['costPct']}% per side. A rule must be profitable "
             f"(profit factor > 1) on both Nifty and Bank Nifty.", ""]
    for m, s in res["spans"].items():
        lines.append(f"- {m}: {s['from']} to {s['to']}, {s['trainSessions']} training and {s['testSessions']} test sessions")
    lines += ["", f"Share of ALL variants profitable on both indices: training {res['shareAllPassingTrain']:.0%}, "
                  f"test {res['shareAllPassingTest']:.0%}.", "", "## Top variants chosen on training data", "",
              "| Variant | Train PF (Nifty / Bank) | Test PF (Nifty / Bank) | Test return (Nifty / Bank) | Test trades |",
              "|---|---|---|---|---|"]
    for r in res["chosen"]:
        ms = list(r["train"])
        lines.append(f"| {r['label']} | " + " / ".join(str(r["train"][m]["pf"]) for m in ms) + " | "
                     + " / ".join(str(r["test"][m]["pf"]) for m in ms) + " | "
                     + " / ".join(f"{r['test'][m]['totalPct']}%" for m in ms) + " | "
                     + " / ".join(str(r["test"][m]["trades"]) for m in ms) + " |")
    lines += ["", f"**Survivors (profitable in training and test, both indices): {len(res['survivors'])}**", ""]
    path.parent.mkdir(exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    import upstox_data
    end = dt.date.today()
    data = {}
    for m, key in (("nifty50", "NSE_INDEX|Nifty 50"), ("banknifty", "NSE_INDEX|Nifty Bank")):
        data[m] = upstox_data.candles(key, "minutes", 15, dt.date(2022, 1, 3), end)
        print(m, len(data[m]), "candles", flush=True)
    res = research(data)
    (ROOT / "docs" / "data" / "research.json").write_text(json.dumps(res))
    write_report(res, ROOT / "research" / "RESULTS.md")
    print((ROOT / "research" / "RESULTS.md").read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
