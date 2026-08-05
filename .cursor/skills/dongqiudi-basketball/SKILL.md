---
name: dongqiudi-basketball
description: >-
  Fetches Dongqiudi basketball match tabs (full/hot), English team and player
  names, scores, basketball boxscores, and score/player-stat events. Use when
  the user asks about 懂球帝 basketball, live scores, boxscores, or stat changes.
---

# Dongqiudi Basketball Data

Project skill for basketball match tabs, score changes, and live player stats.
Scripts live in this skill folder; runtime state is under the repo `data/` directory.

## Quick start

From the repo root (`dongqiudibasket`):

```bash
# Hot tab snapshot (JSON)
python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py list --tab hot --json

# All tabs
python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py list --tab all --json

# One poll + diff for /loop (sentinels on stdout)
python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py watch --tab hot --once

# Same poll as pure JSON (events in .events; no sentinel lines)
python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py watch --tab hot --once --json --quiet

# Resident poll (15s when live, 60s when idle)
python3 .cursor/skills/dongqiudi-basketball/scripts/dqd_match.py watch --tab hot --interval 15 --idle-interval 60
```

Defaults: basketball only (`cmp_type=basketball`), `language=en`, **`--days 3`**. Today comes from `match_list`; later days use `schedule_list` (`tab_type=fixture` + Nuxt-encoded `start`). English team names are cached in `data/dqd_team_en.json`; player names in `data/dqd_person_en.json`.

## Agent workflow

1. **Tab data** — run `list --tab <full|hot|all> --json`.
2. **Watch games** — use `watch --tab hot --once` on a 10–15 second live loop, or run resident mode.
3. **Events** — every score delta produces `score_change`; transition to `FT` produces `match_finished`; a live boxscore stat increase produces `player_stat_change`. All are appended to `data/events.jsonl` and emitted as `DQD_SCORE_CHANGE` sentinels.
4. **Boxscores** — each playing match fetches the basketball lineup endpoint, writing raw responses to `data/boxscores/{match_id}.json`.

## Cooperation contract (for other skills)

| Channel | Path / signal | Purpose |
|---|---|---|
| CLI JSON | stdout from `list` / `watch --once --json` | Immediate tab snapshot |
| Event log | `data/events.jsonl` (append-only) | Durable score_change history (incl. suppressed ET) |
| Sentinel | `DQD_SCORE_CHANGE <json>` on stdout | Wake loop / session notify (regulation + stoppage only) |

Consumer skills should:

- Prefer watch JSON `events` or `DQD_SCORE_CHANGE` sentinels; every basketball event is eligible for downstream delivery.
- Treat `is_score_up: true` as a score increase. `is_goal` is retained as its compatibility alias.
- Not call Dongqiudi HTML/OCR themselves.

### Example handoff

```text
Other skill input:
{
  "type": "score_change",
  "match_id": "...",
  "home": "Los Angeles Lakers",
  "away": "Boston Celtics",
  "prev": {"home": 0, "away": 0},
  "curr": {"home": 1, "away": 0},
  "side": "home",
  "is_score_up": true,
  "tab": "hot"
}
```

## Tabs

| Tab | Rule (same as official /match page) |
|---|---|
| `full` | All basketball matches in the date window (default **today + next 2 days** Beijing) |
| `hot` | `business_status & 320 == 320` basketball hot heuristic |

## Related skill

## Files

- Scripts: [scripts/dqd_match.py](scripts/dqd_match.py), [scripts/dqd_lib.py](scripts/dqd_lib.py)
- Field/event schema: [reference.md](reference.md)
- State (gitignored contents): `data/snapshot.json`, `data/prev_scores.json`, `data/events.jsonl`, `data/boxscores/{match_id}.json`
