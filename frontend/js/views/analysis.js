/* ============================================================
   Вкладка «Анализ и Отклик»: список вакансий с фильтрами
   (docs/03 §4), массовая обработка (docs/03 §6), модалка анализа,
   drawer сопроводительного письма с копированием и .txt-скачиванием.
   ============================================================ */

import { api, ApiError } from '../core/api.js';
import { emit, on } from '../core/bus.js';
import { getState, setState, getMatchThreshold } from '../core/state.js';
import { loadPartial } from '../core/partials.js';
import { popup } from '../components/fadeout-action-popup.js';
import { openOverlay, closeOverlay, confirmDialog } from '../components/overlay.js';
import { statusBadge, sourceBadge, scoreTag, workFormatBadge, MODE_LABELS } from '../components/badges.js';
import { formatSalary, formatDateTime, timeAgo, escapeHtml, copyText, downloadText, debounce } from '../core/utils.js';
import { refreshTasks } from '../core/tasks.js';

let els = {};
let mounted = false;
let items = [];
let total = null;
let selected = new Set();
let currentAnalysis = { vacancyId: null, title: '' };
let currentLetter = { vacancyId: null, title: '', content: '' };
/** Кэш текстов писем: vacancyId → content (заполняется в loadLetter/отклике). */
const letterCache = new Map();
/** Функции отписки текущего монтирования (снимаются в reset()). */
const teardowns = [];
/** Контейнер вкладки — держим ссылку, чтобы снять делегированный клик. */
let viewContainer = null;

export async function mount() {
  if (mounted) return;
  mounted = true;

  document.getElementById('view-analysis').innerHTML = await loadPartial('analysis');

  els = {
    statusChips: [...document.querySelectorAll('#status-filters [data-status]')],
    search: document.getElementById('filter-search'),
    source: document.getElementById('filter-source'),
    score: document.getElementById('filter-score'),
    size: document.getElementById('filter-size'),
    reset: document.getElementById('btn-reset-filters'),
    selectAll: document.getElementById('select-all'),
    batchBar: document.getElementById('batch-bar'),
    selectedCount: document.getElementById('selected-count'),
    batchMode: document.getElementById('batch-mode'),
    batchThreshold: document.getElementById('batch-threshold'),
    runBatch: document.getElementById('btn-run-batch'),
    clearSelection: document.getElementById('btn-clear-selection'),
    list: document.getElementById('vacancy-list'),
    empty: document.getElementById('vacancy-empty'),
    skeleton: document.getElementById('vacancy-skeleton'),
    prev: document.getElementById('btn-prev-page'),
    next: document.getElementById('btn-next-page'),
    pageInfo: document.getElementById('page-info'),
    refresh: document.getElementById('btn-refresh-vacancies')
  };

  // Статус-чипы
  els.statusChips.forEach((chip) => chip.addEventListener('click', () => {
    setFilters({ status: chip.dataset.status, page: 1 });
    els.statusChips.forEach((c) => c.classList.toggle('chip-active', c === chip));
    refreshList();
  }));

  els.search.addEventListener('input', debounce(() => {
    setFilters({ search: els.search.value.trim(), page: 1 });
    refreshList();
  }, 400));

  els.source.addEventListener('change', () => {
    setFilters({ source: els.source.value, page: 1 });
    refreshList();
  });

  els.score.addEventListener('change', debounce(() => {
    setFilters({ minScore: els.score.value, page: 1 });
    refreshList();
  }, 400));

  els.size.addEventListener('change', () => {
    setFilters({ size: Number(els.size.value), page: 1 });
    refreshList();
  });

  els.reset.addEventListener('click', () => {
    setFilters({ status: '', source: '', search: '', minScore: '', page: 1, size: 20 });
    els.search.value = '';
    els.source.value = '';
    els.score.value = '';
    els.size.value = '20';
    els.statusChips.forEach((chip) => chip.classList.toggle('chip-active', chip.dataset.status === ''));
    refreshList();
  });

  els.refresh.addEventListener('click', () => refreshList());

  els.selectAll.addEventListener('change', () => {
    if (els.selectAll.checked) items.forEach((item) => selected.add(item.id));
    else selected.clear();
    renderList();
    syncBatchBar();
  });

  els.clearSelection.addEventListener('click', () => {
    selected.clear();
    renderList();
    syncBatchBar();
  });

  els.runBatch.addEventListener('click', runBatch);
  els.prev.addEventListener('click', () => { setFilters({ page: Math.max(1, getState().filters.page - 1) }); refreshList(); });
  els.next.addEventListener('click', () => { setFilters({ page: getState().filters.page + 1 }); refreshList(); });

  // Делегирование действий внутри списка и пустого состояния.
  viewContainer = document.getElementById('view-analysis');
  viewContainer.addEventListener('click', handleListClick);
  els.list.addEventListener('change', handleSelectionChange);

  // WS: точечные обновления (отписки складываем для reset()).
  teardowns.push(
    on('ws:vacancy.updated', () => scheduleLiveRefresh()),
    on('ws:analysis.ready', () => scheduleLiveRefresh()),
    on('ws:letter.ready', (payload) => {
      if (payload?.vacancy_id) letterCache.delete(payload.vacancy_id);
      if (currentLetter.vacancyId && payload?.vacancy_id === currentLetter.vacancyId) loadLetter(currentLetter.vacancyId);
      scheduleLiveRefresh();
    }),
    on('ws:resync', () => refreshList({ silent: true }))
  );
  // Переподключение WS — обработчик зарегистрирован выше (teardowns).
  syncBatchBar();
  await refreshList();
}

