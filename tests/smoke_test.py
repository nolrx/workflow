#!/usr/bin/env python3
"""
Worksflow — deployment smoke test.

Exercises the live, reachable API surface end-to-end through the nginx
frontend (the worksflow.xyz domain) -> Flask backend -> PostgreSQL. Covers the
active Code domain plus the shared base (auth / users / credits / teams / agent)
and infrastructure health.

AI-generation endpoints (POST /api/code/projects, POST /api/agent/runs, image
generation, etc.) are intentionally excluded: they call live model APIs, cost
credits, and run for minutes — out of scope for a smoke test.

Usage:
    python3 tests/smoke_test.py                       # -> http://worksflow.xyz
    BASE_URL=http://localhost python3 tests/smoke_test.py
    python3 tests/smoke_test.py http://localhost:5001 # direct-to-backend

Exits non-zero if ANY check fails.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("BASE_URL", "http://worksflow.xyz")).rstrip("/")
TIMEOUT = 20

# ANSI colors (disabled when not a TTY)
_tty = sys.stdout.isatty()
GREEN = "\033[32m" if _tty else ""
RED = "\033[31m" if _tty else ""
DIM = "\033[2m" if _tty else ""
BOLD = "\033[1m" if _tty else ""
RST = "\033[0m" if _tty else ""

results = []  # (ok: bool, name: str, detail: str)


def request(method, path, token=None, body=None, headers=None):
    """Return (status_code, parsed_body_or_text)."""
    url = BASE_URL + path
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except Exception as e:  # connection refused, timeout, DNS, etc.
        return 0, f"<request error: {e}>"
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def check(name, cond, detail=""):
    results.append((bool(cond), name, detail))
    mark = f"{GREEN}PASS{RST}" if cond else f"{RED}FAIL{RST}"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"  {DIM}{detail}{RST}"
    print(line)
    return bool(cond)


def section(title):
    print(f"\n{BOLD}{title}{RST}")


def main():
    print(f"{BOLD}Smoke test against {BASE_URL}{RST}")
    ts = int(time.time())
    email = f"smoke_{ts}@example.com"
    password = "Passw0rd!23"

    # ---------------------------------------------------------------- infra
    section("Infrastructure")
    st, b = request("GET", "/health")
    check("GET /health -> 200", st == 200, f"status={st}")
    check("/health reports healthy", isinstance(b, dict) and b.get("status") == "healthy", str(b)[:80])

    st, b = request("GET", "/")
    check("GET / (SPA index via nginx) -> 200", st == 200, f"status={st}")
    check("SPA index returns HTML", isinstance(b, str) and ("<div" in b or "<title" in b), "html body")

    # ---------------------------------------------------------------- auth
    section("Auth")
    st, b = request("POST", "/api/auth/register", body={"email": email, "password": password})
    ok = check("POST /api/auth/register -> 201", st == 201, f"status={st}")
    access = b.get("access_token") if isinstance(b, dict) else None
    refresh = b.get("refresh_token") if isinstance(b, dict) else None
    user_id = (b.get("user") or {}).get("id") if isinstance(b, dict) else None
    check("register returns access+refresh tokens", bool(access and refresh))
    if not access:
        check("ABORT: cannot continue without a token", False, "registration failed")
        return finish()

    st, b = request("POST", "/api/auth/register", body={"email": email, "password": password})
    check("register duplicate email -> 409", st == 409, f"status={st}")

    st, b = request("POST", "/api/auth/register", body={"email": f"x_{ts}@e.com"})
    check("register missing password -> 400", st == 400, f"status={st}")

    st, b = request("POST", "/api/auth/login", body={"email": email, "password": password})
    check("POST /api/auth/login -> 200", st == 200, f"status={st}")
    access = b.get("access_token") if isinstance(b, dict) else access
    refresh = b.get("refresh_token") if isinstance(b, dict) else refresh

    st, b = request("POST", "/api/auth/login", body={"email": email, "password": "wrong-pass"})
    check("login wrong password -> 401", st == 401, f"status={st}")

    st, b = request("GET", "/api/auth/me", token=access)
    check("GET /api/auth/me -> 200", st == 200, f"status={st}")
    check("me returns the registered email", isinstance(b, dict) and (b.get("user") or {}).get("email") == email)

    st, b = request("GET", "/api/auth/me")
    check("me without token -> 401", st == 401, f"status={st}")

    # refresh token is read from the Authorization header (jwt_required(refresh=True))
    st, b = request("POST", "/api/auth/refresh", token=refresh)
    check("POST /api/auth/refresh -> 200", st == 200, f"status={st}")
    if isinstance(b, dict) and b.get("access_token"):
        access = b["access_token"]

    # ---------------------------------------------------------------- users
    section("Users")
    st, b = request("GET", "/api/users/profile", token=access)
    check("GET /api/users/profile -> 200", st == 200, f"status={st}")

    st, b = request("PUT", "/api/users/profile", token=access, body={"display_name": "Smoke Tester"})
    ok = check("PUT /api/users/profile -> 200", st == 200, f"status={st}")
    check("profile update echoed", isinstance(b, dict) and (b.get("user") or {}).get("display_name") == "Smoke Tester")

    # ---------------------------------------------------------------- credits
    section("Credits")
    st, b = request("GET", "/api/credits/balance", token=access)
    check("GET /api/credits/balance -> 200", st == 200, f"status={st}")
    check("balance has a numeric balance field", isinstance(b, dict) and isinstance(b.get("balance"), int), f"balance={b.get('balance') if isinstance(b,dict) else b}")

    st, b = request("GET", "/api/credits/transactions", token=access)
    check("GET /api/credits/transactions -> 200", st == 200, f"status={st}")
    check("transactions returns a list", isinstance(b, dict) and isinstance(b.get("transactions"), list))

    st, b = request("GET", "/api/credits/usage", token=access)
    check("GET /api/credits/usage -> 200", st == 200, f"status={st}")
    check("usage has total_credits", isinstance(b, dict) and "total_credits" in b)

    # ---------------------------------------------------------------- teams
    section("Teams")
    st, b = request("POST", "/api/teams", token=access, body={"name": f"Smoke Team {ts}"})
    check("POST /api/teams -> 201", st == 201, f"status={st}")
    team_id = (b.get("team") or {}).get("id") if isinstance(b, dict) else None
    check("create team returns an id", bool(team_id))

    st, b = request("GET", "/api/teams", token=access)
    check("GET /api/teams -> 200", st == 200, f"status={st}")
    check("team list includes the new team", isinstance(b, dict) and any(t.get("id") == team_id for t in b.get("teams", [])))

    if team_id:
        st, b = request("GET", f"/api/teams/{team_id}", token=access)
        check("GET /api/teams/<id> -> 200", st == 200, f"status={st}")
        st, b = request("GET", f"/api/teams/{team_id}/members", token=access)
        check("GET /api/teams/<id>/members -> 200", st == 200, f"status={st}")
        st, b = request("GET", f"/api/credits/balance?team_id={team_id}", token=access)
        check("GET /api/credits/balance?team_id -> 200", st == 200, f"status={st}")

    # ---------------------------------------------------------------- code (active domain)
    section("Code domain")
    st, b = request("GET", "/api/code/styles", token=access)
    check("GET /api/code/styles -> 200", st == 200, f"status={st}")
    styles = (b.get("data") or {}).get("styles") if isinstance(b, dict) else None
    check("styles list is non-empty", bool(styles))

    st, b = request("GET", "/api/code/prompt-prefixes", token=access)
    check("GET /api/code/prompt-prefixes -> 200", st == 200, f"status={st}")
    prefixes = (b.get("data") or {}).get("prefixes") if isinstance(b, dict) else None
    prefix_id = prefixes[0]["id"] if prefixes else None
    check("prompt-prefixes list is non-empty", bool(prefixes), f"first={prefix_id}")

    if prefix_id:
        st, b = request("GET", f"/api/code/prompt-prefixes/{prefix_id}", token=access)
        check("GET /api/code/prompt-prefixes/<id> -> 200", st == 200, f"status={st}")

    st, b = request("GET", "/api/code/prompt-prefixes/__nope__", token=access)
    check("unknown prompt-prefix -> 404", st == 404, f"status={st}")

    st, b = request("POST", "/api/code/prompt-prefixes/route", token=access,
                    body={"task": "Build a todo list web app with login"})
    check("POST /api/code/prompt-prefixes/route -> 200", st == 200, f"status={st}")
    check("route returns a routing result", isinstance(b, dict) and "route" in (b.get("data") or {}))

    if prefix_id:
        st, b = request("POST", "/api/code/prompt-prefixes/compose", token=access,
                        body={"primary_role": prefix_id})
        check("POST /api/code/prompt-prefixes/compose -> 200", st == 200, f"status={st}")
        check("compose returns a system_prompt", isinstance(b, dict) and bool((b.get("data") or {}).get("system_prompt")))

    st, b = request("POST", "/api/code/prompt-prefixes/compose", token=access, body={})
    check("compose without primary_role -> 400", st == 400, f"status={st}")

    st, b = request("GET", "/api/code/projects", token=access)
    check("GET /api/code/projects -> 200", st == 200, f"status={st}")
    check("projects list present", isinstance(b, dict) and isinstance((b.get("data") or {}).get("projects"), list))

    # ---------------------------------------------------------------- agent
    section("Agent")
    st, b = request("GET", "/api/agent/runs", token=access)
    check("GET /api/agent/runs -> 200", st == 200, f"status={st}")
    check("runs list present", isinstance(b, dict) and isinstance((b.get("data") or {}).get("runs"), list))

    st, b = request("GET", "/api/agent/runs?domain=code", token=access)
    check("GET /api/agent/runs?domain=code -> 200", st == 200, f"status={st}")

    return finish()


def finish():
    passed = sum(1 for ok, _, _ in results if ok)
    total = len(results)
    failed = total - passed
    print(f"\n{BOLD}{'='*60}{RST}")
    if failed == 0:
        print(f"{GREEN}{BOLD}ALL {total} CHECKS PASSED{RST}")
        return 0
    print(f"{RED}{BOLD}{failed}/{total} CHECKS FAILED{RST}")
    for ok, name, detail in results:
        if not ok:
            print(f"  {RED}- {name}{RST}  {DIM}{detail}{RST}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
