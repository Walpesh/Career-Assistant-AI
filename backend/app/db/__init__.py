"""Слой доступа к данным: Base, ORM-модели и асинхронные сессии.

- app.db.base    — DeclarativeBase и общие миксины;
- app.db.models  — 6 таблиц из docs/02_DATABASE.md;
- app.db.session — async engine (SQLAlchemy 2.0 + asyncpg) и dependency get_db.
"""
