/* 认知副驾 · 书籍对比渲染（C 模块）
 *
 * 为什么单独一个文件：对比结果有两个入口——对话卡片（app.html）与书架选书（books.html）。
 * 两处各写一份表格，样式和字段迟早分叉（"同一条规则实现两遍必然分叉"，这是踩过的坑）。
 * 所以渲染只留这一份，`window.CCACompare.render(card)` 两边共用。
 *
 * 纯函数：不碰 DOM，只把后端 `book_compare_card` 的 dict 变成 HTML。
 */
window.CCACompare = (function () {
  'use strict';

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* 跳回内置阅读器的定位链接：char < 0 表示这本没有章节偏移，不渲染 */
  function readerLink(book, charPos) {
    if (!book || !book.book_id || typeof charPos !== 'number' || charPos < 0) {
      return '';
    }
    return ' <a href="/reader.html?book=' + esc(book.book_id) + '&char=' + charPos + '">看原文</a>';
  }

  function render(card) {
    var a = card.book_a || {};
    var b = card.book_b || {};
    var parts = [];

    if (a.title && b.title) {
      parts.push('<div class="plan-head"><strong>《' + esc(a.title) + '》 ↔ 《' +
        esc(b.title) + '》</strong></div>');
    }
    if (card.overview) {
      parts.push('<div class="meta">' + esc(card.overview) + '</div>');
    }

    var rows = card.rows || [];
    if (rows.length) {
      parts.push('<div class="cmp-table">' + rows.map(function (r) {
        var relClass = r.relation === '冲突' ? 'danger'
          : (r.relation === '一致' ? 'ok' : '');
        return '<div class="cmp-row">' +
          '<div class="cmp-head"><strong>' + esc(r.topic) + '</strong>' +
          '<span class="badge ' + relClass + '">' + esc(r.relation) + '</span></div>' +
          '<div class="cmp-cols">' +
            '<div class="cmp-col"><div class="meta">' + esc(a.title || 'A') + '</div>' +
              '<div>' + esc(r.a_says) + '</div>' + readerLink(a, r.a_char) + '</div>' +
            '<div class="cmp-col"><div class="meta">' + esc(b.title || 'B') + '</div>' +
              '<div>' + esc(r.b_says) + '</div>' + readerLink(b, r.b_char) + '</div>' +
          '</div>' +
          '<div class="cf-block"><b>差异：</b>' + esc(r.difference) + '</div>' +
        '</div>';
      }).join('') + '</div>');
    }

    if ((card.only_a || []).length || (card.only_b || []).length) {
      parts.push('<div class="sg-refs">' +
        '<div class="sg-refs-title">只有一本书讲到的</div>' +
        (card.only_a || []).map(function (t) {
          return '<div class="meta">· ' + esc(a.title || 'A') + '：' + esc(t) + '</div>';
        }).join('') +
        (card.only_b || []).map(function (t) {
          return '<div class="meta">· ' + esc(b.title || 'B') + '：' + esc(t) + '</div>';
        }).join('') +
      '</div>');
    }

    if (card.note) {
      parts.push('<div class="notice">' + esc(card.note) + '</div>');
    }
    return '<div class="l4-card">' + parts.join('') + '</div>';
  }

  return { render: render };
})();
