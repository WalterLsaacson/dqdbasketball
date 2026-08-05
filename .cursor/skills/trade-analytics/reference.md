# Trade Analytics — Reference

## CLI

```
trade_analytics.py summary [--since ISO] [--until ISO] [--last-hours N] [--last-days N]
                           [--trades PATH] [--opens PATH] [--json] [--no-persist]

trade_analytics.py list    [window flags] [--trade KIND] [--status S] [--family F]
                           [--match-id ID] [--limit N] [--json]

trade_analytics.py opens   [--opens PATH] [--status S] [--json]
```

Times are parsed as ISO-8601; naive timestamps are treated as Asia/Shanghai (+08:00).

## Report shape (`summary --json`)

| Field | Meaning |
|---|---|
| `counts.by_status` | dry_run / skipped / flatten_* / … |
| `counts.by_trade` | buy_win / sell_lose / flatten_reversal |
| `counts.skip_reasons` | e.g. `no_position` |
| `pnl.buy_win_all` | all successful buy_win attempts in window |
| `pnl.buy_win_kept_after_flatten` | buys not later paired to a flatten |
| `pnl.buy_win_later_flattened` | buys whose match later flattened |
| `by_match` | per fixture aggregated usdc / est_profit |
| `open_positions` | ledger snapshot (not window-filtered) |
| `samples` | last compact rows for buys / flattens / skips |

## Trade row fields used

From `trades.jsonl`: `quoted_at`, `status`, `trade`, `live`, `home`/`away`, scores, `family`, `outcome`, `plan.shares` / `plan.usdc` / `plan.levels`, `skip_reason`, `match_id`, `net_edge`.

## Flatten pairing heuristic

A `buy_win` is marked “later flattened” if a `flatten_reversal` (or flatten_* status) exists for the same `match_id` with `quoted_at >= buy.quoted_at`, and matching `family` when both rows have it. This is session analytics, not a perfect lot-id join.

## Output dir

`data/trade-analytics/latest.json` — overwritten on each `summary` unless `--no-persist`.
