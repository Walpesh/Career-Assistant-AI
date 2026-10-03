/**
 * Tailwind CLI — единственный источник конфигурации темы.
 *
 * Раньше эта же тема объявлялась инлайновым `tailwind.config = {...}` в index.html
 * вместе с Play CDN (`cdn.tailwindcss.com`). В production CDN запрещён
 * (docs/01 §10 — доставка статики и CSP), поэтому тема живёт здесь, а
 * `src/input.css` компилируется в статический `css/styles.min.css`.
 *
 * Палитра: slate (поверхности) + indigo (акцент). Правила @apply, ранее
 * находившиеся в `<style type="text/tailwindcss">`, перенесены в
 * `src/input.css` (директива @layer components).
 *
 * @type {import('tailwindcss').Config}
 */
export default {
  content: [
    './index.html',
    './404.html',
    './partials/*.html',
    // Классы собираются в шаблонных строках views/* и components/* —
    // без сканирования JS-файлов они выпали бы из сборки.
    './js/**/*.js'
  ],
  // Классы, которые в рантайме только переключаются/собираются из частей.
  // Даже если статический анализатор их увидит, явный список делает
  // намерение читаемым и защищает сборку от ложных срабатываний.
  safelist: [
    'hidden',
    'sm:inline',
    'is-striped',
    'bg-emerald-400',
    'bg-amber-400',
    'bg-rose-500',
    'text-emerald-300',
    'text-amber-300',
    'text-rose-300'
  ],
  theme: {
    extend: {
      fontFamily: {
        // Inter self-hosted (152-ФЗ / GDPR — см. src/fonts.css).
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'Arial', 'sans-serif']
      },
      boxShadow: {
        glow: '0 0 0 1px rgba(99,102,241,.35), 0 12px 40px -14px rgba(99,102,241,.55)'
      },
      keyframes: {
        'fade-in': { '0%': { opacity: '0' }, '100%': { opacity: '1' } },
        'slide-up': { '0%': { opacity: '0', transform: 'translateY(10px)' }, '100%': { opacity: '1', transform: 'translateY(0)' } }
      },
      animation: {
        'fade-in': 'fade-in .25s ease-out both',
        'slide-up': 'slide-up .25s ease-out both'
      }
    }
  },
  plugins: []
};
