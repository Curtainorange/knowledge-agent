/* 认知副驾 Service Worker——一律**网络优先**（network-first）。

  本项目前端随代码高频演进，「缓存旧 JS 跑新后端」的教训已多次
  （见 app/main.py static_cache_middleware 的说明）。因此缓存只做
  「网络不可达时的兜底」，绝不做「离线优先」的完整离线应用。

  铁律：
  - /api/ 一律直连网络、不进缓存（响应含 JWT 会话，绝不落磁盘缓存）；
  - 同源页面/JS/CSS/图标 GET 成功后写缓存，网络失败时兜底命中；
  - 跨域请求不拦截。
*/
'use strict';

var CACHE = 'cc-shell-v1';

/* 本 SW 可能挂在子路径下（如 /cognition/sw.js），此时 scope 就是那个前缀。
   判断「这是不是接口请求」前必须先按 scope 剥掉前缀——否则子路径部署时
   pathname 是 /cognition/api/v1/chat，startsWith('/api/') 落空，
   带 JWT 的接口响应会被写进缓存，违反本文件开头的铁律。 */
var SCOPE_PATH = new URL(self.registration.scope).pathname;

function relativePath(url) {
  var path = url.pathname;
  if (SCOPE_PATH !== '/' && path.indexOf(SCOPE_PATH) === 0) {
    return '/' + path.slice(SCOPE_PATH.length);
  }
  return path;
}

self.addEventListener('install', function () {
  // 新 SW 装上就接管，不等旧页面关闭
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') {
    return;
  }
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  if (relativePath(url).indexOf('/api/') === 0) {
    return; // API 直连网络，不缓存
  }

  event.respondWith(
    fetch(req).then(function (res) {
      if (res.ok) {
        var copy = res.clone();
        caches.open(CACHE).then(function (c) { c.put(req, copy); });
      }
      return res;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || new Response('网络不可达，且没有可兜底的缓存。', {
          status: 503,
          headers: { 'Content-Type': 'text/plain; charset=utf-8' }
        });
      });
    })
  );
});
