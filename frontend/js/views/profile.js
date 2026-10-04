/* ============================================================
   Вкладка «Моё резюме»: профиль, compact_resume, навыки,
   зарплатные ожидания, порог матчинга (docs/03 §3, docs/05 §3).
   ============================================================ */

import { api } from '../core/api.js';
import { on } from '../core/bus.js';
import { session } from '../core/session.js';
import {
  getState,
  setState,
  getMatchThreshold,
  getSavedMatchThreshold,
  setMatchThreshold,
  commitMatchThreshold
} from '../core/state.js';
import { CONFIG } from '../config.js';
import { loadPartial } from '../core/partials.js';
import { popup } from '../components/fadeout-action-popup.js';
import { TagInput } from '../components/tag-input.js';
import { copyText, charCount, formatDateTime, clamp } from '../core/utils.js';
import { confirmDialog } from '../components/overlay.js';

let els = {};
let skillsInput = null;
let snapshot = null;   // последнее сохранённое состояние формы
let mounted = false;
let awaitingConvert = false;

export async function mount() {
  if (mounted) return;
  mounted = true;

  const target = document.getElementById('view-profile');
  target.innerHTML = await loadPartial('profile');

  els = {
    fullName: document.getElementById('profile-full-name'),
    email: document.getElementById('profile-email'),
    resume: document.getElementById('profile-resume'),
    resumeCounter: document.getElementById('resume-counter'),
    resumeAddition: document.getElementById('profile-resume-addition'),
    resumeAdditionCounter: document.getElementById('resume-addition-counter'),
    analysisPreferences: document.getElementById('profile-analysis-preferences'),
    analysisPreferencesCounter: document.getElementById('analysis-prefs-counter'),
    skills: document.getElementById('profile-skills'),
    experience: document.getElementById('profile-experience'),
    salaryFrom: document.getElementById('profile-salary-from'),
    salaryTo: document.getElementById('profile-salary-to'),
    threshold: document.getElementById('profile-threshold'),
    thresholdValue: document.getElementById('profile-threshold-value'),
    savedAt: document.getElementById('profile-saved-at'),
    convertButton: document.getElementById('btn-convert-resume'),
    copyCompact: document.getElementById('btn-copy-compact'),
    compactView: document.getElementById('compact-resume-view'),
    compactMeta: document.getElementById('compact-meta'),
    saveButton: document.getElementById('btn-save-profile'),
    resetButton: document.getElementById('btn-reset-profile'),
    workFormats: target.querySelectorAll('input[name="work-format"]'),
    // Секция «Приватность и данные» (docs/03 §10–§11).
    accountSummary: {
      vacancies: document.getElementById('account-sum-vacancies'),
      tasks: document.getElementById('account-sum-tasks'),
      refreshTokens: document.getElementById('account-sum-refresh-tokens'),
      tier: document.getElementById('account-sum-tier')
    },
    billingQuotas: document.getElementById('billing-quotas'),
    billingResetsAt: document.getElementById('billing-resets-at'),
    exportButton: document.getElementById('btn-export-data'),
    deleteButton: document.getElementById('btn-delete-account')
  };

  skillsInput = new TagInput(els.skills, { placeholder: els.skills.dataset.placeholder, max: 50, onChange: markDirty });

  els.resume.addEventListener('input', () => {
    updateResumeCounter();
    markDirty();
  });
  els.resumeAddition.addEventListener('input', () => {
    updateResumeAdditionCounter();
    markDirty();
  });
  els.analysisPreferences.addEventListener('input', () => {
    updateAnalysisPreferencesCounter();
    markDirty();
  });
  els.fullName.addEventListener('input', markDirty);
  els.experience.addEventListener('input', markDirty);
  els.salaryFrom.addEventListener('input', markDirty);
  els.salaryTo.addEventListener('input', markDirty);
  els.workFormats.forEach((input) => input.addEventListener('change', markDirty));

  els.threshold.addEventListener('input', () => {
    // Порог — общий для главной страницы и профиля (state.js).
    setMatchThreshold(els.threshold.value, 'profile');
    els.thresholdValue.textContent = `${els.threshold.value}%`;
    markDirty();
  });

  // Синхронизация с главной страницей и сохранённым профилем.
  on('match:threshold', ({ source }) => {
    if (source === 'profile' || !els.threshold) return;
    const threshold = clamp(getMatchThreshold(), 0, 100);
    if (Number(els.threshold.value) === threshold) return;
    els.threshold.value = threshold;
    els.thresholdValue.textContent = `${threshold}%`;
  });

  els.saveButton.addEventListener('click', saveProfile);
  els.resetButton.addEventListener('click', resetForm);
  els.copyCompact.addEventListener('click', copyCompactResume);
  els.convertButton.addEventListener('click', convertResume);
  els.exportButton.addEventListener('click', exportAccountData);
  els.deleteButton.addEventListener('click', deleteAccount);

  // Сводка аккаунта и квоты загружаются в фоне: они не нужны для правки
  // профиля и не должны задерживать открытие вкладки.
  loadAccountOverview();

  // Завершение задачи convert_resume приходит по WS.
  on('ws:task.completed', () => {
    if (awaitingConvert) {
      awaitingConvert = false;
      setConvertLoading(false);
      refresh();
    }
  });
  on('ws:task.failed', () => {
    if (awaitingConvert) {
      awaitingConvert = false;
      setConvertLoading(false);
    }
  });

  await refresh();
}

