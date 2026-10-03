/* ============================================================
   Глобальный баннер деградации сервиса.

   Источники сигналов:
     - GET /health/ready (docs/01 §9): ollama недоступна → LLM-функции
       (анализ, письма, сжатие резюме) не работают, но парсинг жив;
     - ws:status (docs/03 §8): потеря реал-тайм-канала → прогресс
       задач не обновляется;
     - ошибки REST с error_code LLM_UNAVAILABLE / QUEUE_UNAVAILABLE.

   Баннер не блокирует интерфейс: приложение продолжает работать,
   недоступные операции помечаются текстом, а не модальным окном.
   ============================================================ */

import { CONFIG } from '../config.js';
import { on } from './bus.js';

/** Интервал опроса /health/ready, мс. */
const HEALTH_POLL_MS = 60000;

/** Порог «долго нет связи», после которого предупреждение усиливается, мс. */
const WS_STALE_MS = 45000;

/** error_code бэкенда → уровень деградации (docs/03 §1). */
const ERROR_HINTS = {
  LLM_UNAVAILABLE: { level: 'warning', title: 'LLM недоступна' },
  OLLAMA_UNAVAILABLE: { level: 'warning', title: 'LLM недоступна' },
  QUEUE_UNAVAILABLE: { level: 'error', title: 'Очередь задач недоступна' }
};

const LEVEL_STYLES = {
  info: { wrap: 'border-sky-500/40 bg-sky-500/10 text-sky-100', dot: 'bg-sky-400' },
  warning: { wrap: 'border-amber-500/40 bg-amber-500/10 text-amber-100', dot: 'bg-amber-400' },
  error: { wrap: 'border-rose-500/40 bg-rose-500/10 text-rose-100', dot: 'bg-rose-400' }
};

let root = null;
let pollTimer = null;
let wsDisconnectedSince = 0;

/** Активные проблемы: Map<key, {level, title, detail}>. */
const issues = new Map();

/**
 * Инициализация баннера. Вызывается один раз из main.js.
 * В демо-режиме (?demo=1) ничего не опрашивает — данные фиктивные,
 * а health-эндпоинт может быть недоступен у статического сервера.
 */
export function initDegradationBanner() {
  root = document.getElementById('service-banner');
  if (!root) return;

  // Reал-тайм-канал потерян / восстановлен.
  on('ws:status', (status) => {
    if (status === 'disconnected') {
      wsDisconnectedSince = Date.now();
      setIssue('ws', {
        level: 'info',
        title: 'Нет связи с сервером в реальном времени',
        detail: 'Прогресс задач может не обновляться. Переподключение выполняется автоматически.'
      });
    } else {
      wsDisconnectedSince = 0;
      clearIssue('ws');
    }
  });

  // Ошибки REST: backend сообщает о недоступности LLM или очереди.
  on('api:error', (payload = {}) => {
    const hint = ERROR_HINTS[payload.errorCode];
    if (!hint) return;
    setIssue(`api:${hint.title}`, { ...hint, detail: payload.message || '' });
  });

  // Успешный ответ API снимает предположение о недоступности бэкенда.
  on('api:healthy', () => clearHealthIssues());

  if (CONFIG.DEMO) return;

  checkHealth();
  pollTimer = setInterval(checkHealth, HEALTH_POLL_MS);

  // При возврате на вкладку проверяем здоровье: после сна оно могло восстановиться.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    checkHealth();
    if (wsDisconnectedSince && Date.now() - wsDisconnectedSince > WS_STALE_MS) {
      setIssue('ws', {
        level: 'warning',
        title: 'Соединение потеряно',
        detail: 'Обновления в реальном времени не приходят. Переподключение выполняется автоматически.'
      });
    }
  });
}

/** Остановить опрос (логаут, смена пользователя). */
export function stopDegradationBanner() {
  clearInterval(pollTimer);
  pollTimer = null;
  clearHealthIssues();
}

/* ---------- Health-опрос ---------- */

/**
 * URL readiness: API_BASE оканчивается на /api/v1, а /health живёт в корне
 * (docs/03 §9). Собираем адрес из origin, а не «../../», чтобы не сломаться
 * при нестандартном префиксе API (?api=<url>).
 */
