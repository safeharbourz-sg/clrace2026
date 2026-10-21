"""Email one-time-code sign-in (post-race lockdown).

Password sign-in is retired. The only way in is a 6-digit code emailed to the
single address held in the ``login_email`` Setting. Codes are stored hashed,
expire after 10 minutes, allow 5 attempts, and have a 60-second resend cooldown.
Delivery uses the shared gmail-housekeeper SMTP credential (the same one
cards.clreport.com / the Family Hub already use) so no new secret is stored in
this repo.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List

from sqlmodel import Session, select

from .models import LoginCode

logger = logging.getLogger("clrace.auth")

OTP_TTL_MINUTES = 10
OTP_RESEND_SECONDS = 60
OTP_MAX_ATTEMPTS = 5
OTP_IP_LIMIT = 8          # request-code calls per IP per hour
OTP_IP_WINDOW = 3600

_ip_hits: Dict[str, List[float]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def _smtp_credentials():
    """(from_email, app_password) from the shared gmail-housekeeper config."""
    candidates = []
    env_path = os.environ.get("RACE_SMTP_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / "projects" / "gmail-housekeeper" / "backend" / "config.toml")
    candidates.append(Path("/Users/maxmini/projects/gmail-housekeeper/backend/config.toml"))
    for path in candidates:
        try:
            email = pw = None
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("email"):
                    email = line.split("=", 1)[1].strip().strip('"')
                elif line.startswith("app_password"):
                    pw = line.split("=", 1)[1].strip().strip('"')
            if email and pw:
                return email, pw
        except OSError:
            continue
    raise RuntimeError("SMTP credentials unavailable")


def allow_request(ip: str) -> bool:
    """Simple per-IP throttle so the endpoint cannot be used to spray mail."""
    now = time.time()
    hits = [t for t in _ip_hits.get(ip or "?", []) if now - t < OTP_IP_WINDOW]
    if len(hits) >= OTP_IP_LIMIT:
        _ip_hits[ip or "?"] = hits
        return False
    hits.append(now)
    _ip_hits[ip or "?"] = hits
    return True


def _send(to: str, code: str) -> None:
    from_email, app_password = _smtp_credentials()
    msg = MIMEText(
        f"Your CL Race 2026 sign-in code is: {code}\n\n"
        f"It expires in {OTP_TTL_MINUTES} minutes and can be used once.\n"
        "If you did not request it, ignore this email.\n\n— CL Race 2026",
        "plain", "utf-8")
    msg["Subject"] = "CL Race 2026 — your sign-in code"
    msg["From"] = from_email
    msg["To"] = to
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=25) as srv:
        srv.starttls()
        srv.login(from_email, app_password)
        srv.send_message(msg)


def issue_code(db: Session, email: str, ip: str = "") -> bool:
    """Create + email a code. Returns False when a fresh code is still in its
    resend cooldown (no second email is sent). Raises if delivery fails."""
    now = _now()
    recent = db.exec(select(LoginCode).where(LoginCode.email == email)
                     .order_by(LoginCode.created_at.desc())).first()
    if recent and (now - recent.created_at).total_seconds() < OTP_RESEND_SECONDS \
            and recent.expires_at > now:
        return False

    for old in db.exec(select(LoginCode).where(LoginCode.email == email)).all():
        db.delete(old)
    db.commit()

    code = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_hex(8)
    row = LoginCode(email=email, salt=salt, code_hash=_hash(code, salt),
                    expires_at=now + timedelta(minutes=OTP_TTL_MINUTES),
                    attempts=0, created_at=now, ip=ip or None)
    db.add(row)
    db.commit()
    try:
        _send(email, code)
    except Exception:
        db.delete(row)
        db.commit()
        raise
    logger.info("login code issued (delivery ok)")
    return True


def consume_code(db: Session, email: str, code: str) -> bool:
    """Single-use verification with an attempt cap."""
    row = db.exec(select(LoginCode).where(LoginCode.email == email)
                  .order_by(LoginCode.created_at.desc())).first()
    if row is None:
        return False
    now = _now()
    if row.expires_at <= now:
        db.delete(row)
        db.commit()
        return False
    if row.attempts >= OTP_MAX_ATTEMPTS:
        db.delete(row)
        db.commit()
        return False
    row.attempts += 1
    ok = secrets.compare_digest(row.code_hash, _hash(code, row.salt))
    if ok:
        db.delete(row)
    db.commit()
    return ok
