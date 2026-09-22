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

console.log('\nL3 简报卡片：分布、结构信号、追问');
const brief = CCA.renderCard({
  kind: 'l3_brief',
  state: 'ok',
  analyzed_items: 12,
  topics: [
    { topic: '数据库', count: 9, levels: { 入门: 7, 进阶: 2 } },
    { topic: XSS, count: 3, levels: { 未分类: 3 } }
  ],
  patterns: ['大量存在：入门层内容', '完全缺失：实战层内容'],
  questions: [{ question: XSS, why: '只有原理', evidence: '12 篇里 0 篇实战', next_step: '做一次执行计划分析' }],
  conflicts: { this_week: 3, last_week: 1, delta: 2 },
  overview: '结构上有缺口',
  note: '',
  href: '/brief.html'
});
check('主题名与计数渲染', brief.indexOf('数据库') !== -1 && brief.indexOf('9 篇') !== -1);
check('深度层级用徽标渲染', brief.indexOf('入门 7') !== -1);
check('「大量存在」标为 abundant', brief.indexOf('pattern abundant') !== -1);
check('「完全缺失」标为 missing', brief.indexOf('pattern missing') !== -1);
check('追问渲染出数据依据', brief.indexOf('12 篇里 0 篇实战') !== -1);
check('冲突同比渲染（含涨跌符号）', brief.indexOf('本周新增冲突 3 处') !== -1 && brief.indexOf('↑2') !== -1);
check('保留回原页面的深链', brief.indexOf('href="/brief.html"') !== -1);
check('简报里的用户数据被转义', brief.indexOf('<img src=x') === -1);

const degraded = CCA.renderCard({
  kind: 'l3_brief',
  state: 'degraded',
  analyzed_items: 5,
  topics: [{ topic: '索引', count: 5, levels: { 入门: 5 } }],
  patterns: [],
  questions: [],
  conflicts: {},
  overview: '',
  note: '追问生成失败（模型输出无法解析），主题分布仍然有效。',
  href: '/brief.html'
});
check('降级时保留主题分布', degraded.indexOf('索引') !== -1 && degraded.indexOf('5 篇') !== -1);
check('降级时说明原因', degraded.indexOf('主题分布仍然有效') !== -1);

const briefEmpty = CCA.renderCard({
  kind: 'l3_brief', state: 'empty', analyzed_items: 0, topics: [], patterns: [],
  questions: [], conflicts: {}, overview: '', note: '知识库还是空的。', href: '/brief.html'
});
check('空知识库只给提示，不渲染空表格', briefEmpty.indexOf('知识库还是空的') !== -1
  && briefEmpty.indexOf('topic-row') === -1);

console.log('\n后台任务：占位卡与失败卡');
const pending = CCA.renderCard({
  kind: 'pending', key: 't1', turn_id: 't1', capability: 'l2',
  label: '正在扫描冲突', note: '通常半分钟以内。', href: '/conflicts.html'
});
check('占位卡渲染标题与说明', pending.indexOf('正在扫描冲突') !== -1 && pending.indexOf('通常半分钟以内') !== -1);

const failedCard = CCA.renderCard({
  kind: 'failed', key: 't2', turn_id: 't2', capability: 'l2',
  label: '冲突检测', note: '后台执行失败，可以换原页面手动重试。', href: '/conflicts.html'
});
check('失败卡说明原因', failedCard.indexOf('后台执行失败') !== -1);
check('失败卡给出重试落点', failedCard.indexOf('href="/conflicts.html"') !== -1);

console.log('\nL2 冲突卡片：可操作的按钮要带齐寻址信息');
const conflictCard = CCA.renderCard({
  kind: 'l2_conflicts',
  key: 'card-1',
  summary: { scanned_items: 4, pairs_judged: 2, conflicts_found: 1 },
  href: '/conflicts.html',
  items: [
    {
      conflict_id: 'c1', item_a_id: 'a1', item_b_id: 'b1',
      title_a: '专注优先', title_b: XSS,
      claim_a: '多任务并行是效率的关键', claim_b: '多任务必然降低效率',
      conflict_type: '立场对立', detail: '一边要多任务', suggestion: '挑一个场景实测',
      confidence: 0.82, user_state: 'unseen'
    },
    {
      conflict_id: 'c2', item_a_id: 'a2', item_b_id: 'b2',
      title_a: '甲', title_b: '乙', claim_a: 'x', claim_b: 'y',
      conflict_type: '结论互斥', detail: '', suggestion: '',
      confidence: 0.61, user_state: 'ignored'
    }
  ]
});
check('冲突两侧主张都渲染', conflictCard.indexOf('多任务必然降低效率') !== -1);
check('已忽略的冲突加上 done 标记', conflictCard.indexOf('conflict done') !== -1);
check('按钮带上卡片 key（操作回流靠它寻址）', conflictCard.indexOf('data-card-key="card-1"') !== -1);
check('按钮带上冲突 id 与目标状态',
  conflictCard.indexOf('data-target="c1"') !== -1 && conflictCard.indexOf('data-value="accepted"') !== -1);
