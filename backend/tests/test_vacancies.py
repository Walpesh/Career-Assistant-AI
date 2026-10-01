"""Интеграционные тесты: Vacancy Storage Module (docs/03 §4, docs/04 §7–§8).

Покрытие:
    - POST /vacancies/manual: извлечение hh_vacancy_id, первичный raw-парсинг
      (мокается fetch_raw_vacancy), статусы raw/error, 201/200;
    - дедупликация (user_id, hh_vacancy_id): обновление полей, applied
      не перезаписывается (docs/04 §8);
    - GET /vacancies: пагинация {items,total,page,size} и фильтры
      status / source / search / min_match_score;
    - PATCH /vacancies/{id}/status: граф жизненного цикла (docs/02 §5) → 409;
    - GET/DELETE /vacancies/{id}, изоляция пользователей → 404;
    - ограничения БД: UNIQUE (user_id, hh_vacancy_id) и CHECK status.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

# Модуль роутера (в него патчится fetch_raw_vacancy — точка первичного парсинга).
vacancy_router = importlib.import_module("app.modules.vacancy_storage.router")

from app.modules.vacancy_storage.parser import RawParseError, VacancyNotFound  # noqa: E402

API = "/api/v1"
EMAIL = "vacancies@example.com"
PASSWORD = "strongpassword"
VACANCY_URL = "https://novokuznetsk.hh.ru/vacancy/137866214"

RAW_FIELDS = {
    "title": "Python-разработчик",
    "company_name": "Рога и Копыта",
    "salary_from": 120000,
    "salary_to": 150000,
    "salary_currency": "RUR",
    "description_raw": "Требуется опыт с FastAPI и PostgreSQL.",
}


def make_fetch(**overrides):
    """Фейковый первичный парсер: возвращает RAW_FIELDS (или переданные поля)."""
    fields = {**RAW_FIELDS, **overrides}

    async def fake_fetch(url: str, **kwargs) -> dict:
        assert url  # ссылка всегда передаётся в парсер
        return dict(fields)

    return fake_fetch


async def auth(client, email: str = EMAIL) -> dict[str, str]:
    """Регистрация + login → заголовок Authorization."""
    response = await client.post(
        f"{API}/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    response = await client.post(
        f"{API}/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def add_manual(client, headers: dict, url: str = VACANCY_URL):
    return await client.post(
        f"{API}/vacancies/manual", json={"vacancy_url": url}, headers=headers
    )


# ============================================================
# POST /vacancies/manual — ручной ингест (docs/04 §4.3)
# ============================================================


async def test_manual_ingestion_creates_raw_vacancy(client, monkeypatch):
    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", make_fetch())
    headers = await auth(client)

    response = await add_manual(client, headers)
    assert response.status_code == 201, response.text
    data = response.json()

    assert data["hh_vacancy_id"] == "137866214"  # id из ссылки
    assert data["status"] == "raw"  # docs/02 §5: только добавлена
    assert data["source"] == "manual"
    assert data["url"] == VACANCY_URL
    assert data["title"] == RAW_FIELDS["title"]
    assert data["salary_from"] == RAW_FIELDS["salary_from"]

    listing = await client.get(f"{API}/vacancies", headers=headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1


async def test_manual_ingestion_dedup_updates_existing(client, monkeypatch):
    """(user_id, hh_vacancy_id): повтор → 200, поля обновлены, дублей нет."""
    headers = await auth(client)
    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", make_fetch())
    first = await add_manual(client, headers)
    assert first.status_code == 201

    monkeypatch.setattr(
        vacancy_router,
        "fetch_raw_vacancy",
        make_fetch(title="Senior Python Developer", salary_from=250000),
    )
    second = await add_manual(client, headers)
    assert second.status_code == 200  # обновление, а не создание
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["title"] == "Senior Python Developer"
    assert second.json()["salary_from"] == 250000
    assert second.json()["status"] == "raw"  # без анализа статус не меняется

    listing = await client.get(f"{API}/vacancies", headers=headers)
    assert listing.json()["total"] == 1  # дубликат не появился


async def test_manual_ingestion_never_overwrites_applied(client, monkeypatch):
    """docs/04 §8: статус applied не перезаписывается автоматически."""
    headers = await auth(client)
    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", make_fetch())
    created = await add_manual(client, headers)
    vacancy_id = created.json()["id"]

    patched = await client.patch(
        f"{API}/vacancies/{vacancy_id}/status",
        json={"status": "applied"},
        headers=headers,
    )
    assert patched.status_code == 200

    # Повторный ингест с другими данными не должен трогать запись.
    monkeypatch.setattr(
        vacancy_router, "fetch_raw_vacancy", make_fetch(title="НЕ ТРОГАТЬ")
    )
    response = await add_manual(client, headers)
    assert response.status_code == 200
    assert response.json()["status"] == "applied"
    assert response.json()["title"] == RAW_FIELDS["title"]


async def test_manual_ingestion_analyzed_keeps_status_but_updates_fields(
    client, monkeypatch
):
    """Analyzed не откатывается в raw, но контент-поля обновляются (TASK)."""
    headers = await auth(client)
    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", make_fetch())
    created = await add_manual(client, headers)
    vacancy_id = created.json()["id"]

    patched = await client.patch(
        f"{API}/vacancies/{vacancy_id}/status",
        json={"status": "analyzed"},
        headers=headers,
    )
    assert patched.status_code == 200

    monkeypatch.setattr(
        vacancy_router, "fetch_raw_vacancy", make_fetch(title="Обновлённый заголовок")
    )
    response = await add_manual(client, headers)
    assert response.status_code == 200
    assert response.json()["status"] == "analyzed"
    assert response.json()["title"] == "Обновлённый заголовок"


async def test_manual_ingestion_invalid_url_400(client):
    headers = await auth(client)

    for bad_url in (
        "https://hh.ru/employer/12345",  # не карточка вакансии
        "https://superjob.ru/vacancy/123",  # не hh.ru
        "novokuznetsk.hh.ru/vacancy/137866214",  # без схемы
    ):
        response = await add_manual(client, headers, url=bad_url)
        assert response.status_code == 400, response.text
        assert response.json()["error_code"] == "INVALID_VACANCY_URL"


async def test_manual_ingestion_not_found_marks_error_and_recovers(
    client, monkeypatch
):
    """docs/04 §5: удалённая вакансия → status=error; успех → восстановление raw."""
    headers = await auth(client)

    async def fake_not_found(url: str, **kwargs):
        raise VacancyNotFound("HTTP 404")

    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", fake_not_found)
    response = await add_manual(client, headers)
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "error"

    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", make_fetch())
    recovered = await add_manual(client, headers)
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "raw"
    assert recovered.json()["title"] == RAW_FIELDS["title"]


async def test_manual_ingestion_parse_failure_500_nothing_saved(client, monkeypatch):
    headers = await auth(client)

    async def fake_failure(url: str, **kwargs):
        raise RawParseError("капча")

    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", fake_failure)
    response = await add_manual(client, headers)
    assert response.status_code == 500
    assert response.json()["error_code"] == "PARSING_FAILED"

    listing = await client.get(f"{API}/vacancies", headers=headers)
    assert listing.json()["total"] == 0  # запись не создана


# ============================================================
# GET /vacancies — пагинация и фильтры (docs/03 §4)
# ============================================================


async def seed_vacancy(client, headers, monkeypatch, hh_id: str, **fields) -> dict:
    """Добавляет вакансию через ручной ингест с указанными полями."""
    monkeypatch.setattr(vacancy_router, "fetch_raw_vacancy", make_fetch(**fields))
    response = await add_manual(client, headers, url=f"https://hh.ru/vacancy/{hh_id}")
    assert response.status_code == 201, response.text
    return response.json()


async def test_list_requires_auth(client):
    response = await client.get(f"{API}/vacancies")
    assert response.status_code == 401
    response = await client.post(
        f"{API}/vacancies/manual", json={"vacancy_url": VACANCY_URL}
    )
    assert response.status_code == 401


async def test_list_pagination_and_filters(client, monkeypatch, engine):
    headers = await auth(client)
    first = await seed_vacancy(client, headers, monkeypatch, "111")
    await seed_vacancy(
        client, headers, monkeypatch, "222", title="Go разработчик", company_name="Yandex"
    )
    await seed_vacancy(client, headers, monkeypatch, "333", title="Python ML инженер")

    # Пагинация: { items, total, page, size } (формат фронтенда/мока).
    page1 = await client.get(f"{API}/vacancies?page=1&size=2", headers=headers)
    assert page1.status_code == 200
    body = page1.json()
    assert body["total"] == 3 and body["page"] == 1 and body["size"] == 2
    assert len(body["items"]) == 2

    page2 = await client.get(f"{API}/vacancies?page=2&size=2", headers=headers)
    assert len(page2.json()["items"]) == 1

    # Фильтр по статусу.
    patched = await client.patch(
        f"{API}/vacancies/{first['id']}/status",
        json={"status": "analyzed"},
        headers=headers,
    )
    assert patched.status_code == 200
    by_status = await client.get(f"{API}/vacancies?status=analyzed", headers=headers)
    assert by_status.json()["total"] == 1
    assert by_status.json()["items"][0]["hh_vacancy_id"] == "111"
    raw = await client.get(f"{API}/vacancies?status=raw", headers=headers)
    assert raw.json()["total"] == 2

    # Фильтр по источнику (все добавлены вручную) и невалидное значение.
    assert (await client.get(f"{API}/vacancies?source=manual", headers=headers)).json()[
        "total"
    ] == 3
    assert (await client.get(f"{API}/vacancies?source=auto", headers=headers)).json()[
        "total"
    ] == 0
    bad = await client.get(f"{API}/vacancies?status=processing", headers=headers)
    assert bad.status_code == 400  # docs/03 §9: ошибка валидации
    assert bad.json()["error_code"] == "VALIDATION_ERROR"

    # Поиск по названию и компании.
    by_title = await client.get(f"{API}/vacancies?search=ml", headers=headers)
    assert by_title.json()["total"] == 1
    by_company = await client.get(f"{API}/vacancies?search=yandex", headers=headers)
    assert by_company.json()["total"] == 1

    # min_match_score (значение проставляется Analysis Module — здесь SQL).
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE vacancies SET match_score = 85 WHERE hh_vacancy_id = '111'")
        )
    scored = await client.get(f"{API}/vacancies?min_match_score=80", headers=headers)
    assert scored.json()["total"] == 1
    assert scored.json()["items"][0]["hh_vacancy_id"] == "111"


# ============================================================
# Жизненный цикл статусов (docs/02 §5, docs/04 §8)
# ============================================================


async def test_status_lifecycle_integrity(client, monkeypatch):
    headers = await auth(client)
    vacancy = await seed_vacancy(client, headers, monkeypatch, "555")
    vacancy_id = vacancy["id"]

    async def patch(status_value: str):
        return await client.patch(
            f"{API}/vacancies/{vacancy_id}/status",
            json={"status": status_value},
            headers=headers,
        )

    # raw → analyzed → letter_ready → applied (основной поток docs/02 §5).
    assert (await patch("analyzed")).status_code == 200
    assert (await patch("letter_ready")).status_code == 200
    assert (await patch("applied")).status_code == 200

    # applied — терминальный статус: выход запрещён (409).
    back = await patch("raw")
    assert back.status_code == 409
    assert back.json()["error_code"] == "INVALID_STATUS_TRANSITION"

    # Идемпотентность: повтор того же статуса — 200.
    assert (await patch("applied")).status_code == 200

    # Нет пропусков шагов: raw → letter_ready невозможен.
    other = await seed_vacancy(client, headers, monkeypatch, "556")
    skip = await client.patch(
        f"{API}/vacancies/{other['id']}/status",
        json={"status": "letter_ready"},
        headers=headers,
    )
    assert skip.status_code == 409

    # Невалидное значение статуса → 400 (CHECK и Literal docs/02 §5).
    invalid = await client.patch(
        f"{API}/vacancies/{other['id']}/status",
        json={"status": "processing"},
        headers=headers,
    )
    assert invalid.status_code == 400
    assert invalid.json()["error_code"] == "VALIDATION_ERROR"

    # error восстанавливается в raw (docs/04 §5).
    await client.patch(
        f"{API}/vacancies/{other['id']}/status",
        json={"status": "error"},
        headers=headers,
    )
    restored = await client.patch(
        f"{API}/vacancies/{other['id']}/status",
        json={"status": "raw"},
        headers=headers,
    )
    assert restored.status_code == 200


# ============================================================
# GET/DELETE + изоляция пользователей + ограничения БД
# ============================================================


async def test_get_delete_and_user_isolation(client, monkeypatch):
    headers = await auth(client)
    vacancy = await seed_vacancy(client, headers, monkeypatch, "777")
    vacancy_id = vacancy["id"]

    detail = await client.get(f"{API}/vacancies/{vacancy_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["hh_vacancy_id"] == "777"

    # Чужой пользователь получает 404, а не 403 (без утечки существования).
    stranger = await auth(client, email="stranger@example.com")
    denied = await client.get(f"{API}/vacancies/{vacancy_id}", headers=stranger)
    assert denied.status_code == 404
    denied_delete = await client.delete(
        f"{API}/vacancies/{vacancy_id}", headers=stranger
    )
    assert denied_delete.status_code == 404

    # Неизвестный id → 404.
    missing = await client.get(
        f"{API}/vacancies/{uuid.uuid4()}", headers=headers
    )
    assert missing.status_code == 404

    deleted = await client.delete(f"{API}/vacancies/{vacancy_id}", headers=headers)
    assert deleted.status_code == 204
    gone = await client.get(f"{API}/vacancies/{vacancy_id}", headers=headers)
    assert gone.status_code == 404


async def test_db_unique_index_and_status_check(engine):
    """Ограничения docs/02 §3.3: UNIQUE (user_id, hh_vacancy_id) + CHECK status."""
    user_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                "VALUES (:id, :email, 'hash')"
            ),
            {"id": user_id, "email": f"{user_id}@example.com"},
        )
        await conn.execute(
            text(
                "INSERT INTO vacancies (id, user_id, hh_vacancy_id, url, status, source) "
                "VALUES (:id, :user_id, '42', 'https://hh.ru/vacancy/42', 'raw', 'manual')"
            ),
            {"id": vacancy_id, "user_id": user_id},
        )

    # Дубль (user_id, hh_vacancy_id) нарушает уникальный индекс.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO vacancies (user_id, hh_vacancy_id, url, status) "
                    "VALUES (:user_id, '42', 'https://hh.ru/vacancy/42', 'raw')"
                ),
                {"user_id": user_id},
            )

    # Статус вне перечисления docs/02 §5 нарушает CHECK.
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO vacancies (user_id, hh_vacancy_id, url, status) "
                    "VALUES (:user_id, '43', 'https://hh.ru/vacancy/43', 'processing')"
                ),
                {"user_id": user_id},
            )


# ============================================================
# Первичный raw-парсинг карточки (docs/04 §7 — что сохраняется)
# ============================================================


def test_extract_vacancy_fields_from_html():
    from app.modules.vacancy_storage.parser import extract_vacancy_fields

    html = """
    <html><head>
    <title>Python Dev - hh.ru</title>
    <meta property="og:title" content="Python разработчик | Acme - hh.ru">
    </head><body>
    <span data-qa="vacancy-company-name"><a href="#">Acme</a></span>
    <div data-qa="vacancy-salary">от 120 000 &#8381; до 150 000 &#8381;</div>
    <div data-qa="vacancy-experience">1–3 года</div>
    <div data-qa="vacancy-description">
        <p>Опыт с FastAPI.</p><ul><li>PostgreSQL</li><li>Docker</li></ul>
    </div>
    </body></html>
    """
    fields = extract_vacancy_fields(html)

    assert fields["title"] == "Python разработчик | Acme"  # суффикс hh.ru срезан
    assert fields["company_name"] == "Acme"
    assert fields["salary_from"] == 120000
    assert fields["salary_to"] == 150000
    assert fields["salary_currency"] == "RUR"
    assert fields["experience"] == "1–3 года"
    assert "PostgreSQL" in fields["description_raw"]  # очищенный текст
    assert fields["description_html"] == html  # сырой HTML для отладки
