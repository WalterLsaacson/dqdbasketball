---
name: match-bridge
description: >-
  Bridges dongqiudi-basketball and polymarket-basketball: refreshes both
  sources, fuzzy-matches basketball fixtures, and forwards enriched score,
  player-stat, and full-time events with Polymarket market handles.
---

# Basketball Match Bridge (Dongqiudi ↔ Polymarket)

Matches Dongqiudi basketball fixtures to Polymarket basketball events using English/CN team aliases, Beijing kickoff, and league aliases. Every matched row exposes Polymarket event, market, and player-prop handles.

## Quick start

```bash
# Refresh both upstream skills and match
python3 .cursor/skills/match-bridge/scripts/bridge_match.py once --json

# Match existing snapshots only
python3 .cursor/skills/match-bridge/scripts/bridge_match.py once --offline --json

# Run the in-process bridge loops
python3 .cursor/skills/match-bridge/scripts/bridge_match.py start --foreground
```

## Sources and cadence

| Source | Skill | Default refresh |
|---|---|---|
| Dongqiudi | `dongqiudi-basketball` watch (`full` tab) | 15s live / 60s idle |
| Polymarket | `polymarket-basketball` list | 600s (10 min), `within_hours=48` |

## Event contract

Dongqiudi watch detects `score_change`, `player_stat_change`, and `match_finished`. The bridge forwards only events whose fixture has a Polymarket match, attaches `polymarket.event_id`, `market_refs`, and `props`, then publishes them to:

- `BridgeRuntime.event_queue` — low-latency in-process channel for quote.
- `data/bridge/events.jsonl` — durable event log.
- The current `matches.json` payload’s `events` array.

Basketball overtime (`OT`, `OT1`, etc.) remains live and score changes continue to emit. A match finishes only when DQD `period` transitions to `FT`.

`BridgeRuntime` remains the single in-process owner; `polymarket-quote` imports and owns that runtime rather than spawning a second bridge.

## Related skills

- [`dongqiudi-basketball`](../dongqiudi-basketball/SKILL.md)
- [`polymarket-basketball`](../polymarket-basketball/SKILL.md)
- [`polymarket-quote`](../polymarket-quote/SKILL.md)

See [reference.md](reference.md) for matching rules and output shape.
