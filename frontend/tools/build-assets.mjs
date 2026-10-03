#!/usr/bin/env node
/**
 * Career-Assistant-AI — контроль целостности и локальности статики.
 *
 * Задачи (docs/01_ARCHITECTURE.md §10 «Доставка статики и CSP»):
 *   1. Проверяет, что имена файлов шрифтов содержат content-hash и что
 *      src/fonts.css ссылается именно на них. Content-hash в имени —
 *      условие для `Cache-Control: immutable` в deploy/nginx.conf.
 *   2. Считает SRI-хэши (sha384, base64) для локальных ресурсов, на которые
 *      index.html / 404.html ссылаются с атрибутом integrity.
 *   3. Запрещает внешние (кросс-ориентные) ресурсы: любой IP-адрес пользователя,
 *      ушедший на чужой хост, — это трансфер данных, запрещённый 152-ФЗ и GDPR,
 *      а также несовместимый с CSP без явного allow-list.
 *   4. Проверяет, что в разметке нет inline-скриптов без nonce.
 *
 * Использование:
 *   node tools/build-assets.mjs           # пересчитать assets/manifest.json
 *   node tools/build-assets.mjs --check   # только проверка (0 — всё в порядке)
 *
 * В CI вызывается `npm run verify`; ненулевой код возврата валит сборку.
 */

import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CHECK_ONLY = process.argv.includes('--check');

/** Маркер единственного допустимого inline-скрипта (bootstrap под nonce). */
const BOOTSTRAP_MARKER = 'window.APP_CONFIG';

/** Документы, в которых ищем внешние ресурсы и inline-скрипты. */
const DOCUMENTS = ['index.html', '404.html'];

/** Каталоги со шрифтами: имя файла обязано содержать content-hash. */
const FONT_DIR = join(ROOT, 'fonts');

/** Куда складываем манифест хэшей. */
const MANIFEST = join(ROOT, 'assets', 'manifest.json');

const problems = [];

/** sha256 hex (8 символов) — для суффикса имени файла. */
function shortHash(buffer) {
  return createHash('sha256').update(buffer).digest('hex').slice(0, 8);
}

/** SRI-хэш в формате `sha384-<base64>`. */
function sriHash(buffer) {
  return `sha384-${createHash('sha384').update(buffer).digest('base64')}`;
}

function read(relPath) {
  const absolute = join(ROOT, relPath);
  if (!existsSync(absolute)) {
    problems.push(`отсутствует файл ${relPath} — запустите \`npm run build\``);
    return null;
  }
  return readFileSync(absolute);
}

function report(message) {
  console.error(`  ✗ ${message}`);
  problems.push(message);
}

/* ---------- 1. Синтаксис всех JS-модулей ---------- */

// node --check умеет разбирать ES-модули в каталоге с "type": "module"
// (package.json), поэтому проверяем каждый файл отдельным процессом.
// Это ловит синтаксические ошибки до запуска: без этого сломанный модуль
// падает уже в браузере, и страница остаётся без интерфейса.
const JS_DIRS = ['js', 'tools'];
const jsFiles = JS_DIRS.flatMap((dir) => {
  const absolute = join(ROOT, dir);
  if (!existsSync(absolute)) return [];
  return readdirSync(absolute, { recursive: true })
    .filter((name) => String(name).endsWith('.js'))
    .map((name) => join(dir, String(name)));
});

for (const relPath of jsFiles) {
  if (!existsSync(join(ROOT, relPath))) {
    report(`отсутствует файл ${relPath}`);
    continue;
  }
  const result = spawnSync(process.execPath, ['--check', join(ROOT, relPath)], {
    encoding: 'utf8'
  });
  if (result.status !== 0) {
    const detail = (result.stderr || '').split('\n').find((line) => line.includes('Error')) || 'parse error';
    report(`${relPath}: ${detail.trim()}`);
  }
}

/* ---------- 2. Шрифты: content-hash в имени файла ---------- */

const fontsCss = read('src/fonts.css')?.toString('utf8') ?? '';
const fontFiles = existsSync(FONT_DIR)
  ? readdirSync(FONT_DIR).filter((name) => name.endsWith('.woff2')).sort()
  : [];

if (!fontFiles.length) report('в frontend/fonts/ нет ни одного woff2-файла');

for (const name of fontFiles) {
  const buffer = readFileSync(join(FONT_DIR, name));
  const expected = shortHash(buffer);
  const match = name.match(/^inter-[a-z-]+\.([0-9a-f]{8})\.woff2$/);
  if (!match) {
    report(`имя шрифта ${name} не содержит content-hash (ожидается inter-<subset>.<hash>.woff2)`);
    continue;
  }
  if (match[1] !== expected) {
    report(`шрифт ${name}: хэш в имени (${match[1]}) не совпадает с содержимым (${expected})`);
    continue;
  }
  // Ссылка из fonts.css должна указывать ровно на этот файл.
  if (!fontsCss.includes(`../fonts/${name}`)) {
    report(`src/fonts.css не ссылается на шрифт fonts/${name}`);
  }
}

/* ---------- 2. Внешние ресурсы и inline-скрипты запрещены ---------- */