/**
 * Сброс вкладки (выход из аккаунта): снимаем подписки и делегированные
 * обработчики, очищаем выбор/кэш/DOM — повторный вход монтируется с нуля.
 */
export function reset() {
  teardowns.splice(0).forEach((off) => {
    try {
      off();
    } catch {
      /* ignore */
    }
  });
  viewContainer?.removeEventListener('click', handleListClick);
  viewContainer = null;
  mounted = false;
  items = [];
  total = null;
  selected = new Set();
  currentAnalysis = { vacancyId: null, title: '' };
  currentLetter = { vacancyId: null, title: '', content: '' };
  letterCache.clear();
  els = {};
  const container = document.getElementById('view-analysis');
  if (container) container.innerHTML = '';
}

function setFilters(patch) {
  setState({ filters: { ...getState().filters, ...patch } });
}

const scheduleLiveRefresh = debounce(() => refreshList({ silent: true }), 2500);

/* ---------- Загрузка и рендер списка ---------- */

async function refreshList({ silent = false } = {}) {
  if (!els.list) return; // вкладка сброшена (logout) — обновлять нечего
  const filters = getState().filters;
  if (!silent) els.skeleton.classList.remove('hidden');
  els.empty.classList.add('hidden');

  try {
    const data = await api.listVacancies({
      status: filters.status || undefined,
      source: filters.source || undefined,
      search: filters.search || undefined,
      min_match_score: filters.minScore === '' ? undefined : filters.minScore,
      page: filters.page,
      size: filters.size
    });

    items = Array.isArray(data) ? data : (data?.items || data?.vacancies || []);
    total = Array.isArray(data) ? null : (data?.total ?? null);
    // Чистим выбор от исчезнувших вакансий.
    const ids = new Set(items.map((item) => item.id));
    [...selected].forEach((id) => { if (!ids.has(id)) selected.delete(id); });

    renderList();
    renderPagination();
    syncBatchBar();
  } catch (error) {
    if (!silent) popup.error('Не удалось загрузить вакансии', error.message);
  } finally {
    els.skeleton.classList.add('hidden');
  }
}

function renderList() {
  if (!els.list) return;
  const threshold = getMatchThreshold();
  els.list.innerHTML = items.map((item) => vacancyCardHTML(item, threshold)).join('');
  els.empty.classList.toggle('hidden', items.length > 0);
  if (els.selectAll) {
    const allSelected = items.length > 0 && items.every((item) => selected.has(item.id));
    els.selectAll.checked = allSelected;
    els.selectAll.indeterminate = !allSelected && items.some((item) => selected.has(item.id));
  }
}

