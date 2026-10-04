/* ============================================================
   Реал-тайм журнал: поток WS-событий (docs/03_API_CONTRACTS.md §8)
   + мини-прогрессы активных задач.
   ============================================================ */

import { CONFIG } from '../config.js';
import { on } from '../core/bus.js';
import { subscribe } from '../core/state.js';
import { formatClock, escapeHtml } from '../core/utils.js';
import { TASK_TYPE_LABELS, STATUS_LABELS } from '../components/badges.js';
import { progressBarHTML, updateProgressBar } from '../components/progress.js';

const ACTIVE_STATUSES = new Set(['pending', 'processing', 'waiting_captcha']);

let els = {};
let entries = [];
let autoscroll = true;
let levelFilter = '';
let initialized = false;
const miniRows = new Map(); // taskId -> { row, track, numbers }

export function initLogPanel() {
  if (initialized) return;
  initialized = true;

  els = {
    list: document.getElementById('log-list'),
    level: document.getElementById('log-level-filter'),
    autoscroll: document.getElementById('log-autoscroll'),
    clear: document.getElementById('btn-log-clear'),
    liveDot: document.getElementById('log-live-dot'),
    mini: document.getElementById('log-active-tasks')
  };

  els.level?.addEventListener('change', () => {
    levelFilter = els.level.value;
    renderAll();
  });

  els.autoscroll?.addEventListener('click', () => {
    autoscroll = !autoscroll;
    els.autoscroll.setAttribute('aria-pressed', String(autoscroll));
    els.autoscroll.classList.toggle('chip-active', autoscroll);
  });
  els.autoscroll?.classList.add('chip-active');

  els.clear?.addEventListener('click', () => {
    entries = [];
    els.list.innerHTML = '';
  });

  on('ws:event', ({ event, payload }) => {
    const { level, text } = describe(event, payload || {});
    push(level, text);
  });

  on('log:system', ({ level, message } = {}) => push(level || 'info', message || ''));

  on('ws:status', (status) => {
    if (!els.liveDot) return;
    const color = status === 'connected' ? 'bg-emerald-400' : status === 'connecting' ? 'bg-amber-400' : 'bg-rose-500';
    els.liveDot.className = `pulse-dot h-2 w-2 shrink-0 rounded-full ${color}`;
  });

  subscribe(renderMiniProgress);
  renderMiniProgress({ tasks: [] });
}

/** Человекочитаемое описание WS-события. */
function describe(event, payload) {
  switch (event) {
    case 'task.created':
      return { level: 'info', text: `Задача создана: ${TASK_TYPE_LABELS[payload.task_type] || payload.task_type || '—'} · ${shortId(payload.task_id)}` };
    case 'task.progress':
      return { level: 'info', text: `${payload.message || 'Прогресс'}${payload.current != null ? ` — ${payload.current}/${payload.total ?? '?'}` : ''}` };
    case 'task.completed':
      return { level: 'success', text: `Задача завершена · ${shortId(payload.task_id)}` };
    case 'task.failed':
      return { level: 'error', text: `Задача не выполнена · ${shortId(payload.task_id)}: ${payload.error || 'неизвестная ошибка'}` };
    case 'vacancy.updated':
      return { level: 'info', text: `Вакансия обновлена: статус → ${STATUS_LABELS[payload.status] || payload.status || '—'}` };
    case 'analysis.ready':
      return { level: 'success', text: `Анализ готов · match_score ${payload.match_score ?? '—'}` };
    case 'letter.ready':
      return { level: 'success', text: 'Сопроводительное письмо готово' };
    case 'popup':
      return { level: payload.type || 'info', text: `${payload.title || 'Уведомление'}${payload.message ? `: ${payload.message}` : ''}` };
    default:
      return { level: 'info', text: `${event} ${safeJson(payload)}` };
  }
}

function shortId(id) {
  return id ? `#${String(id).slice(0, 8)}` : '—';
}

function safeJson(value) {
  try {
    return JSON.stringify(value).slice(0, 140);
  } catch {
    return '';
  }
}

function push(level, text) {
  const entry = { level, text, at: new Date() };
  entries.push(entry);
  if (entries.length > CONFIG.LOG_LIMIT) {
    entries.shift();
    els.list?.firstElementChild?.remove();
  }
  if (passesFilter(entry)) appendEntry(entry);
}

function passesFilter(entry) {
  return !levelFilter || entry.level === levelFilter;
}

function appendEntry(entry) {
  if (!els.list) return;
  els.list.insertAdjacentHTML('beforeend', entryHTML(entry));
  if (autoscroll) els.list.scrollTop = els.list.scrollHeight;
}

function entryHTML(entry) {
  return `<li class="log-entry" data-level="${escapeHtml(entry.level)}">
    <span class="log-time">${formatClock(entry.at)}</span>
    <span class="log-dot"></span>
    <span class="log-msg">${escapeHtml(entry.text)}</span>
  </li>`;
}

function renderAll() {
  if (!els.list) return;
  els.list.innerHTML = entries.filter(passesFilter).map(entryHTML).join('');
  if (autoscroll) els.list.scrollTop = els.list.scrollHeight;
}

/* ---------- Мини-прогрессы активных задач ---------- */

/**
 * Сброс журнала перед повторным входом: очищаем записи и мини-прогрессы,
 * подписки и обработчики остаются (initLogPanel идемпотентен).
 */
export function resetLogPanel() {
  entries = [];
  if (els.list) els.list.innerHTML = '';
  miniRows.clear();
  if (els.mini) {
    els.mini.innerHTML = '';
    els.mini.classList.add('hidden');
  }
}

function renderMiniProgress(state) {
  if (!els.mini) return;
  const active = (state.tasks || []).filter((task) => ACTIVE_STATUSES.has(task.status)).slice(0, 3);

  els.mini.classList.toggle('hidden', active.length === 0);

  // Удаляем строки задач, которых больше нет среди активных.
  for (const [id, entry] of miniRows) {
    if (!active.some((task) => task.id === id)) {
      entry.row.remove();
      miniRows.delete(id);
    }
  }

  for (const task of active) {
    let entry = miniRows.get(task.id);
    if (!entry) {
      const row = document.createElement('div');
      row.className = 'space-y-1';
      row.dataset.miniTask = task.id;
      row.innerHTML = `
        <div class="flex items-center justify-between gap-2 text-[11px]">
          <span class="truncate text-slate-300">${escapeHtml(TASK_TYPE_LABELS[task.task_type] || task.task_type || 'Задача')}</span>
          <span class="shrink-0 tabular-nums text-slate-500" data-mini-numbers></span>
        </div>
        ${progressBarHTML(task)}`;
      els.mini.appendChild(row);
      entry = { row, track: row.querySelector('[data-progress-track]'), numbers: row.querySelector('[data-mini-numbers]') };
      miniRows.set(task.id, entry);
    }
    updateProgressBar(entry.track, task);
    entry.numbers.textContent = task.progress_total ? `${task.progress_current ?? 0}/${task.progress_total}` : '';
  }
}
