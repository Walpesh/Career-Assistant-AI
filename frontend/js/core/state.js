/* ============================================================
   Общее состояние приложения (профиль, задачи, фильтры, активная вкладка).
   ============================================================ */

import { CONFIG } from '../config.js';
import { emit } from './bus.js';
import { clamp } from './utils.js';

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
  /**
   * Несохранённое значение порога матчинга (null — берём из профиля).
   * Нужно, чтобы главная страница и «Моё резюме» не затирали друг друга,
   * пока пользователь двигает слайдер.
   */
  threshold: null,
  activeTab: 'dashboard'
};

const listeners = new Set();

export function getState() {
  return state;
}

export function setState(patch) {
  const profileChanged = Object.prototype.hasOwnProperty.call(patch, 'profile');
  Object.assign(state, patch);
  notify();

  // Порог из профиля — источник истины для обеих вкладок, но только если
  // пользователь не редактирует слайдер прямо сейчас (тогда приоритет у него).
  if (profileChanged && state.threshold === null) {
    emit('match:threshold', { value: getSavedMatchThreshold(), source: 'server' });
  }
}

/** Порог матчинга сохранённый в профиле (docs/02 §3.2). */
export function getSavedMatchThreshold() {
  const value = Number(state.profile?.match_threshold);
  return Number.isFinite(value) && value >= 0 ? value : CONFIG.DEFAULT_THRESHOLD;
}

/**
 * Порог матчинга, применяемый к вакансиям и задачам: несохранённое
 * значение слайдера приоритетнее значения профиля.
 */
export function getMatchThreshold() {
  if (state.threshold !== null) return state.threshold;
  return getSavedMatchThreshold();
}

/**
 * Синхронизация порога между главной страницей и профилем.
 *
 * @param {number} value  новое значение (0–100)
 * @param {'dashboard'|'profile'} source  источник изменения, чтобы
 *        получатель не перерисовывал тот же слайдер (защита от «эха»).
 */
export function setMatchThreshold(value, source) {
  const threshold = clamp(Math.round(Number(value)), 0, 100);
  if (!Number.isFinite(threshold)) return;
  if (state.threshold === threshold) return;
  state.threshold = threshold;
  notify();
  emit('match:threshold', { value: threshold, source });
}

/**
 * Порог сохранён на сервере (PUT /profile) — несохранённое значение
 * сбрасывается, обе вкладки показывают значение из профиля.
 */
export function commitMatchThreshold() {
  if (state.threshold === null) return;
  state.threshold = null;
  notify();
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
