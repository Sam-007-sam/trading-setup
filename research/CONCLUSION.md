# Conclusion of intraday research, 18 Nov 2026

Protocol fixed before results: 15-minute Upstox data for Nifty 50 and Nifty Bank, Jan 2022 to
Nov 2026. Variants chosen on 2022-2024 (739 sessions), judged once on 2025-2026 (464 sessions),
realistic futures costs (0.012% per side), must be profitable on both indices.

| Round | Family | Variants | Profitable on both indices, unseen period |
|---|---|---|---|
| 1 | Opening-range breakout (range, direction filter, target, trail, timing, stop, gap skip) | 512 | 0 |
| 2 | Gap fade | 24 | 0 |
| 2 | Failed-breakout fade | 16 | 0 |

- Best training results (profit factor ~1.17 on both indices) all lost money on the unseen years.
- Across all 552 variants, the best weaker-index test profit factor was 0.978; the median 0.81.
- On random data the same pipeline also finds training "winners" at 1.2-1.37 that fail out of
  sample, so a good-looking backtest alone is not evidence.

Decision: research stops here, as the protocol said. No rule earns live trading. The Trade desk
stays paper-only (its live gate needs a profit factor above 1 over 200+ tested trades), and its
current rule remains for paper use and as a benchmark. Re-testing these families on the same
test years would be data mining; any new idea needs new data (from 2027) to be judged.
