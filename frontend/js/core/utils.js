/* ============================================================
   Утилиты: форматирование, буфер обмена, скачивание, debounce.
   ============================================================ */

/** Экранирование HTML-спецсимволов для шаблонных строк. */
export function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

/** Экранирование и переносы строк. */
export function escapeMultiline(value) {
  return escapeHtml(value).replaceAll('\n', '<br />');
}

const CURRENCY_SYMBOLS = { RUR: '₽', RUB: '₽', USD: '$', EUR: '€', KZT: '₸', BYN: 'Br', UZS: 'cум', UAH: '₴', AZN: '₼', GEL: '₾' };

export function currencySymbol(code) {
  return CURRENCY_SYMBOLS[code] || code || '₽';
}

function formatNumber(value) {
  return new Intl.NumberFormat('ru-RU').format(Number(value));
}

/** Зарплата: «от 180 000 ₽», «до 250 000 ₽», «180 000 – 220 000 ₽», «з/п не указана». */
export function formatSalary(from, to, currency = 'RUR') {
  const symbol = currencySymbol(currency);
  const hasFrom = Number.isFinite(Number(from)) && from !== null && from !== '';
  const hasTo = Number.isFinite(Number(to)) && to !== null && to !== '';
  if (hasFrom && hasTo) return `${formatNumber(from)} – ${formatNumber(to)} ${symbol}`;
  if (hasFrom) return `от ${formatNumber(from)} ${symbol}`;
  if (hasTo) return `до ${formatNumber(to)} ${symbol}`;
  return 'з/п не указана';
}

/** Дата/время: «05.02.2026, 14:30». */
export function formatDateTime(value) {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }).format(date);
}

/** Только время для журнала: «14:30:05». */
export function formatClock(value = new Date()) {
  const date = value instanceof Date ? value : new Date(value);
  return new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(date);
}

/** Относительное время: «5 мин назад». */
export function timeAgo(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const diffSec = Math.round((Date.now() - date.getTime()) / 1000);
  if (diffSec < 60) return 'только что';
  const minutes = Math.floor(diffSec / 60);
  if (minutes < 60) return `${minutes} мин назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} дн назад`;
  return formatDateTime(value);
}

export function debounce(fn, delay = 350) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delay);
  };
}

export function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

export function uid() {
  return `id-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** Копирование в буфер обмена с fallback для не-secure контекстов. */
export async function copyText(text) {
  const value = String(text ?? '');
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    /* fallback ниже */
  }
  try {
    const area = document.createElement('textarea');
    area.value = value;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

/** Скачивание текста как файла (.txt). */
export function downloadText(filename, text) {
  const blob = new Blob([String(text ?? '')], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Человекочитаемый размер текста: «1 234 симв.» */
export function charCount(value) {
  return formatNumber(String(value ?? '').length);
}

/** Простой sleep для демо-режима. */
export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
