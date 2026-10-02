"""Интеграционные тесты: Auth flow + Profile updates (docs/03 §2–§3).

Покрытие:
    - register / login / refresh / me (JWT, bcrypt, коды 201/401/409);
    - GET/PUT /profile (частичное обновление, валидация 0–100 / 5000 симв.);
    - POST /profile/convert-resume и алиас /profile/compress-resume (LLM мокается).
"""

from __future__ import annotations

import importlib

from app.core.config import settings
from app.modules.user_profile import llm as llm_module

# Модуль роутера (пакет user_profile экспортирует APIRouter под именем `router`,
# поэтому берём модуль через importlib — в него патчим compress_resume_text).
profile_router = importlib.import_module("app.modules.user_profile.router")

API = "/api/v1"
EMAIL = "tester@example.com"
PASSWORD = "strongpassword"
RESUME_TEXT = "Python-разработчик, 4 года опыта: FastAPI, PostgreSQL, Redis, Docker."


async def register(client, email: str = EMAIL, password: str = PASSWORD) -> dict:
    response = await client.post(
        f"{API}/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def login(client, email: str = EMAIL, password: str = PASSWORD) -> dict:
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_headers(tokens: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ============================================================
# Auth (docs/03 §2)
# ============================================================


async def test_register_creates_user_and_profile(client):
    user = await register(client)

    assert user["email"] == EMAIL
    assert user["is_active"] is True
    assert "password" not in user and "password_hash" not in user

    # Профиль создаётся сразу (docs/02 §4: users 1─1 user_profiles).
    tokens = await login(client)
    profile = await client.get(f"{API}/profile", headers=auth_headers(tokens))
    assert profile.status_code == 200
    assert profile.json()["match_threshold"] == 70  # DEFAULT из docs/02 §3.2


async def test_register_duplicate_email_409(client):
    await register(client)
    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL.upper(), "password": PASSWORD}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error_code"] == "EMAIL_TAKEN"
    assert body["detail"]  # единый формат docs/03 §1


async def test_register_short_password_is_validation_error(client):
    response = await client.post(
        f"{API}/auth/register", json={"email": EMAIL, "password": "short"}
    )
    assert response.status_code == 400  # docs/03 §9: 400 — ошибка валидации
    assert response.json()["error_code"] == "VALIDATION_ERROR"


async def test_login_returns_token_pair(client):
    await register(client)
    tokens = await login(client)

    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["access_token"] != tokens["refresh_token"]


async def test_login_wrong_password_401(client):
    await register(client)
    response = await client.post(
        f"{API}/auth/login", json={"email": EMAIL, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_CREDENTIALS"


async def test_me_requires_valid_access_token(client):
    await register(client)
    tokens = await login(client)

    ok = await client.get(f"{API}/auth/me", headers=auth_headers(tokens))
    assert ok.status_code == 200
    assert ok.json()["email"] == EMAIL

    no_token = await client.get(f"{API}/auth/me")
    assert no_token.status_code == 401
    assert no_token.json()["error_code"] == "UNAUTHORIZED"

    garbage = await client.get(
        f"{API}/auth/me", headers={"Authorization": "Bearer not-a-jwt"}
    )
    assert garbage.status_code == 401
    assert garbage.json()["error_code"] == "INVALID_TOKEN"


async def test_refresh_flow(client):
    await register(client)
    tokens = await login(client)

    refreshed = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 200
    new_tokens = refreshed.json()
    assert new_tokens["access_token"]
    assert new_tokens["access_token"] != tokens["access_token"]

    # Access-токен не принимается как refresh (docs/03 §2 — Refresh-token).
    wrong = await client.post(
        f"{API}/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert wrong.status_code == 401
    assert wrong.json()["error_code"] == "INVALID_REFRESH_TOKEN"

    # Новый access-токен работает.
    me = await client.get(f"{API}/auth/me", headers=auth_headers(new_tokens))
    assert me.status_code == 200


# ============================================================
# Profile (docs/03 §3)
# ============================================================


async def test_profile_requires_auth(client):
    response = await client.get(f"{API}/profile")
    assert response.status_code == 401


async def test_profile_partial_update(client):
    await register(client)
    tokens = await login(client)
    headers = auth_headers(tokens)

    # Частичное обновление из docs/03 §3 (пример тела PUT).
    payload = {
        "full_name": "Иван Иванов",
        "resume_text": RESUME_TEXT,
        "skills": ["Python", "FastAPI", "PostgreSQL"],
        "experience_years": 4.5,
        "desired_salary_from": 180000,
        "desired_salary_to": 250000,
        "match_threshold": 75,
        "preferred_work_formats": ["remote", "hybrid"],
    }
    updated = await client.put(f"{API}/profile", json=payload, headers=headers)
    assert updated.status_code == 200, updated.text
    body = updated.json()
    for key, value in payload.items():
        assert body[key] == value, key

    # Второй PUT меняет только присланные поля (частичное обновление).
    patched = await client.put(
        f"{API}/profile", json={"match_threshold": 80}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["match_threshold"] == 80
    assert patched.json()["full_name"] == "Иван Иванов"
    assert patched.json()["resume_text"] == RESUME_TEXT


async def test_profile_validation_errors(client):
    await register(client)
    tokens = await login(client)
    headers = auth_headers(tokens)

    # docs/02 §3.2: match_threshold CHECK 0–100.
    bad_threshold = await client.put(
        f"{API}/profile", json={"match_threshold": 150}, headers=headers
    )
    assert bad_threshold.status_code == 400
    assert bad_threshold.json()["error_code"] == "VALIDATION_ERROR"

    # docs/02 §3.2: resume_text — до 5000 символов.
    too_long = await client.put(
        f"{API}/profile", json={"resume_text": "ы" * 5001}, headers=headers
    )
    assert too_long.status_code == 400

    # Несогласованная зарплата.
    salary = await client.put(
        f"{API}/profile",
        json={"desired_salary_from": 300000, "desired_salary_to": 100000},
        headers=headers,
    )
    assert salary.status_code == 400


# ============================================================
# Compress / convert resume (docs/03 §3, docs/05 §3)
# ============================================================


async def test_compress_resume_updates_compact_resume(client, monkeypatch):
    """Синхронный вызов LLM: compact_resume сохраняется с лимитом ≤2000 символов."""
    # Ответ «болтливой» модели: заметно длиннее лимита, чтобы проверить обрезку.
    long_answer = "Сжатое резюме кандидата. " * 120  # > 2000 символов

    async def fake_compress(resume_text: str, *, max_chars=None, timeout=None) -> str:
        assert RESUME_TEXT in resume_text  # в промпт уходит исходное резюме
        return long_answer

    monkeypatch.setattr(profile_router, "compress_resume_text", fake_compress)

    await register(client)
    tokens = await login(client)
    headers = auth_headers(tokens)
    await client.put(f"{API}/profile", json={"resume_text": RESUME_TEXT}, headers=headers)

    response = await client.post(f"{API}/profile/compress-resume", headers=headers)
    assert response.status_code == 200, response.text
    compact = response.json()["compact_resume"]
    assert compact
    assert len(compact) <= 2000  # лимит COMPACT_RESUME_MAX_CHARS (TASK)

    # Лимит вырос до 2000 символов: результат больше не обрезается до 1000.
    assert len(compact) > 1000, f"ожидалось >1000 символов, получено {len(compact)}"

    # GET /profile отдаёт сохранённый compact_resume.
    got = await client.get(f"{API}/profile", headers=headers)
    assert got.json()["compact_resume"] == compact
def test_trim_to_limit_respects_limit_and_word_boundary():
    """docs/05 §3: обрезка compact_resume не превышает лимит и не рвёт слова."""
    from app.modules.user_profile.llm import trim_to_limit

    limit = settings.compact_resume_max_chars
    assert limit == 2000  # TASK: лимит compact_resume — 2000 символов

    short = "Краткое резюме."
    assert trim_to_limit(short, limit) == short

    long_text = "Опыт разработки. " * 500  # заметно длиннее лимита
    trimmed = trim_to_limit(long_text, limit)
    assert len(trimmed) <= limit
    assert len(trimmed) > limit // 2  # не выбрасываем половину текста
    assert not trimmed.endswith(" ")  # граница режет по пробелу/точке
    assert long_text.startswith(trimmed)  # текст не искажается, только обрезается

    # Вырожденные входы не падают.
    assert trim_to_limit("", limit) == ""
    assert trim_to_limit(long_text, 0) == long_text.strip()


async def test_convert_resume_alias_endpoint(client, monkeypatch):
    """docs/03 §3: /profile/convert-resume — тот же результат, что compress-resume."""

    async def fake_compress(resume_text: str, *, max_chars=None, timeout=None) -> str:
        return "Компактная версия резюме."

    monkeypatch.setattr(profile_router, "compress_resume_text", fake_compress)

    await register(client)
    tokens = await login(client)
    headers = auth_headers(tokens)
    await client.put(f"{API}/profile", json={"resume_text": RESUME_TEXT}, headers=headers)

    response = await client.post(f"{API}/profile/convert-resume", headers=headers)
    assert response.status_code == 200
    assert response.json()["compact_resume"] == "Компактная версия резюме."


async def test_compress_resume_empty_and_llm_errors(client, monkeypatch):
    await register(client)
    tokens = await login(client)
    headers = auth_headers(tokens)

    # Пустое резюме → 400 RESUME_EMPTY.
    empty = await client.post(f"{API}/profile/compress-resume", headers=headers)
    assert empty.status_code == 400
    assert empty.json()["error_code"] == "RESUME_EMPTY"

    # Ollama недоступна → 500 LLM_UNAVAILABLE (docs/05 §7).
    async def broken_compress(resume_text: str, *, max_chars=None, timeout=None) -> str:
        raise llm_module.LLMError("connection refused")

    monkeypatch.setattr(profile_router, "compress_resume_text", broken_compress)
    await client.put(f"{API}/profile", json={"resume_text": RESUME_TEXT}, headers=headers)
    failed = await client.post(f"{API}/profile/compress-resume", headers=headers)
    assert failed.status_code == 500
    assert failed.json()["error_code"] == "LLM_UNAVAILABLE"

