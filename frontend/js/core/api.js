/* ============================================================
   REST-клиент. Полностью соответствует docs/03_API_CONTRACTS.md:
   /auth, /profile, /vacancies, /parsing, /analysis, /letters, /tasks
   ============================================================ */

import { CONFIG } from '../config.js';
import { session } from './session.js';
import { emit } from './bus.js';

export class ApiError extends Error {
  constructor(status, message, errorCode) {
    super(message || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.errorCode = errorCode || null;
  }
}

function buildQuery(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    query.set(key, String(value));
  });
  const qs = query.toString();
  return qs ? `?${qs}` : '';
}

/** Один общий refresh на все параллельные 401 (docs/03 §2). */
let refreshPromise = null;

/**
 * Обновить access-токен через refresh-ротацию.
 * @returns {Promise<boolean>} true — новый токен получен и сохранён.
 */
function refreshAccessToken() {
  if (!refreshPromise) {
    // Refresh-токен уходит автоматически в HttpOnly cookie (credentials: include).
    refreshPromise = request('POST', '/auth/refresh', undefined, { skipAuth: true })
      .then((data) => {
        if (!data?.access_token) return false;
        session.setTokens({ access_token: data.access_token });
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshPromise = null;
      });
  }
  return refreshPromise;
}

async function request(method, path, body, options = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';

  const token = session.accessToken;
  if (token && !options.skipAuth) headers.Authorization = `Bearer ${token}`;

  let response;
  try {
    response = await fetch(`${CONFIG.API_BASE}${path}`, {
      method,
      headers,
      credentials: 'include',
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: options.signal
    });
  } catch (error) {
    if (error && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Нет соединения с сервером', 'NETWORK_ERROR');
  }

  if (response.status === 204) return null;

  const raw = await response.text();
  let data = null;
  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      data = { detail: raw };
    }
  }

  if (!response.ok) {
    // Единый формат ошибок: { detail, error_code } (docs/03 §1)
    if (response.status === 401 && !options.skipAuth) {
      // Access-токен истёк: пробуем refresh-ротацию и повторяем запрос один раз.
      // Раньше здесь был безусловный выход на экран входа — пользователя
      // выбрасывало из приложения каждые ACCESS_TOKEN_EXPIRE_MINUTES.
      if (!options._retried && (await refreshAccessToken())) {
        return request(method, path, body, { ...options, _retried: true });
      }
      session.clear();
      emit('auth:expired');
    }
    const error = new ApiError(response.status, data?.detail || response.statusText, data?.error_code);
    // Сигнал для баннера деградации: LLM_UNAVAILABLE / QUEUE_UNAVAILABLE и т.п.
    emit('api:error', {
      status: error.status,
      errorCode: error.errorCode,
      message: error.message,
      path
    });
    throw error;
  }

  // Успешный ответ — снимаем предположение о недоступности бэкенда.
  emit('api:healthy', { path });
  return data;
}

export const api = {
  /* --- Auth --- */
  register: (email, password) => request('POST', '/auth/register', { email, password }, { skipAuth: true }),

  login: async (email, password) => {
    const data = await request('POST', '/auth/login', { email, password }, { skipAuth: true });
    // В память кладём только access-токен; refresh уже в HttpOnly cookie.
    const access = data?.access_token || data?.token;
    if (access) session.setTokens({ access_token: access });
    return data;
  },

  me: () => request('GET', '/auth/me'),

  refresh: async () => {
    // Refresh-токен уходит автоматически в HttpOnly cookie.
    const data = await request('POST', '/auth/refresh', undefined, { skipAuth: true });
    // Ротация: сохраняем новый access-токен (новый refresh — тоже в cookie).
    if (data?.access_token) session.setTokens({ access_token: data.access_token });
    return data;
  },

  logout: () => request('POST', '/auth/logout', undefined, { skipAuth: true }),

  wsTicket: () => request('POST', '/auth/ws-ticket'),

  /* --- Profile --- */
  getProfile: () => request('GET', '/profile'),
  updateProfile: (patch) => request('PUT', '/profile', patch),
  convertResume: () => request('POST', '/profile/convert-resume'),
  compressResume: () => request('POST', '/profile/compress-resume'),

  /* --- Parsing (3 режима) --- */
  parseAuto: (payload) => request('POST', '/parsing/auto', payload),
  parseGroup: (payload) => request('POST', '/parsing/group', payload),
  parseManual: (payload) => request('POST', '/parsing/manual', payload),

  /* --- Vacancies --- */
  listVacancies: (params) => request('GET', `/vacancies${buildQuery(params)}`),
  getVacancy: (id) => request('GET', `/vacancies/${encodeURIComponent(id)}`),
  deleteVacancy: (id) => request('DELETE', `/vacancies/${encodeURIComponent(id)}`),
  setVacancyStatus: (id, status) => request('PATCH', `/vacancies/${encodeURIComponent(id)}/status`, { status }),

  /* --- Analysis & Letters --- */
  runAnalysis: (payload) => request('POST', '/analysis/run', payload),
  getAnalysis: (vacancyId) => request('GET', `/analysis/${encodeURIComponent(vacancyId)}`),
  getLetter: (vacancyId) => request('GET', `/letters/${encodeURIComponent(vacancyId)}`),

  /* --- Tasks --- */
  listTasks: () => request('GET', '/tasks'),
  getTask: (id) => request('GET', `/tasks/${encodeURIComponent(id)}`),
  cancelTask: (id) => request('POST', `/tasks/${encodeURIComponent(id)}/cancel`),
  resumeTask: (id) => request('POST', `/tasks/${encodeURIComponent(id)}/resume`)
};
