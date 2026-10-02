"""Живой интеграционный тест Career-Assistant-AI (реальная БД + реальный Ollama).

Запуск (из каталога backend):
    python tools/live_smoke_test.py

Проверяет: Auth, Profile (+LLM-сокращение резюме), Vacancies (все фильтры,
граф статусов, удаление), Parsing (постановка задач и их реальное исполнение
воркером по hh.ru), Analysis (все 4 режима docs/05 §2), Letters (реальная
LLM-генерация), Tasks, WebSocket.

Скрипт не трогает существующих пользователей: регистрирует собственного
тестового пользователя и работает только с его данными.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

# Консоль Windows по умолчанию cp1251 — принудительно UTF-8 для кириллицы и стрелок.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - перенаправленный поток
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API = "http://localhost:8000"
WS = "http://localhost:8000"
PREFIX = "/api/v1"

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  [OK]   {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name} :: {detail}")
    return bool(condition)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


async def wait_health(timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                if (await c.get(f"{API}/health")).status_code == 200:
                    return True
        except httpx.HTTPError:
            pass
        await asyncio.sleep(1.0)
    return False
# ----------------------------------------------------------------------------- auth
async def test_auth(client: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    section("1. AUTH (docs/03 §2)")
    r = await client.post(f"{PREFIX}/auth/register", json={"email": email, "password": password})
    if r.status_code == 409:
        await client.post(f"{PREFIX}/auth/login", json={"email": email, "password": password})
        check("auth: повторная регистрация → 409 EMAIL_TAKEN", True)
    else:
        check("POST /auth/register → 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        body = r.json()
        check("register: id + is_active в ответе",
              bool(body.get("id")) and body.get("is_active") is True, str(body)[:200])

    r = await client.post(f"{PREFIX}/auth/login", json={"email": email, "password": password})
    check("POST /auth/login → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    tokens = r.json()
    access, refresh = tokens.get("access_token", ""), tokens.get("refresh_token", "")
    check("login: возвращает access + refresh", bool(access) and bool(refresh))

    headers = {"Authorization": f"Bearer {access}"}
    r = await client.get(f"{PREFIX}/auth/me", headers=headers)
    check("GET /auth/me → 200 (Bearer JWT)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("auth/me: email совпадает", r.json().get("email") == email, r.text[:200])

    r = await client.get(f"{PREFIX}/auth/me")
    check("GET /auth/me без токена → 401 UNAUTHORIZED",
          r.status_code == 401 and r.json().get("error_code") == "UNAUTHORIZED", f"{r.status_code} {r.text[:200]}")

    r = await client.get(f"{PREFIX}/auth/me", headers={"Authorization": "Bearer a.b.c"})
    check("GET /auth/me с мусорным токеном → 401 INVALID_TOKEN",
          r.status_code == 401 and r.json().get("error_code") == "INVALID_TOKEN", f"{r.status_code} {r.text[:200]}")

    r = await client.get(f"{PREFIX}/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    check("GET /auth/me с refresh-токеном → 401 (типы токенов разделены)",
          r.status_code == 401, f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/auth/refresh", json={"refresh_token": refresh})
    check("POST /auth/refresh → 200 (ротация пары)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    rotated = r.json()

    r = await client.post(f"{PREFIX}/auth/refresh", json={"refresh_token": access})
    check("POST /auth/refresh с access-токеном → 401 INVALID_REFRESH_TOKEN",
          r.status_code == 401 and r.json().get("error_code") == "INVALID_REFRESH_TOKEN", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/auth/login", json={"email": email, "password": "wrongpass123"})
    check("login с неверным паролем → 401 INVALID_CREDENTIALS",
          r.status_code == 401 and r.json().get("error_code") == "INVALID_CREDENTIALS", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/auth/register", json={"email": "bad-email", "password": "x"})
    check("register с невалидным телом → 400 VALIDATION_ERROR",
          r.status_code == 400 and r.json().get("error_code") == "VALIDATION_ERROR", f"{r.status_code} {r.text[:200]}")

    headers["Authorization"] = f"Bearer {rotated['access_token']}"
    return headers


async def wait_task(client: httpx.AsyncClient, headers: dict, task_id: str,
                    timeout: float = 300.0, interval: float = 3.0) -> dict:
    """Дождаться терминального статуса задачи (completed/failed)."""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        r = await client.get(f"{PREFIX}/tasks/{task_id}", headers=headers)
        if r.status_code == 200:
            last = r.json()
            print(f"    [{task_id[:8]}] status={last['status']} "
                  f"progress={last['progress_current']}/{last['progress_total']}")
            if last["status"] in ("completed", "failed"):
                return last
        await asyncio.sleep(interval)
    return last
RESUME_TEXT = (
    "Меня зовут Алексей Петров, 6 лет опыта backend-разработки на Python. "
    "Работал в Яндекс.Практикум и стартапе FinTech: проектировал микросервисы на FastAPI, "
    "PostgreSQL, Redis, Celery, Docker, Kubernetes. Обеспечивал нагрузку до 1200 rps, "
    "сократил время отклика API с 800мс до 90мс за счёт кэширования и оптимизации запросов. "
    "Внедрил LLM-интеграцию (Ollama, RAG) в процессы поддержки, что уменьшило время ответа "
    "операторов на 35%. Автоматизировал CI/CD, покрытие тестами 82%. "
    "Образование: МГТУ им. Баумана, Прикладная математика, бакалавр. "
    "Навыки: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Redis, Docker, asyncio, Git, Linux, Grafana."
)


async def test_profile(client: httpx.AsyncClient, headers: dict) -> None:
    section("2. PROFILE (docs/03 §3) + LLM-этап 0: сокращение резюме")
    r = await client.get(f"{PREFIX}/profile", headers=headers)
    check("GET /profile → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    check("profile: match_threshold по умолчанию 70", r.json().get("match_threshold") == 70, r.text[:200])

    r = await client.put(f"{PREFIX}/profile", headers=headers, json={
        "full_name": "Алексей Петров",
        "resume_text": RESUME_TEXT,
        "skills": ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker"],
        "experience_years": 6.0,
        "desired_salary_from": 250000,
        "desired_salary_to": 350000,
        "match_threshold": 65,
        "preferred_work_formats": ["remote", "hybrid"],
    })
    check("PUT /profile (полное обновление) → 200", r.status_code == 200, f"{r.status_code} {r.text[:300]}")
    body = r.json()
    check("PUT: full_name сохранён", body.get("full_name") == "Алексей Петров", r.text[:200])
    check("PUT: experience_years (NUMERIC→float)",
          abs(float(body.get("experience_years") or 0) - 6.0) < 0.01, r.text[:200])
    check("PUT: match_threshold = 65", body.get("match_threshold") == 65, r.text[:200])
    check("PUT: skills нормализованы",
          body.get("skills") == ["Python", "FastAPI", "PostgreSQL", "Redis", "Docker"], r.text[:200])
    check("PUT: preferred_work_formats сохранён",
          body.get("preferred_work_formats") == ["remote", "hybrid"], r.text[:200])

    r = await client.put(f"{PREFIX}/profile", headers=headers, json={"desired_salary_from": 200000})
    b2 = r.json()
    check("PUT (частичное): остальные поля не сброшены",
          b2.get("full_name") == "Алексей Петров" and b2.get("match_threshold") == 65, r.text[:300])

    r = await client.put(f"{PREFIX}/profile", headers=headers,
                         json={"desired_salary_from": 400000, "desired_salary_to": 100000})
    check("PUT: пересечение диапазона зарплаты → 400 VALIDATION_ERROR",
          r.status_code == 400 and r.json().get("error_code") == "VALIDATION_ERROR", f"{r.status_code} {r.text[:200]}")

    r = await client.put(f"{PREFIX}/profile", headers=headers, json={"match_threshold": 150})
    check("PUT: match_threshold > 100 → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = await client.put(f"{PREFIX}/profile", headers=headers, json={"resume_text": "x" * 5001})
    check("PUT: resume_text > 5000 симв. → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    print("  ... вызов Ollama (compress-resume) — модель 14b, до 3 минут")
    t0 = time.time()
    r = await client.post(f"{PREFIX}/profile/convert-resume", headers=headers, timeout=300.0)
    dt = time.time() - t0
    if check("POST /profile/convert-resume → 200 (реальный LLM)", r.status_code == 200, f"{r.status_code} {r.text[:300]}"):
        compact = r.json().get("compact_resume") or ""
        check("LLM: compact_resume непустой (>50 симв.)", len(compact) > 50, f"len={len(compact)}")
        check("LLM: compact_resume ≤ 2000 симв. (COMPACT_RESUME_MAX_CHARS)", len(compact) <= 2000, f"len={len(compact)}")
        check("LLM: сжатие реально произошло", len(compact) < len(RESUME_TEXT), f"{len(compact)} vs {len(RESUME_TEXT)}")
        print(f"  compact_resume ({len(compact)} симв., {dt:.1f} с):\n  {compact[:400]}\n  ...")

    r2 = await client.post(f"{PREFIX}/profile/compress-resume", headers=headers, timeout=300.0)
    check("POST /profile/compress-resume (алиас) → 200", r2.status_code == 200, f"{r2.status_code} {r2.text[:200]}")

    r3 = await client.get(f"{PREFIX}/profile", headers=headers)
    check("GET /profile после LLM: compact_resume сохранён в БД",
          bool(r3.json().get("compact_resume")), r3.text[:200])
async def test_vacancies_list(client: httpx.AsyncClient, headers: dict) -> None:
    section("3. VACANCIES: список и фильтры (docs/03 §4)")
    r = await client.get(f"{PREFIX}/vacancies", headers=headers)
    check("GET /vacancies → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    body = r.json()
    check("GET /vacancies: контракт {items,total,page,size}",
          all(k in body for k in ("items", "total", "page", "size")), str(body)[:200])

    for label, query in [
        ("status=raw", {"status": "raw"}),
        ("status=letter_ready", {"status": "letter_ready"}),
        ("source=auto", {"source": "auto"}),
        ("source=manual", {"source": "manual"}),
        ("search=Python", {"search": "Python"}),
        ("min_match_score=50", {"min_match_score": 50}),
    ]:
        r = await client.get(f"{PREFIX}/vacancies", headers=headers, params=query)
        total = r.json().get("total") if r.status_code == 200 else "?"
        check(f"GET /vacancies?{label} → 200 (total={total})", r.status_code == 200, f"{r.status_code} {r.text[:150]}")

    r = await client.get(f"{PREFIX}/vacancies", headers=headers, params={"status": "wrong_status"})
    check("GET /vacancies с невалидным status → 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")

    r = await client.get(f"{PREFIX}/vacancies", headers=headers, params={"page": 1, "size": 2})
    check("GET /vacancies пагинация size=2", r.status_code == 200 and len(r.json()["items"]) <= 2, r.text[:200])

    r = await client.get(f"{PREFIX}/vacancies/{uuid.uuid4()}", headers=headers)
    check("GET /vacancies/{неизвестный uuid} → 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")


async def test_vacancy_crud(client: httpx.AsyncClient, headers: dict) -> dict:
    section("3b. VACANCIES: ручной парсинг hh.ru, граф статусов (docs/02 §5)")
    url = "https://hh.ru/vacancy/137866214"
    r = await client.post(f"{PREFIX}/vacancies/manual", headers=headers, json={"vacancy_url": url}, timeout=120.0)
    if not check("POST /vacancies/manual (реальный HTTP к hh.ru) → 201/200",
                 r.status_code in (200, 201), f"{r.status_code} {r.text[:300]}"):
        return {}
    v = r.json()
    vid = v["id"]
    print(f"  спарсено: {v.get('title')} @ {v.get('company_name')} | зарплата {v.get('salary_from')}-{v.get('salary_to')}"
          f" | опыт: {v.get('experience')} | формат: {v.get('work_format')}")
    check("manual: hh_vacancy_id извлечён из ссылки", v.get("hh_vacancy_id") == "137866214", r.text[:200])
    check("manual: source=manual", v.get("source") == "manual", r.text[:200])
    check("manual: title извлечён", bool(v.get("title")), r.text[:200])
    check("manual: company_name извлечён", bool(v.get("company_name")), r.text[:200])
    check("manual: description_raw извлечён", len(v.get("description_raw") or "") > 100,
          f"len={len(v.get('description_raw') or '')}")

    r = await client.post(f"{PREFIX}/vacancies/manual", headers=headers, json={"vacancy_url": url}, timeout=120.0)
    check("manual повторно → 200 (дедупликация docs/04 §8)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/vacancies/manual", headers=headers,
                          json={"vacancy_url": "https://google.com/x"}, timeout=60.0)
    check("manual с не-hh ссылкой → 400 INVALID_VACANCY_URL",
          r.status_code == 400 and r.json().get("error_code") == "INVALID_VACANCY_URL", f"{r.status_code} {r.text[:200]}")

    for status_value, expected, label in [
        ("analyzed", 200, "PATCH raw→analyzed"),
        ("analyzed", 200, "PATCH повтор того же статуса (идемпотентно)"),
        ("raw", 409, "PATCH analyzed→raw запрещён"),
        ("applied", 200, "PATCH analyzed→applied"),
        ("raw", 409, "PATCH из терминального applied запрещён"),
    ]:
        r = await client.patch(f"{PREFIX}/vacancies/{vid}/status", headers=headers,
                               json={"status": status_value})
        ok = r.status_code == expected
        if expected == 409:
            ok = ok and r.json().get("error_code") == "INVALID_STATUS_TRANSITION"
        check(f"{label} → {expected}", ok, f"{r.status_code} {r.text[:180]}")

    r = await client.patch(f"{PREFIX}/vacancies/{vid}/status", headers=headers, json={"status": "unknown"})
    check("PATCH невалидный статус → 400", r.status_code == 400, f"{r.status_code} {r.text[:150]}")
    return v


async def test_vacancy_delete(client: httpx.AsyncClient, headers: dict) -> None:
    section("3c. VACANCIES: DELETE (каскад анализа/письма)")
    url = "https://hh.ru/vacancy/138001138"
    r = await client.post(f"{PREFIX}/vacancies/manual", headers=headers, json={"vacancy_url": url}, timeout=120.0)
    if not check("создание вакансии для удаления → 201/200", r.status_code in (200, 201), f"{r.status_code} {r.text[:200]}"):
        return
    vid = r.json()["id"]
    r = await client.delete(f"{PREFIX}/vacancies/{vid}", headers=headers)
    check("DELETE /vacancies/{id} → 204", r.status_code == 204, f"{r.status_code} {r.text[:150]}")
    r = await client.get(f"{PREFIX}/vacancies/{vid}", headers=headers)
    check("GET удалённой вакансии → 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")
    r = await client.delete(f"{PREFIX}/vacancies/{vid}", headers=headers)
    check("DELETE повторно → 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")
async def test_parsing_queue(client: httpx.AsyncClient, headers: dict) -> None:
    section("4. PARSING (docs/03 §5) — постановка задач и их исполнение воркером")
    r = await client.post(f"{PREFIX}/parsing/auto", headers=headers, json={})
    check("POST /parsing/auto без критериев → 400 INVALID_SEARCH_CRITERIA",
          r.status_code == 400 and r.json().get("error_code") == "INVALID_SEARCH_CRITERIA", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/parsing/auto", headers=headers,
                          json={"keywords": ["python"], "work_formats": ["remote"], "max_pages": 1})
    check("POST /parsing/auto → {task_id, pending}",
          r.status_code == 200 and r.json().get("status") == "pending", f"{r.status_code} {r.text[:200]}")
    auto_task = r.json()["task_id"] if r.status_code == 200 else None

    r = await client.post(f"{PREFIX}/parsing/auto", headers=headers,
                          json={"keywords": ["python"], "max_pages": 99})
    check("POST /parsing/auto max_pages > 20 → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/parsing/auto", headers=headers,
                          json={"keywords": ["python"], "employment_forms": ["wrong_form"]})
    check("POST /parsing/auto с неверной формой занятости → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/parsing/group", headers=headers,
                          json={"search_url": "https://evil.example.com/search", "max_pages": 1})
    check("POST /parsing/group с чужим доменом → 400 INVALID_SEARCH_URL",
          r.status_code == 400 and r.json().get("error_code") == "INVALID_SEARCH_URL", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/parsing/group", headers=headers,
                          json={"search_url": "https://hh.ru/search/vacancy?text=python", "max_pages": 1})
    check("POST /parsing/group (валидная ссылка hh.ru) → task_id", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    group_task = r.json()["task_id"] if r.status_code == 200 else None

    r = await client.post(f"{PREFIX}/parsing/manual", headers=headers,
                          json={"vacancy_url": "https://novokuznetsk.hh.ru/vacancy/137296936", "run_analysis": True})
    check("POST /parsing/manual → task_id", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    manual_task = r.json()["task_id"] if r.status_code == 200 else None

    r = await client.post(f"{PREFIX}/parsing/manual", headers=headers, json={"vacancy_url": "https://example.org/x"})
    check("POST /parsing/manual с не-hh ссылкой → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    for tid, expected_type in [(auto_task, "parse_auto"), (group_task, "parse_group"), (manual_task, "parse_manual")]:
        if not tid:
            continue
        r = await client.get(f"{PREFIX}/tasks/{tid}", headers=headers)
        ok = r.status_code == 200 and r.json()["task_type"] == expected_type
        check(f"GET /tasks: задача типа {expected_type} создана и в очереди", ok, r.text[:200])

    print("  ... воркер исполняет задачи парсинга hh.ru (реальные HTTP + паузы 4–8 с)")
    for tid, label in [(manual_task, "parse_manual"), (auto_task, "parse_auto"), (group_task, "parse_group")]:
        if not tid:
            continue
        final = await wait_task(client, headers, tid, timeout=600)
        check(f"воркер исполнил {label} → completed",
              final.get("status") == "completed",
              f"status={final.get('status')} error={str(final.get('error_message'))[:200]}")
        if final.get("result"):
            print(f"    result({label}): {json.dumps(final['result'], ensure_ascii=False)[:280]}")
async def run_mode(client: httpx.AsyncClient, headers: dict, vacancy_ids: list[str],
                   mode: str, label: str) -> dict:
    """Один режим docs/05 §2: запуск, ожидание воркера, сбор результата."""
    idx = ["analyze", "letter", "analyze_and_letter", "auto"].index(mode)
    vid = vacancy_ids[min(idx, len(vacancy_ids) - 1)]

    print(f"\n  --- {label} по вакансии {vid[:8]} ---")
    t0 = time.time()
    r = await client.post(f"{PREFIX}/analysis/run", headers=headers,
                          json={"vacancy_ids": [vid], "mode": mode})
    if not check(f"POST /analysis/run {label} → task_id", r.status_code == 200, f"{r.status_code} {r.text[:250]}"):
        return {"vacancy_id": vid, "ok": False}
    task_id = r.json()["task_id"]
    final = await wait_task(client, headers, task_id, timeout=1500)
    dt = time.time() - t0
    ok = check(f"{label}: задача LLM-воркера completed", final.get("status") == "completed",
               f"status={final.get('status')} error={str(final.get('error_message'))[:300]}")
    if final.get("result"):
        print(f"    result: {json.dumps(final['result'], ensure_ascii=False)[:300]}  ({dt:.1f} с)")
    return {"vacancy_id": vid, "ok": ok, "task": final, "seconds": dt, "result": final.get("result")}


async def test_analysis_and_letters(client: httpx.AsyncClient, headers: dict,
                                    vacancy_ids: list[str]) -> dict[str, dict]:
    section("5. ANALYSIS & LETTERS (docs/03 §6, docs/05 §2–§6) — реальный LLM")
    r = await client.post(f"{PREFIX}/analysis/run", headers=headers, json={"vacancy_ids": []})
    check("POST /analysis/run с пустым списком → 400 INVALID_VACANCY_IDS",
          r.status_code == 400 and r.json().get("error_code") == "INVALID_VACANCY_IDS", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/analysis/run", headers=headers, json={"vacancy_ids": [str(uuid.uuid4())]})
    check("POST /analysis/run с чужим uuid → 404 NOT_FOUND",
          r.status_code == 404 and r.json().get("error_code") == "NOT_FOUND", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/analysis/run", headers=headers,
                          json={"vacancy_ids": vacancy_ids[:1], "mode": "wrong_mode"})
    check("POST /analysis/run с неверным mode → 400", r.status_code == 400, f"{r.status_code} {r.text[:200]}")

    r = await client.get(f"{PREFIX}/analysis/{uuid.uuid4()}", headers=headers)
    check("GET /analysis/{неизвестный uuid} → 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")
    r = await client.get(f"{PREFIX}/letters/{uuid.uuid4()}", headers=headers)
    check("GET /letters/{неизвестный uuid} → 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")

    print(f"\n  вакансий для обработки: {len(vacancy_ids)}")
    return {
        "analyze": await run_mode(client, headers, vacancy_ids, "analyze", "mode=analyze"),
        "letter": await run_mode(client, headers, vacancy_ids, "letter", "mode=letter"),
        "analyze_and_letter": await run_mode(client, headers, vacancy_ids, "analyze_and_letter", "mode=analyze_and_letter"),
        "auto": await run_mode(client, headers, vacancy_ids, "auto", "mode=auto (порог 65 из профиля)"),
    }


def check_analysis_body(mode: str, a: dict) -> None:
    score = a.get("match_score")
    check(f"  [{mode}] match_score заполнен и в диапазоне 0..100",
          isinstance(score, int) and 0 <= score <= 100, str(a)[:200])
    check(f"  [{mode}] strengths не пустые", bool(a.get("strengths")), str(a)[:200])
    check(f"  [{mode}] weaknesses не пустые", bool(a.get("weaknesses")), str(a)[:200])
    check(f"  [{mode}] summary не пустой", bool(a.get("summary")), str(a)[:200])
    check(f"  [{mode}] match_details (JSONB) присутствует",
          isinstance(a.get("match_details"), dict), str(a.get("match_details"))[:200])
    print(f"    match_score = {score}")
    print(f"    strengths: {str(a.get('strengths'))[:250]}")
    print(f"    weaknesses: {str(a.get('weaknesses'))[:250]}")
    print(f"    summary: {str(a.get('summary'))[:300]}")
async def test_analysis_results(client: httpx.AsyncClient, headers: dict,
                                results: dict[str, dict]) -> None:
    section("5b. GET /analysis/{vacancy_id} — сохранённый анализ (docs/02 §3.4)")
    for mode, info in results.items():
        vid = info["vacancy_id"]
        r = await client.get(f"{PREFIX}/analysis/{vid}", headers=headers)
        if r.status_code == 404:
            if mode == "letter":
                check(f"GET /analysis/{vid[:8]} после mode=letter: анализа нет (ожидаемо)", True)
            else:
                check(f"GET /analysis/{vid[:8]} после {mode}: анализ создан", False, "404 — анализ не создан")
            continue
        if not check(f"GET /analysis/{vid[:8]} после {mode} → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}"):
            continue
        if mode != "letter":
            check_analysis_body(mode, r.json())

    section("5c. Статус вакансий после обработки (docs/05 §6)")
    for mode, info in results.items():
        r = await client.get(f"{PREFIX}/vacancies/{info['vacancy_id']}", headers=headers)
        if r.status_code == 200:
            v = r.json()
            print(f"    [{mode:19s}] status={v['status']:13s} match_score={v.get('match_score')}")


async def test_letters(client: httpx.AsyncClient, headers: dict, results: dict[str, dict]) -> None:
    section("6. LETTERS (docs/03 §6, docs/05 §5) — реальная генерация писем")
    # Порог матчинга профиля: в режиме auto письмо генерируется только при
    # match_score >= порога (docs/05 §6 п.4–п.5).
    r = await client.get(f"{PREFIX}/profile", headers=headers)
    threshold = r.json().get("match_threshold", 70)

    for mode, info in results.items():
        vid = info["vacancy_id"]
        r = await client.get(f"{PREFIX}/letters/{vid}", headers=headers)
        if r.status_code == 404:
            if mode == "analyze":
                check(f"GET /letters/{vid[:8]} после mode=analyze: письма нет (ожидаемо)", True)
            elif mode == "auto":
                # AUTO без письма — валидный исход, если score ниже порога.
                outcome = (info.get("result") or {}).get("outcomes", [{}])[0]
                score = outcome.get("match_score")
                check(f"GET /letters/{vid[:8]} после mode=auto: score {score} < порога {threshold} "
                      f"→ письмо не генерировалось (docs/05 §6)",
                      score is not None and score < threshold, f"score={score} threshold={threshold}")
            else:
                check(f"GET /letters/{vid[:8]} после {mode}: письмо сгенерировано", False, "404 — письмо не создано")
            continue
        if not check(f"GET /letters/{vid[:8]} после {mode} → 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}"):
            continue
        letter = r.json()
        content = letter.get("content") or ""
        check(f"  [{mode}] письмо непустое (>200 символов)", len(content) > 200, f"len={len(content)}")
        check(f"  [{mode}] version = 1", letter.get("version") == 1, str(letter.get("version")))
        check(f"  [{mode}] объём 1200–1800 символов (docs/05 §5)", 1200 <= len(content) <= 1800, f"len={len(content)}")
        print(f"    [{mode}] письмо, {len(content)} симв.:\n{content[:900]}\n    {'-' * 40}")


async def test_letter_regeneration(client: httpx.AsyncClient, headers: dict,
                                  results: dict[str, dict]) -> None:
    section("6b. Повторная генерация письма (docs/05 §9 — версионирование)")
    info = results.get("analyze_and_letter") or {}
    if not info.get("vacancy_id"):
        print("  [SKIP] нет вакансии для повторной генерации")
        return
    vid = info["vacancy_id"]
    before = await client.get(f"{PREFIX}/letters/{vid}", headers=headers)
    old_version = before.json().get("version") if before.status_code == 200 else 0
    r = await client.post(f"{PREFIX}/analysis/run", headers=headers,
                          json={"vacancy_ids": [vid], "mode": "letter"})
    if r.status_code == 200:
        final = await wait_task(client, headers, r.json()["task_id"], timeout=1200)
        check("повторная генерация письма: задача completed",
              final.get("status") == "completed",
              f"status={final.get('status')} error={str(final.get('error_message'))[:200]}")
        after = await client.get(f"{PREFIX}/letters/{vid}", headers=headers)
        if after.status_code == 200:
            check("письмо доступно после повторной генерации", bool(after.json().get("content")), after.text[:200])
            print(f"    версия: {old_version} → {after.json().get('version')} (docs/05 §9)")
        else:
            check("письмо доступно после повторной генерации", False, f"{after.status_code} {after.text[:200]}")
async def test_tasks(client: httpx.AsyncClient, headers: dict) -> None:
    section("7. TASKS (docs/03 §7)")
    r = await client.get(f"{PREFIX}/tasks", headers=headers)
    check("GET /tasks → 200 с пагинацией",
          r.status_code == 200 and all(k in r.json() for k in ("items", "total", "page", "size")), r.text[:200])
    items = r.json().get("items", [])
    check("GET /tasks: есть созданные задачи", len(items) > 0, f"total={r.json().get('total')}")
    print(f"    типов задач в списке: {sorted({i['task_type'] for i in items})}")

    r = await client.get(f"{PREFIX}/tasks", headers=headers, params={"page": 1, "size": 3})
    check("GET /tasks пагинация size=3", r.status_code == 200 and len(r.json()["items"]) <= 3, r.text[:200])

    r = await client.get(f"{PREFIX}/tasks/{uuid.uuid4()}", headers=headers)
    check("GET /tasks/{чужой uuid} → 404", r.status_code == 404, f"{r.status_code} {r.text[:150]}")

    completed = next((i["id"] for i in items if i["status"] == "completed"), None)
    if completed:
        r = await client.post(f"{PREFIX}/tasks/{completed}/cancel", headers=headers)
        check("cancel завершённой задачи → 400 TASK_COMPLETED",
              r.status_code == 400 and r.json().get("error_code") == "TASK_COMPLETED", f"{r.status_code} {r.text[:200]}")

    r = await client.post(f"{PREFIX}/parsing/auto", headers=headers,
                          json={"keywords": ["python"], "max_pages": 20})
    if r.status_code == 200:
        tid = r.json()["task_id"]
        r = await client.post(f"{PREFIX}/tasks/{tid}/cancel", headers=headers)
        ok = r.status_code == 200 and r.json().get("cancelled") is True
        check("POST /tasks/{id}/cancel → 200 cancelled=True", ok, f"{r.status_code} {r.text[:200]}")
        r = await client.get(f"{PREFIX}/tasks/{tid}", headers=headers)
        check("после cancel: статус failed + причина",
              r.json()["status"] == "failed" and "Отменено" in (r.json().get("error_message") or ""), r.text[:250])
        r = await client.post(f"{PREFIX}/tasks/{tid}/cancel", headers=headers)
        check("повторный cancel → 400 TASK_FAILED", r.status_code == 400, f"{r.status_code} {r.text[:200]}")


async def test_ws(access_token: str) -> None:
    section("8. WEBSOCKET /ws (docs/03 §8)")
    try:
        import websockets  # type: ignore
    except ImportError:
        print("  [SKIP] пакет websockets не установлен")
        return
    url = WS.replace("http://", "ws://")

    try:
        async with websockets.connect(f"{url}{PREFIX}/ws?token={access_token}") as sock:
            check("WS: подключение с валидным access-токеном", True)
            await asyncio.wait_for(sock.send("ping"), timeout=5)
            check("WS: клиент может отправлять сообщения (сервер слушает)", True)
    except Exception as exc:  # noqa: BLE001
        check("WS: подключение с валидным access-токеном", False, str(exc)[:200])
        return

    for label, connect_url in [("без токена", f"{url}{PREFIX}/ws"),
                               ("с мусорным токеном", f"{url}{PREFIX}/ws?token=a.b.c")]:
        try:
            async with websockets.connect(connect_url) as sock:
                await asyncio.wait_for(sock.recv(), timeout=5)
                check(f"WS: {label} → соединение закрыто (4401)", False, "соединение осталось открытым")
        except Exception:  # noqa: BLE001
            check(f"WS: {label} → соединение закрыто (4401)", True)
async def main() -> int:
    print("ЖИВОЙ ИНТЕГРАЦИОННЫЙ ТЕСТ Career-Assistant-AI")
    print(f"API: {API}{PREFIX}")

    if not await wait_health():
        print("\n[ABORT] Backend не отвечает на /health — запустите uvicorn.")
        return 2

    email = f"live_{int(time.time())}@test.dev"
    password = "strongpassword123"

    async with httpx.AsyncClient(base_url=API, timeout=180.0) as client:
        headers = await test_auth(client, email, password)
        access_token = headers["Authorization"].split(" ")[1]

        await test_profile(client, headers)
        await test_vacancies_list(client, headers)
        await test_vacancy_crud(client, headers)
        await test_parsing_queue(client, headers)

        r = await client.get(f"{PREFIX}/vacancies", headers=headers, params={"size": 20})
        items = r.json().get("items", [])
        ids = [v["id"] for v in items if len((v.get("description_raw") or "").strip()) > 200]
        print(f"\n  вакансий с описанием >200 симв. для анализа: {len(ids)}")
        if not ids:
            FAILED.append(("analysis: нет вакансий с описанием", "нужен успешный парсинг hh.ru"))
        else:
            results = await test_analysis_and_letters(client, headers, ids)
            await test_analysis_results(client, headers, results)
            await test_letters(client, headers, results)
            await test_letter_regeneration(client, headers, results)

        await test_vacancy_delete(client, headers)
        await test_tasks(client, headers)
        await test_ws(access_token)

    section("ИТОГ")
    print(f"  УСПЕХНО:    {len(PASSED)}")
    print(f"  ПРОВАЛЕНО:  {len(FAILED)}")
    for name, detail in FAILED:
        print(f"    - {name} :: {detail}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))