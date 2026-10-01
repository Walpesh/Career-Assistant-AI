"""User-Agent и браузерные заголовки, согласованные между собой
(docs/04_PARSING_RULES.md §3.3: «ротация реальных User-Agent (последние Chrome,
Firefox, Safari)» + «полный набор браузерных заголовков»).

Ключевой принцип — консистентность отпечатка:
    - версия в User-Agent совпадает с sec-ch-ua (Chrome);
    - платформа в UA совпадает с sec-ch-ua-platform;
    - Firefox/Safari не шлют Client Hints (как в реальных браузерах);
    - Sec-Fetch-* и Accept зависят от типа запроса (document/XHR);
    - Accept-Language един для всех запросов сессии (ru-подсети docs/04 §3.1).

Accept-Encoding намеренно НЕ генерируется: кодеки управляет транспорт
(httpx/curl_cffi), и реклама неподдерживаемых кодеков сломала бы декомпрессию.

Актуальность версий: стабильные каналы на момент разработки — Chrome 154/153
(окт 2026), Firefox 157 (29.09.2026), Safari 27.x. Обновляется в _PROFILES.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

__all__ = [
    "Fingerprint",
    "generate_fingerprint",
    "generate_user_agent",
    "SEC_FETCH_PROFILES",
]

# Единый язык сессии (docs/04 §3.1 — российские подсети / страна пользователя).
_ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.9,en;q=0.8"

_ACCEPT = {
    "chrome": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "firefox": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"
    ),
    "safari": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Sec-Fetch-* наборы по типу запроса (docs/04 §3.3 — полный набор заголовков).
SEC_FETCH_PROFILES: dict[str, dict[str, str]] = {
    # Прямая навигация по URL (листинг, карточка вакансии, прогрев homepage).
    "document": {
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    },
    # XHR/fetch внутри hh.ru (API подгрузки вакансий).
    "empty": {
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "same-origin",
        "Sec-Fetch-Site": "same-origin",
    },
}

_UA_TEMPLATES: dict[str, str] = {
    "chrome": (
        "Mozilla/5.0 ({platform_token}) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/{major}.0.0.0 Safari/537.36"
    ),
    "firefox_windows": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{major}.0) "
        "Gecko/20100101 Firefox/{major}.0"
    ),
    "firefox_macos": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{major}.0) "
        "Gecko/20100101 Firefox/{major}.0"
    ),
    "safari": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/{major}.0 Safari/605.1.15"
    ),
}

# Профили: (браузер, платформа, токен UA-платформы, версия sec-ch-ua-platform,
# major). Только desktop — парсинг идёт с настольных «устройств».
_PROFILES: tuple[tuple[str, str, str, str, int], ...] = (
    ("chrome", "windows", "Windows NT 10.0; Win64; x64", "Windows", 154),
    ("chrome", "windows", "Windows NT 10.0; Win64; x64", "Windows", 153),
    ("chrome", "macos", "Macintosh; Intel Mac OS X 10_15_7", "macOS", 154),
    ("chrome", "linux", "X11; Linux x86_64", "Linux", 153),
    ("firefox", "windows", "Windows NT 10.0; Win64; x64", "Windows", 157),
    ("firefox", "macos", "Macintosh; Intel Mac OS X 10_15_7", "macOS", 157),
    ("safari", "macos", "Macintosh; Intel Mac OS X 10_15_7", "macOS", 27),
)


def _sec_ch_ua(major: int) -> str:
    """Client Hints Chrome в формате реального браузера (+GREASE-бренд)."""
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not(A:Brand";v="24"'


@dataclass(frozen=True)
class Fingerprint:
    """Согласованный браузерный отпечаток одной сессии."""

    browser: str  # "chrome" | "firefox" | "safari"
    platform: str  # "windows" | "macos" | "linux"
    major_version: int
    user_agent: str
    accept: str
    accept_language: str = _ACCEPT_LANGUAGE
    sec_ch_ua: str | None = None
    sec_ch_ua_platform: str | None = None

    def headers(self, dest: str = "document") -> dict[str, str]:
        """Полный набор браузерных заголовков для запроса типа dest.

        Args:
            dest: "document" — навигация по странице, "empty" — XHR/fetch.
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": self.accept,
            "Accept-Language": self.accept_language,
        }
        if self.sec_ch_ua:  # только Chrome (Firefox/Safari не шлют Client Hints)
            headers["sec-ch-ua"] = self.sec_ch_ua
            headers["sec-ch-ua-mobile"] = "?0"
            headers["sec-ch-ua-platform"] = f'"{self.sec_ch_ua_platform}"'
        headers.update(SEC_FETCH_PROFILES[dest])
        if dest == "document" and self.browser in ("chrome", "firefox"):
            headers["Upgrade-Insecure-Requests"] = "1"
        return headers


def generate_fingerprint(rng: random.Random | None = None) -> Fingerprint:
    """Случайный согласованный отпечаток из профилей (ротация §3.3)."""
    source = rng if rng is not None else random
    browser, platform, platform_token, ch_platform, major = source.choice(_PROFILES)
    if browser == "chrome":
        user_agent = _UA_TEMPLATES["chrome"].format(
            platform_token=platform_token, major=major
        )
        sec_ch_ua, sec_ch_platform = _sec_ch_ua(major), ch_platform
    elif browser == "firefox":
        user_agent = _UA_TEMPLATES[f"firefox_{platform}"].format(major=major)
        sec_ch_ua = sec_ch_platform = None
    else:  # safari
        user_agent = _UA_TEMPLATES["safari"].format(major=major)
        sec_ch_ua = sec_ch_platform = None
    return Fingerprint(
        browser=browser,
        platform=platform,
        major_version=major,
        user_agent=user_agent,
        accept=_ACCEPT[browser],
        sec_ch_ua=sec_ch_ua,
        sec_ch_ua_platform=sec_ch_platform,
    )


def generate_user_agent(rng: random.Random | None = None) -> str:
    """Только User-Agent случайного профиля (для curl_cffi impersonate)."""
    return generate_fingerprint(rng).user_agent

