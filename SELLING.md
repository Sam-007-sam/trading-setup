# Selling Pre-Market Desk: what has to be true first

This is a working checklist, not legal advice. Confirm each point with a SEBI-compliance lawyer.

## 1. Information-only mode

Set `docs/data/config.json` to `{"infoOnly": true}`. The dashboard then:

- shows a factual "Overnight summary" instead of the bullish/bearish bias gauge,
- hides "Nifty levels in focus" and the bullish/bearish tags on the Indicators tab,
- renames the pivot ladder to "Pivot levels (calculated)",
- hides the Paper trading tab and "currently long/short" in backtests,
- tells the AI summary to report facts only: no views, targets, stops or expected direction.

Without SEBI Research Analyst (RA) or Investment Adviser registration, paid content must not
include buy/sell/hold calls or recommendations. Have a lawyer review the pivot ladder and the
backtest pages too.

## 2. Licensed market data

Yahoo Finance, NSE's website and broker APIs (Upstox, Kite) are licensed for personal use.
A paid product needs data you are allowed to redistribute: an NSE-authorised data vendor or a
written agreement. Swap the fetch functions in `fetch.py`, `backtest.py`, `intraday.py` and
`upstox_data.py` to that source before charging anyone.

## 3. Automated trading for others

Under SEBI's retail algo framework (mandatory for brokers from 1 April 2026), algo providers
must be empanelled with the exchanges and work through a broker; strategies with hidden logic
need RA registration. Selling auto-trading means a broker partnership, not this repo as is.

## 4. Product build (when 1 to 3 are settled)

- Web app with logins and payments (e.g. Razorpay) in place of GitHub Pages.
- One brief generated per day and shared with all subscribers; per-user watchlists and alerts.
- Telegram or WhatsApp delivery by 8:45 AM; mobile-first layout.
- A 10 to 20 user free beta before setting prices.
