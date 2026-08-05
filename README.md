# dqd basket

Dongqiudi ↔ Polymarket **basketball** pipeline: live scores + player box scores from 懂球帝, fixture pairing, CLOB quoting, and optional in-process trading.

Sibling of the soccer project [`dongqiudihook`](../dongqiudihook) — **no API-Football / secondary confirm**. DQD score and player-stat changes are treated as authoritative.

## What it does

```text
dongqiudi-basketball ──┐
                       ├── match-bridge (in-process · memory queue)
polymarket-basketball ─┘         │
                                 ▼
                   polymarket-quote watch
                         │
                         ├── settle moneyline / spreads / totals / player props
                         └── dry-run / live FAK
```

| Skill | Role |
|---|---|
| `dongqiudi-basketball` | DQD basketball list / scores / boxscore / score+player events |
| `polymarket-basketball` | Polymarket Gamma basketball fixtures + props |
| `match-bridge` | Align DQD ↔ PM; emit `score_change` / `player_stat_change` / `match_finished` |
| `polymarket-quote` | Settle markets from DQD events, quote CLOB, dry-run or live FAK |

**System Main** (`frontend/run_main.py`) is the **only** process you start. It boots:

- Hub :8890
- Boards (UI): Dongqiudi :8887 · Polymarket :8888 · Match Bridge :8889
- `pm_quote watch`, which **owns in-process match-bridge** (memory `event_queue` → quote/trade)

Ports are offset from soccer `dongqiudihook` (8790 / 8787–8789) so both stacks can run side by side.

## Quick start (dry-run)

```bash
pip install -r .cursor/skills/polymarket-quote/requirements-trade.txt

# Repo-root .env (never commit):
#   PRIVATE_KEY, FUNDER, …   — only for --live / live modes
#   PM_PROXY (optional)      — default http://127.0.0.1:1082

python3 frontend/run_main.py --no-browser
open http://127.0.0.1:8890/
```

Stop: `Ctrl-C` / `kill` the `run_main` process, or `POST http://127.0.0.1:8890/api/stop`.

### Trading modes

```bash
python3 frontend/run_main.py --no-browser
python3 frontend/run_main.py --live --max-usdc 2
python3 frontend/run_main.py --score-mode dry --prop-mode live --ft-mode live --max-usdc 1
python3 frontend/run_main.py --no-trade
```

| Flag | Meaning |
|---|---|
| `--live` | score / prop / FT channels all post real CLOB orders |
| `--score-mode dry\|live` | Team score events → totals/spreads (moneyline mid-game off by default) |
| `--prop-mode dry\|live` | `player_stat_change` → player O/U (Over when curr > line) |
| `--ft-mode dry\|live` | `match_finished` → moneyline + remaining markets |
| `--take-depth top\|walk` | Fill from best level or walk the book |
| `--max-usdc` / `--max-shares` | Size caps |

Env: `QUOTE_LIVE`, `QUOTE_SCORE_MODE`, `QUOTE_PROP_MODE`, `QUOTE_FT_MODE`, `QUOTE_TAKE_DEPTH`, `QUOTE_MAX_USDC`, `QUOTE_TRADE=0`, …

## Event model

| Event | Source | Quote action |
|---|---|---|
| `score_change` | Every DQD team score delta | Totals/spreads already decided; reversals flatten |
| `player_stat_change` | Boxscore diff (pts/ast/reb/stl/blk) when available | Player Over when curr > line |
| `match_finished` | `period` → `FT` | 2-way moneyline + remaining markets |

Player boxscores come from `api.dongqiudi.com/.../bkb_match/lineup/{match_id}` when DQD has data; low-tier games may only emit score events.

## Logs & data

| Path | Purpose |
|---|---|
| http://127.0.0.1:8890/ | System Main hub |
| `data/pm-quote/watch.log` | Quote / trade stdout |
| `data/pm-quote/trades.jsonl` | Dry-run plans + live posts |
| `data/bridge/events.jsonl` | Durable bridge events |
| `data/boxscores/` | Per-match lineup snapshots |
| `data/polymarket/snapshot.json` | Last PM list |

## Skill CLIs

```bash
python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py list --tab full --json
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --within-hours 168 --json
python3 .cursor/skills/match-bridge/scripts/bridge_match.py once --json
python3 .cursor/skills/polymarket-quote/scripts/pm_quote.py once --from-bridge --no-trade --json
```
