/* ============================================================
   Сессия пользователя.

   - ACCESS-токен хранится ТОЛЬКО в памяти (не в localStorage) —
     XSS не может его украсть из хранилища.
   - REFRESH-токен хранится в HttpOnly; Secure; SameSite=Strict
     cookie на стороне backend и из JS недоступен.
   - Профиль пользователя — в localStorage (это не секрет) и
     служит подсказкой, что сессию можно восстановить через refresh.
   ============================================================ */

const KEY_USER = 'ca:user';

// access-токен живёт в замыкании модуля: при перезагрузке страницы он
// теряется, и клиент восстанавливает сессию через POST /auth/refresh (cookie).
let accessToken = null;

export const session = {
  get accessToken() {
    return accessToken;
  },

  /** Refresh-токен из JS недоступен (HttpOnly cookie) — оставлено для совместимости. */
  get refreshToken() {
    return null;
  },

  get user() {
    try {
      return JSON.parse(localStorage.getItem(KEY_USER) || 'null');
    } catch {
      return null;
    }
  },

  isAuthed() {
    return Boolean(accessToken);
  },

  /**
   * Подсказка, что сессия могла сохраниться: профиль в localStorage есть,
   * но access-токена в памяти ещё нет — bootstrap попробует refresh-cookie.
   */
  hasSessionHint() {
    return Boolean(this.user);
  },

  setTokens({ access_token } = {}) {
    if (access_token) accessToken = access_token;
  },

  setUser(user) {
    localStorage.setItem(KEY_USER, JSON.stringify(user || null));
  },

  clear() {
    accessToken = null;
    localStorage.removeItem(KEY_USER);
  }
};
