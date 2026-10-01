/* ============================================================
   WebSocket-клиент реал-тайм событий (docs/03_API_CONTRACTS.md §8).
   Подключение: {api-base}/ws?token=<access_token>
   События сервера: task.*, vacancy.updated, analysis.ready, letter.ready, popup.
   Клиент только слушает; команды — через REST.
   ============================================================ */

import { CONFIG, buildWsUrl } from '../config.js';
import { emit } from './bus.js';

export class RealtimeClient {
  constructor() {
    this.socket = null;
    this.token = null;
    this.attempt = 0;
    this.manualClose = false;
    this.reconnectTimer = null;
    this.status = 'disconnected';
  }

  connect(token) {
    this.token = token;
    this.manualClose = false;
    this.open();
  }

  open() {
    if (!this.token || this.manualClose) return;
    if (this.socket && (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)) return;

    this.setStatus('connecting');

    try {
      this.socket = new WebSocket(buildWsUrl(this.token));
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.socket.onopen = () => {
      this.attempt = 0;
      this.setStatus('connected');
      emit('log:system', { level: 'info', message: 'WebSocket-соединение установлено' });
    };

    this.socket.onmessage = (event) => this.handleMessage(event.data);

    this.socket.onclose = () => {
      this.setStatus('disconnected');
      if (!this.manualClose) {
        emit('log:system', { level: 'warning', message: 'WebSocket-соединение потеряно, переподключение…' });
        this.scheduleReconnect();
      }
    };

    // Ошибка всегда сопровождается onclose — реконнект выполняет он.
    this.socket.onerror = () => {};
  }

  handleMessage(raw) {
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (!message || !message.event) return;

    // Поддерживаем оба формата сервера: { event, ...payload } и { event, data: {...} }.
    const { event, data, ...rest } = message;
    const payload = data !== undefined ? data : rest;

    emit('ws:event', { event, payload });
    emit(`ws:${event}`, payload);
  }

  scheduleReconnect() {
    clearTimeout(this.reconnectTimer);
    const delay = Math.min(CONFIG.RECONNECT_MAX_DELAY_MS, 1000 * 2 ** this.attempt) + Math.random() * 400;
    this.attempt = Math.min(this.attempt + 1, 5);
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  setStatus(status) {
    this.status = status;
    emit('ws:status', status);
  }

  close() {
    this.manualClose = true;
    clearTimeout(this.reconnectTimer);
    try {
      this.socket?.close();
    } catch {
      /* ignore */
    }
    this.socket = null;
    this.setStatus('disconnected');
  }
}
