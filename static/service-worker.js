// ====================================================
// Service Worker - نظام فواتير المياه PWA
// ====================================================
const CACHE_VERSION = 'v2';
const CACHE_STATIC  = `water-static-${CACHE_VERSION}`;
const CACHE_PAGES   = `water-pages-${CACHE_VERSION}`;
const ALL_CACHES    = [CACHE_STATIC, CACHE_PAGES];

// الملفات الثابتة التي تُحفظ فور التثبيت
const STATIC_ASSETS = [
  '/static/css/style.css',
  '/static/js/app.js',
  '/static/offline.html',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.rtl.min.css',
  'https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css',
  'https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js',
];

// صفحات التطبيق الرئيسية التي تُحفظ في الزيارة الأولى
const APP_SHELL_PAGES = ['/', '/subscribers', '/invoices', '/payments/manage'];

// ====================================================
// التثبيت: تحميل الأصول الثابتة
// ====================================================
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_STATIC).then(cache => {
      return Promise.allSettled(
        STATIC_ASSETS.map(url =>
          cache.add(url).catch(err => console.warn(`[SW] Failed to cache: ${url}`, err))
        )
      );
    })
  );
});

// ====================================================
// التنشيط: حذف الكاش القديم
// ====================================================
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(
        keys.filter(k => !ALL_CACHES.includes(k)).map(k => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

// ====================================================
// استراتيجية الجلب
// ====================================================
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);

  // تجاهل POST وطلبات غير HTTP
  if (request.method !== 'GET') return;
  if (!['http:', 'https:'].includes(url.protocol)) return;

  // طلبات API الديناميكية → شبكة أولاً فقط
  const dynamicPaths = ['/api/', '/export/', '/reports/pdf'];
  if (dynamicPaths.some(p => url.pathname.startsWith(p))) {
    event.respondWith(fetch(request));
    return;
  }

  // أصول ثابتة (CSS/JS/صور) → كاش أولاً
  if (isStaticAsset(url)) {
    event.respondWith(cacheFirst(request, CACHE_STATIC));
    return;
  }

  // صفحات HTML → شبكة أولاً ثم كاش ثم offline
  if (request.headers.get('accept')?.includes('text/html')) {
    event.respondWith(networkFirstHtml(request));
    return;
  }

  // الباقي → شبكة أولاً ثم كاش
  event.respondWith(networkFirst(request, CACHE_PAGES));
});

// ====================================================
// دوال الاستراتيجيات
// ====================================================
function isStaticAsset(url) {
  return (
    url.pathname.startsWith('/static/') ||
    url.hostname.includes('cdn.jsdelivr.net') ||
    /\.(css|js|png|jpg|svg|ico|woff2?)$/.test(url.pathname)
  );
}

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    return cached || new Response('', { status: 503 });
  }
}

async function networkFirst(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    return cached || new Response('', { status: 503 });
  }
}

async function networkFirstHtml(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_PAGES);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    const offlinePage = await caches.match('/static/offline.html');
    return offlinePage || new Response('<h1>غير متصل بالإنترنت</h1>', {
      headers: { 'Content-Type': 'text/html; charset=utf-8' }
    });
  }
}

// ====================================================
// إشعارات Push (جاهز للتفعيل مستقبلاً)
// ====================================================
self.addEventListener('push', event => {
  const data = event.data?.json() || { title: 'إشعار جديد', body: '' };
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/static/icons/icon-192x192.png',
      badge: '/static/icons/icon-72x72.png',
      dir: 'rtl',
      lang: 'ar',
    })
  );
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  event.waitUntil(clients.openWindow('/'));
});
