"""Отправка email (Auth Module): OTP-коды подтверждения регистрации.

Реализация на ``aiosmtplib``: соединение асинхронное, поэтому отправка из
``BackgroundTasks`` не блокирует event-loop API. Письмо содержит текстовую и
HTML-часть (multipart/alternative) — почтовые клиенты сами выбирают версию.

Хранение кода: в БД уходит только HMAC-SHA256-хэш (см. EmailOtp.otp_code_hash),
сам код нигде не пишется. Ключ подписи — ``JWT_SECRET``: без него хэш не
перебирается офлайн, даже украв дамп БД.

Dev-режим: ``SMTP_HOST`` пуст → письмо не отправляется, код логируется
предупреждением (локальная разработка без SMTP-сервера).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "generate_otp_code",
    "hash_otp_code",
    "verify_otp_code",
    "send_verification_email",
    "build_verification_email",
]

#: Длина OTP-кода (цифры) — строго 6 по ТЗ.
OTP_LENGTH = 6


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
    """Отправить письмо с OTP-кодом. True — письмо ушло (или dev-режим).

    Ошибки SMTP не поднимают исключение: фоновая задача не должна ронять
    ответ API — проблема логируется, а вызывающий код (resend) позволяет
    пользователю запросить новый код.
    """
    if not settings.smtp_host:
        # Development без SMTP: код доступен только в логе сервера.
        logger.warning("SMTP не настроен — код для %s: %s", email, code)
        return False

    subject, text, html = build_verification_email(email, code)

    message = EmailMessage()
    message["From"] = settings.emails_from
    message["To"] = email
    message["Subject"] = subject
    message.set_content(text)
    message.add_alternative(html, subtype="html")

    import aiosmtplib  # noqa: PLC0415 — импорт только при реальной отправке

    security = settings.smtp_security.lower()
    try:
        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=security == "starttls",
            use_tls=security == "ssl",
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001 — внешний сервис
        logger.exception("Не удалось отправить письмо с OTP-кодом на %s", email)
        return False
    logger.info("OTP-код отправлен на %s", email)
    return True
