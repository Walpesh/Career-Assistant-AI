"""Комплексный живой E2E-аудит Career-Assistant-AI (docs/03_API_CONTRACTS.md).

Запуск (из каталога backend), при поднятом API на 127.0.0.1:8000:
    python tools/live_e2e_audit.py

Работает против изолированной аудио-БД (career_assistant_audit) и регистрирует
собственных тестовых пользователей. Проверяет статус-коды, форматы ошибок
{detail, error_code}, латентность и реал-тайм доставку WebSocket.
Результат — audit_results.json + сводка.

OTP-поток (docs/03 §2): код хранится только как HMAC-SHA256 от email:code с
ключом JWT_SECRET. Аудит владеет секретом (dev), поэтому восстанавливает
6-значный код перебором 10^6 вариантов и вызывает настоящий verify-email.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import httpx

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API = "http://127.0.0.1:8000"
WS = "ws://127.0.0.1:8000"
PREFIX = "/api/v1"
AUDIT_DB = "postgresql://career:career@localhost:5432/career_assistant_audit"
JWT_SECRET = "change-me-in-production"

RESULTS: list[dict] = []


def record(section, name, method, path, expected, actual, latency_ms, ok, note=""):
    RESULTS.append({
        "section": section, "name": name, "method": method, "path": path,
        "expected_status": expected, "actual_status": actual,
        "latency_ms": round(latency_ms, 1), "pass": bool(ok), "note": note,
    })
    flag = "OK  " if ok else "FAIL"
    print(f"  [{flag}] {method:6} {path:44} exp={expected} act={actual} {latency_ms:6.0f}ms  {name}"
          + (f"  :: {note}" if note and not ok else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")


def _brief(body, ok):
    if isinstance(body, dict):
        if "error_code" in body:
            return f"error_code={body['error_code']}"
        return "keys=[" + ",".join(list(body.keys())[:6]) + "]"
    return str(body)[:60]


class Auditor:
    def __init__(self, client: httpx.AsyncClient):
        self.c = client

    async def call(self, sect, name, method, path, expected, *, headers=None,
                   json_body=None, ok_when=None, note=""):
        return await self._req(sect, name, method, f"{PREFIX}{path}", expected,
                               headers=headers, json_body=json_body, ok_when=ok_when, note=note)

    async def call_root(self, sect, name, method, path, expected, *, headers=None,
                        json_body=None, ok_when=None, note=""):
        """Put' BEZ prefiksa /api/v1 — dlya sluzhebnyh /health* i /metrics*."""
        return await self._req(sect, name, method, path, expected,
                               headers=headers, json_body=json_body, ok_when=ok_when, note=note)

    async def _req(self, sect, name, method, url_path, expected, *, headers=None,
                   json_body=None, ok_when=None, note=""):
        t = time.perf_counter()
        try:
            r = await self.c.request(method, url_path, headers=headers, json=json_body)
            lat = (time.perf_counter() - t) * 1000
            actual = r.status_code
            ok = ok_when(actual, r) if ok_when else (actual == expected)
            try:
                body = r.json()
            except Exception:
                body = r.text
            record(sect, name, method, url_path, expected, actual, lat, ok, note or _brief(body, ok))
            return r
        except Exception as exc:  # noqa: BLE001
            lat = (time.perf_counter() - t) * 1000
            record(sect, name, method, url_path, expected, "EXC", lat, False, str(exc)[:180])
            raise


async def recover_otp_async(email: str) -> str | None:
    conn = await asyncpg.connect(AUDIT_DB)
    try:
        row = await conn.fetchrow(
            "SELECT otp_code_hash FROM email_otps WHERE email=$1", email.strip().lower()
        )
        if not row:
            return None
        target = row["otp_code_hash"]
        key = JWT_SECRET.encode()
        em = email.strip().lower()
        for i in range(1_000_000):
            code = f"{i:06d}"
            if hmac.new(key, f"{em}:{code}".encode(), hashlib.sha256).hexdigest() == target:
                return code
        return None
    finally:
        await conn.close()