function vacancyCardHTML(vacancy, threshold) {
  const isSelected = selected.has(vacancy.id);
  const status = vacancy.status || 'raw';
  const published = vacancy.published_at ? timeAgo(vacancy.published_at) : '';

  const canViewAnalysis = ['analyzed', 'letter_ready', 'applied'].includes(status);
  const canViewLetter = ['letter_ready', 'applied'].includes(status);
  const canApply = !['applied', 'error'].includes(status);
  // «Откликнуться» доступно только при наличии письма и hh_vacancy_id для ссылки отклика.
  const hasLetterContent = Boolean(vacancy.cover_letter?.content);
  const canRespond = Boolean(vacancy.hh_vacancy_id) && (status === 'letter_ready' || hasLetterContent);

  return `<article class="card p-4 ${isSelected ? 'border-indigo-500/50' : ''}" data-vacancy-id="${escapeHtml(vacancy.id)}">
    <div class="flex items-start gap-3">
      <input type="checkbox" data-vacancy-select class="mt-1 h-4 w-4 shrink-0 rounded border-slate-600 bg-slate-900 accent-indigo-500"
             ${isSelected ? 'checked' : ''} aria-label="Выбрать вакансию" />
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-2">
          <a href="${escapeHtml(vacancy.url || '#')}" target="_blank" rel="noopener noreferrer"
             class="truncate text-sm font-semibold text-slate-100 hover:text-indigo-300">${escapeHtml(vacancy.title || 'Без названия')}</a>
          ${scoreTag(vacancy.match_score, threshold)}
        </div>
        <p class="mt-0.5 text-xs text-slate-400">
          ${escapeHtml(vacancy.company_name || 'Компания не указана')}${vacancy.area ? ` · ${escapeHtml(vacancy.area)}` : ''}
        </p>
        <p class="mt-1 text-xs font-medium text-slate-300">${escapeHtml(formatSalary(vacancy.salary_from, vacancy.salary_to, vacancy.salary_currency))}</p>
        <div class="mt-2 flex flex-wrap items-center gap-2">
          ${statusBadge(status)}
          ${sourceBadge(vacancy.source)}
          ${workFormatBadge(vacancy.work_format)}
          ${vacancy.experience ? `<span class="badge border-slate-700 bg-slate-800/60 text-slate-400">${escapeHtml(vacancy.experience)}</span>` : ''}
          ${published ? `<span class="text-[11px] text-slate-500">${escapeHtml(published)}</span>` : ''}
        </div>
      </div>
    </div>
    <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-800/70 pt-3">
      ${canViewAnalysis ? '<button type="button" data-action="view-analysis" class="btn-secondary btn-sm">Анализ</button>' : ''}
      ${canViewLetter ? '<button type="button" data-action="view-letter" class="btn-secondary btn-sm">Письмо</button>' : ''}
      ${canRespond ? '<button type="button" data-action="respond" class="btn-primary btn-sm">Откликнуться</button>' : ''}
      ${canApply ? '<button type="button" data-action="mark-applied" class="btn-ghost btn-sm">Откликнулся</button>' : ''}
      ${status === 'error' ? '<span class="text-xs text-rose-300">Ошибка обработки — проверьте журнал</span>' : ''}
      <button type="button" data-action="delete" class="btn-ghost btn-sm ml-auto text-rose-300 hover:text-rose-200">Удалить</button>
    </div>
  </article>`;
}

function renderPagination() {
  const { page, size } = getState().filters;
  els.pageInfo.textContent = `Страница ${page}${total !== null ? ` · всего ${total}` : ''}`;
  els.prev.disabled = page <= 1;
  els.next.disabled = total !== null ? page * size >= total : items.length < size;
}

function handleSelectionChange(event) {
  const checkbox = event.target.closest('[data-vacancy-select]');
  if (!checkbox) return;
  const card = checkbox.closest('[data-vacancy-id]');
  if (!card) return;
  if (checkbox.checked) selected.add(card.dataset.vacancyId);
  else selected.delete(card.dataset.vacancyId);
  card.classList.toggle('border-indigo-500/50', checkbox.checked);
  syncBatchBar();
  renderSelectAllState();
}

function renderSelectAllState() {
  if (!els.selectAll) return;
  const allSelected = items.length > 0 && items.every((item) => selected.has(item.id));
  els.selectAll.checked = allSelected;
  els.selectAll.indeterminate = !allSelected && items.some((item) => selected.has(item.id));
}

