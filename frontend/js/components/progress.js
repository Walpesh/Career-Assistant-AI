/* ============================================================
   Прогресс-бары задач (визуальная часть; данные — из WS task.progress).
   ============================================================ */

/**
 * HTML прогресс-бара.
 * @param {{current?:number,total?:number,status?:string}} task
 */
export function progressBarHTML(task = {}) {
  const { width, mode } = computeFill(task);
  return `<div class="progress-track" data-progress-track>
    <div class="progress-fill ${mode}" style="width:${width}%"></div>
  </div>`;
}

/**
 * Обновить прогресс-бар задачи на месте (без перерисовки списка).
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
