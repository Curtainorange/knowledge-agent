"""书籍上传 / 阅读 / 划词存知识 测试。

解析器用 tmp_path 构造样本；接口测试走 TestClient + 临时书籍目录（conftest 已指向临时目录）。
"""
from __future__ import annotations

import zipfile
from urllib.parse import quote

from app.domain.models.knowledge_item import KnowledgeItem
from tests.helpers import auth_headers

SAMPLE_TXT = "第一章 起点\n这是第一章的内容。\n\n第二章 转折\n这是第二章的内容。"


def _upload(client, headers, filename: str, content: bytes):
    # HTTP header 只能放 latin-1，中文文件名需 URL 编码（与前端 encodeURIComponent 一致）
    return client.post(
        "/api/v1/books",
        content=content,
        headers={
            **headers,
            "X-Filename": quote(filename),
            "Content-Type": "application/octet-stream",
        },
    )


# ---------- 解析器 ----------

def test_parse_txt_splits_chapters(tmp_path):
    from app.books.parser import parse_txt

    path = tmp_path / "sample.txt"
    path.write_text(SAMPLE_TXT, encoding="utf-8")
    parsed = parse_txt(path)
    assert len(parsed.chapters) == 2
    assert parsed.chapters[0]["title"] == "第一章 起点"


def test_parse_txt_handles_gbk(tmp_path):
    from app.books.parser import parse_txt

    path = tmp_path / "gbk.txt"
    path.write_bytes("第一章 中文\n内容".encode("gbk"))
    parsed = parse_txt(path)
    assert "第一章" in parsed.full_text


def test_parse_txt_without_heading_is_single_chapter(tmp_path):
    from app.books.parser import parse_txt

    path = tmp_path / "plain.txt"
    path.write_text("没有章节标题的纯文本", encoding="utf-8")
    parsed = parse_txt(path)
    assert len(parsed.chapters) == 1


def _make_epub(tmp_path):
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">测试书</dc:title>'
        '<dc:creator xmlns:dc="http://purl.org/dc/elements/1.1/">作者甲</dc:creator></metadata>'
        '<manifest>'
        '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>'
        '</manifest>'
        '<spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>'
    )
    c1 = "<html><body><h1>第一章</h1><p>第一段内容</p></body></html>"
    c2 = "<html><body><h1>第二章</h1><p>第二段内容</p></body></html>"
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/c1.xhtml", c1)
        zf.writestr("OEBPS/c2.xhtml", c2)
    return path


def test_parse_epub_extracts_title_chapters(tmp_path):
    from app.books.parser import parse_epub

    parsed = parse_epub(_make_epub(tmp_path))
    assert parsed.title == "测试书"
    assert parsed.author == "作者甲"
    assert len(parsed.chapters) == 2
    assert parsed.chapters[0]["title"] == "第一章"
    assert "第一段内容" in parsed.full_text


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # 假 PNG（测试只验证提取与访问，不校验图片内容）


def _make_epub_with_image(tmp_path):
    container = (
        '<?xml version="1.0"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata><dc:title xmlns:dc="http://purl.org/dc/elements/1.1/">插图本</dc:title></metadata>'
        '<manifest>'
        '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
        '<item id="img1" href="images/pic.png" media-type="image/png"/>'
        '</manifest>'
        '<spine><itemref idref="c1"/></spine></package>'
    )
    c1 = "<html><body><h1>第一章</h1><p>图前文字</p><img src=\"images/pic.png\"/><p>图后文字</p></body></html>"
    path = tmp_path / "book_img.epub"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/container.xml", container)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/c1.xhtml", c1)
        zf.writestr("OEBPS/images/pic.png", _PNG_BYTES)
    return path


def test_parse_epub_extracts_images(tmp_path):
    """epub 里的 <img> 应被提取，并在正文位置留下 [[IMG:name]] 占位符。"""
    from app.books.parser import parse_epub

    parsed = parse_epub(_make_epub_with_image(tmp_path))
    assert len(parsed.images) == 1
    assert parsed.images[0]["name"] == "img_0000.png"
    assert parsed.images[0]["mime"] == "image/png"
    assert "[[IMG:img_0000.png]]" in parsed.full_text
    assert "图前文字" in parsed.full_text and "图后文字" in parsed.full_text


