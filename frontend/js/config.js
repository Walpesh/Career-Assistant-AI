/* ============================================================
   Конфигурация клиента Career-Assistant-AI.
   API base / WebSocket URL / демо-режим.
   Переопределения: ?api=<url>, ?demo=1, ?demo=0
   ============================================================ */

const params = new URLSearchParams(window.location.search);

function trimTrailingSlash(value) {
  return String(value).replace(/\/+$/, '');
}

function resolveApiBase() {
  const fromQuery = params.get('api');
  if (fromQuery) {
    const normalized = trimTrailingSlash(fromQuery);
    localStorage.setItem('ca:api_base', normalized);
    return normalized;
  }

  const stored = localStorage.getItem('ca:api_base');
  if (stored) return stored;

  if (window.APP_CONFIG && window.APP_CONFIG.apiBase) {
    return trimTrailingSlash(window.APP_CONFIG.apiBase);
  }

  const { protocol, hostname, port } = window.location;

  // UI открыт статически (например, :5500) — backend по умолчанию на :8000.
  if (protocol.startsWith('http') && port && port !== '8000') {
    return `${protocol}//${hostname}:8000/api/v1`;
  }
  if (protocol.startsWith('http')) {
    return '/api/v1';
  }
  return 'http://localhost:8000/api/v1';
}

// Демо-режим живёт в sessionStorage: ?demo=1 включает, ?demo=0 выключает.
const demoParam = params.get('demo');
if (demoParam === '1') sessionStorage.setItem('ca:demo', '1');
if (demoParam === '0') sessionStorage.removeItem('ca:demo');

export const CONFIG = {
  API_BASE: resolveApiBase(),
  DEMO: sessionStorage.getItem('ca:demo') === '1',

  /** Максимальная задержка реконнекта WebSocket (мс). */
  RECONNECT_MAX_DELAY_MS: 15000,
  /** Максимум записей в реал-тайм журнале. */
  LOG_LIMIT: 400,
  /** Порог матчинга по умолчанию (совпадает с DEFAULT user_profiles.match_threshold). */
  DEFAULT_THRESHOLD: 70
};

/** URL WebSocket: ws(s)://host/<api-base>/ws?token=... (docs/03_API_CONTRACTS.md §8). */
export function buildWsUrl(token) {
  const base = new URL(CONFIG.API_BASE, window.location.origin);
  const protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  const path = trimTrailingSlash(base.pathname);
  return `${protocol}//${base.host}${path}/ws?token=${encodeURIComponent(token)}`;
}
