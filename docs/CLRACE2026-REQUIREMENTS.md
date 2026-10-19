# CL Race 2026 — "CLIRACE 2026" (Wet Weather) — Full Build Requirements

**Document purpose:** a complete, self-contained specification of the existing CL Race 2026
checkpoint-race web application — technical requirements, functional requirements, accounts and
seed content — so that the portal can be rebuilt (or extended) from scratch without access to the
original source tree.

**Instance naming (important — do not "correct" the spelling):**

| Instance | Hostname | Purpose | Local port |
|---|---|---|---|
| Wet Weather (this document) | `https://clirace2026.clreport.com` | Indoor / sheltered programme ("CLI RACE") | 8016 |
| Reference twin (dry) | `https://clrace2026.clreport.com` | Original outdoor programme | 8013 |

The two hostnames differ by one letter (`cli` + `race` vs `cl` + `race`). Both are intentional.
The wet-weather instance is a **direct functional duplicate** of the dry one — identical
mechanics, roles, API surface and admin flows. **Only the content differs**: the wet instance runs
9 indoor checkpoints (Tanjong Pagar CBD buildings), the dry instance runs the outdoor set.
Everything in this document describes the wet instance; where a behaviour is shared it applies to
both.

---

## 1. What the system is

A one-day office teambuilding "amazing race" run at a single event. 12 teams race a fixed sequence
of 9 photo-mission checkpoints around the CBD. Each team has **one phone** (the team leader's) with
the app open. At each checkpoint the team reads the mission, takes **one photo** proving the
mission, uploads it, and the photo goes to a **live admin approval queue**. Admins approve (with a
score they type in) or reject. Approval unlocks the team's next station. The last checkpoint is the
final mission: approving it **stops the team's timer** and marks them finished. Score, ranking and
"fastest race times" are computed server-side and shown **only to admins** — teams are told their
placing at the event's prize giving, not in the app.

Hard operational requirements that shaped the design:

1. **Teams cannot start themselves.** The race timer for a team only starts when an admin clicks
   **Release** for that team from Race Control. 12 teams = 12 deliberate clicks.
2. **Teams must not be able to see, infer or navigate ahead** to their next station. The server
   never sends a team the identity, name or count of its not-yet-cleared checkpoints.
3. **No base points.** The admin types the checkpoint's total award at approval time; nothing is
   auto-awarded.
4. **Mobile-first, on-site, on phones.** Everything must work one-handed, on a phone, with photos
   straight from the camera **or the phone gallery**.

---

## 2. Technical requirements

### 2.1 Architecture

Three components, single machine, no external services:

```
Phone browser (single-page app, static HTML/JS/CSS — served by the backend)
        │  HTTPS  (Cloudflare Tunnel → localhost)
        ▼
FastAPI backend (Uvicorn, one process)  ──►  SQLite database file
        │                                     └─ uploaded photos on disk
        └─ static SPA at /static, uploads at /uploads
```

- **Backend:** Python 3.11+, FastAPI + Uvicorn, SQLModel/SQLAlchemy over SQLite.
- **Frontend:** **one single file** `static/index.html` containing all HTML, CSS and JS. No build
  step, no bundler, no npm. Vanilla JS + hash routing (`#/mission`, `#/admin`…). This is deliberate:
  it can be edited and redeployed on event day in seconds.
- **Database:** single SQLite file, e.g. `backend/app/data/clirace2026.db`.
- **File storage:** plain filesystem — `uploads/refs/` (checkpoint reference images, shipped) and
  `uploads/proof/` (team submissions, written at runtime). Thumbnails are stored next to the main
  image with a `t_` filename prefix.
- **No auth provider, no cloud DB, no queue.** Sessions are DB rows + a cookie.

### 2.2 Suggested repository layout

```
cli-race-2026/
├── backend/
│   ├── .venv/                     # virtualenv (invoke as ./../.venv/bin/python)
│   ├── requirements.txt           # fastapi, uvicorn, sqlmodel, passlib/bcrypt, pillow, pillow-heif, python-multipart
│   └── app/
│       ├── main.py                # ALL routes (API + static mount)
│       ├── models.py              # SQLModel tables
│       ├── auth.py                # hash_password / verify_password, session creation
│       ├── database.py            # engine, init_db(), get_session()
│       ├── seed.py                # event, accounts, 9 checkpoints, routes, settings
│       ├── data/clirace2026.db
│       ├── uploads/refs/cp1.jpg … cp9.jpg
│       ├── uploads/proof/
│       └── static/index.html      # the whole SPA
└── deploy/
    ├── com.clirace2026.backend.plist   # launchd (KeepAlive)
    └── start.sh
```

### 2.3 Runtime & hosting

- **Port:** 8016 (local only; never exposed directly).
- **Process manager:** macOS `launchd` agent `com.clirace2026.backend`, plist in
  `~/Library/LaunchAgents/`, `KeepAlive=true`, `WorkingDirectory=backend/`, running
  `.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8016`.
  Restart with `launchctl kickstart -k gui/$(id -u)/com.clirace2026.backend`.
- **Public exposure:** Cloudflare Tunnel (named tunnel, `cloudflared`), ingress rule
  `clirace2026.clreport.com → http://127.0.0.1:8016`. HTTPS terminates at Cloudflare.
- **Health check:** `GET /api/health` → `{"ok": true, "service": …, "time": …}`.

### 2.4 Data model (tables)

| Table | Purpose | Key columns |
|---|---|---|
| `event` | The event row | `name`, `tagline`, `status` (`not_started`/`live`/`paused`/`completed`), `active_event_mode_id` |
| `event_mode` | Content set within an event (here: "Wet Weather") | `event_id`, `name`, `active` |
| `checkpoint` | One mission | `event_mode_id`, `checkpoint_number`, `location`, `category`, `mission_title`, `mission_description`, `requirements` (newline-separated list), `bonus_description`, `default_score`, `is_final` |
| `checkpoint_image` | Reference photo(s) for a checkpoint | `checkpoint_id`, `image_url`, `thumbnail_url`, `sort_order` |
| `route` | An ordered station list | `event_id`, `name` ("Forward"/"Reverse"), `active` |
| `route_checkpoint` | Route membership + order | `route_id`, `checkpoint_id`, `sequence_number` |
| `team` | A competing team | `name`, `route_id`, `release_order`, `status` (`ready`/`racing`/`finished`), `started_at`, `completed_at`, `final_elapsed_seconds`, `active` |
| `user` | Login accounts (admins + team leaders) | `username`, `password_hash`, `display_name`, `role` (`admin`/`team_leader`), `team_id`, `active` |
| `team_progress` | Per team × checkpoint state machine | `team_id`, `checkpoint_id`, `sequence_position`, `status` (`locked`/`available`/`pending`/`completed`/`rejected`/`replaced`), `unlocked_at`, `completed_at` |
| `submission` | One uploaded photo | `team_id`, `checkpoint_id`, `photo_url`, `thumbnail_url`, `attempt_number`, `status` (`pending`/`completed`/`rejected`/`replaced`), `base_score`, `bonus_score`, `total_score`, `admin_comment`, `is_replaced`, `submitted_at`, `reviewed_at`, `reviewed_by` |
| `score_adjustment` | The single source of truth for team score | `team_id`, `adjustment_type` (`checkpoint`/`completion`/`time`/`manual`/`penalty`), `points`, `reason`, `created_by` |
| `session` | DB-backed login sessions | `token`, `user_id`, `ip`, `user_agent`, `created_at`, `expires_at` |
| `audit_log` | Append-only action trail | `user_id`, `action`, `entity_type`, `entity_id`, `before_json`, `after_json`, `ip_address`, `created_at` |
| `hall_of_fame` | Curated showcase entries | `submission_id`, `rank`, `featured`, `created_by` |
| `setting` | Editable key/value config | `key`, `value` |

**Score rule:** a team's score is `SUM(score_adjustment.points)` for that team — nothing else.
There is exactly **one** `score_adjustment` row of type `checkpoint` per team × checkpoint;
editing a score later **updates** that row (never inserts a second one), so totals move by the
delta and cannot double-count.

**Timer rule:** every elapsed time is computed **server-side** from `team.started_at`
(`completed_at - started_at` once finished; `now - started_at` while racing). Never trust a client
clock or a client-sent duration.

### 2.5 Photo pipeline (server side)

All uploads run through one function, whether the bytes came from the camera or the gallery:

1. **Size guard:** reject > **15 MB** → HTTP **413**.
2. **Decode:** Pillow; unreadable data → HTTP **415** ("not a valid image: JPG, PNG, WebP or HEIC").
3. **HEIC/HEIF support is mandatory** — iPhone default camera format. Register Pillow's HEIC opener
   at startup (`pillow-heif`; keep the import in a try/except so a missing dependency degrades to a
   clear 415 rather than a 500). **Add `pillow-heif` to `requirements.txt`.**
4. **Auto-orient** from EXIF (`ImageOps.exif_transpose`) so portrait photos are never stored
   sideways.
5. **Strip ALL metadata** — convert to RGB and re-save; this removes **EXIF and GPS location** from
   every photo. (Privacy requirement: no team's coordinates may be stored.)
6. **Downscale + compress:** main image max 1600×1600, JPEG quality 82; thumbnail max 480×480,
   JPEG quality 70.
7. Store main as `uploads/proof/<name>.jpg` and thumbnail as `uploads/proof/t_<name>.jpg`;
   filenames are random (`secrets.token_hex`), never user-supplied.

Note on **static reference images** (`uploads/refs/*.jpg`): these are hand-prepared, **not**
re-processed at runtime, so they must be checked by eye before the event — a reference photo stored
rotated 90° shows sideways on every phone (this happened: `cp2.jpg` had to be rotated upright; keep
backups of the originals).

### 2.6 Auth & session

- `POST /api/login` with `{username, password}`; wrong credentials → **401**, disabled account →
  **403**. On success: create a `session` row (`token`, `ip`, `user_agent`, TTL e.g. 12 h), set an
  **HttpOnly, SameSite=Lax** cookie (name `clirace_session`), return `{ok, user}`.
- Cookie is the only client credential; the DB row is authoritative (server-side logout =
  delete the row).
- `GET /api/me` returns the public user (`id`, `username`, `display_name`, `role`, `team_id`).
- Every API route depends on an auth dependency; role gates:
  `admin` for Race Control, approvals, scoring, teams, leaderboard, audit, CSV, HOF admin;
  `team_leader` for portal, next-checkpoint, submit, own submissions.
- **No password-change screen** (deliberate; accounts are seeded and used for one day).
- Passwords are stored **hashed**, never in plaintext, never logged.

### 2.7 Frontend requirements

- **One file**, `static/index.html`, served with `Cache-Control: no-store` (teams refresh mid-race;
  a cached old script has already caused a "login does nothing" bug).
- **Hash routing** with these views:
  `#/login`, `#/mission` (team home + current mission), `#/progress` (team's own cleared list),
  `#/hall` (public Hall of Fame), `#/admin` (Race Control dashboard), `#/admin/teams`,
  `#/admin/approve` (Approvals), `#/admin/leader` (Leaderboard), `#/admin/hall` (HOF curation).
  Route guard: an unauthenticated visit to a private hash must render the login card, not an empty
  page; a successful login must re-render the target view immediately (no manual reload).
- **Login form:** Enter key submits; the form auto-submits on load if credentials were just typed;
  a double-submit guard prevents duplicate login requests.
- **Mobile-first CSS**, large tap targets, works in portrait, minimal vertical scrolling.
- **Polling:** team mission view and the admin Approvals queue refresh on an interval (approvals
  auto-refresh so admins see new uploads without reloading); show the live elapsed timer ticking
  from the server value.
- **Favicon/version:** a versioned favicon URL (and, on the wet instance, a themed icon) so phones
  don't keep an old cached tab icon.
- **Programme labels + cross-links:** the login page states which programme the instance serves and
  links to the sibling instance.

### 2.8 Non-functional requirements

| Area | Requirement |
|---|---|
| Concurrency | Approval is **concurrency-safe**: use `BEGIN IMMEDIATE` (SQLite write lock) and re-check `status == 'pending'` inside the transaction; a second admin gets **409 "Submission already reviewed"**, never a double award. |
| Idempotence | Re-running `python -m app.seed` must be safe: it clears transient state (sessions, submissions, progress, score adjustments, audit) and resets teams to `ready`/`not_started`, while static rows (event, checkpoints, routes, accounts, settings) are updated in place. This is the day-of reset. |
| Timing correctness | All timestamps UTC, ISO-8601 with a `Z` suffix on the wire; client renders local (SGT, UTC+8). |
| Uploads | 15 MB cap, JPG/PNG/WebP/HEIC, auto-orient, EXIF+GPS stripped, thumbnail generated. |
| Reliability | `KeepAlive` restart on crash; one process is enough for 12 teams (~12 phones). |
| Observability | `/api/health`; `audit_log` records login, logout, submission, approve, reject, score edit, team release, race status change, manual adjustment — with actor, entity, before/after and IP. |
| Privacy | No GPS data retained; public Hall of Fame exposes only approved photos and no scores of unfinished teams. |
| Data safety | DB file + `uploads/proof/` are the only stateful data; back both up before the event. Seeded reference images live in `uploads/refs/`. |

---

## 3. Functional requirements

### 3.1 Roles

- **Admin** (event organisers): Race Control, per-team release, approvals + scoring, team pages,
  leaderboard, results CSV, audit log, Hall of Fame curation.
- **Team leader** (one per team, logged in on the team's phone): sees only its own current mission,
  uploads the proof photo, sees its own cleared history and running timer.

### 3.2 Race lifecycle (admin)

`not_started → live → (paused) → completed`, set from **Game Admin → Race Control**.

- Team-side start and submit are **both gated on `event.status == 'live'`**; otherwise the API
  returns **409 "Race is not live"**. (This is the #1 operator confusion: if "START RACE" does
  nothing, the event is not `live`.)
- Race Control shows the status with a **blinking `live` indicator** so the operator can see at a
  glance that the gate is open.
- `release_interval_minutes` (default **5**) is stored and displayed as the intended stagger, but
  the system does **not** enforce it — release order is advisory, the admin releases each team
  manually.

### 3.3 Release (the only way a team starts)

- `POST /api/admin/teams/{team_id}/start` (admin only): requires `status == 'live'`, refuses if the
  team already started (409), sets `started_at = now`, `status = 'racing'`, creates `team_progress`
  rows for the team's route (all `locked`) and unlocks the **first** station (`available`).
- Release button is per team in Race Control ("Race Start (per team)"); the list follows
  `release_order` (Team 1…12).
- There is **no** team-side "START" button. `POST /api/start` exists only as an admin convenience
  alias and must never allow a team to start itself.
- `GET /api/checkpoints/next` returns "Team has not started" until the team is released.

### 3.4 Team mission flow

1. Team logs in → lands on `#/mission`.
2. **Current mission card**, fetched from `GET /api/checkpoints/next` (the single `available`
   checkpoint), shows:
   - checkpoint number, **location** (where to go), category, mission **title** and description;
   - **Mission Requirements** — the newline list, rendered as bullets;
   - the **reference photo** (`checkpoint_image`) so the team knows what "done" looks like;
   - the **"📝 Note:"** line rendered from `bonus_description` **when present** (this is where bonus
     wording lives — it must be rendered on the card, not hidden in an admin screen);
   - an optional **Remarks** field sent with the photo;
   - the photo **upload control** (see 3.5) and a **Submit Photo** button.
3. After submitting, the card shows "awaiting approval" and the app blocks further submissions until
   an admin reviews (`GET /api/checkpoints/next` returns "Awaiting review of previous submission").
4. On approval the next station becomes available automatically; on rejection the same station
   returns to the team for a retry.
5. **Anti-peeking (mandatory):** for team roles the portal response includes **only checkpoints
   already cleared** (`completed`/`rejected`/`replaced`) plus the currently available one; locked
   stations and the route name are **omitted entirely** so the team cannot learn where it is going
   next or how many stops remain. `#/progress` deliberately shows a cleared-history list, not a
   route map.
6. When all stations are completed, the mission view says **FINISHED**.

### 3.5 Photo capture — camera AND gallery (both, always)

The upload control must offer **two explicit buttons**, because a single
`<input type="file" capture="environment">` forces the phone camera and **hides the gallery**:

- **📷 Take Photo** → `<input type="file" accept="image/*" capture="environment">`
- **🖼️ Choose from Gallery** → `<input type="file" accept="image/*">` (no `capture`, so the phone
  offers gallery/files)
- The dashed drop-zone itself opens the **gallery**.
- Whichever input is used, show a local preview, then upload on submit; **clear both inputs after a
  successful submit** (`cam.value = ''; gal.value = ''`) so the same shot isn't re-sent, and show a
  busy state while uploading.
- Requirements: `#filecam`/`#filegal` inputs, `#btnCam`/`#btnGal` buttons, helpers `open()`,
  `selFileInput()`, `uploadBusy()`, `bindUpload()`.

### 3.6 Submissions

`POST /api/submit/{checkpoint_id}` (multipart: `file`, optional `admin_note`):

- requires `live` + released team + the checkpoint is the team's current `available` (or `pending`)
  station, else **409**;
- **replace-before-review:** if a `pending` submission already exists for that team+checkpoint, it
  is marked `replaced`, its files are deleted, and the new photo takes its place (the old one is
  never shown for approval, but is kept as an audit row);
- saves photo + thumbnail, records `attempt_number`, sets submission `pending` and the team's
  progress `pending`.

`GET /api/my/submissions` returns the team's own upload history with status and any admin comment.

### 3.7 Approvals & scoring (admin) — **no base points**

`GET /api/admin/submissions?status=pending&team_id=…` — the approvals queue, **filterable by team
using a query parameter (`?team_id=`)** and auto-refreshing. Note: the wrong separator
(`&team_id=`) yields a 404 — validate it.

`POST /api/admin/submissions/{id}/approve` body `{points, comment}`:

- `points` = the checkpoint's **total award, typed by the admin** (accept 0–200; `points` wins,
  legacy `bonus_score` accepted for compatibility). No fixed 10-point base is ever added.
- Concurrency-safe (see 2.8); sets submission `completed`, writes **one** `score_adjustment`
  (`type='checkpoint'`, `reason="Approved CP{n}: {comment}"`), sets progress `completed`, and
  **unlocks the next station** in that team's route.
- **If the checkpoint is `is_final`:** approval sets the team `finished`, `completed_at = now` and
  freezes `final_elapsed_seconds` — **the timer stops on approval of the final mission**.

`POST /api/admin/submissions/{id}/reject` body `{comment}`:
submission `rejected`, **score 0**, progress back to `available` (team may retry), and the photo is
**removed from the Hall of Fame**.

`POST /api/admin/submissions/{id}/score` — edit the award/comment **at any time** (pending or
already reviewed, e.g. a correction or an after-the-fact penalty): 0–200, `{reject:true}` to convert
to a rejection. It **updates** the existing team+checkpoint `score_adjustment` row so the total
moves by the delta.

### 3.8 Teams, progress and results views (admin)

- **Race Control dashboard:** race status control, live indicator, counters (total teams, teams
  started, teams finished), per-team release buttons in release order.
- **Teams page:** all teams with status, elapsed time, score, completed count, route, **plus a photo
  gallery** of the teams' approved uploads.
- **Per-team detail page:** route progress, every uploaded photo with the score it earned, running
  totals, elapsed time, manual score adjustment (`POST /api/admin/teams/{team_id}/adjust`,
  `{points, reason}` with type `manual`/`penalty`).
- **Leaderboard (`GET /api/leaderboard`) — ADMIN ONLY, always**, regardless of race status. Teams
  must not see their own score or ranking; placing is announced at prize giving. Sort:
  finished teams first, then score desc, then completed-checkpoint count desc, then earliest
  `completed_at`. Rank is shown only for finished teams.
- **Fastest race times (`GET /api/leaderboard/fastest`) — ADMIN ONLY:** finished teams by
  `final_elapsed_seconds` ascending.
- **Results CSV (`GET /api/admin/results.csv`):** `Rank, Team, Score, Status, Elapsed (s),
  Completed At`, downloaded as `clirace2026_results.csv`.
- **Activity log (`GET /api/admin/audit?limit=200`):** newest-first audit trail with before/after
  JSON and IP.

### 3.9 Hall of Fame (showcase)

- Public page `#/hall` (`GET /api/hall-of-fame`) — **does not require login** (it may be projected
  at the event), listing curated entries with rank (1…**20** max), photo, team name and checkpoint.
- Only submissions with `status == 'completed'` (i.e. an admin-approved photo) can be featured;
  rejected or replaced photos are dropped automatically.
- Admin curation at `#/admin/hall` (`GET /api/admin/hall-of-fame`, plus add/update/remove with
  `rank` 1–20 and a `featured` flag).
- **Implementation trap:** register these routes **after** the shared auth dependency is defined, at
  the end of the app module — defining them before it crashes the app at import (a real outage
  during build).

### 3.10 Settings (key/value, editable)

| Key | Default | Meaning |
|---|---|---|
| `release_interval_minutes` | `5` | Intended stagger between releases (display/advisory only) |
| `leaderboard_visible` | `true` | Legacy flag; leaderboard is admin-only by policy |
| `completion_bonus` | `10,8,5` | 1st/2nd/3rd completion bonus values (awarded manually via score adjustments) |

---

## 4. Accounts required

**12 team leader accounts + 4 admin accounts.** Usernames are fixed (they are printed on the
day-of run sheet); display names are cosmetic.

### 4.1 Admin accounts

| Username | Display name | Role |
|---|---|---|
| `max` | Max | admin |
| `lyn` | Lyn | admin |
| `karina` | Karina | admin |
| `shijie` | Shijie | admin |

### 4.2 Team leader accounts

| Username | Team | Route | Release order |
|---|---|---|---|
| `team1` | Team 1 | Forward | 1 |
| `team2` | Team 2 | Reverse | 2 |
| `team3` | Team 3 | Forward | 3 |
| `team4` | Team 4 | Reverse | 4 |
| `team5` | Team 5 | Forward | 5 |
| `team6` | Team 6 | Reverse | 6 |
| `team7` | Team 7 | Forward | 7 |
| `team8` | Team 8 | Reverse | 8 |
| `team9` | Team 9 | Forward | 9 |
| `team10` | Team 10 | Reverse | 10 |
| `team11` | Team 11 | Forward | 11 |
| `team12` | Team 12 | Reverse | 12 |

### 4.3 Password policy

- **Uniform password per role:** all four admin accounts share one password; all twelve team
  accounts share a different one. Participants type these once, on a phone, under time pressure —
  one memorable, easily-typed value per role is intentional.
- Password **values are not reproduced in this document.** Set them in the seed
  (`seed.py`: `admin_spec` / team creation) using the project's `hash_password()`; they are stored
  hashed and never displayed anywhere in the UI.
- No password-change screen, no self-registration. Team accounts are created with `role =
  'team_leader'` and bound to their `team_id` at seed time.
- Retired accounts must be **deleted** by the seed (e.g. the old `chinchuan` admin) so exactly the
  accounts above can log in.

---

## 5. Content — the 9 wet-weather checkpoints (seed data)

Programme: indoor / sheltered Tanjong Pagar CBD. `default_score` is 10 for all (informational — the
admin types the actual award). `Requirements` is a newline-separated list rendered as bullets. The
`Note` column is the `bonus_description` rendered as "📝 Note:" on the mission card.

| # | Location | Category | Mission title | Mission description | Requirements | Note (bonus_description) | Ref |
|---|---|---|---|---|---|---|---|
| 1 | Capital Tower | Founders' Post | The Startup Founders | Take a selfie at the statue where every team member strikes a different startup founder pose — pitching, coding, presenting, brainstorming. | No two people to have the same pose; Entire team visible; Image must be visible in background | — | cp1.jpg |
| 2 | Fraser Tower Linkway | Package Locker | Package Delivered! | Capture a synchronized jump shot at the Shopee pick-up lockers, as if celebrating a successful delivery. | Entire team visible; Everyone's feet off the ground; Image must be visible in background | Bonus 5 Points: Most synchronised jump. | cp2.jpg |
| 3 | Fraser Tower | The Daily Commute | Rush Hour | Recreate a classic MRT scene at the train seats — dozing commuter, phone scroller, standing passenger, seat offerer. | At least 4 different commuter roles; Entire team visible; Image must be visible in background | — | cp3.jpg |
| 4 | Guoco Tower | Sustainability Champions | Become a Tree | Team members must create a giant tree using their bodies — some as roots, some as trunk, some as branches. | Entire team involved / visible; Tree shape recognisable; Pillar must be visible in background | — | cp4.jpg |
| 5 | Tanjong Pagar MRT | Patriotic Pride | Stand for Singapore | At the tap-in gates, every member strikes a patriotic pose with the mural in the background. | Everyone in a patriotic pose; Entire team visible; Mural must be visible in background | Bonus 2 Points for every random stranger (non-CLI employee) to join in the photo. | cp5.jpg |
| 6 | Springleaf McDonald's | The Greatest Show | Step Right Up! | Create a circus scene — ringmaster, acrobats, clowns, lion tamer, strongman. | Entire team visible; At least 4 different circus roles; McDonald's logo must be visible in background | — | cp6.jpg |
| 7 | Capital Tower | The Touch Network | Connected by Technology | Location clue: whether you're touching lives or pushing limits, this touchpoint is all about making a positive impact. Refer to image. Form a human network by connecting every team member through handshakes, elbows and fingers. | No person standing alone; Entire team physically connected; Image must be visible in background | — | cp7.jpg |
| 8 | Capital Tower | Signal Strength | Human Wifi | Location clue: not quite the top, but definitely above the rest. Find the gateway between levels. Refer to image. Form a giant WiFi symbol with your bodies — arced layers above, one person as the dot at the base. | Entire team involved / visible; WiFi symbol clearly recognisable | — | cp8.jpg |
| 9 | Capital Tower | Final Mission | Mission Accomplished | Recreate a movie poster using AI with your team as the cast. The theme must be influenced by the team's name. | Entire team visible; Dramatic pose; Team name must be reflected in the poster; Lyn must be in the photo | Final mission approval stops the race timer and marks the team finished. | cp9.jpg |

Checkpoints 7 and 8 are **clue-led** (the team must read the "Location Clue" text and match it to
the reference image to work out where to go) — keep the description and the image together.

**Routes (both end on the final mission):**

- **Forward** (odd teams 1,3,5,7,9,11): `1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9`
- **Reverse** (even teams 2,4,6,8,10,12): `8 → 7 → 6 → 5 → 4 → 3 → 2 → 1 → 9`

The reverse route is deliberately **not** the exact mirror: it walks the Capital Tower cluster
(stations 8→1) and finishes on the shared final mission 9, so both routes end together.

---

## 6. API surface (checklist)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/health` | none | Liveness |
| POST | `/api/login` | none | Login → session cookie |
| POST | `/api/logout` | any | Delete session |
| GET | `/api/me` | any | Current user |
| GET | `/api/portal` | team_leader | Team status, **cleared-only** history, server timer |
| GET | `/api/checkpoints/next` | team_leader | The single available mission |
| GET | `/api/checkpoint/{id}` | any | Mission detail (title, description, requirements, note, images) |
| POST | `/api/submit/{checkpoint_id}` | team_leader | Upload proof photo (multipart) |
| GET | `/api/my/submissions` | team_leader | Own upload history |
| POST | `/api/start` | admin | Admin alias (never team-initiated) |
| POST | `/api/admin/teams/{team_id}/start` | admin | **Release** a team (starts its timer) |
| GET/POST | `/api/admin/race/status` | admin | Read/set `not_started`/`live`/`paused`/`completed` |
| GET | `/api/admin/teams` | admin | All teams (status, scores, elapsed) |
| GET | `/api/admin/teams/{team_id}/detail` | admin | One team: progress, photos, scores |
| POST | `/api/admin/teams/{team_id}/adjust` | admin | Manual score adjustment |
| GET | `/api/admin/submissions?status=&team_id=` | admin | Approvals queue (+ team filter) |
| POST | `/api/admin/submissions/{id}/approve` | admin | Approve with typed total points |
| POST | `/api/admin/submissions/{id}/reject` | admin | Reject (0 points, retry allowed) |
| POST | `/api/admin/submissions/{id}/score` | admin | Edit/convert any reviewed photo |
| GET | `/api/leaderboard` | admin | Ranking (finished → score → checkpoints → time) |
| GET | `/api/leaderboard/fastest` | admin | Fastest finished teams |
| GET | `/api/admin/results.csv` | admin | Results export |
| GET | `/api/admin/audit?limit=` | admin | Activity log |
| GET | `/api/hall-of-fame` | none (public) | Public showcase |
| GET/POST | `/api/admin/hall-of-fame` | admin | Curate showcase (rank 1–20, featured) |
| POST | `/api/upload/photo` | admin | Generic processed photo upload |

Static: `GET /static/*` (SPA), `GET /uploads/refs/*`, `GET /uploads/proof/*`.

---

## 7. Acceptance criteria (test before the event)

1. **Cold start:** `GET /api/health` ok; `https://clirace2026.clreport.com` loads the login card.
2. **Login:** wrong password → 401; right password → cookie set and the app lands on the correct
   view without a manual reload; pressing Enter in the password field submits.
3. **Cannot self-start:** a logged-in team sees "Team has not started" and no way to start; with
   the event `not_started`, `POST /api/submit/...` returns **409 "Race is not live"**.
4. **Release:** admin sets Race Control to `live`; releasing Team 1 starts its timer; releasing it
   twice → 409; team sees exactly one available mission.
5. **Anti-peeking:** the team's API responses never contain a locked checkpoint's name, location or
   a route name; the team cannot see how many stops remain.
6. **Photo from camera and from gallery:** both buttons work on a real phone; preview appears;
   submit succeeds; **both inputs are cleared** after submit; a HEIC photo straight from an iPhone
   camera uploads successfully (no 415).
7. **Rotated reference images:** every `uploads/refs/cpN.jpg` displays upright on a phone.
8. **Approval:** admin types e.g. 12 points + comment → team total becomes 12 (no phantom +10);
   next station unlocks; the photo appears in the Hall-of-Fame candidate list.
9. **Double approval:** two admins approving the same photo → second gets 409, score awarded once.
10. **Reject:** score stays 0, station returns to the team, photo disappears from HOF candidates.
11. **Final mission:** approving checkpoint 9 stops the timer (`final_elapsed_seconds` frozen) and
    marks the team `finished`.
12. **Score correction:** re-scoring an approved photo changes the total by the delta only.
13. **Leaderboard privacy:** a team leader calling `/api/leaderboard` gets **403**.
14. **Results:** CSV downloads with rank/score/elapsed; audit log shows login, release, submit,
    approve, reject.
15. **Reset:** re-running the seed returns the event to a clean pre-start state (teams ready, no
    sessions/submissions/scores) without losing checkpoints, routes or accounts.

---

## 8. Deployment & day-of runbook

1. `cd ~/projects/cli-race-2026/backend && ./.venv/bin/python -m app.seed` (initial seed / reset).
2. `launchctl kickstart -k gui/$(id -u)/com.clirace2026.backend`; confirm
   `curl -s localhost:8016/api/health` and the public hostname return 200.
3. Cloudflare tunnel must have the ingress rule → `http://127.0.0.1:8016`. A `1010`/`403` response
   for `curl` (non-browser user agent) is **expected** — test with a real browser.
4. Verify all 9 reference photos in `uploads/refs/` are present, upright and legible on a phone.
5. **Reset before the event** (seed) and again after testing; confirm `event.status =
   not_started` on the morning of the race.
6. Print the run sheet: team logins (12) + admin logins (4) + the release order.
7. During the race: Race Control → set `live` → release teams → work the Approvals queue
   (auto-refreshing, filter by team when a submission is queried) → keep the leaderboard hidden from
   teams → showcase best photos in the Hall of Fame → export the CSV at the end.

---

## 9. Out of scope / deliberate decisions

- No self-registration, no password reset, no email/notifications, no push.
- No in-app score or ranking visibility for teams (prize giving only) — by explicit policy.
- No route/next-station disclosure to teams at any time.
- No auto-advance/stagger enforcement of `release_interval_minutes`; the admin releases manually.
- No third-party services: no cloud DB, no image CDN, no analytics, no mapping.
- Mechanics (approval-unlocks-next, one photo per station, retry on reject, final-mission time stop,
  admin-typed scoring) are fixed and must match the dry-weather instance exactly; only the
  checkpoint content, reference photos and programme branding differ.
