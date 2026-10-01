/* ============================================================
   Кастомный компонент <fadeout-action-popup>
   Неблокирующие уведомления (toast) с автоматическим fade-out:
   - стек до 5 карточек, дедупликация одинаковых сообщений;
   - таймер-полоска прогресса, пауза при наведении;
   - типы: info | success | warning | error;
   - опциональная кнопка действия (options.action);
   - история последних уведомлений (getHistory()).

   Также используется как приёмник WS-события `popup`
   (docs/03_API_CONTRACTS.md §8): { type, title, message }.
   ============================================================ */

import { emit } from '../core/bus.js';
import { escapeHtml, uid } from '../core/utils.js';

const ICONS = {
  success: '<path fill-rule="evenodd" d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zm3.857-9.809a.75.75 0 0 0-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 1 0-1.06 1.061l2.5 2.5a.75.75 0 0 0 1.137-.089l4-5.5z" clip-rule="evenodd"/>',
  error: '<path fill-rule="evenodd" d="M10 18a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM8.28 7.22a.75.75 0 0 0-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 1 0 1.06 1.06L10 11.06l1.72 1.72a.75.75 0 1 0 1.06-1.06L11.06 10l1.72-1.72a.75.75 0 0 0-1.06-1.06L10 8.94 8.28 7.22z" clip-rule="evenodd"/>',
  warning: '<path fill-rule="evenodd" d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 0 1 .75.75v3.5a.75.75 0 0 1-1.5 0v-3.5A.75.75 0 0 1 10 5zm0 9a1 1 0 1 0 0-2 1 1 0 0 0 0 2z" clip-rule="evenodd"/>',
  info: '<path fill-rule="evenodd" d="M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0zm-7-4a1 1 0 1 1-2 0 1 1 0 0 1 2 0zM9 9a.75.75 0 0 0 0 1.5h.253a.25.25 0 0 1 .244.304l-.459 2.066A1.75 1.75 0 0 0 10.747 15H11a.75.75 0 0 0 0-1.5h-.253a.25.25 0 0 1-.244-.304l.459-2.066A1.75 1.75 0 0 0 9.253 9H9z" clip-rule="evenodd"/>'
};

const TYPES = {
  info:    { accent: 'bg-sky-500',     icon: 'text-sky-400',     progress: 'text-sky-500',     duration: 5000, title: 'Информация' },
  success: { accent: 'bg-emerald-500', icon: 'text-emerald-400', progress: 'text-emerald-500', duration: 4000, title: 'Готово' },
  warning: { accent: 'bg-amber-500',   icon: 'text-amber-400',   progress: 'text-amber-500',   duration: 6000, title: 'Внимание' },
  error:   { accent: 'bg-rose-500',    icon: 'text-rose-400',    progress: 'text-rose-500',    duration: 8000, title: 'Ошибка' }
};

const MAX_VISIBLE = 5;
const HISTORY_LIMIT = 50;

export class FadeoutActionPopup extends HTMLElement {
  constructor() {
    super();
    this.items = new Map();
    this.history = [];
  }

  connectedCallback() {
    this.setAttribute('role', 'region');
    if (!this.hasAttribute('aria-live')) this.setAttribute('aria-live', 'polite');
  }

