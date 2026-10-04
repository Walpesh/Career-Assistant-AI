/* ============================================================
   Bootstrap Career-Assistant-AI:
   1) подключает оверлеи и журнал; 2) демо-режим (?demo=1);
   3) проверяет сессию; 4) запускает приложение и WebSocket.
   ============================================================ */

import { CONFIG } from './config.js';
import { session } from './core/session.js';
import { api } from './core/api.js';
import { on } from './core/bus.js';
import { resetAppState, setState } from './core/state.js';
import { initTaskTracking } from './core/tasks.js';
import { loadPartial } from './core/partials.js';
import { initDegradationBanner, stopDegradationBanner } from './core/degradation.js';
import { RealtimeClient } from './core/ws.js';
import { DemoSocket, installMock } from './core/mock.js';
import { popup } from './components/fadeout-action-popup.js';
import { bindOverlays, closeAllOverlays } from './components/overlay.js';
import * as authView from './views/auth.js';
import * as shell from './views/shell.js';
import * as logPanel from './views/log-panel.js';
import * as profileView from './views/profile.js';
import * as dashboardView from './views/dashboard.js';
import * as analysisView from './views/analysis.js';

/**
 * WS закрыт кодом 4401 — access-токен истёк. Раньше это немедленно
 * возвращало на экран входа (пользователь воспринимал это как «перезагрузку
 * страницы» каждые ACCESS_TOKEN_EXPIRE_MINUTES). Теперь пробуем refresh-ротацию
 * (docs/03 §2) и переподключаем канал с новым токеном.
 */
async function handleWsUnauthorized() {
  try {
    const data = await api.refresh();
    if (data?.access_token) {
      realtime.connect();
      return;
    }
  } catch (error) {
    console.warn('[ws] не удалось обновить токен:', error.message);
  }
  handleLogout({ silent: true });
  popup.warning('Сессия истекла', 'Войдите в аккаунт заново.');
}

/**
 * Выход из аккаунта (кнопка «Выйти» или удаление аккаунта) — без
 * location.reload(): WS закрывается, состояние и вкладки сбрасываются,
 * пользователь остаётся в той же SPA-сессии страницы.
 * @param {{silent?: boolean}} [options]
 */
function handleLogout({ silent = false } = {}) {
  try {
    realtime.close();
  } catch {
    /* ignore */
  }
  session.clear();
  resetAppState();
  closeAllOverlays();
  shell.resetShell();
  logPanel.resetLogPanel();
  authView.resetAuth();
  showAuthScreen();
  if (!silent) {
    popup.info('Вы вышли из аккаунта', 'Сессия закрыта. Можно войти снова.');
  }
}

/* ---------- Глобальные error boundary ---------- */

/** Антиспам: одна и та же ошибка показывается тостом не чаще раза в 30 сек. */
const reportedErrors = new Map();

/**
 * Перехват необработанных исключений и promise-rejection: вместо «тихой»
 * поломки интерфейса пользователь получает уведомление, а разработчик —
 * запись в консоли. AbortError (отмена fetch) и предупреждение
 * ResizeObserver — штатный шум браузера, их не показываем.
 */
function installGlobalErrorHandlers() {
  const report = (error) => {
    const message = String(error?.message || error || 'Неизвестная ошибка');
    if (/AbortError|ResizeObserver loop/i.test(message)) return;
    const now = Date.now();
    if (reportedErrors.has(message) && now - reportedErrors.get(message) < 30000) return;
    reportedErrors.set(message, now);
    popup.error('Непредвиденная ошибка', message);
  };

  window.addEventListener('error', (event) => {
    console.error('[ui] uncaught error:', event.error || event.message);
    report(event.error || event.message);
  });

  window.addEventListener('unhandledrejection', (event) => {
    if (event.reason?.name === 'AbortError') return;
    console.error('[ui] unhandled rejection:', event.reason);
    report(event.reason);
  });
}

