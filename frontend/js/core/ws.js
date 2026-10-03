/* ============================================================
   WebSocket-клиент реал-тайм событий (docs/03_API_CONTRACTS.md §8).
   Подключение: {api-base}/ws?ticket=<one-time-ticket> (POST /auth/ws-ticket)
   События сервера: task.*, vacancy.updated, analysis.ready, letter.ready, popup.
   Клиент только слушает; команды — через REST.

   Обработчики привязаны к конкретному сокету: если соединение заменено
   (повторный connect или реконнект), его onclose/onmessage игнорируются.
   Иначе устаревший сокет планировал бы ещё один реконнект поверх уже
   открытого — это и вызывало циклические «переподключения» страницы.

   Mobile Safari: iOS держит сокет «живым» в API, но при уходе в фон
   замораживает таймеры и закрывает канал, не вызывая onclose. Поэтому
   добавлены три страховки (CONFIG.WS_STALE_TIMEOUT_MS):
     - watchdog по «молчанию» канала (send не бросает, а пинг-понг зависает);
     - немедленный реконнект на visibilitychange/online;
     - сброс экспоненциальной задержки при возврате на вкладку.
   ============================================================ */

import { CONFIG, buildWsUrl } from '../config.js';
import { emit } from './bus.js';

/** Сервер закрывает канал: access-токен невалиден/истёк (docs/03 §2). */
const CLOSE_AUTH_ERROR = 4401;
/** Сервер закрывает канал: подключение вытеснено более новым. */
const CLOSE_REPLACED = 4001;

export class RealtimeClient {
  /**
   * @param {{ onUnauthorized?: () => void }} [options]
   *   onUnauthorized — вызывается при закрытии 4401 (токен истёк);
   *   по умолчанию эмитится auth:expired (см. main.js: пробуем refresh).
   */
  constructor(options = {}) {
    this.socket = null;
    this.attempt = 0;
    this.manualClose = false;
    this.reconnectTimer = null;
    this.heartbeatTimer = null;
    this.status = 'disconnected';
    this.isAuthenticated = true; // Флаг для отслеживания, нужно ли переподключение
    this.everConnected = false;
    // Провайдер одноразового WS-тикета (docs/03 §8): вызывается при каждом
    // подключении/переподключении, т.к. тикет одноразовый (Redis GETDEL).
    this.getTicket = options.getTicket || (async () => null);
    this.onUnauthorized = options.onUnauthorized || (() => emit('auth:expired'));

    this.lastMessageAt = 0;
    this.watchdogTimer = null;
    this.listenersBound = false;
    this.bindLifecycle();
  }

