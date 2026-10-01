/* ============================================================
   Вкладка «Парсинг дашборд»: 3 режима сбора (Автопоиск, Групповой,
   Ручное добавление) + список задач с прогрессом из WebSocket.
   Контракты: docs/03_API_CONTRACTS.md §5, docs/04_PARSING_RULES.md.
   ============================================================ */

import { api } from '../core/api.js';
import { getState, subscribe } from '../core/state.js';
import { loadPartial } from '../core/partials.js';
import { popup } from '../components/fadeout-action-popup.js';
import { TagInput } from '../components/tag-input.js';
import { refreshTasks, getCurrentTasks } from '../core/tasks.js';
import { progressBarHTML, updateProgressBar } from '../components/progress.js';
import { taskStatusBadge, TASK_TYPE_LABELS } from '../components/badges.js';
import { formatDateTime, escapeHtml, clamp } from '../core/utils.js';
import { confirmDialog } from '../components/overlay.js';

const HH_SEARCH_URL = /^https?:\/\/([\w-]+\.)*hh\.ru\//i;
const HH_VACANCY_URL = /^https?:\/\/([\w-]+\.)*hh\.ru\/vacancy\/\d+/i;

let els = {};
let keywordsInput = null;
let mounted = false;
let thresholdTouched = false;
const cardMap = new Map();

export async function mount() {
  if (mounted) return;
  mounted = true;

  document.getElementById('view-dashboard').innerHTML = await loadPartial('dashboard');

  els = {
    modeButtons: [...document.querySelectorAll('#parsing-modes [data-mode]')],
    keywords: document.getElementById('auto-keywords'),
    autoThreshold: document.getElementById('auto-threshold'),
    autoThresholdValue: document.getElementById('auto-threshold-value'),
    autoMaxPages: document.getElementById('auto-max-pages'),
    autoButton: document.getElementById('btn-parse-auto'),
    groupUrl: document.getElementById('group-url'),
    groupMaxPages: document.getElementById('group-max-pages'),
    groupButton: document.getElementById('btn-parse-group'),
    manualUrl: document.getElementById('manual-url'),
    manualRunAnalysis: document.getElementById('manual-run-analysis'),
    manualButton: document.getElementById('btn-parse-manual'),
    list: document.getElementById('tasks-list'),
    empty: document.getElementById('tasks-empty'),
    count: document.getElementById('tasks-count'),
    refreshButton: document.getElementById('btn-refresh-tasks')
  };

  keywordsInput = new TagInput(els.keywords, {
    placeholder: els.keywords.dataset.placeholder,
    max: 30
  });

  els.modeButtons.forEach((button) => button.addEventListener('click', () => setMode(button.dataset.mode)));
  setMode('auto');

  els.autoThreshold.addEventListener('input', () => {
    thresholdTouched = true;
    els.autoThresholdValue.textContent = `${els.autoThreshold.value}%`;
  });

  els.autoButton.addEventListener('click', submitAuto);
  els.groupButton.addEventListener('click', submitGroup);
  els.manualButton.addEventListener('click', submitManual);
  els.refreshButton.addEventListener('click', () => refreshTasks());
  els.list.addEventListener('click', handleTaskListClick);

  // Порог матчинга по умолчанию берётся из профиля (docs/02 §3.2).
  applyProfileThreshold(getState());
  subscribe((state) => {
    applyProfileThreshold(state);
    renderTasks();
  });
  await refreshTasks();
}

/** Подставить match_threshold из профиля, пока пользователь не менял слайдер. */
function applyProfileThreshold(state) {
  if (thresholdTouched) return;
  const threshold = Number(state?.profile?.match_threshold);
  if (Number.isFinite(threshold) && threshold >= 0) {
    els.autoThreshold.value = clamp(threshold, 0, 100);
    els.autoThresholdValue.textContent = `${els.autoThreshold.value}%`;
  }
}

/* ---------- Режимы ---------- */

function setMode(mode) {
  els.modeButtons.forEach((button) => {
    const active = button.dataset.mode === mode;
    button.classList.toggle('mode-card-active', active);
    button.setAttribute('aria-selected', String(active));
  });
  document.querySelectorAll('[data-mode-panel]').forEach((panel) => {
    panel.classList.toggle('hidden', panel.dataset.modePanel !== mode);
  });
}

function chipValues(group) {
  return [...document.querySelectorAll(`[data-chip-group="${group}"] input:checked`)].map((input) => input.value);
}

/* ---------- Запуск режимов ---------- */

async function submitAuto() {
  const keywords = keywordsInput.getValues();
  if (!keywords.length) {
    popup.warning('Добавьте ключевые слова', 'Автопоиск строит поисковый URL из ключевых слов.');
    return;
  }

  const payload = {
    keywords,
    match_threshold: clamp(Number(els.autoThreshold.value) || 0, 0, 100),
    max_pages: clamp(Number(els.autoMaxPages.value) || 3, 1, 10)
  };
  const employment = chipValues('employment');
  const formats = chipValues('work_format');
  const schedules = chipValues('schedule');
  if (employment.length) payload.employment_forms = employment;
  if (formats.length) payload.work_formats = formats;
  if (schedules.length) payload.schedules = schedules;

  await createTask(() => api.parseAuto(payload), els.autoButton, 'Автопоиск запущен');
}

async function submitGroup() {
  const url = els.groupUrl.value.trim();
  if (!HH_SEARCH_URL.test(url)) {
    popup.warning('Некорректная ссылка', 'Укажите ссылку на страницу поиска hh.ru (домен *.hh.ru).');
    return;
  }
  const payload = {
    search_url: url,
    max_pages: clamp(Number(els.groupMaxPages.value) || 3, 1, 10)
  };
  await createTask(() => api.parseGroup(payload), els.groupButton, 'Групповой парсинг запущен');
}

