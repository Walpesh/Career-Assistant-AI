"""UI smoke test Career-Assistant-AI (Playwright, демо-режим ?demo=1).

Что проверяется:
  1. Страница открывается, экран авторизации рендерится (Tailwind подключён).
  2. Вход в демо-режиме -> приложение показывает парсинг-дашборд (partial загружен).
  3. Вкладка «Анализ и Отклик»: список вакансий рендерится, открывается drawer письма.
  4. Вкладка «Моё резюме»: запуск convert_resume -> появляется Fadeout-action-popup.
  5. Реал-тайм журнал получает записи; мобильная вёрстка (bottom nav) работает.
  6. Скриншоты сохраняются в tools/.artifacts/.

Запуск (из корня репозитория):
    pip install playwright
    python -m playwright install chromium
    python tools/ui_smoke_test.py

Выходной код != 0 — есть ошибки консоли или проваленные проверки.
"""

from __future__ import annotations

import functools
import http.server
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

# Корректный вывод кириллицы в консоли Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
ARTIFACTS = Path(__file__).resolve().parent / ".artifacts"
HOST, PORT = "127.0.0.1", 5599
BASE_URL = f"http://{HOST}:{PORT}/?demo=1"


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    """Статический сервер frontend без лишних логов."""

    def log_message(self, *args):  # noqa: D102
        pass


def start_server() -> http.server.ThreadingHTTPServer:
    handler = functools.partial(QuietHandler, directory=str(FRONTEND_DIR))
    server = http.server.ThreadingHTTPServer((HOST, PORT), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def screenshot(page, name: str) -> None:
    """Скриншот с ретраем: OneDrive может блокировать перезапись файла."""
    target = ARTIFACTS / f"{name}.png"
    try:
        page.screenshot(path=str(target))
    except OSError:
        fallback = ARTIFACTS / f"{name}-{int(time.time())}.png"
        try:
            page.screenshot(path=str(fallback))
            print(f"    (скриншот сохранён как {fallback.name}: исходный файл занят)")
        except OSError as error:
            print(f"    (скриншот {name} пропущен: {error})")


def run_checks() -> int:
    failures: list[str] = []
    console_errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        status = "OK  " if condition else "FAIL"
        print(f"[{status}] {message}")
        if not condition:
            failures.append(message)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # ---------- Desktop ----------
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="ru-RU")
        page = context.new_page()
        page.on("console", lambda msg: console_errors.append(f"console: {msg.text}") if msg.type == "error" else None)
        page.on("pageerror", lambda err: console_errors.append(f"pageerror: {err}"))

        page.goto(BASE_URL, wait_until="load")
        page.wait_for_selector("#login-submit", timeout=15000)
        check(page.is_visible("#auth-screen"), "Экран авторизации отображается")

        # Tailwind подключился (CDN сгенерировал CSS): у карточки входа есть скругление.
        card_radius = page.evaluate(
            "getComputedStyle(document.querySelector('#auth-tabs').parentElement).borderRadius"
        )
        check(card_radius not in ("", "0px"), f"Tailwind CSS применён (border-radius={card_radius})")

        # Вход в демо-режиме (креды предзаполнены в mock-режиме).
        page.click("#login-submit")
        page.wait_for_selector("#parsing-modes", state="visible", timeout=15000)
        check(True, "Демо-вход выполнен, парсинг-дашборд отрендерен")
        check(page.locator("#parsing-modes [data-mode]").count() == 3, "Три режима парсинга отображаются")
        check(page.is_visible("#log-panel"), "Реал-тайм журнал виден на xl-экране")

        # Запуск автопоиска (демо-симуляция задачи + прогресс).
        page.fill("#auto-keywords input", "python")
        page.keyboard.press("Enter")
        page.click("#btn-parse-auto")
        page.wait_for_selector("#tasks-list [data-task-id]", timeout=15000)
        check(True, "Автопоиск запущен, карточка задачи появилась")
        page.wait_for_selector(".fap-item", timeout=15000)
        check(page.locator(".fap-item").count() >= 1, "Fadeout-action-popup показан после действия")
        screenshot(page, "desktop-dashboard")

        # Вкладка анализа.
        page.click('[data-tab="analysis"]')
        page.wait_for_selector("#view-analysis [data-vacancy-id]", timeout=15000)
        count = page.locator("#view-analysis [data-vacancy-id]").count()
        check(count >= 5, f"Список вакансий отрендерен ({count} карточек)")
        check(page.locator("#status-filters .chip").count() == 6, "Фильтры статусов отображаются")

        # Массовое действие: выбор вакансии -> появляется batch-панель.
        page.check("#view-analysis [data-vacancy-id] [data-vacancy-select]")
        check(page.is_visible("#batch-bar"), "Batch-панель появляется при выборе вакансии")
        page.uncheck("#view-analysis [data-vacancy-id] [data-vacancy-select]")

        # Drawer сопроводительного письма.
        letter_button = page.locator('#view-analysis [data-action="view-letter"]').first
        if letter_button.count() > 0:
            letter_button.click()
            page.wait_for_selector('#letter-drawer[data-open="1"]', timeout=10000)
            page.wait_for_function(
                "document.querySelector('#letter-content').textContent.includes('Здравствуйте')",
                timeout=10000,
            )
            check(True, "Drawer письма открылся и содержит текст письма")
            check(page.locator("#btn-download-letter").is_visible(), "Кнопки копирования / .txt доступны")
            page.keyboard.press("Escape")
        else:
            check(False, "Кнопка просмотра письма не найдена")

        # Журнал реал-тайм: появились записи о событиях.
        log_entries = page.locator("#log-list .log-entry").count()
        check(log_entries >= 1, f"Журнал реал-тайм получает события ({log_entries} записей)")
        screenshot(page, "desktop-analysis")

        # Вкладка профиля + convert_resume -> popup.
        page.click('[data-tab="profile"]')
        page.wait_for_selector("#profile-full-name", timeout=10000)
        # Данные профиля подгружаются асинхронно (GET /profile) — ждём compact_resume.
        page.wait_for_function(
            "document.querySelector('#compact-resume-view').textContent.includes('Python')",
            timeout=10000,
        )
        check(True, "compact_resume загружен и отображается в профиле")
        check(page.is_visible("#profile-threshold"), "Слайдер порога матчинга отображается")
        page.click("#btn-convert-resume")
        page.wait_for_selector(".fap-item", timeout=10000)
        check(True, "Уведомление о запуске convert_resume показано")
        screenshot(page, "desktop-profile")
        context.close()
        return finish(browser, failures, console_errors)


