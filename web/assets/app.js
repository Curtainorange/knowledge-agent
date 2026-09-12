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

  function goLogin(message) {
    clearSession();
    var url = '/login.html';
    if (message) {
      url += '?msg=' + encodeURIComponent(message);
    }
    location.replace(url);
  }

  async function api(method, path, body) {
    var t = token();
    var headers = { 'Content-Type': 'application/json' };
    if (t) {
      headers.Authorization = 'Bearer ' + t;
    }
    var options = { method: method, headers: headers };
    if (body !== undefined && body !== null) {
      options.body = JSON.stringify(body);
    }

    var res = await fetch(path, options);
    // auth 接口自身不触发跳转，否则会把「密码错误」误报成「登录过期」
    if (res.status === 401 && path.indexOf('/api/v1/auth/') !== 0) {
      goLogin('登录已过期，请重新登录');
      throw new Error('登录已过期');
    }

    var data = {};
    try {
      data = await res.json();
    } catch (err) {
      data = {};
    }
    if (!res.ok) {
      var detail = data.detail;
      var text = typeof detail === 'string' ? detail : (detail ? JSON.stringify(detail) : 'HTTP ' + res.status);
      throw new Error(text);
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
    var logout = $('#btn-logout');
    if (logout) {
      logout.addEventListener('click', function () {
        clearSession();
        location.replace('/login.html');
      });
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

  /* 业务页统一入口：无令牌直接跳登录；令牌失效由 api() 负责跳转 */
  async function requireAuth() {
    if (!token()) {
      goLogin();
      return null;
    }
    try {
      var me = await api('GET', '/api/v1/auth/me');
      localStorage.setItem(NAME_KEY, me.username);
      renderTopbar();
      highlightNav();
      return me;
    } catch (err) {
      return null;
    }
  }

  return {
    $: $,
    esc: esc,
    token: token,
    username: username,
    setSession: setSession,
    clearSession: clearSession,
    toast: toast,
    api: api,
    withLoading: withLoading,
    requireAuth: requireAuth,
    goLogin: goLogin
  };
})();
