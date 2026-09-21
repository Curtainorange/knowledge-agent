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
    l3_brief: l3BriefHtml,
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

  var state = { convId: null };

  function scrollLog() {
    log.scrollTop = log.scrollHeight;
  }

  function append(kind, text, card) {
    var cardHtml = CCA.renderCard(card);
    var div = document.createElement('div');
    div.className = 'msg ' + kind + (cardHtml ? ' has-card' : '');
    var bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text || '';
    div.appendChild(bubble);
    if (cardHtml) {
      var slot = document.createElement('div');
      slot.className = 'card-slot';
      slot.innerHTML = cardHtml;
      div.appendChild(slot);
    }
    log.appendChild(div);
    scrollLog();
  }

  function pushUser(text) {
    append('user', text, null);
  }

  function pushBot(text, card) {
    append('bot', text, card);
  }

  function pushSys(text) {
    append('sys', text, null);
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

  /* 发送中的互斥：一次只允许一轮在飞，否则双击会并发两条请求、
     会话消息按返回顺序交错写乱 */
  var sending = false;

  async function send(text) {
    var input = document.getElementById('agent-input');
    var message = String(text == null ? input.value : text).trim();
    if (!message || sending) {
      return;
    }
    input.value = '';
    pushUser(message);
    setStatus('处理中…');
    sending = true;
    try {
      var result = await CC.api('POST', '/api/v1/agent/chat', {
        message: message,
        conversation_id: state.convId
      });
      state.convId = result.conversation_id;
      pushBot(result.reply, result.card);
      setStatus('');
    } catch (err) {
      pushSys('出错了：' + ((err && err.message) || err));
      setStatus('');
    } finally {
      sending = false;
    }
    input.focus();
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
    document.getElementById('btn-new').addEventListener('click', function () {
      state.convId = null;
      log.innerHTML = '';
      setStatus('');
      CC.toast('已开启新会话');
      document.getElementById('agent-input').focus();
    });
  }

  (async function boot() {
    if (!(await CC.requireAuth())) {
      return;
    }
    bind();
    loadChips();
    document.getElementById('agent-input').focus();
  })();
})();
