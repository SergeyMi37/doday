// Браузерная проверка очереди неотправленного.
//
// Зачем отдельно от pytest: очередь целиком живёт в браузере — service
// worker, IndexedDB, выключенная сеть. Со стороны сервера этого не видно.
//
// Как запускать:
//   1. поднять приложение на 127.0.0.1:8011 с тестовой базой;
//   2. завести в ней пользователя и пару незакрытых задач;
//   3. node scripts/offline_check.mjs <папка-с-htmx.js-и-alpine.js> [email] [пароль]
//
// Важная деталь: заглушка Tailwind отдаёт настоящие display-утилиты. С пустой
// заглушкой тест врал — атрибут hidden в ней работал, а в проде его перебивал
// класс flex, и значок «ждёт сети» висел на экране всегда.
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';

const root = path.join(process.env.LOCALAPPDATA, 'ms-playwright');
const dir = fs.readdirSync(root).find((d) => d.startsWith('chromium-'));
const exe = path.join(root, dir, 'chrome-win64', 'chrome.exe');

const BASE = 'http://127.0.0.1:8011';
const VENDOR = process.argv[2];
const EMAIL = process.argv[3] || 'offline@getdoday.ru';
const PASSWORD = process.argv[4] || 'offline-password-123';

// Минимальный «Tailwind»: только то, что влияет на видимость значка.
const TAILWIND_STUB = `
.fixed{position:fixed}.flex{display:flex}.grid{display:grid}.block{display:block}
.inline-flex{display:inline-flex}.hidden{display:none}
`;

const browser = await chromium.launch({
  executablePath: exe,
  headless: true,
  args: ['--use-gl=swiftshader', '--no-sandbox'],
});
const ctx = await browser.newContext();

await ctx.route('**/htmx.org*', (r) =>
  r.fulfill({
    body: fs.readFileSync(path.join(VENDOR, 'htmx.js')),
    contentType: 'application/javascript',
  })
);
await ctx.route('**/alpinejs*', (r) =>
  r.fulfill({
    body: fs.readFileSync(path.join(VENDOR, 'alpine.js')),
    contentType: 'application/javascript',
  })
);
// Tailwind с CDN — это скрипт, который дописывает стили на страницу.
await ctx.route('**/cdn.tailwindcss.com/**', (r) =>
  r.fulfill({
    contentType: 'application/javascript',
    body: `const s=document.createElement('style');s.textContent=${JSON.stringify(TAILWIND_STUB)};document.head.appendChild(s);`,
  })
);
await ctx.route('**/fonts.googleapis.com/**', (r) => r.fulfill({ body: '', contentType: 'text/css' }));
await ctx.route('**/sortablejs*', (r) => r.fulfill({ body: '', contentType: 'application/javascript' }));

const page = await ctx.newPage();
page.on('response', (r) => {
  const u = new URL(r.url());
  if (u.pathname.startsWith('/htmx/')) console.log('  [ответ]', r.status(), u.pathname.slice(0, 46));
});

const fail = [];
function check(name, ok, detail = '') {
  console.log(`${ok ? '  ok  ' : ' FAIL '} ${name}${detail ? ' — ' + detail : ''}`);
  if (!ok) fail.push(name);
}

async function queued() {
  return page.evaluate(async () => {
    const db = await new Promise((res) => {
      const r = indexedDB.open('doday-outbox');
      r.onsuccess = () => res(r.result);
      r.onerror = () => res(null);
    });
    if (!db || !db.objectStoreNames.contains('requests')) return [];
    return await new Promise((res) => {
      const q = db.transaction('requests', 'readonly').objectStore('requests').getAll();
      q.onsuccess = () => res(q.result.map((x) => x.method + ' ' + new URL(x.url).pathname.slice(0, 36)));
      q.onerror = () => res(['<ошибка>']);
    });
  });
}

await page.goto(BASE + '/auth/login', { waitUntil: 'domcontentloaded' });
await page.fill('input[name="email"]', EMAIL);
await page.fill('input[name="password"]', PASSWORD);
await Promise.all([
  page.waitForNavigation({ waitUntil: 'domcontentloaded' }),
  page.click('button[type="submit"]'),
]);

await page.goto(BASE + '/app/today', { waitUntil: 'load' });
await page
  .waitForFunction(() => navigator.serviceWorker && navigator.serviceWorker.controller, null, { timeout: 15000 })
  .catch(() => {});
await page.reload({ waitUntil: 'load' });
await page.waitForTimeout(900);

check(
  'воркер управляет страницей',
  await page.evaluate(() => !!(navigator.serviceWorker && navigator.serviceWorker.controller))
);

// Главная проверка, которой раньше не было по-настоящему.
const badgeDisplay = await page
  .locator('#outbox-badge')
  .evaluate((el) => getComputedStyle(el).display)
  .catch(() => '<нет элемента>');
check('значка не видно, пока очередь пуста', badgeDisplay === 'none', 'display: ' + badgeDisplay);

// ── сети нет ──────────────────────────────────────────────────────────────
await ctx.setOffline(true);
console.log('— сеть выключена —');
await page.locator('button[hx-post*="/state"]').first().click();
await page.waitForTimeout(1200);

check('значок появился', await page.locator('#outbox-badge').isVisible(),
  (await page.locator('#outbox-text').textContent().catch(() => '')).trim());
const q1 = await queued();
check('запрос лёг в очередь', q1.length === 1, JSON.stringify(q1));

await ctx.setOffline(false);
await page.evaluate(() => window.dispatchEvent(new Event('online')));
await page.waitForTimeout(2500);
check('очередь ушла', (await queued()).length === 0);

// ── прокси отдаёт 502 (идёт деплой) ───────────────────────────────────────
await page.goto(BASE + '/app/today', { waitUntil: 'load' });
await page.waitForTimeout(700);
await ctx.route('**/htmx/tasks/**', (r) => r.fulfill({ status: 502, body: 'bad gateway' }));
console.log('— прокси отдаёт 502 —');
await page.locator('button[hx-post*="/state"]').first().click();
await page.waitForTimeout(1200);
check('502 тоже попал в очередь', (await queued()).length === 1);

await ctx.unroute('**/htmx/tasks/**');
await page.evaluate(() => window.dispatchEvent(new Event('online')));
await page.waitForTimeout(2500);
check('очередь ушла после «конца деплоя»', (await queued()).length === 0);

await browser.close();
console.log(fail.length ? `\nПРОВАЛЕНО: ${fail.join(', ')}` : '\nвсё зелёное');
process.exit(fail.length ? 1 : 0);
