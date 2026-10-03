/* ============================================================
   Прогресс-бары задач (визуальная часть; данные — из WS task.progress).

   Доступность: роль progressbar с aria-valuenow/min/max и aria-valuetext.
   Роль progressbar скрывает содержимое от скринридера (в т.ч. число «12 / 47»),
   поэтому прогресс обязательно дублируется текстом aria-valuetext.
   ============================================================ */

import { escapeHtml } from '../core/utils.js';

/** Понятная подпись задачи для скринридера. */
function taskAriaLabel(task = {}) {
  const type = task.task_type ? String(task.task_type) : 'задача';
  const message = task.progress_message || task.status || '';
  const base = `Прогресс задачи «${type}»`;
  return message ? `${base}: ${message}` : base;
}

/** Текущее состояние прогресса — и для aria-valuetext, и для подписи. */
function valueText(task = {}) {
  const current = Number(task.progress_current ?? 0);
  const total = Number(task.progress_total ?? 0);
  const percent = progressPercent(task);

  if (task.status === 'completed') return 'выполнено, 100%';
  if (task.status === 'failed') return total ? `ошибка, ${current} из ${total}` : 'ошибка';
  if (task.status === 'waiting_captcha') return 'приостановлено: ожидает капчу';
  if (!total) return 'выполняется';
  return `${current} из ${total}, ${percent}%`;
}

/**
 * HTML прогресс-бара.
 *
 * Ширина НЕ задаётся атрибутом style="width:…" в разметке: CSP со
 * `style-src 'self'` (без 'unsafe-inline') блокирует инлайновые style-атрибуты
 * при вставке через innerHTML. Вместо этого ширина выставляется через CSSOM
 * в updateProgressBar() — CSSOM-присваивания CSP не запрещает.
 * Поэтому после вставки разметки вызов updateProgressBar() обязателен.
 *
 * @param {{current?:number,total?:number,status?:string}} task
 */
export function progressBarHTML(task = {}) {
  const total = Number(task.progress_total ?? 0);
  return `<div class="progress-track" data-progress-track
       role="progressbar"
       aria-label="${escapeHtml(taskAriaLabel(task))}"
       aria-valuemin="0"
       aria-valuemax="${total > 0 ? total : 100}"
       aria-valuenow="${total > 0 ? Number(task.progress_current ?? 0) : progressPercent(task)}"
       aria-valuetext="${escapeHtml(valueText(task))}">
    <div class="progress-fill"></div>
  </div>`;
}

/**
 * Обновить прогресс-бар задачи на месте (без перерисовки списка).
 * Помимо ширины синхронизируем ARIA-атрибуты: без них скринридер
 * продолжал бы объявлять прежний процент.
 * @param {HTMLElement} trackEl элемент .progress-track
 * @param {{current?:number,total?:number,status?:string}} task
 */
export function updateProgressBar(trackEl, task = {}) {
  if (!trackEl) return;
  const fill = trackEl.querySelector('.progress-fill');
  if (!fill) return;
  const { width, mode, color } = computeFill(task);
  fill.style.width = `${width}%`;
  fill.className = `progress-fill ${mode}`.trim();
  fill.style.background = color || '';

  const total = Number(task.progress_total ?? 0);
  trackEl.setAttribute('aria-valuemax', String(total > 0 ? total : 100));
  trackEl.setAttribute('aria-valuenow', String(total > 0 ? Number(task.progress_current ?? 0) : progressPercent(task)));
  trackEl.setAttribute('aria-valuetext', valueText(task));
}

export function progressLabel(task = {}) {
  const current = Number(task.progress_current ?? 0);
  const total = Number(task.progress_total ?? 0);
  if (!total) return '';
  return `${current} / ${total}`;
}

export function progressPercent(task = {}) {
  const current = Number(task.progress_current ?? 0);
  const total = Number(task.progress_total ?? 0);
  if (!total) return 0;
  return Math.min(100, Math.round((current / total) * 100));
}

function computeFill(task) {
  const status = task.status || 'pending';
  const percent = progressPercent(task);

  if (status === 'completed') return { width: 100, mode: '', color: '#10b981' };
  if (status === 'failed') return { width: percent || 100, mode: '', color: '#f43f5e' };
  if (status === 'processing') {
    // Пока сервер не сообщил total — показываем «неопределённый» штрихованный прогресс.
    return { width: percent || 35, mode: 'is-striped', color: '' };
  }
  return { width: percent || 0, mode: '', color: '' };
}
