/* ============================================================
   Экран авторизации: Вход / Регистрация / Подтверждение email
   (docs/03_API_CONTRACTS.md §2).

   Регистрация — двухшаговая (ТЗ «Email OTP Code Verification»):
     шаг 1  POST /auth/register     → код из 6 цифр уходит на email, JWT не выдаётся;
     шаг 2  POST /auth/verify-email → пара JWT и автоматический вход в приложение.
   ============================================================ */

import { api, ApiError } from '../core/api.js';
import { session } from '../core/session.js';
import { CONFIG } from '../config.js';
import { popup } from '../components/fadeout-action-popup.js';

const OTP_LENGTH = 6;
/** Интервал повторной отправки кода — как на сервере (OTP_RESEND_INTERVAL_SECONDS). */
const RESEND_COOLDOWN_SECONDS = 60;

let onAuthed = null;
let initialized = false;

/** Email, для которого сейчас вводится код (шаг 2). */
let pendingEmail = '';
/** Активный таймер обратного отсчёта resend (id таймера). */
let resendTimer = null;
/** Input'ы отдельных цифр OTP (создаются в initAuth). */
const digits = [];

function stopResendTimer() {
  if (resendTimer !== null) {
    clearInterval(resendTimer);
    resendTimer = null;
  }
}

function clearOtp() {
  digits.forEach((input) => {
    input.value = '';
  });
}

