/* ============================================================
   Bootstrap Career-Assistant-AI:
   1) подключает оверлеи и журнал; 2) демо-режим (?demo=1);
   3) проверяет сессию; 4) запускает приложение и WebSocket.
   ============================================================ */

import { CONFIG } from './config.js';
import { session } from './core/session.js';
import { api } from './core/api.js';
import { on } from './core/bus.js';
import { setState } from './core/state.js';
import { initTaskTracking } from './core/tasks.js';
import { loadPartial } from './core/partials.js';
import { RealtimeClient } from './core/ws.js';
import { DemoSocket, installMock } from './core/mock.js';
import { popup } from './components/fadeout-action-popup.js';
import { bindOverlays } from './components/overlay.js';
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
    if (data?.access_token && session.accessToken) {
      realtime.connect(session.accessToken);
      return;
    }
  } catch (error) {
    console.warn('[ws] не удалось обновить токен:', error.message);
  }
  showAuthScreen();
  popup.warning('Сессия истекла', 'Войдите в аккаунт заново.');
}

const realtime = CONFIG.DEMO
  ? new DemoSocket()
  : new RealtimeClient({ onUnauthorized: handleWsUnauthorized });

async function boot() {
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

  // WS-событие `popup` → кастомный Fadeout-action-popup (docs/03 §8).
  on('ws:popup', (payload = {}) => {
    popup.show({ type: payload.type || 'info', title: payload.title || 'Уведомление', message: payload.message || '' });
  });

  // Истёкшая сессия → возвращаем на экран входа.
  on('auth:expired', () => {
    showAuthScreen();
    popup.warning('Сессия истекла', 'Войдите в аккаунт заново.');
  });

  // Переподключение WS: события за время обрыва потеряны — обновляем профиль
  // (порог матчинга) и просим вкладки перечитать данные.
  on('ws:resync', () => {
    api.getProfile()
      .then((profile) => setState({ profile }))
      .catch((error) => console.warn('[ws] профиль не обновлён:', error.message));
  });

  authView.initAuth(() => enterApp());

  if (session.isAuthed()) {
    try {
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

  realtime.connect(session.accessToken);
  await initTaskTracking();

  // Профиль нужен для порога матчинга и настроек — загружаем в фоне.
  api.getProfile()
    .then((profile) => setState({ profile }))
    .catch((error) => console.warn('[boot] профиль не загружен:', error.message));

  await shell.start();
}

function showAuthScreen() {
  document.getElementById('app-screen').classList.add('hidden');
  document.getElementById('auth-screen').classList.remove('hidden');
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
