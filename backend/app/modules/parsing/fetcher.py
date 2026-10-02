"""Fallback Chain получения страниц hh.ru (docs/04_PARSING_RULES.md §2).

Цепочка строго по спецификации, от быстрого к надёжному:

    1. **curl_cffi** — основной быстрый путь: impersonate современного Chrome,
       cookies и заголовки сессии, JSON/HTML приходят без браузера;
    2. **Playwright + stealth** — надёжный путь при неудаче curl_cffi или при
       подозрении на защиту: полная эмуляция браузера + human-mimicry
       (кривые Безье, скролл, паузы «чтения» — docs/04 §3.3);
    3. **captcha/ручное вмешательство** — оба пути не дали usable-контента:
       возвращается лучший ответ, сессия классифицирует его детекторами и
       переводит задачу по таблице docs/04 §5 (failed / waiting_captcha).

Модуль не знает про БД и задачи: на входе URL, на выходе HTML или исключение
Proxy & Anti-Ban Module. Оркестратор Parsing сам решает, что делать с HTML.
"""

from __future__ import annotations

import asyncio
import logging
from types import MappingProxyType
from urllib.parse import urlsplit

from app.modules.anti_ban import FetchResponse, PreparedRequest
from app.modules.anti_ban.detectors import detect_captcha, detect_cloudflare_challenge
from app.modules.anti_ban.exceptions import ProxyError
from app.modules.anti_ban.human import random_mouse_path, reading_pause, scroll_deltas

__all__ = ["HhPageFetcher", "build_page_fetcher", "DEFAULT_TIMEOUT_SECONDS"]

logger = logging.getLogger(__name__)

#: Таймаут сетевого запроса, сек (паузы 4–8 с из docs/04 §1 уже внутри сессии).
DEFAULT_TIMEOUT_SECONDS = 20.0

#: Признаки заглушки/капчи вместо карточки вакансии.
_CAPTCHA_HEAD_MARKERS = ("доступ ограничен", "captcha", "just a moment")


def _is_usable(html: str) -> bool:
    """Контент пригоден для разбора (не капча и не пустая заглушка)."""
    if not html:
        return False
    lowered = html.lower()
    if detect_cloudflare_challenge(lowered):
        return False
    if "hh-lux-initialstate" in lowered or "serp-item" in lowered or "<article" in lowered:
        return True
    head = lowered[:4000]
    if any(marker in head for marker in _CAPTCHA_HEAD_MARKERS):
        return False
    return len(html) > 500


