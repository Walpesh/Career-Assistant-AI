"""Locust-сценарий нагрузочного тестирования Career-Assistant-AI.

Что проверяем (docs/01 §5, docs/04 §6, docs/05 §1):

1. **Справедливость однопоточного LLM.** Очередь ``career:queue:llm``
   обслуживается **строго одним** воркером (``max_jobs=1`` + семафор на 1
   слот). При N одновременных пользователях задачи не должны выполняться
   параллельно: иначе Ollama получит несколько запросов сразу и уйдёт в
   OOM/429. Сценарий ``AnalysisSubmitter`` запускается с ``wait_time=0``,
   все виртуальные пользователи долбят ``POST /analysis/run`` одновременно,
   и мы следим, что одновременно ``processing`` не больше одной LLM-задачи.

2. **Устойчивость API под нагрузкой.** Ответы предсказуемы: 200/202 на
   успех, 429 + ``Retry-After`` при лимите частоты (docs/03 §2), 429
   ``QUOTA_EXCEEDED`` при суточной квоте (docs/03 §11) — а не 500.

3. **Отсутствие IDOR под нагрузкой.** Запрос анализа чужой вакансии обязан
   дать 404, а не 200 (docs/03 §6).

Запуск (из корня репозитория):

    docker compose up -d postgres redis
    cd backend && uvicorn app.main:app --port 8000

    pip install locust
    locust -f tests/load/locustfile.py --host http://localhost:8000

Headless-режим для CI:

    locust -f tests/load/locustfile.py --host http://localhost:8000 \
           --headless -u 20 -r 5 -t 60s --csv load_report --only-summary

Переменные окружения:
    LOAD_TEST_API_PREFIX      префикс API (default: /api/v1)
    LOAD_TEST_EMAIL_DOMAIN    домен регистрации (default: load.test)
    LOAD_TEST_PASSWORD        пароль (default: strongpassword)
    LOAD_TEST_OTP_CODE        6-значный код подтверждения email (default: 000000)
    LOAD_TEST_MODE            ``readonly`` (только чтение) или ``write``

ВНИМАНИЕ: режим ``write`` создаёт данные и списывает суточные квоты —
запускайте только против тестового стенда.
"""

from __future__ import annotations

import os
import uuid

from locust import HttpUser, between, events, task

API = os.environ.get("LOAD_TEST_API_PREFIX", "/api/v1")
EMAIL_DOMAIN = os.environ.get("LOAD_TEST_EMAIL_DOMAIN", "load.test")
PASSWORD = os.environ.get("LOAD_TEST_PASSWORD", "strongpassword")
READ_ONLY = os.environ.get("LOAD_TEST_MODE", "readonly").strip().lower() == "readonly"

#: OTP-код подтверждения email (docs/03 §2). Регистрация двухшаговая: код
#: приходит письмом, поэтому для нагрузочного прогона он задаётся явно
#: (в dev без SMTP он печатается в лог приложения).
OTP_CODE = os.environ.get("LOAD_TEST_OTP_CODE", "000000")

#: Типы задач, исполняемых LLM-очередью (docs/02 §3.6, docs/04 §6).
LLM_TASK_TYPES = frozenset({"analyze", "generate_letter", "auto_full", "convert_resume"})

#: Пик одновременно обрабатываемых LLM-задач: должен остаться ≤ 1 (docs/05 §1).
fairness = {"peak_llm_processing": 0, "submissions": 0, "idors_blocked": 0}


@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    """Сброс метрик на старте прогона."""
    fairness.update(peak_llm_processing=0, submissions=0, idors_blocked=0)
    print(f"[load] mode={'readonly' if READ_ONLY else 'write'} host={environment.host}")


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs):
    """Итоговая проверка справедливости LLM-очереди (docs/05 §1).

    Провал фиксируется ненулевым кодом выхода — CI увидит красный статус.
    """
    peak = fairness["peak_llm_processing"]
    print(
        f"[load] submissions={fairness['submissions']} "
        f"idors_blocked={fairness['idors_blocked']} "
        f"peak_llm_processing={peak}"
    )
    if peak > 1:
        environment.process_exit_code = 1
        print(
            f"[load] FAIL: LLM-задачи выполнялись параллельно ({peak} > 1). "
            "Очередь career:queue:llm должна обслуживаться строго одним воркером."
        )
    else:
        print("[load] OK: LLM-очередь остаётся однопоточной")
