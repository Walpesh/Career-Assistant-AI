"""Vacancy Storage Module — точка подключения роутера (docs/03_API_CONTRACTS.md §4).

Контракт:
    GET    /vacancies                     — список с фильтрами:
             status (raw/analyzed/letter_ready/applied/error),
             source (auto/group/manual), search, min_match_score, page, size
    GET    /vacancies/{vacancy_id}       — детальная информация
    DELETE /vacancies/{vacancy_id}       — удалить вакансию
    PATCH  /vacancies/{vacancy_id}/status — сменить статус (например, applied)

Особенности: уникальный ключ (user_id, hh_vacancy_id); правила дедупликации и
обновления — docs/04_PARSING_RULES.md §8; статусы — docs/02_DATABASE.md §5.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/vacancies", tags=["vacancies"])
