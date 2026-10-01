/* ============================================================
   Экран авторизации: Вход / Регистрация (docs/03_API_CONTRACTS.md §2).
   ============================================================ */

import { api, ApiError } from '../core/api.js';
import { session } from '../core/session.js';
import { CONFIG } from '../config.js';
import { popup } from '../components/fadeout-action-popup.js';

let onAuthed = null;
let initialized = false;

function describeError(error) {
  if (error instanceof ApiError) {
    if (error.status === 0) return 'Backend недоступен. Запустите сервер или откройте интерфейс с ?demo=1.';
    if (error.status === 401) return 'Неверный email или пароль.';
    if (error.status === 409) return 'Пользователь с таким email уже зарегистрирован.';
    return error.message;
  }
  return 'Непредвиденная ошибка. Попробуйте ещё раз.';
}

function setLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  const spinner = button.querySelector('[data-spinner]');
  spinner?.classList.toggle('hidden', !loading);
}

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function initAuth(callback) {
  if (initialized) return;
  initialized = true;
  onAuthed = callback;

  const tabs = document.querySelectorAll('[data-auth-tab]');
  const loginForm = document.getElementById('login-form');
  const registerForm = document.getElementById('register-form');

  const setTab = (tab) => {
    tabs.forEach((button) => {
      const active = button.dataset.authTab === tab;
      button.setAttribute('aria-selected', String(active));
      button.classList.toggle('bg-indigo-600', active);
      button.classList.toggle('text-white', active);
      button.classList.toggle('text-slate-400', !active);
    });
    loginForm.classList.toggle('hidden', tab !== 'login');
    registerForm.classList.toggle('hidden', tab !== 'register');
  };

  tabs.forEach((button) => button.addEventListener('click', () => setTab(button.dataset.authTab)));
  setTab('login');

  if (CONFIG.DEMO) {
    document.getElementById('demo-auth-hint')?.classList.remove('hidden');
    document.getElementById('login-email').value = 'demo@career.local';
    document.getElementById('login-password').value = 'demo-password';
  }

  /* --- Вход --- */
  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;

    if (!EMAIL_PATTERN.test(email)) {
      popup.warning('Проверьте email', 'Укажите корректный email.');
      return;
    }
    if (!password) {
      popup.warning('Введите пароль', 'Пароль не может быть пустым.');
      return;
    }

    const button = document.getElementById('login-submit');
    setLoading(button, true);
    try {
      await api.login(email, password);
      const user = await api.me().catch(() => null);
      if (user) session.setUser(user);
      onAuthed?.(user);
    } catch (error) {
      popup.error('Не удалось войти', describeError(error));
    } finally {
      setLoading(button, false);
    }
  });

  /* --- Регистрация --- */
  registerForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const email = document.getElementById('register-email').value.trim();
    const password = document.getElementById('register-password').value;

    if (!EMAIL_PATTERN.test(email)) {
      popup.warning('Проверьте email', 'Укажите корректный email.');
      return;
    }
    if (password.length < 8) {
      popup.warning('Слишком короткий пароль', 'Минимум 8 символов.');
      return;
    }

    const button = document.getElementById('register-submit');
    setLoading(button, true);
    try {
      await api.register(email, password);
      popup.success('Аккаунт создан', 'Теперь войдите с указанными данными.');
      document.getElementById('login-email').value = email;
      document.getElementById('login-password').value = '';
      setTab('login');
    } catch (error) {
      popup.error('Не удалось зарегистрироваться', describeError(error));
    } finally {
      setLoading(button, false);
    }
  });
}
