"""界面托管测试：页面拆分、静态资源、登录页公开、API 不受影响。"""
from __future__ import annotations

from tests.helpers import auth_headers

PAGES = (
    "/", "/index.html", "/login.html", "/app.html", "/knowledge.html", "/mine.html",
    "/conflicts.html", "/brief.html", "/l4.html", "/l5.html", "/notify.html",
    "/books.html", "/reader.html", "/reading_log.html",
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


def test_static_assets_are_revalidated(client):
    """前端静态资源必须「先校验再使用」，否则用户会拿着旧版 JS 跑新后端——
    表现为「BUG 修了却没生效」。本项目已为此误判多次。"""
    for path in (
        "/knowledge.html", "/assets/app.js", "/assets/agent.js", "/assets/style.css",
        "/sw.js", "/manifest.webmanifest",
    ):
        resp = client.get(path)
        assert resp.headers.get("cache-control") == "no-cache", path
    # 接口响应不该被加上这个头（它们是动态内容，缓存策略另说）
    assert client.get("/health").headers.get("cache-control") != "no-cache"


def test_workbench_page_is_conversational_only(client):
    """对话工作台只做对话：消息流 + 输入框，不内嵌任何能力的表单，
    也不摆一排「功能按钮」——能力识别由分流层负责，用户只管说话。"""
    body = client.get("/app.html").text
    assert "agent-log" in body            # 消息流
    assert "agent-input" in body          # 输入框
    assert "agent-send" in body
    assert "agent-chips" not in body      # 能力快捷入口已移除（见 agent.js 的说明）
    assert "/assets/agent.js" in body
    # 别的能力各自的表单不该出现在这里（否则又变成「一页堆所有功能」）
    assert "k-submit" not in body
    assert "auth-submit" not in body
    assert "btn-scan" not in body
    assert "brief-generate" not in body


def test_workbench_is_the_landing_after_login(client):
    """登录后落到对话工作台，而不是散落的功能页。"""
    assert "/app.html" in client.get("/login.html").text


def test_every_feature_page_links_back_to_workbench(client):
    """改造后原页面全部保留，但每一个都要能回到对话入口——否则越走越散。"""
    for path in (
        "/knowledge.html", "/mine.html", "/books.html", "/reading_log.html",
        "/conflicts.html", "/brief.html", "/l4.html", "/l5.html", "/notify.html",
    ):
        assert "/app.html" in client.get(path).text, path


def test_landing_offers_the_workbench(client):
    assert "/app.html" in client.get("/").text


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


def test_notify_badges_cover_push_types(client):
    """徽章文案映射必须覆盖实际会推送的 push_type——缺映射会把英文代号直接亮给用户。"""
    body = client.get("/notify.html").text
    for fragment in ("conflict:", "question:", "brief:", "diagnosis:", "coach:"):
        assert fragment in body, fragment


def test_nav_links_to_l5_and_notify_everywhere(client):
    """L5 与通知的后端早已就绪，导航入口必须有落点（缺入口等于没有）。"""
    for path in ("/knowledge.html", "/mine.html", "/books.html", "/conflicts.html", "/brief.html"):
        body = client.get(path).text
        assert "/l5.html" in body, path
        assert "/notify.html" in body, path


# ---------- 移动端骨架（阶段 1 · M1）----------

NAV_PAGES = (
    "/app.html", "/knowledge.html", "/mine.html", "/books.html", "/reading_log.html",
    "/conflicts.html", "/brief.html", "/l4.html", "/l5.html", "/notify.html",
)


def test_all_pages_declare_viewport(client):
    """手机适配的前提：每页都声明 viewport（缺它移动端按桌面宽度渲染）。"""
    for path in PAGES:
        assert 'name="viewport"' in client.get(path).text, path


def test_nav_entries_cover_all_capabilities(client):
    """顶栏导航 8 入口齐全——移动端底部横滑条与它同源复用，缺一项手机上就进不去。"""
    for path in NAV_PAGES:
        body = client.get(path).text
        for key in ("app", "knowledge", "books", "conflicts", "brief", "l4", "l5", "notify"):
            assert f'data-nav="{key}"' in body, (path, key)


def test_mobile_skeleton_in_css(client):
    """移动端骨架三件套：底部导航断点、安全区避让、表格转卡片通用类。"""
    css = client.get("/assets/style.css").text
    assert "@media(max-width:720px)" in css
    assert "safe-area-inset-bottom" in css
    assert ".as-cards" in css


def test_mobile_high_freq_pages_in_css(client):
    """高频三页移动方案：对话 flex 贴底（dvh 随键盘收缩）、通知表单单列、知识库搜索吸顶。"""
    css = client.get("/assets/style.css").text
    assert "100dvh" in css            # 对话区随动态视口收缩（键盘不遮输入框）
    assert "#pref-form" in css        # 偏好表单单列
    assert "#kb-search" in css        # 搜索框吸顶
    assert "position:sticky" in css


def test_mobile_secondary_pages_in_css(client):
    """其余页窄屏要点：行内表单纵向堆叠、登录页收紧留白、挖掘日志取消内滚。"""
    css = client.get("/assets/style.css").text
    assert ".inline-form{flex-direction:column" in css
    assert ".auth-wrap{padding:36px 16px}" in css
    assert ".log{max-height:none}" in css


def test_reader_has_toc_and_chapter_navigation(client):
    """阅读器要能顺畅跳章：侧边目录 + 上/下章 + 进度条（而不是只有一个下拉）。"""
    body = client.get("/reader.html").text
    assert "toc-panel" in body           # 侧边目录抽屉
    assert "toc-list" in body
    assert "btn-prev" in body and "btn-next" in body  # 上一章 / 下一章
    assert "progress-fill" in body       # 阅读进度条
    assert "chapter-indicator" in body   # 第 n / N 章
    assert "chapter-select" not in body  # 旧的下拉已替换
    assert "img-lightbox" in body        # 插图点击放大
    assert "restoreScroll" in body       # 章内位置恢复（图片加载后校准）


def test_reader_mobile_experience(client):
    """阅读器移动体验（M3）：触控翻章、字号三档、抽屉上滑关闭、顶栏收纳进 ⋯。"""
    body = client.get("/reader.html").text
    assert "touchstart" in body and "touchend" in body  # 滑动翻章 + 触摸划词弹层
    assert "FONT_TIERS = [15, 18, 22]" in body          # 字号三档（小/中/大）
    assert "bindSwipeUpClose" in body                   # 抽屉 panel-head 上滑关闭
    assert "btn-more" in body and "more-menu" in body    # ⋯ 收纳菜单
    assert "more-back" in body and "more-logout" in body  # 收纳的是书架/退出
    assert "reader-page" in body                        # 阅读页专属标记（无底部导航让位）

    css = client.get("/assets/style.css").text
    assert "body.reader-page" in css                    # 去掉底部导航的 84px 空隙
    assert "#btn-more{display:inline-flex}" in css      # 移动端才露出 ⋯
    assert ".toc-panel,.notes-panel{" in css            # 全屏抽屉
    assert ".more-menu[hidden]" in css                  # display:flex 不吃 hidden 属性，须显式兜底


def test_reading_log_page_has_group_switch_and_chart(client):
    """阅读日志支持 日/周/月 切换与趋势图。"""
    body = client.get("/reading_log.html").text
    assert "rl-group" in body            # 视图切换
    assert 'data-group="week"' in body
    assert 'data-group="month"' in body
    assert "rl-chart" in body            # 趋势柱状图
    assert "rl-list" in body


def test_api_routes_unaffected(client):
    assert client.get("/health").status_code == 200
    headers = auth_headers(client, "web_user")
    assert client.get("/api/v1/knowledge/items", headers=headers).status_code == 200


# ---------- PWA 安装（阶段 1 · M4）----------


def test_pwa_manifest_is_installable(client):
    """manifest 可安装元数据齐全：中文名 / standalone / 图标 192+512 + maskable。"""
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest" in resp.headers["content-type"]

    data = resp.json()
    assert data["name"] == "认知副驾"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/app.html"
    sizes = {icon["sizes"] for icon in data["icons"]}
    assert "192x192" in sizes and "512x512" in sizes
    purposes = {icon.get("purpose") for icon in data["icons"]}
    assert "maskable" in purposes
    for icon in data["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_service_worker_is_network_first(client):
    """SW 必须网络优先——缓存旧 JS 跑新后端是本项目踩过的坑；
    /api/ 响应含 JWT，绝不进缓存。SW 还要挂根路径（scope 才覆盖全站）。"""
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]

    sw = resp.text
    assert "fetch(req)" in sw                          # 先走网络
    assert "caches.match" in sw                        # 网络失败才兜底
    assert "url.pathname.indexOf('/api/')" in sw       # API 直连不缓存
    assert "register('/sw.js')" in client.get("/assets/app.js").text  # 根路径注册


def test_all_pages_link_manifest(client):
    """每页都挂 manifest 与主题色：安装机会可能出现在任何页面。"""
    for path in PAGES:
        body = client.get(path).text
        assert 'rel="manifest"' in body, path
        assert 'name="theme-color"' in body, path


def test_install_prompt_is_dismissible(client):
    """安装引导是轻提示：可「不用了」关闭并记住，不强拦使用。"""
    js = client.get("/assets/app.js").text
    assert "beforeinstallprompt" in js
    assert "pwa_install_dismissed" in js
    assert "pwa-install" in client.get("/assets/style.css").text
