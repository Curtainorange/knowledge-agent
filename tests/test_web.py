"""界面托管测试：页面拆分、静态资源、登录页公开、API 不受影响。"""
from __future__ import annotations

from tests.helpers import auth_headers

PAGES = ("/", "/login.html", "/knowledge.html", "/mine.html", "/conflicts.html", "/brief.html")


def test_all_pages_served(client):
    for path in PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "text/html" in resp.headers["content-type"], path
        assert "认知副驾" in resp.text, path


def test_entry_is_public_login_page(client):
    """入口即登录页，且必须免鉴权——否则用户无法进入登录流程。"""
    resp = client.get("/")
    assert "auth-submit" in resp.text
    assert "/assets/app.js" in resp.text


def test_login_and_knowledge_are_separate_pages(client):
    """登录与知识录入已拆到不同页面：登录页不该有录入表单，知识库页不该有登录表单。"""
    login_body = client.get("/login.html").text
    assert "auth-submit" in login_body
    assert "k-submit" not in login_body

    knowledge_body = client.get("/knowledge.html").text
    assert "k-submit" in knowledge_body
    assert "auth-submit" not in knowledge_body


def test_mine_page_is_isolated(client):
    """L1 挖掘页只做挖掘：不含知识录入，也不含登录表单。"""
    body = client.get("/mine.html").text
    assert "mine-send" in body
    assert "k-submit" not in body
    assert "auth-submit" not in body


def test_shared_assets_served(client):
    css = client.get("/assets/style.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]

    js = client.get("/assets/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]


def test_conflicts_page_is_isolated(client):
    """L2 冲突检测页只做冲突处理：不含录入 / 登录 / 挖掘表单。"""
    body = client.get("/conflicts.html").text
    assert "btn-scan" in body
    assert "cf-list" in body
    assert "k-submit" not in body
    assert "auth-submit" not in body
    assert "mine-send" not in body


def test_nav_links_to_conflicts_everywhere(client):
    """业务页导航都要能进冲突检测页（否则新能力不可达）。"""
    for path in ("/knowledge.html", "/mine.html", "/books.html"):
        assert "/conflicts.html" in client.get(path).text, path


def test_account_settings_present(client):
    """改密 / 注销的入口要在界面上有落点（后端已就绪，缺入口等于没有）。"""
    body = client.get("/knowledge.html").text
    assert "pw-submit" in body
    assert "del-submit" in body


def test_brief_page_is_isolated(client):
    """L3 认知简报页只做简报：不含录入 / 登录 / 挖掘 / 冲突表单。"""
    body = client.get("/brief.html").text
    assert "brief-generate" in body
    assert "brief-questions" in body
    assert "k-submit" not in body
    assert "auth-submit" not in body
    assert "mine-send" not in body
    assert "btn-scan" not in body


def test_nav_links_to_brief_everywhere(client):
    for path in ("/knowledge.html", "/mine.html", "/books.html", "/conflicts.html"):
        assert "/brief.html" in client.get(path).text, path


def test_api_routes_unaffected(client):
    assert client.get("/health").status_code == 200
    headers = auth_headers(client, "web_user")
    assert client.get("/api/v1/knowledge/items", headers=headers).status_code == 200
