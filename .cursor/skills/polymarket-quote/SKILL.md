---
name: polymarket-quote
description: >-
  Consumes trusted Dongqiudi basketball bridge events, settles Polymarket
  basketball markets, quotes CLOB books, and optionally trades settled WIN
  tokens in-process.
---

# Polymarket Basketball Quote

`polymarket-quote` receives enriched basketball events from
[`match-bridge`](../match-bridge/SKILL.md). Dongqiudi (DQD) score and player-stat
events are authoritative immediately: there is no API-Football confirmation or
external score gate.

System Main hub: http://127.0.0.1:8890/

## Signals

- `score_change`: on score increases, quote/trade totals that are already over
  their line. Moneyline is not traded mid-game by default
  (`QUOTE_ML_MIDGAME=0`). A score decrease or `is_reversal=true` flattens open
  lots for that match.
- `player_stat_change`: supported points, assists, rebounds, steals, blocks,
  and threes props settle Over only once `curr > line`. Under is left open
  until `match_finished`.
- `match_finished`: settles two-way moneyline, all remaining player props,
  totals, and spreads.

All trade work for a `match_id` is serialized by a small in-process lock.

## Quick start

```bash
python3 frontend/run_main.py
python3 frontend/run_main.py --score-mode dry --prop-mode live --ft-mode live
python3 frontend/run_main.py --live --max-usdc 2
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --no-trade --json
```

## Trade modes

Default is dry-run planning. `--live` makes all channels live; individual
channel flags override it:

| Flag / environment | Event |
|---|---|
| `--score-mode dry\|live`, `QUOTE_SCORE_MODE` | `score_change` |
| `--prop-mode dry\|live`, `QUOTE_PROP_MODE` | `player_stat_change` |
| `--ft-mode dry\|live`, `QUOTE_FT_MODE` | `match_finished` |

`QUOTE_GOALS_MODE` remains a legacy alias for `QUOTE_SCORE_MODE`.

`--no-trade` still quotes without invoking the executor. The executor retains
`trade_executor`, `clob_trader`, `fill_planner`, `trade_settings`,
`market_cache`, `size_policy`, `score_reversal`, `post_goal_sampler`, and
`data_prune`; post-score samples are research-only and never make extra trades.

## Data

- Source events: `data/bridge/events.jsonl`
- Matched fixtures / handles: `data/bridge/matches.json`
- Quotes: `data/pm-quote/quotes.jsonl`
- Trade attempts and flattening: `data/pm-quote/trades.jsonl`
- Open lots: `data/pm-quote/open_positions.json`

Install live-trading dependencies once:

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt
```

Use `PRIVATE_KEY`, `FUNDER`, `SIGNATURE_TYPE`, `CHAIN_ID`, and `CLOB_HOST` from
the repository `.env`; never commit private keys.
