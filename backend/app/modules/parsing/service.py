"""Parsing Orchestrator — исполнение трёх режимов сбора (docs/04_PARSING_RULES.md §4).

Логика режимов — строго по спецификации:

    §4.1 Автопоиск         1) URL поиска из ключевых слов и фильтров;
                          2) обход страниц результатов с лимитом max_pages;
                          3) переход на детальную страницу каждой карточки;
                          4) извлечение шапки и описания;
                          5) дедупликация и сохранение в БД.
    §4.2 Групповой парсер 1) готовая ссылка от пользователя;
                          2) обход страниц пагинации с лимитом;
                          3) далее — как автопоиск.
    §4.3 Ручное добавление 1) прямая ссылка; 2) один запрос на карточку;
                          3) сохранение + опциональный запуск анализа.

Оркестратор работает поверх Proxy & Anti-Ban Module (fetch через AntiBanSession)
и Vacancy Storage Module (upsert_vacancy). Ошибки защиты hh.ru обрабатываются
по таблице docs/04 §5 и отдаются вызывающему anti_ban-исключениями.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.anti_ban import AntiBanSession, CaptchaDetected, RateLimitExceeded
from app.modules.anti_ban.session import FetchResponse
from app.modules.parsing.fetcher import build_page_fetcher
from app.modules.parsing.listing import parse_listing
from app.modules.parsing.urls import build_auto_search_url, build_page_url, validate_hh_search_url
from app.modules.vacancy_storage.parser import extract_vacancy_fields
from app.modules.vacancy_storage.service import delete_vacancy_by_hh_id, upsert_vacancy

__all__ = [
    "MAX_BLACKLIST_WORDS",
    "normalize_blacklist",
    "find_blacklist_matches",
    "vacancy_text",
    "is_blacklisted",
    "ParsingOutcome",
    "ParsingOrchestrator",
    "ProgressReporter",
    "NullProgress",
]


#: Ограничение на размер чёрного списка (аналогично лимитам остальных полей).
MAX_BLACKLIST_WORDS = 50


def normalize_blacklist(words: object) -> list[str]:
    """Нормализовать список слов чёрного списка.

    Правила: обрезка пробелов, отсечение пустых значений, дедупликация
    без учёта регистра и ограничение длины списка. Регистр и порядок
    исходного текста сохраняются — они используются в логе отсева.
    """
    if words is None:
        return []
    if isinstance(words, str):
        # Текст с переносами/запятыми — на случай ручного ввода.
        raw_items = words.replace(",", "\n").splitlines()
    elif isinstance(words, (list, tuple, set)):
        raw_items = list(words)
    else:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for raw in raw_items:
        if raw is None:
            continue
        word = str(raw).strip()
        if not word:
            continue
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(word)
        if len(result) >= MAX_BLACKLIST_WORDS:
            break
    return result


def vacancy_text(fields: dict) -> str:
    """Текст вакансии для поиска чёрных слов.

    Проверяются название, компания и описание — то есть всё, что реально
    видно пользователю в карточке (docs/04 §4.9).
    """
    parts = [
        fields.get("title"),
        fields.get("company_name"),
        fields.get("description_raw"),
    ]
    return "\n".join(str(part) for part in parts if part)


def find_blacklist_matches(text: str, blacklist: list[str]) -> list[str]:
    """Слова чёрного списка, встречающиеся в тексте (регистронезависимо)."""
    if not text or not blacklist:
        return []
    haystack = text.casefold()
    return [word for word in blacklist if word.casefold() in haystack]


def is_blacklisted(fields: dict, blacklist: list[str]) -> list[str]:
    """Совпавшие чёрные слова для полей вакансии; пусто — вакансия проходит."""
    return find_blacklist_matches(vacancy_text(fields), blacklist)

logger = logging.getLogger(__name__)

#: Статусы «вакансия удалена на hh.ru» (docs/04 §5 → vacancy.status = 'error').
_NOT_FOUND_STATUSES = (404, 410)


@dataclass
class ParsingOutcome:
    """Итог задачи парсинга (пишется в tasks.result, docs/02 §3.6)."""

    vacancy_ids: list[str] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    not_found: int = 0
    failed: int = 0
    blacklisted: int = 0
    pages_visited: int = 0

    def as_dict(self) -> dict:
        return {
            "vacancy_ids": self.vacancy_ids,
            "created": self.created,
            "updated": self.updated,
            "not_found": self.not_found,
            "failed": self.failed,
            "blacklisted": self.blacklisted,
            "pages_visited": self.pages_visited,
            "total": len(self.vacancy_ids),
        }


class ProgressReporter:
    """Обратный вызов прогресса: слот, который заполняет Queue Manager.

    Формат соответствует docs/04 §6 (current/total/stage/message) и событию
    task.progress из docs/03 §8.
    """

    async def __call__(
        self,
        current: int,
        total: int,
        stage: str,
        message: str,
    ) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError


class NullProgress(ProgressReporter):
    """Прогресс не требуется (использование оркестратора вне очереди)."""

    async def __call__(self, current: int, total: int, stage: str, message: str) -> None:
        return None


class ParsingOrchestrator:
    """Сбор вакансий в трёх режимах через AntiBanSession (docs/04 §2, §4)."""

    def __init__(self, session: AntiBanSession | None = None) -> None:
        # Сессия сама обеспечивает прогрев, паузы 4–8 с, ротацию IP и retry (§3, §5).
        self.http = session or AntiBanSession(fetcher=build_page_fetcher())

    # --- публичные режимы ----------------------------------------------------
    async def run_auto(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        keywords: list[str],
        employment_forms: list[str] | None = None,
        work_formats: list[str] | None = None,
        schedules: list[str] | None = None,
        max_pages: int = 5,
        progress: ProgressReporter | None = None,
        blacklist: list[str] | None = None,
    ) -> ParsingOutcome:
        """Автопоиск (docs/04 §4.1)."""
        base_url = build_auto_search_url(
            keywords=keywords,
            employment_forms=employment_forms,
            work_formats=work_formats,
            schedules=schedules,
        )
        return await self._collect_from_search(
            db,
            user_id=user_id,
            base_url=base_url,
            max_pages=max_pages,
            source="auto",
            progress=progress or NullProgress(),
            blacklist=blacklist,
        )

    async def run_group(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        search_url: str,
        max_pages: int = 5,
        progress: ProgressReporter | None = None,
        blacklist: list[str] | None = None,
    ) -> ParsingOutcome:
        """Групповой парсер по готовой ссылке (docs/04 §4.2)."""
        return await self._collect_from_search(
            db,
            user_id=user_id,
            base_url=validate_hh_search_url(search_url),
            max_pages=max_pages,
            source="group",
            progress=progress or NullProgress(),
            blacklist=blacklist,
        )

    async def run_manual(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        vacancy_url: str,
        hh_vacancy_id: str,
        progress: ProgressReporter | None = None,
        blacklist: list[str] | None = None,
    ) -> ParsingOutcome:
        """Ручное добавление одной вакансии (docs/04 §4.3 — один запрос)."""
        report = progress or NullProgress()
        await report(0, 1, "fetching_vacancy", "Загрузка карточки вакансии")
        response = await self.http.fetch(vacancy_url)
        outcome = await self._ingest(
            db,
            user_id=user_id,
            hh_vacancy_id=hh_vacancy_id,
            url=vacancy_url,
            source="manual",
            response=response,
            blacklist=blacklist,
        )
        await report(1, 1, "parsing_vacancy", "Карточка вакансии обработана")
        return outcome

    # --- общий обход страниц выдаи -----------------------------------------
    async def _collect_from_search(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        base_url: str,
        max_pages: int,
        source: str,
        progress: ProgressReporter,
        blacklist: list[str] | None = None,
    ) -> ParsingOutcome:
        """docs/04 §4.1 п.2 / §4.2 п.2: собрать id со страниц, затем обойти карточки."""
        outcome = ParsingOutcome()
        host = urlsplit(base_url).netloc or "hh.ru"
        pages_limit = max(1, max_pages)

        # Сначала собираем список карточек (пагинация с лимитом max_pages).
        for page_index in range(pages_limit):
            url = build_page_url(base_url, page_index)
            await progress(
                0, 0, "loading_list", f"Загрузка страницы {page_index + 1} из {pages_limit}"
            )
            response = await self.http.fetch(url)
            outcome.pages_visited += 1

            listing = parse_listing(response.text)
            for hh_id in listing.vacancy_ids:
                if hh_id not in outcome.vacancy_ids:  # дедупликация внутри выдачи
                    outcome.vacancy_ids.append(hh_id)

            if not listing.vacancy_ids or not listing.has_next_page:
                break  # выдача или пагинация закончились

        total = len(outcome.vacancy_ids)
        await progress(0, total, "parsing_vacancy", f"Найдено вакансий: {total}")

        # Затем детальные страницы каждой карточки (docs/04 §4.1 п.3–§4.5).
        for index, hh_id in enumerate(outcome.vacancy_ids, start=1):
            await progress(
                index - 1, total, "parsing_vacancy", f"Парсинг вакансии {index} из {total}"
            )
            vacancy_url = f"https://{host}/vacancy/{hh_id}"
            try:
                response = await self.http.fetch(vacancy_url)
                await self._ingest(
                    db,
                    user_id=user_id,
                    hh_vacancy_id=hh_id,
                    url=vacancy_url,
                    source=source,
                    response=response,
                    outcome=outcome,
                    blacklist=blacklist,
                )
            except (CaptchaDetected, RateLimitExceeded):
                raise  # docs/04 §5: капча/429 обрабатываются очередью, не глотаются
            except Exception as exc:  # noqa: BLE001 — одна вакансия не роняет задачу
                logger.warning("Не удалось разобрать вакансию %s: %s", hh_id, exc)
                outcome.failed += 1

        return outcome

    # --- сохранение одной вакансии -----------------------------------------
    async def _ingest(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        hh_vacancy_id: str,
        url: str,
        source: str,
        response: FetchResponse,
        outcome: ParsingOutcome | None = None,
        blacklist: list[str] | None = None,
    ) -> ParsingOutcome:
        """Разбор + дедупликация + сохранение (docs/04 §4.5, §7, §8).

        Чёрный список слов (docs/04 §4.9) проверяется ДО сохранения в БД:
        если слово из списка есть в содержании вакансии, вакансия не
        сохраняется, а уже сохранённая ранее — удаляется.
        """
        result = outcome if outcome is not None else ParsingOutcome()

        if response.status_code in _NOT_FOUND_STATUSES:
            # docs/04 §5: «Вакансия реально удалена → error с причиной not_found».
            await upsert_vacancy(
                db,
                user_id=user_id,
                hh_vacancy_id=hh_vacancy_id,
                url=url,
                source=source,
                fields={},
                ingest_status="error",
            )
            result.not_found += 1
            return result

        fields = extract_vacancy_fields(response.text)
        if not fields.get("title"):
            # Капча или изменившаяся вёрстка (docs/04 §5) — карточка не разобрана.
            result.failed += 1
            return result

        # docs/04 §4.9: чёрный список слов — отсев до записи в БД.
        if blacklist:
            matches = is_blacklisted(fields, blacklist)
            if matches:
                removed = await delete_vacancy_by_hh_id(
                    db,
                    user_id=user_id,
                    hh_vacancy_id=hh_vacancy_id,
                )
                result.blacklisted += 1
                logger.info(
                    "Вакансия %s отсечена чёрным списком (%s); удалена из БД: %s",
                    hh_vacancy_id,
                    ", ".join(matches),
                    "да" if removed else "нет",
                )
                return result

        _, created = await upsert_vacancy(
            db,
            user_id=user_id,
            hh_vacancy_id=hh_vacancy_id,
            url=url,
            source=source,
            fields=fields,
            ingest_status="raw",
        )
        if created:
            result.created += 1
        else:
            result.updated += 1
        return result
