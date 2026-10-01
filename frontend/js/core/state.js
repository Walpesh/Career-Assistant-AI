/* ============================================================
   Общее состояние приложения (профиль, задачи, фильтры, активная вкладка).
   ============================================================ */

import { CONFIG } from '../config.js';

const state = {
  user: null,
  /** GET /profile — user_profiles (docs/02_DATABASE.md §3.2) */
  profile: null,
  /** GET /tasks — активные и завершённые задачи */
  tasks: [],
  /** Фильтры вкладки «Анализ и Отклик» (docs/03 §4) */
  filters: {
    status: '',
    source: '',
    search: '',
    minScore: '',
    page: 1,
    size: 20
  },
  activeTab: 'dashboard'
};

const listeners = new Set();

export function getState() {
  return state;
}

export function setState(patch) {
  Object.assign(state, patch);
  notify();
}

/** Порог матчинга: из профиля, иначе дефолт из docs/02. */
export function getMatchThreshold() {
  const value = Number(state.profile?.match_threshold);
  return Number.isFinite(value) && value >= 0 ? value : CONFIG.DEFAULT_THRESHOLD;
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function notify() {
  listeners.forEach((listener) => {
    try {
      listener(state);
    } catch (error) {
      console.error('[state] listener error', error);
    }
  });
}