function describeError(error) {
  if (error instanceof ApiError) {
    if (error.status === 0) return 'Backend недоступен. Запустите сервер или откройте интерфейс с ?demo=1.';
    if (error.status === 401) return 'Неверный email или пароль.';
    if (error.status === 409) return 'Пользователь с таким email уже зарегистрирован.';
    if (error.status === 403 && error.errorCode === 'EMAIL_NOT_VERIFIED') {
      return 'Email ещё не подтверждён — введите код из письма.';
    }
    // Ошибки OTP приходят с текстом от сервера (сколько попыток осталось,
    // срок кода истёк и т.п.) — показываем его как есть.
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
  const verifyForm = document.getElementById('verify-form');
  const otpInputs = document.getElementById('otp-inputs');
  const verifyEmailLabel = document.getElementById('verify-email');
  const resendButton = document.getElementById('resend-code');
  const resendTimerLabel = document.getElementById('resend-timer');

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
    // Шаг 2 — не вкладка, а отдельный экран: на время ввода кода вкладки
    // скрываются, иначе можно уйти на «Вход» с неподтверждённым аккаунтом.
    const verifying = tab === 'verify';
    verifyForm.classList.toggle('hidden', !verifying);
    document.getElementById('auth-tabs')?.classList.toggle('hidden', verifying);
  };

  tabs.forEach((button) => button.addEventListener('click', () => setTab(button.dataset.authTab)));
  setTab('login');

  if (CONFIG.DEMO) {
    document.getElementById('demo-auth-hint')?.classList.remove('hidden');
    document.getElementById('login-email').value = 'demo@career.local';
    document.getElementById('login-password').value = 'demo-password';
  }

  /* ==========================================================
     Поля ввода OTP: шесть отдельных input'ов по одной цифре
     ========================================================== */

  function buildOtpInputs() {
    otpInputs.innerHTML = '';
    digits.length = 0;
    for (let index = 0; index < OTP_LENGTH; index += 1) {
      const input = document.createElement('input');
      input.type = 'text';
      input.inputMode = 'numeric';
      // one-time-code только на первом поле: иначе мобильные браузеры
      // подставляют значение в каждое поле сразу (автозаполнение OTP из SMS).
      input.autocomplete = index === 0 ? 'one-time-code' : 'off';
      input.maxLength = 1;
      input.pattern = '[0-9]*';
      input.setAttribute('aria-label', `Цифра ${index + 1} из ${OTP_LENGTH}`);
      input.className =
        'h-12 w-full min-w-0 rounded-lg border border-slate-700 bg-slate-950 text-center text-lg font-semibold text-slate-100 ' +
        'focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 sm:h-14 sm:text-xl';
      input.addEventListener('input', onDigitInput);
      input.addEventListener('keydown', onDigitKeydown);
      input.addEventListener('paste', onDigitPaste);
      input.addEventListener('focus', () => input.select());
      otpInputs.appendChild(input);
      digits.push(input);
    }
  }

  /** Только цифры: буквы и пробелы отбрасываются. */
  function sanitize(value) {
    return value.replace(/\D/g, '');
  }

  function onDigitInput(event) {
    const input = event.target;
    const index = digits.indexOf(input);
    const value = sanitize(input.value);
    if (!value) {
      input.value = '';
      return;
    }
    // Введённые лишние цифры разносим по следующим полям: тем же путём
    // обрабатывается вставка кода целиком через мобильную автозамену.
    input.value = value[0];
    for (let offset = 1; offset < value.length && index + offset < OTP_LENGTH; offset += 1) {
      digits[index + offset].value = value[offset];
    }
    focusDigit(Math.min(index + value.length, OTP_LENGTH - 1));
    submitWhenComplete();
  }

  function onDigitKeydown(event) {
    const index = digits.indexOf(event.target);
    if (event.key === 'Backspace') {
      // Пустое поле + Backspace — возвращаемся на предыдущую цифру,
      // иначе пришлось бы кликать по каждому полю вручную.
      if (!event.target.value && index > 0) {
        event.preventDefault();
        digits[index - 1].value = '';
        focusDigit(index - 1);
      }
      return;
    }
    if (event.key === 'ArrowLeft' && index > 0) {
      event.preventDefault();
      focusDigit(index - 1);
      return;
    }
    if (event.key === 'ArrowRight' && index < OTP_LENGTH - 1) {
      event.preventDefault();
      focusDigit(index + 1);
    }
  }

  function onDigitPaste(event) {
    event.preventDefault();
    const pasted = sanitize(event.clipboardData?.getData('text') || '');
    if (!pasted) return;
    const start = Math.max(digits.indexOf(event.target), 0);
    for (let offset = 0; offset < OTP_LENGTH; offset += 1) {
      digits[offset].value = pasted[offset] || '';
    }
    focusDigit(Math.min(start + pasted.length, OTP_LENGTH - 1));
    submitWhenComplete();
  }

  function focusDigit(index) {
    const input = digits[index];
    if (!input) return;
    input.focus();
    input.select();
  }

  function otpCode() {
    return digits.map((input) => input.value).join('');
  }

  /** Код введён полностью — отправляем, не дожидаясь нажатия «Подтвердить». */
  function submitWhenComplete() {
    if (otpCode().length === OTP_LENGTH) {
      verifyForm.requestSubmit();
    }
  }

  /* ==========================================================
     Обратный отсчёт повторной отправки кода (сервер: 1/60 сек)
     ========================================================== */

  function startResendTimer(seconds = RESEND_COOLDOWN_SECONDS) {
    stopResendTimer();
    let left = seconds;
    const render = () => {
      resendButton.disabled = left > 0;
      resendButton.classList.toggle('opacity-50', left > 0);
      resendTimerLabel.textContent = left > 0 ? `Повтор через ${left} с` : '';
    };
    render();
    if (left <= 0) return;
    resendTimer = setInterval(() => {
      left -= 1;
      render();
      if (left <= 0) stopResendTimer();
    }, 1000);
  }

  /** Переход на шаг 2: экран ввода кода для указанного email. */
  function showVerifyStep(email) {
    pendingEmail = email;
    verifyEmailLabel.textContent = email;
    clearOtp();
    setTab('verify');
    startResendTimer();
    focusDigit(0);
  }

  function backToRegister() {
    stopResendTimer();
    pendingEmail = '';
    setTab('register');
    document.getElementById('register-email')?.focus();
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
      // 403 EMAIL_NOT_VERIFIED: аккаунт создан, но код не введён — продолжаем
      // подтверждение, а не отправляем пользователя регистрироваться заново.
      if (error instanceof ApiError && error.errorCode === 'EMAIL_NOT_VERIFIED') {
        showVerifyStep(email);
        popup.warning('Подтвердите email', 'Введите код из письма, чтобы закончить регистрацию.');
        return;
      }
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

    // Согласие на обработку ПД обязательно (152-ФЗ ст. 9): без него запрос
    // не отправляется вообще. Проверка идёт на клиенте для быстрой обратной
    // связи, но серверная валидация остаётся обязательной на уровне API.
    const consent = document.getElementById('register-consent');
    if (!consent?.checked) {
      popup.warning(
        'Нужно согласие на обработку данных',
        'Отметьте согласие с условиями обработки персональных данных.'
      );
      consent?.focus();
      return;
    }

    const button = document.getElementById('register-submit');
    setLoading(button, true);
    try {
      await api.register(email, password);
      // JWT здесь нет: аккаунт создан, но не подтверждён (docs/03 §2).
      // Сразу переходим к вводу кода — пароль больше не понадобится.
      showVerifyStep(email);
      popup.success('Аккаунт создан', `Отправили код подтверждения на ${email}.`);
    } catch (error) {
      popup.error('Не удалось зарегистрироваться', describeError(error));
    } finally {
      setLoading(button, false);
    }
  });

  /* ==========================================================
     Шаг 2 — Подтверждение email шестизначным кодом
     ========================================================== */

  verifyForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = otpCode();
    if (code.length !== OTP_LENGTH) {
      popup.warning('Введите код полностью', `Нужно ${OTP_LENGTH} цифр.`);
      focusDigit(0);
      return;
    }

    const button = document.getElementById('verify-submit');
    setLoading(button, true);
    try {
      // Сервер сам выдаёт пару JWT и ставит refresh-cookie (docs/03 §2),
      // поэтому после успеха пользователь уже авторизован.
      await api.verifyEmail(pendingEmail, code);
      stopResendTimer();
      const user = await api.me().catch(() => null);
      if (user) session.setUser(user);
      popup.success('Email подтверждён', 'Аккаунт активирован.');
      onAuthed?.(user);
    } catch (error) {
      clearOtp();
      focusDigit(0);
      const locked = error instanceof ApiError && error.errorCode === 'OTP_LOCKED';
      popup.error(
        locked ? 'Код заблокирован' : 'Не удалось подтвердить email',
        describeError(error)
      );
      if (locked) {
        // Попытки исчерпаны: новый код можно получить только по кнопке,
        // поэтому разблокируем её немедленно, не дожидаясь кулдауна.
        stopResendTimer();
        resendButton.disabled = false;
        resendButton.classList.remove('opacity-50');
        resendTimerLabel.textContent = 'Запросите новый код';
      }
    } finally {
      setLoading(button, false);
    }
  });

  resendButton.addEventListener('click', async () => {
    if (resendButton.disabled) return;
    resendButton.disabled = true;
    try {
      await api.resendCode(pendingEmail);
      popup.success('Новый код отправлен', `Письмо с кодом ушло на ${pendingEmail}.`);
      startResendTimer();
      clearOtp();
      focusDigit(0);
    } catch (error) {
      // 429 → сервер прислал Retry-After: считаем ровно до его значения,
      // иначе пользователь получал бы 429 в ответ на каждую попытку.
      const retryAfter =
        error instanceof ApiError && error.status === 429 && Number(error.retryAfter) > 0
          ? Number(error.retryAfter)
          : RESEND_COOLDOWN_SECONDS;
      startResendTimer(retryAfter);
      popup.warning('Код пока нельзя отправить', describeError(error));
    }
  });

  document.getElementById('verify-back').addEventListener('click', backToRegister);

  buildOtpInputs();
}

/**
 * Сброс экрана входа после выхода из аккаунта: поле пароля очищено
 * (не храним чужие креды в DOM), активна вкладка «Вход».
 */
export function resetAuth() {
  stopResendTimer();
  pendingEmail = '';
  clearOtp();
  const password = document.getElementById('login-password');
  if (password) password.value = '';
  const consent = document.getElementById('register-consent');
  if (consent) consent.checked = false;
  document.querySelector('[data-auth-tab="login"]')?.click();
}
