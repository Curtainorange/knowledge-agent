"""资源目录测试：书名归一化、匹配排序、以及「目录文件不存在」时的优雅退化。

为什么要这一层：`app/agent/book_finder.py` 能联网把「只记得情节」定位成书名，
但它给出的是**一个名字**，用户还得自己去搜资源。把 2.4 万条网盘书目做成离线目录后，
「找到书名 → 立刻告诉他在哪拿」才闭环。这一层是纯函数 + 一次 JSON 载入，
不需要数据库、不出网、不花 token——所以它必须可以被单独测死。

三条断言的口径值得单独说：

- **格式必须单独暴露**（`has_epub`），不只是「有没有资源」。上传白名单只认
  `.txt / .epub / .pdf`，而这份目录给的是 epub / mobi / azw3 三种。
  对 mobi 说「有资源」等于骗用户去下一份进不了阅读器的文件。
- **同名书靠作者分**。目录里重名一堆（「心理学」「经济学」这类通名尤其多），
  只按书名取第一条会稳定地取错。
- **目录缺失不能抛**。`data/` 是 git-ignored，干净检出、换机器时这个文件根本不存在。
  那时应该退化成「查不到资源」，而不是让整次找书 500。
"""
from __future__ import annotations

import json

from app.books.catalog import (
    ResourceEntry,
    browse,
    load,
    normalize,
    reset_cache,
    search,
)

# ---- 归一化 -----------------------------------------------------------------


def test_normalize_strips_book_marks_and_spaces():
    assert normalize("《刻意练习》") == normalize("刻意练习")


def test_normalize_strips_separators_and_punctuation():
    """副标题里的冒号、间隔号、括号都不该参与比对。"""
    assert normalize("人类简史：从动物到上帝") == normalize("人类简史 从动物到上帝")
    assert normalize("爱因斯坦·传") == normalize("爱因斯坦传")


def test_normalize_folds_fullwidth_and_case():
    assert normalize("ＰＹＴＨＯＮ编程") == normalize("python编程")


# ---- 匹配 -------------------------------------------------------------------


def _entry(title: str, author: str = "", formats=("epub", "mobi"), link: str = "") -> ResourceEntry:
    return ResourceEntry(
        title=title,
        author=author,
        link=link or f"https://example.com/{title}",
        categories=("测试",),
        formats=tuple(formats),
    )


def test_exact_title_ranks_before_contains():
    entries = [
        _entry("刻意练习：如何从新手到大师"),
        _entry("刻意练习"),
    ]
    hits = search(entries, "刻意练习")
    assert hits[0].title == "刻意练习"


def test_contains_match_resolves_subtitle():
    """用户只会说主标题，目录里带副标题，这是最常见的一对。"""
    entries = [_entry("人类简史：从动物到上帝", "尤瓦尔·赫拉利")]
    hits = search(entries, "人类简史")
    assert len(hits) == 1
    assert hits[0].author == "尤瓦尔·赫拉利"


def test_title_starting_with_query_beats_midword_contains():
    """真机实测踩到的：查「刻意练习」，第一条给的是「天才源自刻意练习」。

    两条都算「包含」，但书名**以查询开头**的才是用户要的那本，
    书名中间夹着关键词的是相关书，不是目标书。
    """
    entries = [
        _entry("天才源自刻意练习", "杰夫・科尔文"),
        _entry("刻意练习：如何从新手到大师", "安德斯・艾利克森"),
    ]
    hits = search(entries, "刻意练习")
    assert hits[0].title == "刻意练习：如何从新手到大师"


def test_fuzzy_similarity_never_outranks_prefix_match():
    """查「人类简史」，不该让「人类酷刑简史」压过「人类简史（知识漫画）」。

    前者只是字符相近（差了两个字），后者是真正以它开头的版本。
    相似度只配用来兜底，不配和子串命中同档。
    """
    entries = [
        _entry("人类酷刑简史", "马克·P.唐纳利"),
        _entry("人类简史（知识漫画）", "尤瓦尔・赫拉利"),
    ]
    hits = search(entries, "人类简史")
    assert hits[0].title == "人类简史（知识漫画）"


def test_author_disambiguates_same_title():
    entries = [
        _entry("心理学", "张三"),
        _entry("心理学", "李四"),
    ]
    hits = search(entries, "心理学", author="李四")
    assert hits[0].author == "李四"


def test_epub_wins_when_score_ties():
    """同分时优先进得了阅读器的那本——mobi 对当前上传白名单是无用信息。"""
    entries = [
        _entry("深度工作", "卡尔·纽波特", formats=("mobi", "azw3")),
        _entry("深度工作", "卡尔·纽波特", formats=("epub",)),
    ]
    hits = search(entries, "深度工作")
    assert hits[0].has_epub is True


def test_has_epub_is_case_insensitive():
    assert _entry("X", formats=("EPUB",)).has_epub is True
    assert _entry("X", formats=("mobi", "azw3")).has_epub is False


def test_unrelated_query_returns_nothing():
    """宁可空手，也不要糊一条「大概是这本」——和 book_finder 同一条纪律。"""
    entries = [_entry("人类简史")]
    assert search(entries, "线性代数") == []


def test_limit_is_respected():
    entries = [_entry("同名书", f"作者{i}") for i in range(5)]
    assert len(search(entries, "同名书", limit=2)) == 2


def test_empty_query_returns_nothing():
    assert search([_entry("人类简史")], "   ") == []


# ---- 载入 -------------------------------------------------------------------


def test_load_missing_file_degrades_to_empty(tmp_path):
    """`data/` 是 git-ignored：干净检出时文件不存在是常态，不是错误。"""
    reset_cache()
    assert load(tmp_path / "nope.json") == []