  /**
   * Показать уведомление.
   * @param {{type?:string,title?:string,message?:string,duration?:number,sticky?:boolean,action?:{label:string,onClick:Function}}} options
   * @returns {string} id уведомления
   */
  show(options = {}) {
    const type = TYPES[options.type] ? options.type : 'info';
    const config = TYPES[type];
    const title = options.title || config.title;
    const message = options.message || '';
    const sticky = Boolean(options.sticky) || Number(options.duration) < 0;
    const baseDuration = options.duration ?? config.duration + Math.min(8000, Math.max(0, message.length - 60) * 40);

    // Дедупликация: одинаковое сообщение уже на экране — перезапускаем таймер.
    const key = `${type}|${title}|${message}`;
    for (const existing of this.items.values()) {
      if (existing.key === key) {
        this._arm(existing, sticky ? 0 : baseDuration);
        return existing.id;
      }
    }

    if (this.items.size >= MAX_VISIBLE) {
      this.dismiss(this.items.keys().next().value, true);
    }

    const id = uid();
    const element = document.createElement('div');
    element.className = 'fap-item';
    element.dataset.popupId = id;
    element.dataset.type = type;
    element.setAttribute('role', type === 'error' ? 'alert' : 'status');
    element.innerHTML = `
      <span class="fap-accent ${config.accent}"></span>
      <div class="flex items-start gap-3">
        <svg class="mt-0.5 h-5 w-5 shrink-0 ${config.icon}" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">${ICONS[type]}</svg>
        <div class="min-w-0 flex-1">
          <p class="text-sm font-semibold text-slate-100">${escapeHtml(title)}</p>
          ${message ? `<p class="mt-0.5 text-xs leading-relaxed text-slate-400">${escapeHtml(message)}</p>` : ''}
          ${options.action ? `<button type="button" data-popup-action class="mt-2 rounded-md border border-slate-700 bg-slate-800/80 px-2 py-1 text-[11px] font-medium text-slate-200 transition hover:bg-slate-700/80">${escapeHtml(options.action.label)}</button>` : ''}
        </div>
        <button type="button" data-popup-close class="shrink-0 rounded-md p-1 text-slate-500 transition hover:bg-slate-800 hover:text-slate-200" aria-label="Закрыть уведомление">
          <svg class="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22z"/></svg>
        </button>
      </div>
      <span class="fap-progress ${config.progress}"></span>`;

    const item = {
      id,
      key,
      element,
      duration: sticky ? 0 : baseDuration,
      timer: null,
      startedAt: Date.now(),
      progressElement: element.querySelector('.fap-progress')
    };

    element.querySelector('[data-popup-close]').addEventListener('click', () => this.dismiss(id));
    element.querySelector('[data-popup-action]')?.addEventListener('click', () => {
      try {
        options.action.onClick?.();
      } finally {
        this.dismiss(id);
      }
    });

    // Пауза таймера при наведении курсора.
    element.addEventListener('mouseenter', () => this._pause(item));
    element.addEventListener('mouseleave', () => this._resume(item));

    this.appendChild(element);
    this.items.set(id, item);
    this._arm(item, item.duration);

    this.history.unshift({ type, title, message, at: new Date().toISOString() });
    if (this.history.length > HISTORY_LIMIT) this.history.length = HISTORY_LIMIT;

    emit('popup:shown', { id, type, title, message });
    return id;
  }

  info(title, message, options = {}) { return this.show({ ...options, type: 'info', title, message }); }
  success(title, message, options = {}) { return this.show({ ...options, type: 'success', title, message }); }
  warning(title, message, options = {}) { return this.show({ ...options, type: 'warning', title, message }); }
  error(title, message, options = {}) { return this.show({ ...options, type: 'error', title, message }); }

  /** Закрыть уведомление с анимацией fade-out. */
  dismiss(id, immediate = false) {
    const item = this.items.get(id);
    if (!item) return;
    clearTimeout(item.timer);
    this.items.delete(id);
    emit('popup:dismissed', { id });

    if (immediate) {
      item.element.remove();
      return;
    }

    item.element.classList.add('fap-leaving');
    const remove = () => item.element.remove();
    item.element.addEventListener('animationend', remove, { once: true });
    setTimeout(remove, 400); // страховка, если animationend не сработал
  }

  clear() {
    [...this.items.keys()].forEach((id) => this.dismiss(id, true));
  }

  getHistory() {
    return this.history.slice();
  }

  _arm(item, duration) {
    clearTimeout(item.timer);
    if (!duration || duration <= 0) {
      item.duration = 0;
      item.timer = null;
      item.progressElement?.remove();
      return;
    }
    item.duration = duration;
    item.startedAt = Date.now();
    this._restartProgress(item.progressElement, duration);
    item.timer = setTimeout(() => this.dismiss(item.id), duration);
  }

  _pause(item) {
    if (!item.timer) return;
    clearTimeout(item.timer);
    item.timer = null;
    item.remaining = item.duration - (Date.now() - item.startedAt);
  }

  _resume(item) {
    if (item.timer || item.duration <= 0) return;
    this._arm(item, Math.max(1200, item.remaining ?? item.duration));
  }

  _restartProgress(progressElement, durationMs) {
    if (!progressElement) return;
    progressElement.style.animation = 'none';
    void progressElement.offsetWidth; // принудительный reflow для сброса анимации
    progressElement.style.animation = '';
    progressElement.style.animationDuration = `${durationMs}ms`;
  }
}

if (!customElements.get('fadeout-action-popup')) {
  customElements.define('fadeout-action-popup', FadeoutActionPopup);
}

/**
 * Синглтон-обёртка для использования из любого модуля:
 * popup.success('Заголовок', 'Текст сообщения');
 */
export const popup = {
  host() {
    let element = document.getElementById('popups');
    if (!element) {
      element = document.createElement('fadeout-action-popup');
      element.id = 'popups';
      document.body.appendChild(element);
    }
    return element;
  },
  show(options) { return this.host().show(options); },
  info(title, message, options) { return this.host().info(title, message, options); },
  success(title, message, options) { return this.host().success(title, message, options); },
  warning(title, message, options) { return this.host().warning(title, message, options); },
  error(title, message, options) { return this.host().error(title, message, options); },
  clear() { this.host().clear(); },
  history() { return this.host().getHistory(); }
};

