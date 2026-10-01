/* ============================================================
   Каркас приложения: навигация по вкладкам, топбар,
   индикатор WebSocket, счётчик активных задач, журнал.
   ============================================================ */

import { on } from '../core/bus.js';
import { session } from '../core/session.js';
import { getState, setState, subscribe } from '../core/state.js';
import { popup } from '../components/fadeout-action-popup.js';
import { confirmDialog } from '../components/overlay.js';

const PAGE_META = {
  dashboard: { title: 'Парсинг дашборд', subtitle: 'Сбор вакансий: авто / группа / вручную' },
  analysis: { title: 'Анализ и отклик', subtitle: 'Вакансии, матчинг, письма и статусы' },
  profile: { title: 'Моё резюме', subtitle: 'Профиль, compact_resume и настройки матчинга' }
};

const ACTIVE_TASK_STATUSES = new Set(['pending', 'processing', 'waiting_captcha']);
const views = {};
let initialized = false;

/** Зарегистрировать модуль вкладки: { mount?, onShow? }. */
export function registerView(name, module) {
  views[name] = module;
}

export function initShell() {
  if (initialized) return;
  initialized = true;

  // Навигация (сайдбар + мобильная панель) и открытие журнала.
  document.addEventListener('click', (event) => {
    const tabButton = event.target.closest('[data-tab]');
    if (tabButton) {
      setActiveTab(tabButton.dataset.tab);
      return;
    }
    if (event.target.closest('[data-action="toggle-logs"]')) toggleLogs();
  });

  document.getElementById('btn-log-toggle')?.addEventListener('click', () => toggleLogs());
  document.getElementById('log-close')?.addEventListener('click', () => closeLogs());
  document.getElementById('log-backdrop')?.addEventListener('click', () => closeLogs());

  document.getElementById('btn-logout')?.addEventListener('click', async () => {
    const confirmed = await confirmDialog({
      title: 'Выйти из аккаунта?',
      message: 'Локальная сессия и JWT-токены будут сброшены.',
      confirmLabel: 'Выйти',
      danger: true
    });
    if (!confirmed) return;
    session.clear();
    window.location.reload();
  });

  on('ws:status', updateWsIndicator);
  updateWsIndicator('connecting');

  subscribe(updateTasksPill);
  updateTasksPill(getState());

  on('ui:goto-tab', (tab) => setActiveTab(tab));
}

export async function start() {
  await setActiveTab(getState().activeTab || 'dashboard');
}

export async function setActiveTab(name) {
  if (!PAGE_META[name]) name = 'dashboard';
  setState({ activeTab: name });

  document.querySelectorAll('[data-view]').forEach((section) => {
    section.classList.toggle('hidden', section.dataset.view !== name);
  });

  document.querySelectorAll('[data-tab]').forEach((button) => {
    const active = button.dataset.tab === name;
    if (button.classList.contains('nav-btn')) button.classList.toggle('nav-btn-active', active);
    if (button.classList.contains('mobile-nav-btn')) button.classList.toggle('active', active);
  });

  const meta = PAGE_META[name];
  document.getElementById('page-title').textContent = meta.title;
  document.getElementById('page-subtitle').textContent = meta.subtitle;

  const view = views[name];
  if (!view) return;
  try {
    await view.mount?.();
    await view.onShow?.();
  } catch (error) {
    console.error('[shell] ошибка вкладки', name, error);
    popup.error('Ошибка интерфейса', error.message);
  }
}

/* ---------- WebSocket индикатор ---------- */

function updateWsIndicator(status) {
  const dot = document.getElementById('ws-status-dot');
  const label = document.getElementById('ws-status-label');
  if (!dot || !label) return;

  const styles = {
    connected: ['bg-emerald-400', 'Реал-тайм активен'],
    connecting: ['bg-amber-400', 'Подключение…'],
    disconnected: ['bg-rose-500', 'Нет соединения']
  };
  const [colorClass, text] = styles[status] || styles.disconnected;
  dot.className = `h-2 w-2 rounded-full ${colorClass}`;
  label.textContent = text;
  label.className = `hidden sm:inline ${status === 'connected' ? 'text-emerald-300' : status === 'connecting' ? 'text-amber-300' : 'text-rose-300'}`;
}

/* ---------- Счётчик активных задач ---------- */

function updateTasksPill(state) {
  const pill = document.getElementById('active-tasks-pill');
  const count = document.getElementById('active-tasks-count');
  if (!pill || !count) return;

  const active = (state.tasks || []).filter((task) => ACTIVE_TASK_STATUSES.has(task.status));
  count.textContent = String(active.length);
  pill.classList.toggle('hidden', active.length === 0);
}

/* ---------- Журнал реал-тайм ---------- */

function toggleLogs() {
  const panel = document.getElementById('log-panel');
  if (!panel) return;

  // На xl+ журнал — постоянная колонка: сворачиваем/разворачиваем её классом.
  if (window.matchMedia('(min-width: 1280px)').matches) {
    panel.classList.toggle('xl:hidden');
    return;
  }

  const isOpen = panel.style.transform === 'translateX(0)';
  if (isOpen) closeLogs();
  else openLogs();
}

function openLogs() {
  document.getElementById('log-panel')?.style.setProperty('transform', 'translateX(0)');
  document.getElementById('log-backdrop')?.classList.remove('hidden');
}

function closeLogs() {
  document.getElementById('log-panel')?.style.removeProperty('transform');
  document.getElementById('log-backdrop')?.classList.add('hidden');
}
