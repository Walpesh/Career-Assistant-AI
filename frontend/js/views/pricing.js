/* ============================================================
   Тарифы и подписка (docs/03_API_CONTRACTS.md §11):
   каталог GET /billing/tiers → карточки Free / Pro / Enterprise;
   кнопка «Оплатить» → POST /billing/checkout → редирект браузера
   на confirmation_url платёжного шлюза, после оплаты шлюз
   возвращает пользователя на FRONT_BASE_URL + /pricing/success.
   ============================================================ */

import { api } from '../core/api.js';
import { popup } from '../components/fadeout-action-popup.js';
import { escapeHtml } from '../core/utils.js';
import { openOverlay } from '../components/overlay.js';

const MODAL_ID = 'pricing-modal';

// Возврат платёжного шлюза: FRONT_BASE_URL + /pricing/success.
// nginx отдаёт за этот путь frontend/pricing/success/index.html (meta-refresh),
// но если статику раздаёт другой сервер со SPA-fallback на index.html —
// дублируем переход на каноническую страницу подтверждения здесь, пока модули
// не начали загрузку приложения. Цикла нет: /pricing/success.html не совпадает.
if (window.location.pathname.replace(/\/+$/, '') === '/pricing/success') {
  window.location.replace(`/pricing/success.html${window.location.search}`);
}

/** Подписи суточных квот (ключи — daily_quotas из billing/tiers.py). */
const QUOTA_LABELS = {
  parse: 'Задачи парсинга / сут',
  letter: 'Письма / сут',
  analysis: 'Анализы / сут',
  proxy_mb: 'Прокси-трафик / сут'
};

/** Человекочитаемые имена тарифов. */
const TIER_NAMES = { free: 'Free', pro: 'Pro', enterprise: 'Enterprise' };

/** Короткие описания — те же, что в каталоге бэкенда. */
const TIER_DESCRIPTIONS = {
  free: 'Базовый режим для знакомства с продуктом',
  pro: 'Для активного поиска: суточные квоты ×10',
  enterprise: 'Безлимитные квоты под enterprise-контракт'
};

/**
 * Статический каталог на случай недоступного API. Значения обязаны
 * совпадать с backend/app/modules/billing/tiers.py (TIERS + PRICING):
 * free 0 ₽, pro 990 ₽/мес, enterprise 3900 ₽/мес.
 */
const FALLBACK_TIERS = [
  {
    tier: 'free',
    rank: 0,
    daily_quotas: { parse: 5, letter: 10, analysis: 30, proxy_mb: 200 },
    unlimited: [],
    pricing: { price: 0, currency: 'RUB', interval: 'forever' }
  },
  {
    tier: 'pro',
    rank: 1,
    daily_quotas: { parse: 50, letter: 200, analysis: 1000, proxy_mb: 5000 },
    unlimited: [],
    pricing: { price: 990, currency: 'RUB', interval: 'month' }
  },
  {
    tier: 'enterprise',
    rank: 2,
    daily_quotas: { parse: -1, letter: -1, analysis: -1, proxy_mb: -1 },
    unlimited: ['parse', 'letter', 'analysis', 'proxy_mb'],
    pricing: { price: 3900, currency: 'RUB', interval: 'month' }
  }
];

/** Созданная модалка (создаётся один раз и переиспользуется). */
let modal = null;


/* ---------- Публичный API модуля ---------- */

/**
 * Вставить в шапку дашборда кнопку открытия раздела тарифов.
 * @param {Element|null} root — контейнер (header в partials/dashboard.html)
 */
export function mountPricingTrigger(root) {
  if (!root || root.querySelector('[data-action="open-pricing"]')) return;

  const wrapper = document.createElement('div');
  wrapper.className = 'mt-3 flex flex-wrap items-center gap-2';
  wrapper.innerHTML = `
    <button type="button" data-action="open-pricing" class="btn-secondary btn-sm">Тарифы и подписка</button>
    <span class="text-xs text-slate-500">Квоты, цены и оплата тарифа</span>`;
  root.appendChild(wrapper);

  wrapper
    .querySelector('[data-action="open-pricing"]')
    ?.addEventListener('click', () => void openPricing());
}

