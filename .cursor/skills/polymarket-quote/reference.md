# Polymarket Basketball Quote Reference

## Pipeline

1. `match-bridge` delivers trusted DQD `score_change`, `player_stat_change`, or
   `match_finished` events through the memory queue and `data/bridge/events.jsonl`.
2. Quote joins the current bridge fixture for Polymarket event and player-prop
   handles, using `market_cache` when warm.
3. A per-`match_id` re-entrant lock serializes settlement, order planning, and
   reversal flattening.
4. CLOB books are fetched in a batch and only settled `WIN` tokens can be
   purchased.

There is no API-Football dependency, referee, confirmation polling, or
full-time score gate. Hub port is `8890`.

## Settlement

| Event | Eligible settlement |
|---|---|
| `score_change` increase | Already-cleared match/team totals; moneyline stays disabled mid-game by default |
| `score_change` decrease / reversal | Flatten affected open lots; do not open a replacement lot |
| `player_stat_change` | Player prop Over only when `curr > line`; Under waits for FT |
| `match_finished` | Two-way home/away moneyline, totals, spreads, and player props |

Supported player stats are points, assists, rebounds, steals, blocks, and
threes. Player props are matched by normalized player name, stat type, and
Over/Under line from Polymarket handles.

## Trade control

`--live` enables live CLOB orders for all channels. Independent controls:

- `--score-mode` / `QUOTE_SCORE_MODE`
- `--prop-mode` / `QUOTE_PROP_MODE`
- `--ft-mode` / `QUOTE_FT_MODE`

Each accepts `dry` or `live`. `QUOTE_GOALS_MODE` is accepted only as a legacy
alias for `QUOTE_SCORE_MODE`.

## Retention and safety

`data_prune.py` maintains a rolling 24-hour window. `score_reversal.py` and
`trade_executor.py` retain open-lot flatten behavior. Dry-run lots are never
sold live. `sell_lose` remains disabled: only fee-aware `buy_win` trades are
planned or posted.
