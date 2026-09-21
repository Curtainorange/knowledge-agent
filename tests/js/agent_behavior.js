/* 对话工作台卡片渲染验证（Node，无第三方依赖）
 *
 * 目的：`CCA.renderCard` 把后端卡片拼成 HTML，而卡片里的标题、摘要、标签全部来自
 * 用户输入。这类「字符串拼接 + 用户数据」是最容易出注入口的地方，而它又完全
 * 无法用 pytest 覆盖（后端返回的是结构化 JSON，看不到最终 HTML）。
 * 这里用 vm 跑真实的 agent.js，把转义不变量固化成断言。
 *
 * 用法：node tests/js/agent_behavior.js [agent.js 路径]
 *   退出码 0 = 全部通过，1 = 有失败项（供 pytest 断言）
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const AGENT_JS = process.argv[2] || path.join(__dirname, '..', '..', 'web', 'assets', 'agent.js');
const code = fs.readFileSync(AGENT_JS, 'utf8');
console.log('被测文件: ' + AGENT_JS + '\n');

let failed = 0;
function check(name, cond, extra) {
  if (!cond) { failed++; }
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${name}${extra ? '  -> ' + extra : ''}`);
}

/* 与 app.js 同款转义（agent.js 直接复用 CC.esc，所以这里必须给真的实现，
   给个空转义的 stub 会让下面的 XSS 断言变成假通过） */
function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

/* 刻意**不**提供 document：agent.js 的页面装配段应当自行跳过，
   这样纯函数才能在没有 DOM 的环境里被测到 */
const sandbox = { window: {}, CC: { esc: esc }, console: console };
vm.createContext(sandbox);
vm.runInContext(code, sandbox);

const CCA = sandbox.window.CCA;
const XSS = '<img src=x onerror=alert(1)>';

console.log('基础契约');
check('模块挂到 window.CCA', !!CCA);
check('导出 renderCard', typeof (CCA && CCA.renderCard) === 'function');
check('没有 document 也能加载（页面装配段自行跳过）', true);

console.log('\n未知与空输入：少一块，而不是崩一屏');
check('null → 空串', CCA.renderCard(null) === '');
check('无 kind → 空串', CCA.renderCard({ items: [] }) === '');
check('未知 kind → 空串（后端加新卡片时旧前端不该崩）', CCA.renderCard({ kind: 'brand_new' }) === '');
check('非对象 → 空串', CCA.renderCard('oops') === '');

console.log('\n转义：卡片里的用户数据必须全部过 esc');
const located = CCA.renderCard({
  kind: 'l1_located',
  items: [{
    item_id: 'i1',
    title: XSS,
    snippet: XSS,
    read_progress: 0.5,
    embed_status: 'embedded',
    href: '/knowledge.html?item=i1'
  }],
  candidates: [],
  hint: '',
  reason: ''
});
check('命中标题被转义', located.indexOf('&lt;img') !== -1);
check('命中摘要被转义', located.indexOf('<img src=x') === -1, located.slice(0, 80));
check('已读进度按百分比渲染', located.indexOf('已读 50%') !== -1);

const created = CCA.renderCard({
  kind: 'knowledge_created',
  item_id: 'i2',
  title: XSS,
  tags: [XSS],
  embed_status: 'pending',
  href: '/knowledge.html?item=i2'
});
check('入库标题被转义', created.indexOf('<img src=x') === -1);
check('标签被转义', created.indexOf('&lt;img') !== -1);

const guide = CCA.renderCard({
  kind: 'guide',
  capability: 'l2',
  label: XSS,
  href: '/conflicts.html',
  note: XSS
});
check('引导卡标题与说明被转义', guide.indexOf('<img src=x') === -1);
check('引导卡带上原页面链接', guide.indexOf('href="/conflicts.html"') !== -1);

const withEvilHref = CCA.renderCard({
  kind: 'l1_located',
  items: [{ item_id: 'i3', title: 't', snippet: 's', read_progress: 0, embed_status: 'pending', href: '"><script>x</script>' }],
  candidates: []
});
check('链接属性被转义（不能靠引号逃出属性）', withEvilHref.indexOf('<script>') === -1);

console.log('\n候选列表：排除已命中项，但保留「其他可能」');
const cand = CCA.renderCard({
  kind: 'l1_clarify',
  question: '数据库还是哈希？',
  turn: 1,
  max_turns: 3,
  candidates: [
    { item_id: 'i1', title: '已命中', snippet: 'a', score: 0.9, channels: ['vector'] },
    { item_id: 'i2', title: '其他', snippet: 'b', score: 0.4, channels: ['keyword'] }
  ]
});
check('澄清卡渲染追问轮次', cand.indexOf('已追问 1 / 3 轮') !== -1);
check('候选标题出现', cand.indexOf('其他') !== -1);

const filtered = CCA.candidatesHtml([
  { item_id: 'i1', title: '已命中', snippet: 'a', score: 0.9, channels: [] },
  { item_id: 'i2', title: '其他', snippet: 'b', score: 0.4, channels: [] }
], ['i1']);
check('已命中项被排除', filtered.indexOf('已命中') === -1);
check('其他可能被保留', filtered.indexOf('其他') !== -1);
check('无剩余候选时不渲染折叠块', CCA.candidatesHtml([{ item_id: 'i1' }], ['i1']) === '');

console.log('\n数值与徽标');
check('pct 四舍五入', CCA.pct(0.567) === 57);
check('pct 容错非数字', CCA.pct(null) === 0 && CCA.pct('x') === 0);
check('embedBadge 三态', CCA.embedBadge('embedded').indexOf('已向量化') !== -1
  && CCA.embedBadge('embed_failed').indexOf('关键词兜底') !== -1
  && CCA.embedBadge('pending').indexOf('待向量化') !== -1);

console.log('');
if (failed) {
  console.log('失败 ' + failed + ' 项');
  process.exit(1);
}
console.log('全部通过');