async function submitManual() {
  const url = els.manualUrl.value.trim();
  if (!HH_VACANCY_URL.test(url)) {
    popup.warning('Некорректная ссылка', 'Укажите прямую ссылку на вакансию: https://<город>.hh.ru/vacancy/<id>.');
    return;
  }
  const payload = {
    vacancy_url: url,
    run_analysis: els.manualRunAnalysis.checked
  };
  await createTask(() => api.parseManual(payload), els.manualButton, 'Вакансия добавлена в очередь');
}

async function createTask(request, button, title) {
  toggleLoading(button, true);
  try {
    const data = await request();
    popup.info(title, `Задача ${String(data?.task_id || '').slice(0, 8)}… в очереди (${data?.status || 'pending'})`);
    await refreshTasks();
  } catch (error) {
    popup.error('Не удалось запустить', error.message);
  } finally {
    toggleLoading(button, false);
  }
}

function toggleLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  button.querySelector('[data-spinner]')?.classList.toggle('hidden', !loading);
}

/* ---------- Список задач (дифф-обновление без перерисовки) ---------- */

async function handleTaskListClick(event) {
  const cancelButton = event.target.closest('[data-action="cancel-task"]');
  if (!cancelButton) return;
  const taskId = cancelButton.closest('[data-task-id]')?.dataset.taskId;
  if (!taskId) return;

  const confirmed = await confirmDialog({
    title: 'Отменить задачу?',
    message: 'Задача будет остановлена, если это возможно (docs/03_API_CONTRACTS.md §7).',
    confirmLabel: 'Отменить задачу',
    danger: true
  });
  if (!confirmed) return;

  try {
    await api.cancelTask(taskId);
    popup.warning('Задача отменена', 'Отмена зарегистрирована в очереди.');
    await refreshTasks();
  } catch (error) {
    popup.error('Не удалось отменить', error.message);
  }
}

function renderTasks() {
  if (!els.list) return;
  const tasks = getCurrentTasks()
    .slice()
    .sort((a, b) => new Date(b.created_at || 0) - new Date(a.created_at || 0))
    .slice(0, 20);

  els.count.textContent = String(tasks.length);
  els.empty.classList.toggle('hidden', tasks.length > 0);

  const seen = new Set();
  for (const task of tasks) {
    seen.add(task.id);
    let card = cardMap.get(task.id);

    if (!card || !card.isConnected) {
      card = htmlToElement(taskCardHTML(task));
      cardMap.set(task.id, card);
    } else if (card.dataset.status !== (task.status || '')) {
      // Смена статуса — карточка пересобирается (меняются кнопки/бейджи).
      const next = htmlToElement(taskCardHTML(task));
      card.replaceWith(next);
      cardMap.set(task.id, next);
      card = next;
    } else {
      updateProgressBar(card.querySelector('[data-progress-track]'), task);
      const messageEl = card.querySelector('[data-task-message]');
      const numbersEl = card.querySelector('[data-task-numbers]');
      if (messageEl) messageEl.textContent = taskMessage(task);
      if (numbersEl) numbersEl.textContent = taskNumbers(task);
    }
    els.list.appendChild(card); // выставляем порядок «новые сверху»
  }

  for (const [id, card] of cardMap) {
    if (!seen.has(id)) {
      card.remove();
      cardMap.delete(id);
    }
  }
}

function taskCardHTML(task) {
  const typeLabel = TASK_TYPE_LABELS[task.task_type] || task.task_type || 'Задача';
  const canCancel = ['pending', 'processing', 'waiting_captcha'].includes(task.status);
  return `<div class="rounded-xl border border-slate-800 bg-slate-950/50 p-4" data-task-id="${escapeHtml(task.id)}" data-status="${escapeHtml(task.status || '')}">
    <div class="flex flex-wrap items-center gap-2">
      <span class="badge border-indigo-500/30 bg-indigo-500/10 text-indigo-300">${escapeHtml(typeLabel)}</span>
      ${taskStatusBadge(task.status)}
      <span class="ml-auto text-[11px] text-slate-500">${task.created_at ? escapeHtml(formatDateTime(task.created_at)) : ''}</span>
      ${canCancel ? '<button type="button" data-action="cancel-task" class="btn-ghost btn-sm">Отменить</button>' : ''}
    </div>
    ${progressBarHTML(task)}
    <div class="mt-2 flex items-center justify-between gap-3 text-xs text-slate-500">
      <span class="truncate" data-task-message>${escapeHtml(taskMessage(task))}</span>
      <span class="shrink-0 tabular-nums" data-task-numbers>${escapeHtml(taskNumbers(task))}</span>
    </div>
    ${task.error_message ? `<p class="mt-2 text-xs text-rose-300">${escapeHtml(task.error_message)}</p>` : ''}
  </div>`;
}

function taskMessage(task) {
  if (task.progress_message) return task.progress_message;
  switch (task.status) {
    case 'pending': return 'Ожидает свободный слот в очереди';
    case 'processing': return 'В обработке…';
    case 'completed': return 'Задача завершена';
    case 'failed': return 'Задача завершилась ошибкой';
    case 'waiting_captcha': return 'Требуется прохождение капчи hh.ru';
    default: return '';
  }
}

function taskNumbers(task) {
  if (!task.progress_total) return '';
  return `${task.progress_current ?? 0} / ${task.progress_total}`;
}

function htmlToElement(html) {
  const template = document.createElement('template');
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}