check('已忽略的条目不再出现「忽略」按钮',
  conflictCard.indexOf('data-target="c2" data-value="ignored"') === -1);
check('已处理过的条目给出「标回待处理」',
  conflictCard.indexOf('data-target="c2" data-value="unseen"') !== -1);
check('冲突标题里的用户数据被转义', conflictCard.indexOf('<img src=x') === -1);

const emptyScan = CCA.renderCard({
  kind: 'l2_conflicts', key: 'card-2', summary: { conflicts_found: 0 }, items: [], href: '/conflicts.html'
});
check('没扫出冲突时给一句交代而不是空白', emptyScan.indexOf('没有再发现新的矛盾') !== -1);

console.log('\nL5 诊断卡：结论、推理链与采纳按钮');
const diagnosis = CCA.renderCard({
  kind: 'l5_diagnosis',
  key: 'diag-1',
  state: 'ok',
  diagnosis_id: 'd1',
  pattern: '高收藏低完成',
  root_cause: XSS,
  confidence: 0.66,
  suggested_action: '把单次任务压到 15 分钟',
  reasoning_chain: ['收藏 12 条', '读完 1 条'],
  status: 'pending',
  metrics: { total_items: 12, completed_items: 1, study_events_week: 3, unseen_conflicts: 2 },
  note: '',
  href: '/l5.html'
});
check('诊断结论与置信度渲染', diagnosis.indexOf('高收藏低完成') !== -1 && diagnosis.indexOf('置信度 66%') !== -1);
check('归因正文被转义', diagnosis.indexOf('<img src=x') === -1);
check('推理链折叠展示', diagnosis.indexOf('推理链（2 步）') !== -1);
check('行为依据渲染', diagnosis.indexOf('知识库 12 条') !== -1);
check('给出采纳与拒绝两个按钮',
  diagnosis.indexOf('data-agent-action="l5.diagnosis.decide"') !== -1
  && diagnosis.indexOf('data-value="accepted"') !== -1
  && diagnosis.indexOf('data-value="rejected"') !== -1);

const rejected = CCA.renderCard({
  kind: 'l5_diagnosis', key: 'diag-2', state: 'ok', diagnosis_id: 'd2',
  pattern: '启动困难', root_cause: '门槛过高', confidence: 0.5, suggested_action: '先做最小的',
  reasoning_chain: [], status: 'rejected', metrics: {}, note: '', href: '/l5.html'
});
check('已拒绝的诊断不再出现采纳按钮', rejected.indexOf('data-agent-action') === -1);
check('已拒绝的诊断仍然展示结论', rejected.indexOf('启动困难') !== -1);

const diagDegraded = CCA.renderCard({
  kind: 'l5_diagnosis', key: 'diag-3', state: 'degraded', diagnosis_id: '',
  pattern: '', root_cause: '', confidence: 0, suggested_action: '', reasoning_chain: [],
  status: 'pending', metrics: {}, note: '归因分析失败（模型输出无法解析）。', href: '/l5.html'
});
check('降级时不渲染空的诊断框', diagDegraded.indexOf('class="diag"') === -1
  && diagDegraded.indexOf('归因分析失败') !== -1);

console.log('\nL4 卡片：目标 / 周计划 / 偏离');
const goalCard = CCA.renderCard({
  kind: 'l4_goal', goal_id: 'g1', description: XSS, note: '',
  href: '/l4.html', sends: [{ label: '生成周计划', message: '生成周计划' }]
});
check('目标描述被转义', goalCard.indexOf('<img src=x') === -1);
check('带上「生成周计划」按钮（按钮发消息）',
  goalCard.indexOf('data-agent-send="生成周计划"') !== -1);
check('目标卡不带操作类按钮（不调 actions）', goalCard.indexOf('data-agent-action') === -1);

