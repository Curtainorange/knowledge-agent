"""微信读书官方 AI Skills 客户端（只读个人阅读数据）。

接口规范取自官方技能包（https://cdn.weread.qq.com/skills/weread-skills.zip）：

- 统一入口 `POST https://i.weread.qq.com/api/agent/gateway`
- 鉴权 `Authorization: Bearer <wrk-...>`；Key 绑定用户身份，无需再传 vid
- body 用 `api_name` 指定接口，**业务参数平铺在顶层**，且每次必须带 `skill_version`。
  官方文档专门标注了这个坑：把业务参数包进 `params` 会导致后端收不到，
  表现为「分页失效、永远返回第一页」。
- 回包 `errcode` 非 0 即错误；出现 `upgrade_info` 必须停止并提示升级

本模块只调查询接口，不向微信读书写入任何数据。
"""
from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)


class WeReadError(RuntimeError):
    """可以直接展示给用户的错误（Key 失效、限流、接口报错等）。"""


class WeReadNotConfigured(WeReadError):
    """未配置 API Key —— 端点据此返回「先去配置」而不是「同步失败」。"""


class WeReadClient:
    """微信读书 gateway 的最小客户端。

    `http` 可注入（测试用 `httpx.MockTransport`）；`request_interval` 用于逐本拉取时
    留出冷却，避免触发官方限流。
    """

    def __init__(
        self,
        api_key: str,
        *,
        gateway_url: str,
        skill_version: str = "1.0.4",
        page_size: int = 50,
        timeout: float = 20.0,
        request_interval: float = 0.0,
        http: httpx.Client | None = None,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise WeReadNotConfigured("未配置微信读书 API Key（WEREAD_API_KEY）")
        self._api_key = key
        self._gateway_url = gateway_url
        self._skill_version = skill_version
        self._page_size = max(1, int(page_size))
        self._timeout = timeout
        self._request_interval = max(0.0, float(request_interval))
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=timeout)
        self._last_call_at = 0.0

    # ---- 基础设施 --------------------------------------------------------

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "WeReadClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _throttle(self) -> None:
        if self._request_interval <= 0:
            return
        wait = self._request_interval - (time.monotonic() - self._last_call_at)
        if wait > 0:
            time.sleep(wait)

    def call(self, api_name: str, **params) -> dict:
        """调用一次 gateway，返回已校验的回包。"""
        self._throttle()
        payload: dict = {"api_name": api_name, "skill_version": self._skill_version}
        payload.update(params)  # 业务参数平铺：官方规范，包进 params 会失效
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._http.post(
                self._gateway_url, json=payload, headers=headers, timeout=self._timeout
            )
        except WeReadError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层异常统一转成可读错误
            raise WeReadError(f"连接微信读书失败：{exc}") from exc
        finally:
            self._last_call_at = time.monotonic()

        if response.status_code == 401:
            raise WeReadError("微信读书 API Key 无效或已失效，请到官方页面重新获取")
        if response.status_code == 429:
            raise WeReadError("微信读书接口限流（429），请稍后重试")
        if response.status_code != 200:
            raise WeReadError(f"微信读书接口返回 HTTP {response.status_code}")

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise WeReadError("微信读书接口返回了非 JSON 内容") from exc
        if not isinstance(data, dict):
            raise WeReadError("微信读书接口返回格式异常")

        upgrade = data.get("upgrade_info")
        if upgrade:
            message = upgrade.get("message") if isinstance(upgrade, dict) else str(upgrade)
            raise WeReadError(
                f"微信读书技能包需要升级：{message or '请到官方页面获取最新版本后重试'}"
            )

        errcode = data.get("errcode") or 0
        if errcode:
            raise WeReadError(
                f"微信读书接口报错（errcode={errcode}）：{data.get('errmsg') or '未知原因'}"
            )
        return data

    # ---- 业务接口 --------------------------------------------------------

    def notebooks(self) -> list[dict]:
        """书架上有笔记的书（笔记本概览）。

        官方用「时间排序值」做游标分页：首次只传 `count`，`hasMore=1` 时把本页最后
        一条的 `sort` 作为下一页 `lastSort`；**不支持 offset/limit**。
        """
        books: list[dict] = []
        last_sort = None
        while True:
            params: dict = {"count": self._page_size}
            if last_sort is not None:
                params["lastSort"] = last_sort
            data = self.call("/user/notebooks", **params)
            page = data.get("books") or []
            books.extend(page)
            if not data.get("hasMore") or not page:
                break
            next_sort = page[-1].get("sort")
            if next_sort is None or next_sort == last_sort:
                break  # 游标没前进，防死循环
            last_sort = next_sort
        return books

    def bookmarks(self, book_id: str) -> dict:
        """单本书的划线内容（官方已过滤书签，只返回划线）。"""
        return self.call("/book/bookmarklist", bookId=book_id)

    def my_reviews(self, book_id: str) -> list[dict]:
        """单本书的个人想法 / 点评（游标分页）。

        注意参数名与其它接口不一致：这里是**小写的 `bookid`**，以官方文档为准。
        """
        reviews: list[dict] = []
        synckey = None
        while True:
            params: dict = {"bookid": book_id, "count": self._page_size}
            if synckey:
                params["synckey"] = synckey
            data = self.call("/review/list/mine", **params)
            page = data.get("reviews") or []
            reviews.extend(page)
            if not data.get("hasMore") or not page:
                break
            next_key = data.get("synckey")
            if not next_key or next_key == synckey:
                break
            synckey = next_key
        return reviews

    def book_info(self, book_id: str) -> dict:
        """书籍基本信息（含 `deepLink`，用于跳回微信读书原文）。"""
        data = self.call("/book/info", bookId=book_id)
        book = data.get("book")
        return book if isinstance(book, dict) else data