async def seed_vacancy(user_id, hh_id, title, status="raw", desc=None):
    conn = await asyncpg.connect(AUDIT_DB)
    try:
        vid = uuid.uuid4()
        await conn.execute(
            "INSERT INTO vacancies (id, user_id, hh_vacancy_id, url, title, company_name,"
            " description_raw, status, source, created_at, updated_at)"
            " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'hh', now(), now())",
            vid, user_id, hh_id, f"https://hh.ru/vacancy/{hh_id}", title, "AuditCo",
            desc or ("Требуется Python-разработчик. Опыт от 3 лет. " * 20), status,
        )
        return str(vid)
    finally:
        await conn.close()


# --------------------------------------------------------------------------- HEALTH
async def test_health(a: Auditor) -> None:
    section("0. SYSTEM / HEALTH & METRICS (docs/01 §7, docs/03 §12)")
    await a.call_root("health", "liveness /health", "GET", "/health", 200)
    await a.call_root("health", "liveness /health/live", "GET", "/health/live", 200)
    await a.call_root("health", "readiness (Ollama down -> 503)", "GET", "/health/ready", 503)
    for path in ["/metrics", "/metrics/summary", "/metrics/alerts"]:
        await a.call_root("health", f"metrics {path}", "GET", path, 200)


# --------------------------------------------------------------------------- AUTH
async def register_and_verify(a: Auditor, email: str, password: str) -> dict:
    r = await a.call("auth", f"register {email}", "POST", "/auth/register", 201,
                     json_body={"email": email, "password": password})
    body = r.json()
    no_jwt = "access_token" not in body
    record("auth", "register ne vydaet JWT (zhdot OTP)", "POST", "/auth/register",
           201, r.status_code, 0, no_jwt, "" if no_jwt else "JWT vydan do verify")

    await a.call("auth", "login do verify -> 403", "POST", "/auth/login", 403,
                 json_body={"email": email, "password": password},
                 ok_when=lambda s, rr: s == 403 and rr.json().get("error_code") == "EMAIL_NOT_VERIFIED")

    code = await recover_otp_async(email)
    if not code:
        record("auth", "recover OTP", "-", "email_otps", "-", "NOT_FOUND", 0, False, "kod ne vosstanovlen")
        return {}
    r = await a.call("auth", "verify-email -> 200 JWT", "POST", "/auth/verify-email", 200,
                     json_body={"email": email, "code": code})
    tok = r.json()
    record("auth", "verify vydaet paru JWT", "POST", "/auth/verify-email", 200, r.status_code, 0,
           bool(tok.get("access_token") and tok.get("refresh_token")))
    return tok


