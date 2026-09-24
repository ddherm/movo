const SHELL_CACHE = 'movo-shell-v4'

async function cacheShell() {
  const response = await fetch('/', { cache: 'reload', credentials: 'same-origin' })
  if (!response.ok) return
  const cache = await caches.open(SHELL_CACHE)
  const html = await response.clone().text()
  await cache.put('/', response)
  const assets = [...html.matchAll(/(?:src|href)="(\/assets\/[^\"]+)"/g)].map((match) => match[1])
  await Promise.allSettled(assets.map(async (path) => {
    const asset = await fetch(path, { cache: 'reload', credentials: 'same-origin' })
    if (asset.ok) await cache.put(path, asset)
  }))
}

self.addEventListener('install', (event) => {
  event.waitUntil(cacheShell().catch(() => {}).then(() => self.skipWaiting()))
})

self.addEventListener('activate', (event) => {
  event.waitUntil(Promise.all([
    self.clients.claim(),
    caches.keys().then((keys) => Promise.all(keys.filter((key) => (key.startsWith('voice-companion-shell-') || key.startsWith('movo-shell-')) && key !== SHELL_CACHE).map((key) => caches.delete(key)))),
  ]))
})

self.addEventListener('fetch', (event) => {
  const request = event.request
  if (request.method !== 'GET') return
  const url = new URL(request.url)
  if (url.origin !== self.location.origin) return
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/preview/')) return

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).then(async (response) => {
      if (response.ok && response.headers.get('content-type')?.includes('text/html')) {
        const cache = await caches.open(SHELL_CACHE)
        await cache.put('/', response.clone())
      }
      return response
    }).catch(async () => (await caches.match('/')) || new Response('运行服务的电脑离线，请稍后重试。', { status: 503 })))
    return
  }

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request).then(async (response) => {
      if (response.ok) {
        const cache = await caches.open(SHELL_CACHE)
        await cache.put(request, response.clone())
      }
      return response
    })))
  }
})