for (const doc of DOCUMENTS) {
  const html = read(doc)?.toString('utf8');
  if (html === undefined) continue;

  // Кросс-ориентные ресурсы: scheme://host/... Внутренние ссылки — относительные.
  const external = html.match(/(?:src|href)="https?:\/\/[^"]+"/g) || [];
  for (const match of external) {
    report(`${doc}: внешний ресурс ${match} — перенесите на локальный origin`);
  }

  // Inline-скрипты обязаны нести nonce (CSP без 'unsafe-inline').
  const inlineScripts = [...html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g)];
  for (const [, attrs, body] of inlineScripts) {
    if (!body.trim()) continue; // пустой <script> безвреден
    if (!/nonce=/.test(attrs)) {
      report(`${doc}: inline-скрипт без nonce — CSP его заблокирует`);
    }
    if (!body.includes(BOOTSTRAP_MARKER)) {
      report(`${doc}: inline-скрипт не является bootstrap'ом (${BOOTSTRAP_MARKER}) — вынесите в js/`);
    }
  }

  // Tailwind Play CDN недопустим в production.
  // Проверяем теги, а не весь текст: упоминания в HTML-комментариях
  // (объяснение, почему CDN удалён) — это нормально.
  if (/<script[^>]+src=["'][^"']*cdn\.tailwindcss\.com/.test(html)) {
    report(`${doc}: подключён Tailwind Play CDN — соберите статику через \`npm run build\``);
  }
  if (/<style[^>]+type=["']text\/tailwind/.test(html)) {
    report(`${doc}: остался <style type="text/tailwindcss"> — перенесите правила в src/input.css`);
  }
  if (/tailwind\.config\s*=/.test(html)) {
    report(`${doc}: осталась инлайновая тема tailwind.config — перенесите в tailwind.config.js`);
  }
}

/* ---------- 3. SRI-хэши локальных ресурсов ---------- */

/**
 * Ресурсы, на которые index.html / 404.html обязаны ссылаться
 * с `integrity` + `crossorigin="anonymous"`.
 *
 * Пути в href бывают относительными (index.html) и абсолютными (404.html —
 * его отдаёт error_page nginx на любом пути, поэтому корень не подставляется
 * относительно текущего URL), поэтому regex допускает оба варианта.
 */
const INTEGRITY_REQUIRED = [
  {
    file: 'css/styles.min.css',
    pattern: /<link[^>]+href="(?:\/)?css\/styles\.min\.css"[^>]*>/g
  },
  {
    file: 'fonts/inter-latin.3100e775.woff2',
    pattern: /<link[^>]+href="(?:\/)?fonts\/inter-latin\.3100e775\.woff2"[^>]*>/g
  },
  {
    file: 'fonts/inter-cyrillic.71d5ee93.woff2',
    pattern: /<link[^>]+href="(?:\/)?fonts\/inter-cyrillic\.71d5ee93\.woff2"[^>]*>/g
  }
];

const manifest = {};
for (const { file } of INTEGRITY_REQUIRED) {
  const buffer = read(file);
  if (buffer) manifest[file] = sriHash(buffer);
}
// Хэши шрифтов — для диагностики и принудительной инвалидации кэшей.
for (const name of fontFiles) {
  manifest[`fonts/${name}`] = sriHash(readFileSync(join(FONT_DIR, name)));
}

// Проверяем (и в режиме сборки — обновляем) integrity в разметке.
for (const { file, pattern } of INTEGRITY_REQUIRED) {
  for (const doc of DOCUMENTS) {
    const absolute = join(ROOT, doc);
    if (!existsSync(absolute)) continue;
    let html = readFileSync(absolute, 'utf8');
    let changed = false;

    for (const tag of html.match(pattern) || []) {
      const integrity = tag.match(/integrity="([^"]+)"/)?.[1];
      if (!integrity) {
        report(`${doc}: ссылка на ${file} без атрибута integrity (SRI)`);
      } else if (integrity !== manifest[file]) {
        if (CHECK_ONLY) {
          report(`${doc}: integrity для ${file} устарел — пересоберите (\`npm run build\`)`);
        } else {
          // SRI-хэш меняется при каждой сборке CSS, поэтому проставлять его
          // вручную нельзя: инструмент обновляет атрибут сам.
          html = html.replace(tag, tag.replace(/integrity="[^"]+"/, `integrity="${manifest[file]}"`));
          changed = true;
        }
      }
      if (!/\bcrossorigin="anonymous"/.test(tag)) {
        report(`${doc}: ссылка на ${file} без crossorigin="anonymous"`);
      }
    }

    if (changed) writeFileSync(absolute, html, 'utf8');
  }
}

/* ---------- 4. Запись манифеста ---------- */

const serialized = `${JSON.stringify(manifest, null, 2)}\n`;

if (CHECK_ONLY) {
  const current = existsSync(MANIFEST) ? readFileSync(MANIFEST, 'utf8') : '';
  if (current !== serialized) {
    report('assets/manifest.json устарел — пересоберите статику (`npm run build`)');
  }
} else {
  writeFileSync(MANIFEST, serialized, 'utf8');
}

if (problems.length) {
  console.error(`\nbuild-assets: ${problems.length} проблем(ы) — статика не готова к production.\n`);
  process.exit(1);
}

console.log(
  `build-assets: OK — шрифтов ${fontFiles.length}, ресурсов с SRI ${Object.keys(manifest).length}` +
    (CHECK_ONLY ? ' (проверка)' : '')
);