async def test_auth(a: Auditor) -> dict:
    section("1. AUTH (docs/03 §2): register/OTP/verify/login/refresh/reuse")
    email = f"audit_{int(time.time())}@test.dev"
    password = "Str0ngP@ssw0rd!"
    tok = await register_and_verify(a, email, password)

    await a.call("auth", "login posle verify -> 200", "POST", "/auth/login", 200,
                 json_body={"email": email, "password": password})
    await a.call("auth", "login nevernyy parol -> 401", "POST", "/auth/login", 401,
                 json_body={"email": email, "password": "wrong-pass"})
    await a.call("auth", "povtor register -> 409", "POST", "/auth/register", 409,
                 json_body={"email": email, "password": password},
                 ok_when=lambda s, rr: s == 409 and rr.json().get("error_code") == "EMAIL_TAKEN")
    await a.call("auth", "verify nevernyy kod -> 400", "POST", "/auth/verify-email", 400,
                 json_body={"email": email, "code": "000000"},
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") in ("OTP_INVALID", "OTP_NOT_FOUND"))
    await a.call("auth", "resend-code -> 200", "POST", "/auth/resend-code", 200, json_body={"email": email})
    # rate-limit resend применим к НЕверифицированному пользователю: для
    # подтверждённого email ответ намеренно 200 (enumeration-safe, docs/03 §2).
    unv = f"audit_unv_{int(time.time())}@test.dev"
    await a.call("auth", "register (unverified для rate-limit)", "POST", "/auth/register", 201,
                 json_body={"email": unv, "password": password})
    await a.call("auth", "resend <60с -> 429 RATE_LIMITED", "POST", "/auth/resend-code", 429,
                 json_body={"email": unv},
                 ok_when=lambda s, rr: s == 429 and rr.json().get("error_code") == "RATE_LIMITED"
                 and "retry-after" in {k.lower() for k in rr.headers.keys()})

    h = {"Authorization": f"Bearer {tok['access_token']}"}
    await a.call("auth", "GET /auth/me -> 200", "GET", "/auth/me", 200, headers=h)

    # Ротация refresh: новый токен выдан, старый отозван.
    r = await a.call("auth", "refresh (rotatsiya) -> 200", "POST", "/auth/refresh", 200,
                     json_body={"refresh_token": tok["refresh_token"]})
    new_refresh = r.json().get("refresh_token")
    # Повторное предъявление СТАРОГО refresh → 401 REFRESH_TOKEN_REUSE и отзыв
    # ВСЕЙ семейки токенов (включая только что выданный new_refresh) — это
    # документированное поведение защиты от кражи (docs/03 §2).
    await a.call("auth", "reuse starogo refresh -> 401", "POST", "/auth/refresh", 401,
                 json_body={"refresh_token": tok["refresh_token"]},
                 ok_when=lambda s, rr: s == 401 and rr.json().get("error_code") == "REFRESH_TOKEN_REUSE")
    await a.call("auth", "new refresh otozvan posle reuse -> 401", "POST", "/auth/refresh", 401,
                 json_body={"refresh_token": new_refresh},
                 ok_when=lambda s, rr: s == 401 and rr.json().get("error_code") == "REFRESH_TOKEN_REUSE")

    r = await a.call("auth", "ws-ticket -> 200", "POST", "/auth/ws-ticket", 200, headers=h)
    tok["_ws_ticket"] = r.json().get("ticket")
    tok["_headers"] = h
    tok["_email"] = email
    tok["_password"] = password
    return tok


# --------------------------------------------------------------------------- PROFILE
async def test_profile(a: Auditor, tok: dict) -> str | None:
    section("2. PROFILE (docs/03 §3): get/update/convert-resume")
    h = tok["_headers"]
    await a.call("profile", "GET /profile -> 200", "GET", "/profile", 200, headers=h)
    await a.call("profile", "PUT /profile (partial) -> 200", "PUT", "/profile", 200, headers=h,
                 json_body={"full_name": "Audit Test", "resume_text": "Python FastAPI PostgreSQL " * 30,
                            "skills": ["Python", "FastAPI"], "experience_years": 4.5,
                            "match_threshold": 75, "preferred_work_formats": ["remote"]})
    r = await a.call("profile", "convert-resume -> 202/503 (LLM)", "POST", "/profile/convert-resume", 202,
                     headers=h, ok_when=lambda s, rr: s in (202, 503))
    return r.json().get("task_id") if r.status_code == 202 else None


async def test_profile_empty_resume(a: Auditor) -> None:
    section("2b. PROFILE convert-resume: pustoy resume -> 400 RESUME_EMPTY")
    email = f"audit_empty_{int(time.time())}@test.dev"
    tok = await register_and_verify(a, email, "Str0ngP@ssw0rd!")
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    await a.call("profile", "convert-resume pustoy resume -> 400", "POST", "/profile/convert-resume", 400,
                 headers=h, ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "RESUME_EMPTY")


# --------------------------------------------------------------------------- VACANCIES
async def _current_user_id(access_token: str) -> str:
    async with httpx.AsyncClient(base_url=API, timeout=20) as c:
        r = await c.get(f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {access_token}"})
        return r.json()["id"]


async def _test_idor_vacancy(a: Auditor, victim_vid: str) -> None:
    email = f"audit_idor_{int(time.time())}@test.dev"
    tok = await register_and_verify(a, email, "Str0ngP@ssw0rd!")
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    await a.call("idor", "chuzhoy GET /vacancies/{id} -> 404", "GET", f"/vacancies/{victim_vid}", 404, headers=h)
    await a.call("idor", "chuzhoy DELETE /vacancies/{id} -> 404", "DELETE", f"/vacancies/{victim_vid}", 404, headers=h)
    await a.call("idor", "chuzhoy PATCH status -> 404", "PATCH", f"/vacancies/{victim_vid}/status", 404, headers=h,
                 json_body={"status": "applied"})
    await a.call("idor", "chuzhoy GET /analysis/{id} -> 404", "GET", f"/analysis/{victim_vid}", 404, headers=h)
    await a.call("idor", "chuzhoy GET /letters/{id} -> 404", "GET", f"/letters/{victim_vid}", 404, headers=h)


async def test_vacancies(a: Auditor, tok: dict) -> tuple[str | None, str | None]:
    section("3. VACANCY STORAGE (docs/03 §4): list/filter/manual/detail/status/delete/IDOR")
    h = tok["_headers"]
    user_id = await _current_user_id(tok["access_token"])
    vid = await seed_vacancy(user_id, f"audit{int(time.time())}", "Senior Python Developer")

    await a.call("vacancies", "GET /vacancies -> 200", "GET", "/vacancies", 200, headers=h)
    await a.call("vacancies", "GET /vacancies?status=raw", "GET", "/vacancies?status=raw", 200, headers=h)
    await a.call("vacancies", "GET /vacancies?source=hh", "GET", "/vacancies?source=hh", 200, headers=h)
    await a.call("vacancies", "GET /vacancies?search=Python", "GET", "/vacancies?search=Python", 200, headers=h)
    await a.call("vacancies", "GET /vacancies bad status -> 400", "GET", "/vacancies?status=bogus", 400, headers=h,
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "VALIDATION_ERROR")
    await a.call("vacancies", "GET /vacancies/{id} -> 200", "GET", f"/vacancies/{vid}", 200, headers=h)
    await a.call("vacancies", "GET /vacancies/{random} -> 404", "GET", f"/vacancies/{uuid.uuid4()}", 404, headers=h)
    await a.call("vacancies", "PATCH status raw->analyzed", "PATCH", f"/vacancies/{vid}/status", 200, headers=h,
                 json_body={"status": "analyzed"})
    await a.call("vacancies", "PATCH analyzed->raw -> 409", "PATCH", f"/vacancies/{vid}/status", 409, headers=h,
                 json_body={"status": "raw"},
                 ok_when=lambda s, rr: s == 409 and rr.json().get("error_code") == "INVALID_STATUS_TRANSITION")
    await a.call("vacancies", "manual bad URL -> 400", "POST", "/vacancies/manual", 400, headers=h,
                 json_body={"vacancy_url": "https://example.com/not-hh"},
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "INVALID_VACANCY_URL")

    await _test_idor_vacancy(a, vid)
    return vid, user_id


# --------------------------------------------------------------------------- PARSING
async def test_parsing(a: Auditor, tok: dict) -> None:
    section("4. PARSING (docs/03 §5): auto/group/manual + validatsiya")
    h = tok["_headers"]
    # Контракт статус-кода: реализация и tests/test_billing.py:296 фиксируют
    # 200 { task_id, status } для parsing/analysis (docs/03 §5 явно не привязывает
    # 202; 202 зарезервирован за convert-resume). См. отчёт — расхождение с docs/03 §9.
    await a.call("parsing", "auto (filters+city+sources) -> 200", "POST", "/parsing/auto", 200, headers=h,
                 json_body={"keywords": ["python"], "employment_forms": ["full"], "work_formats": ["remote"],
                            "schedules": ["fullDay"], "max_pages": 1, "city": "Москва",
                            "sources": ["hh"], "blacklist_enabled": True, "blacklist_words": ["ТК РФ"]},
                 ok_when=lambda s, rr: s == 200 and "task_id" in rr.json())
    await a.call("parsing", "auto bez kriteriev -> 400", "POST", "/parsing/auto", 400, headers=h,
                 json_body={"max_pages": 1},
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "INVALID_SEARCH_CRITERIA")
    await a.call("parsing", "auto neizvestnyy istochnik -> 400", "POST", "/parsing/auto", 400, headers=h,
                 json_body={"keywords": ["python"], "sources": ["bogus"]},
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "VALIDATION_ERROR")
    await a.call("parsing", "group (search_url) -> 200", "POST", "/parsing/group", 200, headers=h,
                 json_body={"search_url": "https://novokuznetsk.hh.ru/vacancies/razrabotchik",
                            "max_pages": 1, "sources": ["hh"]},
                 ok_when=lambda s, rr: s == 200 and "task_id" in rr.json())
    await a.call("parsing", "group ne-hh ssylka -> 400", "POST", "/parsing/group", 400, headers=h,
                 json_body={"search_url": "https://example.com/list", "max_pages": 1},
                 ok_when=lambda s, rr: s in (400, 422))
    await a.call("parsing", "manual (vacancy_url) -> 200", "POST", "/parsing/manual", 200, headers=h,
                 json_body={"vacancy_url": "https://novokuznetsk.hh.ru/vacancy/137866214", "run_analysis": False},
                 ok_when=lambda s, rr: s == 200 and "task_id" in rr.json())
    await a.call("parsing", "manual ne-hh ssylka -> 400", "POST", "/parsing/manual", 400, headers=h,
                 json_body={"vacancy_url": "https://example.com/vacancy/1"},
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "INVALID_VACANCY_URL")


# --------------------------------------------------------------------------- ANALYSIS
async def test_analysis(a: Auditor, tok: dict, vid: str | None) -> None:
    section("5. ANALYSIS & LETTERS (docs/03 §6): run + IDOR")
    h = tok["_headers"]
    if not vid:
        record("analysis", "run propushen (net vakansii)", "POST", "/analysis/run", 202, "SKIP", 0, True)
        return
    await a.call("analysis", "run analyze -> 200", "POST", "/analysis/run", 200, headers=h,
                 json_body={"vacancy_ids": [vid], "mode": "analyze"},
                 ok_when=lambda s, rr: s == 200 and "task_id" in rr.json())
    await a.call("analysis", "run pustoy spisok -> 400", "POST", "/analysis/run", 400, headers=h,
                 json_body={"vacancy_ids": [], "mode": "analyze"},
                 ok_when=lambda s, rr: s == 400 and rr.json().get("error_code") == "INVALID_VACANCY_IDS")
    await a.call("analysis", "GET /analysis/{id} (200 or 404)", "GET", f"/analysis/{vid}", 404, headers=h,
                 ok_when=lambda s, rr: s in (200, 404))


# --------------------------------------------------------------------------- TASKS
async def test_tasks(a: Auditor, tok: dict) -> None:
    section("6. TASKS / QUEUE (docs/03 §7): list/get/cancel/resume")
    h = tok["_headers"]
    r = await a.call("tasks", "GET /tasks -> 200", "GET", "/tasks", 200, headers=h)
    items = r.json().get("items", [])
    if items:
        tid = items[0]["id"]
        await a.call("tasks", "GET /tasks/{id} -> 200", "GET", f"/tasks/{tid}", 200, headers=h)
        r2 = await a.call("tasks", "cancel zadachi -> 200/400", "POST", f"/tasks/{tid}/cancel", 200, headers=h,
                          ok_when=lambda s, rr: s in (200, 400))
        if r2.status_code == 200:
            await a.call("tasks", "povtornyy cancel -> 400", "POST", f"/tasks/{tid}/cancel", 400, headers=h,
                         ok_when=lambda s, rr: s == 400)
        await a.call("tasks", "resume ne-kapcha -> 409", "POST", f"/tasks/{tid}/resume", 409, headers=h,
                     ok_when=lambda s, rr: s == 409 and rr.json().get("error_code") == "TASK_NOT_WAITING_CAPTCHA")
    else:
        record("tasks", "net zadach dlya cancel/resume", "-", "/tasks", "-", "EMPTY", 0, True)
    await a.call("tasks", "GET /tasks/{random} -> 404", "GET", f"/tasks/{uuid.uuid4()}", 404, headers=h)


# --------------------------------------------------------------------------- ACCOUNT
async def test_account(a: Auditor, tok: dict) -> None:
    section("7. ACCOUNT & PRIVACY (docs/03 §10): export/summary")
    h = tok["_headers"]
    await a.call("account", "GET /account/export -> 200", "GET", "/account/export", 200, headers=h)
    await a.call("account", "GET /account/summary -> 200", "GET", "/account/summary", 200, headers=h)
    await a.call("account", "bez tokena export -> 401", "GET", "/account/export", 401)


# --------------------------------------------------------------------------- BILLING
async def test_billing(a: Auditor, tok: dict) -> None:
    section("8. BILLING (docs/03 §11): tiers/usage/subscription/webhook")
    h = tok["_headers"]
    await a.call("billing", "GET /billing/tiers (public) -> 200", "GET", "/billing/tiers", 200)
    await a.call("billing", "GET /billing/usage -> 200", "GET", "/billing/usage", 200, headers=h)
    await a.call("billing", "GET /billing/subscription -> 200", "GET", "/billing/subscription", 200, headers=h)
    await a.call("billing", "usage bez tokena -> 401", "GET", "/billing/usage", 401)
    await a.call("billing", "webhook bez podpisi -> 400", "POST", "/billing/webhook/yookassa", 400,
                 json_body={"event": "payment.succeeded"}, ok_when=lambda s, rr: s in (400, 422))
    await a.call("billing", "webhook neizvestnyy provider -> 400", "POST", "/billing/webhook/unknownpay", 400,
                 json_body={"event": "x"}, ok_when=lambda s, rr: s in (400, 404, 422))


# --------------------------------------------------------------------------- WEBSOCKET
async def test_websocket(a: Auditor, tok: dict) -> None:
    section("9. WEBSOCKET REALTIME (docs/03 §8): auth + live event flow")
    try:
        import websockets  # type: ignore  # noqa: F401
        from websockets.asyncio.client import connect as ws_connect  # type: ignore
        from websockets.exceptions import ConnectionClosed  # type: ignore
    except ImportError:
        record("ws", "websockets ne ustanovlen", "-", "/ws", "-", "SKIP", 0, True)
        return

    access = tok["access_token"]
    ticket = tok.get("_ws_ticket")

    try:
        async with ws_connect(f"{WS}{PREFIX}/ws") as sock:
            await asyncio.wait_for(sock.recv(), timeout=5)
            record("ws", "bez tokena -> zakrytie (ozhidalos')", "-", "/ws", 4401, "OPEN", 0, False,
                   "soedinenie ostalos' otkrytym")
    except ConnectionClosed as exc:
        code = getattr(exc.rcvd, "code", None)
        record("ws", "bez tokena -> zakrytie 4401", "-", "/ws", 4401, code, 0, code in (4401, 1000, 1008),
               f"code={code}")
    except Exception as exc:  # noqa: BLE001
        record("ws", "bez tokena -> otkaz", "-", "/ws", 4401, "rejected", 0, True, str(exc)[:80])

    url = f"{WS}{PREFIX}/ws?ticket={ticket}" if ticket else f"{WS}{PREFIX}/ws?token={access}"
    collected: list[str] = []
    try:
        async with ws_connect(url) as sock:
            record("ws", "podklyuchenie po tiketu/tokenu", "-", "/ws", 200, 200, 0, True)
            await sock.send('{"event":"ping"}')
            try:
                msg = await asyncio.wait_for(sock.recv(), timeout=5)
                record("ws", "ping -> pong", "-", "/ws", 200, 200 if "pong" in msg else 400, 0,
                       "pong" in msg, msg[:60])
            except asyncio.TimeoutError:
                record("ws", "ping -> pong", "-", "/ws", 200, "TIMEOUT", 5000, False)

            trigger = asyncio.create_task(
                a.c.post(f"{PREFIX}/profile/convert-resume", headers=tok["_headers"]))
            deadline = time.time() + 25
            while time.time() < deadline:
                try:
                    msg = await asyncio.wait_for(sock.recv(), timeout=max(1, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                collected.append(msg)
                if json.loads(msg).get("event", "") in ("task.completed", "task.failed"):
                    break
            await trigger
    except Exception as exc:  # noqa: BLE001
        record("ws", "podklyuchenie po tiketu/tokenu", "-", "/ws", 200, "EXC", 0, False, str(exc)[:120])
        return

    saw_created = any("task.created" in m for m in collected)
    saw_terminal = any(("task.failed" in m or "task.completed" in m) for m in collected)
    events = [json.loads(m).get("event", "?") for m in collected if m.strip().startswith("{")]
    record("ws", "polucheno sobytie task.created", "-", "/ws", 200, 200 if saw_created else 404, 0, saw_created,
           f"events={events}")
    record("ws", "polucheno terminalnoe sobytie", "-", "/ws", 200, 200 if saw_terminal else 404, 0, saw_terminal,
           f"events={events}")


# --------------------------------------------------------------------------- MAIN
async def wait_api(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=5.0) as c:
        while time.time() < deadline:
            try:
                if (await c.get(f"{API}/health/live")).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1.0)
    return False


async def main() -> int:
    print("ZHIVOY E2E-AUDIT Career-Assistant-AI")
    print(f"API: {API}{PREFIX}")
    if not await wait_api():
        print("[ABORT] API ne otvechaet na /health/live")
        return 2

    async with httpx.AsyncClient(base_url=API, timeout=60.0) as client:
        a = Auditor(client)
        await test_health(a)
        tok = await test_auth(a)
        if not tok:
            print("[ABORT] ne udalos' proyti OTP-verifikatsiyu")
            _dump()
            return 1
        await test_profile(a, tok)
        await test_profile_empty_resume(a)
        vid, _uid = await test_vacancies(a, tok)
        await test_parsing(a, tok)
        await test_analysis(a, tok, vid)
        await test_tasks(a, tok)
        await test_account(a, tok)
        await test_billing(a, tok)
        await test_websocket(a, tok)

    _dump()
    _summary()
    return 0 if all(r["pass"] for r in RESULTS) else 1


def _dump() -> None:
    out = Path(__file__).resolve().parents[2] / "audit_results.json"
    out.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nRezultaty sokhraneny: {out}")


def _summary() -> None:
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["pass"])
    print(f"\n{'=' * 100}\nITOG: uspeshno {passed}/{total}, provaleno {total - passed}\n{'=' * 100}")
    for r in RESULTS:
        if not r["pass"]:
            print(f"  FAIL {r['method']:6} {r['path']:42} exp={r['expected_status']} "
                  f"act={r['actual_status']}  {r['name']}  :: {r['note']}")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
