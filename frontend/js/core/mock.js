/* ============================================================
   Демо-режим (?demo=1): фиктивные данные и WebSocket-симуляция
   строго по контрактам docs/03_API_CONTRACTS.md.
   Позволяет проверять UI без запущенного backend.
   ============================================================ */

import { api, ApiError } from './api.js';
import { emit } from './bus.js';
import { session } from './session.js';
import { sleep } from './utils.js';

/* ---------- Время ---------- */

const daysAgo = (n) => new Date(Date.now() - n * 86400000).toISOString();
const minutesAgo = (n) => new Date(Date.now() - n * 60000).toISOString();

/* ---------- Отправка WS-событий в шину (как это делает ws.js) ---------- */

function emitWs(event, payload) {
  emit('ws:event', { event, payload });
  emit(`ws:${event}`, payload);
}

/* ---------- Пользователь и профиль ---------- */

const DEMO_USER = { id: 'demo-user-0001', email: 'demo@career.local', is_active: true };

const COMPACT_RESUME_DEMO = [
  'Python-разработчик, 4.5 года опыта (backend, автоматизация, интеграции).',
  'Стек: Python 3.12, FastAPI, SQLAlchemy, PostgreSQL, Redis, Docker, asyncio, Playwright.',
  'Опыт: разработка API под 200+ rps; ETL-пайплайны на 40+ источников; интеграция LLM (Ollama) в рабочие процессы.',
  'Достижения: сократил время обработки заявок на 65% (микросервисы), внедрил мониторинг и алертинг (Grafana).',
  'Образование: НГТУ, ИВТ (бакалавр). Готов к удалённой и гибридной работе.'
].join('\n');

let PROFILE = {
  user_id: DEMO_USER.id,
  full_name: 'Иван Иванов',
  resume_text: [
    'Иван Иванов — Python-разработчик.',
    '',
    'Опыт работы:',
    '· ООО «Сибсофт» — Python-разработчик (2022—2026): проектирование REST API (FastAPI), работа с PostgreSQL, Redis, Docker; ускорение выгрузок на 65%.',
    '· «Дата-Крипт» — Junior Python (2021—2022): ETL, парсинг, Telegram-боты.',
    '',
    'Навыки: Python, FastAPI, Django, SQLAlchemy, PostgreSQL, Redis, Docker, Git, Linux, Playwright, asyncio.',
    'Образование: НГТУ, Информатика и вычислительная техника (бакалавр, 2021).'
  ].join('\n'),
  compact_resume: COMPACT_RESUME_DEMO,
  skills: ['Python', 'FastAPI', 'PostgreSQL', 'Redis', 'Docker', 'asyncio', 'Playwright'],
  experience_years: 4.5,
  desired_salary_from: 180000,
  desired_salary_to: 250000,
  match_threshold: 75,
  preferred_work_formats: ['remote', 'hybrid'],
  analysis_preferences: 'Не хочу трудоустройство по ТК РФ. Нужен удалённый формат. Готов к переезду только в Санкт-Петербург.',
  resume_addition: 'Готов обсудить условия и приехать на собеседование в удобное время.',
  created_at: daysAgo(40),
  updated_at: daysAgo(2)
};

/* ---------- Вакансии (поля — docs/02_DATABASE.md §3.3) ---------- */

