/* Юнит-тесты маппинга фильтров Автопоиска (node --test, без зависимостей).

   Проверяют контракт с backend/app/modules/parsing/schemas.py →
   ParseAutoRequest: русские подписи → коды бэкенда, коды проходят без
   изменений, неизвестные значения отбрасываются (нет 400).

   Запуск: cd frontend && node --test js/core/filter-mapping.test.js
*/

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  EMPLOYMENT_FORM_CODES,
  EMPLOYMENT_FORM_MAP,
  SCHEDULE_CODES,
  SCHEDULE_MAP,
  WORK_FORMAT_CODES,
  WORK_FORMAT_MAP,
  mapAutoFilters,
  mapFilterValues
} from './filter-mapping.js';

test('подписи формы занятости → коды бэкенда', () => {
  assert.deepEqual(
    mapFilterValues(['Полная', 'Частичная', 'Проектная', 'Волонтёрство', 'Стажировка', 'ГПХ'], EMPLOYMENT_FORM_MAP, EMPLOYMENT_FORM_CODES),
    ['full', 'part', 'project', 'volunteer', 'probation', 'gph']
  );
});

test('подписи формата работы и графика → коды бэкенда', () => {
  assert.deepEqual(mapFilterValues(['Удалённо', 'Гибрид', 'Офис'], WORK_FORMAT_MAP, WORK_FORMAT_CODES), ['remote', 'hybrid', 'onsite']);
  assert.deepEqual(mapFilterValues(['Полный день', 'Сменный', 'Гибкий', 'Вахта'], SCHEDULE_MAP, SCHEDULE_CODES), ['fullDay', 'shift', 'flexible', 'flyInFlyOut']);
});

test('суточные коды из словарей совпадают со схемой бэкенда', () => {
  // Регрессия: раньше предлагались неверные коды partial/internship/rotation,
  // которые бэкенд отклоняет 400. Фиксируем корректные значения.
  assert.deepEqual(Object.values(EMPLOYMENT_FORM_MAP), ['full', 'part', 'project', 'volunteer', 'probation', 'gph']);
  assert.deepEqual(Object.values(SCHEDULE_MAP), ['fullDay', 'shift', 'flexible', 'flyInFlyOut']);
});

test('уже валидные коды проходят без изменений (текущие чекбоксы)', () => {
  assert.deepEqual(mapFilterValues(['full', 'part'], EMPLOYMENT_FORM_MAP, EMPLOYMENT_FORM_CODES), ['full', 'part']);
  assert.deepEqual(mapFilterValues(['onsite', 'fullDay', 'flyInFlyOut'], WORK_FORMAT_CODES.length ? SCHEDULE_MAP : SCHEDULE_MAP, SCHEDULE_CODES), ['fullDay', 'flyInFlyOut']);
});

test('неизвестные значения отбрасываются (нет 400)', () => {
  // Некорректные коды из старой постановки задачи не должны протекать в payload.
  assert.deepEqual(mapFilterValues(['partial', 'Полная', 'internship'], EMPLOYMENT_FORM_MAP, EMPLOYMENT_FORM_CODES), ['full']);
  assert.deepEqual(mapFilterValues(['rotation', 'Полный день', ''], SCHEDULE_MAP, SCHEDULE_CODES), ['fullDay']);
});

test('дедупликация с сохранением порядка', () => {
  assert.deepEqual(mapFilterValues(['full', 'Полная', 'part'], EMPLOYMENT_FORM_MAP, EMPLOYMENT_FORM_CODES), ['full', 'part']);
});

test('пустые/null/пробельные значения игнорируются', () => {
  assert.deepEqual(mapFilterValues([], EMPLOYMENT_FORM_MAP, EMPLOYMENT_FORM_CODES), []);
  assert.deepEqual(mapFilterValues([null, undefined, '   '], SCHEDULE_MAP, SCHEDULE_CODES), []);
});

test('mapAutoFilters собирает три фильтра сразу', () => {
  assert.deepEqual(
    mapAutoFilters({
      employment: ['Полная'],
      work_formats: ['Удалённо'],
      schedules: ['Полный день']
    }),
    {
      employment_forms: ['full'],
      work_formats: ['remote'],
      schedules: ['fullDay']
    }
  );
});

test('mapAutoFilters на пустом выборе даёт пустые массивы', () => {
  assert.deepEqual(mapAutoFilters({}), { employment_forms: [], work_formats: [], schedules: [] });
  assert.deepEqual(mapAutoFilters(), { employment_forms: [], work_formats: [], schedules: [] });
});