function healthUrl() {
  const base = new URL(CONFIG.API_BASE, window.location.origin);
  const prefix = String(CONFIG.API_BASE).replace(base.origin, '').replace(/\/+$/, '');
  const root = prefix.replace(/\/api\/v\d+$/, '');
  return `${base.origin}${root}/health/ready`;
}

async function checkHealth() {
  try {
    const response = await fetch(healthUrl(), {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      credentials: 'include'
    });
    // 503 — сервис жив, но не готов: тело всё равно полезно.
    const report = await response.json().catch(() => null);
    applyHealthReport(report);
  } catch {
    // Health недоступен целиком — скорее всего, нет связи с API.
    setIssue('health:unreachable', {
      level: 'error',
      title: 'Сервер недоступен',
      detail: 'Проверьте соединение и адрес API. Данные не отправляются и не принимаются.'
    });
  }
}

function applyHealthReport(report) {
  const checks = report?.checks;
  if (!checks) {
    // Неизвестный формат ответа — не считаем это деградацией.
    clearHealthIssues();
    return;
  }

  // LLM (Ollama) недоступна: парсинг работает, анализ и письма — нет.
  if (checks.ollama?.status === 'down') {
    setIssue('health:ollama', {
      level: 'warning',
      title: 'LLM недоступна — анализ и письма приостановлены',
      detail: 'Парсинг вакансий продолжает работать. Задачи анализа и генерации писем дождутся восстановления LLM-сервиса.'
    });
  } else {
    clearIssue('health:ollama');
  }

  // Очередь (Redis) недоступна: задачи нельзя ни поставить, ни выполнить.
  if (checks.redis?.status === 'down') {
    setIssue('health:redis', {
      level: 'error',
      title: 'Очередь задач недоступна',
      detail: 'Новые задачи не запустятся. Проверяется Redis (ARQ) — см. docs/01_ARCHITECTURE.md §6.'
    });
  } else {
    clearIssue('health:redis');
  }

  // БД недоступна — приложение фактически нерабочее.
  if (checks.postgres?.status === 'down') {
    setIssue('health:postgres', {
      level: 'error',
      title: 'База данных недоступна',
      detail: 'Профиль, вакансии и задачи временно недоступны.'
    });
  } else {
    clearIssue('health:postgres');
  }

  clearIssue('health:unreachable');
}

/* ---------- Рендер ---------- */

function setIssue(key, issue) {
  const previous = issues.get(key);
  issues.set(key, issue);
  // Не перерисовываем баннер, если текст не изменился: иначе он мигает
  // и повторно озвучивается скринридером на каждом опросе.
  if (previous && previous.level === issue.level && previous.title === issue.title
      && previous.detail === issue.detail) {
    return;
  }
  render();
}

function clearIssue(key) {
  if (!issues.delete(key)) return;
  render();
}

function clearHealthIssues() {
  const keys = [...issues.keys()].filter((key) => key.startsWith('health:'));
  if (!keys.length) return;
  keys.forEach((key) => issues.delete(key));
  render();
}

function render() {
  if (!root) return;

  if (!issues.size) {
    root.innerHTML = '';
    root.classList.add('hidden');
    return;
  }

  // Приоритет: error → warning → info (вверху — самое важное).
  const order = { error: 0, warning: 1, info: 2 };
  const list = [...issues.values()].sort((a, b) => order[a.level] - order[b.level]);

  root.innerHTML = list
    .map((issue) => {
      const style = LEVEL_STYLES[issue.level] || LEVEL_STYLES.info;
      const detail = issue.detail
        ? `<span class="hidden sm:inline text-slate-300/90"> — ${escapeText(issue.detail)}</span>`
        : '';
      return `<div class="border-b ${style.wrap} px-4 py-2.5 text-xs leading-relaxed shadow-lg backdrop-blur">
        <div class="mx-auto flex max-w-5xl items-start gap-2.5">
          <span class="mt-1.5 h-2 w-2 shrink-0 rounded-full ${style.dot}" aria-hidden="true"></span>
          <p class="min-w-0">
            <span class="font-semibold">${escapeText(issue.title)}</span>${detail}
          </p>
        </div>
      </div>`;
    })
    .join('');

  root.classList.remove('hidden');
}

/** Экранирование: текст приходит из сообщений backend и не должен ломать разметку. */
function escapeText(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