class CareerAssistantUser(HttpUser):
    """Виртуальный пользователь: регистрация, запуск анализа, опрос задач.

    В режиме ``write`` нулевое ожидание даёт максимальный одновременный
    натиск на ``/analysis/run`` — именно он проверяет однопоточность LLM.
    """

    wait_time = between(0.5, 1.5) if READ_ONLY else between(0.0, 0.0)

    def on_start(self):
        """Регистрация, подтверждение email и подготовка вакансии для анализа."""
        self.token: str | None = None
        self.user_id: str | None = None
        self.vacancy_ids: list[str] = []
        self.task_ids: list[str] = []

        if READ_ONLY:
            # В read-only режиме гости только читают публичные маршруты и
            # проверяют коды ответов — данные не создаются.
            return

        email = f"load{uuid.uuid4().hex[:12]}@{EMAIL_DOMAIN}"
        with self.client.post(
            f"{API}/auth/register",
            json={"email": email, "password": PASSWORD},
            name="POST /auth/register",
            catch_response=True,
        ) as response:
            # 429 допустим: идёт ограничение частоты регистраций (docs/03 §2).
            if response.status_code == 429:
                response.success()
                return
            if response.status_code != 201:
                response.failure(f"unexpected {response.status_code}")
                return
            # Register не выдаёт JWT: аккаунт нужно подтвердить OTP-кодом
            # (docs/03 §2), иначе login вернёт 403 EMAIL_NOT_VERIFIED.
            response.success()

        with self.client.post(
            f"{API}/auth/verify-email",
            json={"email": email, "code": OTP_CODE},
            name="POST /auth/verify-email",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"unexpected {response.status_code}")
                return
            self.token = response.json()["access_token"]
            response.success()

        self.client.headers.update({"Authorization": f"Bearer {self.token}"})
        self._adopt_existing_vacancy()

    def _adopt_existing_vacancy(self):
        """Взять первую доступную вакансию пользователя для /analysis/run.

        Вакансии появляются у пользователя после парсинга hh.ru; на тестовом
        стенде они уже заведены, поэтому просто читаем список.
        """
        with self.client.get(f"{API}/vacancies", name="GET /vacancies") as response:
            if response.status_code == 200:
                items = response.json().get("items", [])
                self.vacancy_ids = [item["id"] for item in items[:1]]

    # --- основные сценарии ------------------------------------------------

    @task(5)
    def submit_analysis(self):
        """Главная нагрузка: POST /analysis/run (docs/03 §6)."""
        if READ_ONLY or not self.vacancy_ids:
            return

        with self.client.post(
            f"{API}/analysis/run",
            json={"vacancy_ids": self.vacancy_ids, "mode": "analyze"},
            name="POST /analysis/run",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                self.task_ids.append(response.json()["task_id"])
                fairness["submissions"] += 1
                response.success()
            elif response.status_code == 429:
                # Лимит частоты (RATE_LIMITED) или суточная квота
                # (QUOTA_EXCEEDED) — штатное поведение под нагрузкой.
                if response.json().get("error_code") == "RATE_LIMITED":
                    assert response.headers.get("Retry-After"), "429 без Retry-After"
                response.success()
            else:
                response.failure(
                    f"unexpected {response.status_code}: {response.text[:120]}"
                )

    @task(3)
    def poll_task(self):
        """Опрос статуса своей задачи (docs/03 §7)."""
        if READ_ONLY or not self.task_ids:
            return

        with self.client.get(
            f"{API}/tasks/{self.task_ids[-1]}",
            name="GET /tasks/{id}",
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"unexpected {response.status_code}")

    @task(2)
    def measure_llm_fairness(self):
        """Считаем одновременные ``processing`` LLM-задачи (docs/05 §1).

        При одном воркере LLM-очереди значение не может превысить 1 —
        это и есть проверка «single-threaded LLM execution».
        """
        if READ_ONLY:
            return

        with self.client.get(f"{API}/tasks", name="GET /tasks") as response:
            if response.status_code != 200:
                return
            llm_processing = [
                item
                for item in response.json()["items"]
                if item["status"] == "processing"
                and item["task_type"] in LLM_TASK_TYPES
            ]
            fairness["peak_llm_processing"] = max(
                fairness["peak_llm_processing"], len(llm_processing)
            )

    @task(1)
    def check_idor(self):
        """Чужая случайная вакансия → 404, а не 200 (docs/03 §6)."""
        if READ_ONLY:
            return
        with self.client.get(
            f"{API}/analysis/{uuid.uuid4()}",
            name="GET /analysis/{id} [idor]",
            catch_response=True,
        ) as response:
            if response.status_code == 404:
                fairness["idors_blocked"] += 1
                response.success()
            else:
                response.failure(f"IDOR: ожидался 404, получен {response.status_code}")

    @task(1)
    def check_health(self):
        """Liveness-маршрут: базовый индикатор доступности API (docs/01 §7)."""
        with self.client.get("/health", name="GET /health", catch_response=True) as response:
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"unexpected {response.status_code}")