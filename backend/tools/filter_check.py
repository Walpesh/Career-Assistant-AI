"""Лёгкая проверка фильтров парсинга (docs/04_PARSING_RULES.md §4.1).

Запуск (из каталога backend):
    python tools/filter_check.py            # офлайн-проверка, без сети и БД
    python tools/filter_check.py --live     # + реальные POST /parsing/auto

Что проверяется (минимальная стоимость выполнения — нет сети, БД и LLM):
    1. каждый критерий фильтра (форма занятости / формат работы / график)
       корректно попадает в URL поиска hh.ru;
    2. фильтры комбинируются между собой и сохраняются при пагинации;
    3. полезная нагрузка интерфейса (все чипы сразу) проходит валидацию
       схемы POST /parsing/auto — комбинация больше не отклоняется с 400;
    4. ссылка группового парсера валидируется на hh.ru.

Режим --live дополнительно ставит задачи в очередь на запущенном backend
(сеть до hh.ru при этом не используется — парсинг выполняет воркер).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

# Консоль Windows по умолчанию cp1251 — принудительно UTF-8 для кириллицы.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover — перенаправленный поток
            pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.modules.parsing.schemas import ParseAutoRequest, ParseGroupRequest  # noqa: E402
from app.modules.parsing.service import is_blacklisted, normalize_blacklist  # noqa: E402
from app.modules.parsing.urls import (  # noqa: E402
    build_auto_search_url,
    build_page_url,
    validate_hh_search_url,
)

API_BASE = "http://localhost:8000/api/v1"

#: Критерии фильтра из интерфейса (frontend/partials/dashboard.html).
FILTER_CRITERIA: list[tuple[str, str, list[str]]] = [
    ("employment", "Форма занятости", ["full", "part", "project", "volunteer", "probation", "gph"]),
    ("work_format", "Формат работы", ["remote", "hybrid", "onsite"]),
    ("schedule", "График работы", ["fullDay", "flexible", "shift", "flyInFlyOut"]),
]

#: Имя поля ParseAutoRequest для каждого параметра URL.
FIELD_BY_PARAM = {
    "employment": "employment_forms",
    "work_format": "work_formats",
    "schedule": "schedules",
}

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
    print(f"\n{'-' * 78}\n{title}\n{'-' * 78}")


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


# --- 1. Каждый критерий фильтра (docs/04 §4.1 п.1) -------------------------
def check_each_criterion() -> None:
    section("1. Каждый критерий фильтра (docs/04 §4.1 п.1)")

    for param, title, values in FILTER_CRITERIA:
        url = build_auto_search_url(
            keywords=["python"], **{FIELD_BY_PARAM[param]: values}
        )
        query = query_of(url)
        check(
            f"{title}: {'/'.join(values)} → {param}",
            query.get(param) == ",".join(values),
            f"{param}={query.get(param)!r}",
        )

        # Пагинация сохраняет фильтр (docs/04 §4.2 п.2).
        page_query = query_of(build_page_url(url, 2))
        check(
            f"{title}: фильтр сохраняется на странице 3",
            page_query.get(param) == ",".join(values) and page_query.get("page") == "2",
            str(page_query),
        )


def check_combined_filters() -> None:
    section("2. Комбинация нескольких фильтров одновременно")
    url = build_auto_search_url(
        keywords=["python", "fastapi"],
        employment_forms=["full", "project"],
        work_formats=["remote", "hybrid", "onsite"],
        schedules=["fullDay", "flexible"],
    )
    query = query_of(url)
    check("Ключевые слова объединены в text", query.get("text") == "python fastapi", str(query))
    check(
        "Занятость + формат + график в одном URL",
        query.get("employment") == "full,project"
        and query.get("work_format") == "remote,hybrid,onsite"
        and query.get("schedule") == "fullDay,flexible",
        str(query),
    )

    page_query = query_of(build_page_url(url, 1))
    check(
        "Комбинация фильтров сохраняется при пагинации",
        page_query.get("employment") == "full,project"
        and page_query.get("work_format") == "remote,hybrid,onsite"
        and page_query.get("schedule") == "fullDay,flexible",
        str(page_query),
    )


# --------------------------------------------------------------------------
# 3. Валидация контракта POST /parsing/auto
# --------------------------------------------------------------------------
def check_request_schema() -> None:
    section("3. Контракт POST /parsing/auto (docs/03 §5)")

    full_payload = {
        "keywords": ["python", "fastapi"],
        "employment_forms": FILTER_CRITERIA[0][2],
        "work_formats": FILTER_CRITERIA[1][2],
        "schedules": FILTER_CRITERIA[2][2],
        "match_threshold": 75,
        "max_pages": 3,
    }
    try:
        ParseAutoRequest.model_validate(full_payload)
        check("Все чипы фильтров проходят валидацию (комбинация)", True)
    except Exception as exc:  # noqa: BLE001 — нужен текст ошибки в отчёте
        check("Все чипы фильтров проходят валидацию (комбинация)", False, str(exc))

    try:
        ParseAutoRequest.model_validate({"work_formats": ["remote"]})
        check("Фильтр без ключевых слов допустим", True)
    except Exception as exc:  # noqa: BLE001
        check("Фильтр без ключевых слов допустим", False, str(exc))

    try:
        ParseAutoRequest.model_validate({"keywords": ["python"], "work_formats": ["teleport"]})
        check("Неизвестное значение фильтра отклоняется", False, "валидация не сработала")
    except Exception:  # noqa: BLE001
        check("Неизвестное значение фильтра отклоняется", True)


# --------------------------------------------------------------------------
# 3.1. Чёрный список слов (docs/04 §4.9)
# --------------------------------------------------------------------------
def check_blacklist() -> None:
    section("3.1. Чёрный список слов (docs/04 §4.9)")

    payload = {
        "keywords": ["python"],
        "blacklist_enabled": True,
        "blacklist_words": ["ТК РФ", " ГПХ ", "тк рф"],
    }
    try:
        request = ParseAutoRequest.model_validate(payload)
        check("Чёрный список принимается схемой /parsing/auto", True)
        normalized = normalize_blacklist(request.blacklist_words)
        check(
            "Слова нормализуются (пробелы + дубли без учёта регистра)",
            normalized == ["ТК РФ", "ГПХ"],
            str(normalized),
        )
    except Exception as exc:  # noqa: BLE001
        check("Чёрный список принимается схемой /parsing/auto", False, str(exc))

    try:
        ParseGroupRequest.model_validate(
            {
                "search_url": "https://hh.ru/search/vacancy?text=python",
                "blacklist_enabled": True,
                "blacklist_words": ["ТК РФ"],
            }
        )
        check("Чёрный список принимается схемой /parsing/group", True)
    except Exception as exc:  # noqa: BLE001
        check("Чёрный список принимается схемой /parsing/group", False, str(exc))

    # По умолчанию тумблер выключен — фильтр не применяется.
    default_request = ParseAutoRequest.model_validate({"keywords": ["python"]})
    check(
        "По умолчанию тумблер выключен",
        default_request.blacklist_enabled is False and default_request.blacklist_words == [],
    )

    fields = {"title": "Python-разработчик", "description_raw": "Оформление строго по ТК РФ."}
    check(
        "Чёрное слово в описании найдено",
        is_blacklisted(fields, ["ТК РФ"]) == ["ТК РФ"],
    )
    check(
        "Регистронезависимая проверка",
        is_blacklisted(fields, ["тк рф"]) == ["тк рф"],
    )
    check(
        "Нет совпадений — вакансия проходит",
        is_blacklisted(fields, ["ГПХ", "1С"]) == [],
    )
    check(
        "Выключенный тумблер ничего не отсекает",
        is_blacklisted(fields, []) == [],
    )


# --------------------------------------------------------------------------
# 4. Ссылка группового парсера
# --------------------------------------------------------------------------
def check_group_url() -> None:
    section("4. Ссылка группового парсера (docs/04 §4.2)")
    try:
        url = validate_hh_search_url(
            "https://novokuznetsk.hh.ru/search/vacancy?text=python&work_format=REMOTE"
        )
        check("Ссылка hh.ru с фильтрами принимается", True)
        page_query = query_of(build_page_url(url, 1))
        check(
            "Фильтры из чужой ссылки сохраняются при пагинации",
            page_query.get("work_format") == "REMOTE" and page_query.get("page") == "1",
            str(page_query),
        )
    except Exception as exc:  # noqa: BLE001
        check("Ссылка hh.ru с фильтрами принимается", False, str(exc))

    for bad in ("https://evil.example.com/search?text=x", "ftp://hh.ru/search/vacancy"):
        try:
            validate_hh_search_url(bad)
            check(f"Внешний домен отклоняется: {bad[:32]}", False, "не отклонена")
        except Exception:  # noqa: BLE001
            check(f"Внешний домен отклоняется: {bad[:32]}", True)
# --------------------------------------------------------------------------
# Опционально: реальные POST /parsing/auto на запущенном backend
# --------------------------------------------------------------------------
async def check_live() -> None:
    import httpx

    section("5. Реальные POST /parsing/auto на запущенном backend")
    cases = [
        ("только формат работы", {"keywords": ["python"], "work_formats": ["remote"]}),
        (
            "комбинация всех фильтров",
            {
                "keywords": ["python"],
                "employment_forms": ["full", "project"],
                "work_formats": ["remote", "hybrid"],
                "schedules": ["fullDay", "flexible"],
            },
        ),
    ]

    async with httpx.AsyncClient(base_url=API_BASE, timeout=15.0) as client:
        email, password = "filter@example.com", "strongpassword"
        try:
            await client.post("/auth/register", json={"email": email, "password": password})
        except Exception:  # noqa: BLE001 — пользователь мог остаться с прошлого прогона
            pass

        login = await client.post("/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            check("Авторизация для live-проверок", False, f"{login.status_code} {login.text[:120]}")
            return
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        for title, payload in cases:
            response = await client.post(
                "/parsing/auto", json={**payload, "max_pages": 1}, headers=headers
            )
            check(
                f"POST /parsing/auto: {title}",
                response.status_code == 200 and "task_id" in response.json(),
                f"{response.status_code} {response.text[:160]}",
            )


def main() -> int:
    print("Проверка фильтров парсинга Career-Assistant-AI (docs/04 §4.1–§4.2)")
    check_each_criterion()
    check_combined_filters()
    check_request_schema()
    check_blacklist()
    check_group_url()

    if "--live" in sys.argv:
        asyncio.run(check_live())

    total = len(PASSED) + len(FAILED)
    print(f"\nИтог: {len(PASSED)}/{total} проверок пройдено")
    for name, detail in FAILED:
        print(f"  FAIL: {name} :: {detail}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
    print(f"\n{'-' * 78}\n{title}\n{'-' * 78}")


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))