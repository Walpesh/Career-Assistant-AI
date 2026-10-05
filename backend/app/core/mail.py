"""Отправка email (Auth Module): OTP-коды подтверждения регистрации.

Реализация на ``aiosmtplib``: соединение асинхронное, поэтому отправка из
``BackgroundTasks`` не блокирует event-loop API. Письмо содержит текстовую и
HTML-часть (multipart/alternative) — почтовые клиенты сами выбирают версию.

Хранение кода: в БД уходит только HMAC-SHA256-хэш (см. EmailOtp.otp_code_hash),
сам код нигде не пишется. Ключ подписи — ``JWT_SECRET``: без него хэш не
перебирается офлайн, даже украв дамп БД.

Отправка никогда не «проглатывает» ошибки молча: ``send_verification_email``
возвращает ``True`` только если письмо ушло, а любой отказ (нет SMTP_HOST,
не установлен aiosmtplib, отказ аутентификации/сети) логируется с полным
traceback и возвращает ``False``. Решение «что делать с False» принимает
вызывающий код (``app.modules.auth.router``): в development код печатается
в лог и разработка не блокируется, в production эндпоинт отвечает
``503 SMTP_UNAVAILABLE``, а не сообщает об успешной отправке (docs/03 §2).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from email.message import EmailMessage
from typing import TypedDict

from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "generate_otp_code",
    "hash_otp_code",
    "verify_otp_code",
    "send_verification_email",
    "build_verification_email",
    "smtp_connect_kwargs",
    "SmtpConnectKwargs",
]

#: Длина OTP-кода (цифры) — строго 6 по ТЗ.
OTP_LENGTH = 6

#: Таймаут SMTP-операции (connect + отправка), сек.
SMTP_TIMEOUT_SECONDS = 15.0

#: Порты, для которых режим шифрования известен однозначно: 465 — implicit
#: SSL, 587/2525 — STARTTLS после обычного CONNECT.
KNOWN_PORT_SECURITY: dict[int, str] = {465: "ssl", 587: "starttls", 2525: "starttls"}


class SmtpConnectKwargs(TypedDict):
    """Параметры подключения к SMTP в терминах ``aiosmtplib.send``."""

    hostname: str
    port: int
    username: str | None
    password: str | None
    start_tls: bool
    use_tls: bool


def smtp_connect_kwargs() -> SmtpConnectKwargs:
    """Параметры подключения к SMTP с проверкой согласованности порта и TLS.

    Расхождение ``SMTP_PORT``/``SMTP_SECURITY`` — самая частая причина
    «регистрация проходит, писем нет»: попытка STARTTLS на неявно-TLS порту
    (или наоборот) обрывается уже после того, как сервер принял соединение,
    и без явного предупреждения выглядит как «сервер молчит».
    """
    _, kwargs = _resolve_security()
    return kwargs


def _resolve_security() -> tuple[str, SmtpConnectKwargs]:
    """Согласованный с портом режим шифрования + параметры подключения.

    Предупреждение о рассогласовании пишется один раз на отправку, а сам
    режим шифрования возвращается вместе с kwargs — иначе лог и фактически
    использованный транспорт могли бы разойтись.
    """
    security = settings.smtp_security.lower()
    port = settings.smtp_port

    expected = KNOWN_PORT_SECURITY.get(port)
    if expected is not None and expected != security:
        logger.warning(
            "SMTP_PORT=%s не соответствует SMTP_SECURITY=%s — для этого порта "
            "ожидается %s (465=ssl, 587/2525=starttls). Используется %s.",
            port,
            security,
            expected,
            expected,
        )
        security = expected
    elif expected is None and security != "none":
        logger.warning(
            "Порт SMTP=%s нестандартный: убедитесь, что SMTP_SECURITY=%s "
            "соответствует серверу (иначе отправка молча не удастся).",
            port,
            security,
        )

    return security, {
        "hostname": settings.smtp_host,
        "port": port,
        "username": settings.smtp_user or None,
        "password": settings.smtp_password or None,
        "start_tls": security == "starttls",
        "use_tls": security == "ssl",
    }


def generate_otp_code() -> str:
    """Криптостойкий 6-значный числовой код (``secrets``, не ``random``)."""
    return f"{secrets.randbelow(10**OTP_LENGTH):0{OTP_LENGTH}d}"


def hash_otp_code(email: str, code: str) -> str:
    """HMAC-SHA256-хэш кода, связанный с email (hex)."""
    key = settings.jwt_secret.encode("utf-8")
    message = f"{email.strip().lower()}:{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_otp_code(email: str, code: str, code_hash: str) -> bool:
    """Сравнение кода с хэшем за постоянное время (защита от timing-атак)."""
    return hmac.compare_digest(hash_otp_code(email, code), code_hash)


def build_verification_email(email: str, code: str) -> tuple[str, str, str]:
    """Собрать (subject, text, html) письма с кодом верификации."""
    ttl = settings.otp_ttl_minutes
    subject = f"Код подтверждения регистрации — {code}"
    text = (
        f"Здравствуйте!\n\n"
        f"Код подтверждения вашей регистрации в Career-Assistant-AI:\n\n"
        f"    {code}\n\n"
        f"Код состоит из 6 цифр и действует {ttl} минут. "
        f"Вводите его на экране подтверждения регистрации.\n\n"
        f"Если вы не регистрировались — просто проигнорируйте письмо, "
        f"аккаунт не будет активирован.\n\n"
        f"С уважением,\n"
        f"Команда Career-Assistant-AI\n"
    )
    html = f"""<!DOCTYPE html>
