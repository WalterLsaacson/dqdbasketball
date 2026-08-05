# Basketball Match Bridge — Reference

## Matching

The bridge reads `data/snapshot.json` from `dongqiudi-basketball` and `data/polymarket/snapshot.json` from `polymarket-basketball`.

Greedy 1:1 matching requires:

1. PM kickoff no more than six hours stale by default.
2. Beijing kickoff skew of at most 90 minutes.
3. Home and away team similarity of at least 0.75 in direct or reversed orientation.
4. Matching canonical league aliases when both sources identify a known league.
5. Composite score of at least 0.70.

`scripts/team_aliases.py` covers common NBA, WNBA, and CBA Chinese ↔ English forms. `scripts/league_aliases.py` covers their common DQD and Gamma names.

## Output

Each matched `polymarket` object includes:

- `event_id`, `slug`, `url`, `gamma_event_url`, and `condition_ids`
- Every `market_refs` entry, including moneyline, spread, total, and player-prop markets
- Parsed `props` for points, assists, rebounds, steals, blocks, and threes

## Events

DQD watch events are enriched only after the associated fixture matches Polymarket:

```json
{
  "type": "player_stat_change",
  "match_id": "12345",
  "person_en_name": "LeBron James",
  "changes": { "points": 3 },
  "polymarket": {
    "event_id": "67890",
    "market_refs": [{ "category": "player_prop", "condition_id": "0x…" }],
    "props": [{ "player": "LeBron James", "stat": "points" }]
  }
}
```

The bridge forwards `score_change`, `player_stat_change`, and `match_finished` to the in-memory `BridgeRuntime.event_queue`, the result payload, and `data/bridge/events.jsonl`.

Overtime periods remain live: score and player-stat events continue through OT. `match_finished` is emitted when DQD `period` changes to `FT`.

## State files

| File | Role |
|---|---|
| `data/bridge/matches.json` | Latest matched fixtures and tick events |
| `data/bridge/events.jsonl` | Durable enriched event log |
| `data/bridge/prev_scores.json` | Offline score-diff baseline |
| `data/bridge/prev_period.json` | Offline full-time baseline |
| `data/snapshot.json` | Dongqiudi basketball snapshot |
| `data/polymarket/snapshot.json` | Polymarket basketball snapshot |

Run local checks:

```bash
python3 .cursor/skills/match-bridge/scripts/smoke_match_hardening.py
python3 .cursor/skills/match-bridge/scripts/smoke_ft_period.py
```
