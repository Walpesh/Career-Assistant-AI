"""Live E2E-аудит Career-Assistant-AI (временный диагностический скрипт).

Сценарии: auth (register -> OTP -> verify -> JWT -> refresh), парсинг с
multi-source, WS-события задач, хранение/фильтры вакансий, analysis/run
через Ollama. Запуск: cd backend && python _audit_e2e.py (uvicorn на :8000).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import asyncpg
import httpx
import websockets

from app.core.config import settings
from app.core.mail import hash_otp_code

BASE = "http://127.0.0.1:8000/api/v1"
DSN = "postgresql://career:career@localhost:5432/career_assistant"
EMAIL = f"audit-{uuid.uuid4().hex[:8]}@gmail.com"
PASSWORD = "Audit-Passw0rd!"

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name} :: {detail}", flush=True)


async def find_otp_code(email: str) -> str | None:
    """Подобрать 6-значный OTP-код по HMAC-хэшу из БД (dev-обход почты)."""
    conn = await asyncpg.connect(DSN)
    try:
        row = await conn.fetchrow(
            "SELECT otp_code_hash FROM email_otps WHERE email=$1", email.lower()
        )
    finally:
        await conn.close()
    if row is None:
        return None
    target = row["otp_code_hash"]
    for i in range(10**6):
        code = f"{i:06d}"
        if hash_otp_code(email, code) == target:
            return code
    return None


async def wait_task(task_id: str, token: str, timeout: float = 90.0) -> dict:
    """Опрос GET /tasks/{id} до терминального статуса."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    async with httpx.AsyncClient(timeout=15) as client:
        while time.monotonic() < deadline:
            r = await client.get(
                f"{BASE}/tasks/{task_id}", headers={"Authorization": f"Bearer {token}"}
            )
            if r.status_code == 200:
                last = r.json()
                if last.get("status") in {"completed", "failed", "waiting_captcha"}:
                    return last
            await asyncio.sleep(1.0)
    return last


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        # ---------- 1. Auth: register -> OTP -> verify ----------
        r = await client.post(
            f"{BASE}/auth/register", json={"email": EMAIL, "password": PASSWORD}
        )
        check("register -> 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")

        r = await client.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": PASSWORD})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check(
            "login до verify -> 403 EMAIL_NOT_VERIFIED",
            r.status_code == 403 and body.get("error_code") == "EMAIL_NOT_VERIFIED",
            f"{r.status_code} {body}",
        )

        r = await client.post(f"{BASE}/auth/login", json={"email": EMAIL, "password": "wrong-pass"})
        check("login с неверным паролем -> 401", r.status_code == 401, f"{r.status_code}")

        code = await find_otp_code(EMAIL)
        check("OTP-код найден подбором HMAC", code is not None, f"code={code}")

        r = await client.post(
            f"{BASE}/auth/verify-email",
            json={"email": EMAIL, "code": "000000" if code else "111111"},
        )
        if code:
            check("verify-email с неверным кодом -> 400", r.status_code == 400, f"{r.status_code}")

        r = await client.post(f"{BASE}/auth/verify-email", json={"email": EMAIL, "code": code})
        ok = r.status_code == 200 and bool(r.json().get("access_token"))
        check("verify-email -> 200 + пара JWT", ok, f"{r.status_code}")
        if not ok:
            print(json.dumps(results, ensure_ascii=False, indent=2))
            return
        tokens = r.json()
        access = tokens["access_token"]
        refresh_cookie = "; ".join(
            f"{c.name}={c.value}" for c in client.cookies.jar if c.name == "refresh_token"
        )
        auth = {"Authorization": f"Bearer {access}"}

        r = await client.get(f"{BASE}/auth/me", headers=auth)
        check("GET /auth/me", r.status_code == 200 and r.json().get("email") == EMAIL, f"{r.status_code}")

        # Refresh-ротация (HttpOnly cookie).
        r = await client.post(
            f"{BASE}/auth/refresh", headers={"Cookie": refresh_cookie} if refresh_cookie else {}
        )
        rotated = r.status_code == 200 and bool(r.json().get("access_token"))
        check(
            "POST /auth/refresh -> ротация access",
            rotated,
            f"{r.status_code} cookie={'yes' if refresh_cookie else 'NO'}",
        )
        if rotated:
            access = r.json()["access_token"]
            auth = {"Authorization": f"Bearer {access}"}

        # ---------- 2. WS: тикет + канал ----------
        r = await client.post(f"{BASE}/auth/ws-ticket", headers=auth)
        check("POST /auth/ws-ticket", r.status_code == 200, f"{r.status_code}")
        ticket = r.json().get("ticket") or r.json().get("ws_ticket")

        ws_events: list[dict] = []
        ws_url = f"ws://127.0.0.1:8000/api/v1/ws?ticket={ticket}"
        ws = await websockets.connect(ws_url, open_timeout=10)

        async def pump() -> None:
            try:
                async for raw in ws:
                    ws_events.append(json.loads(raw))
            except websockets.ConnectionClosed:
                pass

        ws_task = asyncio.create_task(pump())
        await asyncio.sleep(0.3)
        check("WS подключён по тикету", True, f"ticket={str(ticket)[:8]}...")

        await ws.send(json.dumps({"event": "ping"}))
        await asyncio.sleep(0.5)
        check(
            "WS ping -> pong",
            any(e.get("event") == "pong" for e in ws_events),
            str([e.get("event") for e in ws_events[:5]]),
        )

        # ---------- 3. Парсинг: auto с city + sources ----------
        payload = {
            "keywords": ["python"],
            "city": "Москва",
            "sources": ["hh"],
            "max_pages": 1,
            "match_threshold": 70,
        }
        r = await client.post(f"{BASE}/parsing/auto", json=payload, headers=auth)
        body = r.json() if r.status_code == 200 else {}
        check(
            "POST /parsing/auto (sources=[hh], city) -> task",
            r.status_code == 200 and body.get("status") == "pending",
            f"{r.status_code} {r.text[:200]}",
        )
        parse_task = body.get("task_id")

        # Некорректный источник: Pydantic Literal -> RequestValidationError,
        # глобальный обработчик app/main.py отдаёт 400 VALIDATION_ERROR
        # (docs/03 §9: «400 — ошибка валидации», не 422).
        r = await client.post(
            f"{BASE}/parsing/auto",
            json={"keywords": ["python"], "sources": ["unknown_board"]},
            headers=auth,
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        check(
            "POST /parsing/auto sources=[unknown] -> 400 VALIDATION_ERROR",
            r.status_code == 400 and body.get("error_code") == "VALIDATION_ERROR",
            f"{r.status_code}",
        )

        if parse_task:
            final = await wait_task(parse_task, access, timeout=120)
            print(f"    parse task final: {final.get('status')} {final.get('error_message') or ''}", flush=True)
            ws_kinds = [e.get("event") for e in ws_events]
            check(
                "WS получил task.* события парсинга",
                any(str(k).startswith("task.") for k in ws_kinds),
                str(ws_kinds[:10]),
            )
            check(
                "Задача парсинга финализирована",
                final.get("status") in {"completed", "failed", "waiting_captcha"},
                f"{final.get('status')} progress={final.get('progress_current')}/{final.get('progress_total')} "
                f"err={str(final.get('error_message'))[:120]}",
            )

        # ---------- 4. Вакансии: фильтры ----------
        r = await client.get(f"{BASE}/vacancies?page=1&size=20", headers=auth)
        check("GET /vacancies -> 200", r.status_code == 200, f"{r.status_code}")
        total = r.json().get("total", 0) if r.status_code == 200 else -1
        r2 = await client.get(f"{BASE}/vacancies?source=hh", headers=auth)
        hh_total = r2.json().get("total") if r2.status_code == 200 else "?"
        check("GET /vacancies?source=hh", r2.status_code == 200, f"total={total} hh={hh_total}")

        # ---------- 5. Analysis: вакансия в БД + compact_resume + run ----------
        conn = await asyncpg.connect(DSN)
        try:
            user_id = await conn.fetchval("SELECT id FROM users WHERE email=$1", EMAIL.lower())
            vac = await conn.fetchrow(
                """INSERT INTO vacancies (id, user_id, hh_vacancy_id, url, source, status, title,
                                          company_name, description_raw, created_at, updated_at)
                   VALUES ($1,$2,$3,$4,'hh','raw','Python Developer (audit)',
                           'Audit Corp','Ищем Python-разработчика с опытом FastAPI и PostgreSQL.',
                           now(), now()) RETURNING id""",
                uuid.uuid4(), user_id, f"9{uuid.uuid4().hex[:8]}", "https://hh.ru/vacancy/900000001",
            )
            await conn.execute(
                """INSERT INTO user_profiles (user_id, compact_resume, created_at, updated_at)
                   VALUES ($1,$2,now(),now())
                   ON CONFLICT (user_id) DO UPDATE SET compact_resume=$2""",
                user_id,
                "Python-разработчик, 5 лет: FastAPI, PostgreSQL, asyncio, Docker. Ищет удалённую работу.",
            )
        finally:
            await conn.close()

        r = await client.post(
            f"{BASE}/analysis/run",
            json={"vacancy_ids": [str(vac["id"])], "mode": "analyze"},
            headers=auth,
        )
        body = r.json() if r.status_code in (200, 201) else {}
        check(
            "POST /analysis/run -> task",
            r.status_code == 200 and bool(body.get("task_id")),
            f"{r.status_code} {r.text[:200]}",
        )
        llm_t0 = time.monotonic()
        if body.get("task_id"):
            final = await wait_task(body["task_id"], access, timeout=180)
            elapsed = time.monotonic() - llm_t0
            check(
                "LLM-задача analyze завершилась",
                final.get("status") == "completed",
                f"status={final.get('status')} elapsed={elapsed:.1f}s "
                f"err={str(final.get('error_message'))[:160]}",
            )
            r = await client.get(f"{BASE}/analysis/{vac['id']}", headers=auth)
            if r.status_code == 200:
                data = r.json()
                check(
                    "GET /analysis/{id} -> match_score",
                    data.get("match_score") is not None,
                    f"score={data.get('match_score')} summary={str(data.get('summary'))[:80]}",
                )
            else:
                check("GET /analysis/{id}", False, f"{r.status_code}")

        # ---------- 6. Прямой замер Ollama ----------
        async with httpx.AsyncClient(timeout=120) as ollama:
            t0 = time.monotonic()
            resp = await ollama.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": settings.llm_model,
                    "prompt": "Ответь одним словом: работоспособен?",
                    "stream": False,
                    "options": {"num_predict": 10},
                },
            )
            dt = time.monotonic() - t0
            ok = resp.status_code == 200 and bool(resp.json().get("response"))
            check(f"Ollama /api/generate ({settings.llm_model})", ok, f"{resp.status_code} {dt:.2f}s")

        await ws.close()
        await ws_task

    failed = [row for row in results if row[0] == "FAIL"]
    print(f"\n=== E2E: {len(results) - len(failed)} passed, {len(failed)} failed ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

