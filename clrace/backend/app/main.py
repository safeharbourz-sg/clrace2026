"""CL Race 2026 — FastAPI backend.

Implements auth (sessions/roles), the core team-leader game API (release,
server timer, checkpoint unlock/secret-word, submission state machine),
photo upload (validate/compress/orient/strip-GPS, thumbnails, replace-before-review),
admin API (concurrency-safe approvals, scoring, leaderboard), results CSV export,
and an audit log.

Run: uvicorn app.main:app --port 8013
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import (Cookie, Depends, FastAPI, File, Form, HTTPException,
                     Query, Request, Response, UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, or_
from sqlmodel import Session, select

from .auth import create_db_session, hash_password, log_audit, verify_password
from .database import get_session, init_db
from .models import (AuditLog, Checkpoint, CheckpointImage, Event, EventMode, Route,
                     RouteCheckpoint, ScoreAdjustment, Session as DBSession, Setting,
                     HallOfFame, Submission, Team, TeamProgress, User, utcnow)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
PROOF_DIR = UPLOAD_DIR / "proof"
REFS_DIR = UPLOAD_DIR / "refs"
PROOF_DIR.mkdir(parents=True, exist_ok=True)

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

COOKIE = "clrace_session"
SESSION_TTL_HOURS = 12
MAX_PHOTO_BYTES = 15 * 1024 * 1024
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="CL Race 2026", version="1.0.0", lifespan=lifespan)

# Serves refs + proof (photos uploaded by players/admin); auth-gated in handlers.
app.mount("/uploads/refs", StaticFiles(directory=str(REFS_DIR)), name="refs")
app.mount("/uploads/proof", StaticFiles(directory=str(PROOF_DIR)), name="proof")

# ---------- single-page frontend (served by backend, same-origin cookies) ----------
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browser tab icon (also requested implicitly at /favicon.ico)."""
    return FileResponse(str(STATIC_DIR / "favicon.ico"),
                        media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=300"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    idx = STATIC_DIR / "index.html"
    if not idx.exists():
        return PlainTextResponse("Frontend not built", status_code=404)
    # Never let a browser cache the SPA shell: JS fixes must reach clients on a normal
    # reload, otherwise stale JS keeps serving an old login flow (heuristic caching).
    return FileResponse(str(idx), headers={"Cache-Control": "no-store, no-cache, must-revalidate",
                                          "Pragma": "no-cache", "Expires": "0"})


# ------------------------------------------------------------------ helpers
def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt) -> str | None:
    """Serialise a naive-UTC datetime with an explicit Z so browsers parse it as UTC.

    All datetimes are stored naive-UTC (see _now). Emitting a bare isoformat made
    JS new Date() treat the string as LOCAL time, which added a constant +8h to
    the client-side race clock (Karina/Lyn: "clock always starts at 08:00:00")."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        # stored naive-UTC -> mark it explicitly as UTC
        return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _current_user(request: Request, db: Session) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Not authenticated")
    s = db.exec(select(DBSession).where(DBSession.token == token)).first()
    if not s or s.expires_at < _now():
        raise HTTPException(401, "Session expired")
    u = db.exec(select(User).where(User.id == s.user_id, User.active == True)).first()  # noqa: E712
    if not u:
        raise HTTPException(401, "Account disabled")
    return u


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")


def _auth_dep(request: Request, db: Session = Depends(get_session)) -> User:
    return _current_user(request, db)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else request.client.host if request.client else ""


# ------------------------------------------------------------------ event state
def _get_event(db: Session) -> Event:
    ev = db.exec(select(Event).order_by(Event.id)).first()
    if ev is None:
        raise HTTPException(500, "No event seeded")
    return ev


def _active_mode(db: Session, ev: Event) -> EventMode:
    if ev.active_event_mode_id:
        m = db.exec(select(EventMode).where(EventMode.id == ev.active_event_mode_id)).first()
        if m:
            return m
    m = db.exec(select(EventMode).where(EventMode.event_id == ev.id, EventMode.active == True)).first()  # noqa: E712
    if not m:
        m = db.exec(select(EventMode).where(EventMode.event_id == ev.id)).first()
    if not m:
        raise HTTPException(500, "No event mode active")
    return m


def _setting(db: Session, key: str, default: str = "") -> str:
    s = db.exec(select(Setting).where(Setting.key == key)).first()
    return s.value if s else default


def _team_route(db: Session, team: Team) -> list[RouteCheckpoint]:
    if not team.route_id:
        return []
    return db.exec(select(RouteCheckpoint).where(RouteCheckpoint.route_id == team.route_id)
                   .order_by(RouteCheckpoint.sequence_number)).all()


# ------------------------------------------------------------------ auth
@app.post("/api/login")
def login(payload: dict, request: Request, db: Session = Depends(get_session)):
    u = db.exec(select(User).where(User.username == payload.get("username", "").strip())).first()
    if not u or not verify_password(payload.get("password", ""), u.password_hash):
        raise HTTPException(401, "Invalid credentials")
    if not u.active:
        raise HTTPException(403, "Account disabled")
    s = create_db_session(db, u.id, _client_ip(request),
                          request.headers.get("user-agent"), SESSION_TTL_HOURS)
    log_audit(db, actor_id=u.id, action="login", entity_type="user", entity_id=u.id, ip=_client_ip(request))
    resp = JSONResponse({"ok": True, "user": _user_pub(u)})
    resp.set_cookie(COOKIE, s.token, httponly=True, samesite="lax", max_age=SESSION_TTL_HOURS * 3600, secure=False)
    return resp


@app.post("/api/logout")
def logout(request: Request, db: Session = Depends(get_session)):
    token = request.cookies.get(COOKIE)
    if token:
        s = db.exec(select(DBSession).where(DBSession.token == token)).first()
        if s:
            db.delete(s)
            db.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/api/me")
def me(user: User = Depends(_auth_dep)):
    return _user_pub(user)


def _user_pub(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "role": u.role, "team_id": u.team_id}


# ------------------------------------------------------------------ photo processing
# HEIC/HEIF (iPhone default camera format) needs an explicit Pillow opener.
# Without this, Image.open() raises UnidentifiedImageError on every iPhone photo —
# this was Karina/Lyn's "cannot upload any photos" bug.
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:  # pragma: no cover - HEIC uploads then fail with a clear 415
    pass


def _process_photo(bytes_: bytes) -> tuple[bytes, bytes]:
    """Validate, auto-orient, strip EXIF/GPS, downscale+compress to JPG; return (main, thumb)."""
    if len(bytes_) > MAX_PHOTO_BYTES:
        raise HTTPException(413, f"Photo too large (max {MAX_PHOTO_BYTES // (1024*1024)}MB)")
    try:
        img = Image.open(io.BytesIO(bytes_))
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise HTTPException(415, "File is not a valid image (JPG, PNG, WebP or HEIC)") from e
    img = ImageOps.exif_transpose(img)  # auto-orient using EXIF, drops EXIF in output
    img = img.convert("RGB")            # strips alpha, strips all metadata on save
    img.thumbnail((1600, 1600))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=82, optimize=True)
    main = out.getvalue()

    t = img.copy()
    t.thumbnail((480, 480))
    tob = io.BytesIO()
    t.save(tob, format="JPEG", quality=70, optimize=True)
    return main, tob.getvalue()


@app.post("/api/upload/photo")
def upload_photo(file: UploadFile = File(...),
                 user: User = Depends(_auth_dep), db: Session = Depends(get_session),
                 request: Request = None):
    """Generic photo upload. Used by admin and (optionally) for future flows."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    raw = file.file.read()
    main, thumb = _process_photo(raw)
    suffix = secrets.token_hex(6)
    fname = f"{suffix}.jpg"
    (PROOF_DIR / fname).write_bytes(main)
    (PROOF_DIR / f"t_{fname}").write_bytes(thumb)
    log_audit(db, actor_id=user.id, action="upload_photo", entity_type="photo", ip=_client_ip(request))
    return {"url": f"/uploads/proof/{fname}", "thumbnail_url": f"/uploads/proof/t_{fname}"}


