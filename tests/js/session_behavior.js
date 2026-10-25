/* 前端会话行为验证（Node，无第三方依赖）
 *
 * 目的：app.js 的会话逻辑（静默续期 / 令牌过期跳登录 / 退出）无法用
 * 「页面里有没有某个字符串」这类断言覆盖——它只在运行时才暴露。
 * 这里用 vm 跑真实的 app.js，stub 掉 fetch / localStorage / location，
 * 把真实发生过的 bug 场景固化成断言。
 *
 * 用法：node tests/js/session_behavior.js [app.js 路径]
 *   - 默认跑 web/assets/app.js；传入旧版本路径即可复现历史 bug
 *   - 退出码 0 = 全部通过，1 = 有失败项（供 pytest 断言）
 *
 * 对应线上 bug（2026-09-15）：
 *   读完书退出来 → 记录全没了 + 账号不显示 + 退出按钮点了没反应
 * 根因：会话探针 /api/v1/auth/me 的 401 被「auth 路径不跳转」规则误伤，
 *      既不跳登录页也不报错；requireAuth 静默返回 null，boot() 直接结束，
 *      页面渲染成一个空壳（列表空、用户名空、退出按钮无监听）。
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const APP_JS = process.argv[2] || path.join(__dirname, '..', '..', 'web', 'assets', 'app.js');
const code = fs.readFileSync(APP_JS, 'utf8');
console.log('被测文件: ' + APP_JS + '\n');

const ME = '/api/v1/auth/me';
const REFRESH = '/api/v1/auth/refresh';

let failed = 0;
function check(name, cond, extra) {
  if (!cond) { failed++; }
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}${extra ? '  -> ' + extra : ''}`);
}
async function scenario(title, fn) {
  console.log(title);
  try {
    await fn();
  } catch (e) {
    failed++;
    console.log('  [FAIL] 场景抛出未预期异常: ' + ((e && e.message) || e));
  }
}

/* 搭一个最小浏览器环境：只实现 app.js 真正用到的那几个 API */
function makeEnv(opts) {
  const store = Object.assign({}, opts.storage || {});
  const calls = { fetch: [], replace: [], logoutBinds: 0 };
  const elements = {
    '#toast': { textContent: '', classList: { add() {}, remove() {} } },
    '#who-name': { textContent: '' },
    '#btn-logout': {
      addEventListener(ev, fn) {
        calls.logoutBinds++;
        if (!elements.__logout) { elements.__logout = fn; }
      }
    }
  };
  const document = {
    querySelector: (sel) => elements[sel] || null,
    /* 导航项只有测「底部横滑条定位」时才需要；其余场景给空数组（app.js 循环零次） */
    querySelectorAll: (sel) => (sel === '.nav a' && opts.navLinks) ? opts.navLinks : [],
    body: { getAttribute: () => opts.page || 'knowledge' }
  };
  const location = {
    pathname: opts.pathname || '/knowledge.html',
    search: opts.search || '',
    replace: (url) => calls.replace.push(url)
  };
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; }
  };
  const fetch = (url, options) => {
    const headers = (options && options.headers) || {};
    const bearer = String(headers.Authorization || '').replace('Bearer ', '');
    calls.fetch.push({ url, bearer });
    const reply = opts.route(url, options, bearer);
    return Promise.resolve({
      status: reply.status,
      ok: reply.status >= 200 && reply.status < 300,
      json: () => Promise.resolve(reply.body || {})
    });
  };
  /* window / navigator：app.js 注册 PWA 的 SW 与安装提示要用
     （真实浏览器必有；这里按「只 stub 用到的 API」哲学补上即可） */
  const sandbox = {
    window: { addEventListener() {} },
    navigator: {},
    document, location, localStorage, fetch, console
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return {
    CC: sandbox.window.CC,
    store, calls, elements,
    clickLogout: () => { if (elements.__logout) { elements.__logout(); } return !!elements.__logout; }
  };
}

