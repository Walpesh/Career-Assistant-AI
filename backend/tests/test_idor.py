"""IDOR-регрессия: чужие ресурсы отвечают 404, а не 200 (docs/03 §1, §4, §6, §7).

Модель угроз (docs/03 §9 — коды ответов):
    пользователь A знает UUID вакансии/задачи пользователя B и подставляет
    его в URL. Ожидаемое поведение — **404 NOT_FOUND**, а не 403 и тем более
    не 200: иначе по разнице ответов можно перебором узнать, существует ли
    объект (enumeration), а по 403 — что он существует, но чужой.

Что проверяется:
    - ``GET /analysis/{vacancy_id}`` и ``GET /letters/{vacancy_id}`` (docs/03 §6);
    - ``POST /analysis/run`` с чужой вакансией — 404 и **ни** задачи в БД,
      **ни** списанной квоты (docs/03 §11 — квота после проверки владения);
    - ``GET /tasks/{task_id}``, ``/cancel``, ``/resume`` (docs/03 §7);
    - ``GET``/``PATCH``/``DELETE /vacancies/{vacancy_id}`` (docs/03 §4);
    - ``GET /vacancies`` — в выдаче нет чужих вакансий;
    - ``GET /account/summary`` и ``/account/export`` — только свои данные;
    - ``GET /billing/usage`` — квоты только своего пользователя;
    - анонимный доступ (401) и деактивация аккаунта (403).

Все проверки идут через настоящее ASGI-приложение с подменой ``get_db``
на тестовую БД (tests/conftest.py); Redis-очередь заменена на RecordingPool —
сеть и LLM не используются.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from app.db.models import Analysis, CoverLetter, Task, UsageCounter, User, Vacancy
from app.main import app
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"
PASSWORD = "strongpassword"


# ============================================================
# Фикстуры и помощники
# ============================================================


@pytest.fixture
def factory(engine, client):
    """Sessionmaker тестовой БД.

    Зависит от ``client``: тот устанавливает ``app.dependency_overrides[get_db]``
    на общий объект приложения, поэтому «свои» клиенты (``fresh_client``) тоже
    пишут в тестовую БД, а не в дефолтную career_assistant.
    """
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@asynccontextmanager
async def fresh_client():
    """Отдельный HTTP-клиент: своя cookie-jar = «другое устройство».

    ``app.dependency_overrides`` — общий для приложения, поэтому клиент
    работает с той же тестовой БД, но не наследует cookie основного ``client``.
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http


async def _register_and_login(email: str) -> tuple[str, dict]:
    """Зарегистрировать пользователя, подтвердить email и вернуть (user_id, Authorization)."""
    from conftest import register_verified, user_id_for

    async with fresh_client() as client:
        tokens = await register_verified(client, email, PASSWORD)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        return await user_id_for(client, headers), headers


async def _seed_vacancy_with_results(
    factory, user_id: str, *, with_results: bool = True
) -> str:
    """Вакансия пользователя; опционально — анализ и письмо по ней."""
    async with factory() as session:
        vacancy = Vacancy(
            user_id=uuid.UUID(str(user_id)),
            hh_vacancy_id=uuid.uuid4().hex[:10],
            url="https://hh.ru/vacancy/1",
            title="Senior Python разработчик",
            company_name="ООО Ромашка",
            description_raw="Требуется опыт FastAPI и PostgreSQL.",
            status="analyzed" if with_results else "raw",
            source="manual",
        )
        session.add(vacancy)
        await session.flush()
        if with_results:
            session.add(
                Analysis(
                    vacancy_id=vacancy.id,
                    match_score=90,
                    summary="Отличное совпадение",
                    strengths="FastAPI",
                    weaknesses="Нет Kubernetes",
                )
            )
            session.add(
                CoverLetter(
                    vacancy_id=vacancy.id,
                    content="Здравствуйте! Меня зовут ...",
                )
            )
        await session.commit()
        return str(vacancy.id)