# ------------------------------------------------------------------ team leader: portal + game API
@app.get("/api/portal")
def portal(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    """Aggregated view for a team leader: status, route, active/final checkpoint, timer."""
    ev = _get_event(db)
    mode = _active_mode(db, ev)
    if user.role != "team_leader" or not user.team_id:
        raise HTTPException(403, "Team Leader only")
    team = db.exec(select(Team).where(Team.id == user.team_id)).first()
    if not team:
        raise HTTPException(404, "Team not found")

    route = _team_route(db, team)
    cps_by_id = {c.id: c for c in db.exec(select(Checkpoint).where(Checkpoint.event_mode_id == mode.id)).all()}
    progress = db.exec(select(TeamProgress).where(TeamProgress.team_id == team.id)).all()
    prog_by_cp = {p.checkpoint_id: p for p in progress}
    if team.route_id and not progress:
        for rc in route:
            db.add(TeamProgress(team_id=team.id, checkpoint_id=rc.checkpoint_id,
                                sequence_position=rc.sequence_number, status="locked"))
        db.commit()

    seq = []
    for rc in route:
        c = cps_by_id.get(rc.checkpoint_id)
        if not c:
            continue
        p = prog_by_cp.get(c.id)
        st = p.status if p else "locked"
        # Karina: "Remove progress tab - we do not want the teams to see their
        # route/next station." For non-admins we send ONLY checkpoints the team has
        # already cleared; locked and currently-available stops are omitted, so the
        # next station cannot be inferred from position or count.
        if user.role != "admin" and st not in ("completed", "rejected", "replaced"):
            continue
        seq.append({"checkpoint_number": c.checkpoint_number, "location": c.location,
                    "sequence": rc.sequence_number, "status": st,
                    "is_final": c.is_final})

    # server-side timer = elapsed from team.started_at
    elapsed = None
    if team.started_at:
        now = _now()
        if team.completed_at:
            elapsed = int((team.completed_at - team.started_at).total_seconds())
        else:
            elapsed = max(0, int((now - team.started_at).total_seconds()))

    return {
        "event": ev.name,
        "mode": mode.name,
        "status": ev.status,
        "team": {"id": team.id, "name": team.name, "status": team.status,
                 "route": (db.exec(select(Route).where(Route.id == team.route_id)).first().name
                           if team.route_id and user.role == "admin" else "")},
        # same rule as seq above: locked/next stops never leave the server for teams
        "route": seq,
        "timer_elapsed_seconds": elapsed,
        "started_at": _iso(team.started_at),
        "completed_at": _iso(team.completed_at),
        "release_order": team.release_order,
    }


@app.get("/api/checkpoint/{checkpoint_id}")
def checkpoint_detail(checkpoint_id: int, user: User = Depends(_auth_dep),
                      db: Session = Depends(get_session), request: Request = None):
    ev = _get_event(db)
    mode = _active_mode(db, ev)
    cp = db.exec(select(Checkpoint).where(Checkpoint.id == checkpoint_id,
                                          Checkpoint.event_mode_id == mode.id)).first()
    if not cp:
        raise HTTPException(404, "Checkpoint not found")
    user_team = []
    if user.role == "team_leader" and user.team_id:
        team = db.exec(select(Team).where(Team.id == user.team_id)).first()
        if team and team.route_id:
            rc = db.exec(select(RouteCheckpoint).where(RouteCheckpoint.route_id == team.route_id,
                                                       RouteCheckpoint.checkpoint_id == cp.id)).first()
            if rc:
                user_team = [{"id": team.id, "name": team.name, "released": _team_released(team)}]
    imgs = db.exec(select(CheckpointImage).where(CheckpointImage.checkpoint_id == cp.id)).all()
    return {
        "id": cp.id, "number": cp.checkpoint_number, "location": cp.location, "category": cp.category,
        "title": cp.mission_title, "description": cp.mission_description,
        "requirements": [r for r in cp.requirements.split("\n") if r.strip()],
        "bonus_description": cp.bonus_description, "default_score": cp.default_score, "is_final": cp.is_final,
        "images": [{"url": i.image_url, "thumbnail_url": i.thumbnail_url} for i in imgs],
        "unlocked": bool(user_team),
    }


def _team_released(team: Team) -> bool:
    return team.started_at is not None


@app.post("/api/start")
def start_team(user: User = Depends(_auth_dep), db: Session = Depends(get_session),
               request: Request = None):
    """Teams do NOT start themselves. The Game Admin releases each team from
    Race Control -> "Race Start (per team)" -> Release, which starts its timer.
    Kept only as an admin alias (team taken from the admin's own session)."""
    _require_admin(user)
    team = db.exec(select(Team).where(Team.id == user.team_id)).first() if user.team_id else None
    if not team:
        raise HTTPException(400, "Releasing is per team: POST /api/admin/teams/{team_id}/start")
    return admin_start_team(team.id, user=user, db=db, request=request)


@app.post("/api/admin/teams/{team_id}/start")
def admin_start_team(team_id: int, user: User = Depends(_auth_dep),
                     db: Session = Depends(get_session), request: Request = None):
    """Release a team's race timer (admin only), bypassing the release-order gate."""
    _require_admin(user)
    ev = _get_event(db)
    if ev.status != "live":
        raise HTTPException(409, "Race is not live")
    team = db.exec(select(Team).where(Team.id == team_id)).first()
    if not team:
        raise HTTPException(404, "Team not found")
    if team.started_at:
        raise HTTPException(409, "Team already started")
    team.started_at = _now()
    team.status = "racing"
    route = _team_route(db, team)
    for rc in route:
        existing = db.exec(select(TeamProgress).where(
            TeamProgress.team_id == team.id,
            TeamProgress.checkpoint_id == rc.checkpoint_id)).first()
        if not existing:
            db.add(TeamProgress(team_id=team.id, checkpoint_id=rc.checkpoint_id,
                                status="locked", unlocked_at=None))
    db.commit()
    route = _team_route(db, team)
    first = route[0] if route else None
    if first:
        p = db.exec(select(TeamProgress).where(TeamProgress.team_id == team.id,
                                               TeamProgress.checkpoint_id == first.checkpoint_id)).first()
        if p:
            p.status = "available"
            p.unlocked_at = _now()
    team.updated_at = _now()
    db.commit()
    log_audit(db, actor_id=user.id, action="team_started", entity_type="team", entity_id=team.id,
              after={"started_at": _iso(team.started_at), "by_admin": True}, ip=_client_ip(request))
    return {"ok": True, "started_at": _iso(team.started_at)}


@app.get("/api/checkpoints/next")
def next_checkpoint(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    """Return the single unlocked 'available' checkpoint the leader must solve now."""
    ev = _get_event(db)
    mode = _active_mode(db, ev)
    team = db.exec(select(Team).where(Team.id == user.team_id)).first() if user.team_id else None
    if not team or user.role != "team_leader":
        raise HTTPException(403, "Team Leader only")
    if not team.started_at:
        return {"available": False, "checkpoint": None, "message": "Team has not started"}
    progress = db.exec(select(TeamProgress).where(TeamProgress.team_id == team.id,
                                                 TeamProgress.status == "available")).all()
    pending = db.exec(select(TeamProgress).where(TeamProgress.team_id == team.id,
                                                 TeamProgress.status == "pending")).all()
    if pending:
        return {"available": False, "checkpoint": None,
                "message": "Awaiting review of previous submission"}
    if not progress:
        # all done?
        completed = db.exec(select(func.count()).select_from(TeamProgress).where(
            TeamProgress.team_id == team.id, TeamProgress.status == "completed")).one() or 0
        route = _team_route(db, team)
        if route and completed >= len(route):
            return {"available": False, "checkpoint": None, "message": "FINISHED"}
        return {"available": False, "checkpoint": None, "message": "No active checkpoint"}
    cp = db.exec(select(Checkpoint).where(Checkpoint.id == progress[0].checkpoint_id)).first()
    return {"available": True, "checkpoint": _cp_pub(cp, db), "progress_id": progress[0].id}


def _cp_pub(cp: Checkpoint, db: Session) -> dict:
    imgs = db.exec(select(CheckpointImage).where(CheckpointImage.checkpoint_id == cp.id)).all()
    return {"id": cp.id, "number": cp.checkpoint_number, "location": cp.location, "category": cp.category,
            "title": cp.mission_title, "description": cp.mission_description,
            "requirements": [r for r in cp.requirements.split("\n") if r.strip()],
            "bonus_description": cp.bonus_description, "default_score": cp.default_score, "is_final": cp.is_final,
            "images": [{"url": i.image_url, "thumbnail_url": i.thumbnail_url} for i in imgs]}


# ------------------------------------------------------------------ photo submission (team leader)
@app.post("/api/submit/{checkpoint_id}")
async def submit(checkpoint_id: int, file: UploadFile = File(...),
                 admin_note: str = Form(""),
                 user: User = Depends(_auth_dep), db: Session = Depends(get_session),
                 request: Request = None):
    ev = _get_event(db)
    if ev.status != "live":
        raise HTTPException(409, "Race is not live")
    team = db.exec(select(Team).where(Team.id == user.team_id)).first() if user.team_id else None
    if user.role != "team_leader" or not team:
        raise HTTPException(403, "Team Leader only")
    if not team.started_at:
        raise HTTPException(409, "Team has not started")
    mode = _active_mode(db, ev)
    cp = db.exec(select(Checkpoint).where(Checkpoint.id == checkpoint_id,
                                          Checkpoint.event_mode_id == mode.id)).first()
    if not cp:
        raise HTTPException(404, "Checkpoint not found")

    # must be the currently available checkpoint
    prog = db.exec(select(TeamProgress).where(TeamProgress.team_id == team.id,
                                              TeamProgress.checkpoint_id == cp.id)).first()
    if not prog or prog.status not in ("available", "pending"):
        raise HTTPException(409, "This checkpoint is not currently active for your team")

    raw = await file.read()
    main, thumb = _process_photo(raw)

    # replace-before-review: if the previous submission is still pending, replace it
    prev = db.exec(select(Submission).where(Submission.team_id == team.id,
                                            Submission.checkpoint_id == cp.id,
                                            Submission.status == "pending")).first()
    if prev:
        # mark replaced, keep for audit, delete files
        prev.is_replaced = True
        prev.status = "replaced"
        old_path = Path(BASE_DIR / prev.photo_url.lstrip("/"))
        if old_path.exists():
            old_path.unlink()
        t_old = Path(BASE_DIR / prev.thumbnail_url.lstrip("/"))
        if t_old.exists():
            t_old.unlink()

    suffix = secrets.token_hex(6)
    fname = f"t{team.id}_c{cp.id}_{suffix}.jpg"
    (PROOF_DIR / fname).write_bytes(main)
    (PROOF_DIR / f"t_{fname}").write_bytes(thumb)

    attempt = (db.exec(select(func.count()).select_from(Submission).where(
        Submission.team_id == team.id, Submission.checkpoint_id == cp.id)).one() or 0) + 1

    s = Submission(team_id=team.id, checkpoint_id=cp.id,
                   photo_url=f"/uploads/proof/{fname}", thumbnail_url=f"/uploads/proof/t_{fname}",
                   attempt_number=attempt, status="pending", submitted_at=_now(),
                   base_score=0, admin_comment=admin_note)
    db.add(s)
    prog.status = "pending"
    db.commit()
    log_audit(db, actor_id=user.id, action="submission", entity_type="submission",
              entity_id=s.id, after={"checkpoint_id": cp.id, "attempt": attempt}, ip=_client_ip(request))
    db.expire_all()
    return {"ok": True, "submission_id": s.id, "status": "pending"}


@app.get("/api/my/submissions")
def my_submissions(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    """Team Leader's own submission history: what was uploaded, status, score, admin reason."""
    if user.role != "team_leader" or not user.team_id:
        raise HTTPException(403, "Team Leader only")
    q = (select(Submission).where(Submission.team_id == user.team_id)
         .order_by(Submission.submitted_at.desc()))
    subs = db.exec(q).all()
    out = []
    for s in subs:
        cp = db.exec(select(Checkpoint).where(Checkpoint.id == s.checkpoint_id)).first()
        out.append({
            "id": s.id, "checkpoint_number": cp.checkpoint_number if cp else None,
            "checkpoint": cp.mission_title if cp else "", "location": cp.location if cp else "",
            "status": s.status, "thumbnail_url": s.thumbnail_url, "photo_url": s.photo_url,
            "attempt_number": s.attempt_number, "submitted_at": _iso(s.submitted_at),
            # No base/bonus/total score here on purpose: the team leader view must not
            # reveal scores before prize giving (Lyn, 6 Oct 2026).
            "reason": s.admin_comment, "reviewed_at": _iso(s.reviewed_at)})
    return {"submissions": out}


# ------------------------------------------------------------------ admin: game control
@app.get("/api/admin/race/status")
def get_race_status(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    _require_admin(user)
    ev = _get_event(db)
    return {"status": ev.status, "release_interval_minutes": getattr(ev, "release_interval_minutes", None)}


@app.post("/api/admin/race/status")
def set_race_status(payload: dict, user: User = Depends(_auth_dep),
                    db: Session = Depends(get_session), request: Request = None):
    _require_admin(user)
    st = payload.get("status")
    if st not in ("not_started", "live", "paused", "completed"):
        raise HTTPException(400, "Invalid status")
    ev = _get_event(db)
    before = ev.status
    ev.status = st
    ev.updated_at = _now()
    db.commit()
    log_audit(db, actor_id=user.id, action="set_race_status", entity_type="event", entity_id=ev.id,
              before={"status": before}, after={"status": st}, ip=_client_ip(request))
    return {"ok": True, "status": ev.status}


@app.get("/api/admin/teams")
def admin_teams(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    _require_admin(user)
    teams = db.exec(select(Team).order_by(Team.release_order)).all()
    return {"teams": [_team_admin(t, db) for t in teams]}


def _team_elapsed(t: Team) -> Optional[int]:
    """Race time for a team: frozen once finished, otherwise ticking from started_at."""
    if t.final_elapsed_seconds is not None:
        return t.final_elapsed_seconds
    if t.started_at and t.completed_at:
        return max(0, int((t.completed_at - t.started_at).total_seconds()))
    if t.started_at:
        return max(0, int((_now() - t.started_at).total_seconds()))
    return None


def _team_admin(t: Team, db: Session) -> dict:
    rname = ""
    if t.route_id:
        r = db.exec(select(Route).where(Route.id == t.route_id)).first()
        rname = r.name if r else ""
    return {"id": t.id, "name": t.name, "route": rname, "release_order": t.release_order,
            "status": t.status, "started_at": _iso(t.started_at),
            "elapsed_seconds": _team_elapsed(t),
            "running": bool(t.started_at and not t.completed_at),
            "completed_at": _iso(t.completed_at)}


@app.get("/api/admin/checkpoints")
def admin_checkpoints(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    _require_admin(user)
    mode = _active_mode(db, _get_event(db))
    cps = db.exec(select(Checkpoint).where(Checkpoint.event_mode_id == mode.id)
                  .order_by(Checkpoint.checkpoint_number)).all()
    return {"checkpoints": [_cp_pub(c, db) for c in cps]}


@app.get("/api/admin/submissions")
def admin_submissions(status: Optional[str] = None, team_id: Optional[int] = None,
                      user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    _require_admin(user)
    q = select(Submission).order_by(Submission.submitted_at.desc())
    if status:
        q = q.where(Submission.status == status)
    if team_id:
        q = q.where(Submission.team_id == team_id)
    subs = db.exec(q).all()
    out = []
    for s in subs:
        t = db.exec(select(Team).where(Team.id == s.team_id)).first()
        cp = db.exec(select(Checkpoint).where(Checkpoint.id == s.checkpoint_id)).first()
        out.append({"id": s.id, "team": t.name if t else "", "team_id": s.team_id,
                    "checkpoint": cp.mission_title if cp else "", "checkpoint_id": s.checkpoint_id,
                    "checkpoint_number": cp.checkpoint_number if cp else None,
                    "status": s.status, "photo_url": s.photo_url, "thumbnail_url": s.thumbnail_url,
                    "attempt_number": s.attempt_number, "submitted_at": _iso(s.submitted_at),
                    "base_score": s.base_score, "bonus_score": s.bonus_score, "total_score": s.total_score,
                    "admin_comment": s.admin_comment, "reviewed_at": _iso(s.reviewed_at)})
    return {"submissions": out}


@app.post("/api/admin/submissions/{submission_id}/approve")
def approve_submission(submission_id: int, payload: dict,
                       user: User = Depends(_auth_dep), db: Session = Depends(get_session),
                       request: Request = None):
    """Concurrency-safe approval: only pending submissions accepted; the admin types the
    checkpoint's total award (Lyn: no base points, admin allocates)."""
    _require_admin(user)
    s = db.exec(select(Submission).where(Submission.id == submission_id)).first()
    if not s:
        raise HTTPException(404, "Submission not found")
    bonus = int(payload.get("bonus_score", 0) or 0)
    comment = payload.get("comment", "")

    # Lyn: "No base point, Admin will allocate the points accordingly." The admin
    # now types the checkpoint's TOTAL award; there is no fixed 10-point base.
    # `points` (new UI) wins; `bonus_score` is accepted for backwards compatibility.
    points = payload.get("points")
    if points is None:
        points = bonus
    points = int(points or 0)

    # optimistic concurrency guard: BEGIN IMMEDIATE gives SQLite a write lock;
    # if another admin already approved, the status check inside the txn fails.
    from sqlalchemy import text
    db.exec(text("BEGIN IMMEDIATE"))
    try:
        s2 = db.exec(select(Submission).where(Submission.id == submission_id)).first()
        if s2.status != "pending":
            db.commit()
            raise HTTPException(409, "Submission already reviewed")
        s2.status = "completed"
        s2.bonus_score = 0
        s2.total_score = points
        s2.admin_comment = comment
        s2.reviewed_at = _now()
        s2.reviewed_by = user.id

        # update team progress -> completed
        prog = db.exec(select(TeamProgress).where(TeamProgress.team_id == s.team_id,
                                                  TeamProgress.checkpoint_id == s.checkpoint_id)).first()
        if prog:
            prog.status = "completed"
            prog.completed_at = _now()
        team = db.exec(select(Team).where(Team.id == s.team_id)).first()
        # award the admin-entered checkpoint points as its own auditable component
        db.add(ScoreAdjustment(team_id=s.team_id, adjustment_type="checkpoint",
                               points=s.total_score, reason=f"Approved CP{s.checkpoint_id}: {comment or 'mission'}",
                               created_by=user.id))
        # unlock next checkpoint
        route = _team_route(db, team) if team else []
        rc_list = [rc.checkpoint_id for rc in route]
        if s.checkpoint_id in rc_list:
            idx = rc_list.index(s.checkpoint_id)
            if idx + 1 < len(rc_list):
                nxt = db.exec(select(TeamProgress).where(TeamProgress.team_id == s.team_id,
                                                         TeamProgress.checkpoint_id == rc_list[idx + 1])).first()
                if nxt:
                    nxt.status = "available"
                    nxt.unlocked_at = _now()
        # if final mission -> finish team + stop timer. No points are awarded here:
        # Lyn: "no base points... Admin will allocate the points accordingly."
        cp = db.exec(select(Checkpoint).where(Checkpoint.id == s.checkpoint_id)).first()
        if cp and cp.is_final and team:
            team.status = "finished"
            team.completed_at = _now()
            if team.started_at:
                team.final_elapsed_seconds = int((team.completed_at - team.started_at).total_seconds())
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        raise HTTPException(500, "Unable to approve; concurrent modification")

    log_audit(db, actor_id=user.id, action="approve", entity_type="submission",
              entity_id=s.id, before={"status": "pending"},
              after={"status": "completed", "total_score": s.total_score, "points": points}, ip=_client_ip(request))
    return {"ok": True, "total_score": s.total_score}


@app.post("/api/admin/submissions/{submission_id}/reject")
def reject_submission(submission_id: int, payload: dict,
                      user: User = Depends(_auth_dep), db: Session = Depends(get_session),
                      request: Request = None):
    _require_admin(user)
    s = db.exec(select(Submission).where(Submission.id == submission_id)).first()
    if not s:
        raise HTTPException(404, "Submission not found")
    comment = payload.get("comment", "")
    if s.status != "pending":
        raise HTTPException(409, "Submission already reviewed")
    s.status = "rejected"
    s.admin_comment = comment
    s.bonus_score = 0
    s.total_score = 0  # rejected submissions earn no points
    s.reviewed_at = _now()
    s.reviewed_by = user.id
    prog = db.exec(select(TeamProgress).where(TeamProgress.team_id == s.team_id,
                                              TeamProgress.checkpoint_id == s.checkpoint_id)).first()
    if prog:
        prog.status = "available"  # back to available so team can retry
    _hof_drop(db, s.id)  # a rejected photo must not stay in the showcase
    db.commit()
    log_audit(db, actor_id=user.id, action="reject", entity_type="submission", entity_id=s.id,
              after={"status": "rejected", "comment": comment}, ip=_client_ip(request))
    return {"ok": True}


# ------------------------------------------------------------------ scoring + leaderboard
def _team_score(db: Session, team_id: int) -> int:
    """Sum of all score adjustments for a team (checkpoint + completion + time + manual + penalty)."""
    total = 0
    for a in db.exec(select(ScoreAdjustment).where(ScoreAdjustment.team_id == team_id)).all():
        total += a.points
    return total


def _completed_count(db: Session, team_id: int) -> int:
    return db.exec(select(func.count()).select_from(TeamProgress).where(
        TeamProgress.team_id == team_id, TeamProgress.status == "completed")).one() or 0


# ------------------------------------------- admin: per-team photo / score review
def _sub_admin(s: Submission, db: Session) -> dict:
    t = db.exec(select(Team).where(Team.id == s.team_id)).first()
    cp = db.exec(select(Checkpoint).where(Checkpoint.id == s.checkpoint_id)).first()
    return {"id": s.id, "team": t.name if t else "", "team_id": s.team_id,
            "checkpoint": cp.mission_title if cp else "", "checkpoint_id": s.checkpoint_id,
            "checkpoint_number": cp.checkpoint_number if cp else None,
            "is_final": bool(cp.is_final) if cp else False,
            "status": s.status, "photo_url": s.photo_url, "thumbnail_url": s.thumbnail_url,
            "attempt_number": s.attempt_number, "submitted_at": _iso(s.submitted_at),
            "base_score": s.base_score, "bonus_score": s.bonus_score, "total_score": s.total_score,
            "admin_comment": s.admin_comment, "reviewed_at": _iso(s.reviewed_at)}


@app.get("/api/admin/teams/{team_id}/detail")
def admin_team_detail(team_id: int, user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    """Everything the admin needs on one page: route progress, every uploaded photo,
    the score each photo earned, and the team's running totals."""
    _require_admin(user)
    t = db.exec(select(Team).where(Team.id == team_id)).first()
    if not t:
        raise HTTPException(404, "Team not found")
    rname = ""
    if t.route_id:
        r = db.exec(select(Route).where(Route.id == t.route_id)).first()
        rname = r.name if r else ""
    cps = []
    route = _team_route(db, t)
    for rc in route:
        cp = db.exec(select(Checkpoint).where(Checkpoint.id == rc.checkpoint_id)).first()
        prog = db.exec(select(TeamProgress).where(TeamProgress.team_id == t.id,
                                                  TeamProgress.checkpoint_id == rc.checkpoint_id)).first()
        cps.append({"checkpoint_id": rc.checkpoint_id,
                    "number": cp.checkpoint_number if cp else None,
                    "title": cp.mission_title if cp else "",
                    "location": cp.location if cp else "",
                    "is_final": bool(cp.is_final) if cp else False,
                    "default_score": cp.default_score if cp else 0,
                    "progress": prog.status if prog else "locked",
                    "unlocked_at": _iso(prog.unlocked_at) if prog else None,
                    "completed_at": _iso(prog.completed_at) if prog else None})
    subs = db.exec(select(Submission).where(Submission.team_id == t.id)
                   .order_by(Submission.submitted_at)).all()
    adj = db.exec(select(ScoreAdjustment).where(ScoreAdjustment.team_id == t.id)
                  .order_by(ScoreAdjustment.id)).all()
    return {
        "team": {"id": t.id, "name": t.name, "route": rname, "release_order": t.release_order,
                 "status": t.status, "started_at": _iso(t.started_at),
                 "elapsed_seconds": _team_elapsed(t),
                 "running": bool(t.started_at and not t.completed_at),
                 "completed_at": _iso(t.completed_at)},
        "score": _team_score(db, t.id),
        "checkpoints": cps,
        "submissions": [_sub_admin(s, db) for s in subs if not s.is_replaced],
        "adjustments": [{"id": a.id, "type": a.adjustment_type, "points": a.points,
                         "reason": a.reason, "created_at": _iso(a.created_at)} for a in adj],
    }


@app.post("/api/admin/submissions/{submission_id}/score")
def score_submission(submission_id: int, payload: dict,
                     user: User = Depends(_auth_dep), db: Session = Depends(get_session),
                     request: Request = None):
    """Admin edits the award and/or comment of an uploaded photo, at any time.

    Works on pending photos (first approval) and on already-reviewed ones (score correction).
    The single ScoreAdjustment row approval created for this team+checkpoint is UPDATED so the
    team's total moves by the delta instead of being awarded twice."""
    _require_admin(user)
    s = db.exec(select(Submission).where(Submission.id == submission_id)).first()
    if not s:
        raise HTTPException(404, "Submission not found")
    if s.is_replaced or s.status == "replaced":
        raise HTTPException(409, "Submission was replaced before review")
    reject = bool(payload.get("reject"))
    raw_points = payload.get("points")
    if reject:
        points = 0
    else:
        points = int(raw_points if raw_points is not None else (s.total_score or 0))
    if points < 0 or points > 200:
        raise HTTPException(400, "Points must be between 0 and 200")
    comment = payload.get("comment")
    if comment is None:
        comment = s.admin_comment or ""
    comment = str(comment)
    was_pending = s.status == "pending"
    before = {"status": s.status, "total_score": s.total_score, "admin_comment": s.admin_comment}

    adj = db.exec(select(ScoreAdjustment).where(
        ScoreAdjustment.team_id == s.team_id,
        ScoreAdjustment.adjustment_type == "checkpoint",
        ScoreAdjustment.reason.like(f"%CP{s.checkpoint_id}:%")).order_by(ScoreAdjustment.id)).first()

    s.base_score = 0
    s.bonus_score = 0
    s.total_score = points
    s.admin_comment = comment
    s.reviewed_at = _now()
    s.reviewed_by = user.id
    s.status = "rejected" if reject else "completed"
    reason = (f"Rejected CP{s.checkpoint_id}: {comment or 'no reason'}" if reject
              else f"Approved CP{s.checkpoint_id}: {comment or 'mission'}")
    if adj:
        adj.points = points
        adj.reason = reason
        adj.created_by = user.id
    else:
        db.add(ScoreAdjustment(team_id=s.team_id, adjustment_type="checkpoint",
                               points=points, reason=reason, created_by=user.id))

    team = db.exec(select(Team).where(Team.id == s.team_id)).first()
    cp = db.exec(select(Checkpoint).where(Checkpoint.id == s.checkpoint_id)).first()
    prog = db.exec(select(TeamProgress).where(TeamProgress.team_id == s.team_id,
                                              TeamProgress.checkpoint_id == s.checkpoint_id)).first()
    if reject:
        _hof_drop(db, s.id)  # a rejected photo must not stay in the showcase
        if prog:
            prog.status = "available"   # back to the team so they can retry
            prog.completed_at = None
    else:
        if prog:
            prog.status = "completed"
            prog.completed_at = prog.completed_at or _now()
        if was_pending and team:
            rc_list = [rc.checkpoint_id for rc in _team_route(db, team)]
            if s.checkpoint_id in rc_list:
                idx = rc_list.index(s.checkpoint_id)
                if idx + 1 < len(rc_list):
                    nxt = db.exec(select(TeamProgress).where(TeamProgress.team_id == s.team_id,
                                                             TeamProgress.checkpoint_id == rc_list[idx + 1])).first()
                    if nxt and nxt.status == "locked":
                        nxt.status = "available"
                        nxt.unlocked_at = _now()
        if cp and cp.is_final and team and team.status != "finished":
            team.status = "finished"
            team.completed_at = _now()
            if team.started_at:
                team.final_elapsed_seconds = int((team.completed_at - team.started_at).total_seconds())
    db.commit()

    log_audit(db, actor_id=user.id, action=("reject" if reject else "score_edit"),
              entity_type="submission", entity_id=s.id, before=before,
              after={"status": s.status, "total_score": s.total_score,
                     "admin_comment": s.admin_comment, "points": points},
              ip=_client_ip(request))
    return {"ok": True, "total_score": s.total_score, "status": s.status,
            "team_score": _team_score(db, s.team_id)}


@app.get("/api/leaderboard")
def leaderboard(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    ev = _get_event(db)
    # Lyn: "We do not want the teams to view their final scores in the portal.
    # Ranking will only be shared at the end of the race during prize giving."
    # => the leaderboard is admin-only for the whole race, regardless of status.
    if user.role != "admin":
        raise HTTPException(403, "Leaderboard is admin only")
    teams = db.exec(select(Team).where(Team.active == True)).all()  # noqa: E712
    rows = []
    for t in teams:
        rows.append({
            "id": t.id, "name": t.name, "score": _team_score(db, t.id),
            "completed": _completed_count(db, t.id), "status": t.status,
            # elapsed: frozen final time for finished teams, "now - started_at" while racing
            "elapsed_seconds": _team_elapsed(t),
            "running": bool(t.started_at and not t.completed_at),
            "started_at": _iso(t.started_at),
            "completed_at": _iso(t.completed_at),
        })
    # Guide §10.1: Overall by score desc, then more completed checkpoints,
    # then (if finished) earlier completed_at. Finished teams rank ahead of unfinished.
    rows.sort(key=lambda r: (
        0 if r["status"] == "finished" else 1,          # finished first
        -r["score"],                                     # total score desc
        -r["completed"],                                 # more checkpoints first
        r["completed_at"] or "9999-12-31T00:00:00",       # earlier finish first
    ))
    for i, r in enumerate(rows, 1):
        r["rank"] = i if r["status"] == "finished" else None
    return {"leaderboard": rows}


@app.get("/api/leaderboard/fastest")
def leaderboard_fastest(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    """Completed teams ranked by elapsed time ascending (guide §10.1 Fastest Race Times)."""
    # Lyn: ranking is only revealed at prize giving -> admin only, like the main leaderboard.
    if user.role != "admin":
        raise HTTPException(403, "Leaderboard is admin only")
    teams = db.exec(select(Team).where(Team.status == "finished")).all()
    rows = []
    for t in teams:
        elapsed = t.final_elapsed_seconds
        if elapsed is None and t.started_at and t.completed_at:
            elapsed = int((t.completed_at - t.started_at).total_seconds())
        rows.append({"id": t.id, "name": t.name, "elapsed_seconds": elapsed,
                     "completed_at": _iso(t.completed_at),
                     "score": _team_score(db, t.id)})
    rows.sort(key=lambda r: r["elapsed_seconds"] if r["elapsed_seconds"] is not None else 10 ** 9)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return {"fastest": rows}


@app.post("/api/admin/teams/{team_id}/adjust")
def adjust_score(team_id: int, payload: dict, user: User = Depends(_auth_dep),
                 db: Session = Depends(get_session), request: Request = None):
    """Manual bonus (+) or penalty (-) with required reason (guide §10 components 5/6)."""
    _require_admin(user)
    team = db.exec(select(Team).where(Team.id == team_id)).first()
    if not team:
        raise HTTPException(404, "Team not found")
    points = int(payload.get("points", 0))
    reason = (payload.get("reason", "") or "").strip()
    if not reason:
        raise HTTPException(400, "A reason is required for score adjustment")
    adj_type = "penalty" if points < 0 else "manual_bonus"
    db.add(ScoreAdjustment(team_id=team_id, adjustment_type=adj_type, points=points,
                           reason=reason, created_by=user.id))
    db.commit()
    log_audit(db, actor_id=user.id, action="adjust_score", entity_type="team", entity_id=team_id,
              after={"points": points, "reason": reason, "type": adj_type}, ip=_client_ip(request))
    return {"ok": True, "score": _team_score(db, team_id)}


# ------------------------------------------------------------------ results CSV export
@app.get("/api/admin/results.csv")
def results_csv(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    _require_admin(user)
    teams = db.exec(select(Team).where(Team.active == True)).all()  # noqa: E712
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Rank", "Team", "Score", "Status", "Elapsed (s)", "Completed At"])
    rows = []
    for t in teams:
        rows.append({"name": t.name, "score": _team_score(db, t.id), "status": t.status,
                     "elapsed": t.final_elapsed_seconds if t.final_elapsed_seconds is not None else "",
                     "completed_at": (_iso(t.completed_at) or ""), "rank": ""})
    rows.sort(key=lambda r: (0 if r["status"] == "finished" else 1, -r["score"], str(r["completed_at"])))
    for i, r in enumerate(rows, 1):
        if r["status"] == "finished":
            r["rank"] = i
    for r in rows:
        w.writerow([r["rank"], r["name"], r["score"], r["status"], r["elapsed"], r["completed_at"]])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=clrace2026_results.csv"})


# ------------------------------------------------------------------ audit log
@app.get("/api/admin/audit")
def audit_log_view(limit: int = 200, user: User = Depends(_auth_dep),
                   db: Session = Depends(get_session)):
    _require_admin(user)
    logs = db.exec(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {"audit": [{"id": l.id, "user_id": l.user_id, "action": l.action, "entity_type": l.entity_type,
                       "entity_id": l.entity_id, "before": json.loads(l.before_json) if l.before_json else None,
                       "after": json.loads(l.after_json) if l.after_json else None,
                       "ip": l.ip_address, "created_at": _iso(l.created_at)} for l in logs]}


@app.get("/api/health")
def health():
    return {"ok": True, "service": "cl-race-2026", "time": _now().isoformat()}


# ------------------------------------------------------------------ hall of fame
HOF_MAX_RANK = 20


def _hof_drop(db: Session, submission_id: int) -> None:
    """A photo that is no longer approved must never stay in the showcase."""
    row = db.exec(select(HallOfFame).where(HallOfFame.submission_id == submission_id)).first()
    if row:
        db.delete(row)


def _hof_json(db: Session, s: Submission, rank: Optional[int] = None,
              headline: str = "", featured_at=None) -> dict:
    team = db.exec(select(Team).where(Team.id == s.team_id)).first()
    cp = db.exec(select(Checkpoint).where(Checkpoint.id == s.checkpoint_id)).first()
    return {
        "submission_id": s.id,
        "rank": rank,
        "featured": rank is not None,
        "headline": headline or "",
        "team_id": s.team_id,
        "team": team.name if team else f"team{s.team_id}",
        "checkpoint_id": s.checkpoint_id,
        "checkpoint": cp.mission_title if cp else "",
        "checkpoint_number": cp.checkpoint_number if cp else None,
        "photo_url": s.photo_url,
        "thumbnail_url": s.thumbnail_url or s.photo_url,
        "total_score": s.total_score,
        "attempt_number": s.attempt_number,
        "submitted_at": _iso(s.submitted_at),
        "featured_at": _iso(featured_at),
    }


@app.get("/api/admin/hall-of-fame")
def admin_hall_of_fame(user: User = Depends(_auth_dep), db: Session = Depends(get_session)):
    """Every approved photo is a candidate; the featured ones carry their rank."""
    _require_admin(user)
    ranks = {r.submission_id: r for r in db.exec(select(HallOfFame)).all()}
    items = []
    approved = db.exec(select(Submission).where(Submission.status == "completed")
                       .order_by(Submission.submitted_at.desc())).all()
    for s in approved:
        r = ranks.get(s.id)
        items.append(_hof_json(db, s, r.rank if r else None,
                               r.headline if r else "", r.updated_at if r else None))
    items.sort(key=lambda i: ((0, i["rank"] or 0) if i["featured"] else (1, 0),
                              -i["submission_id"]))
    return {"items": items, "rank_max": HOF_MAX_RANK,
            "featured_count": sum(1 for i in items if i["featured"])}


@app.post("/api/admin/hall-of-fame")
def set_hall_of_fame(payload: dict, user: User = Depends(_auth_dep),
                     db: Session = Depends(get_session), request: Request = None):
    """Feature / re-rank / un-feature an approved photo. Rank 1..20, or null to remove."""
    _require_admin(user)
    sid = int(payload.get("submission_id") or 0)
    s = db.exec(select(Submission).where(Submission.id == sid)).first()
    if not s:
        raise HTTPException(404, "Submission not found")
    if s.status != "completed":
        raise HTTPException(409, "Approve the photo first — only approved photos can be featured")
    raw = payload.get("rank", None)
    rank: Optional[int] = None
    if raw is not None and str(raw).strip() != "":
        try:
            rank = int(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Rank must be a whole number between 1 and {HOF_MAX_RANK}")
        if rank < 1 or rank > HOF_MAX_RANK:
            raise HTTPException(400, f"Rank must be between 1 and {HOF_MAX_RANK}")
    headline = str(payload.get("headline", "") or "").strip()[:120]
    row = db.exec(select(HallOfFame).where(HallOfFame.submission_id == sid)).first()
    now = _now()
    if rank is None:
        if row:
            db.delete(row)
            db.commit()
        log_audit(db, actor_id=user.id, action="hall_of_fame_remove", entity_type="submission",
                  entity_id=sid, ip=_client_ip(request))
        return {"ok": True, "rank": None, "featured": False}
    if row:
        row.rank, row.headline, row.updated_at = rank, headline, now
    else:
        db.add(HallOfFame(submission_id=sid, rank=rank, headline=headline,
                          created_by=user.id, created_at=now, updated_at=now))
    db.commit()
    log_audit(db, actor_id=user.id, action="hall_of_fame", entity_type="submission",
              entity_id=sid, after={"rank": rank, "headline": headline}, ip=_client_ip(request))
    return {"ok": True, "rank": rank, "featured": True}


def _optional_user(request: Request, db: Session = Depends(get_session)):
    """Same as _auth_dep but returns None instead of 401ing — for the public showcase."""
    try:
        return _auth_dep(request, db)
    except HTTPException:
        return None


@app.get("/api/hall-of-fame")
def hall_of_fame(request: Request, db: Session = Depends(get_session)):
    """The showcase: highlighted photos ordered by rank (ties break on score).

    Public on purpose: the Hall of Fame is meant to be shared/projected after the
    race, so it renders for anonymous visitors too. Anonymous callers do not get
    the team's score (rankings stay hidden from the teams)."""
    user = _optional_user(request, db)
    signed_in = user is not None
    out = []
    for r in db.exec(select(HallOfFame)).all():
        s = db.exec(select(Submission).where(Submission.id == r.submission_id)).first()
        if not s or s.status != "completed":
            continue                      # rejected / replaced photos drop out silently
        out.append(_hof_json(db, s, r.rank, r.headline, r.updated_at))
    out.sort(key=lambda i: (999 if i["rank"] is None else i["rank"], -(i["total_score"] or 0)))
    if not signed_in:
        for i in out:
            i["total_score"] = None
    return {"entries": out, "count": len(out), "rank_max": HOF_MAX_RANK,
            "public": not signed_in}
