/* ============================================================
   Внутренняя шина событий (EventTarget + helpers).
   Используется для WS-событий, статусов UI и уведомлений.
   ============================================================ */

const bus = new EventTarget();

/** Отправить событие в шину. */
export function emit(name, detail) {
  bus.dispatchEvent(new CustomEvent(name, { detail }));
}

/** Подписаться на событие. Возвращает функцию отписки. */
export function on(name, callback) {
  const handler = (event) => callback(event.detail);
  bus.addEventListener(name, handler);
  return () => bus.removeEventListener(name, handler);
}

export default bus;