const planCard = CCA.renderCard({
  kind: 'l4_plan', key: 'p1', state: 'ok',
  goal_description: '三个月掌握数据分析', plan_id: 'pl1', version: 2,
  rationale: '先打基础再上工具',
  tasks: [
    { task_id: 't1', week_index: 1, subject: '读完《入门》并写三条标准', status: 'done' },
    { task_id: 't2', week_index: 2, subject: XSS, status: 'pending' }
  ],
  progress: { total: 2, done: 1, pending: 1 },
  note: '', href: '/l4.html', sends: [{ label: '重新生成周计划', message: '重新生成周计划' }]
});
check('周任务按周渲染并标出完成状态',
  planCard.indexOf('第 1 周') !== -1 && planCard.indexOf('已完成') !== -1
  && planCard.indexOf('task-row done') !== -1);
check('计划版本号渲染', planCard.indexOf('v2') !== -1);
check('完成度渲染', planCard.indexOf('共 2 项，已完成 1 项') !== -1);
check('排序理由渲染', planCard.indexOf('先打基础再上工具') !== -1);
check('任务标题被转义', planCard.indexOf('<img src=x') === -1);
check('计划卡带重新生成按钮', planCard.indexOf('data-agent-send="重新生成周计划"') !== -1);

const planDegraded = CCA.renderCard({
  kind: 'l4_plan', key: 'p2', state: 'degraded', goal_description: '三个月掌握数据分析',
  plan_id: '', version: 0, rationale: '', tasks: [], progress: {},
  note: '计划生成失败（模型输出无法解析），已保留既有计划，请重试。',
  href: '/l4.html', sends: []
});
check('计划降级时说明原因而不是空表格',
  planDegraded.indexOf('已保留既有计划') !== -1 && planDegraded.indexOf('task-row') === -1);

const deviation = CCA.renderCard({
  kind: 'l4_deviation', key: 'd1', state: 'ok', plan_id: 'pl1',
  signals: { reasons: ['连续 7 天没有学习行为'], plan_total: 2, plan_done: 0 },
  analysis: {
    root_cause: XSS, adjustment: '把第一周任务拆成每天 15 分钟',
    expected_gain: '完成率翻倍', confidence: 0.7
  },
  note: '', href: '/l4.html'
});
check('偏离归因与建议渲染', deviation.indexOf('每天 15 分钟') !== -1
  && deviation.indexOf('预期改善') !== -1);
check('归因文本被转义', deviation.indexOf('<img src=x') === -1);
check('本地信号单独列出', deviation.indexOf('连续 7 天没有学习行为') !== -1);
check('给出两个操作按钮',
  deviation.indexOf('data-agent-action="l4.adjustment.apply"') !== -1
  && deviation.indexOf('data-agent-action="l4.adjustment.keep"') !== -1);
check('两个按钮都带上计划 id 作为 target',
  deviation.indexOf('data-target="pl1"') !== -1);

const devNoPlan = CCA.renderCard({
  kind: 'l4_deviation', key: 'd2', state: 'no_plan', plan_id: '', signals: {},
  analysis: {}, note: '还没有学习计划，先设定目标并生成计划。', href: '/l4.html'
});
check('没有计划时不给操作按钮',
  devNoPlan.indexOf('data-agent-action') === -1 && devNoPlan.indexOf('还没有学习计划') !== -1);

const devRestored = CCA.renderCard({
  kind: 'l4_deviation', key: 'd3', state: 'ok', plan_id: 'pl1',
  signals: { reasons: ['连续 7 天没有学习行为'] },
  analysis: { root_cause: '门槛过高', adjustment: '拆小', expected_gain: '', confidence: 0.6 },
  note: '后台执行失败（可能网络或模型超时），可以再试一次。', href: '/l4.html'
});
check('任务失败还原后仍显示失败原因与重试入口',
  devRestored.indexOf('可以再试一次') !== -1
  && devRestored.indexOf('data-agent-action="l4.adjustment.apply"') !== -1);

const notice = CCA.renderCard({
  kind: 'notice', title: '还没有学习目标', note: XSS, href: '/l4.html',
  sends: [{ label: '生成周计划', message: '生成周计划' }]
});
check('提示卡渲染标题与说明', notice.indexOf('还没有学习目标') !== -1);
check('提示卡说明被转义', notice.indexOf('<img src=x') === -1);
check('提示卡可以带「接下来做什么」的按钮',
  notice.indexOf('data-agent-send="生成周计划"') !== -1);

const noticeBare = CCA.renderCard({ kind: 'notice', title: 't', note: 'n', href: '', sends: [] });
check('提示卡没有按钮时不渲染空的按钮行', noticeBare.indexOf('cf-actions') === -1);

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
