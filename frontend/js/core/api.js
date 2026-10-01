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
    if (response.status === 401) {
      if (!options.skipAuth) {
        session.clear();
        emit('auth:expired');
      }
    }
    throw new ApiError(response.status, data?.detail || response.statusText, data?.error_code);
  }

  return data;
}

export const api = {
  /* --- Auth --- */
  register: (email, password) => request('POST', '/auth/register', { email, password }, { skipAuth: true }),

  login: async (email, password) => {
    const data = await request('POST', '/auth/login', { email, password }, { skipAuth: true });
    // Поддерживаем оба варианта именования токена.
    const access = data?.access_token || data?.token;
    if (access) session.setTokens({ access_token: access, refresh_token: data?.refresh_token });
    return data;
  },

  me: () => request('GET', '/auth/me'),

  refresh: async () => {
    const data = await request('POST', '/auth/refresh', { refresh_token: session.refreshToken }, { skipAuth: true });
    // Ротация: сохраняем новую пару токенов.
    if (data?.access_token) session.setTokens({ access_token: data.access_token, refresh_token: data.refresh_token });
    return data;
  },

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
  cancelTask: (id) => request('POST', `/tasks/${encodeURIComponent(id)}/cancel`)
};