  /**
   * Страховки для mobile Safari (iOS закрывает WS в фоне, не вызывая onclose).
   * Слушатели вешаются один раз на весь жизненный цикл клиента.
   */
  bindLifecycle() {
    if (this.listenersBound || typeof window === 'undefined') return;
    this.listenersBound = true;

    // Возврат на вкладку: iOS мог закрыть канал. Сбрасываем backoff и
    // переподключаемся сразу, не дожидаясь 15-секундной задержки.
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      if (this.manualClose || !this.isAuthenticated) return;
      if (this.socket && this.socket.readyState === WebSocket.OPEN) {
        // Канал выглядит живым, но мог «заморозиться» — шлём пинг.
        this.sendPing();
        return;
      }
      this.attempt = 0;
      this.open();
    });

    // Сеть вернулась (вылет из фона, смена Wi-Fi/LTE).
    window.addEventListener('online', () => {
      if (this.manualClose || !this.isAuthenticated) return;
      this.attempt = 0;
      this.open();
    });

    // Уход со страницы: закрываем канал явно, чтобы не оставлять «висящий».
    window.addEventListener('pagehide', () => this.close());
  }

  connect() {
    this.manualClose = false;
    this.isAuthenticated = true;
    this.open();
  }

  async open() {
    // Не пытаемся подключиться при ручном закрытии / ошибке аутентификации.
    if (this.manualClose || !this.isAuthenticated) return;

    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;

    // Забываем прежнее соединение ДО close(): его обработчики уже сняты,
    // поэтому onclose не инициирует лишний реконнект поверх нового сокета.
    this.dropSocket();

    this.setStatus('connecting');

    // Одноразовый тикет запрашивается заново на каждое подключение.
    let ticket;
    try {
      ticket = await this.getTicket();
    } catch {
      ticket = null;
    }
    if (this.manualClose || !this.isAuthenticated) return;
    if (!ticket) {
      this.scheduleReconnect();
      return;
    }

    let socket;
    try {
      socket = new WebSocket(buildWsUrl(ticket));
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      if (socket !== this.socket) return; // соединение уже заменено
      this.attempt = 0;
      this.lastMessageAt = Date.now();
      this.startHeartbeat();
      this.startWatchdog();
      this.setStatus('connected');
      emit('log:system', { level: 'info', message: 'WebSocket-соединение установлено' });
      // После переподключения события могли потеряться — нужна пересинхронизация.
      if (this.everConnected) emit('ws:resync');
      this.everConnected = true;
    };

    socket.onmessage = (event) => {
      if (socket !== this.socket) return;
      // Любой кадр, включая pong, доказывает, что канал жив.
      this.lastMessageAt = Date.now();
      this.handleMessage(event.data);
    };

    socket.onclose = (event) => {
      if (socket !== this.socket) return; // устаревший сокет — не наше дело
      this.socket = null;
      this.stopHeartbeat();
      this.stopWatchdog();
      this.setStatus('disconnected');

      if (this.manualClose) return; // штатное закрытие (логаут/выход)

      // 4401 — access-токен невалиден: переподключение не поможет.
      if (event.code === CLOSE_AUTH_ERROR) {
        this.isAuthenticated = false;
        this.onUnauthorized();
        return;
      }
      // Соединение вытеснено более новым — переподключение снова вытеснило бы
      // его и запустило ping-pong, поэтому не переподключаемся.
      if (event.code === CLOSE_REPLACED) {
        emit('log:system', { level: 'warning', message: 'WebSocket вытеснен более новым подключением' });
        return;
      }

      emit('log:system', { level: 'warning', message: 'WebSocket-соединение потеряно, переподключение…' });
      this.scheduleReconnect();
    };

    // Ошибка всегда сопровождается onclose — реконнект выполняет он.
    socket.onerror = () => {};
  }

  handleMessage(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (!message || !message.event) return;
    if (message.event === 'pong') return; // heartbeat — в журнал не пишем

    // Поддерживаем оба формата сервера: { event, ...payload } и { event, data: {...} }.
    const { event, data, ...rest } = message;
    const payload = data !== undefined ? data : rest;

    emit('ws:event', { event, payload });
    emit(`ws:${event}`, payload);
  }

  /** Heartbeat: клиент шлёт ping, сервер отвечает pong (docs/03 §8). */
  startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => this.sendPing(), CONFIG.WS_HEARTBEAT_INTERVAL_MS);
  }

  stopHeartbeat() {
    clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  /** Отправить ping, если канал открыт. Ошибки проглотит onclose. */
  sendPing() {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    try {
      socket.send(JSON.stringify({ event: 'ping' }));
    } catch {
      /* ignore — о разрыве сообщит onclose или watchdog */
    }
  }

  /**
   * Watchdog «молчания» канала — страховка для mobile Safari.
   *
   * В фоне iOS замораживает таймеры и может закрыть сокет, не вызвав onclose:
   * readyState остаётся OPEN, send() не бросает, а данных не приходит.
   * Такой канал выглядит живым, но прогресс задач перестаёт обновляться.
   *
   * Если за WS_STALE_TIMEOUT_MS не пришёл ни один кадр (включая pong) —
   * считаем канал мёртвым и переподключаемся принудительно.
   */
  startWatchdog() {
    this.stopWatchdog();
    this.watchdogTimer = setInterval(() => {
      if (this.manualClose || !this.socket) return;
      // В фоне таймеры всё равно заморожены — проверка бессмысленна.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
      if (Date.now() - this.lastMessageAt < CONFIG.WS_STALE_TIMEOUT_MS) return;

      emit('log:system', {
        level: 'warning',
        message: 'WebSocket не отвечает — переподключение принудительно'
      });
      // dropSocket снимает обработчики, поэтому onclose не запустит
      // второй реконнект поверх уже запланированного.
      this.dropSocket();
      this.attempt = 0;
      this.setStatus('disconnected');
      this.open();
    }, CONFIG.WS_WATCHDOG_INTERVAL_MS);
  }

  stopWatchdog() {
    clearInterval(this.watchdogTimer);
    this.watchdogTimer = null;
  }

  scheduleReconnect() {
    // Не переподключаемся, если это ручное закрытие или ошибка аутентификации
    if (this.manualClose || !this.isAuthenticated) return;

    clearTimeout(this.reconnectTimer);
    const delay = Math.min(CONFIG.RECONNECT_MAX_DELAY_MS, 1000 * 2 ** this.attempt) + Math.random() * 400;
    this.attempt = Math.min(this.attempt + 1, 5);
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  setStatus(status) {
    if (this.status === status) return; // индикатор не «дёргается» зря
    this.status = status;
    emit('ws:status', status);
  }

  /** Снять обработчики и закрыть текущее соединение (если есть). */
  dropSocket() {
    const socket = this.socket;
    this.socket = null;
    this.stopHeartbeat();
    this.stopWatchdog();
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    try {
      socket.close();
    } catch {
      /* ignore */
    }
  }

  close() {
    this.manualClose = true;
    this.isAuthenticated = false;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.stopHeartbeat();
    this.stopWatchdog();
    this.dropSocket();
    this.setStatus('disconnected');
  }
}
