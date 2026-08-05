# Polymarket Basketball — Reference

## Upstream (Gamma API)

Base: `https://gamma-api.polymarket.com`  
Docs: [Fetching Markets](https://docs.polymarket.com/market-data/fetching-markets), [Sports metadata](https://docs.polymarket.com/api-reference/sports/get-sports-metadata-information)

| Step | Request | Role |
|---|---|---|
| 1 | `GET /sports` | Sport code → `series` id |
| 2 | `GET /events?series_id=&…` | Events for each basketball league (optional local time window) |
| 3 | `markets[].gameStartTime` | Kickoff (UTC) → Beijing |

### Upcoming 48h window (default)

`list` defaults to **next 48 hours** (plus games that kicked off up to 3h ago, for live coverage).

Filtering is **local only** on `markets[].gameStartTime`. Do **not** send Gamma `order=start_date` or `start_date_*` / `start_time_*` for basketball — those may 400 or return empty / wrong rows.

Basketball leagues come dynamically from `GET /sports`: sport names matching `nba`, `wnba`, `euroleague`, `ncaab`, or starting with `bk`, with tags **`100639`** and **`745`** retained as basketball/NBA signals. Gamma currently shares `100639` with unrelated sports metadata, so a basketball name is also required; this prevents soccer and other sports from leaking into the fixture list. WNBA is discovered dynamically rather than through a hard-coded tag list.

CLI: `--within-hours 48` (default), `--within-hours 0` disables the window. A mapped event requires a two-outcome moneyline (no draw). Its `market_refs` retain moneyline, spreads, totals, and other handles; supported player props are exposed separately.

## Proxy (default)

Outbound HTTPS goes through the local Shadowrocket proxy by default (matches system HTTP/HTTPS proxy used by Chrome):

| Setting | Value |
|---|---|
| Default | `http://127.0.0.1:1082` |
| Env | `PM_PROXY` or `ALL_PROXY` |
| CLI | `--proxy URL` / `--no-proxy` |
| Disable | `--no-proxy` or `PM_PROXY=none` |

- Shadowrocket on this machine exposes **HTTP** on `1082` (`HTTPEnable`/`HTTPSEnable`); SOCKS may be off in system settings even if the port also accepts SOCKS.
- Bare `host:port` is treated as `socks5h://` (remote DNS). Prefer an explicit `http://` URL for Shadowrocket.
- SOCKS needs [PySocks](https://pypi.org/project/PySocks/): `pip3 install PySocks`.
- Requests go through `curl -x` first. If Shadowrocket returns **CONNECT 503** for the hostname `gamma-api.polymarket.com` (while `polymarket.com` still works), the client retries with Cloudflare IP pinning (`curl --connect-to`).
- Optional: in Shadowrocket, add `DOMAIN-SUFFIX,gamma-api.polymarket.com,PROXY` (or GLOBAL) so the hostname itself is allowed.

When the season has no open fixtures, `list` may return `count: 0` with default filters. Use `--include-closed` to include settled games.

## Basketball markets and props

`markets[].sportsMarketType` is retained as `sports_market_type`. Markets are classified as `moneyline`, `spread`, `total`, `player_prop`, or `other`; each includes the Gamma market / condition id, question, outcomes, and CLOB token ids.

`props` includes markets whose type or question names one of: `points`, `assists`, `rebounds`, `steals`, `blocks`, or `threes` / three-pointers. A prop record contains:

```json
{
  "player": "Caitlin Clark",
  "stat": "points",
  "line": 22.5,
  "sports_market_type": "player_points",
  "question": "Caitlin Clark Over 22.5 Points",
  "condition_id": "…",
  "clob_token_ids": ["…", "…"]
}
```

Only event titles matching `A vs[. ] B` (or `A v B`) and carrying a two-way moneyline are kept as fixtures.

## `list` JSON shape

```json
{
  "fetched_at": "2026-07-19T15:40:00+08:00",
  "source": "polymarket-gamma",
  "proxy": "http://127.0.0.1:1082",
  "window": {
    "within_hours": 48,
    "start_utc": "2026-07-19T08:00:00Z",
    "end_utc": "2026-07-21T08:00:00Z"
  },
  "count": 12,
  "leagues": [{"id": "nba", "series_id": "…", "name": "NBA", "count": 5}],
  "matches": [
    {
      "event_id": "…",
      "slug": "new-york-liberty-vs-minnesota-lynx",
      "league": "WNBA",
      "league_id": "wnba",
      "series_id": "…",
      "home": "New York Liberty",
      "away": "Minnesota Lynx",
      "start_play": "2025-08-15 19:00:00+00",
      "time": "03:00",
      "local_date": "2025-08-16",
      "kickoff_beijing": "2025-08-16 03:00",
      "url": "https://polymarket.com/event/new-york-liberty-vs-minnesota-lynx",
      "market_refs": [{"category": "moneyline", "sports_market_type": "moneyline"}],
      "props": [{"player": "…", "stat": "points", "line": 22.5}]
    }
  ]
}
```

Timezone: `Asia/Shanghai` (UTC+8). Primary display field: `kickoff_beijing`.

## State files

| File | Role |
|---|---|
| `data/polymarket/snapshot.json` | Last `list` result |
| `data/polymarket/leagues.json` | Last `leagues` result |

## Cooperation

Other skills/modules should consume CLI stdout JSON or the snapshot file. Do not scrape `polymarket.com` HTML for this data.
