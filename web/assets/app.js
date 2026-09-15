/* 认知副驾 · 共享前端逻辑：会话 / 请求 / 提示 / 顶栏
 *
 * 三个页面（login / knowledge / mine）共用这一份；页面自身只写各自的业务逻辑。
 * 注意：本文件里所有语句都显式带分号——以 `[` 或 `(` 开头的续行会被解析成
 * 对上一行结果的索引/调用（ASI 不会补分号），这类错误语法检查查不出来、只在运行时崩。
 */
window.CC = (function () {
  'use strict';

  var TOKEN_KEY = 'cc_access_token';
  var REFRESH_KEY = 'cc_refresh_token';
  var NAME_KEY = 'cc_username';

  /* 这些端点的 401 表示「你给的凭证本身不对」（密码错 / 刷新令牌无效），
     必须把原因原样报给用户；若也当成「登录过期」跳转，就会把「密码错误」
     误报成「登录过期」，用户永远看不到真正的原因。

     ⚠️ 这里必须是**白名单**而不是「/api/v1/auth/ 前缀」：
     `/api/v1/auth/me` 是**会话探针**，它的 401 恰恰就是「登录已过期」，
     必须跳转登录页。早期版本排除了整个 auth 前缀，于是令牌一过期，
     页面既不跳登录也不报错，`requireAuth` 静默返回 null、`boot()` 直接
     结束——界面渲染成一个空壳：列表空、用户名空、退出按钮没绑事件
     （用户看到的就是「记录全没了 + 账号不显示 + 退不掉」）。 */
  var NO_AUTH_REDIRECT_PATHS = [
    '/api/v1/auth/login',
    '/api/v1/auth/register',
    '/api/v1/auth/password',
    '/api/v1/auth/account',
    '/api/v1/auth/refresh'
  ];

  var _topbarBound = false;
  var _refreshInFlight = null;

  function $(selector) {
    return document.querySelector(selector);
  }

  function esc(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function token() {
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function refreshToken() {
    return localStorage.getItem(REFRESH_KEY) || '';
  }

  function username() {
    return localStorage.getItem(NAME_KEY) || '';
  }

  function setSession(body) {
    localStorage.setItem(TOKEN_KEY, body.access_token);
    localStorage.setItem(REFRESH_KEY, body.refresh_token || '');
    localStorage.setItem(NAME_KEY, body.username || '');
  }

  function clearSession() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(REFRESH_KEY);
    localStorage.removeItem(NAME_KEY);
  }

  function toast(message) {
    var el = $('#toast');
    if (!el) {
      return;
    }
    el.textContent = message;
    el.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () {
      el.classList.remove('show');
    }, 2600);
  }

  function isLoginPage() {
    return location.pathname.indexOf('login.html') !== -1;
  }

  function goLogin(message) {
    clearSession();
    var params = [];
    if (message) {
      params.push('msg=' + encodeURIComponent(message));
    }
    // 带上回跳目标：重新登录后回到刚才那一页，而不是一律丢到知识库。
    // 在登录页自身则不带（否则会自我嵌套）。
    if (!isLoginPage()) {
      params.push('next=' + encodeURIComponent(location.pathname + location.search));
    }
    location.replace('/login.html' + (params.length ? '?' + params.join('&') : ''));
  }

  /* 把错误响应转成能直接显示给人看的短句。
     后端已把 422 校验错误翻译成中文，这里再兜一层：万一拿到的是对象/数组
     （pydantic 原始格式），也只取可读的 msg，绝不整串 JSON 怼到界面上。 */
  function errorText(data, res) {
    var detail = data && data.detail;
    if (typeof detail === 'string' && detail) {
      return detail;
    }
    if (Array.isArray(detail)) {
      var msgs = [];
      for (var i = 0; i < detail.length; i++) {
        var item = detail[i] || {};
        if (item.msg || item.message) {
          msgs.push(item.msg || item.message);
        }
      }
      if (msgs.length) {
        return msgs.join('；');
      }
    }
    if (detail && typeof detail === 'object') {
      if (detail.msg) {
        return detail.msg;
      }
      if (detail.message) {
        return detail.message;
      }
    }
    return 'HTTP ' + res.status;
  }

  function rawFetch(method, path, body) {
    var headers = { 'Content-Type': 'application/json' };
    var t = token();
    if (t) {
      headers.Authorization = 'Bearer ' + t;
    }
    var options = { method: method, headers: headers };
    if (body !== undefined && body !== null) {
      options.body = JSON.stringify(body);
    }
    return fetch(path, options);
  }

  /* 用 refresh token 静默续期：成功则换掉 access token 并返回 true。
     并发的多个请求共享同一次续期，避免同时打出多个 /refresh。 */
  function tryRefresh() {
    if (_refreshInFlight) {
      return _refreshInFlight;
    }
    var rt = refreshToken();
    if (!rt) {
      return Promise.resolve(false);
    }
    _refreshInFlight = fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: rt })
    }).then(function (res) {
      if (!res.ok) {
        return false;
      }
      return res.json().then(function (data) {
        if (!data || !data.access_token) {
          return false;
        }
        localStorage.setItem(TOKEN_KEY, data.access_token);
        return true;
      });
    }).catch(function () {
      return false;
    }).then(function (ok) {
      _refreshInFlight = null;
      return ok;
    });
    return _refreshInFlight;
  }

  /* options.noAuthRedirect：401 时不跳登录页，由调用方自己处置
     （登录页的探针就是这么用的——它已经站在登录页上了）。 */
  async function api(method, path, body, options) {
    options = options || {};
    var basePath = path.split('?')[0];
    var res = await rawFetch(method, path, body);

    if (res.status === 401 && NO_AUTH_REDIRECT_PATHS.indexOf(basePath) === -1) {
      // access token 过期：先静默续期，再原样重试一次。
      // 读一本书动辄一两小时，access token 只有 2 小时——不续期的话
      // 「读完书退出来」必然撞上过期，用户就会看到一整页空白。
      if (await tryRefresh()) {
        res = await rawFetch(method, path, body);
      }
      if (res.status === 401 && !options.noAuthRedirect) {
        var expired = new Error('登录已过期，请重新登录');
        expired.authRedirect = true;
        goLogin('登录已过期，请重新登录');
        throw expired;
      }
    }

    var data = {};
    try {
      data = await res.json();
    } catch (err) {
      data = {};
    }
    if (!res.ok) {
      throw new Error(errorText(data, res));
    }
    return data;
  }

  function withLoading(button, task, loadingText) {
    var original = button.textContent;
    button.disabled = true;
    button.textContent = loadingText || '处理中…';
    return Promise.resolve().then(task).finally(function () {
      button.disabled = false;
      button.textContent = original;
    });
  }

  function renderTopbar() {
    var nameEl = $('#who-name');
    if (nameEl) {
      nameEl.textContent = username();
    }
    if (_topbarBound) {
      return; // 幂等：重复调用不会把退出按钮绑多次
    }
    var logout = $('#btn-logout');
    if (logout) {
      logout.addEventListener('click', function () {
        clearSession();
        location.replace('/login.html');
      });
      _topbarBound = true;
    }
  }

  function highlightNav() {
    var page = document.body.getAttribute('data-page');
    var links = document.querySelectorAll('.nav a');
    for (var i = 0; i < links.length; i++) {
      if (links[i].getAttribute('data-nav') === page) {
        links[i].classList.add('active');
      }
    }
  }

  /* 业务页统一入口：无令牌直接跳登录；令牌失效由 api() 负责续期或跳转。
     顶栏在**校验之前**就渲染好——这样即使校验失败，用户名仍可见、
     退出按钮仍可用，不会留下一个「退不掉」的死页面。 */
  async function requireAuth() {
    renderTopbar();
    highlightNav();
    if (!token()) {
      goLogin();
      return null;
    }
    try {
      var me = await api('GET', '/api/v1/auth/me');
      localStorage.setItem(NAME_KEY, me.username);
      renderTopbar();
      return me;
    } catch (err) {
      // 401 已由 api() 跳转登录页；能走到这里说明是网络/服务端异常，
      // 必须让用户看得见，不能静默留一个空壳。
      if (!(err && err.authRedirect)) {
        toast('会话校验失败：' + ((err && err.message) || err) + '（可刷新页面重试）');
      }
      return null;
    }
  }

  return {
    $: $,
    esc: esc,
    token: token,
    refreshToken: refreshToken,
    username: username,
    setSession: setSession,
    clearSession: clearSession,
    toast: toast,
    api: api,
    withLoading: withLoading,
    requireAuth: requireAuth,
    goLogin: goLogin,
    tryRefresh: tryRefresh
  };
})();