function syncBatchBar() {
  els.batchBar.classList.toggle('hidden', selected.size === 0);
  els.selectedCount.textContent = String(selected.size);
}

/* ---------- Действия со списком ---------- */

async function handleListClick(event) {
  const actionButton = event.target.closest('[data-action]');
  if (!actionButton) return;
  const action = actionButton.dataset.action;

  if (action === 'goto-dashboard') {
    emit('ui:goto-tab', 'dashboard');
    return;
  }

  const card = actionButton.closest('[data-vacancy-id]');
  const vacancyId = card?.dataset.vacancyId;
  if (!vacancyId) return;
  const vacancy = items.find((item) => item.id === vacancyId);

  switch (action) {
    case 'view-analysis': return openAnalysisModal(vacancyId, vacancy?.title);
    case 'view-letter': return openLetterDrawer(vacancyId, vacancy?.title);
    case 'respond': return respondToVacancy(vacancy);
    case 'mark-applied': return markApplied(vacancyId);
    case 'delete': return deleteVacancy(vacancyId, vacancy?.title);
    default: return undefined;
  }
}

/* ---------- Прямой отклик на hh.ru ---------- */

/** Точный текст успешного уведомления (используется в acceptance-критерии). */
const RESPOND_COPIED_MESSAGE = 'Письмо скопировано. Вставьте его (Ctrl+V) в поле сопроводительного письма на hh.ru';
const RESPOND_COPY_FAILED_MESSAGE = 'Браузер запретил доступ к буферу обмена. Скопируйте письмо вручную через кнопку «Письмо» и вставьте его на hh.ru.';

/**
 * Текст письма: из объекта вакансии → из кэша → из API GET /letters/{id}.
 * @returns {Promise<string>} '' — письма нет
 */
async function fetchLetterContent(vacancy) {
  if (vacancy?.cover_letter?.content) return vacancy.cover_letter.content;
  const cached = letterCache.get(vacancy?.id);
  if (cached) return cached;
  const letter = await api.getLetter(vacancy.id);
  const content = letter?.content || letter?.letter_text || '';
  if (content) letterCache.set(vacancy.id, content);
  return content;
}

/**
 * «Откликнуться»: копирует письмо в буфер и открывает страницу отклика hh.ru.
 * Вкладка открывается синхронно внутри обработчика клика, иначе браузер
 * заблокирует её как popup. Ошибка копирования не блокирует открытие.
 */
async function respondToVacancy(vacancy) {
  if (!vacancy) return;
  const hhId = vacancy.hh_vacancy_id;
  if (!hhId) {
    popup.warning('Отклик недоступен', 'У вакансии нет идентификатора hh.ru — откройте её по ссылке вручную.');
    return;
  }

  window.open(`https://hh.ru/applicant/vacancy_response?vacancyId=${encodeURIComponent(hhId)}`, '_blank', 'noopener,noreferrer');

  try {
    const content = await fetchLetterContent(vacancy);
    if (!content) {
      popup.warning('Письмо не найдено', 'Сгенерируйте сопроводительное письмо и повторите отклик.');
      return;
    }
    const copied = await copyText(content);
    if (copied) popup.success('Готово', RESPOND_COPIED_MESSAGE);
    else popup.warning('Не удалось скопировать письмо', RESPOND_COPY_FAILED_MESSAGE);
  } catch (error) {
    popup.warning('Не удалось скопировать письмо', RESPOND_COPY_FAILED_MESSAGE);
  }
}

async function markApplied(vacancyId) {
  try {
    await api.setVacancyStatus(vacancyId, 'applied');
    const item = items.find((entry) => entry.id === vacancyId);
    if (item) item.status = 'applied';
    renderList();
    popup.success('Статус обновлён', 'Вакансия отмечена как «Отклик отправлен».');
  } catch (error) {
    popup.error('Не удалось обновить статус', error.message);
  }
}

