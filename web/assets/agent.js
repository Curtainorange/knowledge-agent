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
    /* 书籍通读来源（book:<id>）没有知识条目页，标题不做成链接 */
    var isBook = typeof itemId === 'string' && itemId.indexOf('book:') === 0;
    var who = isBook
      ? '<div class="cf-who">' + esc(title) + '</div>'
      : '<div class="cf-who"><a href="/knowledge.html?item=' + esc(itemId) + '">' + esc(title) + '</a></div>';
    return '<div class="cf-side">' +
      who +
      '<div>' + esc(claim || '（该条主张已不存在）') + '</div>' +
    '</div>';
  }

  function actionButton(cardKey, action, targetId, value, label) {
    return '<button class="sm" data-agent-action="' + esc(action) + '"' +
      ' data-card-key="' + esc(cardKey) + '" data-target="' + esc(targetId) + '"' +
      ' data-value="' + esc(value) + '">' + esc(label) + '</button>';
  }

  /* 卡片上的第二类按钮：点它等于替用户说一句话。

     用于「再做一件耗时的事」（生成周计划、按建议重排、再同步一次）——
     复用整条异步回合链路，不必为它单开一个动作端点。
     与 `data-agent-action` 的分界线是**耗时**：秒级改状态走 actions，要跑模型就发消息。 */
  function sendsHtml(sends) {
    return (sends || []).map(function (s) {
      return '<button class="sm" data-agent-send="' + esc(s.message) + '">' + esc(s.label) + '</button>';
    }).join('');
  }

  function actionsRow(card) {
    var sends = sendsHtml(card.sends);
    var link = card.href
      ? '<a href="' + esc(card.href) + '">在原页面查看</a>'
      : '';
    if (!sends) {
      return link ? '<div class="card-actions">' + link + '</div>' : '';
    }
    return '<div class="cf-actions">' + sends + '</div>' +
      (link ? '<div class="card-actions">' + link + '</div>' : '');
  }

  function noticeHtml(card) {
    var parts = ['<div class="notice">' +
      (card.title ? '<div class="n-title">' + esc(card.title) + '</div>' : '') +
      esc(card.note || '') + '</div>'];
    parts.push(actionsRow(card));
    return '<div class="notice-card">' + parts.join('') + '</div>';
  }

  function l4GoalHtml(card) {
    var parts = ['<div class="diag">' +
      '<div class="row"><strong>' + esc(card.description) + '</strong>' +
      '<span class="badge">学习目标</span></div>' +
      '<div class="meta">目标已记下。拆成每周要做的事，执行进度才有据可查。</div>' +
    '</div>'];
    parts.push(actionsRow(card));
    return '<div class="l4-card">' + parts.join('') + '</div>';
  }

  /* 微信读书同步结果：数字用统计块摆出来。
     同步是「一次性拉一批」的动作，用户最关心的是「拉进来多少、还剩多少」。 */
  function wereadSyncHtml(card) {
    var r = card.result || {};
    var stats = [
      { label: '有笔记的书', value: r.total_books },
      { label: '本次扫描', value: r.scanned_books },
      { label: '新增知识', value: r.created },
      { label: '已存在跳过', value: r.skipped }
    ];
    if (r.failed_books) {
      stats.push({ label: '拉取失败', value: r.failed_books });
    }
    var parts = ['<div class="stats">' + stats.map(function (s) {
      return '<div class="stat"><div class="stat-num">' + Number(s.value || 0) + '</div>' +
        '<div class="stat-label">' + esc(s.label) + '</div></div>';
    }).join('') + '</div>'];

    if (Number(r.pending_books || 0) > 0) {
      parts.push('<div class="meta">还有 ' + Number(r.pending_books) +
        ' 本没扫完（单次有上限），点下面的按钮接着扫。</div>');
    }
    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    parts.push(actionsRow(card));
    return '<div class="weread-card">' + parts.join('') + '</div>';
  }

  var TASK_STATE_LABEL = { done: '已完成', pending: '待办' };

  /* 书架：每本一行，带进度与「继续读」深链。
     阅读器是独立页面——正文阅读这种深度交互不适合塞进消息流。 */
  function booksHtml(card) {
    var parts = [];
    var books = card.books || [];
    if (!books.length) {
      parts.push('<div class="notice">' + esc(card.note || '书架还是空的。') + '</div>');
    } else {
      parts.push(books.map(function (b) {
        var pct = Math.round(Number(b.read_progress || 0) * 100);
        var label = pct >= 100 ? '已读完' : (pct > 0 ? '已读 ' + pct + '%' : '未开始');
        return '<div class="book-row">' +
          '<div class="book-main">' +
            '<div class="book-title">' + esc(b.title) + '</div>' +
            '<div class="book-meta">' + esc(b.author || '佚名') +
              (b.chapter_count ? ' · ' + Number(b.chapter_count) + ' 章' : '') + '</div>' +
          '</div>' +
          '<div class="book-side">' +
            '<span class="badge' + (pct >= 100 ? ' ok' : '') + '">' + esc(label) + '</span>' +
            '<a href="' + esc(b.href) + '">' + (pct > 0 && pct < 100 ? '继续读' : '打开') + '</a>' +
          '</div>' +
        '</div>';
      }).join(''));
    }
    if (card.note && books.length) {
      parts.push('<div class="meta">' + esc(card.note) + '</div>');
    }
    parts.push(actionsRow(card));
    return '<div class="books-card">' + parts.join('') + '</div>';
  }

  function l4PlanHtml(card) {
    var parts = [];
    if (card.state !== 'ok') {
      parts.push('<div class="notice">' + esc(card.note || '这次没能生成计划，稍后再试一次。') + '</div>');
      parts.push(actionsRow(card));
      return '<div class="l4-card">' + parts.join('') + '</div>';
    }

    parts.push('<div class="plan-head"><strong>' + esc(card.goal_description) + '</strong>' +
      '<span class="badge">v' + Number(card.version || 1) + '</span></div>');

    /* 每条任务带上时长与类型，并把「为什么排在这里」放进折叠细节。
       依据是规则算出来的（周容量 / 复习点 / 剩余进度），不是模型的说辞——
       用户能看到它、也能据此反驳规则。 */
    parts.push((card.tasks || []).map(function (t) {
      var done = t.status === 'done';
      var basis = t.basis || {};
      var meta = [];
      if (t.minutes) {
        meta.push(Number(t.minutes) + ' 分钟');
      }
      meta.push(t.task_type === 'review' ? '复习' : '新学');
      var row = '<div class="task-row' + (done ? ' done' : '') + '">' +
        '<span class="task-week">第 ' + Number(t.week_index) + ' 周</span>' +
        '<span class="task-subject">' + esc(t.subject) + '</span>' +
        '<span class="meta">' + esc(meta.join(' · ')) + '</span>' +
        '<span class="badge' + (done ? ' ok' : '') + '">' +
          esc(TASK_STATE_LABEL[t.status] || t.status) + '</span>' +
      '</div>';
      if (basis.why) {
        row += '<details class="task-basis"><summary>为什么排在这里</summary>' +
          '<div class="meta">' + esc(basis.why) + '</div></details>';
      }
      return row;
    }).join(''));

    var snapshot = card.rules_snapshot || {};
    if (snapshot.weekly_minutes) {
      parts.push('<div class="meta">排期规则：每周 ' + Number(snapshot.weekly_minutes) +
        ' 分钟 · 新学占 ' + Math.round(Number(snapshot.new_ratio || 0) * 100) + '%' +
        (snapshot.review_count ? (' · 含 ' + Number(snapshot.review_count) + ' 个复习任务') : '') +
        (Number(snapshot.remaining_ratio || 1) < 0.99
          ? (' · 只排剩下没学的 ' + Math.round(Number(snapshot.remaining_ratio) * 100) + '%')
          : '') +
      '</div>');
    }

    var progress = card.progress || {};
    parts.push('<div class="meta">共 ' + Number(progress.total || 0) + ' 项，已完成 ' +
      Number(progress.done || 0) + ' 项</div>');

    if (card.rationale) {
      parts.push('<div class="cf-block"><b>为什么这样排：</b>' + esc(card.rationale) + '</div>');
    }
    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    parts.push(actionsRow(card));
    return '<div class="l4-card">' + parts.join('') + '</div>';
  }

  function l4DeviationHtml(card) {
    var parts = [];
    if (card.state !== 'ok') {
      parts.push('<div class="notice">' + esc(card.note || '暂时没有可展示的偏离分析。') + '</div>');
      parts.push(actionsRow(card));
      return '<div class="l4-card">' + parts.join('') + '</div>';
    }

    var analysis = card.analysis || {};
    var signals = card.signals || {};
    parts.push('<div class="diag">' +
      '<div class="row"><strong>' + esc(analysis.root_cause || '检测到偏离') + '</strong>' +
      '<span class="badge ' + (Number(analysis.confidence || 0) >= 0.6 ? 'ok' : 'warn') +
      '">置信度 ' + Math.round(Number(analysis.confidence || 0) * 100) + '%</span></div>' +
      (analysis.adjustment
        ? '<div class="cf-block"><b>建议调整：</b>' + esc(analysis.adjustment) + '</div>' : '') +
      (analysis.expected_gain
        ? '<div class="cf-block"><b>预期改善：</b>' + esc(analysis.expected_gain) + '</div>' : '') +
    '</div>');

    var reasons = signals.reasons || [];
    if (reasons.length) {
      parts.push('<div class="patterns"><div class="meta">本地统计出的偏离信号</div>' +
        reasons.map(function (r) {
          return '<div class="pattern abundant">' + esc(r) + '</div>';
        }).join('') +
      '</div>');
    }

    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }

    parts.push('<div class="cf-actions">' +
      actionButton(card.key, 'l4.adjustment.apply', card.plan_id, 'accepted', '按建议重排计划') +
      actionButton(card.key, 'l4.adjustment.keep', card.plan_id, 'kept', '保持原计划') +
    '</div>');
    if (card.href) {
      parts.push('<div class="card-actions"><a href="' + esc(card.href) + '">在原页面查看</a></div>');
    }
    return '<div class="l4-card">' + parts.join('') + '</div>';
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

  /* 书观点 ↔ 笔记的互补对：矛盾逼你修正，印证给你确认。 */
  function echoesHtml(card) {
    var echoes = card.echoes || [];
    if (!echoes.length) {
      return '';
    }
    var rows = echoes.map(function (e) {
      return '<div class="cand"><div class="cand-main">' +
        '<div class="cand-title">' + esc(e.title_a) + ' ↔ ' + esc(e.title_b) + '</div>' +
        '<div class="cand-snip">' + esc(e.claim_a) + '</div>' +
        '<div class="cand-snip">' + esc(e.claim_b) + '</div>' +
      '</div></div>';
    }).join('');
    return '<details class="candidates"><summary>互相印证（' + echoes.length + '）</summary>' +
      rows + '</details>';
  }

  function l2ConflictsHtml(card) {
    var items = card.items || [];
    var parts = [];
    if (!items.length) {
      parts.push('<div class="notice">这次没有再发现新的矛盾。</div>');
    } else {
      parts.push(items.map(function (c) { return conflictHtml(c, card.key); }).join(''));
    }
    parts.push(echoesHtml(card));
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

  /* 通读笔记：总评 + 分章要点折叠 + 「聊聊这本书」。
     类名复用现有卡片语言（.cf-block / .meta / details.candidates / .cand），不新增视觉样式。 */
  function bookDigestHtml(card) {
    var parts = [];
    if (card.summary) {
      parts.push('<div class="cf-block"><b>总评：</b>' + esc(card.summary) + '</div>');
    }
    var chapters = (card.chapters || []).filter(function (c) { return c.gist; });
    if (chapters.length) {
      parts.push('<details class="candidates"><summary>分章要点（' + chapters.length + ' 段）</summary>' +
        chapters.map(function (c) {
          var body = '<div class="cand"><div class="cand-main">' +
            '<div class="cand-title">' + esc(c.title) + '</div>' +
            '<div class="cand-snip">' + esc(c.gist) + '</div>' +
            ((c.points || []).length
              ? '<div class="cand-snip">' + c.points.map(esc).join('；') + '</div>'
              : '') +
          '</div></div>';
          return body;
        }).join('') +
      '</details>');
    }
    var meta = [];
    if (Number(card.chunk_count || 0) > 0) {
      meta.push('读了 ' + Number(card.chunk_count) + ' 段');
    }
    if (Number(card.failed_chunks || 0) > 0) {
      meta.push(Number(card.failed_chunks) + ' 段没读成');
    }
    if (card.truncated) {
      meta.push('全书超长，只精读了前一部分');
    }
    if (meta.length) {
      parts.push('<div class="meta">' + esc(meta.join(' · ')) + '</div>');
    }
    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    var actions = actionsRow(card);
    if (actions) {
      parts.push(actions);
    }
    return '<div class="l4-card">' + parts.join('') + '</div>';
  }

  /* 书籍推荐：每本一张问题卡形状的条目（书名/作者 + 与你学习的关系 + 为什么值得读）。

     这些书**不在你的知识库里**，所以顶部固定一条说明——外部推荐与「你的笔记怎么说」
     是两种依据，混在一起用户会以为系统读过这些书。 */
  function bookRecommendHtml(card) {
    var parts = [];
    if (card.overview) {
      parts.push('<div class="meta">' + esc(card.overview) + '</div>');
    }
    if ((card.items || []).some(function (i) { return i.external; })) {
      parts.push('<div class="sg-refs-title">延伸参考（非你的知识库，未入库，仅作线索）</div>');
    }
    parts.push((card.items || []).map(function (item) {
      var body = '<div class="question-card"><div class="q-head">' + esc(item.title) +
        (item.author ? ' <span class="badge">' + esc(item.author) + '</span>' : '') +
        (item.external ? ' <span class="badge">外部</span>' : '') + '</div>';
      if (item.fit) {
        body += '<div class="cf-block"><b>和你现在的关系：</b>' + esc(item.fit) + '</div>';
      }
      if (item.reason) {
        body += '<div class="cf-block"><b>为什么值得读：</b>' + esc(item.reason) + '</div>';
      }
      return body + '</div>';
    }).join(''));
    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    var actions = actionsRow(card);
    if (actions) {
      parts.push(actions);
    }
    return '<div class="brief">' + parts.join('') + '</div>';
  }

  /* ---- 需求确认卡（B 模块）-------------------------------------------------

     一次最多两题，全部是按钮（自由文本题直接让用户在下面输入框说）。
     系统已经推断出来的东西（正在读哪本、读到哪）放在最上面一行，
     用户看着不对可以直接说——**能推断的不要问出口**，这是「最多 3 轮」的前提。 */
  function intakeHtml(card) {
    var parts = ['<div class="intake">'];
    var inf = card.inferred || {};
    var book = inf.book || {};
    if (book.title) {
      var where = book.chapter ? ('，读到' + book.chapter) : '';
      parts.push('<div class="meta">我猜你在读《' + esc(book.title) + '》' + esc(where) + '。不对就直接说。</div>');
    }
    (card.questions || []).forEach(function (q) {
      parts.push('<div class="intake-q">');
      parts.push('<div class="q-label">' + esc(q.label) + '</div>');
      if (q.hint) {
        parts.push('<div class="meta">' + esc(q.hint) + '</div>');
      }
      var opts = q.options || [];
      if (q.kind === 'single' && opts.length) {
        parts.push('<div class="cf-actions">' + opts.map(function (o) {
          return actionButton(card.key, 'intake.answer', q.slot, o.value, o.label);
        }).join('') + '</div>');
      } else {
        parts.push('<div class="meta">直接在下面输入框说就行。</div>');
      }
      parts.push('</div>');
    });
    if (card.note) {
      parts.push('<div class="meta">' + esc(card.note) + '</div>');
    }
    /* 没有题了就不给「跳过」——那时该点的是「生成建议」 */
    if ((card.questions || []).length) {
      parts.push('<div class="cf-actions">' +
        actionButton(card.key, 'intake.skip', '', '', '跳过这轮') +
        '</div>');
    }
    parts.push('</div>');
    parts.push(actionsRow(card));
    return parts.join('');
  }

  /* 依据来源标签：用户的笔记与智能体的通读笔记必须看得出差别 */
  function basisBadge(basis) {
    return basis === 'agent_summary'
      ? '<span class="badge">通读笔记</span>'
      : '<span class="badge">你的笔记</span>';
  }

  function evidenceJump(e) {
    if (!e.jumpable) {
      return '';
    }
    var loc = e.locator || {};
    if (e.source === 'weread' && loc.deep_link) {
      return '<a href="' + esc(loc.deep_link) + '" target="_blank" rel="noopener">看原文</a>';
    }
    if (e.source === 'book' && loc.book_id && loc.char_start !== undefined && loc.char_start !== null) {
      return '<a href="/reader.html?book=' + esc(loc.book_id) + '&char=' + esc(loc.char_start) + '">看原文</a>';
    }
    if (e.item_id) {
      return '<a href="/knowledge.html?item=' + esc(e.item_id) + '">看这条</a>';
    }
    return '';
  }

  /* ---- 建议卡（B 模块）----------------------------------------------------

     三层信息：建议正文（做什么 / 为什么 / 怎么验）、依据（可展开、可跳转）、
     延伸参考（外部、未入库，独立分区）。**依据与外部参考不许混排**——
     外部内容看起来像"你的知识库说的"，是这个功能最容易失信的地方。 */
  function suggestionHtml(card) {
    var parts = ['<div class="suggestion">'];

    if (card.state === 'insufficient_evidence') {
      parts.push('<div class="notice"><div class="n-title">依据不足，先不给建议</div>' +
        esc(card.note || '') + '</div>');
      (card.hints || []).forEach(function (h) {
        parts.push('<div class="meta">· ' + esc(h) + '</div>');
      });
      parts.push('</div>');
      return parts.join('');
    }

    if (card.state === 'degraded') {
      parts.push('<div class="notice"><div class="n-title">这次没生成成功</div>' +
        esc(card.note || '再试一次？') + '</div>');
      parts.push('</div>');
      return parts.join('');
    }

    var g = card.digest || {};
    if (g.question) {
      parts.push('<div class="meta">按你确认的：「' + esc(g.question) + '」</div>');
    }

    var evidenceById = {};
    (card.evidence || []).forEach(function (e) { evidenceById[e.evidence_id] = e; });

    (card.items || []).forEach(function (it) {
      parts.push('<div class="sg-item">');
      parts.push('<div class="sg-action">' + esc(it.action) + '</div>');
      if (it.why) {
        parts.push('<div class="meta">为什么：' + esc(it.why) + '</div>');
      }
      if (it.steps && it.steps.length) {
        parts.push('<ol class="sg-steps">' + it.steps.map(function (s) {
          return '<li>' + esc(s) + '</li>';
        }).join('') + '</ol>');
      }
      if (it.how_to_verify) {
        parts.push('<div class="meta">怎么判断有用：' + esc(it.how_to_verify) + '</div>');
      }
      var refs = (it.basis_item_ids || []).map(function (id) { return evidenceById[id]; })
        .filter(function (e) { return !!e; });
      if (refs.length) {
        parts.push('<details class="sg-evidence"><summary>依据 ' + refs.length + ' 条</summary>' +
          refs.map(function (e) {
            return '<div class="ev-row">' + basisBadge(e.basis) +
              '<strong>' + esc(e.title) + '</strong>' +
              '<span>' + esc(e.quote) + '</span>' + evidenceJump(e) + '</div>';
          }).join('') + '</details>');
      }
      parts.push('<div class="cf-actions">' +
        actionButton(card.key, 'suggestion.reject', it.id, '', '这条不对') + '</div>');
      parts.push('</div>');
    });

    if (card.note) {
      parts.push('<div class="meta">' + esc(card.note) + '</div>');
    }

    var refs = card.references || [];
    if (refs.length) {
      parts.push('<div class="sg-refs">' +
        '<div class="sg-refs-title">延伸参考（非你的知识库，未入库，仅作线索）</div>' +
        refs.map(function (r) {
          return '<div class="ev-row"><span class="badge">外部</span>' +
            '<strong>' + esc(r.title || '') + '</strong>' +
            '<span>' + esc(r.reason || '') + '</span>' +
            '<span class="meta">' + esc(r.external_source || '') + '</span></div>';
        }).join('') + '</div>');
    }

    parts.push('</div>');
    parts.push(actionsRow(card));
    return parts.join('');
  }

  /* ---- 理解度探查卡（E 模块）----------------------------------------------

     四个步骤：接触史 → 判断题 → 复述 → 结论。
     复述那一步没有按钮——直接让用户在下面输入框说，卡片只做提示。
     理由：文本框提交要另开一套交互，而对话本来就有一个输入框；
     用说话的方式还能"说错了再补一句"。 */
  var LEVEL_OPTIONS = [
    { value: 'first', label: '第一次接触' },
    { value: 'once', label: '读过一遍' },
    { value: 'teach', label: '能讲给别人听' }
  ];

  function probeHtml(card) {
    var parts = ['<div class="probe">'];
    if (card.topic) {
      parts.push('<div class="meta">内容：' + esc(card.topic) + '</div>');
    }

    if (card.step === 'level') {
      parts.push('<div class="q-label">这块内容你之前接触过吗？</div>');
      parts.push('<div class="meta">先说明基础，我才知道答错是"没学"还是"没读懂"。</div>');
      parts.push('<div class="cf-actions">' + LEVEL_OPTIONS.map(function (o) {
        return actionButton(card.key, 'probe.level', o.value, '', o.label);
      }).join('') + '</div>');
    } else if (card.step === 'judge') {
      parts.push('<div class="q-label">' + esc(card.question) + '</div>');
      parts.push('<div class="cf-actions">' + (card.options || []).map(function (o) {
        return actionButton(card.key, 'probe.answer', o.key, '', o.key.toUpperCase() + '. ' + o.text);
      }).join('') + '</div>');
    } else if (card.step === 'recall') {
      parts.push('<div class="q-label">用一句话说说它是什么</div>');
      parts.push('<div class="meta">直接在下面输入框说就行——说错没关系，我看的是你抓住了哪几点。</div>');
    } else if (card.step === 'verdict') {
      var v = card.verdict || {};
      parts.push('<div class="q-label">' + esc(v.verdict_label || card.note || '记下了') + '</div>');
      if ((v.covered || []).length) {
        parts.push('<div class="meta">你说到的：</div>');
        parts.push(v.covered.map(function (p) { return '<div class="meta">· ' + esc(p) + '</div>'; }).join(''));
      }
      if ((v.missed || []).length) {
        parts.push('<div class="meta">漏掉的：</div>');
        parts.push(v.missed.map(function (p) { return '<div class="meta">· ' + esc(p) + '</div>'; }).join(''));
      }
      if (typeof v.coverage === 'number' && v.coverage > 0) {
        parts.push('<div class="meta">覆盖要点 ' + Math.round(v.coverage * 100) + '%</div>');
      }
    }

    if (card.note && card.step !== 'verdict') {
      parts.push('<div class="meta">' + esc(card.note) + '</div>');
    }
    parts.push('</div>');
    return parts.join('');
  }

  /* ---- 书籍对比（C 模块）---------------------------------------------------
     表格本身在 assets/compare.js（书架页也要用同一份），这里只补卡片独有的动作按钮。 */
  function compareHtml(card) {
    var body = window.CCACompare ? window.CCACompare.render(card) : '';
    return body + actionsRow(card);
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
    l4_goal: l4GoalHtml,
    l4_plan: l4PlanHtml,
    l4_deviation: l4DeviationHtml,
    l5_diagnosis: l5DiagnosisHtml,
    books: booksHtml,
    book_digest: bookDigestHtml,
    book_recommend: bookRecommendHtml,
    pending: pendingHtml,
    failed: failedHtml,
    knowledge_created: createdHtml,
    note_empty: noteEmptyHtml,
    notice: noticeHtml,
    weread_sync: wereadSyncHtml,
    guide: guideHtml,
    reading_intake: intakeHtml,
    suggestion: suggestionHtml,
    comprehension_probe: probeHtml,
    book_compare: compareHtml
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

  /* 历史侧栏的时间标签：从「刚刚」渐退到日期。输入是 ISO 串，容错非法值。 */
  function relTime(iso) {
    if (!iso) {
      return '';
    }
    var t = new Date(iso).getTime();
    if (isNaN(t)) {
      return '';
    }
    var diff = Date.now() - t;
    if (diff < 0) {
      diff = 0;
    }
    var minute = 60 * 1000;
    var hour = 60 * minute;
    var day = 24 * hour;
    if (diff < minute) {
      return '刚刚';
    }
    if (diff < hour) {
      return Math.floor(diff / minute) + ' 分钟前';
    }
    if (diff < day) {
      return Math.floor(diff / hour) + ' 小时前';
    }
    if (diff < 2 * day) {
      return '昨天';
    }
    var d = new Date(t);
    var mm = d.getMonth() + 1;
    var dd = d.getDate();
    return (mm < 10 ? '0' : '') + mm + '-' + (dd < 10 ? '0' : '') + dd;
  }

  /* 历史侧栏一行。标题与预览都是用户数据，一律过 esc。
     行本身可点（data-history-open），改名/删除是行内小按钮（stopPropagation 由
     事件委托层按 closest 判断先后处理）。 */
  function historyRow(item, activeId) {
    var id = String(item && item.conversation_id || '');
    var title = String(item && item.title || '').trim() || '新会话';
    var preview = String(item && item.preview || '');
    var active = activeId && id === String(activeId) ? ' active' : '';
    return '<div class="item history-item' + active + '" data-history-open data-conversation-id="' + esc(id) + '">' +
      '<div class="row">' +
      '<div class="h-title">' + esc(title) + '</div>' +
      '<div class="history-actions">' +
      '<button type="button" class="h-act" data-history-rename title="重命名">改名</button>' +
      '<button type="button" class="h-act" data-history-delete title="删除会话">删除</button>' +
      '</div>' +
      '</div>' +
      (preview ? '<div class="snippet">' + esc(preview) + '</div>' : '') +
      '<div class="meta">' + esc(relTime(item && item.updated_at)) + '</div>' +
      '</div>';
  }

  return {
    renderCard: renderCard,
    candidatesHtml: candidatesHtml,
    embedBadge: embedBadge,
    pct: pct,
    relTime: relTime,
    historyRow: historyRow
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

  /* 进工作台只调一次 `/start`：新会话由副驾主动开场，已有会话把历史读回来。

     两件事共用一条路径，是因为它们本来就是同一件事的两面——分开写的话，
     「恢复历史」与「插入开场」迟早会对不上（开场重复插、或历史丢卡片）。

     这里原本还有一个 `loadChips()`：拉 /agent/capabilities 渲染一排能力按钮。
     已移除——把「能做什么」摊成一排按钮，等于要用户先学会这个产品的功能分类再点下去，
     那还是「点功能」而不是对话。现在「能做什么」由副驾主动说出来。 */
  async function startConversation() {
    var saved = savedConversation();
    try {
      var data = await CC.api('POST', '/api/v1/agent/start', {
        conversation_id: saved || null
      });
      rememberConversation(data.conversation_id);
      state.messages = data.messages || [];
      state.polls = 0;
      syncMessages();
      return data;
    } catch (err) {
      pushNotice('连不上服务：' + ((err && err.message) || err));
      return null;
    }
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
      loadHistory();  // 首条消息可能刚定下标题，且该会话升到列表顶部
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
    var target = button.getAttribute('data-target') || '';
    var value = button.getAttribute('data-value') || '';
    /* target 允许为空：「跳过这轮」这类操作本来就没有目标对象。
       之前这里要求 !target 也 return，导致跳过按钮点了完全没反应。 */
    if (!cardKey || !action || !state.convId) {
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

  /* ---- 历史侧栏：找回旧对话、切换、改名、删除 ----
     列表行由 CCA.historyRow 纯函数渲染（用户数据在那层已过 esc），
     这里只负责取数、事件委托与状态切换。 */

  var HISTORY_PAGE = 50;
  var historyLoaded = 0;
  var historyTotal = 0;

  async function loadHistory(append) {
    try {
      var offset = append ? historyLoaded : 0;
      var data = await CC.api('GET', '/api/v1/agent/conversations?limit=' + HISTORY_PAGE + '&offset=' + offset);
      var items = data.items || [];
      historyTotal = Number(data.total || 0);
      historyLoaded = append ? historyLoaded + items.length : items.length;
      var box = document.getElementById('history-list');
      if (box) {
        var html = items.map(function (item) {
          return CCA.historyRow(item, state.convId);
        }).join('');
        if (append) {
          box.insertAdjacentHTML('beforeend', html);
        } else {
          box.innerHTML = historyLoaded ? html : '<div class="empty">暂无历史会话</div>';
        }
      }
      var moreWrap = document.getElementById('history-more-wrap');
      if (moreWrap) {
        moreWrap.hidden = historyLoaded >= historyTotal;
      }
    } catch (err) {
      // 侧栏是辅助入口：拉不下来不打断聊天，也不往消息流里刷错误
    }
  }

  function openHistoryDrawer(open) {
    var panel = document.getElementById('history-panel');
    if (panel) {
      panel.classList.toggle('open', !!open);
    }
  }

  function resetConversationView() {
    state.messages = [];
    state.notices = [];
    state.polls = 0;
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    syncMessages();
    setStatus('');
  }

  /* 切换会话：清场后走 startConversation() 同一条恢复路径（不新开恢复函数——
     两条恢复路径迟早会对不上）。已删除的会话在服务端会被静默换成新会话，
     所以对比回包 id 提示用户，别让他对着一段不存在的历史继续说。 */
  async function switchConversation(id) {
    if (state.sending) {
      CC.toast('正在处理，请稍候');
      return;
    }
    if (id && id === state.convId) {
      openHistoryDrawer(false);
      return;
    }
    resetConversationView();
    rememberConversation(id || null);
    openHistoryDrawer(false);
    var data = await startConversation();
    if (data && id && data.conversation_id !== id) {
      CC.toast('该会话已不存在，已开启新会话');
    }
    loadHistory();
    document.getElementById('agent-input').focus();
  }

  async function renameConversation(id, oldTitle) {
    var title = window.prompt('会话标题', oldTitle || '');
    if (title == null) {
      return;
    }
    title = title.trim();
    if (!title) {
      CC.toast('标题不能为空');
      return;
    }
    if (title.length > 128) {
      CC.toast('标题最多 128 个字符');
      return;
    }
    try {
      await CC.api('PATCH', '/api/v1/agent/conversations/' + encodeURIComponent(id), { title: title });
      CC.toast('已重命名');
      loadHistory();
    } catch (err) {
      CC.toast('重命名失败：' + ((err && err.message) || err));
    }
  }

  async function deleteConversation(id) {
    if (!window.confirm('确定删除该会话？（含全部消息记录，不可恢复）')) {
      return;
    }
    try {
      await CC.api('DELETE', '/api/v1/agent/conversations/' + encodeURIComponent(id));
      CC.toast('已删除');
      if (id === state.convId) {
        // 删掉的正是当前会话：落回全新会话，副驾重新开场
        resetConversationView();
        rememberConversation(null);
        await startConversation();
      }
      loadHistory();
    } catch (err) {
      CC.toast('删除失败：' + ((err && err.message) || err));
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
        return;
      }
      // 「再做一件事」类按钮：就当用户自己说了那句话，走完整的对话回合
      var sender = target.closest('[data-agent-send]');
      if (sender) {
        event.preventDefault();
        send(sender.getAttribute('data-agent-send'));
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
      // 新会话同样要副驾先开口，否则用户面对的又是一块空白
      startConversation();
      loadHistory();
      document.getElementById('agent-input').focus();
    });
    // 历史侧栏：行会因 loadHistory 重建，必须事件委托而不是逐行绑定
    var historyList = document.getElementById('history-list');
    if (historyList) {
      historyList.addEventListener('click', function (event) {
        var target = event.target;
        if (!target || !target.closest) {
          return;
        }
        var del = target.closest('[data-history-delete]');
        if (del) {
          event.preventDefault();
          var delRow = del.closest('[data-conversation-id]');
          if (delRow) {
            deleteConversation(delRow.getAttribute('data-conversation-id'));
          }
          return;
        }
        var ren = target.closest('[data-history-rename]');
        if (ren) {
          event.preventDefault();
          var renRow = ren.closest('[data-conversation-id]');
          if (renRow) {
            var titleEl = renRow.querySelector('.h-title');
            renameConversation(
              renRow.getAttribute('data-conversation-id'),
              titleEl ? titleEl.textContent : ''
            );
          }
          return;
        }
        var open = target.closest('[data-history-open]');
        if (open) {
          event.preventDefault();
          switchConversation(open.getAttribute('data-conversation-id'));
        }
      });
    }
    var btnHistory = document.getElementById('btn-history');
    if (btnHistory) {
      btnHistory.addEventListener('click', function () {
        var panel = document.getElementById('history-panel');
        if (panel) {
          openHistoryDrawer(!panel.classList.contains('open'));
        }
      });
    }
    var btnHistoryClose = document.getElementById('btn-history-close');
    if (btnHistoryClose) {
      btnHistoryClose.addEventListener('click', function () {
        openHistoryDrawer(false);
      });
    }
    var btnHistoryMore = document.getElementById('btn-history-more');
    if (btnHistoryMore) {
      btnHistoryMore.addEventListener('click', function () {
        loadHistory(true);
      });
    }
  }

  (async function boot() {
    if (!(await CC.requireAuth())) {
      return;
    }
    bind();
    startConversation();   // 开场 or 恢复历史（含会话已失效时静默开新会话）
    loadHistory();
    document.getElementById('agent-input').focus();
  })();
})();