const VACANCIES = [
  {
    id: 'v-01', hh_vacancy_id: '137866214', url: 'https://novokuznetsk.hh.ru/vacancy/137866214',
    title: 'Старший Python-разработчик (FastAPI)', company_name: 'Нетворк Технологии',
    salary_from: 220000, salary_to: 280000, salary_currency: 'RUR',
    experience: '3–6 лет', employment_form: 'full', work_format: 'remote', schedule: 'fullDay',
    area: 'Новокузнецк', published_at: daysAgo(1), description_raw: 'Разработка высоконагруженного API, микросервисы, asyncio, PostgreSQL.',
    status: 'letter_ready', match_score: 87, source: 'auto', created_at: daysAgo(1), updated_at: minutesAgo(40)
  },
  {
    id: 'v-02', hh_vacancy_id: '135541238', url: 'https://novokuznetsk.hh.ru/vacancy/135541238',
    title: 'Backend-разработчик Python/Django', company_name: 'Клауд Софт',
    salary_from: 180000, salary_to: 230000, salary_currency: 'RUR',
    experience: '3–6 лет', employment_form: 'full', work_format: 'hybrid', schedule: 'flexible',
    area: 'Новокузнецк', published_at: daysAgo(2), description_raw: 'Django, DRF, интеграции с внешними сервисами, PostgreSQL.',
    status: 'analyzed', match_score: 74, source: 'auto', created_at: daysAgo(2), updated_at: minutesAgo(90)
  },
  {
    id: 'v-03', hh_vacancy_id: '136112345', url: 'https://novokuznetsk.hh.ru/vacancy/136112345',
    title: 'Разработчик Telegram-ботов (Python)', company_name: 'Регард Диджитал',
    salary_from: 120000, salary_to: 160000, salary_currency: 'RUR',
    experience: '1–3 года', employment_form: 'full', work_format: 'remote', schedule: 'fullDay',
    area: 'Удалённо', published_at: daysAgo(3), description_raw: 'Боты на aiogram, интеграции с CRM, очереди задач.',
    status: 'raw', match_score: null, source: 'group', created_at: daysAgo(3), updated_at: daysAgo(3)
  },
  {
    id: 'v-04', hh_vacancy_id: '134887654', url: 'https://moscow.hh.ru/vacancy/134887654',
    title: 'Data Engineer (Junior+)', company_name: 'Дата Крафт',
    salary_from: 150000, salary_to: 190000, salary_currency: 'RUR',
    experience: '1–3 года', employment_form: 'full', work_format: 'remote', schedule: 'fullDay',
    area: 'Москва', published_at: daysAgo(4), description_raw: 'ETL-пайплайны, Airflow, ClickHouse, оптимизация запросов.',
    status: 'analyzed', match_score: 61, source: 'auto', created_at: daysAgo(4), updated_at: daysAgo(1)
  }
];

VACANCIES.push(
  {
    id: 'v-05', hh_vacancy_id: '137455111', url: 'https://novokuznetsk.hh.ru/vacancy/137455111',
    title: 'Python-разработчик (LLM/AI)', company_name: 'Нейро Лаб',
    salary_from: 250000, salary_to: 320000, salary_currency: 'RUR',
    experience: '3–6 лет', employment_form: 'full', work_format: 'remote', schedule: 'flexible',
    area: 'Удалённо', published_at: daysAgo(1), description_raw: 'Интеграция LLM (Ollama, OpenAI API), RAG, оптимизация промптов.',
    status: 'letter_ready', match_score: 93, source: 'manual', created_at: daysAgo(1), updated_at: minutesAgo(15)
  },
  {
    id: 'v-06', hh_vacancy_id: '133298765', url: 'https://novokuznetsk.hh.ru/vacancy/133298765',
    title: 'Fullstack-разработчик (Django + Vue)', company_name: 'Сибирь Софт',
    salary_from: 170000, salary_to: 220000, salary_currency: 'RUR',
    experience: '3–6 лет', employment_form: 'full', work_format: 'onsite', schedule: 'fullDay',
    area: 'Новокузнецк', published_at: daysAgo(6), description_raw: 'Django REST + Vue 3, внутренние продукты компании.',
    status: 'applied', match_score: 78, source: 'auto', created_at: daysAgo(6), updated_at: daysAgo(1)
  },
  {
    id: 'v-07', hh_vacancy_id: '136770012', url: 'https://novokuznetsk.hh.ru/vacancy/136770012',
    title: 'Автоматизатор на Python (парсинг данных)', company_name: 'ФинТех Лаб',
    salary_from: 140000, salary_to: 180000, salary_currency: 'RUR',
    experience: '1–3 года', employment_form: 'gph', work_format: 'hybrid', schedule: 'flexible',
    area: 'Новокузнецк', published_at: daysAgo(5), description_raw: 'Сбор данных, Playwright/curl, устойчивость к блокировкам, отчётность.',
    status: 'raw', match_score: null, source: 'manual', created_at: daysAgo(5), updated_at: daysAgo(5)
  },
  {
    id: 'v-08', hh_vacancy_id: '132145889', url: 'https://moscow.hh.ru/vacancy/132145889',
    title: 'Стажёр Python-разработчик', company_name: 'Академ Софт',
    salary_from: 60000, salary_to: 80000, salary_currency: 'RUR',
    experience: 'нет опыта', employment_form: 'probation', work_format: 'onsite', schedule: 'fullDay',
    area: 'Москва', published_at: daysAgo(8), description_raw: 'Обучение, задачи на Python, поддержка legacy-кода.',
    status: 'error', match_score: null, source: 'group', created_at: daysAgo(8), updated_at: daysAgo(7)
  }
);