<html lang="ru">
  <body style="margin:0;background:#0b1220;font-family:Segoe UI,Arial,sans-serif;color:#e2e8f0;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center" style="padding:32px 16px;">
        <table role="presentation" width="520" cellpadding="0" cellspacing="0"
               style="max-width:520px;background:#111a2e;border:1px solid #1e293b;border-radius:16px;padding:32px;">
          <tr><td style="font-size:18px;font-weight:700;color:#a5b4fc;">
            Career-Assistant-AI
          </td></tr>
          <tr><td style="padding-top:16px;font-size:15px;line-height:1.6;">
            Здравствуйте!<br />
            Для подтверждения email введите этот код:
          </td></tr>
          <tr><td align="center" style="padding:24px 0;">
            <div style="display:inline-block;font-size:34px;letter-spacing:10px;font-weight:700;
                        color:#f8fafc;background:#1e293b;border:1px solid #334155;
                        border-radius:12px;padding:14px 10px 14px 20px;">
              {code}
            </div>
          </td></tr>
          <tr><td style="font-size:14px;color:#94a3b8;line-height:1.6;">
            Код действует <strong style="color:#e2e8f0;">{ttl} минут</strong>.
            Не сообщите его никому — по нему можно активировать аккаунт.<br />
            Если вы не регистрировались, просто проигнорируйте письмо.
          </td></tr>
          <tr><td style="padding-top:24px;font-size:12px;color:#64748b;">
            Письмо отправлено на {email}. Команда Career-Assistant-AI.
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""
    return subject, text, html


async def send_verification_email(email: str, code: str) -> bool:
    """Отправить письмо с OTP-кодом. ``True`` — письмо ушло.

    ``False`` означает «письмо не доставлено»: SMTP не настроен, ``aiosmtplib``
    не установлен либо соединение/отправка упали. Исключения наружу не
    поднимаются — фоновая задача не должна ронять ответ API, — но причина
    всегда попадает в лог с полным traceback, а вызывающий код решает, что
    с этим делать: в development он печатает код в лог, в production
    возвращает 503 SMTP_UNAVAILABLE вместо ложного «код отправлен»
    (docs/03 §2).
    """
    if not settings.smtp_configured:
        # Development без SMTP: письмо уйти не может, но локальная разработка
        # не должна вставать колом — код печатаем в лог явно.
        logger.warning(
            "SMTP не настроен (SMTP_HOST пуст) — OTP-код для %s: %s "
            "(письмо не отправлено)",
            email,
            code,
        )
        return False

    subject, text, html = build_verification_email(email, code)

    message = EmailMessage()
    message["From"] = settings.emails_from
    message["To"] = email
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    try:
        # Импорт внутри try: отсутствие aiosmtplib — такой же отказ доставки,
        # как недоступный сервер, и он обязан логироваться, а не ронять
        # процесс на NameError/ImportError при первой же отправке.
        import aiosmtplib  # noqa: PLC0415 — импорт только при реальной отправке
    except ImportError as exc:
        logger.exception(
            "aiosmtplib не установлен — письмо с OTP-кодом на %s не отправлено (%s). "
            "Установите aiosmtplib (см. backend/pyproject.toml)",
            email,
            exc,
        )
        return False

    security, connect_kwargs = _resolve_security()
    try:
        await aiosmtplib.send(
            message,
            **connect_kwargs,
            timeout=SMTP_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — внешний сервис
        # Любой отказ SMTP (аутентификация, TLS, таймаут, отказ получателя)
        # одинаково означает «письмо не доставлено»: логируем с traceback,
        # чтобы причина (а не только факт) была видна в логах и Sentry.
        logger.exception(
            "Не удалось отправить письмо с OTP-кодом на %s (SMTP %s:%s, %s)",
            email,
            settings.smtp_host,
            settings.smtp_port,
            security,
        )
        return False
    logger.info("OTP-код отправлен на %s (SMTP %s:%s)", email, settings.smtp_host, settings.smtp_port)
    return True
