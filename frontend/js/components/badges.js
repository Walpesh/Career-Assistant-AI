/* ============================================================
   Бейджи и теги: статусы вакансий, источники, match_score,
   типы и статусы задач, режимы обработки.
   Справочники соответствуют docs/02_DATABASE.md и docs/05_LLM_PIPELINE.md.
   ============================================================ */

import { escapeHtml } from '../core/utils.js';

export const STATUS_LABELS = {
  raw: 'Новая',
  analyzed: 'Проанализирована',
  letter_ready: 'Письмо готово',
  applied: 'Отклик отправлен',
  error: 'Ошибка'
};

const STATUS_CLASSES = {
  raw: 'border-slate-600/60 bg-slate-500/10 text-slate-300',
  analyzed: 'border-sky-500/40 bg-sky-500/10 text-sky-300',
  letter_ready: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
  applied: 'border-violet-500/40 bg-violet-500/10 text-violet-300',
  error: 'border-rose-500/40 bg-rose-500/10 text-rose-300'
};

export function statusBadge(status) {
  const classes = STATUS_CLASSES[status] || STATUS_CLASSES.raw;
  const label = STATUS_LABELS[status] || status || '—';
  return `<span class="badge ${classes}">${escapeHtml(label)}</span>`;
}

export const SOURCE_LABELS = { auto: 'Автопоиск', group: 'Групповой', manual: 'Ручное' };

export function sourceBadge(source) {
  if (!source) return '';
  const label = SOURCE_LABELS[source] || source;
  return `<span class="badge border-slate-700 bg-slate-800/60 text-slate-400">${escapeHtml(label)}</span>`;
}

/**
 * Тег match_score. Цвет относительно порога пользователя:
 * ≥ порога — зелёный, ≥ порог−15 — жёлтый, иначе — красный.
 */
export function scoreTag(score, threshold = 70) {
  if (score === null || score === undefined || score === '') {
    return '<span class="badge border-slate-700 bg-slate-800/60 text-slate-500">без оценки</span>';
  }
  const value = Number(score);
  const classes = value >= threshold
    ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
    : value >= Math.max(0, threshold - 15)
      ? 'border-amber-500/40 bg-amber-500/10 text-amber-300'
      : 'border-rose-500/40 bg-rose-500/10 text-rose-300';
  return `<span class="badge ${classes}">${value}% match</span>`;
}

export const TASK_TYPE_LABELS = {
  parse_auto: 'Автопоиск',
  parse_group: 'Групповой парсер',
  parse_manual: 'Ручное добавление',
  analyze: 'Анализ',
  generate_letter: 'Генерация письма',
  auto_full: 'AUTO (анализ + письмо)',
  convert_resume: 'Сокращение резюме'
};

export const TASK_STATUS_LABELS = {
  pending: 'В очереди',
  processing: 'В обработке',
  completed: 'Завершена',
  failed: 'Ошибка',
  waiting_captcha: 'Ожидает капчу'
};

const TASK_STATUS_CLASSES = {
  pending: 'border-slate-600/60 bg-slate-500/10 text-slate-300',
  processing: 'border-indigo-500/40 bg-indigo-500/10 text-indigo-300',
  completed: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
  failed: 'border-rose-500/40 bg-rose-500/10 text-rose-300',
  waiting_captcha: 'border-amber-500/40 bg-amber-500/10 text-amber-300'
};

export function taskStatusBadge(status) {
  const classes = TASK_STATUS_CLASSES[status] || TASK_STATUS_CLASSES.pending;
  const label = TASK_STATUS_LABELS[status] || status || '—';
  return `<span class="badge ${classes}">${escapeHtml(label)}</span>`;
}

export const MODE_LABELS = {
  analyze: 'Только анализ',
  letter: 'Только письмо',
  analyze_and_letter: 'Анализ + письмо',
  auto: 'AUTO (порог → письмо)'
};

export const WORK_FORMAT_LABELS = { remote: 'Удалённо', hybrid: 'Гибрид', onsite: 'Офис' };

export function workFormatBadge(format) {
  if (!format) return '';
  return `<span class="badge border-slate-700 bg-slate-800/60 text-slate-400">${escapeHtml(WORK_FORMAT_LABELS[format] || format)}</span>`;
}