const realtime = CONFIG.DEMO
  ? new DemoSocket()
  : new RealtimeClient({
      onUnauthorized: handleWsUnauthorized,
      // Одноразовый тикет запрашивается под каждый connect/реконнект (docs/03 §8).
      getTicket: async () => {
        const data = await api.wsTicket();
        return data?.ticket || null;
      }
    });

async function boot() {
  installGlobalErrorHandlers();
  bindOverlays();

  // Оверлеи (модалка анализа, drawer письма, confirm-диалог).
  document.getElementById('overlay-root').innerHTML = await loadPartial('modals');

  if (CONFIG.DEMO) {
    installMock();
    document.getElementById('demo-banner')?.classList.remove('hidden');
  }

  // Регистрация вкладок в каркасе.
  shell.registerView('dashboard', dashboardView);
  shell.registerView('analysis', analysisView);
  shell.registerView('profile', profileView);
  shell.initShell();

  logPanel.initLogPanel();

  // Баннер деградации: LLM/очередь/WS недоступны — предупреждаем, не блокируя.
  initDegradationBanner();

  // WS-событие `popup` → кастомный Fadeout-action-popup (docs/03 §8).
  on('ws:popup', (payload = {}) => {
    popup.show({ type: payload.type || 'info', title: payload.title || 'Уведомление', message: payload.message || '' });
  });

  // Истёкшая сессия → сброс состояния и возврат на экран входа (без reload).
  on('auth:expired', () => {
    handleLogout({ silent: true });
    popup.warning('Сессия истекла', 'Войдите в аккаунт заново.');
  });

  // Выход из аккаунта и удаление аккаунта — мгновенный переход на экран
  // входа без перезагрузки страницы (SPA-state сбрасывается здесь).
  on('auth:logout', (payload) => handleLogout(payload || {}));

  // Переподключение WS: события за время обрыва потеряны — обновляем профиль
  // (порог матчинга) и просим вкладки перечитать данные.
  on('ws:resync', () => {
    api.getProfile()
      .then((profile) => setState({ profile }))
      .catch((error) => console.warn('[ws] профиль не обновлён:', error.message));
  });

  authView.initAuth(() => enterApp());

  if (session.isAuthed() || session.hasSessionHint()) {
    try {
      // После перезагрузки access-токен в памяти пуст — восстанавливаем сессию
      // через refresh-cookie (HttpOnly), затем читаем профиль.
      if (!session.isAuthed() && session.hasSessionHint()) {
        await api.refresh();
      }
      const user = await api.me();
      if (user && typeof user === 'object') session.setUser(user);
      await enterApp();
    } catch (error) {
      console.warn('[boot] сессия недействительна:', error.message);
      session.clear();
      showAuthScreen();
    }
  } else {
    showAuthScreen();
  }
}

async function enterApp() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-screen').classList.remove('hidden');

  const user = session.user;
  setState({ user });
  const emailEl = document.getElementById('user-email');
  if (emailEl) emailEl.textContent = user?.email || '—';

  realtime.connect();
  await initTaskTracking();

  // Профиль нужен для порога матчинга и настроек — загружаем в фоне.
  api.getProfile()
    .then((profile) => setState({ profile }))
    .catch((error) => console.warn('[boot] профиль не загружен:', error.message));

  await shell.start();
}

function showAuthScreen() {
  // Открытые модалки/drawer'ы принадлежат прошлой сессии — закрываем.
  closeAllOverlays();
  document.getElementById('app-screen').classList.add('hidden');
  document.getElementById('auth-screen').classList.remove('hidden');
  // Баннер деградации относится к активной сессии — на экране входа он не нужен.
  stopDegradationBanner();
  try {
    realtime.close();
  } catch {
    /* ignore */
  }
}

boot().catch((error) => {
  console.error('[boot] критическая ошибка', error);
  popup.error('Ошибка запуска интерфейса', error.message);
});
