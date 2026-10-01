/* ============================================================
   TagInput — ввод тегов (ключевые слова автопоиска, навыки профиля).
   Добавление: Enter / «,» / «;» / blur. Удаление: × или Backspace в пустом поле.
   ============================================================ */

import { escapeHtml } from '../core/utils.js';

export class TagInput {
  /**
   * @param {HTMLElement} root контейнер (класс tag-input-root навешивается автоматически)
   * @param {{placeholder?:string,max?:number,onChange?:(values:string[])=>void}} options
   */
  constructor(root, options = {}) {
    this.root = root;
    this.max = options.max ?? 30;
    this.onChange = options.onChange || (() => {});
    this.values = [];

    this.root.classList.add('tag-input-root');
    this.root.innerHTML = `<input type="text" autocomplete="off" spellcheck="false" placeholder="${escapeHtml(options.placeholder || '')}" />`;
    this.input = this.root.querySelector('input');

    this._bindEvents();
    this.render();
  }

  _bindEvents() {
    this.root.addEventListener('click', (event) => {
      if (event.target === this.root) this.input.focus();
    });

    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ',' || event.key === ';') {
        event.preventDefault();
        this.add(this.input.value);
      } else if (event.key === 'Backspace' && this.input.value === '' && this.values.length) {
        this.removeAt(this.values.length - 1);
      }
    });

    this.input.addEventListener('blur', () => {
      if (this.input.value.trim()) this.add(this.input.value);
    });
  }

  add(raw) {
    const value = String(raw || '').replace(/[;,]+$/, '').trim();
    if (!value) return false;
    const exists = this.values.some((item) => item.toLowerCase() === value.toLowerCase());
    if (exists) {
      this.input.value = '';
      return false;
    }
    if (this.values.length >= this.max) {
      this.input.value = '';
      return false;
    }
    this.values.push(value);
    this.input.value = '';
    this.render();
    this.onChange(this.getValues());
    return true;
  }

  removeAt(index) {
    if (index < 0 || index >= this.values.length) return;
    this.values.splice(index, 1);
    this.render();
    this.onChange(this.getValues());
  }

  setValues(list) {
    this.values = (Array.isArray(list) ? list : [])
      .map((item) => String(item).trim())
      .filter(Boolean)
      .slice(0, this.max);
    this.render();
  }

  getValues() {
    return this.values.slice();
  }

  render() {
    const chips = this.values.map((value, index) => {
      const chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.innerHTML = `<span>${escapeHtml(value)}</span><button type="button" data-tag-remove="${index}" aria-label="Удалить ${escapeHtml(value)}">✕</button>`;
      return chip;
    });

    this.root.replaceChildren(...chips, this.input);
    this.root.querySelectorAll('[data-tag-remove]').forEach((button) => {
      button.addEventListener('click', () => this.removeAt(Number(button.dataset.tagRemove)));
    });
  }
}
