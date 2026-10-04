"""Тесты Billing Module: тарифы, суточные квоты и платёжные вебхуки.

TASK «Monetization Engine & Quota Management» (docs/03 §11, docs/02 §3.8–§3.10):

* каталог тарифов free / pro / enterprise и их суточные лимиты;
* атомарное списание квот и ``429 QUOTA_EXCEEDED`` при исчерпании;
* безлимитные квоты enterprise (списание идёт, но лимит не превышается);
* защита от гонок: параллельные запросы не превышают лимит;
* вебхуки YooKassa / CloudPayments / Stripe: подпись, отказ при неверной
  подписи и **идемпотентность** повторной доставки;
* неизвестный провайдер и ненастроенный секрет отклоняются.

Сеть и Redis не используются: квоты считаются в PostgreSQL, подписи
вычисляются локально.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
from app.core.config import settings
from app.db.models import PaymentEvent, Subscription, UsageCounter, User
from app.modules.billing.providers import verify_signature
from app.modules.billing.service import (
    QuotaExceeded,
    consume_quota,
    get_tier,
    process_webhook,
    quota_day,
    quota_states,
    quota_summary,
)
from app.modules.billing.tiers import (
    ENTERPRISE,
    FREE,
    PRO,
    UNLIMITED,
    QuotaKind,
    available_tiers,
    quota_for,
    resolve_tier,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

API = "/api/v1"
SECRET = "webhook-test-secret"


def _factory(engine):
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture
def make_user(engine):
    """Создать пользователя (или переиспользовать существующего по email)."""

    async def _make(email: str | None = None) -> uuid.UUID:
        target = email or f"b{uuid.uuid4().hex[:10]}@test.dev"
        async with _factory(engine)() as session:
            user = await session.scalar(select(User).where(User.email == target))
            if user is None:
                user = User(email=target, password_hash="x")
                session.add(user)
                await session.commit()
            return user.id

    return _make


async def _auth_headers(client, email: str) -> dict[str, str]:
    """Зарегистрировать пользователя, подтвердить email и вернуть Bearer JWT.

    Токены выдаёт POST /auth/verify-email: до подтверждения email вход
    запрещён (docs/03 §2).
    """
    from conftest import register_verified

    tokens = await register_verified(client, email, "strongpassword")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
def set_tier(engine):
    """Выдать пользователю подписку заданного тарифа."""

    async def _set(user_id: uuid.UUID, tier: str, status: str = "active") -> None:
        async with _factory(engine)() as session:
            existing = await session.scalar(
                select(Subscription).where(Subscription.user_id == user_id)
            )
            if existing is None:
                existing = Subscription(user_id=user_id)
                session.add(existing)
            existing.tier = tier
            existing.status = status
            await session.commit()

    return _set


# ============================================================
# Каталог тарифов (docs/03 §11)
# ============================================================


def test_tier_catalog_has_expected_quotas():
    """У тарифов заявлены суточные лимиты и корректный порядок."""
    assert FREE.parse == 5
    assert FREE.letter == 10
    assert PRO.parse == 50
    # Enterprise безлимитен по всем видам квот.
    assert all(ENTERPRISE.is_unlimited(kind) for kind in QuotaKind.ALL)
    assert ENTERPRISE.limit_for(QuotaKind.PARSE) == UNLIMITED


def test_available_tiers_is_sorted_and_reports_unlimited():
    """Каталог отдаётся от младшего тарифа к старшему."""
    catalog = available_tiers()
    assert [entry["tier"] for entry in catalog] == ["free", "pro", "enterprise"]
    assert catalog[0]["unlimited"] == []
    assert catalog[2]["daily_quotas"][QuotaKind.PARSE] == UNLIMITED


def test_quota_for_unknown_tier_falls_back_to_free():
    """Неизвестный тариф не должен молча снимать лимиты."""
    assert quota_for("gold") is FREE
    assert resolve_tier("GOLD") == "free"
    assert resolve_tier("PRO") == "pro"


async def test_tiers_endpoint_is_public(client):
    """GET /billing/tiers открыт без авторизации: смена тарифа видна до оплаты."""
    response = await client.get(f"{API}/billing/tiers")
    assert response.status_code == 200
    body = response.json()
    assert len(body["tiers"]) == 3
    assert "yookassa" in body["providers"]


# ============================================================
# Определение тарифа
# ============================================================


async def test_new_user_defaults_to_free(engine, make_user):
    """Без строки subscriptions эффективный тариф — free (docs/03 §11)."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "free"