class HhPageFetcher:
    """Стратегия доступа к hh.ru: curl_cffi → Playwright+stealth (docs/04 §2).

    Экземпляр передаётся в AntiBanSession как fetcher: сессия сама делает
    прогрев, паузы 4–8 с, ротацию IP и retry по правилам docs/04 §3–§5,
    а этот объект отвечает только за способ получения HTML.
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        enable_browser_fallback: bool = True,
        impersonate: str = "chrome",
    ) -> None:
        self._timeout = timeout
        self._enable_browser_fallback = enable_browser_fallback
        self._impersonate = impersonate

    # --- интерфейс Fetcher (docs/01 §3: «Запрос → успешный HTML/JSON или ошибка») ---
    async def __call__(self, request: PreparedRequest) -> FetchResponse:
        """Получить страницу, последовательно пробуя методы fallback chain."""
        last_response: FetchResponse | None = None
        last_error: Exception | None = None

        # Шаг 1 — curl_cffi (основной быстрый путь, docs/04 §2 п.1).
        try:
            response = await self._fetch_curl_cffi(request)
            last_response = response
            if _is_usable(response.text) and not self._is_captcha(response):
                return response
        except ProxyError:
            raise
        except Exception as exc:  # noqa: BLE001 — переходим к браузерному пути
            last_error = exc
            logger.debug("curl_cffi не удался для %s: %s", request.url, exc)

        # Шаг 2 — Playwright + stealth (надёжный путь, docs/04 §2 п.2).
        if self._enable_browser_fallback:
            try:
                return await self._fetch_playwright(request)
            except ProxyError:
                raise
            except Exception as exc:  # noqa: BLE001 — далее шаг 3
                last_error = exc
                logger.debug("Playwright не удался для %s: %s", request.url, exc)

        # Шаг 3 — капча/защита: отдаём лучший ответ, чтобы сессия классифицировала
        # его детекторами и применила реакцию из таблицы docs/04 §5.
        if last_response is not None:
            return last_response
        if last_error is not None:
            raise ProxyError(f"Не удалось получить {request.url}: {last_error}") from last_error
        raise ProxyError(f"Пустой ответ по {request.url}")

    @staticmethod
    def _is_captcha(response: FetchResponse) -> bool:
        """Детект капчи по телу ответа (docs/04 §5 — остановка воркера)."""
        return detect_captcha(
            response.status_code,
            headers=dict(response.headers or {}),
            body=(response.text or "")[:20000],
            url=response.url or "",
        )

    async def _fetch_curl_cffi(self, request: PreparedRequest) -> FetchResponse:
        """HTTP-запрос с impersonate Chrome (docs/04 §2 п.1)."""
        from curl_cffi.requests import AsyncSession

        proxies = (
            {"http": request.proxy_url, "https": request.proxy_url}
            if request.proxy_url
            else None
        )
        async with AsyncSession() as session:
            response = await session.get(
                request.url,
                headers=dict(request.headers),
                cookies=request.cookies or None,
                impersonate=self._impersonate,
                proxies=proxies,
                timeout=self._timeout,
                allow_redirects=True,
            )
            return FetchResponse(
                status_code=response.status_code,
                headers=dict(response.headers),
                text=response.text or "",
                url=str(response.url),
            )

    async def _fetch_playwright(self, request: PreparedRequest) -> FetchResponse:
        """Браузерный путь со stealth и human-mimicry (docs/04 §2 п.2, §3.3)."""
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                proxy=self._browser_proxy(request.proxy_url),
            )
            try:
                context = await browser.new_context(
                    user_agent=request.fingerprint.user_agent,
                    locale="ru-RU",
                    extra_http_headers=self._extra_headers(request.headers),
                )
                await self._stealth(context)
                if request.cookies:
                    await context.add_cookies(
                        [
                            {"name": name, "value": value, "domain": "hh.ru", "path": "/"}
                            for name, value in request.cookies.items()
                        ]
                    )
                page = await context.new_page()
                response = await page.goto(
                    request.url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout * 1000,
                )
                await self._human_mimicry(page)
                html = await page.content()
                status = response.status if response is not None else 200
                headers = dict(await response.all_headers()) if response is not None else {}
                result = FetchResponse(
                    status_code=status,
                    headers=MappingProxyType(headers),
                    text=html,
                    url=page.url,
                )
                await context.close()
                return result
            finally:
                await browser.close()

    @staticmethod
    async def _stealth(context) -> None:
        """Применение stealth-патчей (docs/04 §2 п.2: playwright-stealth).

        Отсутствие stealth не должно ломать парсинг — патчи best-effort.
        """
        try:
            from playwright_stealth import Stealth  # type: ignore

            await Stealth().apply_stealth_async(context)
        except Exception as exc:  # noqa: BLE001 — stealth опционален
            logger.debug("stealth-патчи не применены: %s", exc)

    @staticmethod
    def _extra_headers(headers) -> dict[str, str]:
        """Заголовки, которые Chromium задаёт сам (прокидываем остальные)."""
        skip = {"user-agent", "accept-encoding", "content-length", "host", "connection"}
        return {key: value for key, value in headers.items() if key.lower() not in skip}

    @staticmethod
    def _browser_proxy(proxy_url: str | None) -> dict[str, str] | None:
        """Прокси для Chromium (docs/04 §3.1 — только резидентные)."""
        if not proxy_url:
            return None
        parts = urlsplit(proxy_url)
        if not parts.hostname:
            return None
        result: dict[str, str] = {
            "server": f"{parts.scheme}://{parts.hostname}:{parts.port or 80}"
        }
        if parts.username:
            result["username"] = parts.username
            result["password"] = parts.password or ""
        return result

    @staticmethod
    async def _human_mimicry(page) -> None:
        """Живое поведение на странице: мышь по Безье, скролл, паузы чтения."""
        try:
            for x, y in random_mouse_path((12.0, 12.0), (420.0, 360.0))[1:]:
                await page.mouse.move(x, y)
                await asyncio.sleep(0.01)
            for delta in scroll_deltas():
                await page.mouse.wheel(0, delta)
                await asyncio.sleep(0.08)
            await asyncio.sleep(reading_pause())
        except Exception as exc:  # noqa: BLE001 — мимикрия не влияет на результат
            logger.debug("human-mimicry пропущена: %s", exc)


def build_page_fetcher(**kwargs) -> HhPageFetcher:
    """Fetcher-фабрика для AntiBanSession (удобно для подмены в тестах)."""
    return HhPageFetcher(**kwargs)
