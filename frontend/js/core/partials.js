/* ============================================================
   Загрузка HTML-партиалов вкладок (кэшируется в памяти).
   ============================================================ */

const cache = new Map();

export async function loadPartial(name) {
  if (cache.has(name)) return cache.get(name);

  const response = await fetch(`partials/${name}.html`, { cache: 'no-cache' });
  if (!response.ok) {
    throw new Error(`Не удалось загрузить partials/${name}.html (HTTP ${response.status})`);
  }
  const html = await response.text();
  cache.set(name, html);
  return html;
}
