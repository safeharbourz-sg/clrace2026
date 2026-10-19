"""End-to-end backend smoke test mirroring guide sections 20-21 acceptance tests.

Run: backend/.venv/bin/python backend/smoke_test.py
"""
import io
import os
import sys
import warnings
from PIL import Image

warnings.filterwarnings("ignore")
os.environ.setdefault("CLRACE_TEST", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient

from app.database import init_db
from app.main import app

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  -> ' + str(extra)[:200]) if extra else ''}")


print("=== reset DB ===")
init_db()
from app import seed as seed_mod
seed_mod.seed()

admin = TestClient(app)

print("\n=== 8. Auth + roles ===")
r = admin.post("/api/login", json={"username": "max", "password": os.environ.get("RACE_ADMIN_PASSWORD", "CHANGE-ME-admin")})
check("8a admin login", r.status_code == 200 and r.json().get("user", {}).get("role") == "admin", r.text)
check("8b session cookie set", "clrace_session" in admin.cookies)

r = admin.post("/api/login", json={"username": "max", "password": "wrong"})
check("8c bad password rejected", r.status_code == 401, r.text)

r = admin.get("/api/me")
check("8d /api/me authed", r.status_code == 200, r.text)

tl = TestClient(app)
r = tl.post("/api/login", json={"username": "team1", "password": os.environ.get("RACE_TEAM_PASSWORD", "CHANGE-ME-team")})
check("8e team leader login", r.status_code == 200, r.text)
tl_user = r.json().get("user", {})
check("8f TL tied to team", tl_user.get("role") == "team_leader" and tl_user.get("team_id") == 1, tl_user)

r = tl.get("/api/admin/teams")
check("8g TL blocked from admin", r.status_code in (401, 403), r.status_code)

print("\n=== 9. Race control ===")
r = admin.post("/api/admin/race/status", json={"status": "not_started"})
check("9a set not_started", r.status_code == 200, r.text)
r = admin.post("/api/admin/race/status", json={"status": "live"})
check("9b set live", r.status_code == 200, r.text)

# team cannot start before live — reset flow: TL can't start while not_started
r = admin.post("/api/admin/race/status", json={"status": "not_started"})
r = tl.post("/api/start")
check("9c TL cannot start when not live", r.status_code == 409, r.status_code)
r = admin.post("/api/admin/race/status", json={"status": "live"})
check("9d back to live", r.status_code == 200)

print("\n=== 10-12. Team release + start + checkpoint ===")
r = tl.post("/api/start")
check("10 TL team1 starts (release order 1)", r.status_code == 200, r.text)

r = tl.get("/api/checkpoints/next")
cp = r.json().get("checkpoint") if r.status_code == 200 else None
check("11a next checkpoint available", r.status_code == 200 and cp is not None, r.text[:200])
check("11b CP1 is the first (number 1)", bool(cp) and cp.get("number") == 1, cp)
cp_id = cp.get("id") if cp else None

r = admin.get("/api/admin/teams")
teams = r.json().get("teams", [])
t1 = next((t for t in teams if t["id"] == 1), None)
check("11c team1 status racing", t1 and t1.get("status") == "racing", t1)

print("\n=== 12. Photo upload + submit ===")
buf = io.BytesIO()
Image.new("RGB", (64, 48), color=(200, 40, 40)).save(buf, format="JPEG")
buf.seek(0)
r = tl.post(f"/api/submit/{cp_id}", files={"file": ("proof.jpg", buf, "image/jpeg")})
check("12a submit accepted (pending)", r.status_code == 200 and r.json().get("status") == "pending", r.text)

# replace-before-review: second submit should replace pending (attempt 2)
r = tl.post(f"/api/submit/{cp_id}", files={"file": ("proof2.jpg", buf, "image/jpeg")})
check("12b replacement submit OK (attempt 2)", r.status_code == 200 and r.json().get("status") == "pending", r.text)

r = admin.get("/api/admin/submissions", params={"status": "pending"})
subs = r.json().get("submissions", [])
check("12c admin sees pending submission", r.status_code == 200 and len(subs) == 1, r.text[:200])
sub_id = subs[0]["id"] if subs else None

print("\n=== 13. Admin approval (concurrency + scoring) ===")
r = admin.post(f"/api/admin/submissions/{sub_id}/approve", json={"bonus_score": 2, "comment": "good evidence"})
check("13a approve with bonus", r.status_code == 200 and r.json().get("total_score") == 12, r.text)
# second approve must fail (already reviewed)
r = admin.post(f"/api/admin/submissions/{sub_id}/approve", json={"comment": "again"})
check("13b double-approve rejected", r.status_code == 409, r.status_code)

r = tl.get("/api/checkpoints/next")
check("13c next checkpoint unlocked (CP2, number 2)", r.status_code == 200
      and r.json().get("checkpoint") and r.json().get("checkpoint").get("number") == 2,
      r.json() if r.status_code == 200 else r.text)

print("\n=== 14. Leaderboard + scoring ===")
r = admin.get("/api/leaderboard")
lb = r.json().get("leaderboard", [])
t1row = next((x for x in lb if x["id"] == 1), None)
check("14 leaderboard team1 score == 12", t1row and t1row.get("score") == 12, t1row)

r = admin.get("/api/leaderboard/fastest")
check("14b fastest board exists", r.status_code == 200, r.text[:120])

print("\n=== 15. Manual score adjustment ===")
r = admin.post("/api/admin/teams/1/adjust", json={"points": -2, "reason": "missed checkpoint photo rule"})
check("15a penalty applied", r.status_code == 200 and r.json().get("score") == 10, r.text)
r = admin.post("/api/admin/teams/1/adjust", json={"points": 5, "reason": ""})
check("15b empty reason rejected", r.status_code == 400, r.status_code)

print("\n=== 16. Audit log ===")
r = admin.get("/api/admin/audit")
check("16 audit returns rows", r.status_code == 200 and len(r.json().get("audit", [])) >= 4, r.text[:160])

print("\n=== 17. Results CSV ===")
r = admin.get("/api/admin/results.csv")
check("17a CSV export", r.status_code == 200 and "Team" in r.text, r.text[:100])
r = admin.get("/api/admin/results.csv")
print("     csv sample:", r.text.replace("\r\n", " | ")[:200])

print("\n=== 18. Static/ref image serving + health ===")
r = admin.get("/api/health")
check("18a health ok", r.status_code == 200 and r.json().get("ok") is True, r.text)

print(f"\n=== RESULT: {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL GREEN")
