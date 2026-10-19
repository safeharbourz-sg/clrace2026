"""Seed: event, admin/team accounts, WET-weather checkpoints, routes.

Wet Weather programme (indoor / sheltered Tanjong Pagar buildings).
Source: "DT Teambuilding Event 2026 - Checkpoints - Wet.pptx" (9 checkpoints).

Accounts are identical to the dry-weather instance: admins max / lyn /
chinchuan / karina (RACE_ADMIN_PASSWORD env) and team1..team12 (RACE_TEAM_PASSWORD env).

Run with: python -m app.seed
"""
from pathlib import Path

from sqlmodel import Session, select

from .database import engine
from .models import (
    Checkpoint, CheckpointImage, Event, EventMode, Route, RouteCheckpoint, Team, User,
)
import os
import secrets
from .auth import hash_password


def _seed_password(env_name: str, label: str) -> str:
    """Password for the seeded accounts: env override, else a random one printed once."""
    value = os.environ.get(env_name)
    if value:
        return value
    value = secrets.token_urlsafe(9)
    print(f"[seed] {label} accounts password (generated): {value}")
    print(f"[seed] set {env_name} next time to choose it yourself.")
    return value

UPLOAD_ROOT = Path(__file__).resolve().parent / "uploads"


def _cp_image(checkpoint_id: int, ref_file: str) -> CheckpointImage:
    url = f"/uploads/refs/{ref_file}"
    return CheckpointImage(checkpoint_id=checkpoint_id, image_url=url, thumbnail_url=url, sort_order=0)


