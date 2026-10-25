/* 阅读页渲染验证（Node，无第三方依赖）
 *
 * 目的：页码标记必须**不产生任何字符**。
 * reader.html 的划词偏移是拿 `Range.toString().length` 算的（getSelectionInfo），
 * 页码一旦以真字符插入 DOM，所有高亮与摘录的 char_start 就会整体错位——
 * 表现是「存下的摘录和原文对不上」，而且要等用户读了几十页才发现。
 * 这一条 pytest 看不到（后端只返回 JSON），浏览器里也不会报错，只能在这里钉死。
 *
 * 用法：node tests/js/reader_render.js [reader.html 路径]
 *   退出码 0 = 全部通过，1 = 有失败项
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HTML_PATH = process.argv[2] || path.join(__dirname, '..', '..', 'web', 'reader.html');
const html = fs.readFileSync(HTML_PATH, 'utf8');
const match = html.match(/<script>([\s\S]*?)<\/script>/);
if (!match) {
  console.error('找不到内联脚本：' + HTML_PATH);
  process.exit(1);
}
console.log('被测文件: ' + HTML_PATH + '\n');

let failed = 0;
function check(name, cond, extra) {
  if (!cond) { failed++; }
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}${extra ? '  -> ' + extra : ''}`);
}

/* 万能假元素：reader.html 在装配阶段只需要这些方法与属性 */
function fakeEl() {
  const el = {
    style: { setProperty() {} },
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    scrollHeight: 1000,
    clientHeight: 400,
    scrollTop: 0,
    innerHTML: '',
    textContent: '',
    hidden: true,
    src: '',
    disabled: false,
    value: '',
    addEventListener() {},
    removeEventListener() {},
    querySelector() { return fakeEl(); },
    querySelectorAll() { return []; },
    scrollTo() {},
    scrollIntoView() {},
    contains() { return false; },
    closest() { return null; },
    getAttribute() { return null; }
  };
  return el;
}

const elements = new Map();
function $(selector) {
  if (!elements.has(selector)) { elements.set(selector, fakeEl()); }
  return elements.get(selector);
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

/* requireAuth 返回永不 resolve 的 Promise：让 boot() 停在第一行，
   装配阶段跑完就够了，这里只测纯函数 */
const pending = () => new Promise(() => {});

const sandbox = {
  console: console,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  requestAnimationFrame: (fn) => fn(),
  URLSearchParams: URLSearchParams,
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  location: { search: '?book=test-book', href: '', replace: () => {} },
  CC: {
    $: $,
    esc: esc,
    token: () => 'tok',
    toast: () => {},
    api: pending,
    requireAuth: pending
  },
  document: {
    body: fakeEl(),
    addEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    createRange: () => ({ selectNodeContents() {}, setEnd() {}, toString: () => '' })
  }
};
sandbox.window = {
  addEventListener: () => {},
  getSelection: () => null,
  innerWidth: 1200,
  __readerInternals: null
};
sandbox.window.document = sandbox.document;
sandbox.window.localStorage = sandbox.localStorage;

vm.createContext(sandbox);
vm.runInContext(match[1], sandbox);

const internals = sandbox.window.__readerInternals;
check('暴露了测试钩子 __readerInternals', !!internals);

if (!internals) {
  console.log('\n失败 1 项');
  process.exit(1);
}

const { renderContent, pageMarkHtml } = internals;

/* 剥掉 HTML 标签后剩下的「可见文本」—— 划词偏移数的是这个长度 */
function textOf(html) {
  return String(html == null ? '' : html).replace(/<[^>]*>/g, '');
}

/* ---------- 1. 页码标记本身不含字符 ---------- */
const mark = pageMarkHtml({ page: 7, label: '42' });
check('页码标记不含数字字符', !/[0-9]/.test(textOf(mark)), mark);
check('页码标记带 data-show', /data-show="P\.42"/.test(mark), mark);
check('没有印刷页码时退回物理页', /data-show="第 7 页"/.test(pageMarkHtml({ page: 7, label: '' })));

/* ---------- 2. 插了页码，正文可见文本一个字符都不变 ---------- */
const body = '第一段文字。第二段文字更长一些，用来验证偏移。';
const chapter = { index: 1, char_start: 0 };
const plain = renderContent(body, chapter, []);
const withMarks = renderContent(body, chapter, [
  { page: 1, label: '10', offset: 0 },
  { page: 2, label: '11', offset: 6 },
  { page: 3, label: '12', offset: body.length }
]);

check('不插页码时文本等于原文', textOf(plain) === body);
check('插页码后文本仍等于原文', textOf(withMarks) === body, textOf(withMarks));
check('页码数量正确（含章末那个）', (withMarks.match(/class="page-mark"/g) || []).length === 3);

/* ---------- 3. 页码按偏移插在正确位置 ---------- */
const segments = withMarks.split('class="page-mark"');
check('offset=0 的页码在最前面', segments[0].indexOf('第一段') === -1);
check('offset=6 的页码落在第二段前',
  segments[1].indexOf('第二段') === 0 || segments[1].indexOf('第二段') === -1);

/* ---------- 4. 越界 / 非法偏移不能把渲染搞坏 ---------- */
const clamped = renderContent(body, chapter, [
  { page: 1, label: '', offset: -5 },
  { page: 2, label: '', offset: 99999 },
  { page: 3, label: '', offset: 'NaN' }
]);
check('越界偏移被裁到正文内', textOf(clamped) === body);
check('非法偏移整条忽略', (clamped.match(/class="page-mark"/g) || []).length === 2);

/* ---------- 5. 页码与插图同位置时先页码后图 ---------- */
const withImg = renderContent('前文[[IMG:img_0001.png]]后文', chapter, [
  { page: 1, label: '5', offset: 2 }
]);
check('同位置时页码排在插图之前',
  withImg.indexOf('page-mark') < withImg.indexOf('reader-figure'));
check('插图占位文本仍保留（偏移不变）',
  withImg.indexOf('[[IMG:img_0001.png]]') > -1);

/* ---------- 6. 页码的显示值必须转义 ---------- */
const evil = pageMarkHtml({ page: 1, label: '"><script>alert(1)</script>' });
check('页码标签被转义', evil.indexOf('<script>') === -1, evil);

console.log('');
if (failed) {
  console.log(`失败 ${failed} 项`);
  process.exit(1);
}
console.log('全部通过');
