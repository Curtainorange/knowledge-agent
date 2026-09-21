/* 认知副驾 · 对话工作台（app.html）
 *
 * 分两层：
 *   window.CCA —— **纯函数**：把后端返回的卡片渲染成 HTML。不碰 DOM，可以直接在
 *                 Node 里跑（tests/js/agent_behavior.js）。之所以把渲染单独摘出来，
 *                 是因为「转义」是最容易出漏洞又最难靠肉眼发现的地方——卡片里的标题、
 *                 摘要、标签全部来自用户输入，漏一个 esc 就是一个注入口。
 *   页面装配   —— 消息流、能力快捷入口、发送。只在浏览器里执行。
 *
 * 注意：本文件所有语句都显式带分号——以 `[` 或 `(` 开头的续行会被解析成对上一行的
 * 索引 / 调用（ASI 不补分号），这类错误语法检查查不出来，只在运行时崩（同 app.js）。
 */
window.CCA = (function () {
  'use strict';

  var esc = CC.esc;  // 复用共享转义。不提供「无转义兜底」——静默不转义比报错危险得多

  function pct(value) {
    var n = Number(value);
    if (isNaN(n)) {
      return 0;
    }
    return Math.round(n * 100);
  }

  function embedBadge(status) {
    if (status === 'embedded') {
      return '<span class="badge ok">已向量化</span>';
    }
    if (status === 'embed_failed') {
      return '<span class="badge warn">关键词兜底</span>';
    }
    return '<span class="badge">待向量化</span>';
  }

  /* 召回候选：包含已命中项（后端约定），这里按 excludeIds 排掉。
     只给一条结果时用户无从判断模型是真定位到了还是随手挑了一条，所以必须摆出「其他可能」。 */
  function candidatesHtml(candidates, excludeIds) {
    var excluded = excludeIds || [];
    var rest = (candidates || []).filter(function (c) {
      return excluded.indexOf(c.item_id) === -1;
    });
    if (!rest.length) {
      return '';
    }
    var rows = rest.map(function (c) {
      var channels = (c.channels || []).join(' + ');
      return '<div class="cand">' +
        '<div class="cand-main">' +
          '<div class="cand-title">' + esc(c.title) + '</div>' +
          '<div class="cand-snip">' + esc(c.snippet) + '</div>' +
        '</div>' +
        '<div class="cand-side">相关度 ' + Number(c.score || 0).toFixed(2) + '<br>' + esc(channels) + '</div>' +
      '</div>';
    }).join('');
    return '<details class="candidates"><summary>其他可能（' + rest.length + '）</summary>' + rows + '</details>';
  }

  function locatedHtml(card) {
    var items = card.items || [];
    var ids = items.map(function (i) { return i.item_id; });
    var blocks = items.map(function (item) {
      return '<div class="hit">' +
        '<div class="row"><strong>' + esc(item.title) + '</strong>' + embedBadge(item.embed_status) + '</div>' +
        '<div class="snippet">' + esc(item.snippet) + '</div>' +
        '<div class="meta">已读 ' + pct(item.read_progress) + '%</div>' +
        '<div class="card-actions"><a href="' + esc(item.href) + '">查看全文</a></div>' +
      '</div>';
    }).join('');
    var hint = card.hint ? '<div class="hintline" style="font-size:13px;color:var(--warn);margin-top:8px">' + esc(card.hint) + '</div>' : '';
    var reason = card.reason ? '<div class="meta">判定依据：' + esc(card.reason) + '</div>' : '';
    return blocks + hint + reason + candidatesHtml(card.candidates, ids);
  }

  function clarifyHtml(card) {
    var turn = Number(card.turn || 0);
    var max = Number(card.max_turns || 3);
    var bar = turn > 0 ? '<div class="meta">已追问 ' + turn + ' / ' + max + ' 轮</div>' : '';
    var reason = card.reason ? '<div class="meta">为什么问这个：' + esc(card.reason) + '</div>' : '';
    return bar + reason + candidatesHtml(card.candidates, []);
  }

  function emptyHtml(card) {
    return '<div class="notice">' + esc(card.note) +
      (card.href ? '<div class="card-actions"><a href="' + esc(card.href) + '">去录入</a></div>' : '') +
      '</div>';
  }

  function createdHtml(card) {
    var tags = (card.tags || []).map(function (tag) {
      return '<span class="badge">' + esc(tag) + '</span>';
    }).join('');
    return '<div class="created">' +
      '<div class="row"><div class="c-head">已入库：' + esc(card.title) + '</div>' + embedBadge(card.embed_status) + '</div>' +
      (tags ? '<div class="c-tags">' + tags + '</div>' : '') +
      '<div class="card-actions"><a href="' + esc(card.href) + '">打开这条</a></div>' +
      '</div>';
  }

  function noteEmptyHtml(card) {
    return '<div class="notice">' + esc(card.note) + '</div>';
  }

  /* 「已经开工、结果稍后」的占位卡。前端看到这个 kind 就去轮询会话，
     直到它被结果卡替换（后端 worker 会就地改写那条消息）。 */
  function pendingHtml(card) {
    return '<div class="pending-card">' +
      '<div class="p-head"><span class="p-dot"></span>' + esc(card.label || '正在处理') + '</div>' +
      (card.note ? '<div class="p-note">' + esc(card.note) + '</div>' : '') +
    '</div>';
  }

  /* 执行失败（重试耗尽）的兜底卡：既要说清出了事，也要给一个重试的落点，
     否则用户只能对着一条永远「正在处理」的消息干等。 */
  function failedHtml(card) {
    return '<div class="notice">' + esc(card.note) +
      (card.href ? '<div class="card-actions"><a href="' + esc(card.href) + '">去原页面重试</a></div>' : '') +
    '</div>';
  }

  /* L2 冲突卡片组。
     类名与 conflicts.html 完全共用——同一份数据在对话和原页面里长得一样，
     用户不需要在两个地方学两套说法。 */
  var CONFLICT_STATE_LABEL = { unseen: '待处理', accepted: '已采纳', ignored: '已忽略' };

  function confidenceBadge(value) {
    var pct = Math.round(Number(value || 0) * 100);
    return '<span class="badge ' + (pct >= 80 ? 'ok' : 'warn') + '">置信度 ' + pct + '%</span>';
  }

  function conflictStateBadge(value) {
    var cls = value === 'accepted' ? 'ok' : (value === 'ignored' ? 'warn' : '');
    return '<span class="badge ' + cls + '">' +
      esc(CONFLICT_STATE_LABEL[value] || value) + '</span>';
  }

  function conflictSideHtml(title, itemId, claim) {
    return '<div class="cf-side">' +
      '<div class="cf-who"><a href="/knowledge.html?item=' + esc(itemId) + '">' + esc(title) + '</a></div>' +
      '<div>' + esc(claim || '（该条主张已不存在）') + '</div>' +
    '</div>';
  }

  function actionButton(cardKey, action, targetId, value, label) {
    return '<button class="sm" data-agent-action="' + esc(action) + '"' +
      ' data-card-key="' + esc(cardKey) + '" data-target="' + esc(targetId) + '"' +
      ' data-value="' + esc(value) + '">' + esc(label) + '</button>';
  }

  function conflictHtml(c, cardKey) {
    var done = c.user_state !== 'unseen';
    var body = '<div class="cf-head">' +
      '<span class="cf-type">' + esc(c.conflict_type || '冲突') + '</span>' +
      '<span class="cf-pair">' + esc(c.title_a) + ' ↔ ' + esc(c.title_b) + '</span>' +
      confidenceBadge(c.confidence) + conflictStateBadge(c.user_state) +
    '</div>';
    body += '<div class="cf-claims">' +
      conflictSideHtml(c.title_a, c.item_a_id, c.claim_a) +
      '<div class="cf-vs">↔</div>' +
      conflictSideHtml(c.title_b, c.item_b_id, c.claim_b) +
    '</div>';
    if (c.detail) {
      body += '<div class="cf-block"><b>依据：</b>' + esc(c.detail) + '</div>';
    }
    if (c.suggestion) {
      body += '<div class="cf-block"><b>建议：</b>' + esc(c.suggestion) + '</div>';
    }
    body += '<div class="cf-actions">';
    if (c.user_state !== 'accepted') {
      body += actionButton(cardKey, 'l2.conflict.state', c.conflict_id, 'accepted', '采纳');
    }
    if (c.user_state !== 'ignored') {
      body += actionButton(cardKey, 'l2.conflict.state', c.conflict_id, 'ignored', '忽略');
    }
    if (done) {
      body += actionButton(cardKey, 'l2.conflict.state', c.conflict_id, 'unseen', '标回待处理');
    }
    body += '</div>';
    return '<div class="conflict' + (done ? ' done' : '') + '">' + body + '</div>';
  }

  function l2ConflictsHtml(card) {
    var items = card.items || [];
    var parts = [];
    if (!items.length) {
      parts.push('<div class="notice">这次没有再发现新的矛盾。</div>');
    } else {
      parts.push(items.map(function (c) { return conflictHtml(c, card.key); }).join(''));
    }
    var summary = card.summary || {};
    if (typeof summary.scanned_items === 'number') {
      parts.push('<div class="meta">本次扫描 ' + Number(summary.scanned_items) + ' 条 · 判定 ' +
        Number(summary.pairs_judged || 0) + ' 对主张 · 新增冲突 ' +
        Number(summary.conflicts_found || 0) + ' 处</div>');
    }
    if (card.href) {
      parts.push('<div class="card-actions"><a href="' + esc(card.href) + '">在原页面查看全部冲突</a></div>');
    }
    return '<div class="l2-card">' + parts.join('') + '</div>';
  }

  /* L5 归因诊断。诊断结论是**一次性**的处置对象（采纳/不用），
     所以按钮只在状态还是 pending 时出现。 */
  var DIAGNOSIS_STATE_LABEL = { pending: '待处理', accepted: '已采纳', rejected: '已拒绝' };

  function l5DiagnosisHtml(card) {
    var parts = [];
    if (card.state === 'ok') {
      parts.push('<div class="diag">' +
        '<div class="row"><strong>' + esc(card.pattern || '诊断') + '</strong>' +
        '<span class="badge ' + (card.confidence >= 0.6 ? 'ok' : 'warn') + '">置信度 ' +
        Math.round(Number(card.confidence || 0) * 100) + '%</span>' +
        '<span class="badge">' + esc(DIAGNOSIS_STATE_LABEL[card.status] || card.status || '') + '</span></div>' +
        '<div class="d-body">' + esc(card.root_cause) + '</div>' +
        (card.suggested_action
          ? '<div class="cf-block"><b>可以做什么：</b>' + esc(card.suggested_action) + '</div>'
          : '') +
      '</div>');
      var chain = card.reasoning_chain || [];
      if (chain.length) {
        parts.push('<details class="candidates"><summary>推理链（' + chain.length + ' 步）</summary>' +
          chain.map(function (step) {
            return '<div class="cand"><div class="cand-main">' + esc(step) + '</div></div>';
          }).join('') +
        '</details>');
      }
      var metrics = card.metrics || {};
      if (typeof metrics.total_items === 'number') {
        parts.push('<div class="meta">知识库 ' + Number(metrics.total_items) + ' 条 · 读完 ' +
          Number(metrics.completed_items || 0) + ' 条 · 本周学习行为 ' +
          Number(metrics.study_events_week || 0) + ' 次 · 未处理冲突 ' +
          Number(metrics.unseen_conflicts || 0) + ' 处</div>');
      }
      if (card.status !== 'rejected') {
        parts.push('<div class="cf-actions">' +
          actionButton(card.key, 'l5.diagnosis.decide', card.diagnosis_id, 'accepted', '采纳建议') +
          actionButton(card.key, 'l5.diagnosis.decide', card.diagnosis_id, 'rejected', '不用了') +
        '</div>');
      }
    }
    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    if (card.href) {
      parts.push('<div class="card-actions"><a href="' + esc(card.href) + '">在原页面查看</a></div>');
    }
    return '<div class="l5-card">' + parts.join('') + '</div>';
  }

  /* L3 认知简报：主题分布 + 结构信号 + 追问。
     类名与 brief.html 共用（.topic-row / .patterns / .question-card），
     所以同一份结果在对话里和原页面长得一样，不出现两套视觉语言。 */
  var LEVELS = ['入门', '进阶', '实战', '未分类'];

  function topicRowsHtml(topics) {
    return (topics || []).map(function (t) {
      var levels = LEVELS.filter(function (lv) {
        return (t.levels || {})[lv];
      }).map(function (lv) {
        return '<span class="badge level">' + esc(lv) + ' ' + Number(t.levels[lv]) + '</span>';
      }).join(' ');
      return '<div class="topic-row">' +
        '<div class="topic-name">' + esc(t.topic) + '</div>' +
        '<div class="topic-levels"><span class="badge ok">' + Number(t.count) + ' 篇</span> ' + levels + '</div>' +
      '</div>';
    }).join('');
  }

  function patternListHtml(patterns) {
    var rows = (patterns || []).map(function (p) {
      var kind = String(p).indexOf('完全缺失') === 0 ? 'missing' : 'abundant';
      return '<div class="pattern ' + kind + '">' + esc(p) + '</div>';
    }).join('');
    if (!rows) {
      return '';
    }
    return '<div class="patterns"><div class="meta">结构信号</div>' + rows + '</div>';
  }

  function questionCardsHtml(questions) {
    return (questions || []).map(function (q) {
      var body = '<div class="q-head">' + esc(q.question) + '</div>';
      if (q.why) {
        body += '<div class="cf-block"><b>为什么问：</b>' + esc(q.why) + '</div>';
      }
      if (q.evidence) {
        body += '<div class="cf-block"><b>数据依据：</b>' + esc(q.evidence) + '</div>';
      }
      if (q.next_step) {
        body += '<div class="cf-block"><b>可以做什么：</b>' + esc(q.next_step) + '</div>';
      }
      return '<div class="question-card">' + body + '</div>';
    }).join('');
  }

  function l3BriefHtml(card) {
    var parts = [];
    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    if (card.overview) {
      parts.push('<div class="meta">' + esc(card.overview) + '</div>');
    }

    var body = topicRowsHtml(card.topics);
    if (body) {
      parts.push(body);
    }
    parts.push(patternListHtml(card.patterns));
    parts.push(questionCardsHtml(card.questions));

    var stats = card.conflicts || {};
    if (typeof stats.this_week === 'number') {
      var delta = Number(stats.delta || 0);
      var arrow = delta > 0 ? ('↑' + delta) : (delta < 0 ? ('↓' + Math.abs(delta)) : '持平');
      parts.push(
        '<div class="meta">本周新增冲突 ' + Number(stats.this_week) + ' 处（上周 ' +
        Number(stats.last_week || 0) + '，' + arrow + '）· 本次分析 ' +
        Number(card.analyzed_items || 0) + ' 条</div>'
      );
    }
    if (card.href) {
      parts.push('<div class="card-actions"><a href="' + esc(card.href) + '">在原页面查看完整简报</a></div>');
    }
    return '<div class="brief">' + parts.join('') + '</div>';
  }

  function guideHtml(card) {
    return '<div class="guide">' +
      '<div class="g-head">' + esc(card.label) + '</div>' +
      '<div class="g-note">' + esc(card.note) + '</div>' +
      (card.href ? '<div class="card-actions"><a href="' + esc(card.href) + '">去原页面</a></div>' : '') +
      '</div>';
  }

  var RENDERERS = {
    l1_located: locatedHtml,
    l1_clarify: clarifyHtml,
    l1_empty: emptyHtml,
    l2_conflicts: l2ConflictsHtml,
    l3_brief: l3BriefHtml,
    l5_diagnosis: l5DiagnosisHtml,
    pending: pendingHtml,
    failed: failedHtml,
    knowledge_created: createdHtml,
    note_empty: noteEmptyHtml,
    guide: guideHtml
  };

  /* 未知 kind 一律不渲染（而不是把原始对象塞进页面）：
     后端新增卡片类型时，旧前端应该是「少一块」而不是「崩一屏」。 */
  function renderCard(card) {
    if (!card || typeof card !== 'object' || !card.kind) {
      return '';
    }
    var render = RENDERERS[card.kind];
    if (!render) {
      return '';
    }
    return render(card);
  }

  return {
    renderCard: renderCard,
    candidatesHtml: candidatesHtml,
    embedBadge: embedBadge,
    pct: pct
  };
})();


