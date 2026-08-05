# Dongqiudi Basketball — Reference

## Upstream APIs

```text
GET https://www.dongqiudi.com/magicball/v1/list/match_list
  ?language=zh-cn&cmp_type=basketball&tab_type=all

GET https://www.dongqiudi.com/magicball/v1/list/schedule_list
  ?language=zh-CN&tab_type=fixture&cmp_type=basketball&start=<Nuxt-encoded>

GET https://api.dongqiudi.com/soccer/biz/dqd/bkb_match/lineup/{match_id}
  ?app=dqd&lang=zh-cn
```

The boxscore request falls back to the same `/soccer/biz/dqd/bkb_match/lineup/{match_id}` path on `www.dongqiudi.com`. English names use `/magicball/v1/team/detail?team_id=X&language=en` and `/magicball/v1/person/detail?person_id=X&language=en`.

- Scope is **basketball only**: every match feed requests `cmp_type=basketball`.
- Default window is today plus the next two Beijing calendar days (`--days 3`).
- `schedule_list.start` is Nuxt's UTC-like encoding: Beijing `2026-07-22` becomes `2026-07-21 16:00:00`.
- English team and player names cache in `data/dqd_team_en.json` and `data/dqd_person_en.json`.

## Tabs and mapped match fields

| Tab | Rule |
|---|---|
| `full` | Every basketball game in the date window |
| `hot` | `business_status & 320 == 320` basketball hot heuristic |

Mapped basketball fields include `period` (`Q1`–`Q4`, `OT`, `FT`), `home_fs_4` / `away_fs_4`, `score_diff` (home minus away), `score_total`, and `minute_str` (game clock as supplied by DQD).

## Events

All events append to `data/events.jsonl`. Text-mode watcher output prefixes each emitted event with `DQD_SCORE_CHANGE`; JSON mode returns them under `events`.

### `score_change`

Emitted for **every** final-score (`fs`) delta, including corrections or decreases. `is_score_up` is true if either team's score increased; `is_goal` remains a compatibility alias with the same value.

```json
{
  "type": "score_change",
  "match_id": "123",
  "prev": {"home": 64, "away": 60},
  "curr": {"home": 66, "away": 60},
  "side": "home",
  "is_score_up": true,
  "is_goal": true,
  "period": "Q3",
  "fs_4": {"home": "66", "away": "60"},
  "score_diff": 6,
  "score_total": 126,
  "minute_str": "05:42"
}
```

### `match_finished`

Emitted exactly when a match's tracked period transitions to `FT`. The score baseline stores `period` to prevent repeat events.

### `player_stat_change`

While a match is `Playing`, the watcher fetches and saves its raw lineup response at `data/boxscores/{match_id}.json`. It normalizes player rows and emits a change if any of these stats increases: `score`, `rebound`, `assist`, `steal`, or `block`.

```json
{
  "type": "player_stat_change",
  "match_id": "123",
  "person_id": "456",
  "person_name": "勒布朗·詹姆斯",
  "person_en_name": "LeBron James",
  "changes": {"assist": 1, "score": 2},
  "curr": {"score": 18, "rebound": 7, "assist": 6, "steal": 1, "block": 0},
  "period": "Q3"
}
```

First observation seeds a score or player-stat baseline and emits no event.

## State files

| File | Purpose |
|---|---|
| `data/snapshot.json` | Last list/watch snapshot |
| `data/prev_scores.json` | Match score/period and player-stat baselines |
| `data/events.jsonl` | Append-only event log |
| `data/boxscores/{match_id}.json` | Last raw lineup payload for each live game |
