// Doday service worker: кеш оболочки + очередь неотправленных действий.
//
// Зачем очередь. Телефон в школе или в метро теряет сеть в самый неудобный
// момент — между «запрос ушёл» и «ответ пришёл». Без очереди отметка задачи
// в такой момент просто пропадает: человек нажал, ничего не произошло.
//
// Поэтому запрос, который не смог уйти, мы не выбрасываем, а кладём в
// IndexedDB и повторяем, когда сеть вернётся. Повторять безопасно: каждый
// такой запрос несёт заголовок Idempotency-Key, и сервер выполнит работу
// только один раз (см. app/idempotency/).

const CACHE = 'doday-shell-v3';
const SHELL = ['/app/today', '/manifest.webmanifest'];
const DB_NAME = 'doday-outbox';
const STORE = 'requests';
const SYNC_TAG = 'doday-outbox';

// ── хранилище очереди ──────────────────────────────────────────────────────
// localStorage здесь не годится: он синхронный и недоступен из service
// worker. IndexedDB доступен и переживает перезапуск браузера.

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function tx(db, mode) {
  return db.transaction(STORE, mode).objectStore(STORE);
}

async function enqueue(entry) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const req = tx(db, 'readwrite').add(entry);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function allQueued() {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const req = tx(db, 'readonly').getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function drop(id) {
  const db = await openDb();
  return new Promise((resolve) => {
    const req = tx(db, 'readwrite').delete(id);
    req.onsuccess = () => resolve();
    req.onerror = () => resolve();
  });
}

async function tellPages(message) {
  const clients = await self.clients.matchAll({ includeUncontrolled: true });
  for (const c of clients) c.postMessage(message);
}

// ── отправка очереди ───────────────────────────────────────────────────────

let flushing = false;

async function flush() {
  if (flushing) return;
  flushing = true;
  try {
    const queued = await allQueued();
    let sent = 0;
    for (const item of queued) {
      let response;
      try {
        response = await fetch(item.url, {
          method: item.method,
          headers: item.headers,
          body: item.body || undefined,
          credentials: 'same-origin',
        });
      } catch (e) {
        // Сеть снова пропала. Остальное ждёт следующего раза — порядок
        // важен: «создать задачу» должно уйти раньше «отметить её».
        break;
      }
      if (response.status === 409) {
        // Первый такой же запрос ещё в работе на сервере. Оставляем в
        // очереди и пробуем позже.
        break;
      }
      if (response.status >= 500) break; // сервер лежит — не теряем запрос
      // 2xx, 4xx — ответ получен, повторять нечего. 4xx значит, что запрос
      // не примут никогда (задачу успели удалить с другого устройства).
      await drop(item.id);
      sent += 1;
    }
    const left = (await allQueued()).length;
    await tellPages({ type: 'outbox', pending: left, sent });
  } finally {
    flushing = false;
  }
}

// ── жизненный цикл ─────────────────────────────────────────────────────────

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL).catch(() => null))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('sync', (e) => {
  if (e.tag === SYNC_TAG) e.waitUntil(flush());
});

self.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'flush') e.waitUntil ? e.waitUntil(flush()) : flush();
});

// ── перехват запросов ──────────────────────────────────────────────────────

const QUEUEABLE = ['POST', 'PATCH', 'DELETE'];

function queueable(request, url) {
  if (!QUEUEABLE.includes(request.method)) return false;
  if (url.origin !== self.location.origin) return false;
  // Вход, регистрация и оплата в очередь не попадают: их повтор через час
  // бессмысленен, а иногда и вреден.
  return !/^\/(auth|api\/billing|miniapp\/auth)/.test(url.pathname);
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);

  if (req.method === 'GET') {
    // Чужие домены не наше дело: htmx, шрифты и аналитику пусть кеширует сам
    // браузер. Если пропускать их через воркер, то любой сбой у CDN
    // превращается в наш собственный «Offline» — и страница остаётся без
    // скриптов, хотя сеть в порядке.
    if (url.origin !== self.location.origin) return;
    if (url.pathname.startsWith('/api/')) return;
    e.respondWith(
      fetch(req)
        .then((r) => {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => null);
          return r;
        })
        .catch(() => caches.match(req).then((c) => c || new Response('Offline', { status: 503 })))
    );
    return;
  }

  if (!queueable(req, url)) return;

  // Тело запроса читается один раз, поэтому копию снимаем заранее — до
  // того, как его заберёт fetch.
  const copy = req.clone();

  async function queueIt() {
    const headers = {};
    copy.headers.forEach((v, k) => {
      if (['content-type', 'idempotency-key', 'hx-request', 'hx-current-url'].includes(k))
        headers[k] = v;
    });
    const body = await copy.text();
    await enqueue({ url: req.url, method: req.method, headers, body, at: Date.now() });
    const left = (await allQueued()).length;
    await tellPages({ type: 'outbox', pending: left, sent: 0 });
    if ('sync' in self.registration) {
      try {
        await self.registration.sync.register(SYNC_TAG);
      } catch (err) {
        /* Safari не умеет Background Sync — отправим по событию online */
      }
    }
    // HX-Reswap: none — чтобы htmx не подставил этот ответ вместо строки
    // задачи и не стёр её с экрана.
    return new Response('', {
      status: 202,
      headers: { 'X-Doday-Queued': '1', 'HX-Reswap': 'none' },
    });
  }

  e.respondWith(
    fetch(req)
      .then((r) => {
        // Обрыв сети — не единственная причина, по которой запрос не доехал.
        // Наш собственный деплой выглядит для клиента ровно так же: nginx
        // секунд сорок отвечает 502, пока перезапускается приложение. Такой
        // ответ значит «до приложения не дошло», и его тоже надо повторить.
        if ([502, 503, 504].includes(r.status)) return queueIt();
        return r;
      })
      .catch(queueIt)
  );
});
