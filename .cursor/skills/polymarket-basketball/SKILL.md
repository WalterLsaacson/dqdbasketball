---
name: polymarket-basketball
description: >-
  Fetches Polymarket basketball matchups and market handles through the public
  Gamma API. Use for NBA, WNBA, EuroLeague, other basketball leagues, spreads,
  totals, and basketball player props.
---

# Polymarket Basketball

Project skill for all basketball fixtures listed by Polymarket. Data comes from the official **Gamma API** — do not scrape the page DOM.

## Quick start

From the repo root:

```bash
# All basketball leagues in Gamma's sports catalog
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --json

# One or more leagues
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --league nba --json
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --league nba,wnba --json

# Time window (default: next 48h; use a longer window in the NBA offseason)
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --within-hours 168 --json
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --within-hours 0 --json

# Available league codes
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py leagues --json
```

Outbound requests **default to** `http://127.0.0.1:1082` (Shadowrocket / MacPacket system HTTP proxy; same port as Chrome). SOCKS5 (`socks5h://127.0.0.1:1082`) also works when enabled. Override with `--proxy`, `PM_PROXY` / `ALL_PROXY`, or disable with `--no-proxy`. SOCKS needs `PySocks` (`pip3 install PySocks`).

```bash
# Explicit proxy / direct
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --proxy http://127.0.0.1:1082 --json
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --proxy socks5h://127.0.0.1:1082 --json
python3 .cursor/skills/polymarket-basketball/scripts/pm_basket.py list --no-proxy --json
```

## Agent workflow

1. **Match list** — run `list` (optionally `--league`). Return fixture teams and its `market_refs` / `props`.
2. **League codes** — run `leagues` when the user asks which competitions are available.
3. **Handoff** — give stdout JSON or `data/polymarket/snapshot.json` to cooperating skills/modules. Do not invent fixtures.

## Cooperation contract

| Channel | Path / signal | Purpose |
|---|---|---|
| CLI JSON | stdout from `list` / `leagues` | Immediate structured data |
| Snapshot | `data/polymarket/snapshot.json` | Last successful `list` |

Each match includes `league`, `home`, `away`, `kickoff_beijing` (Asia/Shanghai), `event_id`, `slug`, `url`, `market_refs`, and `props`. `market_refs` classifies moneylines, spreads, totals, and player props; each prop has player, stat, line, question, and CLOB token handles.

## Related frontend

Demo board that starts this skill and renders the match list with league colors:

```bash
python3 frontend/run_polymarket.py
# → http://127.0.0.1:8788/
```

Module: [`frontend/polymarket-board/`](../../../frontend/polymarket-board/).

## Related skill

For Dongqiudi live scores / hot tabs, see [`dongqiudi-basketball`](../dongqiudi-basketball/SKILL.md).

To align Dongqiudi fixtures with Polymarket markets, see [`match-bridge`](../match-bridge/SKILL.md).

## Files

- Scripts: [scripts/pm_basket.py](scripts/pm_basket.py), [scripts/pm_lib.py](scripts/pm_lib.py)
- Compatibility: `scripts/pm_soccer.py` remains a thin alias to `pm_basket.py`.
- API details: [reference.md](reference.md)