def finish(browser, failures: list[str], console_errors: list[str]) -> int:
    """Мобильные проверки + итоговая сводка."""
    mobile = browser.new_context(viewport={"width": 390, "height": 844}, locale="ru-RU", is_mobile=True, has_touch=True)
    mpage = mobile.new_page()
    mpage.goto(BASE_URL, wait_until="load")
    mpage.wait_for_selector("#login-submit", timeout=15000)
    mpage.click("#login-submit")
    mpage.wait_for_selector("#parsing-modes", state="visible", timeout=15000)

    mobile_ok = mpage.is_visible('nav[aria-label="Мобильная навигация"]')
    print(f"[{'OK  ' if mobile_ok else 'FAIL'}] Мобильная нижняя навигация отображается")
    if not mobile_ok:
        failures.append("mobile nav")

    mpage.click('nav[aria-label="Мобильная навигация"] [data-tab="analysis"]')
    mpage.wait_for_selector("#view-analysis [data-vacancy-id]", timeout=15000)
    print("[OK  ] Мобильная версия вкладки «Анализ и отклик» рендерится")
    screenshot(mpage, "mobile-analysis")
    mobile.close()
    browser.close()

    # Ошибки консоли: игнорируем только шум внешних CDN/фавикона.
    ignorable = ("favicon", "net::ERR_INTERNET_DISCONNECTED")
    real_errors = [e for e in console_errors if not any(token in e for token in ignorable)]
    print(f"[{'OK  ' if not real_errors else 'FAIL'}] Ошибок в консоли нет (найдено: {len(real_errors)})")
    for error in real_errors[:10]:
        print(f"    ! {error}")
    if real_errors:
        failures.append("console errors")

    print()
    print(f"Скриншоты: {ARTIFACTS}")
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        return 1
    print("Все проверки пройдены.")
    return 0


def main() -> int:
    if not FRONTEND_DIR.exists():
        print(f"Каталог frontend не найден: {FRONTEND_DIR}", file=sys.stderr)
        return 2
    ARTIFACTS.mkdir(exist_ok=True)

    server = start_server()
    time.sleep(0.4)
    try:
        return run_checks()
    except Exception as error:  # noqa: BLE001
        print(f"Ошибка выполнения смоук-теста: {error}", file=sys.stderr)
        print("Подсказка: python -m playwright install chromium", file=sys.stderr)
        return 3
    finally:
        server.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