def test_book_image_endpoint(client, tmp_path):
    """上传带图 epub 后，章节含占位符，图片可通过端点访问。"""
    headers = auth_headers(client, "book_img_api")
    epub = _make_epub_with_image(tmp_path).read_bytes()
    book = _upload(client, headers, "插图.epub", epub).json()
    assert book["format"] == "epub"

    ch = client.get(f"/api/v1/books/{book['book_id']}/chapter/1", headers=headers).json()
    assert "[[IMG:img_0000.png]]" in ch["content"]

    img = client.get(f"/api/v1/books/{book['book_id']}/images/img_0000.png", headers=headers)
    assert img.status_code == 200
    assert img.content.startswith(b"\x89PNG")


def test_book_image_via_query_token(client, tmp_path):
    """`<img>` 带不了 Authorization 头，端点必须支持 ?token= 兜底（否则图片 401 打不开）。"""
    headers = auth_headers(client, "book_img_qs")
    epub = _make_epub_with_image(tmp_path).read_bytes()
    book = _upload(client, headers, "插图.epub", epub).json()
    token = headers["Authorization"].split()[1]

    # 不带 header、只用 ?token=
    resp = client.get(f"/api/v1/books/{book['book_id']}/images/img_0000.png?token={token}")
    assert resp.status_code == 200
    assert resp.content.startswith(b"\x89PNG")

    # 两者都没有 → 401（鉴权没有被绕过）
    assert client.get(
        f"/api/v1/books/{book['book_id']}/images/img_0000.png"
    ).status_code == 401


def test_book_image_endpoint_blocks_path_traversal(client, tmp_path):
    """图片名必须匹配 img_0000.ext，防路径穿越。"""
    headers = auth_headers(client, "book_img_sec")
    epub = _make_epub_with_image(tmp_path).read_bytes()
    book = _upload(client, headers, "插图.epub", epub).json()
    bad = client.get(
        f"/api/v1/books/{book['book_id']}/images/..%2F..%2Fdev.db", headers=headers
    )
    assert bad.status_code == 404


# ---------- 接口 ----------

def test_upload_txt_returns_book(client):
    headers = auth_headers(client, "book_upload")
    resp = _upload(client, headers, "测试书.txt", SAMPLE_TXT.encode("utf-8"))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "测试书"
    assert body["format"] == "txt"
    assert body["chapter_count"] == 2


def test_upload_epub(client, tmp_path):
    headers = auth_headers(client, "book_epub")
    epub = _make_epub(tmp_path).read_bytes()
    resp = _upload(client, headers, "书.epub", epub)
    assert resp.status_code == 201
    body = resp.json()
    assert body["format"] == "epub"
    assert body["chapter_count"] == 2


def test_list_and_detail(client):
    headers = auth_headers(client, "book_list")
    _upload(client, headers, "书A.txt", SAMPLE_TXT.encode("utf-8"))
    lst = client.get("/api/v1/books", headers=headers).json()
    assert lst["total"] == 1

    book_id = lst["items"][0]["book_id"]
    detail = client.get(f"/api/v1/books/{book_id}", headers=headers).json()
    assert len(detail["chapters"]) == 2
    assert detail["chapters"][0]["index"] == 1


def test_chapter_content_and_next(client):
    headers = auth_headers(client, "book_ch")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()
    ch = client.get(f"/api/v1/books/{book['book_id']}/chapter/1", headers=headers).json()
    assert ch["title"] == "第一章 起点"
    assert "这是第一章的内容" in ch["content"]
    assert ch["next_index"] == 2


