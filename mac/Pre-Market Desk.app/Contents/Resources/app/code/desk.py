"""
Trade desk engine: the platform's live trading flow for ONE user (you), on your own machine.

  1. Morning questions -> today's limits (risk.py).
  2. During market hours the desk watches Nifty and Bank Nifty with the same rule the intraday
     backtest tests every day (intraday.s_orb_plus) and suggests a trade: index, buy or sell,
     entry, stop-loss, target and quantity sized to the user's limit.
  3. The user confirms. The desk enters, places the stop-loss at once, moves it to break-even
     at +1R, books the target at +2R, exits everything at 3:15 PM, and locks the day when the
     daily loss limit is reached. "Exit now" is always available.

Paper mode is the default: real prices, simulated fills. Live mode needs LIVE_TRADING set to
broker.CONFIRM and UPSTOX_ACCESS_TOKEN, and every entry still needs the user's confirm click.
Live order handling has NOT been tested against a real account yet.

Stop-losses are managed on the index level (for options too), checked on every tick; in live
mode a stop order also rests at the exchange so it works if this app is closed.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
import threading
import uuid

import intraday as X
import risk as R

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
DATA = pathlib.Path(os.environ.get("PMD_DATA") or (pathlib.Path(__file__).resolve().parent / "docs" / "data"))
MARKETS = {"nifty50": ("Nifty 50", "^NSEI", "NSE_INDEX|Nifty 50", "NIFTY"),
           "banknifty": ("Nifty Bank", "^NSEBANK", "NSE_INDEX|Nifty Bank", "BANKNIFTY")}
OPEN, LAST_ENTRY, SQUARE_OFF, CLOSE = "09:15", "13:00", "15:15", "15:30"
CANDLE_MIN = 15
MAX_ENTRY_DRIFT_R = 0.5      # if price has run more than this since the signal, the setup is stale


def charges_estimate(turnover: float, instrument: str) -> float:
    """Round-trip charges, approximately: ₹20 brokerage per order plus GST, and per-turnover
    STT (futures 0.02% on the sell side; options 0.1% of sell premium), exchange fees and stamp duty."""
    if instrument == "option":
        return 47 + 0.0012 * turnover        # turnover = buy + sell premium value
    return 47 + 0.00013 * turnover           # turnover = buy + sell contract value


MIN_TESTED_TRADES = 200


def tested_record() -> dict:
    """The desk rule's latest backtest (intraday.json), pooled over both indices."""
    try:
        r = json.loads((DATA / "intraday.json").read_text())
        stats = [s["stats"] for m in r["markets"] for s in m["strategies"] if s["id"] == "orb_plus"]
        trades = sum(x["trades"] for x in stats)
        pf = min((x["profitFactor"] or 0) for x in stats) if stats else 0
        return {"trades": trades, "profitFactor": pf, "ok": trades >= MIN_TESTED_TRADES and pf > 1}
    except Exception:
        return {"trades": 0, "profitFactor": None, "ok": False}


def live_enabled() -> bool:
    import broker
    return os.environ.get("LIVE_TRADING") == broker.CONFIRM and bool(os.environ.get("UPSTOX_ACCESS_TOKEN"))