/* Пул вакансий, «найденных» автопоиском в демо-режиме. */
const PARSE_POOL = [
  ['Python-разработчик (интеграции)', 'Интегра Софт', 160000, 200000, 'remote'],
  ['Backend-разработчик (FastAPI, Redis)', 'Очередь Технолоджи', 190000, 240000, 'hybrid'],
  ['Python Developer (парсинг и антибот)', 'Скрапер Лайн', 150000, 210000, 'remote']
];
let poolIndex = 0;

/* ---------- Анализы (docs/02 §3.4) ---------- */

const ANALYSES = {
  'v-01': {
    id: 'a-01', vacancy_id: 'v-01', match_score: 87,
    strengths: [
      '4.5 года опыта backend-разработки на Python — закрывает требование «3–6 лет».',
      'Прямое совпадение стека: FastAPI, PostgreSQL, Redis, Docker.',
      'Опыт микросервисов и asyncio под высокую нагрузку (200+ rps).'
    ],
    weaknesses: [
      'Нет упоминания Kubernetes — в вакансии указан как плюс.',
      'Английский не подтверждён сертификатом (требуется Upper-Intermediate).'
    ],
    summary: 'Сильный кандидат с прямым совпадением по стеку и уровню. Ключевой риск — отсутствие Kubernetes, но это не блокирующее требование.',
    updated_at: minutesAgo(40)
  },
  'v-02': {
    id: 'a-02', vacancy_id: 'v-02', match_score: 74,
    strengths: 'Опыт Django/Django REST — профильный для вакансии.\nПонимание интеграций и работы с внешними API.\nУверенный PostgreSQL.',
    weaknesses: 'Часть стека (DRF) не отражена в последнем проекте — потребуется подтвердить на собеседовании.\nОжидания по зарплате у верхней границы вилки.',
    summary: 'Хорошее соответствие с умеренными рисками. Имеет смысл откликаться с акцентом на интеграционный опыт.',
    updated_at: minutesAgo(90)
  },
  'v-04': {
    id: 'a-04', vacancy_id: 'v-04', match_score: 61,
    strengths: ['Опыт работы с данными и ETL-задачами.', 'Python-стек переносится на инструменты Data Engineer.'],
    weaknesses: ['Нет опыта Airflow и ClickHouse.', 'Junior+ предполагает меньше самостоятельности — возможен пересмотр грейда.'],
    summary: 'Частичное соответствие. Рекомендуется отклик только при готовности быстро закрыть пробелы по Airflow и ClickHouse.',
    updated_at: daysAgo(1)
  },
  'v-05': {
    id: 'a-05', vacancy_id: 'v-05', match_score: 93,
    strengths: [
      'Практический опыт интеграции LLM (Ollama) в рабочие процессы.',
      'Совпадение по всему ключевому стеку и формату работы (удалённо).',
      'Опыт оптимизации промптов и обработки больших текстов.'
    ],
    weaknesses: ['RAG-пайплайны в продакшене — только базовый уровень.'],
    summary: 'Практически идеальное совпадение. Приоритетная вакансия для отклика.',
    updated_at: minutesAgo(15)
  },
  'v-06': {
    id: 'a-06', vacancy_id: 'v-06', match_score: 78,
    strengths: ['Django-опыт закрывает backend-часть.', 'Знаком с Vue на уровне поддержки проектов.'],
    weaknesses: ['Frontend-экспертиза ниже требуемой для самостоятельных задач.'],
    summary: 'Подходящий вариант, если акцент роли — backend при поддержке frontend.',
    updated_at: daysAgo(1)
  }
};

