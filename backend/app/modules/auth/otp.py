"""Auth Module — OTP-коды подтверждения email (хранение и проверка).

Жизненный цикл (ТЗ «Email OTP verification»):

    issue  — register / resend-code: сгенерировать 6-значный код, сохранить
             HMAC-SHA256-хэш в ``email_otps`` с TTL ``otp_ttl_minutes`` (10 мин)
             и обнулить счётчик попыток. Одна активная запись на email.
    verify — verify-email: сверить код, проверить срок и лимит попыток
             (``otp_max_attempts`` = 5). Успех → запись удаляется, ошибка →
             счётчик попыток растёт; на 5-й неверной попытке код блокируется.
    resend — rate-limit 1 запрос / 60 сек на email считается по ``created_at``
             (момент последней отправки) — без Redis, единый источник истины.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.mail import generate_otp_code, hash_otp_code, verify_otp_code
from app.db.models import EmailOtp

__all__ = [
    "OtpError",
    "issue_otp",
    "get_otp",
    "verify_otp",
    "resend_retry_after",
]


class OtpError(Exception):
    """Ошибка проверки OTP-кода (→ 400 с error_code в API)."""

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(UTC)


async def get_otp(db: AsyncSession, email: str) -> EmailOtp | None:
    """Активная OTP-запись по email (или None)."""
    return await db.scalar(select(EmailOtp).where(EmailOtp.email == email))


async def issue_otp(db: AsyncSession, email: str) -> str:
    """Создать/перезаписать OTP-код для email. Возвращает код (в БД — хэш).

    Старая запись удаляется: единственный активный код на email, счётчик
    попыток и TTL сбрасываются.
    """
    email = email.strip().lower()
    await db.execute(delete(EmailOtp).where(EmailOtp.email == email))

    code = generate_otp_code()
    now = _now()
    db.add(
        EmailOtp(
            email=email,
            otp_code_hash=hash_otp_code(email, code),
            expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
            attempts_count=0,
            created_at=now,
        )
    )
    await db.flush()
    return code


def resend_retry_after(row: EmailOtp | None, now: datetime | None = None) -> int:
    """Секунды до разрешённого resend (0 — можно отправлять сразу)."""
    if row is None or row.created_at is None:
        return 0
    now = now or _now()
    elapsed = (now - row.created_at).total_seconds()
    return max(0, int(settings.otp_resend_interval_seconds - elapsed))


async def verify_otp(db: AsyncSession, email: str, code: str) -> None:
    """Проверить код. ``None`` — успех; иначе ``OtpError``.

    Raises:
        OtpError: OTP_NOT_FOUND — код не запрашивался/уже использован;
                  OTP_EXPIRED   — прошло ``otp_ttl_minutes`` (запись удаляется);
                  OTP_LOCKED    — исчерпаны попытки (5 неверных вводов);
                  OTP_INVALID   — код не совпал (попытка засчитана).
    """
    email = email.strip().lower()
    row = await get_otp(db, email)
    if row is None:
        raise OtpError("OTP_NOT_FOUND", "Код не запрашивался или уже использован")

    now = _now()
    if row.expires_at <= now:
        await db.execute(delete(EmailOtp).where(EmailOtp.id == row.id))
        raise OtpError("OTP_EXPIRED", "Срок действия кода истёк, запросите новый")

    if row.attempts_count >= settings.otp_max_attempts:
        raise OtpError(
            "OTP_LOCKED",
            f"Превышен лимит попыток ({settings.otp_max_attempts}), запросите новый код",
        )

    if not verify_otp_code(email, code, row.otp_code_hash):
        row.attempts_count += 1
        await db.flush()
        if row.attempts_count >= settings.otp_max_attempts:
            raise OtpError(
                "OTP_LOCKED",
                f"Превышен лимит попыток ({settings.otp_max_attempts}), запросите новый код",
            )
        remaining = settings.otp_max_attempts - row.attempts_count
        raise OtpError("OTP_INVALID", f"Неверный код, осталось попыток: {remaining}")

    # Успех: одноразовый код больше не нужен.
    await db.execute(delete(EmailOtp).where(EmailOtp.id == row.id))
