/* ============================================================
   Трекинг задач (GET /tasks + WS-события task.*).
   Единственный источник правды для счётчика в топбаре,
   списка задач в дашборде и мини-прогрессов в журнале.
   ============================================================ */

import { api } from './api.js';
import { on } from './bus.js';
import { setState, subscribe } from './state.js';

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

/** Подписка на WS-события задач + первичная загрузка. */
export async function initTaskTracking() {
  on('ws:task.created', (payload) => {
    if (!payload?.task_id) return;
    upsert({ id: payload.task_id, task_type: payload.task_type, status: payload.status || 'pending', progress_current: 0, progress_total: 0 });
  });

  on('ws:task.progress', (payload) => {
    if (!payload?.task_id) return;
    upsert({
      id: payload.task_id,
      status: 'processing',
      progress_current: payload.current,
      progress_total: payload.total,
      progress_message: payload.message,
      progress_stage: payload.stage
    });
  });

  on('ws:task.completed', (payload) => {
    if (!payload?.task_id) return;
    upsert({ id: payload.task_id, status: 'completed', result: payload.result ?? null, finished_at: new Date().toISOString() });
  });

  on('ws:task.failed', (payload) => {
    if (!payload?.task_id) return;
    upsert({ id: payload.task_id, status: 'failed', error_message: payload.error || 'Неизвестная ошибка', finished_at: new Date().toISOString() });
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