async def test_active_subscription_is_respected(engine, make_user, set_tier):
    """Активная подписка pro даёт тариф pro."""
    user_id = await make_user()
    await set_tier(user_id, "pro")
    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "pro"


async def test_canceled_subscription_drops_to_free(engine, make_user, set_tier):
    """Отменённая подписка не даёт прав: тариф возвращается к free."""
    user_id = await make_user()
    await set_tier(user_id, "pro", status="canceled")
    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "free"


# ============================================================
# Списание суточных квот
# ============================================================


async def test_consume_quota_accumulates_per_day(engine, make_user):
    """Расход накапливается по календарным суткам (UTC)."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        await consume_quota(session, user_id, QuotaKind.PARSE)
        await consume_quota(session, user_id, QuotaKind.PARSE)
        states = await quota_states(session, user_id)

    assert states[QuotaKind.PARSE].used == 2
    assert states[QuotaKind.PARSE].limit == FREE.parse
    assert states[QuotaKind.PARSE].remaining == FREE.parse - 2
    assert states[QuotaKind.PARSE].resets_at.endswith("T00:00:00+00:00")


async def test_consume_quota_raises_when_exhausted(engine, make_user):
    """Исчерпание суточной квоты → QuotaExceeded (HTTP 429)."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        for _ in range(FREE.parse):
            await consume_quota(session, user_id, QuotaKind.PARSE)
        with pytest.raises(QuotaExceeded) as excinfo:
            await consume_quota(session, user_id, QuotaKind.PARSE)

    assert excinfo.value.status_code == 429
    assert excinfo.value.error_code == "QUOTA_EXCEEDED"
    assert excinfo.value.kind == QuotaKind.PARSE


async def test_quota_exhausted_does_not_write_counter_past_limit(engine, make_user):
    """Отклонённое начисление не увеличивает счётчик сверх лимита."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        for _ in range(FREE.letter):
            await consume_quota(session, user_id, QuotaKind.LETTER)
        with pytest.raises(QuotaExceeded):
            await consume_quota(session, user_id, QuotaKind.LETTER)

    async with _factory(engine)() as session:
        counters = list(
            (
                await session.scalars(select(UsageCounter).where(UsageCounter.user_id == user_id))
            ).all()
        )
        letter = next(c for c in counters if c.quota_kind == QuotaKind.LETTER)
        assert letter.used == FREE.letter


async def test_enterprise_unlimited_quota_never_blocks(engine, make_user, set_tier):
    """Безлимитная квота списывается, но не блокирует работу."""
    user_id = await make_user()
    await set_tier(user_id, "enterprise")
    async with _factory(engine)() as session:
        for _ in range(FREE.parse + 25):
            state = await consume_quota(session, user_id, QuotaKind.PARSE)
        states = await quota_states(session, user_id)

    assert state.unlimited is True
    assert states[QuotaKind.PARSE].used == FREE.parse + 25
    assert states[QuotaKind.PARSE].remaining == UNLIMITED


async def test_pro_tier_has_higher_quota_than_free(engine, make_user, set_tier):
    """Смена тарифа увеличивает доступный объём операций."""
    user_id = await make_user()
    await set_tier(user_id, "free")
    async with _factory(engine)() as session:
        free_state = await quota_states(session, user_id)

    await set_tier(user_id, "pro")
    async with _factory(engine)() as session:
        pro_state = await quota_states(session, user_id)

    assert pro_state[QuotaKind.PARSE].limit > free_state[QuotaKind.PARSE].limit
    assert pro_state[QuotaKind.PARSE].limit == PRO.parse


async def test_quota_disabled_skips_enforcement(engine, make_user):
    """При выключенном контроле квоты не начисляются и не блокируют."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        state = await consume_quota(session, user_id, QuotaKind.PARSE, enforce=False)
        states = await quota_states(session, user_id)

    assert state is None
    assert states[QuotaKind.PARSE].used == 0


