# CL Race 2026 — Game-Day Runbook

Game window: **Wednesday 21 Oct 2026, 12:00–16:00 SGT**
Instances: `clrace2026.clreport.com` (dry run, port 8013) · `clirace2026.clreport.com` (live event, port 8016)

---

## 1. What is already automated

| Control | What it does | Where |
|---|---|---|
| `com.clrace2026.backend` | launchd agent, `KeepAlive`, restarts the dry backend within ~1 s of a crash | `~/Library/LaunchAgents/` |
| `com.clirace2026.backend` | same for the wet backend | `~/Library/LaunchAgents/` |
| `com.familyhub.cloudflared` | the public tunnel process (`cloudflared tunnel run familyhub`), keeps both hostnames alive | `~/Library/LaunchAgents/` |
| `com.clrace2026.snapshot` | **every 5 minutes**: consistent SQLite snapshot of both DBs → `~/.hermes/backups/race-live/<ts>/`, last 100 kept, plus a copy to Google Drive → `My Drive/CLrace2026-backups/` (last 20) | `~/.hermes/scripts/race_snapshot.py` |
| `com.clrace2026.watchdog` | **every 2 minutes**: probes 8013, 8016, both public hosts; auto-restarts the dead agent; alerts Telegram (primary) with a 10-min cooldown and a recovery message | `~/.hermes/scripts/race_watchdog.py` |
| Machine power | `pmset sleep 0`, `womp 1`, `powernap 1` — the Mac mini does not sleep | `pmset -g custom` |

Logs: `~/.hermes/logs/race_snapshot.log`, `~/.hermes/logs/race_watchdog.log`.

---

## 2. Pre-game checklist (do at 11:30)

```bash
# 1. Everything loaded and alive
launchctl list | grep clrace2026
#    expect: com.clrace2026.backend, com.clirace2026.backend,
#            com.clrace2026.snapshot (0), com.clrace2026.watchdog (0)

# 2. Both instances answer locally and publicly
for p in 8013 8016; do curl -s -m 5 http://127.0.0.1:$p/api/health; echo; done
curl -s -m 8 https://clrace2026.clreport.com/api/health; echo
curl -s -m 8 https://clirace2026.clreport.com/api/health; echo

# 3. Data is clean
sqlite3 ~/projects/cli-race-2026/backend/app/data/clirace2026.db \
 "select (select count(*) from sessions), (select count(*) from submissions), (select status from events);"
#    expect: 0|0|not_started

# 4. Log in as admin on each instance in a browser and confirm the Teams page shows 12 ready teams.

# 5. Full end-to-end drill on the DRY instance (dry only — leaves wet data untouched):
python3 ~/.hermes/cache/scratch/readiness_e2e.py https://clrace2026.clreport.com ~/projects/cl-race-2026
```

`readiness_e2e.py` drives the whole flow over HTTPS: admin login → event live → start team → team login → checkpoints → photo upload → admin approval → points. It prints `PASS`/`FAIL` per step.

---

## 3. If something breaks mid-game

### A. Page is slow / one team stuck
Nothing to do — this is normal load. Refresh. If a team's timer looks wrong, open **Race Control** as admin; it self-refreshes every second.

### B. Public URL does not load (tunnel trouble)
1. Wait 60 s — the watchdog probes every 2 min and restarts the tunnel automatically; you will get a Telegram message either way.
2. Manual restart (~10 s outage):
   ```bash
   launchctl kickstart -k gui/$(id -u)/com.familyhub.cloudflared
   ```
3. Still down → check `pgrep -fl cloudflared` and `tail -20 ~/.cloudflared/*.log`. If the tunnel cannot come back, fall back to plan **D**.

### C. A backend crashed or hangs
```bash
launchctl kickstart -k gui/$(id -u)/com.clrace2026.backend   # dry
launchctl kickstart -k gui/$(id -u)/com.clirace2026.backend   # wet
```
Recovery is ~1–2 s; sessions live in SQLite so nobody is logged out. If the process restarts in a loop, check `~/projects/cli-race-2026/backend/logs/` — usually a port clash (`lsof -ti :8016`).

### D. Data looks wrong / someone deleted the event / DB corrupted
```bash
# restore the newest good snapshot (5-minute granularity)
NEW=$(ls -1d ~/.hermes/backups/race-live/* | tail -1)
launchctl bootout gui/$(id -u)/com.clrace2026.backend
cp "$NEW/clrace2026.db" ~/projects/cl-race-2026/backend/app/data/clrace2026.db
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clrace2026.backend.plist
```
Same pattern for wet (`clirace2026.db` + `com.clirace2026.backend`). You lose at most the last 5 minutes of submissions — re-key them from the checkpoint sheets.

### E. The Mac mini itself dies (power, hardware, macOS update)
This is the one failure that cannot be covered in place. Fallbacks, in order:
1. **Paper/scoring fallback (use immediately, no downtime):** the checkpoint marshal writes the team name, checkpoint, time, and answer on the printed checkpoint sheet; the race keeps running exactly as clocked. Re-key everything into the app when the host is back (the backup is ≤5 min old and mirrored to Google Drive).
2. **Restore on another Mac in ~10–15 min:** clone `https://github.com/safeharbourz-sg/clrace2026`, `pip install -r clirace/backend/requirements.txt`, copy the newest snapshot from `My Drive/CLrace2026-backups/` into `backend/app/data/`, `uvicorn app.main:app --host 127.0.0.1 --port 8016`, then point a Cloudflare quick tunnel at it (`cloudflared tunnel --url http://127.0.0.1:8016`) and hand out the new URL in the team group chat.
3. Mobile hotspot from a phone if the venue Wi-Fi/router is the problem.

### F. Total network loss at the venue
Backends bind to `127.0.0.1` only, so LAN access is not available as-is — the public hostnames depend on internet. In this case: paper fallback (E1); mobile hotspot into the Mac mini's Ethernet/Wi-Fi to restore the tunnel.

---

## 4. After the race

```bash
# freeze the result: one last snapshot, then export
/usr/bin/python3 ~/.hermes/scripts/race_snapshot.py
sqlite3 -header -csv ~/projects/cli-race-2026/backend/app/data/clirace2026.db \
 "select t.name, p.checkpoint_id, p.arrived_at, p.points, p.status from team_progress p join teams t on t.id=p.team_id order by t.id, p.checkpoint_id;" \
 > ~/Documents/clrace2026-results.csv
```
Admin → **Leaderboard** → *Export CSV* gives the same numbers from the UI. Leave the instances running; purge with `~/.hermes/skills/games/cl-race-2026/scripts/purge_race.py` before the next event.
