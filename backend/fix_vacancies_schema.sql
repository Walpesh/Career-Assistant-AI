-- fix_vacancies_schema.sql
-- Schema-sync: дополняет БД объектами, которые уже есть в ORM (models.py).
-- PostgreSQL >= 12 (сервер использует gen_random_uuid() => PG >= 13).
-- Выполняется в одной транзакции.

BEGIN;

-- 1) external_id: хранимое сгенерированное поле = hh_vacancy_id
ALTER TABLE vacancies
    ADD COLUMN external_id VARCHAR(32) NOT NULL
    GENERATED ALWAYS AS (hh_vacancy_id) STORED;

-- 2) составной уникальный индекс для мульти-источникового парсинга
CREATE UNIQUE INDEX IF NOT EXISTS uq_vacancies_user_source_external
    ON vacancies (user_id, source, external_id);

-- 3) CHECK-ограничение source с учётом 'hh'
ALTER TABLE vacancies
    DROP CONSTRAINT ck_vacancies_source,
    ADD CONSTRAINT ck_vacancies_source
    CHECK (source IN ('hh', 'auto', 'group', 'manual'));

COMMIT;