# ---------------------------------------------------------------- market data
class Feed:
    """Today's 15-minute candles, the latest price, the daily trend and VIX.
    Upstox first (public candles), Yahoo Finance as a fallback."""

    def __init__(self):
        self._daily: dict = {}

    def candles(self, market: str, today: dt.date) -> list[dict]:
        _, yf_t, up_key, _ = MARKETS[market]
        try:
            import urllib.parse
            import requests
            url = ("https://api.upstox.com/v3/historical-candle/intraday/"
                   f"{urllib.parse.quote(up_key, safe='')}/minutes/{CANDLE_MIN}")
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            rows = r.json()["data"]["candles"]
            out = []
            for row in rows:
                ts = dt.datetime.fromisoformat(str(row[0]))
                if ts.date() == today:
                    out.append({"t": ts.strftime("%H:%M"), "o": row[1], "h": row[2], "l": row[3], "c": row[4]})
            if out:
                return sorted(out, key=lambda x: x["t"])
        except Exception:
            pass
        import yfinance as yf
        df = yf.Ticker(yf_t).history(period="1d", interval=f"{CANDLE_MIN}m")
        df.index = df.index.tz_convert("Asia/Kolkata")
        return [{"t": ix.strftime("%H:%M"), "o": float(r.Open), "h": float(r.High), "l": float(r.Low),
                 "c": float(r.Close)} for ix, r in df.iterrows() if ix.date() == today]

    def price(self, market: str, today: dt.date) -> float:
        _, yf_t, up_key, _ = MARKETS[market]
        try:
            import urllib.parse
            import requests
            url = ("https://api.upstox.com/v3/historical-candle/intraday/"
                   f"{urllib.parse.quote(up_key, safe='')}/minutes/1")
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return float(r.json()["data"]["candles"][0][4])      # newest first
        except Exception:
            import yfinance as yf
            return float(yf.Ticker(yf_t).history(period="1d", interval="1m")["Close"].iloc[-1])

    def daily(self, market: str, today: dt.date) -> dict:
        """Trend from completed daily closes (before today) and India VIX, cached for the day."""
        key = (market, today)
        if key not in self._daily:
            import yfinance as yf
            closes = [float(v) for ix, v in yf.Ticker(MARKETS[market][1]).history(period="3mo")["Close"].items()
                      if ix.date() < today]
            vix = float(yf.Ticker("^INDIAVIX").history(period="5d")["Close"].iloc[-1])
            self._daily[key] = {"trend": X.daily_trend(closes), "vix": vix, "prevClose": closes[-1]}
        return self._daily[key]


def days_to_expiry(market: str, today: dt.date) -> float:
    """Nifty: weekly expiry on Tuesday. Bank Nifty: monthly, last Tuesday. (Live mode uses the
    broker's contract list instead.) Returns trading-time-ish days, at least half a day."""
    def next_tue(d):
        return d + dt.timedelta(days=(1 - d.weekday()) % 7)
    if market == "nifty50":
        e = next_tue(today)
    else:
        y, m = today.year, today.month
        while True:
            last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
            e = last - dt.timedelta(days=(last.weekday() - 1) % 7)
            if e >= today:
                break
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return max(0.5, (e - today).days + 0.5)


# ---------------------------------------------------------------- brokers
class PaperBroker:
    mode = "paper"

    def prepare(self, pos):
        pass

    def fill(self, side, qty, price, what):
        return {"price": price, "id": "paper-" + uuid.uuid4().hex[:8]}

    def place_stop(self, pos):
        return "paper-stop"

    def move_stop(self, pos, new_stop):
        pass

    def cancel_stop(self, pos):
        pass

    def option_premium(self, pos, spot, now):
        return R.bs_premium(spot, pos["option"]["strike"], pos["option"]["daysToExpiry"]
                            - (now - dt.datetime.fromisoformat(pos["entryTime"])).total_seconds() / 86400,
                            pos["option"]["vix"], pos["option"]["kind"])


class UpstoxBroker:
    """Real orders through broker.py. NOT TESTED against a live account."""
    mode = "live"

    def __init__(self):
        import broker
        self.b = broker

    def prepare(self, pos):
        self.b.prepare(pos)

    def fill(self, side, qty, price, what):
        return self.b.place_and_wait(what["instrument_key"], "BUY" if side > 0 else "SELL", qty, price)

    def place_stop(self, pos):
        return self.b.place_stop(pos)

    def move_stop(self, pos, new_stop):
        self.b.modify_stop(pos, new_stop)

    def cancel_stop(self, pos):
        self.b.cancel_order(pos.get("stopOrderId"))

    def option_premium(self, pos, spot, now):
        return self.b.ltp(pos["option"]["instrument_key"])


