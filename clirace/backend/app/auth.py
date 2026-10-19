"""Password hashing (bcrypt) + audit helpers."""
import json
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

from .models import AuditLog, Session as DBSession, utcnow
from .database import get_session


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(48)


def create_db_session(db, user_id: int, ip: str | None, ua: str | None, ttl_hours: int = 12) -> DBSession:
    tok = new_session_token()
    s = DBSession(
        token=tok,
        user_id=user_id,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(hours=ttl_hours),
        ip_address=ip,
        user_agent=(ua or "")[:255],
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def log_audit(db, *, actor_id: int | None, action: str, entity_type: str = "",
              entity_id: int | None = None, before=None, after=None, ip: str = "") -> AuditLog:
    a = AuditLog(
        user_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=json.dumps(before, default=str) if before is not None else "",
        after_json=json.dumps(after, default=str) if after is not None else "",
        ip_address=ip or "",
        created_at=utcnow(),
    )
    db.add(a)
    db.commit()
    return a