export async function refresh() {
  try {
    const profile = await api.getProfile();
    setState({ profile });
    fillForm(profile);
  } catch (error) {
    popup.error('Профиль не загружен', error.message);
  }
}

/* ---------- Форма ---------- */

function fillForm(profile = {}) {
  const user = getState().user || {};
  els.fullName.value = profile.full_name || '';
  els.email.value = user.email || '—';
  els.resume.value = profile.resume_text || '';
  updateResumeCounter();
  els.resumeAddition.value = profile.resume_addition || '';
  updateResumeAdditionCounter();
  els.analysisPreferences.value = profile.analysis_preferences || '';
  updateAnalysisPreferencesCounter();
  skillsInput.setValues(profile.skills || []);
  els.experience.value = profile.experience_years ?? '';
  els.salaryFrom.value = profile.desired_salary_from ?? '';
  els.salaryTo.value = profile.desired_salary_to ?? '';
  // Не затираем слайдер, если пользователь двигает его прямо сейчас —
  // приоритет у несохранённого значения (state.threshold).
  els.threshold.value = clamp(getMatchThreshold(), 0, 100);
  els.thresholdValue.textContent = `${els.threshold.value}%`;
  const formats = profile.preferred_work_formats || [];
  els.workFormats.forEach((input) => { input.checked = formats.includes(input.value); });
  renderCompact(profile);
  snapshot = readForm();
  // В снимке — СОХРАНЁННЫЙ порог: показанное значение может быть
  // несохранённым (например, изменённым на главной странице), и иначе
  // кнопка «Сохранить профиль» решила бы, что порог менять не нужно.
  snapshot.match_threshold = clamp(
    Number(profile.match_threshold ?? getSavedMatchThreshold()),
    0,
    100
  );
  els.savedAt.textContent = profile.updated_at ? `Сохранено: ${formatDateTime(profile.updated_at)}` : '—';
}

function readForm() {
  return {
    full_name: els.fullName.value.trim(),
    resume_text: els.resume.value,
    skills: skillsInput.getValues(),
    experience_years: els.experience.value === '' ? null : Number(els.experience.value),
    desired_salary_from: els.salaryFrom.value === '' ? null : Number(els.salaryFrom.value),
    desired_salary_to: els.salaryTo.value === '' ? null : Number(els.salaryTo.value),
    match_threshold: clamp(Number(els.threshold.value), 0, 100),
    preferred_work_formats: [...els.workFormats].filter((input) => input.checked).map((input) => input.value),
    // Дописывается в конец письма «с красной строки» (backend: llm.append_resume_addition).
    resume_addition: els.resumeAddition.value.trim() || null,
    // Передаётся в промпт анализа нейросети (backend: llm.build_preferences_block).
    analysis_preferences: els.analysisPreferences.value.trim() || null
  };
}

async function saveProfile() {
  const values = readForm();
  const patch = {};
  Object.keys(values).forEach((key) => {
    if (JSON.stringify(values[key]) !== JSON.stringify(snapshot?.[key])) patch[key] = values[key];
  });

  if (Object.keys(patch).length === 0) {
    popup.info('Нечего сохранять', 'Изменений в профиле нет.');
    return;
  }

  toggleLoading(els.saveButton, true);
  try {
    const updated = await api.updateProfile(patch);
    const merged = { ...getState().profile, ...patch, ...(updated && typeof updated === 'object' ? updated : {}) };
    // Порог сохранён на сервере → несохранённое значение сбрасывается,
    // обе вкладки показывают значение из профиля.
    commitMatchThreshold();
    setState({ profile: merged });
    fillForm(merged);
    popup.success('Профиль сохранён', 'Изменения применены.');
  } catch (error) {
    popup.error('Не удалось сохранить', error.message);
  } finally {
    toggleLoading(els.saveButton, false);
  }
}

