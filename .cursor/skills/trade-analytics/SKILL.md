---
name: trade-analytics
description: >-
  Analyzes historical Polymarket quote trades from trades.jsonl and
  open_positions.json: windowed counts, dry-run/live buy_win estimated PnL,
  flatten pairing, skip reasons, and open lots. Use when the user asks about
  overnight results, trade history, profit estimates, flatten stats, or
  post-session trade analytics.
---

# Trade Analytics

独立 Skill：只读分析 **polymarket-quote** 落盘，不拉起 watch / 不下单。

## Inputs

| Path | Purpose |
|---|---|
| `data/pm-quote/trades.jsonl` | dry-run / live / flatten / skipped attempts |
| `data/pm-quote/open_positions.json` | buy_win lots still open (or pending flatten) |

## Quick start

```bash
# Full ledger summary
python3 .cursor/skills/trade-analytics/scripts/trade_analytics.py summary

# Overnight / rolling window
python3 .cursor/skills/trade-analytics/scripts/trade_analytics.py summary --last-hours 12
python3 .cursor/skills/trade-analytics/scripts/trade_analytics.py summary \
  --since 2026-07-22T23:20:00+08:00 --json

# List fills
python3 .cursor/skills/trade-analytics/scripts/trade_analytics.py list --trade buy_win --last-hours 24
python3 .cursor/skills/trade-analytics/scripts/trade_analytics.py list --status flatten_dry_run

# Open lots
python3 .cursor/skills/trade-analytics/scripts/trade_analytics.py opens --status open
```

Report snapshot（默认写出）：`data/trade-analytics/latest.json`

## Agent workflow

1. Prefer this skill (not ad-hoc Python) when the user asks 昨晚结果 / 利润 / 历史成交 / flatten 统计.
2. Run `summary` with an explicit window (`--last-hours` / `--since`) when discussing a session.
3. Treat `buy_win_* est_profit` as **redeem@$1 estimates** for dry-run/posted buys — not banked live PnL unless `live=true` and status is posted/filled.
4. `buy_win_kept_after_flatten` subtracts buys later paired to a `flatten_reversal` on the same match (and family when present).
5. Use `list` / `opens` for drill-down; cite `quoted_at`, match, family/outcome, usdc, est.

## PnL notes

- **buy_win**: `shares * (1 - fill_price)` from plan levels / worst_price.
- **sell_lose** skips (`no_position`) are expected in dry-run with no inventory.
- **flatten_reversal**: emergency exit; dry-run often logs `usdc=0` / empty book — do not treat as realized sell proceeds unless live fill data is present.

## Related skills

- [`polymarket-quote`](../polymarket-quote/SKILL.md) — produces `trades.jsonl` / open ledger
- [`match-bridge`](../match-bridge/SKILL.md) — score_change / reversal triggers upstream

## Details

See [reference.md](reference.md).