(function () {
  'use strict';

  if (typeof document === 'undefined') {
    return;  // Node 里只加载上面的纯函数（tests/js/agent_behavior.js）
  }
  var log = document.getElementById('agent-log');
  if (!log) {
    return;
  }

  var CONV_KEY = 'cc_agent_conversation';
  var POLL_MS = 2500;
  var POLL_MAX = 60;   // ≈2.5 分钟；L2 扫描最慢也就这个量级，超了就停手不再轮询

  /* 消息数组是**唯一事实来源**，DOM 是它的函数。
     这样三件事共用一条路径：发一轮对话、轮询后台结果、卡片内操作后刷新——
     各写各的 append/替换逻辑，迟早会出现「DOM 说已忽略、轮询又把它变回待处理」。 */
  var state = { convId: null, messages: [], notices: [], sending: false, polls: 0, timer: null };

  function rememberConversation(id) {
    state.convId = id;
    try {
      if (id) {
        localStorage.setItem(CONV_KEY, id);
      } else {
        localStorage.removeItem(CONV_KEY);
      }
    } catch (err) { /* 隐私模式下 localStorage 可能不可用，不影响使用 */ }
  }

  function savedConversation() {
    try {
      return localStorage.getItem(CONV_KEY) || '';
    } catch (err) {
      return '';
    }
  }

  function kindOf(message) {
    if (message.role === 'user') {
      return 'user';
    }
    if (message.role === 'assistant') {
      return 'bot';
    }
    return 'sys';
  }

  /* 用「内容 + 卡片」的指纹判断一条消息是否需要重建，
     避免每轮轮询都把整屏消息重建一遍（会闪、也会丢掉展开状态）。 */
  function signature(message) {
    return kindOf(message) + '\u0000' + (message.content || '') + '\u0000' +
      JSON.stringify(message.card || null);
  }

  function messageNode(message, index) {
    var card = message.card;
    var cardHtml = CCA.renderCard(card);
    var node = document.createElement('div');
    node.className = 'msg ' + kindOf(message) + (cardHtml ? ' has-card' : '');
    node.setAttribute('data-index', String(index));
    node.setAttribute('data-sig', signature(message));

    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = message.content || '';
    node.appendChild(bubble);

    if (cardHtml) {
      var slot = document.createElement('div');
      slot.className = 'card-slot';
      if (card && card.key) {
        slot.setAttribute('data-card-key', card.key);   // 操作回流时就地替换的锚点
      }
      slot.innerHTML = cardHtml;
      node.appendChild(slot);
    }
    return node;
  }

  function allMessages() {
    return state.messages.concat(state.notices);
  }

  function syncMessages() {
    var messages = allMessages();
    var nodes = log.children;
    for (var i = 0; i < messages.length; i++) {
      var fresh = messageNode(messages[i], i);
      var existing = nodes[i];
      if (!existing) {
        log.appendChild(fresh);
      } else if (existing.getAttribute('data-sig') !== fresh.getAttribute('data-sig')) {
        log.replaceChild(fresh, existing);   // pending 卡变结果卡就走这里
      }
    }
    while (log.children.length > messages.length) {
      log.removeChild(log.lastChild);
    }
    log.scrollTop = log.scrollHeight;
    schedulePollIfPending();
  }

  function pushNotice(text) {
    state.notices.push({ role: 'sys', content: text, card: null });
    syncMessages();
  }

  function hasPending() {
    return state.messages.some(function (m) {
      return m.card && m.card.kind === 'pending';
    });
  }

  /* 有 pending 卡就轮询：后端 worker 会就地改写那条消息，这里负责把更新后的
     会话读回来。用「重读会话」而不是专门的查询端点，顺带把刷新页面恢复历史
     一起解决了——两件事本来就是同一份数据。 */
  function schedulePollIfPending() {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    if (!hasPending() || state.polls >= POLL_MAX) {
      return;
    }
    state.timer = setTimeout(poll, POLL_MS);
  }

  async function poll() {
    state.timer = null;
    if (!state.convId) {
      return;
    }
    state.polls += 1;
    try {
      var data = await CC.api('GET', '/api/v1/agent/conversation/' + encodeURIComponent(state.convId));
      state.messages = data.messages || [];
      syncMessages();
    } catch (err) {
      // 轮询是尽力而为：网络抖动就等下一轮，不往消息流里刷错误
      schedulePollIfPending();
    }
  }

  function setStatus(text) {
    var el = document.getElementById('agent-status');
    if (el) {
      el.textContent = text || '';
    }
  }

  /* 快捷入口。文案与「是否已接入」都来自服务端（/agent/capabilities）——
     前端硬编码一份必然与路由规则漂移：示例话术改了、本地规则没同步，点按钮就变成
     一次模型调用甚至兜底成闲聊。 */
  async function loadChips() {
    var box = document.getElementById('agent-chips');
    if (!box) {
      return;
    }
    var items = [];
    try {
      items = await CC.api('GET', '/api/v1/agent/capabilities');
    } catch (err) {
      return;  // 快捷入口是便利功能，拿不到就不显示，不该拦着用户打字
    }
    box.innerHTML = '';
    items.forEach(function (item) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip' + (item.wired ? '' : ' pending');
      btn.textContent = item.wired ? item.label : item.label + '（去原页面）';
      btn.title = item.example;
      btn.addEventListener('click', function () {
        if (item.wired) {
          send(item.example);
        } else if (item.href) {
          location.href = item.href;
        }
      });
      box.appendChild(btn);
    });
  }

  async function send(text) {
    var input = document.getElementById('agent-input');
    var message = String(text == null ? input.value : text).trim();
    if (!message || state.sending) {
      return;
    }
    input.value = '';
    state.sending = true;
    // 乐观追加用户那条：不等回包，输入立刻可见（后端也会落库，下一轮轮询会以
    // 服务端版本为准覆盖它，所以这里不需要更聪明的合并逻辑）
    state.messages.push({ role: 'user', content: message, card: null });
    syncMessages();
    setStatus('处理中…');
    try {
      var result = await CC.api('POST', '/api/v1/agent/chat', {
        message: message,
        conversation_id: state.convId
      });
      rememberConversation(result.conversation_id);
      state.messages.push({
        role: 'assistant', content: result.reply, card: result.card, source: 'agent'
      });
      state.polls = 0;
      syncMessages();
      setStatus('');
    } catch (err) {
      // 失败时把乐观追加的那条撤掉，避免界面里留下一条后端并不知道的「我说的」
      state.messages.pop();
      pushNotice('出错了：' + ((err && err.message) || err));
      setStatus('');
    } finally {
      state.sending = false;
    }
    input.focus();
  }

  /* 卡片内操作：采纳 / 忽略 / 标回。
     不追加新消息——对已有结果的处置不是新的一轮对话。服务端返回刷新后的卡片，
     这里替换 state 里的那张再整体重绘，保证界面与数据库一致。 */
  async function runAction(button) {
    var cardKey = button.getAttribute('data-card-key');
    var action = button.getAttribute('data-agent-action');
    var target = button.getAttribute('data-target');
    var value = button.getAttribute('data-value') || '';
    if (!cardKey || !action || !target || !state.convId) {
      return;
    }
    button.disabled = true;
    try {
      var result = await CC.api('POST', '/api/v1/agent/actions', {
        conversation_id: state.convId,
        card_key: cardKey,
        action: action,
        target_id: target,
        value: value
      });
      state.messages.forEach(function (message) {
        if (message.card && message.card.key === cardKey) {
          message.card = result.card;
        }
      });
      syncMessages();
      CC.toast(result.reply || '已处理');
    } catch (err) {
      button.disabled = false;
      CC.toast('操作失败：' + ((err && err.message) || err));
    }
  }

  function bind() {
    document.getElementById('agent-send').addEventListener('click', function () {
      send(null);
    });
    document.getElementById('agent-input').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        send(null);
      }
    });
    // 事件委托：卡片会被重建（轮询、操作后重绘），逐个绑监听会失效
    log.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) {
        return;
      }
      var button = target.closest('[data-agent-action]');
      if (button) {
        event.preventDefault();
        runAction(button);
      }
    });
    document.getElementById('btn-new').addEventListener('click', function () {
      rememberConversation(null);
      state.messages = [];
      state.notices = [];
      state.polls = 0;
      if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      syncMessages();
      setStatus('');
      CC.toast('已开启新会话');
      document.getElementById('agent-input').focus();
    });
  }

  /* 刷新页面后恢复上一条会话：消息与卡片都在服务端，不需要前端自己存历史。
     会话已被删除（或换了账号）时静默开新会话，不打扰用户。 */
  async function restore() {
    var saved = savedConversation();
    if (!saved) {
      return;
    }
    try {
      var data = await CC.api('GET', '/api/v1/agent/conversation/' + encodeURIComponent(saved));
      rememberConversation(data.conversation_id);
      state.messages = data.messages || [];
      state.polls = 0;
      syncMessages();
    } catch (err) {
      rememberConversation(null);
    }
  }

  (async function boot() {
    if (!(await CC.requireAuth())) {
      return;
    }
    bind();
    loadChips();
    restore();
    document.getElementById('agent-input').focus();
  })();
})();
