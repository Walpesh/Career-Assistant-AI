/* ============================================================
   Сессия пользователя: JWT (access/refresh) и профиль в localStorage.
   ============================================================ */

const KEYS = {
  access: 'ca:access_token',
  refresh: 'ca:refresh_token',
  user: 'ca:user'
};

export const session = {
  get accessToken() {
    return localStorage.getItem(KEYS.access);
  },

  get refreshToken() {
    return localStorage.getItem(KEYS.refresh);
  },

  get user() {
    try {
      return JSON.parse(localStorage.getItem(KEYS.user) || 'null');
    } catch {
      return null;
    }
  },

  isAuthed() {
    return Boolean(this.accessToken);
  },

  setTokens({ access_token, refresh_token } = {}) {
    if (access_token) localStorage.setItem(KEYS.access, access_token);
    // Refresh-токен опционален в ответе — не затираем существующий пустым значением.
    if (refresh_token) localStorage.setItem(KEYS.refresh, refresh_token);
  },

  setUser(user) {
    localStorage.setItem(KEYS.user, JSON.stringify(user || null));
  },

  clear() {
    localStorage.removeItem(KEYS.access);
    localStorage.removeItem(KEYS.refresh);
    localStorage.removeItem(KEYS.user);
  }
};