def seed() -> None:
    with Session(engine) as db:
        # --- Idempotent reset: clear transient game/run state on every seed so a
        # re-seed returns the event to a clean pre-start state (used by smoke test /
        # day-of reset). Static rows (event, checkpoints, routes, accounts) stay.
        from .models import AuditLog, ScoreAdjustment, Session as DB_Session, Submission, TeamProgress
        for m in (DB_Session, Submission, TeamProgress, ScoreAdjustment, AuditLog):
            for row in db.exec(select(m)).all():
                db.delete(row)
        db.commit()
        for team in db.exec(select(Team)).all():
            team.status = "ready"
            team.started_at = None
            team.completed_at = None
            team.final_elapsed_seconds = None
        ev0 = db.exec(select(Event).where(Event.name == "CL Race 2026")).first()
        if ev0:
            ev0.status = "not_started"
        db.commit()

        # --- Event + Wet Weather mode ---
        ev = db.exec(select(Event).where(Event.name == "CL Race 2026")).first()
        if ev is None:
            ev = Event(
                name="CL Race 2026",
                tagline="Agent DT: City Quest - CBD Amazing Race",
                status="not_started",
            )
            db.add(ev)
            db.commit()
            db.refresh(ev)
        wet = db.exec(select(EventMode).where(EventMode.event_id == ev.id, EventMode.name == "Wet Weather")).first()
        if wet is None:
            wet = EventMode(event_id=ev.id, name="Wet Weather", active=True)
            db.add(wet)
            db.commit()
            db.refresh(wet)
        ev.active_event_mode_id = wet.id
        db.add(ev)
        db.commit()

        # --- Checkpoints (wet weather, indoor route, from the Wet deck) ---
        cp_spec = [
            dict(n=1, location="Capital Tower", category="Founders' Post", title="The Startup Founders",
                 desc="Take a selfie at the statue where every team member strikes a different startup founder pose - pitching, coding, presenting, brainstorming.",
                 reqs="No two people to have the same pose\nEntire team visible\nImage must be visible in background",
                 score=10, bonus="", img="cp1.jpg"),
            dict(n=2, location="Fraser Tower Linkway", category="Package Locker", title="Package Delivered!",
                 desc="Capture a synchronized jump shot at the Shopee pick-up lockers, as if celebrating a successful delivery.",
                 reqs="Entire team visible\nEveryone's feet off the ground\nImage must be visible in background",
                 score=10, bonus="Bonus 5 Points: Most synchronised jump.",
                 img="cp2.jpg"),
            dict(n=3, location="Fraser Tower", category="The Daily Commute", title="Rush Hour",
                 desc="Recreate a classic MRT scene at the train seats - dozing commuter, phone scroller, standing passenger, seat offerer.",
                 reqs="At least 4 different commuter roles\nEntire team visible\nImage must be visible in background",
                 score=10, bonus="", img="cp3.jpg"),
            dict(n=4, location="Guoco Tower", category="Sustainability Champions", title="Become a Tree",
                 desc="Team members must create a giant tree using their bodies - some as roots, some as trunk, some as branches.",
                 reqs="Entire team involved / visible\nTree shape recognisable\nPillar must be visible in background",
                 score=10, bonus="", img="cp4.jpg"),
            dict(n=5, location="Tanjong Pagar MRT", category="Patriotic Pride", title="Stand for Singapore",
                 desc="At the tap-in gates, every member strikes a patriotic pose with the mural in the background.",
                 reqs="Everyone in a patriotic pose\nEntire team visible\nMural must be visible in background",
                 score=10, bonus="Bonus 2 Points for every random stranger (non-CLI employee) to join in the photo.", img="cp5.jpg"),
            dict(n=6, location="Springleaf McDonald's", category="The Greatest Show", title="Step Right Up!",
                 desc="Create a circus scene - ringmaster, acrobats, clowns, lion tamer, strongman.",
                 reqs="Entire team visible\nAt least 4 different circus roles\nMcDonald's logo must be visible in background",
                 score=10, bonus="", img="cp6.jpg"),
            dict(n=7, location="Capital Tower", category="The Touch Network", title="Connected by Technology",
                 desc="Location Clue: Whether you're touching lives or pushing limits, this touchpoint is all about making a positive impact. Refer to image.\nForm a human network by connecting every team member through handshakes, elbows and fingers.",
                 reqs="No person standing alone\nEntire team physically connected\nImage must be visible in background",
                 score=10, bonus="", img="cp7.jpg"),
            dict(n=8, location="Capital Tower", category="Signal Strength", title="Human Wifi",
                 desc="Location Clue: Not quite the top, but definitely above the rest. Find the gateway between levels. Refer to image.\nForm a giant WiFi symbol with your bodies - arced layers above, one person as the dot at the base.",
                 reqs="Entire team involved / visible\nWiFi symbol clearly recognisable",
                 score=10, bonus="", img="cp8.jpg"),
            dict(n=9, location="Capital Tower", category="Final Mission", title="Mission Accomplished",
                 desc="Recreate a movie poster using AI with your team as the cast. The theme of the poster must be influenced by the team's name.",
                 reqs="Entire team visible\nDramatic pose\nTeam name must be reflected in the poster\nLyn must be in the photo",
                 score=10, bonus="Final mission approval stops race timer and marks team finished.", img="cp9.jpg",
                 final=True),
        ]

        for spec in cp_spec:
            cp = db.exec(select(Checkpoint).where(
                Checkpoint.event_mode_id == wet.id, Checkpoint.checkpoint_number == spec["n"])).first()
            if cp is None:
                cp = Checkpoint(
                    event_mode_id=wet.id, checkpoint_number=spec["n"], location=spec["location"],
                    category=spec["category"], mission_title=spec["title"], mission_description=spec["desc"],
                    requirements=spec["reqs"], default_score=spec["score"], bonus_description=spec["bonus"],
                    is_final=spec.get("final", False),
                )
                db.add(cp)
                db.commit()
                db.refresh(cp)
                db.add(_cp_image(cp.id, spec["img"]))
                db.commit()

        # --- Routes (Forward / Reverse) ---
        forward = db.exec(select(Route).where(Route.event_id == ev.id, Route.name == "Forward")).first()
        reverse = db.exec(select(Route).where(Route.event_id == ev.id, Route.name == "Reverse")).first()
        cps = {n: db.exec(select(Checkpoint).where(
            Checkpoint.event_mode_id == wet.id, Checkpoint.checkpoint_number == n)).one()
            for n in range(1, 10)}
        if forward is None:
            forward = Route(event_id=ev.id, name="Forward", active=True)
            db.add(forward); db.commit(); db.refresh(forward)
            for i in range(1, 10):
                db.add(RouteCheckpoint(route_id=forward.id, checkpoint_id=cps[i].id, sequence_number=i))
            db.commit()
        if reverse is None:
            reverse = Route(event_id=ev.id, name="Reverse", active=True)
            db.add(reverse); db.commit(); db.refresh(reverse)
            seq = [8, 7, 6, 5, 4, 3, 2, 1, 9]
            for i, n in enumerate(seq, 1):
                db.add(RouteCheckpoint(route_id=reverse.id, checkpoint_id=cps[n].id, sequence_number=i))
            db.commit()

        ADMIN_PW = _seed_password("RACE_ADMIN_PASSWORD", "admin")
        TEAM_PW = _seed_password("RACE_TEAM_PASSWORD", "team")
        admin_spec = [("max", "Max", ADMIN_PW), ("lyn", "Lyn", ADMIN_PW),
                      ("karina", "Karina", ADMIN_PW), ("shijie", "Shijie", ADMIN_PW)]
        for uname, disp, pw in admin_spec:
            if db.exec(select(User).where(User.username == uname)).first() is None:
                db.add(User(username=uname, password_hash=hash_password(pw), display_name=disp, role="admin"))
        db.commit()

        # Retired admin accounts: drop them so exactly the four above remain.
        for stale in ("chinchuan",):
            row = db.exec(select(User).where(User.username == stale)).first()
            if row is not None:
                db.delete(row)
        db.commit()

        # --- Teams 1-12 + TL accounts (identical to the dry-weather instance) ---
        # Odd-numbered teams run the route clockwise (Forward), even-numbered
        # teams anti-clockwise (Reverse). Both lists start at their own first
        # station and end on the final mission.
        route_for = {t: (forward.id if t % 2 else reverse.id) for t in range(1, 13)}
        for n in range(1, 13):
            name = f"Team {n}"
            team = db.exec(select(Team).where(Team.name == name)).first()
            if team is None:
                team = Team(name=name, route_id=route_for.get(n), release_order=n, status="ready")
                db.add(team); db.commit(); db.refresh(team)
            else:
                # Re-seed keeps existing teams in sync with the table above.
                team.route_id = route_for.get(n)
                team.release_order = n
                db.commit()
            # team leader account
            uname = f"team{n}"
            if db.exec(select(User).where(User.username == uname)).first() is None:
                db.add(User(username=uname, password_hash=hash_password(TEAM_PW),
                           display_name=f"{name} Leader", role="team_leader", team_id=team.id))
                db.commit()

        # --- Settings defaults ---
        from .models import Setting
        defaults = {
            "release_interval_minutes": "5",
            "leaderboard_visible": "true",
            "completion_bonus": "10,8,5",   # 1st, 2nd, 3rd
        }
        for k, v in defaults.items():
            if db.exec(select(Setting).where(Setting.key == k)).first() is None:
                db.add(Setting(key=k, value=v))
        db.commit()

    print("Seed complete (Wet Weather: 9 checkpoints).")


if __name__ == "__main__":
    from .database import init_db
    init_db()
    seed()