(async function main() {
  await scenario('A. access 过期 + refresh 有效 → 静默续期后请求成功', async () => {
    const env = makeEnv({
      storage: { cc_access_token: 'old', cc_refresh_token: 'r1', cc_username: '111' },
      route: (url, o, bearer) => {
        if (url === REFRESH) { return { status: 200, body: { access_token: 'new' } }; }
        if (url.indexOf('/api/v1/knowledge/items') === 0) {
          return bearer === 'old'
            ? { status: 401, body: { detail: 'token 已过期' } }
            : { status: 200, body: { items: [], total: 0 } };
        }
        return { status: 404, body: {} };
      }
    });
    const data = await env.CC.api('GET', '/api/v1/knowledge/items?limit=3');
    check('请求最终成功', data && data.total === 0);
    check('access token 已换新', env.store.cc_access_token === 'new', env.store.cc_access_token);
    check('未跳转（用户无感）', env.calls.replace.length === 0, JSON.stringify(env.calls.replace));
  });

  await scenario('B. requireAuth：access 过期 + refresh 有效 → 页面正常（不是空壳）', async () => {
    const env = makeEnv({
      storage: { cc_access_token: 'old', cc_refresh_token: 'r1', cc_username: '' },
      route: (url, o, bearer) => {
        if (url === REFRESH) { return { status: 200, body: { access_token: 'new' } }; }
        if (url === ME) {
          return bearer === 'new'
            ? { status: 200, body: { user_id: 'u', username: '111' } }
            : { status: 401, body: { detail: 'token 已过期' } };
        }
        return { status: 404, body: {} };
      }
    });
    const me = await env.CC.requireAuth();
    check('会话校验通过', !!(me && me.username === '111'));
    check('顶栏显示用户名（旧版这里是空白）', env.elements['#who-name'].textContent === '111',
      env.elements['#who-name'].textContent || '<空>');
    check('退出按钮已绑定（旧版这里完全没绑）', env.calls.logoutBinds === 1, String(env.calls.logoutBinds));
    check('未跳转', env.calls.replace.length === 0);
  });

  await scenario('C. requireAuth：refresh 也失效 → 跳登录页（不是静默空壳）', async () => {
    const env = makeEnv({
      storage: { cc_access_token: 'bad', cc_refresh_token: 'bad', cc_username: '111' },
      route: (url) => {
        if (url === REFRESH) { return { status: 401, body: { detail: 'refresh token 已过期' } }; }
        return { status: 401, body: { detail: 'token 已过期' } };
      }
    });
    await env.CC.requireAuth();
    check('跳转到登录页', env.calls.replace.length === 1 && env.calls.replace[0].indexOf('/login.html') === 0,
      JSON.stringify(env.calls.replace));
    check('带上原因提示', env.calls.replace[0].indexOf('msg=') > 0, env.calls.replace[0]);
    check('带回来路（登录后回到这一页）', env.calls.replace[0].indexOf('next=') > 0, env.calls.replace[0]);
    check('本地会话已清空', !env.store.cc_access_token && !env.store.cc_refresh_token);
  });

  await scenario('D. 登录失败 401 → 不跳转、原样报错', async () => {
    const env = makeEnv({ storage: {}, route: () => ({ status: 401, body: { detail: '用户名或密码不正确' } }) });
    let err = null;
    try { await env.CC.api('POST', '/api/v1/auth/login', { username: 'a', password: 'b' }); } catch (e) { err = e; }
    check('错误信息原样透出', !!err && err.message === '用户名或密码不正确', err && err.message);
    check('没有跳转登录页', env.calls.replace.length === 0);
    check('没有尝试刷新令牌', env.calls.fetch.length === 1, env.calls.fetch.map((f) => f.url).join(' , '));
  });

  await scenario('E. 无令牌 → 直接跳登录页', async () => {
    const env = makeEnv({ storage: {}, route: () => ({ status: 401, body: {} }) });
    const me = await env.CC.requireAuth();
    check('返回 null', me === null);
    check('跳到登录页并带回跳目标',
      env.calls.replace.length === 1 && env.calls.replace[0].indexOf('/login.html?next=') === 0,
      JSON.stringify(env.calls.replace));
  });

  await scenario('F. 会话失效时退出按钮仍可用（本次 bug 的回归点）', async () => {
    const env = makeEnv({
      storage: { cc_access_token: 'bad', cc_refresh_token: 'bad', cc_username: '111' },
      route: (url) => (url === REFRESH ? { status: 401, body: {} } : { status: 401, body: {} })
    });
    await env.CC.requireAuth();
    check('退出按钮已绑定', env.calls.logoutBinds === 1, String(env.calls.logoutBinds));
    env.calls.replace.length = 0;
    env.store.cc_access_token = 'again';
    const clicked = env.clickLogout();
    check('点击退出成功触发', clicked);
    check('退出后跳登录页', env.calls.replace.length === 1 && env.calls.replace[0] === '/login.html',
      JSON.stringify(env.calls.replace));
    check('退出后清空会话', !env.store.cc_access_token && !env.store.cc_username);
  });

  await scenario('G. renderTopbar 幂等（不会重复绑定退出）', async () => {
    const env = makeEnv({
      storage: { cc_access_token: 't', cc_refresh_token: 'r', cc_username: '111' },
      route: (url) => (url === ME ? { status: 200, body: { username: '111' } } : { status: 404, body: {} })
    });
    await env.CC.requireAuth();
    await env.CC.requireAuth();
    await env.CC.requireAuth();
    check('退出按钮只绑定一次', env.calls.logoutBinds === 1, String(env.calls.logoutBinds));
  });

  await scenario('H. 移动端底部导航：active 项滚进视野（不再是屏幕外的隐形高亮）', async () => {
    // 8 项 × 70px = 560px，容器 375px：在 scrollLeft=0 时第 8 项（通知）完全不可见
    const keys = ['app', 'knowledge', 'books', 'conflicts', 'brief', 'l4', 'l5', 'notify'];
    const nav = { clientWidth: 375, scrollWidth: 560, scrollLeft: 0 };
    const links = keys.map((key, i) => ({
      key,
      offsetLeft: i * 70,
      offsetWidth: 70,
      parentNode: nav,
      classList: { added: false, add() { this.added = true; } },
      getAttribute: (n) => (n === 'data-nav' ? key : null)
    }));
    const env = makeEnv({
      page: 'notify',
      navLinks: links,
      storage: { cc_access_token: 't', cc_refresh_token: 'r', cc_username: '111' },
      route: (url) => (url === ME ? { status: 200, body: { username: '111' } } : { status: 404, body: {} })
    });
    await env.CC.requireAuth();
    check('active 标在「通知」这一项', links[7].classList.added === true);
    check('其余项没被误标', links.slice(0, 7).every((l) => !l.classList.added));
    check('导航条已横向滚动（旧版恒为 0，高亮项在屏幕外）', nav.scrollLeft > 0, String(nav.scrollLeft));
    const visible = nav.scrollLeft <= links[7].offsetLeft &&
      links[7].offsetLeft + links[7].offsetWidth <= nav.scrollLeft + nav.clientWidth;
    check('「通知」项落在可视区内', visible,
      `scrollLeft=${nav.scrollLeft} 项=[${links[7].offsetLeft}, ${links[7].offsetLeft + links[7].offsetWidth}]`);
    check('没有滚过头', nav.scrollLeft <= nav.scrollWidth - nav.clientWidth, String(nav.scrollLeft));
  });

  await scenario('I. 桌面导航排得下时不横向位移（避免桌面抖动）', async () => {
    const nav = { clientWidth: 1100, scrollWidth: 1100, scrollLeft: 0 };
    const links = ['app'].map((key) => ({
      key,
      offsetLeft: 260,
      offsetWidth: 70,
      parentNode: nav,
      classList: { added: false, add() { this.added = true; } },
      getAttribute: (n) => (n === 'data-nav' ? key : null)
    }));
    const env = makeEnv({
      page: 'app',
      navLinks: links,
      storage: { cc_access_token: 't', cc_refresh_token: 'r', cc_username: '111' },
      route: (url) => (url === ME ? { status: 200, body: { username: '111' } } : { status: 404, body: {} })
    });
    await env.CC.requireAuth();
    check('active 照常标记', links[0].classList.added === true);
    check('scrollLeft 保持 0', nav.scrollLeft === 0, String(nav.scrollLeft));
  });

  console.log('');
  console.log(failed === 0 ? '全部通过' : `失败 ${failed} 项`);
  process.exit(failed === 0 ? 0 : 1);
})();