function resetForm() {
  if (!snapshot) return;
  els.fullName.value = snapshot.full_name || '';
  els.resume.value = snapshot.resume_text || '';
  updateResumeCounter();
  els.resumeAddition.value = snapshot.resume_addition || '';
  updateResumeAdditionCounter();
  els.analysisPreferences.value = snapshot.analysis_preferences || '';
  updateAnalysisPreferencesCounter();
  skillsInput.setValues(snapshot.skills || []);
  els.experience.value = snapshot.experience_years ?? '';
  els.salaryFrom.value = snapshot.desired_salary_from ?? '';
  els.salaryTo.value = snapshot.desired_salary_to ?? '';
  els.threshold.value = snapshot.match_threshold ?? 70;
  els.thresholdValue.textContent = `${els.threshold.value}%`;
  setMatchThreshold(els.threshold.value, 'profile');
  els.workFormats.forEach((input) => { input.checked = (snapshot.preferred_work_formats || []).includes(input.value); });
  popup.info('Изменения сброшены', 'Форма возвращена к сохранённому состоянию.');
}

function markDirty() {
  els.savedAt.textContent = 'Есть несохранённые изменения';
}

function updateResumeCounter() {
  els.resumeCounter.textContent = `${charCount(els.resume.value)} / ${CONFIG.RESUME_MAX_CHARS}`;
}

/* ---------- «Хотите добавить информацию в конец резюме?» и предпочтения ---------- */

function updateResumeAdditionCounter() {
  els.resumeAdditionCounter.textContent =
    `${charCount(els.resumeAddition.value)} / ${CONFIG.RESUME_ADDITION_MAX_CHARS}`;
}

function updateAnalysisPreferencesCounter() {
  els.analysisPreferencesCounter.textContent =
    `${charCount(els.analysisPreferences.value)} / ${CONFIG.ANALYSIS_PREFERENCES_MAX_CHARS}`;
}

/* ---------- compact_resume ---------- */

function renderCompact(profile) {
  const compact = profile?.compact_resume;
  const hasCompact = Boolean(compact && compact.trim());
  els.compactView.textContent = hasCompact ? compact : '— ещё не создано —';
  els.compactView.classList.toggle('text-slate-500', !hasCompact);
  els.copyCompact.disabled = !hasCompact;
  if (hasCompact) {
    // Лимит compact_resume (должен совпадать с backend COMPACT_RESUME_MAX_CHARS).
    els.compactMeta.textContent = `${charCount(compact)} / ${CONFIG.COMPACT_MAX_CHARS} симв.`;
  } else {
    els.compactMeta.textContent = 'не создано';
  }
}

async function copyCompactResume() {
  const text = els.compactView.textContent;
  const ok = await copyText(text);
  if (ok) popup.success('Скопировано', 'compact_resume в буфере обмена.');
  else popup.error('Не удалось скопировать', 'Буфер обмена недоступен.');
}

async function convertResume() {
  if (!els.resume.value.trim()) {
    popup.warning('Сначала заполните резюме', 'Конвертация работает с полным текстом резюме.');
    return;
  }
  setConvertLoading(true);
  try {
    const result = await api.convertResume();

    // Синхронный результат (LLM ответил в рамках запроса) → сразу обновляем UI.
    const compact = result?.compact_resume;
    if (typeof compact === 'string' && compact.trim()) {
      const merged = { ...getState().profile, ...result };
      setState({ profile: merged });
      fillForm(merged);
      setConvertLoading(false);
      popup.success('Резюме сжато', `compact_resume: ${charCount(compact)} симв.`);
      return;
    }

    // Асинхронный сценарий (задача convert_resume ушла в очередь LLM).
    awaitingConvert = true;
    popup.info('Задача создана', 'Сокращение резюме поставлено в очередь LLM (convert_resume).');
    // Страховка: если WS-событие не придёт, обновим профиль через 45 секунд.
    setTimeout(() => {
      if (awaitingConvert) {
        awaitingConvert = false;
        setConvertLoading(false);
        refresh();
      }
    }, 45000);
  } catch (error) {
    setConvertLoading(false);
    popup.error('Не удалось запустить конвертацию', error.message);
  }
}

function setConvertLoading(loading) {
  toggleLoading(els.convertButton, loading);
}

/* ---------- Приватность и квоты (docs/03 §10–§11) ---------- */

/** Подписи видов квот: ключи приходят из backend (QuotaKind). */
const QUOTA_LABELS = {
  parse: 'Запуски парсинга',
  letter: 'Сопроводительные письма',
  analysis: 'Анализы вакансий',
  proxy_mb: 'Прокси-трафик, МБ'
};

