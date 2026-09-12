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