async def test_quota_summary_reports_remaining_and_reset(engine, make_user):
    """Сводка содержит тариф, расход, остаток и время сброса."""
    user_id = await make_user()
    async with _factory(engine)() as session:
        await consume_quota(session, user_id, QuotaKind.ANALYSIS, 2)
        summary = await quota_summary(session, user_id)

    assert summary["tier"] == "free"
    assert summary["day"] == quota_day().isoformat()
    assert summary["quotas"][QuotaKind.ANALYSIS]["used"] == 2
    assert summary["quotas"][QuotaKind.ANALYSIS]["remaining"] == FREE.analysis - 2
    assert summary["quotas"][QuotaKind.ANALYSIS]["exhausted"] is False
    assert summary["unlimited"] == []


# ============================================================
# Контроль квот в API (docs/03 §11)
# ============================================================


async def test_parsing_endpoint_enforces_daily_quota(client, engine, make_user, queue_pool):
    """POST /parsing/manual отдаёт 429 QUOTA_EXCEEDED после лимита free."""
    email = "quota-parsing@test.dev"
    headers = await _auth_headers(client, email)
    user_id = await make_user(email)
    payload = {"vacancy_url": "https://hh.ru/vacancy/123456"}

    # Контракт POST /parsing/manual — 200 { task_id, status } (docs/03 §5).
    accepted = 200
    statuses = []
    for _ in range(FREE.parse + 1):
        response = await client.post(f"{API}/parsing/manual", json=payload, headers=headers)
        statuses.append(response.status_code)
        if response.status_code != accepted:
            break

    assert statuses[:-1] == [accepted] * FREE.parse
    assert statuses[-1] == 429
    assert response.json()["error_code"] == "QUOTA_EXCEEDED"

    # Проверяем, что лимит действительно записан в БД, а не сработал «на лету».
    async with _factory(engine)() as session:
        states = await quota_states(session, user_id)
    assert states[QuotaKind.PARSE].used == FREE.parse


async def test_quota_block_creates_no_extra_tasks(client, engine, make_user, queue_pool):
    """Отклонённая по квоте задача не создаётся (docs/03 §11 — до постановки)."""
    from app.db.models import Task

    email = "quota-no-task@test.dev"
    headers = await _auth_headers(client, email)
    user_id = await make_user(email)
    payload = {"vacancy_url": "https://hh.ru/vacancy/654321"}

    for _ in range(FREE.parse + 1):
        await client.post(f"{API}/parsing/manual", json=payload, headers=headers)

    async with _factory(engine)() as session:
        created = list((await session.scalars(select(Task).where(Task.user_id == user_id))).all())
    assert len(created) == FREE.parse


async def test_usage_endpoint_reports_current_quota(client, make_user):
    """GET /billing/usage отдаёт тариф и расход текущего пользователя."""
    email = "usage-endpoint@test.dev"
    headers = await _auth_headers(client, email)
    await make_user(email)

    response = await client.get(f"{API}/billing/usage", headers=headers)
    assert response.status_code == 200
    assert response.json()["tier"] == "free"
    assert response.json()["quotas"]["parse"]["used"] == 0


async def test_billing_endpoints_require_auth(client):
    """Кроме /tiers и вебхука эндпоинты биллинга закрыты авторизацией."""
    assert (await client.get(f"{API}/billing/usage")).status_code == 401
    assert (await client.get(f"{API}/billing/subscription")).status_code == 401


async def test_subscription_endpoint_for_free_user(client, make_user):
    """Без подписки /subscription отдаёт эффективный тариф free."""
    email = "sub-free@test.dev"
    headers = await _auth_headers(client, email)
    await make_user(email)

    payload = (await client.get(f"{API}/billing/subscription", headers=headers)).json()
    assert payload["tier"] == "free"
    assert payload["provider"] is None


# ============================================================
# Вебхуки платёжных шлюзов (docs/03 §11)
# ============================================================


def _yookassa_body(user_id: uuid.UUID, payment_id: str | None = None) -> bytes:
    """Тело события YooKassa payment.succeeded с metadata.user_id.

    ``payment_id`` по умолчанию уникален: он входит в ключ идемпотентности
    ``(provider, external_event_id)``, поэтому тесты, работающие с общей
    тестовой БД, иначе видели бы «duplicate» вместо «processed».
    """
    return json.dumps(
        {
            "event": "payment.succeeded",
            "object": {
                "id": payment_id or f"pay-{uuid.uuid4().hex[:12]}",
                "metadata": {"user_id": str(user_id), "tier": "pro"},
            },
        }
    ).encode()