/* ---------- Сопроводительные письма (docs/02 §3.5) ---------- */

/**
 * Текст «Хотите добавить информацию в конец резюме?» дописывается в конец
 * письма «с красной строки» — скриптовым методом, как в backend
 * (analysis_letter/llm.append_resume_addition).
 */
function appendResumeAddition(letter, addition) {
  const extra = String(addition || '').trim();
  if (!extra) return letter;
  const body = String(letter || '').trimEnd();
  if (body.endsWith(extra)) return body;
  return body ? `${body}\n\n${extra}` : extra;
}

function buildLetter(title, company) {
  const base = [
    'Здравствуйте! Меня зовут Иван, я Python-разработчик с 4.5 годами опыта в backend-разработке и автоматизации.',
    '',
    `Вакансия «${title}» в компании ${company} совпадает с моим профилем: последние два года я проектировал REST API на FastAPI, работал с PostgreSQL и Redis, строил ETL-пайплайны и внедрял LLM-инструменты в рабочие процессы. В одном из проектов сократил время обработки заявок на 65% за счёт перехода на асинхронные микросервисы.`,
    '',
    'Что я могу дать команде:',
    '— быстрый выход на продуктивность за счёт прямого совпадения по стеку;',
    '— аккуратный код с тестами и понятной архитектурой;',
    '— опыт устойчивых интеграций и работы с данными под нагрузкой.',
    '',
    'Готов обсудить задачи команды и показать примеры кода на созвоне. Удобно созвониться на этой неделе?'
  ].join('\n');
  return appendResumeAddition(base, PROFILE.resume_addition);
}

const LETTERS = {
  'v-01': { id: 'l-01', vacancy_id: 'v-01', content: buildLetter('Старший Python-разработчик (FastAPI)', 'Нетворк Технологии'), version: 2, updated_at: minutesAgo(38) },
  'v-05': { id: 'l-05', vacancy_id: 'v-05', content: buildLetter('Python-разработчик (LLM/AI)', 'Нейро Лаб'), version: 1, updated_at: minutesAgo(14) }
};

/* ---------- Задачи и симуляция (docs/02 §3.6, docs/03 §7) ---------- */

const TASKS = [];
let taskCounter = 0;

function makeTask(task_type, payload = {}) {
  taskCounter += 1;
  const task = {
    id: `task-${String(taskCounter).padStart(4, '0')}-${Math.random().toString(36).slice(2, 6)}`,
    task_type,
    payload,
    status: 'pending',
    progress_current: 0,
    progress_total: 0,
    progress_message: null,
    error_message: null,
    result: null,
    created_at: new Date().toISOString(),
    started_at: null,
    finished_at: null
  };
  TASKS.unshift(task);
  emitWs('task.created', { task_id: task.id, task_type });
  return task;
}

function taskProgress(task, current, total, stage, message) {
  task.status = 'processing';
  task.progress_current = current;
  task.progress_total = total;
  task.progress_message = message;
  if (!task.started_at) task.started_at = new Date().toISOString();
  emitWs('task.progress', { task_id: task.id, current, total, stage, message });
}

