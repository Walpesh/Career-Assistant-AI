"""Billing Module — FastAPI-зависимости контроля квот (docs/03 §11).

Квота проверяется **до** постановки задачи в очередь, а не после: если
сначала создать `tasks`-запись, отправить её в Redis и только потом узнать,
что лимит исчерпан, пользователь получит «висящую» задачу, которую
воркер тут же упадёт с 429. Поэтому проверка стоит в зависимости FastAPI
раньше эндпоинта.

:func:`require_quota` — декларативная зависимость:

    @router.post("/auto", dependencies=[Depends(require_quota(QuotaKind.PARSE))])

Она возвращает callable с ``kind``, поэтому FastAPI видит её как обычную
зависимость, а код эндпоинта остаётся чистым.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.db.session import get_db
from app.modules.auth.deps import get_current_user
from app.modules.billing.service import consume_quota

__all__ = [
    "QuotaGuard",
    "quota_consumed",
    "require_quota",
]


async def quota_consumed(db: AsyncSession, user: User, kind: str, amount: int = 1) -> None:
    """Начислить квоту пользователя; лимит исчерпан → ``429 QUOTA_EXCEEDED``.

    Единственная точка, где «разрешение на операцию» превращается в
    записанный расход. Вызывается из сервисного слоя модулей, которые
    ставят задачи в очередь (parsing, analysis_letter, user_profile).
    """
    await consume_quota(db, user.id, kind, amount)


#: Тип зависимости FastAPI: функция → coroutine, разрешается до эндпоинта.
QuotaGuard = Callable[..., Awaitable[None]]


def require_quota(kind: str, amount: int = 1) -> QuotaGuard:
    """Создать зависимость FastAPI, начисляющую квоту ``kind``.

    Args:
        kind: вид квоты (``QuotaKind``).
        amount: величина расхода за одно срабатывание зависимости.

    Returns:
        Зависимость с теми же аргументами, что и ``get_current_user`` и
        ``get_db``, поэтому её можно повесить в ``dependencies=[...]``.
    """

    async def _guard(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        await quota_consumed(db, user, kind, amount)

    # Имя помогает в документации OpenAPI и трассировке.
    _guard.__name__ = f"quota_{kind}"
    return _guard