# ---------------------------------------------------------------- the desk
class Desk:
    def __init__(self, feed=None, clock=None, broker=None, subdir="desk"):
        self.subdir = subdir
        self.feed = feed or Feed()
        self.clock = clock or (lambda: dt.datetime.now(IST))
        self.broker = broker
        self.lock = threading.RLock()
        self.state = None

    # ---- state
    def _path(self, day):
        d = DATA / self.subdir
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{day.isoformat()}.json"

    def load(self):
        today = self.clock().date()
        if self.state and self.state["date"] == today.isoformat():
            return self.state
        p = self._path(today)
        self.state = json.loads(p.read_text()) if p.exists() else {
            "date": today.isoformat(), "profile": None, "suggestions": {}, "position": None,
            "trades": [], "realized": 0.0, "locked": None, "log": []}
        return self.state

    def save(self):
        self._path(dt.date.fromisoformat(self.state["date"])).write_text(json.dumps(self.state, indent=1))

    def log(self, msg):
        self.state["log"].append({"t": self.clock().strftime("%H:%M:%S"), "msg": msg})

    def _broker(self):
        if self.broker:
            return self.broker
        return UpstoxBroker() if self.state["profile"] and self.state["profile"]["mode"] == "live" else PaperBroker()

    # ---- 1. morning questions
    def start(self, answers: dict) -> dict:
        with self.lock:
            s = self.load()
            if s["trades"] or s["position"]:
                return {"error": "Today's session already has trades; limits can't be changed mid-day."}
            s["answers"] = answers
            s["profile"] = R.evaluate(answers, live_enabled())
            rec = tested_record()
            if s["profile"].get("mode") == "live" and not rec["ok"]:
                s["profile"]["mode"] = "paper"
                s["profile"]["notes"].append(
                    f"Live trading stays off: the rule hasn't shown a positive record in testing yet "
                    f"(needs a profit factor above 1 over at least {MIN_TESTED_TRADES} trades; latest: "
                    f"{rec['profitFactor']} over {rec['trades']} trades).")
            self.log("Session started: " + (f"{s['profile']['mode']} mode, max loss ₹{s['profile']['maxLoss']:,}, "
                                            f"{s['profile']['maxTrades']} trade(s), {s['profile']['instrument']}"
                                            if s["profile"]["allowed"] else s["profile"]["reason"]))
            self.save()
            return s

    # ---- 2. suggestions
    def _session_status(self, now):
        t = now.strftime("%H:%M")
        if now.weekday() >= 5:
            return "closed", "Markets are closed today."
        if t < OPEN:
            return "pre", "Market opens at 9:15. The first suggestion can come after the 9:30 candle closes."
        if t >= CLOSE:
            return "closed", "Market has closed for the day."
        return "open", ""

    def budget_left(self):
        s = self.state
        p = s["profile"]
        return p["maxLoss"] + min(0.0, s["realized"])

    def suggest(self) -> dict:
        with self.lock:
            s = self.load()
            now = self.clock()
            p = s["profile"]
            if not p or not p.get("allowed"):
                return {"status": "no-session", "message": "Answer today's questions first."}
            phase, msg = self._session_status(now)
            if phase != "open":
                return {"status": phase, "message": msg}
            if s["locked"]:
                return {"status": "locked", "message": s["locked"]}
            if s["position"]:
                return {"status": "in-trade", "message": "Managing your open trade."}
            if len(s["trades"]) >= p["maxTrades"]:
                return {"status": "done", "message": f"You've taken your {p['maxTrades']} trade(s) for today."}
            if now.strftime("%H:%M") >= LAST_ENTRY:
                return {"status": "done", "message": "No new trades after 1 PM; the rule's edge fades late in the day."}
            out = []
            for m in MARKETS:
                out.append(self._check_market(m, now))
            live = [x for x in out if x.get("signal")]
            seen = s.setdefault("signalsSeen", [])
            for x in live:                      # audit trail: every distinct signal, once
                if x["id"] not in seen:
                    seen.append(x["id"])
                    z = x.get("size") or {}
                    self.log(f"Signal {x['name']} {'BUY' if x['side'] > 0 else 'SELL'} on the {x['candle']} candle: "
                             f"entry ~{x['entry']:,.2f}, stop {x['stop']:,.2f} ({x['riskPoints']:,.0f} pts), "
                             f"target {x['target']:,.2f}. "
                             + (f"Suggested: {z['qty']} qty, ₹{z['riskAtStop']:,} at risk." if z.get("ok")
                                else f"Not suggested: {x.get('message') or z.get('reason')}"))
            for x in out:                       # signals that fired but were skipped (price ran away etc.)
                if not x.get("signal") and x.get("message", "").startswith("Signal fired"):
                    key = f"{x['market']}-skip-{now.strftime('%H:%M')[:4]}"
                    if key not in seen:
                        seen.append(key)
                        self.log(f"{x['name']}: {x['message']}")
            s["suggestions"] = {x["id"]: x for x in live}
            self.save()
            return {"status": "ok", "markets": out}

    def _check_market(self, market, now):
        name = MARKETS[market][0]
        today = now.date()
        info = self.feed.daily(market, today)
        bars = self.feed.candles(market, today)
        # only candles that have closed
        done = [b for b in bars
                if (dt.datetime.combine(today, dt.time.fromisoformat(b["t"]), IST)
                    + dt.timedelta(minutes=CANDLE_MIN)) <= now]
        res = {"market": market, "name": name, "trend": info["trend"]}
        if not done:
            res["message"] = "Waiting for the 9:15 candle to close."
            return res
        hi, lo = done[0]["h"], done[0]["l"]
        res.update(rangeHigh=hi, rangeLow=lo)
        taken = sum(1 for t in self.state["trades"] if t["market"] == market)
        # Like the backtest, only the FIRST breakout candle of the day counts; later candles that
        # close beyond the range would mean a worse entry and a bigger stop than the tested rule.
        first = next((i for i in range(1, len(done))
                      if X.s_orb_plus(i, done[:i + 1], {"n": 0, "pos": 0}, {"trend": info["trend"]})), None)
        if first is not None and first != len(done) - 1:
            res["message"] = (f"The breakout came on the {done[first]['t']} candle and that setup has passed; "
                              "the tested rule takes only the first breakout of the day.")
            return res
        sig = X.s_orb_plus(len(done) - 1, done, {"n": taken, "pos": 0}, {"trend": info["trend"]})
        width = (hi - lo) / done[-1]["c"] * 100
        if not info["trend"]:
            res["message"] = "No clear daily trend: no trade."
        elif width > X.ORB_SKIP_RANGE_PCT:
            res["message"] = f"Opening range is {width:.2f}% wide, too wide to trade safely today."
        elif taken:
            res["message"] = "Already traded this index today."
        elif not sig:
            lvl = hi if info["trend"] > 0 else lo
            res["message"] = (f"Watching: {'buy' if info['trend'] > 0 else 'sell'} if a 15-minute candle closes "
                              f"{'above' if info['trend'] > 0 else 'below'} {lvl:,.2f} (trend is "
                              f"{'up' if info['trend'] > 0 else 'down'}).")
        if not sig:
            return res
        price = self.feed.price(market, today)
        side, stop = sig["side"], sig["stop"]
        risk_pts = abs(price - stop)
        planned = abs(done[-1]["c"] - stop)
        if side * (price - stop) <= 0:
            res["message"] = "Signal fired but price is already past the stop: skipped."
            return res
        if side * (price - done[-1]["c"]) > MAX_ENTRY_DRIFT_R * planned:
            res["message"] = "Signal fired but price has already run away from the entry: skipped."
            return res
        target = price + side * 2 * risk_pts
        p = self.state["profile"]
        budget = min(p["perTradeRisk"], self.budget_left())
        if p["instrument"] == "futures":
            size = R.size_futures(market, price, stop, budget, p["capital"])
        else:
            size = R.size_option(market, price, stop, side, budget, p["capital"],
                                 days_to_expiry(market, today), info["vix"])
            if size.get("ok"):
                size["daysToExpiry"] = days_to_expiry(market, today)
                size["vix"] = info["vix"]
        res.update(signal=True, side=side, entry=round(price, 2), stop=round(stop, 2), target=round(target, 2),
                   riskPoints=round(risk_pts, 2), size=size, candle=done[-1]["t"],
                   id=f"{market}-{done[-1]['t']}", validUntil=(dt.datetime.combine(
                       today, dt.time.fromisoformat(done[-1]["t"]), IST) + dt.timedelta(minutes=2 * CANDLE_MIN)
                   ).strftime("%H:%M"))
        if not size.get("ok"):
            res["message"] = size["reason"]
        return res

    # ---- 3. confirm and manage
    def confirm(self, sid: str) -> dict:
        with self.lock:
            s = self.load()
            now = self.clock()
            sug = s["suggestions"].get(sid)
            if not sug:
                return {"error": "That suggestion is no longer available. Refresh to see the current one."}
            if now.strftime("%H:%M") >= sug["validUntil"]:
                return {"error": "That suggestion has expired. Refresh to see the current one."}
            if s["position"] or s["locked"] or len(s["trades"]) >= s["profile"]["maxTrades"]:
                return {"error": "Can't open a trade now (one is open, the day is locked, or the trade limit is used)."}
            size = sug["size"]
            if not size.get("ok"):
                return {"error": size["reason"]}
            m, side = sug["market"], sug["side"]
            price = self.feed.price(m, now.date())
            if side * (price - sug["stop"]) <= 0 or abs(price - sug["entry"]) > MAX_ENTRY_DRIFT_R * sug["riskPoints"]:
                return {"error": "Price has moved too far since the suggestion. Refresh for a new plan."}
            b = self._broker()
            pos = {"id": uuid.uuid4().hex[:8], "market": m, "name": sug["name"], "side": side, "qty": size["qty"],
                   "instrument": size["instrument"], "entryIndex": price, "stop": sug["stop"],
                   "initialStop": sug["stop"], "target": sug["target"], "risk": abs(price - sug["stop"]),
                   "trailed": False, "entryTime": now.isoformat(), "mode": b.mode}
            if size["instrument"] == "option":
                pos["option"] = {k: size[k] for k in ("kind", "strike", "daysToExpiry", "vix", "delta")}
            try:
                b.prepare(pos)
            except Exception as e:
                return {"error": f"Couldn't set up the order: {e}"}
            if size["instrument"] == "option":
                prem = b.option_premium(pos, price, now)
                fill = b.fill(1, size["qty"], prem, pos["option"])          # options are always bought
                pos["entryPrice"] = fill["price"]
            else:
                fill = b.fill(side, size["qty"], price, pos)
                pos["entryPrice"] = fill["price"]
            pos["entryOrderId"] = fill["id"]
            try:
                pos["stopOrderId"] = b.place_stop(pos)
            except Exception as e:                       # never hold a live position without a resting stop
                s["position"] = pos
                self._exit(f"stop order failed ({e}); exited for safety", price, now)
                self.save()
                return {"error": f"Entered, but the stop-loss order failed, so the trade was closed at once: {e}"}
            s["position"] = pos
            s["suggestions"] = {}
            what = (f"{pos['option']['strike']} {pos['option']['kind']} @ ₹{pos['entryPrice']:,.2f}"
                    if size["instrument"] == "option" else f"futures @ {pos['entryPrice']:,.2f}")
            self.log(f"Entered {'long' if side > 0 else 'short'} {sug['name']}: {size['qty']} {what}. "
                     f"Stop {pos['stop']:,.2f}, target {pos['target']:,.2f} (index levels).")
            self.save()
            return {"ok": True, "position": pos}

    def _pnl(self, pos, spot, now, exit_price=None):
        if pos["instrument"] == "option":
            px = exit_price if exit_price is not None else self._broker().option_premium(pos, spot, now)
            gross = (px - pos["entryPrice"]) * pos["qty"]
            turnover = (px + pos["entryPrice"]) * pos["qty"]
        else:
            px = exit_price if exit_price is not None else spot
            gross = pos["side"] * (px - pos["entryPrice"]) * pos["qty"]
            turnover = (px + pos["entryPrice"]) * pos["qty"]
        return px, gross - charges_estimate(turnover, pos["instrument"])

    def _exit(self, why, spot, now):
        pos = self.state["position"]
        b = self._broker()
        b.cancel_stop(pos)
        if pos["instrument"] == "option":
            px = b.option_premium(pos, spot, now)
            fill = b.fill(-1, pos["qty"], px, pos["option"])
        else:
            fill = b.fill(-pos["side"], pos["qty"], spot, pos)
        px, net = self._pnl(pos, spot, now, fill["price"])
        trade = {**pos, "exitIndex": spot, "exitPrice": fill["price"], "exitTime": now.isoformat(),
                 "why": why, "pnl": round(net, 2)}
        self.state["trades"].append(trade)
        self.state["position"] = None
        self.state["realized"] = round(self.state["realized"] + net, 2)
        self.log(f"Exited {pos['name']} ({why}) at index {spot:,.2f}: {'+' if net >= 0 else '−'}₹{abs(net):,.0f} "
                 f"after estimated charges. Day so far: ₹{self.state['realized']:,.0f}.")
        if self.budget_left() <= 0:
            self.state["locked"] = f"Daily loss limit reached (₹{self.state['realized']:,.0f}). Trading is locked until tomorrow."
            self.log(self.state["locked"])

    def exit_now(self) -> dict:
        with self.lock:
            s = self.load()
            if not s["position"]:
                return {"error": "No open trade."}
            now = self.clock()
            self._exit("exited by you", self.feed.price(s["position"]["market"], now.date()), now)
            self.save()
            return {"ok": True}

    def tick(self) -> None:
        """Called every few seconds by the server: manages the open trade."""
        with self.lock:
            s = self.load()
            pos = s["position"]
            if not pos:
                return
            now = self.clock()
            spot = self.feed.price(pos["market"], now.date())
            side = pos["side"]
            if now.strftime("%H:%M") >= SQUARE_OFF:
                self._exit("3:15 PM square-off", spot, now)
            elif side * (spot - pos["stop"]) <= 0:
                self._exit("break-even stop" if pos["trailed"] else "stop-loss", spot, now)
            elif side * (spot - pos["target"]) >= 0:
                self._exit("target", spot, now)
            else:
                if not pos["trailed"] and side * (spot - pos["entryIndex"]) >= pos["risk"]:
                    pos["stop"], pos["trailed"] = pos["entryIndex"], True
                    self._broker().move_stop(pos, pos["entryIndex"])
                    self.log(f"Up 1R: stop moved to break-even ({pos['entryIndex']:,.2f}). This trade can no longer lose "
                             "beyond charges and slippage.")
                _, unreal = self._pnl(pos, spot, now)
                pos["lastIndex"], pos["unrealized"] = spot, round(unreal, 2)
                if s["realized"] + unreal <= -s["profile"]["maxLoss"]:
                    self._exit("daily loss limit", spot, now)
            self.save()

    def view(self) -> dict:
        with self.lock:
            s = self.load()
            return {**s, "budgetLeft": round(self.budget_left()) if s["profile"] and s["profile"].get("allowed") else None,
                    "now": self.clock().isoformat(timespec="seconds"), "questions": R.QUESTIONS,
                    "quiz": [{k: q[k] for k in ("id", "q", "options")} for q in R.QUIZ], "liveEnabled": live_enabled()}
