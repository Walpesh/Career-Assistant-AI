/* ============================================================
   Трекинг задач (GET /tasks + WS-события task.*).
   Единственный источник правды для счётчика в топбаре,
   списка задач в дашборде и мини-прогрессов в журнале.
   ============================================================ */

import { api } from './api.js';
import { on } from './bus.js';
import { setState, subscribe } from './state.js';
import { popup } from '../components/fadeout-action-popup.js';

const ACTIVE_STATUSES = new Set(['pending', 'processing', 'waiting_captcha']);

function normalizeTaskList(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.items)) return data.items;
  if (Array.isArray(data?.tasks)) return data.tasks;
  return [];
}

function emitTasks(list) {
  setState({ tasks: list });
}

function upsertTask(list, task) {
  const index = list.findIndex((item) => item.id === task.id);
  if (index === -1) return [task, ...list];
  const next = list.slice();
  next[index] = { ...next[index], ...task };
  return next;
}

/** Загрузить список задач с сервера. */
export async function refreshTasks() {
  try {
    const data = await api.listTasks();
    emitTasks(normalizeTaskList(data));
  } catch (error) {
    console.warn('[tasks] не удалось загрузить список задач:', error.message);
  }
}

export function isTaskActive(task) {
  return ACTIVE_STATUSES.has(task?.status);
}

export function getActiveTasks() {
  return getCurrentTasks().filter(isTaskActive);
}

let currentTasks = [];
export function getCurrentTasks() {
  return currentTasks;
}

/**
 * Завершённые статусы. WS-события не должны «оживлять» или перетирать их:
 * после реконнекта или отмены задачи могло прийти запаздывающее событие.
 */
const TERMINAL_STATUSES = new Set(['completed', 'failed']);

function findTask(id) {
  return currentTasks.find((item) => item.id === id);
}

function isTerminal(task) {
  return Boolean(task) && TERMINAL_STATUSES.has(task.status);
}

/** Подписка на WS-события задач + первичная загрузка. */
let initialized = false;

export async function initTaskTracking() {
  // Идемпотентно: enterApp() вызывается и при повторном входе, а каждый
  // новый вызов раньше добавлял ещё один набор подписок на ws:task.* —
  // события начислись N раз, счётчики и лог «дёргались».
  if (initialized) {
    await refreshTasks();
    return;
  }
  initialized = true;

  on('ws:task.created', (payload) => {
    if (!payload?.task_id) return;
    const existing = findTask(payload.task_id);
    // Догоняющее/дублированное событие не должно откатывать завершённую
    // задачу обратно в pending.
    if (isTerminal(existing)) return;
    upsert({
      id: payload.task_id,
      ...(payload.task_type ? { task_type: payload.task_type } : {}),
      ...(payload.created_at ? { created_at: payload.created_at } : {}),
      status: payload.status || existing?.status || 'pending',
      progress_current: existing?.progress_current ?? 0,
      progress_total: existing?.progress_total ?? 0
    });
  });

  // task.started (docs/03 §8): воркер взял задачу из очереди. Без этого
  // обработчика карточка до первого progress «висела» как «В очереди».
  on('ws:task.started', (payload) => {
    if (!payload?.task_id) return;
    const existing = findTask(payload.task_id);
    if (isTerminal(existing)) return;
    upsert({
      id: payload.task_id,
      ...(payload.task_type ? { task_type: payload.task_type } : {}),
      status: 'processing',
      started_at: payload.started_at || new Date().toISOString(),
      error_message: null
    });
  });

  on('ws:task.progress', (payload) => {
    if (!payload?.task_id) return;
    const existing = findTask(payload.task_id);
    // Запаздывающий прогресс завершённой задачи игнорируем — иначе
    // «Завершена» снова превратилась бы в «В обработке».
    if (isTerminal(existing)) return;
    // waiting_captcha — терминальное для прогресса состояние (docs/04 §5):
    // событие прогресса не должно «оживлять» задачу до ручного вмешательства.
    const status = existing?.status === 'waiting_captcha' ? 'waiting_captcha' : 'processing';
    upsert({
      id: payload.task_id,
      status,
      progress_current: payload.current,
      progress_total: payload.total,
      progress_message: payload.message,
      progress_stage: payload.stage
    });
  });

  on('ws:task.completed', (payload) => {
    if (!payload?.task_id) return;
    const existing = findTask(payload.task_id);
    if (isTerminal(existing) && existing.status !== 'completed') return;
    upsert({
      id: payload.task_id,
      status: 'completed',
      result: payload.result ?? null,
      error_message: null,
      finished_at: new Date().toISOString()
    });
  });

  on('ws:task.failed', (payload) => {
    if (!payload?.task_id) return;
    const existing = findTask(payload.task_id);
    if (isTerminal(existing)) return;
    // waiting_captcha приходит как task.failed со status (docs/03 §8):
    // капча — это пауза задачи, а не её финальная ошибка.
    if (payload.status === 'waiting_captcha') {
      const alreadyWaiting = existing?.status === 'waiting_captcha';
      upsert({
        id: payload.task_id,
        status: 'waiting_captcha',
        error_message: payload.error || 'Требуется прохождение капчи hh.ru',
        finished_at: null
      });
      // Уведомляем один раз за эпизод: пауза требует действий пользователя.
      if (!alreadyWaiting) {
        popup.warning(
          'Требуется капча hh.ru',
          payload.error ||
            'Пройдите капчу и нажмите «Капча пройдена — продолжить» в карточке задачи.'
        );
      }
      return;
    }
    upsert({
      id: payload.task_id,
      status: 'failed',
      error_message: payload.error || 'Неизвестная ошибка',
      finished_at: new Date().toISOString()
    });
  });

  on('ws:task.cancelled', (payload) => {
    if (!payload?.task_id) return;
    // Отмена финализирует задачу — повторное task.failed после неё не нужно.
    if (isTerminal(findTask(payload.task_id))) return;
    upsert({
      id: payload.task_id,
      status: payload.status || 'failed',
      error_message: payload.error || 'Отменено пользователем',
      finished_at: new Date().toISOString()
    });
  });

  on('ws:task.resumed', (payload) => {
    if (!payload?.task_id) return;
    // waiting_captcha → pending: снова активна, слот освобождён для очереди.
    upsert({
      id: payload.task_id,
      status: payload.status || 'pending',
      error_message: null,
      finished_at: null
    });
  });

  // Переподключение WS: события могли потеряться — перечитываем задачи.
  on('ws:resync', () => {
    refreshTasks();
  });

  // Держим локальную копию синхронно с общим store.
  subscribe((s) => {
    currentTasks = s.tasks;
  });
  await refreshTasks();
}

function upsert(task) {
  currentTasks = upsertTask(currentTasks, task);
  emitTasks(currentTasks);
}
