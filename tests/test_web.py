"""界面托管测试：页面拆分、静态资源、登录页公开、API 不受影响。"""
from __future__ import annotations

from tests.helpers import auth_headers

PAGES = (
    "/", "/index.html", "/login.html", "/knowledge.html", "/mine.html",
    "/conflicts.html", "/brief.html", "/l4.html", "/l5.html", "/notify.html",
)


def test_all_pages_served(client):
    for path in PAGES:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "text/html" in resp.headers["content-type"], path
        assert "认知副驾" in resp.text, path


def test_entry_is_public_landing_page(client):
    """入口是公开首页（可先浏览、点功能才登录），不是登录墙。"""
    resp = client.get("/")
    assert "feature-grid" in resp.text
    assert "开始使用" in resp.text
    assert "/assets/app.js" in resp.text
    assert "auth-submit" not in resp.text  # 登录表单不应出现在首页


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


def test_l4_page_is_isolated(client):
    """L4 路径修正页只做目标/计划/偏离：不含录入 / 登录 / 挖掘 / 冲突 / 简报表单。"""
    body = client.get("/l4.html").text
    assert "goal-form" in body
    assert "dev-check" in body
    assert "k-submit" not in body
    assert "auth-submit" not in body
    assert "mine-send" not in body
    assert "btn-scan" not in body
    assert "brief-generate" not in body


def test_nav_links_to_l4_everywhere(client):
    for path in ("/knowledge.html", "/mine.html", "/books.html", "/conflicts.html", "/brief.html"):
        assert "/l4.html" in client.get(path).text, path


def test_l5_page_is_isolated(client):
    """L5 健康诊断页只做诊断：不含录入 / 登录 / 挖掘 / 冲突 / 简报 / 计划表单。"""
    body = client.get("/l5.html").text
    assert "diag-generate" in body
    assert "diag-view" in body
    assert "k-submit" not in body
    assert "auth-submit" not in body
    assert "mine-send" not in body
    assert "btn-scan" not in body
    assert "brief-generate" not in body
    assert "goal-form" not in body


def test_notify_page_is_isolated(client):
    """通知页只做推送通知与偏好：不含其它能力的表单。"""
    body = client.get("/notify.html").text
    assert "pref-form" in body
    assert "notify-list" in body
    assert "k-submit" not in body
    assert "auth-submit" not in body
    assert "mine-send" not in body
    assert "brief-generate" not in body
    assert "goal-form" not in body


def test_nav_links_to_l5_and_notify_everywhere(client):
    """L5 与通知的后端早已就绪，导航入口必须有落点（缺入口等于没有）。"""
    for path in ("/knowledge.html", "/mine.html", "/books.html", "/conflicts.html", "/brief.html"):
        body = client.get(path).text
        assert "/l5.html" in body, path
        assert "/notify.html" in body, path


def test_api_routes_unaffected(client):
    assert client.get("/health").status_code == 200
    headers = auth_headers(client, "web_user")
    assert client.get("/api/v1/knowledge/items", headers=headers).status_code == 200
