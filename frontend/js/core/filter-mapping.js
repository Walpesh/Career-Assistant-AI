/* ============================================================
   Маппинг UI-подписей фильтров Автопоиска → коды-энумы бэкенда.

   Источник истины — backend/app/modules/parsing/schemas.py →
   ParseAutoRequest (docs/03_API_CONTRACTS.md §5, docs/04 §4.1):

     employment_forms: ["full","part","project","volunteer","probation","gph"]
     work_formats:     ["remote","hybrid","onsite","office"]
     schedules:        ["fullDay","flexible","shift","remote","flyInFlyOut"]

   Бэкенд отклоняет (400 VALIDATION_ERROR) любое значение вне этих
   списков — см. tests/test_autosearch_scenarios.py::
   test_s4_rejects_russian_labels (нет silent-fallback). Поэтому перед
   сериализацией payload значения приводятся к кодам:
     · русская подпись чипа → код бэкенда;
     · уже валидный код      → без изменений (так работают текущие
       чекбоксы dashboard.html, у которых input.value = код);
     · неизвестное значение  → отбрасывается, чтобы запрос гарантированно
       не получил 400 из-за несовпадения строки.
   ============================================================ */

/** Форма занятости: русская подпись чипа → код бэкенда. */
export const EMPLOYMENT_FORM_MAP = Object.freeze({
  'Полная': 'full',
  'Частичная': 'part',
  'Проектная': 'project',
  'Волонтёрство': 'volunteer',
  'Стажировка': 'probation',
  'ГПХ': 'gph'
});

/** Формат работы: русская подпись чипа → код бэкенда. */
export const WORK_FORMAT_MAP = Object.freeze({
  'Удалённо': 'remote',
  'Гибрид': 'hybrid',
  'Офис': 'onsite'
});

/** График работы: русская подпись чипа → код бэкенда. */
export const SCHEDULE_MAP = Object.freeze({
  'Полный день': 'fullDay',
  'Сменный': 'shift',
  'Гибкий': 'flexible',
  'Вахта': 'flyInFlyOut'
});

/**
 * Допустимые коды бэкенда (источник истины — schemas.py Literal).
 * ``office`` оставлен в work_formats наряду с ``onsite``: бэкенд принимает
 * оба, а UI использует ``onsite`` («Офис»).
 */
export const EMPLOYMENT_FORM_CODES = Object.freeze(['full', 'part', 'project', 'volunteer', 'probation', 'gph']);
export const WORK_FORMAT_CODES = Object.freeze(['remote', 'hybrid', 'onsite', 'office']);
export const SCHEDULE_CODES = Object.freeze(['fullDay', 'flexible', 'shift', 'remote', 'flyInFlyOut']);

/**
 * Привести массив выбранных значений к кодам бэкенда.
 *
 * Значение проходит без изменений, если оно уже является допустимым кодом
 * (текущие чекбоксы отдают именно коды). Русская подпись транслируется в
 * код. Неизвестное значение отбрасывается — запрос не должен упасть в 400.
 *
 * @param {Iterable<string|null|undefined>} values — выбранные значения (коды или подписи)
 * @param {Record<string,string>} labelMap — словарь «подпись → код»
 * @param {readonly string[]} validCodes — допустимые коды бэкенда
 * @returns {string[]} коды бэкенда без дублей, порядок ввода сохранён
 */
export function mapFilterValues(values, labelMap, validCodes) {
  const codes = new Set(validCodes);
  const out = [];
  const seen = new Set();
  for (const raw of values || []) {
    if (raw === null || raw === undefined) continue;
    const value = String(raw).trim();
    if (!value) continue;
    // Уже код — пропускаем как есть; иначе транслируем подпись.
    const code = codes.has(value) ? value : labelMap[value];
    if (code && !seen.has(code)) {
      seen.add(code);
      out.push(code);
    }
  }
  return out;
}

/**
 * Собрать три фильтра Автопоиска в коды бэкенда для payload.
 *
 * @param {{employment?:string[], work_formats?:string[], schedules?:string[]}} selected
 * @returns {{employment_forms:string[], work_formats:string[], schedules:string[]}}
 */
export function mapAutoFilters(selected = {}) {
  return {
    employment_forms: mapFilterValues(selected.employment, EMPLOYMENT_FORM_MAP, EMPLOYMENT_FORM_CODES),
    work_formats: mapFilterValues(selected.work_formats, WORK_FORMAT_MAP, WORK_FORMAT_CODES),
    schedules: mapFilterValues(selected.schedules, SCHEDULE_MAP, SCHEDULE_CODES)
  };
}
