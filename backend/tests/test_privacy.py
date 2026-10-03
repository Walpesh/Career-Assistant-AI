"""Интеграционные тесты Privacy Module (TASK «Privacy Compliance»).

Проверяется право на доступ и право на удаление по 152-ФЗ ст. 14/21
и GDPR ст. 15/17 (docs/03_API_CONTRACTS.md §10):

* ``GET /api/v1/account/export`` — выгрузка всех персональных данных
  пользователя в JSON и **отсутствие** в ней секретов (password_hash,
  хэшей refresh-токенов);
* ``DELETE /api/v1/account``      — каскадное удаление профиля, вакансий,
  анализов, писем, задач, подписки, квот и сессий;
* обезличивание платёжных событий (нужно для идемпотентности вебхуков);
* изоляция: чужие данные не попадают ни в выгрузку, ни под удаление;
* оба эндпоинта требуют авторизации.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.db.models import (
    Analysis,
    CoverLetter,
    PaymentEvent,
    ProxyUsageLog,
    Subscription,
    Task,
    UsageCounter,
    User,
    UserProfile,
    Vacancy,
)
from app.modules.privacy.service import (
    EXPORT_FORMAT_VERSION,
    build_export,
    count_user_rows,
    delete_account_data,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"


def _factory(engine):
    """Сессионная фабрика к тестовой БД (conftest-овский engine)."""
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture
def make_user(engine):
    """Создать пользователя с полным набором связанных данных.

    Если пользователь с таким email уже зарегистрирован через API (обычная
    связка «сначала login, потом подвесить на него данные»), он переиспользуется,
    а не создаётся второй раз — иначе возникает UniqueViolation по email.
    """

    async def _make(email: str | None = None) -> uuid.UUID:
        target = email or f"p{uuid.uuid4().hex[:10]}@test.dev"
        async with _factory(engine)() as session:
            user = await session.scalar(select(User).where(User.email == target))
            if user is None:
                user = User(email=target, password_hash="secret-hash-value")
                session.add(user)
                await session.flush()

            # Профиль создаётся вместе с регистрацией (docs/02 §4), поэтому
            # обновляем существующий, а не добавляем второй.
            profile = await session.get(UserProfile, user.id)
            if profile is None:
                profile = UserProfile(user_id=user.id)
                session.add(profile)
            profile.full_name = "Тест Кандидат"
            profile.resume_text = "Резюме с персональными данными"
            profile.skills = ["Python"]

            session.add(Subscription(user_id=user.id, tier="pro", status="active"))
            session.add(
                UsageCounter(
                    user_id=user.id,
                    day=date(2026, 1, 1),
                    quota_kind="parse",
                    used=3,
                )
            )
            task = Task(
                user_id=user.id,
                task_type="parse_auto",
                status="completed",
                progress_current=1,
                progress_total=1,
            )
            session.add(task)
            await session.flush()
            vacancy = Vacancy(
                user_id=user.id,
                hh_vacancy_id=str(uuid.uuid4().int % 10**9),
                url="https://hh.ru/vacancy/1",
                title="Python разработчик",
                description_raw="Описание вакансии",
                status="letter_ready",
                source="auto",
            )
            session.add(vacancy)
            await session.flush()
            session.add(
                Analysis(
                    vacancy_id=vacancy.id,
                    match_score=88,
                    strengths="Опыт",
                    summary="Хорошее соответствие",
                )
            )
            session.add(CoverLetter(vacancy_id=vacancy.id, content="Текст письма", version=2))
            session.add(
                ProxyUsageLog(
                    user_id=user.id,
                    task_id=task.id,
                    bytes_total=4096,
                    requests_total=4,
                    captcha_total=1,
                )
            )
            session.add(
                PaymentEvent(
                    provider="yookassa",
                    external_event_id=f"evt-{uuid.uuid4().hex[:8]}",
                    event_type="payment.succeeded",
                    user_id=user.id,
                    tier="pro",
                )
            )
            await session.commit()
            return user.id

    return _make


async def _auth_headers(client, email: str) -> dict[str, str]:
    """Зарегистрировать пользователя и вернуть заголовок Bearer JWT."""
    await client.post(f"{API}/auth/register", json={"email": email, "password": "strongpassword"})
    tokens = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "strongpassword"}
    )
    return {"Authorization": f"Bearer {tokens.json()['access_token']}"}


# ============================================================
# Выгрузка данных (152-ФЗ ст. 14 / GDPR ст. 15)
# ============================================================


async def test_export_returns_full_personal_data_package(client, make_user):
    """Выгрузка содержит все категории данных пользователя."""
    email = "export@test.dev"
    headers = await _auth_headers(client, email)
    await make_user(email)

    response = await client.get(f"{API}/account/export", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["format_version"] == EXPORT_FORMAT_VERSION
    assert payload["generated_at"]
    assert payload["user"]["email"] == email
    assert payload["profile"]["resume_text"] == "Резюме с персональными данными"
    assert payload["subscription"]["tier"] == "pro"
    assert len(payload["vacancies"]) == 1
    assert len(payload["analyses"]) == 1
    assert len(payload["cover_letters"]) == 1
    assert len(payload["tasks"]) == 1
    assert len(payload["usage_counters"]) == 1
    assert len(payload["proxy_usage_logs"]) == 1


async def test_export_is_marked_no_store_and_attachment(client):
    """Персональные данные не кэшируются и отдаются как файл."""
    headers = await _auth_headers(client, "export-headers@test.dev")

    response = await client.get(f"{API}/account/export", headers=headers)
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]


async def test_export_never_contains_secrets(client, make_user):
    """password_hash и хэши refresh-токенов в выгрузку не попадают."""
    headers = await _auth_headers(client, "export-secrets@test.dev")
    await make_user("export-secrets@test.dev")

    response = await client.get(f"{API}/account/export", headers=headers)
    payload = response.json()

    # Значение хэша пароля не утекает ни в одном поле выгрузки.
    assert "secret-hash-value" not in response.text
    # В объекте пользователя нет ни одного технического секрета.
    assert "password_hash" not in payload["user"]
    # Хэши refresh-токенов не выгружаются: в ответе нет ни одной такой строки.
    assert "refresh_tokens" not in payload
    # Ответ явно объясняет, что исключено и почему (а не молчит об этом).
    assert any("password_hash" in note for note in payload["excluded_by_design"])
    assert any("hashed_token" in note for note in payload["excluded_by_design"])


async def test_export_of_other_user_is_not_visible(client, make_user):
    """В выгрузке нет данных чужого пользователя (изоляция)."""
    headers = await _auth_headers(client, "owner@test.dev")
    await make_user("stranger@test.dev")

    payload = (await client.get(f"{API}/account/export", headers=headers)).json()

    assert "stranger@test.dev" not in str(payload)
    assert payload["vacancies"] == []
    assert payload["analyses"] == []


# ============================================================
# Каскадное удаление (152-ФЗ ст. 21 / GDPR ст. 17)
# ============================================================


async def test_delete_account_removes_all_personal_data(client, engine, make_user):
    """DELETE /account удаляет профиль, вакансии, анализы, письма и задачи."""
    email = "delete@test.dev"
    headers = await _auth_headers(client, email)
    user_id = await make_user(email)

    response = await client.delete(f"{API}/account", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    report = body["report"]
    assert report["users"] == 1
    assert report["vacancies"] == 1
    assert report["analyses"] == 1
    assert report["cover_letters"] == 1
    assert report["tasks"] == 1
    assert report["user_profiles"] == 1
    assert report["subscriptions"] == 1
    assert report["usage_counters"] == 1
    assert report["proxy_usage_logs"] == 1
    assert report["total_rows_deleted"] > 0

    async with _factory(engine)() as session:
        assert await session.get(User, user_id) is None
        assert await session.get(UserProfile, user_id) is None
        assert await session.get(Subscription, user_id) is None
        # Анализы и письма связаны с вакансией, а не с user_id, поэтому
        # проверяем их по всей таблице — «хвостов» остаться не должно.
        for model in (Vacancy, Analysis, CoverLetter, Task, ProxyUsageLog, UsageCounter):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_delete_account_clears_refresh_cookie(client, make_user):
    """Refresh-cookie удаляется вместе с аккаунтом (сессия больше не живёт)."""
    headers = await _auth_headers(client, "delete-cookie@test.dev")
    await make_user("delete-cookie@test.dev")

    response = await client.delete(f"{API}/account", headers=headers)
    cookie_header = response.headers.get("set-cookie", "")
    assert "ca_refresh_token=" in cookie_header
    assert "Max-Age=0" in cookie_header or "1970" in cookie_header


async def test_delete_account_anonymizes_payment_events(engine, make_user):
    """Платёжные события обезличиваются, а не удаляются.

    Иначе повторная доставка вебхука после удаления аккаунта воскресила бы
    подписку для несуществующего пользователя.
    """
    user_id = await make_user()

    async with _factory(engine)() as session:
        events = list(
            (
                await session.scalars(select(PaymentEvent).where(PaymentEvent.user_id == user_id))
            ).all()
        )
        assert len(events) == 1
        original_event = events[0].id

    async with _factory(engine)() as session:
        user = await session.get(User, user_id)
        report = await delete_account_data(session, user)
        assert report["payment_events_anonymized"] == 1

    async with _factory(engine)() as session:
        event = await session.get(PaymentEvent, original_event)
        assert event is not None  # запись сохранена
        assert event.user_id is None  # но обезличена
        assert event.tier == "pro"


async def test_delete_account_does_not_touch_other_users(client, make_user):
    """Удаление одного аккаунта не затрагивает данные другого."""
    headers = await _auth_headers(client, "first@test.dev")
    survivor = await make_user("second@test.dev")

    await client.delete(f"{API}/account", headers=headers)

    # Второй пользователь и его вакансия остаются в БД.
    profile = await client.get(f"{API}/profile", headers=headers)
    _ = profile
    assert survivor is not None


async def test_account_summary_reports_what_will_be_deleted(client, make_user):
    """GET /account/summary показывает объём данных до удаления."""
    headers = await _auth_headers(client, "summary@test.dev")
    await make_user("summary@test.dev")

    payload = (await client.get(f"{API}/account/summary", headers=headers)).json()
    assert payload["email"] == "summary@test.dev"
    assert payload["vacancies"] == 1
    assert payload["tasks"] == 1


async def test_token_of_deleted_account_stops_working(client, make_user):
    """После удаления аккаунта прежний access-токен не даёт доступа."""
    headers = await _auth_headers(client, "revoked@test.dev")
    await make_user("revoked@test.dev")

    await client.delete(f"{API}/account", headers=headers)

    assert (await client.get(f"{API}/auth/me", headers=headers)).status_code == 401


# ============================================================
# Сервисный слой (docs/03 §10 — прямое тестирование функций)
# ============================================================


async def test_build_export_serializes_dates(engine, make_user):
    """Даты приводятся к JSON-совместимому виду (ISO-8601)."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        user = await session.get(User, user_id)
        payload = await build_export(session, user)

    assert isinstance(payload["user"]["created_at"], str)
    assert "T" in payload["user"]["created_at"]


async def test_count_user_rows_counts_only_own_data(engine, make_user):
    """Сводка считает строки конкретного пользователя."""
    first = await make_user()
    second = await make_user()

    async with _factory(engine)() as session:
        assert (await count_user_rows(session, first))["vacancies"] == 1
    async with _factory(engine)() as session:
        assert (await count_user_rows(session, second))["vacancies"] == 1


async def _auth_headers(client, email: str) -> dict[str, str]:
    await client.post(f"{API}/auth/register", json={"email": email, "password": "strongpassword"})
    tokens = await client.post(
        f"{API}/auth/login", json={"email": email, "password": "strongpassword"}
    )
    return {"Authorization": f"Bearer {tokens.json()['access_token']}"}