/** Загрузить сводку данных аккаунта и состояние суточных квот. */
async function loadAccountOverview() {
  // Две независимые загрузки: падение одной (например, отключённого биллинга)
  // не должно оставлять вторую незаполненной.
  const [summary, usage] = await Promise.allSettled([
    api.accountSummary(),
    api.billingUsage()
  ]);

  if (summary.status === 'fulfilled') {
    const data = summary.value || {};
    els.accountSummary.vacancies.textContent = data.vacancies ?? '—';
    els.accountSummary.tasks.textContent = data.tasks ?? '—';
    els.accountSummary.refreshTokens.textContent = data.refresh_tokens ?? '—';
  }

  if (usage.status === 'fulfilled') {
    renderQuotas(usage.value || {});
  } else if (els.billingQuotas) {
    // Квоты недоступны (BILLING_ENABLED=false или ошибка) — это не поломка
    // интерфейса, поэтому показываем нейтральный текст, а не ошибку.
    els.billingQuotas.innerHTML =
      '<li class="hint">Лимиты тарифа сейчас не применяются.</li>';
    els.billingResetsAt.textContent = '';
  }
}

/** Отрисовать суточные квоты с прогресс-барами. */
function renderQuotas(data) {
  const quotas = data.quotas || {};
  const rows = Object.entries(QUOTA_LABELS)
    .map(([kind, label]) => {
      const quota = quotas[kind];
      if (!quota) return '';
      const used = Number(quota.used ?? 0);
      const unlimited = Boolean(quota.unlimited);
      // Безлимит рисуем как «использовано», иначе процент бессмыслен.
      const percent = unlimited || !quota.limit ? 0 : Math.min(100, (used / quota.limit) * 100);
      const barColor = quota.exhausted ? 'bg-rose-500' : percent > 75 ? 'bg-amber-400' : 'bg-indigo-500';
      const value = unlimited
        ? `${used} · без лимита`
        : `${used} / ${quota.limit}`;
      const barWidth = unlimited ? '0%' : `${percent}%`;

      return `
        <li class="flex items-center gap-3">
          <span class="w-40 shrink-0 text-slate-400">${label}</span>
          <span class="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
            <span class="block h-full rounded-full ${barColor}" style="width: ${barWidth}"></span>
          </span>
          <span class="w-28 shrink-0 text-right tabular-nums text-slate-400">${value}</span>
        </li>`;
    })
    .filter(Boolean)
    .join('');

  els.billingQuotas.innerHTML = rows || '<li class="hint">Нет данных о квотах.</li>';
  els.billingResetsAt.textContent = data.resets_at
    ? `Обновление в ${formatDateTime(data.resets_at)}`
    : '';
}

/**
 * Выгрузить все персональные данные в файл (152-ФЗ ст. 14).
 * Ответ — большой JSON, поэтому он скачивается блобом, а не вставляется
 * в DOM: иначе десятки тысяч строк резюме и вакансий затормозят интерфейс.
 */
async function exportAccountData() {
  toggleLoading(els.exportButton, true);
  try {
    const { blob, filename } = await api.exportAccountData();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // Освобождение URL: без revokeObjectURL блоб держится в памяти до
    // перезагрузки страницы.
    URL.revokeObjectURL(url);
    popup.success('Данные выгружены', `Файл ${filename} сохранён в загрузки.`);
  } catch (error) {
    popup.error('Не удалось выгрузить данные', error.message);
  } finally {
    toggleLoading(els.exportButton, false);
  }
}

/**
 * Безвозвратно удалить аккаунт и все персональные данные (152-ФЗ ст. 21).
 * Действие необратимо, поэтому требует явного подтверждения с указанием
 * того, что именно будет удалено.
 */
async function deleteAccount() {
  const confirmed = await confirmDialog({
    title: 'Удалить аккаунт?',
    message:
      'Будут безвозвратно удалены профиль, резюме, все вакансии, анализы, ' +
      'сопроводительные письма, история задач и подписка. Восстановление ' +
      'невозможно. Перед удалением можно скачать копию данных.',
    confirmLabel: 'Удалить навсегда',
    danger: true
  });
  if (!confirmed) return;

  toggleLoading(els.deleteButton, true);
  try {
    const result = await api.deleteAccount();
    const rows = result?.report?.total_rows_deleted;
    session.clear();
    popup.success(
      'Аккаунт удалён',
      rows ? `Удалено записей: ${rows}. Данные аккаунта стёрты.` : 'Данные аккаунта стёрты.'
    );
    // Перезагрузка — единственный надёжный способ сбросить состояние SPA
    // после удаления сессии и всех загруженных данных.
    setTimeout(() => window.location.reload(), 1200);
  } catch (error) {
    toggleLoading(els.deleteButton, false);
    popup.error('Не удалось удалить аккаунт', error.message);
  }
}

function toggleLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  button.querySelector('[data-spinner]')?.classList.toggle('hidden', !loading);
}