async function finishTask(task, result = null, popupData = null) {
  task.status = 'completed';
  task.result = result;
  task.finished_at = new Date().toISOString();
  emitWs('task.completed', { task_id: task.id, result });
  if (popupData) emitWs('popup', popupData);
}

function scoreFor(id) {
  const seed = [...String(id)].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return 55 + (seed % 40); // 55..94
}

function addVacancy(patch = {}) {
  const hhId = patch.hh_vacancy_id || String(130000000 + Math.floor(Math.random() * 9000000));
  const vacancy = {
    id: `v-${String(100 + Math.floor(Math.random() * 800))}`,
    hh_vacancy_id: hhId,
    url: patch.url || `https://novokuznetsk.hh.ru/vacancy/${hhId}`,
    title: patch.title || 'Новая вакансия',
    company_name: patch.company_name || 'Компания',
    salary_from: patch.salary_from ?? 100000,
    salary_to: patch.salary_to ?? 150000,
    salary_currency: 'RUR',
    experience: '1–3 года',
    employment_form: 'full',
    work_format: patch.work_format || 'remote',
    schedule: 'fullDay',
    area: 'Новокузнецк',
    published_at: new Date().toISOString(),
    description_raw: 'Демо-описание вакансии.',
    status: 'raw',
    match_score: null,
    source: patch.source || 'auto',
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  };
  VACANCIES.unshift(vacancy);
  emitWs('vacancy.updated', { vacancy_id: vacancy.id, status: vacancy.status });
  return vacancy;
}

async function simulateParse(task, { source = 'auto', keywords = [], vacancyUrl = null, runAnalysis = false, blacklist = null }) {
  await sleep(250);
  const total = vacancyUrl ? 2 : Math.min(10, 4 + Math.max(1, keywords.length));

  for (let i = 1; i <= total; i += 1) {
    await sleep(650);
    taskProgress(task, i, total, 'parsing_vacancy', `Парсинг вакансии ${i} из ${total}`);
  }

  const created = [];
  let skipped = 0;
  // Чёрный список слов (docs/04 §4.9): работает только при включённом тумблере.
  const blocked = blacklist && blacklist.enabled
    ? (blacklist.words || []).map((w) => String(w).trim().toLowerCase()).filter(Boolean)
    : [];
  const isBlocked = (vacancy) => blocked.some((word) =>
    [vacancy.title, vacancy.company_name, vacancy.description_raw]
      .filter(Boolean)
      .some((part) => String(part).toLowerCase().includes(word))
  );

  if (vacancyUrl) {
    const hhId = (vacancyUrl.match(/vacancy\/(\d+)/) || [])[1] || '136000000';
    const vacancy = {
      title: `Вакансия hh.ru №${hhId}`,
      company_name: 'Компания с hh.ru',
      source: 'manual',
      url: vacancyUrl,
      hh_vacancy_id: hhId
    };
    if (isBlocked(vacancy)) skipped += 1;
    else created.push(addVacancy(vacancy));
  } else {
    const slots = source === 'group' ? 2 : 1;
    for (let i = 0; i < slots && poolIndex < PARSE_POOL.length; i += 1) {
      const [title, company, from, to, format] = PARSE_POOL[poolIndex];
      poolIndex += 1;
      const vacancy = { title, company_name: company, salary_from: from, salary_to: to, work_format: format, source };
      if (isBlocked(vacancy)) skipped += 1;
      else created.push(addVacancy(vacancy));
    }
  }

  await finishTask(
    task,
    { found: created.length + skipped, saved: created.length, blacklisted: skipped, vacancy_ids: created.map((v) => v.id) },
    skipped
      ? { type: 'warning', title: 'Парсинг завершён', message: `Сохранено: ${created.length}, отсечено чёрным списком: ${skipped}` }
      : { type: 'success', title: 'Парсинг завершён', message: `Сохранено вакансий: ${created.length}` }
  );

  if (runAnalysis && created[0]) {
    const analysisTask = makeTask('analyze');
    simulateAnalysis(analysisTask, [created[0].id], 'analyze');
  }
}