async def _seed_task(factory, user_id: str, *, status: str = "pending") -> str:
    async with factory() as session:
        task = Task(
            user_id=uuid.UUID(str(user_id)),
            task_type="parse_auto",
            status=status,
            payload={"keywords": ["python"]},
        )
        session.add(task)
        await session.commit()
        return str(task.id)


# ============================================================
# /analysis/{id} и /letters/{id} — основной вектор IDOR (docs/03 §6)
# ============================================================


async def test_analysis_of_foreign_vacancy_returns_404(factory):
    """Чужой vacancy_id в GET /analysis/{id} → 404 NOT_FOUND, не 403/200."""
    owner_id, owner_headers = await _register_and_login("idor-owner@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, owner_id)

    # Владелец видит свой ресурс (контроль: тест не проходит «заглушкой»).
    async with fresh_client() as client:
        own = await client.get(f"{API}/analysis/{vacancy_id}", headers=owner_headers)
    assert own.status_code == 200, own.text
    assert own.json()["match_score"] == 90

    _, attacker_headers = await _register_and_login("idor-attacker@test.dev")
    async with fresh_client() as client:
        stolen = await client.get(
            f"{API}/analysis/{vacancy_id}", headers=attacker_headers
        )

    assert stolen.status_code == 404
    assert stolen.json()["error_code"] == "NOT_FOUND"
    # Утечки содержимого чужого анализа в теле ответа нет.
    assert "match_score" not in stolen.json()
    assert "Отличное совпадение" not in stolen.text


async def test_letter_of_foreign_vacancy_returns_404(factory):
    """Чужой vacancy_id в GET /letters/{id} → 404 NOT_FOUND."""
    owner_id, owner_headers = await _register_and_login("letter-owner@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, owner_id)

    async with fresh_client() as client:
        own = await client.get(f"{API}/letters/{vacancy_id}", headers=owner_headers)
    assert own.status_code == 200, own.text

    _, attacker_headers = await _register_and_login("letter-attacker@test.dev")
    async with fresh_client() as client:
        stolen = await client.get(f"{API}/letters/{vacancy_id}", headers=attacker_headers)

    assert stolen.status_code == 404
    assert stolen.json()["error_code"] == "NOT_FOUND"
    assert "content" not in stolen.json()
    assert "Меня зовут" not in stolen.text


async def test_analysis_and_letter_404_are_indistinguishable(factory):
    """Существующая чужая и несуществующая вакансия дают одинаковый 404.

    Иначе по разнице ответов можно перебрать валидные UUID (enumeration).
    """
    owner_id, _ = await _register_and_login("enum-owner@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, owner_id)
    _, attacker_headers = await _register_and_login("enum-attacker@test.dev")

    async with fresh_client() as client:
        foreign = await client.get(
            f"{API}/analysis/{vacancy_id}", headers=attacker_headers
        )
        unknown = await client.get(
            f"{API}/analysis/{uuid.uuid4()}", headers=attacker_headers
        )
        foreign_letter = await client.get(
            f"{API}/letters/{vacancy_id}", headers=attacker_headers
        )
        unknown_letter = await client.get(
            f"{API}/letters/{uuid.uuid4()}", headers=attacker_headers
        )

    assert foreign.status_code == unknown.status_code == 404
    assert foreign.json() == unknown.json()
    assert foreign_letter.status_code == unknown_letter.status_code == 404
    assert foreign_letter.json() == unknown_letter.json()


async def test_analysis_without_results_returns_404_for_owner(factory):
    """Своя вакансия без анализа → 404 (ресурс ещё не создан, не 403)."""
    user_id, headers = await _register_and_login("noanalysis@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, user_id, with_results=False)

    async with fresh_client() as client:
        analysis = await client.get(f"{API}/analysis/{vacancy_id}", headers=headers)
        letter = await client.get(f"{API}/letters/{vacancy_id}", headers=headers)

    assert analysis.status_code == 404
    assert analysis.json()["error_code"] == "NOT_FOUND"
    assert letter.status_code == 404


async def test_analysis_and_letters_require_authentication(factory):
    """Без Bearer-токена — 401, а не обход проверки владения (docs/03 §1)."""
    user_id, _ = await _register_and_login("anon-owner@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, user_id)

    async with fresh_client() as client:
        analysis = await client.get(f"{API}/analysis/{vacancy_id}")
        letter = await client.get(f"{API}/letters/{vacancy_id}")

    assert analysis.status_code == 401
    assert analysis.json()["error_code"] == "UNAUTHORIZED"
    assert letter.status_code == 401
    assert letter.json()["error_code"] == "UNAUTHORIZED"


# ============================================================
# POST /analysis/run — чужая вакансия (docs/03 §6, §11)
# ============================================================


async def test_run_analysis_rejects_foreign_vacancy(factory, queue_pool):
    """Чужая вакансия в POST /analysis/run → 404, задача и квота не тронуты.

    Порядок в роутере: сначала проверка владения, потом списание квоты,
    потом постановка в очередь (docs/03 §11). Другой порядок приводил бы к
    тому, что 404 «съедает» суточную квоту и создаёт висящую задачу.
    """
    owner_id, owner_headers = await _register_and_login("run-owner@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, owner_id)
    _, attacker_headers = await _register_and_login("run-attacker@test.dev")

    async with fresh_client() as client:
        response = await client.post(
            f"{API}/analysis/run",
            json={"vacancy_ids": [vacancy_id], "mode": "analyze"},
            headers=attacker_headers,
        )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "NOT_FOUND"

    # Задача не создана и в очередь ничего не ушло.
    async with factory() as session:
        created = await session.scalar(
            select(func.count()).select_from(Task).where(Task.task_type == "analyze")
        )
    assert created == 0
    assert queue_pool.jobs == []

    # Квота анализа не списана.
    async with factory() as session:
        used = await session.scalar(
            select(func.coalesce(func.sum(UsageCounter.used), 0)).where(
                UsageCounter.quota_kind == "analysis"
            )
        )
    assert used == 0

    # Контроль: владелец той же вакансии запускает анализ успешно.
    async with fresh_client() as client:
        owner_run = await client.post(
            f"{API}/analysis/run",
            json={"vacancy_ids": [vacancy_id], "mode": "analyze"},
            headers=owner_headers,
        )
    assert owner_run.status_code == 200, owner_run.text
    assert queue_pool.jobs


async def test_run_analysis_mixed_owned_and_foreign_vacancies_rejected(factory):
    """Список из своей и чужой вакансии → 404 целиком (никакой частичной работы)."""
    owner_id, owner_headers = await _register_and_login("mixed-owner@test.dev")
    own_id = await _seed_vacancy_with_results(factory, owner_id)
    other_id, _ = await _register_and_login("mixed-other@test.dev")
    foreign_id = await _seed_vacancy_with_results(factory, other_id)

    async with fresh_client() as client:
        response = await client.post(
            f"{API}/analysis/run",
            json={"vacancy_ids": [own_id, foreign_id], "mode": "analyze_and_letter"},
            headers=owner_headers,
        )

    assert response.status_code == 404
    assert response.json()["error_code"] == "NOT_FOUND"


# ============================================================
# /tasks/{id} — IDOR на задачах (docs/03 §7)
# ============================================================


@pytest.mark.parametrize("action", ["get", "cancel", "resume"])
async def test_foreign_task_is_404_on_all_task_endpoints(factory, action):
    """Задача другого пользователя скрыта от GET/cancel/resume (docs/03 §7)."""
    victim_id, _ = await _register_and_login("task-victim@test.dev")
    task_id = await _seed_task(factory, victim_id, status="waiting_captcha")
    _, attacker_headers = await _register_and_login("task-attacker@test.dev")

    async with fresh_client() as client:
        if action == "get":
            response = await client.get(f"{API}/tasks/{task_id}", headers=attacker_headers)
        elif action == "cancel":
            response = await client.post(
                f"{API}/tasks/{task_id}/cancel", headers=attacker_headers
            )
        else:
            response = await client.post(
                f"{API}/tasks/{task_id}/resume", headers=attacker_headers
            )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "NOT_FOUND"


async def test_foreign_task_cancel_does_not_change_state(factory):
    """Попытка отменить чужую задачу не меняет её статус в БД."""
    victim_id, _ = await _register_and_login("cancel-victim@test.dev")
    task_id = await _seed_task(factory, victim_id, status="processing")
    _, attacker_headers = await _register_and_login("cancel-attacker@test.dev")

    async with fresh_client() as client:
        response = await client.post(
            f"{API}/tasks/{task_id}/cancel", headers=attacker_headers
        )
    assert response.status_code == 404

    async with factory() as session:
        task = await session.get(Task, uuid.UUID(task_id))
    assert task.status == "processing"
    assert task.error_message is None


async def test_task_list_contains_only_own_tasks(factory):
    """GET /tasks не выдаёт чужие задачи даже при пагинации (docs/03 §7)."""
    victim_id, _ = await _register_and_login("list-victim@test.dev")
    for _ in range(3):
        await _seed_task(factory, victim_id)
    owner_id, owner_headers = await _register_and_login("list-owner@test.dev")
    own_task = await _seed_task(factory, owner_id)

    async with fresh_client() as http:
        response = await http.get(f"{API}/tasks", headers=owner_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [own_task]


# ============================================================
# /vacancies/{id} — IDOR на вакансиях (docs/03 §4)
# ============================================================


@pytest.mark.parametrize(
    ("method", "suffix"),
    [("get", ""), ("delete", ""), ("patch", "/status")],
)
async def test_foreign_vacancy_is_404_on_all_vacancy_endpoints(factory, method, suffix):
    """Карточка чужой вакансии недоступна для чтения, удаления и смены статуса."""
    owner_id, _ = await _register_and_login("vac-owner@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, owner_id, with_results=False)
    _, attacker_headers = await _register_and_login("vac-attacker@test.dev")

    path = f"{API}/vacancies/{vacancy_id}{suffix}"
    async with fresh_client() as client:
        if method == "get":
            response = await client.get(path, headers=attacker_headers)
        elif method == "delete":
            response = await client.delete(path, headers=attacker_headers)
        else:
            response = await client.patch(
                path, json={"status": "applied"}, headers=attacker_headers
            )

    assert response.status_code == 404, response.text
    assert response.json()["error_code"] == "NOT_FOUND"

    # Вакансия осталась у владельца и не сменила статус.
    async with factory() as session:
        vacancy = await session.get(Vacancy, uuid.UUID(vacancy_id))
    assert vacancy is not None
    assert vacancy.user_id == uuid.UUID(owner_id)
    assert vacancy.status == "raw"


async def test_vacancy_list_never_leaks_other_users(factory):
    """GET /vacancies отдаёт только вакансии текущего пользователя (docs/03 §4)."""
    victim_id, _ = await _register_and_login("list-vac-victim@test.dev")
    victim_vacancy = await _seed_vacancy_with_results(
        factory, victim_id, with_results=False
    )
    owner_id, owner_headers = await _register_and_login("list-vac-owner@test.dev")
    own_vacancy = await _seed_vacancy_with_results(factory, owner_id, with_results=False)

    async with fresh_client() as http:
        response = await http.get(f"{API}/vacancies", headers=owner_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [item["id"] for item in body["items"]] == [own_vacancy]
    assert victim_vacancy not in response.text


# ============================================================
# Профиль, приватность и биллинг — per-user данные (docs/03 §3, §10, §11)
# ============================================================


async def test_profile_is_scoped_to_current_user(factory):
    """GET /profile возвращает профиль именно владельца токена."""
    attacker_id, attacker_headers = await _register_and_login("prof-attacker@test.dev")

    async with fresh_client() as client:
        response = await client.get(f"{API}/profile", headers=attacker_headers)

    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == attacker_id


async def test_account_summary_is_scoped_to_current_user(factory):
    """GET /account/summary показывает объём данных только владельца (docs/03 §10)."""
    victim_id, _ = await _register_and_login("sum-victim@test.dev")
    await _seed_vacancy_with_results(factory, victim_id, with_results=False)
    await _seed_task(factory, victim_id)

    attacker_id, attacker_headers = await _register_and_login("sum-attacker@test.dev")
    async with fresh_client() as client:
        response = await client.get(f"{API}/account/summary", headers=attacker_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vacancies"] == 0
    assert body["tasks"] == 0
    assert body["user_id"] == attacker_id
    assert body["user_id"] != victim_id


async def test_account_export_does_not_include_other_users_data(factory):
    """GET /account/export не содержит персональных данных других (152-ФЗ ст. 14)."""
    victim_id, _ = await _register_and_login("exp-victim@test.dev")
    victim_vacancy = await _seed_vacancy_with_results(factory, victim_id)

    attacker_id, attacker_headers = await _register_and_login("exp-attacker@test.dev")
    async with fresh_client() as client:
        response = await client.get(f"{API}/account/export", headers=attacker_headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["user"]["id"] == attacker_id
    assert payload["vacancies"] == []
    assert payload["analyses"] == []
    assert payload["cover_letters"] == []
    assert victim_vacancy not in response.text
    assert victim_id not in response.text


async def test_billing_usage_is_scoped_to_current_user(factory):
    """Квоты /billing/usage считаются по тарифу владельца токена (docs/03 §11)."""
    from app.modules.billing.service import quota_day

    victim_id, _ = await _register_and_login("bill-victim@test.dev")
    async with factory() as session:
        session.add(
            UsageCounter(
                user_id=uuid.UUID(victim_id),
                day=quota_day(),
                quota_kind="parse",
                used=5,
            )
        )
        await session.commit()

    _, attacker_headers = await _register_and_login("bill-attacker@test.dev")
    async with fresh_client() as client:
        response = await client.get(f"{API}/billing/usage", headers=attacker_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tier"] == "free"
    # Чужие 5/5 запросов не отражаются в квотах атакующего.
    for kind_state in body["quotas"].values():
        assert kind_state["used"] == 0

    async with factory() as session:
        victim_counter = await session.scalar(
            select(UsageCounter).where(UsageCounter.user_id == uuid.UUID(victim_id))
        )
    assert victim_counter.used == 5


# ============================================================
# Деактивация аккаунта — доступ закрывается всем сессиям (docs/02 §3.1)
# ============================================================


async def test_deactivated_account_loses_access_to_its_resources(factory):
    """``is_active = False`` → 403 ACCOUNT_DISABLED на защищённых маршрутах.

    Даже ранее выданный access-токен перестаёт работать: проверка
    активности выполняется в ``get_current_user`` на каждый запрос.
    """
    user_id, headers = await _register_and_login("deactivated@test.dev")
    vacancy_id = await _seed_vacancy_with_results(factory, user_id)

    async with factory() as session:
        user = await session.get(User, uuid.UUID(user_id))
        user.is_active = False
        await session.commit()

    async with fresh_client() as client:
        analysis = await client.get(f"{API}/analysis/{vacancy_id}", headers=headers)
        me = await client.get(f"{API}/auth/me", headers=headers)

    assert analysis.status_code == 403
    assert analysis.json()["error_code"] == "ACCOUNT_DISABLED"
    assert me.status_code == 403
    assert me.json()["error_code"] == "ACCOUNT_DISABLED"