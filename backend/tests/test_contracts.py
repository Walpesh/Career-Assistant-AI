"""Контрактная валидация: OpenAPI ↔ docs/03 ↔ frontend ``api.js``.

Смысл проверки: контрактная документация (``docs/03_API_CONTRACTS.md``) —
единственный источник правды, а фронтенд (``frontend/js/core/api.js``)
кодирует эти же пути строками. Расхождение ломает приложение в рантайме
и не ловится компилятором, поэтому здесь оно превращается в тест.

Что проверяется:
    - каждый вызов ``request(...)`` из ``api.js`` существует в OpenAPI;
    - метод и путь совпадают (с учётом шаблонных литералов и api_prefix);
    - каждый эндпоинт из таблиц ``docs/03`` реализован в приложении;
    - каждый маршрут ``/api/v1`` описан в ``docs/03`` (документация не устарела);
    - коды ответов из ``docs/03 §9`` объявлены в схеме ошибок приложения.

Тесты не ходят в сеть: OpenAPI строится из ASGI-приложения, ``api.js``
и ``docs/03`` читаются как файлы репозитория.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.core.config import settings
from app.core.errors import DEFAULT_ERROR_CODES
from app.main import app

#: Корень репозитория: backend/tests/test_contracts.py → корень.
REPO_ROOT = Path(__file__).resolve().parents[2]
API_JS = REPO_ROOT / "frontend" / "js" / "core" / "api.js"
CONTRACTS_MD = REPO_ROOT / "docs" / "03_API_CONTRACTS.md"


@pytest.fixture(scope="module")
def schema() -> dict:
    """OpenAPI-схема приложения (строится один раз на модуль)."""
    return app.openapi()


@pytest.fixture(scope="module")
def operations(schema: dict) -> set[tuple[str, str]]:
    """Множество пар (path, METHOD) из OpenAPI — с нормализованными путями."""
    return {
        (_openapi_normalized(path), method.upper())
        for path, methods in schema["paths"].items()
        for method in methods
    }


# ============================================================
# api.js ↔ OpenAPI
# ============================================================

#: request('METHOD', 'path') — статические строковые пути.
_STATIC_CALL = re.compile(r"request\(\s*'(GET|POST|PUT|PATCH|DELETE)'\s*,\s*'([^']+)'")

#: request('METHOD', `path`) — шаблонные литералы с ${...}.
_TEMPLATE_CALL = re.compile(r"request\(\s*'(GET|POST|PUT|PATCH|DELETE)'\s*,\s*`([^`]+)`")

#: fetch(`${CONFIG.API_BASE}/path`) — прямые вызовы мимо request().
#: Второй аргумент-guard отсекает обобщённый вызов внутри самой функции
#: request(): ``fetch(`${CONFIG.API_BASE}${path}`, …)``.
_DIRECT_FETCH = re.compile(r"fetch\(\s*`\$\{CONFIG\.API_BASE\}(?!\$\{)([^`]+)`")


def _normalize_template(path: str) -> str:
    """``/tasks/${id}/cancel`` → ``/tasks/{param}/cancel``.

    Имена параметров во фронтенде и в OpenAPI различаются (``id`` против
    ``task_id``), поэтому сравниваем структуру пути, а не имя параметра.
    """
    path = re.sub(r"\$\{buildQuery\([^}]*\)\}", "", path)
    path = re.sub(r"\$\{[^}]+\}", "{param}", path)
    return path.rstrip("/") or "/"


def _openapi_normalized(path: str) -> str:
    """``/api/v1/tasks/{task_id}`` → ``/tasks/{param}`` (api_prefix убран)."""
    prefix = settings.api_prefix.rstrip("/")
    if prefix and path.startswith(prefix):
        path = path[len(prefix) :]
    return re.sub(r"\{[^}]+\}", "{param}", path).rstrip("/") or "/"


def _frontend_calls() -> list[tuple[str, str]]:
    """Все REST-вызовы из ``api.js`` как (путь, METHOD)."""
    source = API_JS.read_text(encoding="utf-8")
    calls: list[tuple[str, str]] = [
        (_normalize_template(path), method) for method, path in _STATIC_CALL.findall(source)
    ]
    calls += [
        (_normalize_template(path), method) for method, path in _TEMPLATE_CALL.findall(source)
    ]
    # Прямой fetch идёт без метода (GET) — так сделан /account/export.
    calls += [(_normalize_template(path), "GET") for path in _DIRECT_FETCH.findall(source)]
    return calls


def test_sources_exist():
    """Фронтенд-клиент и документация на месте — иначе проверка бессмысленна."""
    assert API_JS.exists(), f"Не найден {API_JS}"
    assert CONTRACTS_MD.exists(), f"Не найден {CONTRACTS_MD}"


def test_frontend_calls_are_discovered():
    """Парсер находит вызовы в api.js (защита от «тест проходит вхолостую»)."""
    calls = _frontend_calls()
    assert len(calls) >= 20, f"Найдено лишь {len(calls)} вызовов — парсер сломался"
    assert ("/auth/login", "POST") in calls
    assert ("/analysis/{param}", "GET") in calls
    assert ("/tasks/{param}/resume", "POST") in calls


def test_every_frontend_endpoint_exists_in_openapi(operations):
    """Каждый вызов api.js есть в OpenAPI: иначе фронт получит 404/405."""
    missing = sorted(
        (path, method) for path, method in _frontend_calls() if (path, method) not in operations
    )
    assert not missing, (
        "Вызовы frontend/js/core/api.js, которых нет в OpenAPI:\n"
        + "\n".join(f"  {method} {path}" for path, method in missing)
        + "\n(сверьте с docs/03_API_CONTRACTS.md)"
    )


# ============================================================
# docs/03_API_CONTRACTS.md ↔ OpenAPI
# ============================================================

#: Строка таблицы: ``| POST | `/auth/login` | Описание | Нет |``
_DOC_ROW = re.compile(
    r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`\s*\|[^|]*\|\s*([^|]+?)\s*\|",
    re.MULTILINE,
)

#: Разделы документации со сводным кодом ответов (docs/03 §9).
STATUS_SECTION = "## 9."


def _documented_endpoints() -> set[tuple[str, str]]:
    """Эндпоинты из markdown-таблиц docs/03 как (путь, METHOD).

    Путь нормализуется так же, как в OpenAPI, чтобы ``/tasks/{task_id}``
    совпадал с ``/tasks/${id}`` из фронтенда.
    """
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    rows: set[tuple[str, str]] = set()
    for method, path, _auth in _DOC_ROW.findall(text):
        if not path.startswith("/"):
            continue
        # WebSocket-эндпоинт не попадает в OpenAPI.
        rows.add((_openapi_normalized(path), method))
    return rows


def _documented_auth_requirements() -> dict[tuple[str, str], bool]:
    """Карта (путь, METHOD) → требуется ли авторизация (docs/03 «Auth»)."""
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    result: dict[tuple[str, str], bool] = {}
    for method, path, auth in _DOC_ROW.findall(text):
        if not path.startswith("/"):
            continue
        result[(_openapi_normalized(path), method)] = auth.strip().strip("*") == "Да"
    return result


def test_documented_endpoints_are_discovered():
    """Парсер находит эндпоинты в docs/03 (защита от «тест проходит вхолостую»)."""
    documented = _documented_endpoints()
    assert len(documented) >= 25, f"Найдено лишь {len(documented)} эндпоинтов"
    assert ("/auth/login", "POST") in documented
    assert ("/analysis/run", "POST") in documented
    assert ("/tasks/{param}/resume", "POST") in documented
    assert ("/account", "DELETE") in documented


def test_every_documented_endpoint_is_implemented(operations):
    """Каждый эндпоинт из docs/03 реализован.

    Устаревший пункт в документации опаснее отсутствующего: по нему пишут
    клиенты, которых потом приходится ломать.
    """
    missing = sorted(
        (path, method) for path, method in _documented_endpoints() if (path, method) not in operations
    )
    assert not missing, (
        "Эндпоинты из docs/03_API_CONTRACTS.md, которых нет в OpenAPI:\n"
        + "\n".join(f"  {method} {path}" for path, method in missing)
    )


def test_implemented_api_routes_are_documented(schema, operations):
    """Каждый маршрут ``/api/v1`` описан в docs/03 (документация не устарела)."""
    documented = _documented_endpoints()
    undocumented = sorted(
        f"{method} {path}"
        for path, methods in schema["paths"].items()
        if path.startswith(settings.api_prefix)
        for method in methods
        if (_openapi_normalized(path), method.upper()) not in documented
    )
    assert not undocumented, (
        "Маршруты OpenAPI, отсутствующие в docs/03_API_CONTRACTS.md:\n"
        + "\n".join(f"  {entry}" for entry in undocumented)
    )


def test_auth_requirements_match_documentation(operations):
    """Колонка «Auth» в docs/03 совпадает с наличием security-схемы в OpenAPI."""
    components = app.openapi().get("components", {}).get("securitySchemes", {})
    has_bearer = bool(components)
    assert has_bearer, "В OpenAPI нет securitySchemes — Bearer не описан"

    mismatches = []
    for (path, method), needs_auth in _documented_auth_requirements().items():
        if (path, method) not in operations:
            continue  # уже проверено test_every_documented_endpoint_is_implemented
        operation = app.openapi()["paths"].get(
            _denormalize(path, method), {}
        ).get(method.lower(), {})
        declared = bool(operation.get("security"))
        if declared != needs_auth:
            mismatches.append(f"  {method} {path}: docs={needs_auth}, openapi={declared}")
    assert not mismatches, (
        "Расхождение требований авторизации с docs/03 «Auth»:\n" + "\n".join(mismatches)
    )


def _denormalize(normalized_path: str, method: str) -> str:
    """Обратное преобразование ``/tasks/{param}`` → реальный путь OpenAPI."""
    prefix = settings.api_prefix.rstrip("/")
    for path in app.openapi()["paths"]:
        if method.lower() in app.openapi()["paths"][path]:
            if _openapi_normalized(path) == normalized_path:
                return path
    return f"{prefix}{normalized_path}"


# ============================================================
# Коды ответов docs/03 §9 ↔ DEFAULT_ERROR_CODES
# ============================================================


def _documented_status_codes() -> set[int]:
    """Числовые коды из сводной таблицы docs/03 §9."""
    text = CONTRACTS_MD.read_text(encoding="utf-8")
    start = text.index(STATUS_SECTION)
    table = text[start : start + 2000]
    return {int(code) for code in re.findall(r"^\|\s*(\d{3})\s*\|", table, re.MULTILINE)}


def test_documented_status_codes_are_discovered():
    """Парсер находит коды в docs/03 §9 (защита от пустого теста)."""
    codes = _documented_status_codes()
    assert {200, 201, 202, 400, 401, 403, 404, 409, 429, 500} <= codes, codes
    assert 413 in codes, "docs/03 §9 описывает 413 (вебхук), код должен быть в таблице"


def test_documented_status_codes_have_error_code_defaults():
    """Каждому документированному **коду ошибки** соответствует error_code.

    Формат ошибок — единый ``{ detail, error_code }`` (docs/03 §1); без кода
    по умолчанию клиент не отличит QUOTA_EXCEEDED от RATE_LIMITED в теле 429.
    Успешные коды (2xx) error_code не имеют — это не ошибки.
    """
    error_codes = sorted(
        code for code in _documented_status_codes() if code >= 400
    )
    without_code = sorted(code for code in error_codes if code not in DEFAULT_ERROR_CODES)
    assert not without_code, (
        "Коды ошибок из docs/03 §9 без error_code по умолчанию в app.core.errors: "
        f"{without_code}"
    )


def test_error_response_schema_is_uniform():
    """Все ошибки отдают ``{ detail, error_code }`` (docs/03 §1)."""
    from app.core.errors import AppError

    error = AppError(404, "Не найдено")
    assert error.error_code == "NOT_FOUND"
    assert AppError(429, "...", "RATE_LIMITED").error_code == "RATE_LIMITED"
    # Без явного кода берётся дефолт по статусу — ошибка всегда с error_code.
    assert AppError(401, "...").error_code == "UNAUTHORIZED"
    assert AppError(500, "...").error_code == "INTERNAL_ERROR"


def test_schema_declares_bearer_security():
    """Bearer-схема описана в OpenAPI и используется защищёнными маршрутами."""
    schema = app.openapi()
    schemes = schema.get("components", {}).get("securitySchemes", {})
    assert any(
        value.get("scheme", "").lower() == "bearer" for value in schemes.values()
    ), schemes

    # Хотя бы один маршрут объявляет требование Bearer.
    secured = [
        f"{method.upper()} {path}"
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
        if operation.get("security")
    ]
    assert secured, "Ни один маршрут не объявляет security в OpenAPI"
    assert any("/auth/me" in entry for entry in secured), secured


def test_websocket_endpoint_is_not_in_openapi():
    """WebSocket-канал не описывается в OpenAPI (docs/03 §8), только в docs."""
    schema = app.openapi()
    ws_paths = [path for path in schema["paths"] if path.startswith("/ws")]
    assert not ws_paths, f"WebSocket-путь попал в OpenAPI: {ws_paths}"
    # При этом он описан в документации.
    assert "/ws" in CONTRACTS_MD.read_text(encoding="utf-8")


def test_health_and_metrics_are_separate_from_api_contract():
    """Служебные маршруты (/health, /metrics) не смешаны с /api/v1."""
    schema = app.openapi()
    assert "/health" in schema["paths"]
    assert "/metrics/summary" in schema["paths"]
    # Их нет в контрактных таблицах docs/03 — они служебные (docs/01 §7).
    documented = _documented_endpoints()
    assert not any(path.startswith("/health") for path, _ in documented)
    assert not any(path.startswith("/metrics") for path, _ in documented)