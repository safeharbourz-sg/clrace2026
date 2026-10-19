"""Production smoke test over the PUBLIC URL — READ-ONLY (does not mutate the live DB).

Runs auth + role gating + read-only endpoints against the deployed portal
at clirace2026.clreport.com. Mirrors smoke_test.py but skips all state-changing
calls (start/submit/approve/adjust) that would leave the shared seeded DB dirty.

Usage: backend/.venv/bin/python prod_smoke_test.py
"""
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "https://clirace2026.clreport.com"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": BASE,
}
PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{('  -> ' + str(extra)[:200]) if extra else ''}")


def session():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPRedirectHandler(),
    ), cj


def call(op, cookie_jar, path, data=None, method=None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if data is not None else "GET"))
    for k, v in HDRS.items():
        req.add_header(k, v)
    req.add_header("Referer", BASE + "/")
    body = json_dump(data).encode() if data is not None else None
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        r = op.open(req, body)
        txt = r.read().decode()
        return r.status, txt, [c.name for c in cookie_jar]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), [c.name for c in cookie_jar]


def json_dump(o):
    import json
    return json.dumps(o)


# --- 8. Auth + roles (read-only: just logins + /api/me + role gating) ---
admin = session()
s, body, cookies = call(admin[0], admin[1], "/api/login", {"username": "max", "password": os.environ.get("RACE_ADMIN_PASSWORD", "CHANGE-ME-admin")})
check("8a admin login (public)", s == 200 and '"role":"admin"' in body, body[:160])
check("8b session cookie set", "clirace_session" in cookies)

bad = session()
s, body, _ = call(bad[0], bad[1], "/api/login", {"username": "max", "password": "wrong"})
check("8c bad password rejected (public)", s == 401, s)

s, body, _ = call(admin[0], admin[1], "/api/me")
check("8d /api/me authed (public)", s == 200, body[:160])

tl = session()
s, body, _ = call(tl[0], tl[1], "/api/login", {"username": "team1", "password": os.environ.get("RACE_TEAM_PASSWORD", "CHANGE-ME-team")})
check("8e TL login (public)", s == 200 and '"role":"team_leader"' in body and '"team_id":1' in body, body[:200])

s, body, _ = call(tl[0], tl[1], "/api/leaderboard")
# Lyn: "we do not want the teams to view their final scores in the portal —
# ranking will only be shared at the end of the race during prize giving."
check("8g-1 TL blocked from leaderboard", s == 403, s)
s, body, _ = call(tl[0], tl[1], "/api/leaderboard/fastest")
check("8g-2 TL blocked from fastest board", s == 403, s)
s, body, _ = call(tl[0], tl[1], "/api/admin/teams")
check("8g TL blocked from admin teams", s in (401, 403), s)

s, body, _ = call(admin[0], admin[1], "/api/admin/teams")
check("8h admin teams visible", s == 200 and '"teams"' in body, body[:160])

# --- Read-only / non-mutating endpoints ---
s, body, _ = call(admin[0], admin[1], "/api/health")
check("18a health ok", s == 200 and '"ok":true' in body, body[:120])

s, body, _ = call(admin[0], admin[1], "/api/leaderboard")
check("14 leaderboard present", s == 200 and '"leaderboard"' in body, body[:160])

s, body, _ = call(admin[0], admin[1], "/api/leaderboard/fastest")
check("14b fastest board present", s == 200, s)

s, body, _ = call(admin[0], admin[1], "/api/admin/teams")
check("16a teams rows", s == 200 and '"teams"' in body, body[:120])

s, body, _ = call(admin[0], admin[1], "/api/admin/audit")
check("16 audit read", s == 200 and '"audit"' in body, body[:120])

s, body, _ = call(tl[0], tl[1], "/api/checkpoints/next")
check("11 TL checkpoints/next read", s in (200, 409, 403), s)  # 409 if race not live — acceptable read path

s, body, _ = call(admin[0], admin[1], "/api/admin/results.csv")
check("17a CSV export", s == 200 and "Team" in body, body[:120])

# --- static / SPA ---
import urllib.request as u2
try:
    req = u2.Request(BASE + "/", headers=HDRS)
    r = u2.urlopen(req, timeout=20)
    html = r.read().decode()
    check("root serves SPA", r.status == 200 and "CL Race 2026" in html, r.status)
except Exception as e:
    check("root serves SPA", False, e)

print(f"\n=== PROD RESULT: {len(PASS)} passed, {len(FAIL)} failed ===")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL GREEN (read-only)")