async function simulateAnalysis(task, vacancyIds, mode, thresholdOverride) {
  const threshold = Number(thresholdOverride ?? PROFILE.match_threshold ?? 70);
  const total = vacancyIds.length;
  let current = 0;

  for (const vacancyId of vacancyIds) {
    current += 1;
    const vacancy = VACANCIES.find((entry) => entry.id === vacancyId) || { id: vacancyId, title: 'Вакансия', company_name: '' };
    const withAnalysis = mode !== 'letter';
    const score = scoreFor(vacancyId);

    if (withAnalysis) {
      await sleep(700);
      taskProgress(task, current, total, 'analyzing', `Анализ вакансии ${current} из ${total}`);
      ANALYSES[vacancyId] = {
        id: `a-${vacancyId}`,
        vacancy_id: vacancyId,
        match_score: score,
        strengths: [`Подтверждённый опыт под требования «${vacancy.title}».`, 'Релевантный стек и достижения с цифрами.'],
        weaknesses: ['Часть требований требует проверки на собеседовании.'],
        summary: `Соответствие по вакансии «${vacancy.title}» оценено в ${score} из 100 (демо-режим).`,
        updated_at: new Date().toISOString()
      };
      vacancy.match_score = score;
      emitWs('analysis.ready', { vacancy_id: vacancyId, match_score: score });
    }

    const needLetter = mode === 'letter' || mode === 'analyze_and_letter' || (mode === 'auto' && (!withAnalysis || score >= threshold));
    if (needLetter) {
      await sleep(650);
      taskProgress(task, current, total, 'generating_letter', `Генерация письма ${current} из ${total}`);
      const existing = LETTERS[vacancyId];
      LETTERS[vacancyId] = {
        id: `l-${vacancyId}`,
        vacancy_id: vacancyId,
        content: buildLetter(vacancy.title, vacancy.company_name),
        version: (existing?.version || 0) + 1,
        updated_at: new Date().toISOString()
      };
      emitWs('letter.ready', { vacancy_id: vacancyId });
    }

    vacancy.status = needLetter ? 'letter_ready' : (withAnalysis ? 'analyzed' : vacancy.status);
    vacancy.updated_at = new Date().toISOString();
    emitWs('vacancy.updated', { vacancy_id: vacancyId, status: vacancy.status });
  }

  await finishTask(task, { processed: total }, {
    type: 'success',
    title: 'Обработка завершена',
    message: `Обработано вакансий: ${total} (демо)`
  });
}

async function simulateConvert(task) {
  const steps = ['выделение ключевого опыта', 'сжатие стека и достижений', 'финальная вычитка текста'];
  for (let i = 0; i < steps.length; i += 1) {
    await sleep(700);
    taskProgress(task, i + 1, steps.length, 'converting_resume', `Сокращение резюме: ${steps[i]}`);
  }
  PROFILE.compact_resume = COMPACT_RESUME_DEMO;
  PROFILE.updated_at = new Date().toISOString();
  await finishTask(
    task,
    { compact_resume_length: COMPACT_RESUME_DEMO.length },
    { type: 'success', title: 'Резюме сконвертировано', message: `compact_resume готов (${COMPACT_RESUME_DEMO.length} симв.)` }
  );
}

/* ---------- Демо-WebSocket ---------- */

export class DemoSocket {
  constructor() {
    this.status = 'disconnected';
    this.timer = null;
  }

  connect() {
    this.status = 'connected';
    emit('ws:status', 'connected');
    emit('log:system', { level: 'info', message: 'Демо-режим: WebSocket-события симулируются локально' });
    this.timer = setInterval(() => this.tick(), 18000);
  }