/** Открыть модалку тарифов и загрузить актуальный каталог. */
export async function openPricing() {
  const element = ensureModal();
  openOverlay(MODAL_ID);

  const list = element.querySelector('#pricing-tiers');
  if (list) list.innerHTML = '<p class="text-xs text-slate-400">Загружаем каталог тарифов…</p>';
  await loadCatalog();
}

/* ---------- Данные ---------- */

async function loadCatalog() {
  const element = ensureModal();
  const list = element.querySelector('#pricing-tiers');
  if (!list) return;

  let tiers = FALLBACK_TIERS;
  let billingEnabled = true;
  let note = '';
  try {
    const data = await api.billingTiers();
    if (Array.isArray(data?.tiers) && data.tiers.length) tiers = data.tiers;
    if (data && typeof data.billing_enabled === 'boolean') billingEnabled = data.billing_enabled;
  } catch (error) {
    note = `Не удалось загрузить каталог тарифов (${error.message}) — показаны базовые цены.`;
  }

  let currentTier = 'free';
  try {
    const subscription = await api.billingSubscription();
    if (subscription?.tier) currentTier = subscription.tier;
  } catch {
    /* Без подписки или эндпоинт недоступен — эффективный free (docs/03 §11). */
  }

  renderTiers(list, tiers, currentTier, { billingEnabled, note });
}

function renderTiers(list, tiers, currentTier, { billingEnabled, note }) {
  const current = TIER_NAMES[currentTier] || currentTier;
  const currentLine = document.getElementById('pricing-current');
  if (currentLine) {
    currentLine.textContent = `Ваш тариф: ${current} · квоты обновляются ежедневно в 00:00 UTC`;
  }

  const ordered = [...tiers].sort((a, b) => (a.rank ?? 0) - (b.rank ?? 0));
  list.innerHTML = `
    ${note ? `<p class="mb-4 rounded-lg border border-indigo-500/30 bg-indigo-500/10 px-3 py-2 text-xs text-indigo-200">${escapeHtml(note)}</p>` : ''}
    <div class="grid gap-4 sm:grid-cols-3">
      ${ordered.map((tier) => tierCardHTML(tier, currentTier, billingEnabled)).join('')}
    </div>
    <p class="mt-4 text-xs leading-relaxed text-slate-500">
      Оплата проходит на стороне платёжного шлюза (данные карты вводятся у него).
      После оплаты тариф обновится автоматически — обычно в течение нескольких минут.
    </p>`;
}

function tierCardHTML(tier, currentTier, billingEnabled) {
  const name = TIER_NAMES[tier.tier] || tier.tier;
  const description = TIER_DESCRIPTIONS[tier.tier] || '';
  const pricing = tier.pricing || {};
  const isCurrent = tier.tier === currentTier;
  const price = formatPrice(pricing);

  const quotas = Object.entries(QUOTA_LABELS)
    .map(([kind, label]) => {
      const value = quotaValue(tier.daily_quotas?.[kind], kind);
      return `<li class="flex items-center justify-between gap-2">
        <span>${escapeHtml(label)}</span>
        <span class="tabular-nums text-slate-300">${escapeHtml(value)}</span>
      </li>`;
    })
    .join('');

  let action;
  if (isCurrent) {
    action = '<p class="mt-4 text-center text-xs font-medium text-indigo-300">Ваш текущий тариф</p>';
  } else if (Number(pricing.price ?? 0) === 0) {
    action = '<p class="mt-4 text-center text-xs text-slate-500">Доступен бесплатно</p>';
  } else if (!billingEnabled) {
    action = '<p class="mt-4 text-center text-xs text-slate-500">Оплата временно недоступна</p>';
  } else {
    action = `<button type="button" class="btn-primary btn-sm mt-4 w-full" data-checkout-tier="${escapeHtml(tier.tier)}">
      <span class="hidden h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" data-spinner></span>
      <span>Оплатить</span>
    </button>`;
  }

  return `<div class="rounded-xl border ${isCurrent ? 'border-indigo-500/60' : 'border-slate-800'} bg-slate-950/60 p-4">
    <div class="flex items-start justify-between gap-3">
      <div class="min-w-0">
        <p class="text-sm font-semibold text-slate-100">${escapeHtml(name)}</p>
        <p class="mt-0.5 text-xs text-slate-400">${escapeHtml(description)}</p>
      </div>
      <div class="shrink-0 text-right">
        <p class="text-lg font-bold text-slate-100">${escapeHtml(price)}</p>
        <p class="text-[11px] text-slate-500">${escapeHtml(intervalLabel(pricing))}</p>
      </div>
    </div>
    <ul class="mt-3 space-y-1.5 text-xs text-slate-400">${quotas}</ul>
    ${action}
  </div>`;
}

