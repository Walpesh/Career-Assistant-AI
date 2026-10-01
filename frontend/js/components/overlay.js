/* ============================================================
   Оверлеи: модалки и drawer-панели (управление через data-open),
   общий confirm-диалог на базе partials/modals.html.
   ============================================================ */

let pendingConfirm = null;
let lastFocused = null;

export function openOverlay(id) {
  const element = document.getElementById(id);
  if (!element) return;
  lastFocused = document.activeElement;
  element.dataset.open = '1';
  document.body.classList.add('overflow-hidden');

  const focusTarget =
    element.querySelector('[data-autofocus]') ||
    element.querySelector('.btn-primary, .btn-secondary, [data-close]');
  focusTarget?.focus?.();
}

export function closeOverlay(id) {
  const element = document.getElementById(id);
  if (!element) return;
  element.dataset.open = '0';
  if (!document.querySelector('.overlay[data-open="1"]')) {
    document.body.classList.remove('overflow-hidden');
  }
  lastFocused?.focus?.();
}

export function closeAllOverlays() {
  document.querySelectorAll('.overlay[data-open="1"]').forEach((element) => closeOverlay(element.id));
}

/**
 * Универсальный confirm-диалог.
 * @returns {Promise<boolean>}
 */
export function confirmDialog({ title = 'Подтвердите действие', message = '', confirmLabel = 'Подтвердить', danger = false } = {}) {
  const modal = document.getElementById('confirm-modal');
  if (!modal) return Promise.resolve(window.confirm(message || title));

  modal.querySelector('#confirm-title').textContent = title;
  modal.querySelector('#confirm-message').textContent = message;

  const okButton = modal.querySelector('#btn-confirm-ok');
  okButton.textContent = confirmLabel;
  okButton.className = danger ? 'btn-danger' : 'btn-primary';

  openOverlay('confirm-modal');

  return new Promise((resolve) => {
    pendingConfirm = resolve;
  });
}

function resolveConfirm(result) {
  if (!pendingConfirm) return;
  const resolve = pendingConfirm;
  pendingConfirm = null;
  closeOverlay('confirm-modal');
  resolve(result);
}

/** Делегирование: клики по [data-close], Escape, кнопки confirm-диалога. */
export function bindOverlays() {
  document.addEventListener('click', (event) => {
    const closeTrigger = event.target.closest('[data-close]');
    if (closeTrigger) {
      const id = closeTrigger.dataset.close;
      if (id === 'confirm-modal') {
        resolveConfirm(false);
      } else {
        closeOverlay(id);
      }
      return;
    }

    if (event.target.closest('#btn-confirm-ok')) {
      resolveConfirm(true);
      return;
    }
    if (event.target.closest('#btn-confirm-cancel')) {
      resolveConfirm(false);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (pendingConfirm) {
      resolveConfirm(false);
      return;
    }
    closeAllOverlays();
  });
}