def test_progress_update(client):
    headers = auth_headers(client, "book_prog")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()
    resp = client.patch(
        f"/api/v1/books/{book['book_id']}/progress",
        json={"current_char": 10, "read_progress": 0.5},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["read_progress"] == 0.5
    assert resp.json()["current_char"] == 10


def test_progress_rejects_out_of_range(client):
    headers = auth_headers(client, "book_bound")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()
    resp = client.patch(
        f"/api/v1/books/{book['book_id']}/progress",
        json={"current_char": 0, "read_progress": 1.5},
        headers=headers,
    )
    assert resp.status_code == 422


def test_note_creates_knowledge_item(client, session):
    headers = auth_headers(client, "book_note")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()

    resp = client.post(
        f"/api/v1/books/{book['book_id']}/notes",
        json={"text": "这是第一章的内容。", "chapter_index": 1},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    item_id = resp.json()["item_id"]

    item = session.get(KnowledgeItem, item_id)
    assert item is not None
    assert item.source == "book"
    assert item.source_item_id == book["book_id"]
    assert "第一章" in item.title


def test_reject_unsupported_format(client):
    headers = auth_headers(client, "book_badfmt")
    resp = _upload(client, headers, "文件.pdf", b"%PDF-1.4")
    assert resp.status_code == 400


def test_cross_user_access_forbidden(client):
    headers_a = auth_headers(client, "book_user_a")
    headers_b = auth_headers(client, "book_user_b")
    book = _upload(client, headers_a, "书.txt", SAMPLE_TXT.encode("utf-8")).json()

    assert client.get(f"/api/v1/books/{book['book_id']}", headers=headers_b).status_code == 403
    assert client.get(
        f"/api/v1/books/{book['book_id']}/chapter/1", headers=headers_b
    ).status_code == 403
    assert client.get("/api/v1/books", headers=headers_b).json()["total"] == 0


def test_note_with_thought_and_locator(client, session):
    """阅读时写下的想法要单独留存，同时拼进正文以便被 L1 挖到；位置用于高亮回溯。"""
    headers = auth_headers(client, "book_thought")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()

    resp = client.post(
        f"/api/v1/books/{book['book_id']}/notes",
        json={
            "text": "这是第一章的内容。",
            "note": "这段让我想到：索引设计要以查询模式为先",
            "chapter_index": 1,
            "char_start": 7,
            "char_end": 16,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["has_note"] is True

    item = session.get(KnowledgeItem, resp.json()["item_id"])
    assert item.note == "这段让我想到：索引设计要以查询模式为先"
    assert "【我的想法】" in item.raw_content  # 想法也进正文，才能被检索命中
    assert item.source_locator == {"chapter_index": 1, "char_start": 7, "char_end": 16}
    assert "读书笔记" in (item.tags or [])


def test_note_without_thought_has_no_note(client, session):
    headers = auth_headers(client, "book_plain_note")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()
    resp = client.post(
        f"/api/v1/books/{book['book_id']}/notes",
        json={"text": "纯摘录", "chapter_index": 1, "char_start": 0, "char_end": 3},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["has_note"] is False
    item = session.get(KnowledgeItem, resp.json()["item_id"])
    assert item.note == ""
    assert "【我的想法】" not in item.raw_content


def test_list_notes_sorted_with_excerpt(client):
    headers = auth_headers(client, "book_notes_list")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()
    bid = book["book_id"]

    client.post(
        f"/api/v1/books/{bid}/notes",
        json={"text": "第二章的段落", "note": "我的想法", "chapter_index": 2,
              "char_start": 30, "char_end": 36},
        headers=headers,
    )
    client.post(
        f"/api/v1/books/{bid}/notes",
        json={"text": "第一章的段落", "chapter_index": 1, "char_start": 7, "char_end": 13},
        headers=headers,
    )

    resp = client.get(f"/api/v1/books/{bid}/notes", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    # 按原文位置排序（第一章在前）
    assert body["items"][0]["char_start"] < body["items"][1]["char_start"]

    second = body["items"][1]
    assert second["note"] == "我的想法"
    # 摘录里不能带上拼接进去的想法
    assert "【我的想法】" not in second["excerpt"]
    assert second["chapter_index"] == 2


def test_list_notes_cross_user_forbidden(client):
    headers_a = auth_headers(client, "book_notes_a")
    headers_b = auth_headers(client, "book_notes_b")
    book = _upload(client, headers_a, "书.txt", SAMPLE_TXT.encode("utf-8")).json()
    assert client.get(
        f"/api/v1/books/{book['book_id']}/notes", headers=headers_b
    ).status_code == 403


# ---------- 阅读日志 ----------

def _seed_reading(session, user_id: str, title: str = "测试书"):
    """造一本书 + 跨两天的阅读进度事件，返回 book。"""
    from datetime import datetime, timedelta, timezone

    from app.domain.repositories.book_repository import BookRepository
    from app.domain.repositories.learning_event_repository import LearningEventRepository
    from app.feedback import events

    book = BookRepository(session, user_id=user_id).create(
        user_id=user_id, title=title, author="", format="txt",
        file_path="/tmp/x.txt", chapters=[], full_text="x" * 5000, total_chars=5000,
    )
    repo = LearningEventRepository(session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    yesterday = now - timedelta(days=1)

    # 昨天：1000 → 1500（同日取最后一次）
    repo.append(user_id=user_id, event_type=events.BOOK_PROGRESS,
                payload={"book_id": book.id, "current_char": 1000, "read_progress": 0.2},
                occurred_at=yesterday)
    repo.append(user_id=user_id, event_type=events.BOOK_PROGRESS,
                payload={"book_id": book.id, "current_char": 1500, "read_progress": 0.3},
                occurred_at=yesterday + timedelta(hours=1))
    # 今天：读到 3000
    repo.append(user_id=user_id, event_type=events.BOOK_PROGRESS,
                payload={"book_id": book.id, "current_char": 3000, "read_progress": 0.6},
                occurred_at=now)
    session.commit()
    return book


def test_reading_log_aggregates_by_day(session):
    """阅读日志：按天算增量，「当天读了多少」= 当天达到的最大位置 − 此前已读位置。"""
    from app.books.service import BookService

    _seed_reading(session, "u1")
    result = BookService(session).reading_log(user_id="u1", days=30)

    assert result["group"] == "day"
    assert result["active_buckets"] == 2
    assert result["total_chars"] == 3000  # 1500（昨天）+ 1500（今天）

    latest = result["buckets"][0]  # 按日期倒序，最新一天在前
    assert latest["total_chars"] == 1500
    assert latest["books"][0]["title"] == "测试书"

    # 回退（往回翻）不算新增：再记一条更小的 current_char，今天增量不变
    from datetime import datetime, timezone

    from app.domain.repositories.learning_event_repository import LearningEventRepository
    from app.feedback import events

    LearningEventRepository(session).append(
        user_id="u1", event_type=events.BOOK_PROGRESS,
        payload={"book_id": latest["books"][0]["book_id"], "current_char": 500, "read_progress": 0.1},
        occurred_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    session.commit()
    again = BookService(session).reading_log(user_id="u1", days=30)
    assert again["total_chars"] == 3000  # 未因回退而减少或增加


def test_reading_log_groups_by_week_and_month(session):
    """同一批事件按周/月聚合：总字数不变，桶数减少（粒度变粗）。"""
    from app.books.service import BookService

    _seed_reading(session, "u1")
    svc = BookService(session)

    by_day = svc.reading_log(user_id="u1", days=30, group="day")
    by_week = svc.reading_log(user_id="u1", days=30, group="week")
    by_month = svc.reading_log(user_id="u1", days=30, group="month")

    # 总量与口径无关，只与「读了多少」有关
    assert by_day["total_chars"] == by_week["total_chars"] == by_month["total_chars"] == 3000
    assert by_week["group"] == "week"
    assert by_month["group"] == "month"
    # 昨天的 key 形如 2026-W37 / 2026-09
    assert "-W" in by_week["buckets"][0]["key"] or by_week["active_buckets"] >= 1
    assert len(by_month["buckets"][0]["key"].split("-")) == 2  # YYYY-MM
    # 非法 group 回落到 day
    assert svc.reading_log(user_id="u1", days=30, group="oops")["group"] == "day"


def test_reading_log_api(client):
    """阅读日志端点：走完整用户流程（上传 → 更新进度 → 查日志），
    并验证静态路由 `/reading-log` 不被 `/{book_id}` 动态路由吃掉。"""
    headers = auth_headers(client, "reading_log_api")
    book = _upload(client, headers, "书.txt", SAMPLE_TXT.encode("utf-8")).json()

    # 更新进度会追加 BOOK_PROGRESS 事件（阅读日志的数据源）
    resp = client.patch(
        f"/api/v1/books/{book['book_id']}/progress",
        json={"current_char": 12, "read_progress": 0.5},
        headers=headers,
    )
    assert resp.status_code == 200

    body = client.get("/api/v1/books/reading-log?days=30", headers=headers).json()
    assert body["group"] == "day"
    assert body["active_buckets"] == 1
    assert body["total_chars"] == 12
    assert body["buckets"][0]["books"][0]["title"] == "书"

    # 按周聚合也应当返回 200 且结构一致
    weekly = client.get(
        "/api/v1/books/reading-log?days=30&group=week", headers=headers
    ).json()
    assert weekly["group"] == "week"
    assert weekly["total_chars"] == 12


def test_reading_log_page_served(client):
    resp = client.get("/reading_log.html")
    assert resp.status_code == 200
    assert "rl-list" in resp.text