def _yookassa_signature(body: bytes, secret: str = SECRET) -> str:
    """Подпись YooKassa: sha256 от тела запроса (как у шлюза)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _stripe_signature(body: bytes, secret: str = SECRET, timestamp: int | None = None):
    """Подпись Stripe: t=<unix> и v1=HMAC от «<t>.<body>»."""
    stamp = timestamp if timestamp is not None else int(time.time())
    signed = f"{stamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={digest}"


def _cloudpayments_body(user_id: uuid.UUID, payment_id: str | None = None) -> bytes:
    """Тело уведомления CloudPayments Completed с Tier."""
    return json.dumps(
        {
            "Status": "Completed",
            "PaymentId": payment_id or f"cp-{uuid.uuid4().hex[:12]}",
            "Tier": "enterprise",
            "user_id": str(user_id),
        }
    ).encode()


def _cloudpayments_signature(body: bytes, secret: str = SECRET) -> str:
    """Подпись CloudPayments: sha1 от тела запроса."""
    digest = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return f"sha1={digest}"


def test_verify_signature_rejects_bad_or_missing_secret():
    """Подпись проверяется строго; без настроенного секрета — отказ."""
    body = _yookassa_body(uuid.uuid4())
    headers = {"X-Signature": _yookassa_signature(body)}

    assert verify_signature("yookassa", body, headers, secret="wrong") is None
    # Секрет не настроен → подпись проверить нечем → принимать нельзя.
    assert verify_signature("yookassa", body, headers, secret="") is None
    assert verify_signature("yookassa", body, headers, secret=SECRET) is not None


def test_verify_signature_rejects_unknown_provider():
    """Неизвестный провайдер не принимается даже с «похожей» подписью."""
    body = _yookassa_body(uuid.uuid4())
    headers = {"X-Signature": _yookassa_signature(body)}
    assert verify_signature("paypal", body, headers, secret=SECRET) is None


def test_verify_stripe_rejects_stale_timestamp():
    """Stripe: событие старше окна отклоняется (защита от replay)."""
    user_id = uuid.uuid4()
    body = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "metadata": {"user_id": str(user_id), "tier": "pro"},
                }
            },
        }
    ).encode()
    stale = int(time.time()) - settings.billing_webhook_max_age_seconds - 60
    headers = {"Stripe-Signature": _stripe_signature(body, timestamp=stale)}

    assert verify_signature("stripe", body, headers, secret=SECRET) is None


async def test_yookassa_webhook_upgrades_tier(engine, make_user, monkeypatch):
    """Вебхук YooKassa переводит пользователя на оплаченный тариф."""
    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    user_id = await make_user()
    body = _yookassa_body(user_id)
    headers = {"X-Signature": _yookassa_signature(body)}

    async with _factory(engine)() as session:
        result = await process_webhook(session, "yookassa", body, headers)

    assert result["status"] == "processed"
    assert result["tier"] == "pro"

    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "pro"
        subscription = await session.scalar(
            select(Subscription).where(Subscription.user_id == user_id)
        )
        assert subscription.status == "active"
        assert subscription.provider == "yookassa"


async def test_cloudpayments_webhook_upgrades_to_enterprise(engine, make_user, monkeypatch):
    """Вебхук CloudPayments Completed переводит на тариф из payload."""
    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    user_id = await make_user()
    body = _cloudpayments_body(user_id)
    headers = {"Content-HMAC": _cloudpayments_signature(body)}

    async with _factory(engine)() as session:
        result = await process_webhook(session, "cloudpayments", body, headers)

    assert result["status"] == "processed"
    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "enterprise"


async def test_stripe_webhook_upgrades_tier(engine, make_user, monkeypatch):
    """Вебхук Stripe checkout.session.completed принимается по подписи t=…,v1=…."""
    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    user_id = await make_user()
    body = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex[:12]}",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_1",
                    "metadata": {"user_id": str(user_id), "tier": "pro"},
                }
            },
        }
    ).encode()
    headers = {"Stripe-Signature": _stripe_signature(body)}

    async with _factory(engine)() as session:
        result = await process_webhook(session, "stripe", body, headers)
    assert result["status"] == "processed"

    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "pro"


async def test_webhook_rejects_invalid_signature(engine, make_user, monkeypatch):
    """Вебхук с неверной подписью отклоняется и тариф не меняется."""
    from app.modules.billing.service import WebhookError

    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    user_id = await make_user()
    body = _yookassa_body(user_id, payment_id=f"pay-badsig-{uuid.uuid4().hex[:8]}")
    headers = {"X-Signature": "sha256=deadbeef"}

    async with _factory(engine)() as session:
        with pytest.raises(WebhookError) as excinfo:
            await process_webhook(session, "yookassa", body, headers)

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == "WEBHOOK_SIGNATURE_INVALID"

    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "free"


async def test_webhook_rejects_unknown_provider(engine, make_user, monkeypatch):
    """Неизвестный провайдер отклоняется с явной ошибкой."""
    from app.modules.billing.service import WebhookError

    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    body = _yookassa_body(uuid.uuid4())
    async with _factory(engine)() as session:
        with pytest.raises(WebhookError) as excinfo:
            await process_webhook(session, "bitcoin", body, {})

    assert excinfo.value.error_code == "WEBHOOK_PROVIDER_UNSUPPORTED"


async def test_webhook_endpoint_needs_no_auth_but_validates_signature(
    client, engine, make_user, monkeypatch
):
    """POST /billing/webhook/{provider} открыт, но проверяет подпись."""
    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    user_id = await make_user()
    body = _yookassa_body(user_id)

    # Неверная подпись → 400 (не 401: авторизация шлюзу не нужна).
    bad = await client.post(
        f"{API}/billing/webhook/yookassa",
        content=body,
        headers={"X-Signature": "sha256=00", "Content-Type": "application/json"},
    )
    assert bad.status_code == 400
    assert bad.json()["error_code"] == "WEBHOOK_SIGNATURE_INVALID"

    good = await client.post(
        f"{API}/billing/webhook/yookassa",
        content=body,
        headers={
            "X-Signature": _yookassa_signature(body),
            "Content-Type": "application/json",
        },
    )
    assert good.status_code == 200
    assert good.json()["status"] == "processed"


async def test_webhook_without_secret_is_rejected(client, engine, make_user, monkeypatch):
    """Если секрет не настроен, вебхук не принимается (безопасность оплаты)."""
    monkeypatch.setattr(settings, "billing_webhook_secret", "", raising=False)
    user_id = await make_user()
    body = _yookassa_body(user_id)

    response = await client.post(
        f"{API}/billing/webhook/yookassa",
        content=body,
        headers={
            "X-Signature": _yookassa_signature(body),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400

    async with _factory(engine)() as session:
        assert await get_tier(session, user_id) == "free"


async def test_webhook_too_large_body_is_rejected(client, monkeypatch):
    """Тело вебхука сверх BILLING_WEBHOOK_MAX_BODY_BYTES отклоняется (413)."""
    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    # Лимит не может быть меньше 1 КБ (защита от абсурдных значений), поэтому
    # тело делаем заведомо больше нижней границы.
    monkeypatch.setattr(settings, "billing_webhook_max_body_bytes", 2048, raising=False)
    body = json.dumps({"event": "payment.succeeded", "object": {"id": "x" * 8192}}).encode()

    response = await client.post(
        f"{API}/billing/webhook/yookassa",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error_code"] == "WEBHOOK_BODY_TOO_LARGE"


async def test_yookassa_webhook_is_idempotent(engine, make_user, monkeypatch):
    """Повторная доставка того же события не применяется дважды."""
    monkeypatch.setattr(settings, "billing_webhook_secret", SECRET, raising=False)
    user_id = await make_user()
    body = _yookassa_body(user_id, payment_id="pay-dup")
    headers = {"X-Signature": _yookassa_signature(body)}

    async with _factory(engine)() as session:
        first = await process_webhook(session, "yookassa", body, headers)
    async with _factory(engine)() as session:
        second = await process_webhook(session, "yookassa", body, headers)

    assert first["status"] == "processed"
    assert second["status"] == "duplicate"

    async with _factory(engine)() as session:
        events = list(
            (
                await session.scalars(
                    select(PaymentEvent).where(PaymentEvent.provider == "yookassa")
                )
            ).all()
        )
    assert len([e for e in events if e.external_event_id.endswith("pay-dup")]) == 1