def test_load_tolerates_broken_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{ 这不是 json", encoding="utf-8")
    assert load(bad) == []


def test_load_skips_entries_without_link(tmp_path):
    """原始数据里有 69 条字面量「链接未找到」，它们不该进目录。"""
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            [
                {"title": "有链接", "author": "A", "link": "https://x/1", "category": "测试", "formats": ["epub"]},
                {"title": "没链接", "author": "B", "link": "链接未找到", "category": "测试", "formats": ["epub"]},
                {"title": "空链接", "author": "C", "link": "", "category": "测试", "formats": ["epub"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    titles = [e.title for e in load(path)]
    assert titles == ["有链接"]


def test_lookup_goes_through_load(tmp_path):
    """端到端一条：从磁盘 JSON 到可用的检索结果。"""
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            [{"title": "深度工作", "author": "卡尔·纽波特", "link": "https://x/dw", "category": "效率", "formats": ["epub"]}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reset_cache()
    hits = load(path)
    assert hits and hits[0].title == "深度工作"


# ---- 多标签 -----------------------------------------------------------------


def test_load_merges_rows_sharing_a_link_and_keeps_every_tag(tmp_path):
    """同一本书被多个标签收录是**正常**的，不是脏数据。

    原仓库的 `md/` 是按标签组织的，一本《思考，快与慢》既在「认知」也在「心理学」，
    导出成全量索引时就变成两行。正确处理是**合并成一条、标签全留**——
    按「书名+作者+链接」去重会把除第一个以外的标签静默删掉，实测每本书平均丢 1.2 个标签。
    """
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            [
                {"title": "思考，快与慢", "author": "卡尼曼", "link": "https://x/1", "category": "认知", "formats": ["epub"]},
                {"title": "思考，快与慢", "author": "卡尼曼", "link": "https://x/1", "category": "心理学", "formats": ["epub"]},
                {"title": "思考，快与慢", "author": "卡尼曼", "link": "https://x/1", "category": "心理学", "formats": ["mobi"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reset_cache()
    entries = load(path)

    assert len(entries) == 1
    assert entries[0].categories == ("认知", "心理学")   # 去重、保序，不丢
    assert set(entries[0].formats) == {"epub", "mobi"}


def test_category_property_is_the_first_tag():
    """旧调用方（book_finder / 卡片）只认一个标签，给它主标签即可。"""
    assert ResourceEntry(title="X", categories=("认知", "心理学")).category == "认知"
    assert ResourceEntry(title="Y").category == ""


def test_load_accepts_categories_array_from_new_format(tmp_path):
    """导入脚本产出的新格式是 `categories` 数组；老格式是 `category` 字符串。两种都要认。"""
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            [{"title": "人类简史", "link": "https://x/2", "categories": ["历史", "科普"], "formats": ["epub"]}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reset_cache()
    entries = load(path)
    assert entries[0].categories == ("历史", "科普")


# ---- 按标签找书 -------------------------------------------------------------


def _tagged(title: str, *tags: str) -> ResourceEntry:
    return ResourceEntry(
        title=title,
        author="某作者",
        link=f"https://example.com/{title}",
        categories=tuple(tags),
        formats=("epub",),
    )


def test_browse_by_single_tag():
    """「找本心理学的书」——没有书名，只能按标签挑。"""
    entries = [
        _tagged("思考，快与慢", "思维", "心理学", "决策"),
        _tagged("深度工作", "效率", "方法"),
        _tagged("被讨厌的勇气", "心理学", "成长"),
    ]
    hits = browse(entries, tags=["心理学"])
    assert [h.title for h in hits] == ["思考，快与慢", "被讨厌的勇气"]


def test_browse_ranks_more_matched_tags_first():
    """命中两个标签的比命中一个的更贴题。"""
    entries = [
        _tagged("深度工作", "效率"),
        _tagged("思考，快与慢", "心理", "心理学"),
    ]
    hits = browse(entries, tags=["心理", "心理学"])
    assert hits[0].title == "思考，快与慢"


def test_browse_matches_tag_partially():
    """用户说「心理」，目录里是「心理学」——互相包含就算命中。"""
    entries = [_tagged("被讨厌的勇气", "心理学")]
    assert browse(entries, tags=["心理"])[0].title == "被讨厌的勇气"


def test_browse_without_match_returns_nothing():
    entries = [_tagged("三体", "科幻")]
    assert browse(entries, tags=["菜谱"]) == []


def test_browse_requires_at_least_one_tag():
    """没给标签就不该把全部书目倒出来。"""
    entries = [_tagged("三体", "科幻")]
    assert browse(entries, tags=[]) == []
    assert browse(entries, tags=["  "]) == []


def test_search_prefers_the_book_whose_tags_match():
    """书名一样时（重名书很常见），用户给的标签决定取哪一条。"""
    entries = [
        ResourceEntry(title="心理学", author="张三", link="https://x/a", categories=("教材",), formats=("epub",)),
        ResourceEntry(title="心理学", author="李四", link="https://x/b", categories=("入门",), formats=("epub",)),
    ]
    hits = search(entries, "心理学", tags=["入门"])
    assert hits[0].author == "李四"


def test_search_with_tags_does_not_drop_title_matches():
    """标签只加分、不做过过滤：用户给的标签可能不在目录词表里，过滤会把唯一的正确结果杀掉。"""
    entries = [_tagged("人类简史", "历史", "科普")]
    hits = search(entries, "人类简史", tags=["社科"])
    assert [h.title for h in hits] == ["人类简史"]
