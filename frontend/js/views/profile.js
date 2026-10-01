/* ============================================================
   Вкладка «Моё резюме»: профиль, compact_resume, навыки,
   зарплатные ожидания, порог матчинга (docs/03 §3, docs/05 §3).
   ============================================================ */

import { api } from '../core/api.js';
import { on } from '../core/bus.js';
import { getState, setState } from '../core/state.js';
import { loadPartial } from '../core/partials.js';
import { popup } from '../components/fadeout-action-popup.js';
import { TagInput } from '../components/tag-input.js';
import { copyText, charCount, formatDateTime, clamp } from '../core/utils.js';

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
    workFormats: target.querySelectorAll('input[name="work-format"]')
  };

  skillsInput = new TagInput(els.skills, { placeholder: els.skills.dataset.placeholder, max: 50, onChange: markDirty });

  els.resume.addEventListener('input', () => {
    updateResumeCounter();
    markDirty();
  });
  els.fullName.addEventListener('input', markDirty);
  els.experience.addEventListener('input', markDirty);
  els.salaryFrom.addEventListener('input', markDirty);
  els.salaryTo.addEventListener('input', markDirty);
  els.workFormats.forEach((input) => input.addEventListener('change', markDirty));

  els.threshold.addEventListener('input', () => {
    els.thresholdValue.textContent = `${els.threshold.value}%`;
    markDirty();
  });

  els.saveButton.addEventListener('click', saveProfile);
  els.resetButton.addEventListener('click', resetForm);
  els.copyCompact.addEventListener('click', copyCompactResume);
  els.convertButton.addEventListener('click', convertResume);

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
  skillsInput.setValues(profile.skills || []);
  els.experience.value = profile.experience_years ?? '';
  els.salaryFrom.value = profile.desired_salary_from ?? '';
  els.salaryTo.value = profile.desired_salary_to ?? '';
  els.threshold.value = clamp(Number(profile.match_threshold ?? 70), 0, 100);
  els.thresholdValue.textContent = `${els.threshold.value}%`;
  const formats = profile.preferred_work_formats || [];
  els.workFormats.forEach((input) => { input.checked = formats.includes(input.value); });
  renderCompact(profile);
  snapshot = readForm();
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
    preferred_work_formats: [...els.workFormats].filter((input) => input.checked).map((input) => input.value)
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
  skillsInput.setValues(snapshot.skills || []);
  els.experience.value = snapshot.experience_years ?? '';
  els.salaryFrom.value = snapshot.desired_salary_from ?? '';
  els.salaryTo.value = snapshot.desired_salary_to ?? '';
  els.threshold.value = snapshot.match_threshold ?? 70;
  els.thresholdValue.textContent = `${els.threshold.value}%`;
  els.workFormats.forEach((input) => { input.checked = (snapshot.preferred_work_formats || []).includes(input.value); });
  popup.info('Изменения сброшены', 'Форма возвращена к сохранённому состоянию.');
}

function markDirty() {
  els.savedAt.textContent = 'Есть несохранённые изменения';
}

function updateResumeCounter() {
  els.resumeCounter.textContent = `${charCount(els.resume.value)} / 5000`;
}

/* ---------- compact_resume ---------- */

function renderCompact(profile) {
  const compact = profile?.compact_resume;
  const hasCompact = Boolean(compact && compact.trim());
  els.compactView.textContent = hasCompact ? compact : '— ещё не создано —';
  els.compactView.classList.toggle('text-slate-500', !hasCompact);
  els.copyCompact.disabled = !hasCompact;
  if (hasCompact) {
    els.compactMeta.textContent = `${charCount(compact)} симв.`;
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

function toggleLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  button.querySelector('[data-spinner]')?.classList.toggle('hidden', !loading);
}