async function deleteVacancy(vacancyId, title) {
  const confirmed = await confirmDialog({
    title: 'Удалить вакансию?',
    message: `${title || 'Вакансия'} будет удалена вместе с анализом и письмом (docs/03 §4).`,
    confirmLabel: 'Удалить',
    danger: true
  });
  if (!confirmed) return;

  try {
    await api.deleteVacancy(vacancyId);
    items = items.filter((item) => item.id !== vacancyId);
    selected.delete(vacancyId);
    renderList();
    renderPagination();
    syncBatchBar();
    popup.success('Вакансия удалена');
  } catch (error) {
    popup.error('Не удалось удалить', error.message);
  }
}

/* ---------- Массовая обработка (docs/03 §6) ---------- */

async function runBatch() {
  if (selected.size === 0) return;
  const mode = els.batchMode.value;
  const thresholdRaw = els.batchThreshold.value;

  const payload = { vacancy_ids: [...selected], mode };
  if (thresholdRaw !== '') payload.match_threshold = Math.min(100, Math.max(0, Number(thresholdRaw)));

  toggleLoading(els.runBatch, true);
  try {
    const data = await api.runAnalysis(payload);
    popup.info('Обработка запущена', `${MODE_LABELS[mode] || mode} · вакансий: ${selected.size} · задача ${String(data?.task_id || '').slice(0, 8)}…`);
    selected.clear();
    syncBatchBar();
    renderList();
    await refreshTasks();
  } catch (error) {
    popup.error('Не удалось запустить обработку', error.message);
  } finally {
    toggleLoading(els.runBatch, false);
  }
}

function toggleLoading(button, loading) {
  if (!button) return;
  button.disabled = loading;
  button.querySelector('[data-spinner]')?.classList.toggle('hidden', !loading);
}

/* ---------- Модалка анализа ---------- */

async function openAnalysisModal(vacancyId, title) {
  currentAnalysis = { vacancyId, title: title || '' };
  document.getElementById('analysis-vacancy-title').textContent = title || '';
  document.getElementById('analysis-body').innerHTML = '<div class="skeleton h-28"></div>';
  openOverlay('analysis-modal');

  try {
    const analysis = await api.getAnalysis(vacancyId);
    renderAnalysis(analysis);
  } catch (error) {
    const message = error instanceof ApiError && error.status === 404
      ? 'Анализ ещё не готов — запустите обработку вакансии.'
      : `Не удалось загрузить анализ: ${error.message}`;
    document.getElementById('analysis-body').innerHTML = `<p class="text-sm text-slate-400">${escapeHtml(message)}</p>`;
  }
}

function analysisList(value) {
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === 'string' && value.trim()) {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return parsed.map(String);
    } catch { /* обычная строка с переносами строк */ }
    return value.split('\n').map((line) => line.replace(/^[-•\d.\s]+/, '').trim()).filter(Boolean);
  }
  return [];
}

function renderAnalysis(analysis) {
  const score = analysis?.match_score;
  const threshold = getMatchThreshold();
  const strengths = analysisList(analysis?.strengths);
  const weaknesses = analysisList(analysis?.weaknesses);
  const scoreClass = score >= threshold ? 'text-emerald-300' : score >= Math.max(0, threshold - 15) ? 'text-amber-300' : 'text-rose-300';

  document.getElementById('analysis-body').innerHTML = `
    <div class="flex flex-wrap items-center gap-4">
      <div class="flex items-center gap-3">
        <span class="text-3xl font-extrabold ${scoreClass}">${score ?? '—'}</span>
        <div class="text-xs text-slate-400">
          <p>match_score из 100</p>
          <p>порог: ${threshold}%</p>
        </div>
      </div>
      <span class="ml-auto text-[11px] text-slate-500">${analysis?.updated_at ? `обновлено: ${escapeHtml(formatDateTime(analysis.updated_at))}` : ''}</span>
    </div>

    ${analysis?.summary ? `<div class="mt-4 rounded-xl border border-slate-800 bg-slate-950/60 p-3">
      <h4 class="text-xs font-semibold uppercase tracking-wide text-slate-400">Итоговый вывод</h4>
      <p class="mt-1.5 text-sm leading-relaxed text-slate-300">${escapeHtml(analysis.summary)}</p>
    </div>` : ''}

    <div class="mt-4 grid gap-4 sm:grid-cols-2">
      <div class="rounded-xl border border-emerald-500/20 bg-emerald-500/5 p-3">
        <h4 class="text-xs font-semibold uppercase tracking-wide text-emerald-300">Сильные стороны</h4>
        <ul class="mt-2 space-y-1.5 text-sm text-slate-300">
          ${strengths.length ? strengths.map((s) => `<li class="flex gap-2"><span class="text-emerald-400">✓</span><span>${escapeHtml(s)}</span></li>`).join('') : '<li class="text-slate-500">—</li>'}
        </ul>
      </div>
      <div class="rounded-xl border border-amber-500/20 bg-amber-500/5 p-3">
        <h4 class="text-xs font-semibold uppercase tracking-wide text-amber-300">Слабые стороны / риски</h4>
        <ul class="mt-2 space-y-1.5 text-sm text-slate-300">
          ${weaknesses.length ? weaknesses.map((s) => `<li class="flex gap-2"><span class="text-amber-400">⚠</span><span>${escapeHtml(s)}</span></li>`).join('') : '<li class="text-slate-500">—</li>'}
        </ul>
      </div>
    </div>`;
}

