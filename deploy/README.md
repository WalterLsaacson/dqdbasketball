# hk1 basketball deployment

The installed `dqdbasketball.service` starts the hub and three boards on
`127.0.0.1:8890`–`8887`. All score, player, and FT channels are **dry-run**.
It uses direct Polymarket access (`PM_PROXY=none`) and DQD scores by default.

```bash
systemctl status dqdbasketball.service
journalctl -u dqdbasketball.service -f
tail -f /root/workspace/dqdbasketball/data/pm-quote/watch.log
```

From a workstation, use an SSH tunnel to see the hub:

```bash
ssh -L 8890:127.0.0.1:8890 hk1
```

Open `http://127.0.0.1:8890/` and `http://127.0.0.1:8890/trades` locally.
The service does not have a private key and must remain dry-run until book
access, settlement logic, order minimums, wallet balance, and cancellation
behavior have been checked on live match events.

## Public read-only preview

`dqdbasketball-public-preview.service` exposes the hub and three boards on
public ports 8794–8797. It requires the private access link, then shares a
12-hour browser cookie across those ports. Only listed GET routes are forwarded;
POST control endpoints return 405. The token is stored outside the repository
at `/etc/dqdbasketball-preview.token` (root:www-data, mode 0640). The public
link uses HTTP, so treat it as a preview link and do not share it broadly.

```bash
systemctl status dqdbasketball-public-preview.service
journalctl -u dqdbasketball-public-preview.service -f
```

## Order rules

- Score changes are handled immediately. Mid-game candidates are limited to
  outcomes already mathematically settled, such as Over after the current
  total has crossed its line. Moneyline, spreads, odd/even and overtime wait
  until FT.
- A settled WIN with an absent ask or an ask above 0.995, plus a credible bid,
  can place one 0.99
  GTC rest buy per token. Rest fills are reconciled into the persisted ledger.
- Confirmed inventory bought for at least 0.98 is listed at 0.999 only if
  the book permits a 0.001 tick and the CLOB share minimum is met.
- Reversal flatten never lowers its minimum sale price below 80% of entry cost
  (`QUOTE_FLATTEN_FLOOR_PCT=0.80`). Unfilled exits remain pending.

## Optional Nami HTTP score source

`QUOTE_SCORE_SOURCE=nami_http` polls Nami every five seconds. It requires a
verified DQD-to-Nami ID map when DQD does not include a tracker URL:

```text
NAMI_MATCH_IDS_JSON={"<dqd_match_id>":{"id":"<nami_id>","swap":false}}
```

`swap` must reflect the actual Nami home/away orientation. The first Nami
observation establishes a baseline and never triggers a trade. DQD remains
the default source until mapping and live scores are validated. No Nami MQTT
connection is enabled in this version.

Run offline regressions with:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```
