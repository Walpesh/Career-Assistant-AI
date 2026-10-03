"""Проверка CSP-nonce: заголовок, подстановка в тело, отсутствие 'unsafe-inline'.

Запуск: cd backend && python -m tools.csp_check
Результат дублируется в csp_check_report.txt (обход проблем кодировки
консоли Windows с русскими сообщениями).
"""

import asyncio
import re
from pathlib import Path

from app.main import app
from httpx import ASGITransport, AsyncClient

#: Отчёт в файл — PowerShell mangling'ит UTF-8 stdout.
REPORT = Path(__file__).with_name("csp_check_report.txt")

_lines: list[str] = []


def emit(text: str) -> None:
    _lines.append(text)
    REPORT.write_text("\n".join(_lines) + "\n", encoding="utf-8")


def check(label: str, ok: bool, extra: str = "") -> bool:
    emit(f"  {'OK  ' if ok else 'FAIL'} {label}{(' — ' + extra) if extra else ''}")
    return ok


async def main() -> int:
    transport = ASGITransport(app=app)
    failures = 0

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        # 1. HTML-ответ: nonce в CSP и в теле должны совпадать.
        response = await client.get("/", headers={"Accept-Encoding": "gzip"})
        csp = response.headers.get("content-security-policy", "")
        header_nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        body_nonce = re.search(r'<script nonce="([^"]+)"', response.text)

        results = [
            check("HTML not gzipped", not response.headers.get("content-encoding")),
            check("placeholder __CSP_NONCE__ substituted", "__CSP_NONCE__" not in response.text),
            check(
                "CSP nonce matches HTML nonce",
                bool(header_nonce and body_nonce and header_nonce.group(1) == body_nonce.group(1)),
            ),
            check(
                "Content-Length matches body",
                response.headers.get("content-length") == str(len(response.content)),
            ),
            check("no 'unsafe-inline' in CSP", "unsafe-inline" not in csp),
            check("no external hosts in CSP", "http://" not in csp and "https://" not in csp),
            check("style-src is 'self' only", "style-src 'self'" in csp),
            check("font-src is 'self' only", "font-src 'self'" in csp),
        ]

        # 2. Nonce должен быть новым на каждый запрос.
        second = await client.get("/", headers={"Accept-Encoding": "gzip"})
        second_nonce = re.search(
            r"'nonce-([A-Za-z0-9_-]+)'", second.headers.get("content-security-policy", "")
        )
        results.append(
            check(
                "nonce rotates per request",
                bool(header_nonce and second_nonce and header_nonce.group(1) != second_nonce.group(1)),
            )
        )

        # 3. JSON-ответ: nonce не нужен, плейсхолдеров быть не должно.
        health = await client.get("/health")
        health_csp = health.headers.get("content-security-policy", "")
        results += [
            check("JSON CSP has no {nonce}", "{nonce}" not in health_csp),
            check("JSON CSP has no 'nonce-…'", "nonce-" not in health_csp),
            check("JSON still gzipped", bool(health.headers.get("content-encoding"))),
        ]

        # 4-5. Статика: собранный CSS и self-hosted шрифт.
        css = await client.get("/css/styles.min.css")
        results.append(check("styles.min.css served", css.status_code == 200))
        results.append(check("styles.min.css gzipped", bool(css.headers.get("content-encoding"))))

        font = await client.get("/fonts/inter-latin.3100e775.woff2")
        results.append(check("self-hosted font served", font.status_code == 200))

        failures = results.count(False)

    emit("")
    emit(
        f"csp_check: {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}"
    )
    print(f"csp_check: {'ALL CHECKS PASSED' if not failures else str(failures) + ' CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