  close() {
    clearInterval(this.timer);
    this.timer = null;
    this.status = 'disconnected';
    emit('ws:status', 'disconnected');
  }

  /** Фоновые события для «живости» журнала реал-тайм. */
  tick() {
    const roll = Math.random();
    if (roll < 0.5) {
      emitWs('popup', { type: 'info', title: 'Anti-Ban монитор', message: 'Доля капчи 0% — интенсивность парсинга в норме (демо).' });
    } else if (roll < 0.8) {
      const vacancy = VACANCIES[Math.floor(Math.random() * VACANCIES.length)];
      if (vacancy) emitWs('vacancy.updated', { vacancy_id: vacancy.id, status: vacancy.status });
    } else {
      emitWs('popup', { type: 'warning', title: 'Лимит запросов', message: '60–80 запросов с одного IP — запланирована смена прокси (демо).' });
    }
  }
}

/* ---------- Подмена api-клиента фиктивными реализациями ---------- */

export function installMock() {
  /* Auth */
  api.login = async () => {
    await sleep(400);
    session.setTokens({ access_token: 'demo-access-token', refresh_token: 'demo-refresh-token' });
    return { access_token: 'demo-access-token', refresh_token: 'demo-refresh-token', token_type: 'bearer' };
  };

  api.register = async (email) => {
    await sleep(400);
    return { id: 'demo-user-0001', email };
  };

  api.me = async () => {
    await sleep(150);
    return { ...DEMO_USER };
  };

  api.refresh = async () => ({ access_token: 'demo-access-token' });

  api.logout = async () => ({ revoked: true });

  api.wsTicket = async () => ({ ticket: 'demo-ws-ticket', expires_in: 30 });

  /* Profile */
  api.getProfile = async () => {
    await sleep(200);
    return { ...PROFILE };
  };

  api.updateProfile = async (patch) => {
    await sleep(350);
    Object.assign(PROFILE, patch, { updated_at: new Date().toISOString() });
    return { ...PROFILE };
  };

  api.convertResume = async () => {
    await sleep(250);
    const task = makeTask('convert_resume');
    simulateConvert(task);
    return { task_id: task.id, status: 'pending' };
  };

  /* Parsing */
  api.parseAuto = async (payload = {}) => {
    await sleep(250);
    const task = makeTask('parse_auto', payload);
    simulateParse(task, {
      source: 'auto',
      keywords: payload.keywords || [],
      blacklist: { enabled: Boolean(payload.blacklist_enabled), words: payload.blacklist_words || [] }
    });
    return { task_id: task.id, status: 'pending' };
  };

  api.parseGroup = async (payload = {}) => {
    await sleep(250);
    const task = makeTask('parse_group', payload);
    simulateParse(task, {
      source: 'group',
      blacklist: { enabled: Boolean(payload.blacklist_enabled), words: payload.blacklist_words || [] }
    });
    return { task_id: task.id, status: 'pending' };
  };

  api.parseManual = async (payload = {}) => {
    await sleep(250);
    const task = makeTask('parse_manual', payload);
    simulateParse(task, { source: 'manual', vacancyUrl: payload.vacancy_url, runAnalysis: Boolean(payload.run_analysis) });
    return { task_id: task.id, status: 'pending' };
  };

  /* Vacancies */
  api.listVacancies = async (params = {}) => {
    await sleep(250);
    let list = VACANCIES.slice();
    if (params.status) list = list.filter((v) => v.status === params.status);
    if (params.source) list = list.filter((v) => v.source === params.source);
    if (params.search) {
      const query = String(params.search).toLowerCase();
      list = list.filter((v) => (v.title || '').toLowerCase().includes(query) || (v.company_name || '').toLowerCase().includes(query));
    }
    if (params.min_match_score !== undefined && params.min_match_score !== '') {
      list = list.filter((v) => Number(v.match_score || 0) >= Number(params.min_match_score));
    }
    const page = Number(params.page || 1);
    const size = Number(params.size || 20);
    return {
      items: list.slice((page - 1) * size, page * size).map((v) => ({ ...v })),
      total: list.length,
      page,
      size
    };
  };

  api.getVacancy = async (id) => {
    await sleep(150);
    const vacancy = VACANCIES.find((v) => v.id === id);
    if (!vacancy) throw new ApiError(404, 'Вакансия не найдена', 'NOT_FOUND');
    return { ...vacancy };
  };

  api.deleteVacancy = async (id) => {
    await sleep(250);
    const index = VACANCIES.findIndex((v) => v.id === id);
    if (index === -1) throw new ApiError(404, 'Вакансия не найдена', 'NOT_FOUND');
    VACANCIES.splice(index, 1);
    return null;
  };

  api.setVacancyStatus = async (id, status) => {
    await sleep(250);
    const vacancy = VACANCIES.find((v) => v.id === id);
    if (!vacancy) throw new ApiError(404, 'Вакансия не найдена', 'NOT_FOUND');
    vacancy.status = status;
    vacancy.updated_at = new Date().toISOString();
    emitWs('vacancy.updated', { vacancy_id: id, status });
    return { ...vacancy };
  };

  /* Analysis & Letters */
  api.runAnalysis = async (payload = {}) => {
    await sleep(250);
    const mode = payload.mode || 'auto';
    const taskType = mode === 'analyze' ? 'analyze' : mode === 'letter' ? 'generate_letter' : 'auto_full';
    const task = makeTask(taskType, payload);
    simulateAnalysis(task, payload.vacancy_ids || [], mode, payload.match_threshold);
    return { task_id: task.id, status: 'pending' };
  };

  api.getAnalysis = async (vacancyId) => {
    await sleep(250);
    const analysis = ANALYSES[vacancyId];
    if (!analysis) throw new ApiError(404, 'Анализ не найден', 'NOT_FOUND');
    return { ...analysis };
  };

  api.getLetter = async (vacancyId) => {
    await sleep(250);
    const letter = LETTERS[vacancyId];
    if (!letter) throw new ApiError(404, 'Письмо не найдено', 'NOT_FOUND');
    return { ...letter };
  };

  /* Tasks */
  api.listTasks = async () => {
    await sleep(120);
    return { items: TASKS.map((task) => ({ ...task })) };
  };

  api.getTask = async (id) => {
    await sleep(100);
    const task = TASKS.find((entry) => entry.id === id);
    if (!task) throw new ApiError(404, 'Задача не найдена', 'NOT_FOUND');
    return { ...task };
  };

  api.cancelTask = async (id) => {
    await sleep(200);
    const task = TASKS.find((entry) => entry.id === id);
    if (!task) throw new ApiError(404, 'Задача не найдена', 'NOT_FOUND');
    if (['pending', 'processing', 'waiting_captcha'].includes(task.status)) {
      task.status = 'failed';
      task.error_message = 'Отменено пользователем';
      task.finished_at = new Date().toISOString();
      emitWs('task.cancelled', { task_id: task.id, status: 'failed', error: 'Отменено пользователем' });
      emitWs('task.failed', { task_id: task.id, error: 'Отменено пользователем' });
    }
    return { ...task };
  };

  api.resumeTask = async (id) => {
    await sleep(200);
    const task = TASKS.find((entry) => entry.id === id);
    if (!task) throw new ApiError(404, 'Задача не найдена', 'NOT_FOUND');
    if (task.status !== 'waiting_captcha') {
      throw new ApiError(409, 'Возобновить можно только waiting_captcha', 'TASK_NOT_WAITING_CAPTCHA');
    }
    task.status = 'pending';
    task.error_message = null;
    task.finished_at = null;
    emitWs('task.resumed', { task_id: task.id, status: 'pending' });
    return { task_id: task.id, status: 'pending', resumed: true };
  };
}