/* ---------- Checkout ---------- */

async function checkout(tier, button) {
  if (!button || button.disabled) return;
  const original = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `
    <span class="hidden h-4 w-4 animate-spin rounded-full border-2 border-white/30 border-t-white" data-spinner></span>
    <span>Создаём оплату…</span>`;
  button.querySelector('[data-spinner]')?.classList.remove('hidden');

  try {
    const session = await api.billingCheckout(tier);
    const url = session?.confirmation_url;
    if (!url) throw new Error('Платёжный шлюз не вернул ссылку оплаты');
    popup.info('Переход к оплате', 'Открываем защищённую страницу платежа…');
    // Браузер уходит на страницу шлюза; возврат — на /pricing/success.
    window.location.assign(url);
  } catch (error) {
    popup.error('Не удалось начать оплату', error.message);
    button.disabled = false;
    button.innerHTML = original;
  }
}


/* ---------- Разметка ---------- */

function ensureModal() {
  if (modal?.isConnected) return modal;

  const element = document.createElement('div');
  element.id = MODAL_ID;
  element.className = 'overlay';
  element.dataset.open = '0';
  element.setAttribute('role', 'dialog');
  element.setAttribute('aria-modal', 'true');
  element.setAttribute('aria-labelledby', 'pricing-modal-title');
  element.innerHTML = `
    <div class="overlay-backdrop" data-close="${MODAL_ID}"></div>
    <div class="overlay-panel max-w-3xl">
      <div class="overlay-header">
        <div class="min-w-0 flex-1">
          <h3 id="pricing-modal-title" class="text-sm font-semibold text-slate-200">Тарифы и подписка</h3>
          <p id="pricing-current" class="mt-0.5 text-xs text-slate-400">—</p>
        </div>
        <button type="button" data-close="${MODAL_ID}" class="btn-ghost btn-sm" aria-label="Закрыть">✕</button>
      </div>
      <div id="pricing-tiers" class="overlay-body scroll-slim p-4"></div>
      <div class="overlay-footer">
        <button type="button" class="btn-ghost btn-sm ml-auto" data-close="${MODAL_ID}">Закрыть</button>
      </div>
    </div>`;

  // Делегирование: одна привязка на все карточки тарифов в модалке.
  element.addEventListener('click', (event) => {
    const button = event.target.closest('[data-checkout-tier]');
    if (!button) return;
    void checkout(button.dataset.checkoutTier, button);
  });

  document.body.appendChild(element);
  modal = element;
  return element;
}

/* ---------- Форматирование ---------- */

function formatPrice(pricing) {
  const price = Number(pricing?.price ?? 0);
  if (!price) return 'Бесплатно';
  const currency = pricing?.currency || 'RUB';
  const symbol = currency === 'RUB' ? '₽' : currency;
  return `${price.toLocaleString('ru-RU')} ${symbol}`;
}

function intervalLabel(pricing) {
  switch (pricing?.interval) {
    case 'month': return 'в месяц';
    case 'year': return 'в год';
    case 'forever': return 'навсегда';
    default: return '';
  }
}

function quotaValue(value, kind) {
  if (value === -1 || value === null || value === undefined) return 'без лимита';
  return kind === 'proxy_mb' ? `${value} МБ` : String(value);
}

