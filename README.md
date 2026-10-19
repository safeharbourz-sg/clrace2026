# CL RACE 2026

Photo-scavenger-hunt race platform used for the CapitaLand team-building event: teams walk a checkpoint course on foot, reproduce a reference photo at each stop, upload from their phones, and admins score each photo live.

This repository contains **the complete source of both live instances**, which run the identical codebase and differ only in their seeded content:

| Folder | Instance | Public URL | Content | Checkpoints |
|---|---|---|---|---|
| `clrace/` | **CL RACE 2026** (dry / haze) | https://clrace2026.clreport.com | Dry-weather set | 7 |
| `clirace/` | **CLI RACE 2026** (wet) | https://clirace2026.clreport.com | Wet-weather set (Tanjong Pagar) | 9 |

📖 **User guide with screenshots: [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md)**
📐 **Full requirements & spec: [`docs/CLRACE2026-REQUIREMENTS.md`](docs/CLRACE2026-REQUIREMENTS.md)**

---

## Screenshots

| Admin dashboard | Team mission card | Approvals & scoring |
|---|---|---|
| ![Dashboard](docs/images/02-admin-dashboard.png) | ![Mission](docs/images/10-team-mission.png) | ![Approvals](docs/images/03-admin-approvals.png) |

More in [`docs/images/`](docs/images).

---

## How it works

- **FastAPI** backend (`backend/app/main.py`) serves both the JSON API and the single-page app.
- **Single-file SPA** (`backend/app/static/index.html`) — no build step, no npm, no framework. Vanilla JS with hash routing (`#/admin/approve`, `#/team/mission`, `#/hall` …), mobile-first CSS, served with `Cache-Control: no-store`.
- **SQLite** database (`backend/app/data/<name>.db`) created and seeded on first start.
- **Photo pipeline** — camera *and* gallery upload inputs, HEIC/HEIF support, EXIF auto-orient, GPS stripping, 1600 px / 480 px downscale, stored under `backend/app/uploads/`.
- **Server-side timing** — elapsed time is derived from `teams.started_at` on the server, so phones sleeping or clocks drifting cannot cheat it.

### Race mechanics in one paragraph

An admin sets the event to `live` and then **releases** teams one by one — release is the *only* way a team starts, there is no self-start. Each team sees exactly one checkpoint at a time (future checkpoints are never sent to the client), reproduces the photo, uploads it, and waits. The admin types the points and approves; approval unlocks the next checkpoint. Odd release orders walk the course forward, even orders in reverse, so teams do not queue up. Finishing all checkpoints stamps the final elapsed time and awards the completion bonus. Approved photos can be featured in a public Hall of Fame.

---

## Repository layout

```
clrace2026/
├── clrace/                     # CL RACE 2026 — dry instance
│   ├── backend/
│   │   ├── app/
│   │   │   ├── main.py         # FastAPI app: all routes
│   │   │   ├── models.py       # SQLAlchemy models
│   │   │   ├── database.py     # engine / session / init
│   │   │   ├── auth.py         # password hashing + session helpers
│   │   │   ├── seed.py         # accounts, teams, routes, checkpoints, refs
│   │   │   ├── static/index.html   # the whole SPA
│   │   │   ├── uploads/refs/   # reference photos (seed assets)
│   │   │   └── data/           # SQLite DB (git-ignored, created at runtime)
│   │   ├── smoke_test.py       # end-to-end test against a local instance
│   │   ├── prod_smoke_test.py  # read-only test against the public URL
│   │   └── requirements.txt
│   └── deploy/                 # launchd plist + Cloudflare ingress notes
├── clirace/                    # CLI RACE 2026 — wet instance (same layout)
└── docs/
    ├── USER-GUIDE.md
    ├── CLRACE2026-REQUIREMENTS.md
    └── images/                 # guide screenshots
```

---

## Run it locally

```bash
cd clirace/backend            # or clrace/backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
RACE_ADMIN_PASSWORD='<choose-one>' \
RACE_TEAM_PASSWORD='<choose-one>' \
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8016
```

Open http://127.0.0.1:8016 — the database is created and seeded on first start (accounts, 12 teams with forward/reverse routes, checkpoints, reference photos, settings).

The dry instance conventionally runs on **8013**, the wet instance on **8016**; run them from their own folders and they keep separate databases.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `RACE_ADMIN_PASSWORD` | *generated* | Password given to the seeded admin accounts (`max`, `lyn`, `karina`, `shijie`). If unset, a random password is generated and printed once during seeding. |
| `RACE_TEAM_PASSWORD` | *generated* | Password given to the seeded team accounts (`team1`…`team12`). If unset, generated and printed once during seeding. |

Passwords are stored hashed; there is no password-change screen — to rotate them, re-seed a fresh database or update the `users` table.

### Tests

```bash
cd clirace/backend
.venv/bin/python smoke_test.py                     # full flow, local instance (mutates the DB)
RACE_ADMIN_PASSWORD=… RACE_TEAM_PASSWORD=… \
  .venv/bin/python prod_smoke_test.py              # read-only, against the public URL
```

---

## Deployment

Both instances are hosted the same way on a Mac mini:

- **uvicorn** under a launchd agent — `com.clrace2026.backend` (dry, :8013) and `com.clirace2026.backend` (wet, :8016), `KeepAlive` on, logs under `backend/logs/`.
- **Cloudflare Tunnel** terminates TLS and maps each hostname to its local port (`~/.cloudflared/config.yml`, ingress rules per hostname). Non-browser user agents may get Cloudflare `1010`/`403` — that is expected, test with a real browser.
- The SPA HTML is served `no-store`, so a deploy is visible on the next reload without cache-busting.

Redeploy flow:

```bash
cd clirace/backend
.venv/bin/python -m py_compile app/main.py app/seed.py app/models.py
launchctl kickstart -k gui/$(id -u)/com.clirace2026.backend
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8016/api/health
```

---

## Data & privacy

- Participant data (sessions, submissions, uploaded proof photos, scores) is **runtime data and is not in this repository** — `*.db`, `uploads/proof/`, `logs/` and `.venv/` are git-ignored.
- Only seed assets ship: the checkpoint reference photos (`uploads/refs/`) and the seed script (which stores password *hashes*).
- Uploaded photos have GPS EXIF stripped server-side before storage.

## Reset between events

Set `events.status` back to `not_started`, clear `sessions`, `submissions`, `team_progress`, `score_adjustments`, `hall_of_fame`, `audit_logs`, delete `uploads/proof/*`, and reset each team (`status='ready'`, `started_at=NULL`). Accounts, teams, routes, checkpoints and settings are preserved — details in `docs/CLRACE2026-REQUIREMENTS.md §8`.
