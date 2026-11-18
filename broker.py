"""
Upstox broker connection: account checks and order tickets. NOT TESTED AGAINST A LIVE ACCOUNT YET.

  python broker.py check                       read-only: profile, funds, open positions
  python broker.py ticket nifty50 ema_cross    the futures order the paper strategy implies (prints only)
  python broker.py ticket nifty50 ema_cross --live
                                               sends it, after you type the exact confirmation

Safety rules built in:
  * Nothing is ever sent without --live, LIVE_TRADING=yes-i-accept-the-risk in the environment,
    AND typing the confirmation phrase for each order. There is no unattended mode.
  * One lot per order, LIMIT orders only (SEBI rules restrict market orders over the API).
  * Since 1 April 2026, API orders must come from the static IP registered on your Upstox app,
    so live orders cannot run from GitHub Actions. Run this on a machine with that IP.

Needs UPSTOX_ACCESS_TOKEN (a daily token from your Upstox developer app's login flow).
"""
from __future__ import annotations

import gzip
import io
import json
import os
import pathlib
import sys

import requests

API = "https://api.upstox.com/v2"
ORDER_API = "https://api-hft.upstox.com/v2/order/place"
INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CONFIRM = "yes-i-accept-the-risk"
DATA = pathlib.Path(os.environ.get("PMD_DATA") or (pathlib.Path(__file__).resolve().parent / "docs" / "data"))
UNDERLYING = {"nifty50": "NIFTY", "banknifty": "BANKNIFTY"}


def _h() -> dict:
    tok = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not tok:
        sys.exit("Set UPSTOX_ACCESS_TOKEN first (see README, 'Connecting Upstox').")
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json", "Content-Type": "application/json"}


def get(path: str) -> dict:
    r = requests.get(API + path, headers=_h(), timeout=20)
    if r.status_code == 401:
        sys.exit("Upstox says the token is invalid or expired. Tokens last one day; log in again.")
    r.raise_for_status()
    return r.json().get("data")


def check() -> None:
    p = get("/user/profile")
    print(f"Connected as {p.get('user_name')} ({p.get('user_id')}), broker {p.get('broker')}")
    f = (get("/user/get-funds-and-margin?segment=SEC") or {}).get("equity", {})
    print(f"Available margin: ₹{f.get('available_margin', 0):,.2f}, used: ₹{f.get('used_margin', 0):,.2f}")
    pos = get("/portfolio/short-term-positions") or []
    open_pos = [x for x in pos if x.get("quantity")]
    print(f"Open positions: {len(open_pos)}")
    for x in open_pos:
        print(f"  {x.get('trading_symbol')}: {x.get('quantity')} @ {x.get('average_price')}, P&L {x.get('pnl')}")


def near_future(underlying: str) -> dict:
    """The nearest-expiry index future from Upstox's instruments file."""
    r = requests.get(INSTRUMENTS, timeout=60)
    r.raise_for_status()
    rows = json.load(gzip.open(io.BytesIO(r.content)))
    futs = [x for x in rows if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT"
            and x.get("underlying_symbol") == underlying]
    if not futs:
        raise ValueError(f"no {underlying} future in the instruments file")
    return min(futs, key=lambda x: x["expiry"])


def ticket(market: str, strategy: str, live: bool) -> None:
    paper = json.loads((DATA / "paper.json").read_text())
    m = next((x for x in paper["markets"] if x["id"] == market), None)
    a = next((x for x in (m or {}).get("accounts", []) if x["id"] == strategy), None)
    if not a:
        sys.exit(f"No paper account {market}/{strategy}. Options: "
                 + ", ".join(f"{x['id']}/{y['id']}" for x in paper["markets"] for y in x["accounts"]))
    target, current = a["next"]["target"], a["position"]
    print(f"{a['name']} on {m['name']}: paper position {current:+d}, target {target:+d} "
          f"(from the {a['next']['basedOn']} close): {a['next']['action']}")
    if target == current:
        print("No order needed.")
        return
    fut = near_future(UNDERLYING[market])
    side = "BUY" if target > current else "SELL"
    lots = abs(target - current)                      # 1 to enter/exit, 2 to reverse
    q = requests.get(f"{API}/market-quote/ltp?instrument_key={requests.utils.quote(fut['instrument_key'])}",
                     headers=_h(), timeout=20)
    q.raise_for_status()
    ltp = next(iter(q.json()["data"].values()))["last_price"]
    limit = round(ltp * (1.002 if side == "BUY" else 0.998) / 0.05) * 0.05   # 0.2% through the market
    order = {"quantity": lots * int(fut["lot_size"]), "product": "D", "validity": "DAY", "price": round(limit, 2),
             "tag": f"pmd-{strategy}"[:20], "instrument_token": fut["instrument_key"], "order_type": "LIMIT",
             "transaction_type": side, "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False}
    print("Order ticket:", json.dumps({**order, "symbol": fut.get("trading_symbol"), "ltp": ltp}, indent=1))
    if not live:
        print("Dry run: nothing sent. Add --live to send it (see the safety rules at the top of broker.py).")
        return
    if os.environ.get("LIVE_TRADING") != CONFIRM:
        sys.exit(f"Refusing: LIVE_TRADING is not set to {CONFIRM!r}.")
    typed = input(f"Type '{CONFIRM}' to send this REAL order: ")
    if typed.strip() != CONFIRM:
        sys.exit("Not confirmed. Nothing sent.")
    r = requests.post(ORDER_API, headers=_h(), json=order, timeout=20)
    print(r.status_code, r.text)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["check"]:
        check()
    elif args[:1] == ["ticket"] and len(args) >= 3:
        ticket(args[1], args[2], "--live" in args)
    else:
        print(__doc__)