async function generateLetter(vacancyId) {
  try {
    await api.runAnalysis({ vacancy_ids: [vacancyId], mode: 'letter' });
    closeOverlay('analysis-modal');
    popup.info('Генерация письма запущена', 'Задача добавлена в очередь LLM.');
    await refreshTasks();
  } catch (error) {
    popup.error('Не удалось запустить генерацию', error.message);
  }
}

/* ---------- Drawer сопроводительного письма ---------- */

async function openLetterDrawer(vacancyId, title) {
  currentLetter = { vacancyId, title: title || '', content: '' };
  document.getElementById('letter-vacancy-title').textContent = title || '';
  document.getElementById('letter-content').textContent = 'Загрузка…';
  openOverlay('letter-drawer');
  await loadLetter(vacancyId);
}

async function loadLetter(vacancyId) {
  try {
    const letter = await api.getLetter(vacancyId);
    const content = letter?.content || letter?.letter_text || '';
    currentLetter.content = content;
    if (content) letterCache.set(vacancyId, content);
    document.getElementById('letter-content').textContent = content || 'Письмо ещё не создано — запустите генерацию.';
    document.getElementById('letter-version').textContent = `v${letter?.version ?? 1}`;
    document.getElementById('letter-updated').textContent = letter?.updated_at ? `обновлено: ${formatDateTime(letter.updated_at)}` : '';
  } catch (error) {
    document.getElementById('letter-content').textContent = error instanceof ApiError && error.status === 404
      ? 'Письмо ещё не создано — запустите генерацию.'
      : `Не удалось загрузить письмо: ${error.message}`;
  }
}

function letterFileName() {
  const base = (currentLetter.title || 'cover-letter').toLowerCase()
    .replace(/[^\wа-яё]+/gi, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
  return `${base || 'cover-letter'}.txt`;
}

async function copyLetter() {
  if (!currentLetter.content) {
    popup.warning('Письмо пустое', 'Сначала сгенерируйте письмо.');
    return;
  }
  const ok = await copyText(currentLetter.content);
  if (ok) popup.success('Скопировано', 'Письмо в буфере обмена — можно вставлять на hh.ru.');
  else popup.error('Не удалось скопировать', 'Буфер обмена недоступен.');
}

function downloadLetter() {
  if (!currentLetter.content) {
    popup.warning('Письмо пустое', 'Сначала сгенерируйте письмо.');
    return;
  }
  downloadText(letterFileName(), currentLetter.content);
  popup.success('Файл сохранён', letterFileName());
}

/* Кнопки модалки анализа и drawer — единая точка делегирования. */
document.addEventListener('click', (event) => {
  if (event.target.closest('#btn-copy-letter')) {
    copyLetter();
  } else if (event.target.closest('#btn-download-letter')) {
    downloadLetter();
  } else if (event.target.closest('#btn-regen-letter')) {
    if (currentLetter.vacancyId) generateLetter(currentLetter.vacancyId);
  } else if (event.target.closest('#btn-analysis-gen-letter')) {
    if (currentAnalysis.vacancyId) generateLetter(currentAnalysis.vacancyId);
  }
});
