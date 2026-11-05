"""DeepSeek 官方搜索/抓取接口的客户端（只读，不入库）。

用途只有一个：给「资料研究」能力提供**真实的**外部信息，让模型能自己决定搜什么、
读哪一页，而不是凭记忆编一份书单。

三条实现约定，都是从「宁可少给，也不骗人」这条主线上来的：

1. **失败不抛异常，返回 `ok=False + 一句原因`。** 一次检索失败不该让整轮对话 500，
   更不该让模型以为自己拿到了材料。调用方据此在回复里说清「这次没搜到」。
2. **不重试。** 搜索是「模型自己决定要不要再搜一次」的链路——它看到失败会换 query，
   比我们原地重试同一个词有效。重试只会把等待时间翻倍。
3. **正文必须截断。** 一个网页动辄几万字，原样塞回 prompt 既贵又会把真正的线索冲淡；
   截断长度在 `FETCH_CONTENT_CHARS`。

计费口径：搜索/抓取按**成功请求**计费（Search 需返回非空结果，Fetch 需取到正文），
所以失败不花钱。客户端不做本地记账——那条链路在 `cost_logs` 之外，属于工具调用费，
`research` 的卡片上会显示实际用了几次。
"""
from __future__ import annotations

import logging
import ssl

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

# 单次检索/抓取的超时。比模型调用短得多：用户在等，而这两个接口本来就是毫秒级。
_TIMEOUT_SECONDS = 20.0
# 回灌给模型的单页正文上限。够覆盖一篇长文的要点，又不至于把上下文占满。
FETCH_CONTENT_CHARS = 4000
# 搜索摘要上限：摘要只用于判断「这条要不要读」，不需要全文
SUMMARY_CHARS = 600
MAX_RESULTS = 8


def available() -> bool:
    """有没有 Key 可用。没有就说明没配置，能力该退回「不搜，直接给建议」。"""
    return bool((settings.deepseek_api_key or "").strip())


def _request(path: str, payload: dict, key: str, *, verify=None) -> httpx.Response:
    url = f"{settings.deepseek_base_url.rstrip('/')}{path}"
    kwargs = {} if verify is None else {"verify": verify}
    return httpx.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=_TIMEOUT_SECONDS,
        **kwargs,
    )


def _post(path: str, payload: dict) -> tuple[dict | None, str]:
    """发一次请求。返回 (json, 错误原因)；成功时错误原因为空串。

    证书校验失败时会用**系统证书库**再试一次。这不是兜底「关掉校验」：
    企业网络与本地代理常做 TLS 中间人，它们的根证书装在系统证书库里，
    而 httpx 默认用的 certifi 里没有——只跳过这一层会导致联网检索在
    这类环境里永远返回「连不上」，看起来像功能坏了。
    """
    key = (settings.deepseek_api_key or "").strip()
    if not key:
        return None, "没有配置 DEEPSEEK_API_KEY，无法联网检索"
    try:
        resp = _request(path, payload, key)
    except httpx.TimeoutException:
        return None, "检索超时"
    except httpx.HTTPError as exc:
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            logger.warning("deepseek %s 请求失败: %s", path, exc)
            return None, "检索服务连接失败"
        logger.warning("deepseek %s 证书校验失败，改用系统证书库重试: %s", path, exc)
        try:
            resp = _request(path, payload, key, verify=ssl.create_default_context())
        except httpx.HTTPError as retry_exc:
            logger.warning("deepseek %s 重试仍失败: %s", path, retry_exc)
            return None, "检索服务连接失败"

    if resp.status_code == 401:
        return None, "模型 Key 无权调用检索接口"
    if resp.status_code == 429:
        return None, "检索被限流"
    if resp.status_code >= 400:
        logger.warning("deepseek %s 返回 %s: %s", path, resp.status_code, resp.text[:200])
        return None, f"检索服务返回 {resp.status_code}"
    try:
        return resp.json(), ""
    except ValueError:
        return None, "检索结果不是合法 JSON"


def search(query: str, *, count: int = 5) -> dict:
    """检索网页。返回 `{ok, results, error}`，每项含 title/url/site/summary/content。

    `ok=False` 且 `results=[]` 表示这次真的没搜到——调用方必须区分「没搜到」与
    「搜到了空结果」，因为前者要说出来，后者可能只是 query 太窄。
    """
    text = (query or "").strip()
    if not text:
        return {"ok": False, "results": [], "error": "检索词为空"}
    payload = {
        "query": text[:200],
        "count": max(1, min(int(count or 5), MAX_RESULTS)),
        "need_content": False,
    }
    data, error = _post(SEARCH_PATH, payload)
    if data is None:
        return {"ok": False, "results": [], "error": error}

    raw = data.get("results")
    if not isinstance(raw, list):
        return {"ok": False, "results": [], "error": "检索结果格式异常"}

    results: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": str(item.get("title") or url).strip()[:200],
                "url": url,
                "site": str(item.get("site_name") or "").strip()[:60],
                "date": str(item.get("date") or "").strip()[:32],
                "summary": str(item.get("summary") or "").strip()[:SUMMARY_CHARS],
            }
        )
    return {"ok": bool(results), "results": results, "error": "" if results else "没有搜到结果"}


def fetch(url: str) -> dict:
    """抓取单页正文（Markdown）。返回 `{ok, title, content, error}`。"""
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return {"ok": False, "title": "", "content": "", "error": "不是可抓取的网址"}
    data, error = _post(FETCH_PATH, {"url": target})
    if data is None:
        return {"ok": False, "title": "", "content": "", "error": error}

    content = str(data.get("content") or "").strip()
    if not content:
        return {"ok": False, "title": "", "content": "", "error": "这一页没能取到正文"}
    return {
        "ok": True,
        "title": str(data.get("title") or target).strip()[:200],
        "content": content[:FETCH_CONTENT_CHARS],
        "error": "",
    }
