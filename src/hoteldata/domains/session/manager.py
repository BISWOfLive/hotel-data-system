"""登录管家(T2.4)—— 6 个**动作结果码** + 失效检测 + 自动重登 + 主动续登 + 滑块轨迹。

这是 B 级遗产里**最值钱的一个**(B13),逐字继承旧 ``crawl/login_manager.py``:

  * **轻量 HTTP 探活**(免开浏览器)—— 300 账号串行只需约 25 分钟;
  * **滑块轨迹**:``randint(8,14)`` 步 + **中途回撤**;
  * **人工兜底等待 600 秒**(``MANUAL_LOGIN_WAIT_S``,``config.py:159`` 唯一定义处);
  * **``auto_login()`` 永不抛异常**(失败返回 :class:`ActionCode`,不炸调用方)。

⚠️ 计划书 §T2.4 的「6 态状态机 ``unknown/valid/stale/invalid/logging_in/blocked``」
**是错的**(见 :mod:`hoteldata.domains.session.status` 的说明):
旧系统那 6 个是**动作结果码**,长期会话态只有 4 个。

★ 段1 的两处修正(有据可查,不是"顺手改")
------------------------------------------
1. **探活打的接口**:计划书说 ``fetchVisitorTitleV2``(**全仓库零命中**);
   实际是 ``pages["经营报告"].api_defs[0].url`` —— 实测
   ``.../dataCenter/report/getDayReportRealTimeDate``。新实现从**规则里取**,
   不硬编码 URL,规则改了探活自动跟着改。
2. **``addtime`` 时区陷阱**(批次 D)与本模块无关,但同属"宿主机时区"类坑,
   本模块的探活不做任何本地时间推断。
"""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from hoteldata.domains.session.status import (
    BUSINESS_TEXT_KEYWORDS,
    LOGIN_FORM_WAIT_S,
    LOGIN_URL_MARKERS,
    REQUIRED_API_HEADERS,
    ActionCode,
    SessionState,
    looks_like_auth_error,
    state_for_action,
)
from hoteldata.infra.paths import Layout, get_layout
from hoteldata.infra.session_store import SessionHandle, SessionStore
from hoteldata.settings import Settings, get_settings

__all__ = [
    "LOGIN_BUTTON_SELECTORS",
    "LOGIN_ENTRY_URLS",
    "PASSWORD_SELECTOR",
    "SLIDER_SELECTORS",
    "USERNAME_SELECTORS",
    "LoginManager",
]

# ---------------------------------------------------------------------------
# 逐字继承的选择器候选表
# ---------------------------------------------------------------------------

#: 用户名框候选(**11 条**,旧 ``crawl/login_manager.py:284-289``)
USERNAME_SELECTORS: tuple[str, ...] = (
    "input[type='text']",
    "input[name*='account']",
    "input[name*='user']",
    "input[name*='login']",
    "input[name*='mobile']",
    "input[name*='phone']",
    "input[name*='email']",
    "input[placeholder*='账号']",
    "input[placeholder*='手机']",
    "input[placeholder*='邮箱']",
    "input[placeholder*='用户名']",
)

#: ★ 密码框(等待选择器逐字,``state="visible"``)
PASSWORD_SELECTOR = "input[type='password']"

#: 兜底:密码框之前的第一个可见文本框(旧 ``xpath=preceding::input[1]``)
USERNAME_XPATH_FALLBACK = "xpath=preceding::input[1]"

#: 登录按钮候选(**6 条**,旧 ``crawl/login_manager.py:318-321``)
LOGIN_BUTTON_SELECTORS: tuple[str, ...] = (
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('登录')",
    "button:has-text('登 录')",
    "a:has-text('登录')",
    "a:has-text('登 录')",
)

#: 滑块候选选择器(**3 条**;第三项含 CSS 逗号 OR:
#: captcha 容器**或** canvas —— 单个字符串,不要拆开)
SLIDER_SELECTORS: tuple[str, ...] = (
    "[class*='slider' i]",
    "[role='slider']",
    "[class*='captcha' i], canvas",
)

#: 平台登录入口(人工登录时打开的第一页)
LOGIN_ENTRY_URLS: dict[str, str] = {
    "ctrip": "https://ebooking.ctrip.com/",
    "meituan": "https://e.meituan.com/",
}

#: 探活用的 UA(与采集链路一致)
PROBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)


@dataclass(slots=True)
class ProbeCandidate:
    """一个可用于探活的接口(``url``/``params``/``body`` 里**没有占位符**)。"""

    name: str
    method: str
    url: str
    referer: str = "https://ebooking.ctrip.com/"
    json_body: dict[str, Any] | None = None
    content: str | None = None


#: 该端点不可用(不代表会话失效)→ 换下一个候选
_UNUSABLE_STATUS = frozenset({400, 404, 405, 406, 415, 429, 500, 501, 502, 503, 504})


def _probe_verdict(attempt: Any) -> str:
    """把一次探活响应判成 ``"valid"`` / ``"invalid"`` / ``"unusable"``。

    * ``valid``    —— 干净 ``200``(正文不含鉴权失败标记);
    * ``invalid``  —— **权威失效**:401/403、重定向到登录页、``200 + 鉴权失败正文``;
    * ``unusable`` —— 该接口本身不可用(404/405/5xx),换个接口再试。
    """
    status = int(getattr(attempt, "status_code", 0) or 0)
    text = getattr(attempt, "text", "") or ""

    # ① 重定向:302→login 是权威失效;其它重定向也算失效(没 follow,拿不到真结果)
    if status in (301, 302, 303, 307, 308):
        location = (getattr(attempt, "location", lambda: "")() or "").lower()
        if any(m in location for m in LOGIN_URL_MARKERS):
            return "invalid"
        return "invalid"

    # ② 401/403 → 权威失效
    if status in (401, 403):
        return "invalid"

    # ③ ★ 200 还不够:平台在**会话失效时也返回 200**,正文是
    #    {"error": "invalid_grant", "error_description": "authorized fail!"}
    if status == 200:
        if looks_like_auth_error(text) is not None:
            return "invalid"
        return "valid"

    # ④ 端点不可用 → 换一个候选(★ 不能据此判会话失效)
    if status in _UNUSABLE_STATUS:
        return "unusable"

    return "unusable"


@dataclass(slots=True)
class LoginAttempt:
    """一次登录动作的完整记录。"""

    code: ActionCode
    detail: str = ""
    cookies: int = 0
    at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.code is ActionCode.OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": str(self.code),
            "detail": self.detail,
            "cookies": self.cookies,
            "at": (self.at or datetime.now()).isoformat(timespec="seconds"),
        }


class LoginManager:
    """登录态检测 + 自动重登 + 人工兜底。

    ``credentials`` 由调用方从 ``core_accounts`` 解密后传入(**本模块不碰密文**)。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sessions: SessionStore | None = None,
        pool: Any = None,
        layout: Layout | None = None,
        limiter: Any = None,
        http: Any = None,
        rules: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.sessions = sessions or SessionStore(self.settings)
        self.pool = pool
        self.layout = layout or get_layout(self.settings)
        self.limiter = limiter
        self.http = http
        self._rules = rules

    # ==================================================================
    # ① 轻量 HTTP 探活(免开浏览器)—— 最省资源的探测方式
    # ==================================================================

    @property
    def rules(self) -> Any:
        if self._rules is None:
            from hoteldata.domains.collect.rules import get_api_rules

            self._rules = get_api_rules()
        return self._rules

    def probe_url(self, platform: str = "ctrip") -> str | None:
        """探活目标 = ``pages["经营报告"].api_defs[0].url``(**从规则取,不硬编码**)。

        ⚠️ 计划书写的是 ``fetchVisitorTitleV2`` —— 该字符串在旧仓库**零命中**。
        """
        try:
            page = self.rules.page("经营报告")
        except Exception:  # noqa: BLE001
            return None
        for d in page.api_defs:
            if d.method == "GET" and d.url:
                return str(d.url)
        return str(page.api_defs[0].url) if page.api_defs else None

    async def probe_login_valid(
        self,
        handle: SessionHandle,
        *,
        timeout_s: float = 30.0,
        platform: str | None = None,
    ) -> bool:
        """轻量探活(**免开浏览器**)—— ★ **多候选**:任意一个接口干净 200 即判有效。

        为什么必须多候选(2026-09-30 实测)
        ----------------------------------
        旧实现只打 ``经营报告.api_defs[0]`` 一个接口。实测该 GET 接口现在返回
        **HTTP 405**,而同一份**完全有效**的会话打 POST 类接口返回
        ``200 {"visitorTotal":21,...}`` 真实数据。

        → **只盯一个接口,会把有效会话判成失效。** 后果是连锁的:
        ``sessions --check`` 永远 invalid → ``ops patrol`` 永远触发重登 →
        登录成功后落盘校验也不过 → 好会话被回滚丢掉。

        判定规则:
          * 干净 ``200``(正文不含鉴权失败标记)→ **有效**;
          * ``401/403``、``302→login``、``200 + 鉴权失败正文`` → **失效且权威**,立即返回;
          * ``404/405/5xx``/网络异常 → **该端点不可用**,换下一个候选;
          * 全部候选都不可用 → 保守判失效,并在日志里说清是"端点不可用"而非"会话失效"。
        """
        platform = platform or handle.platform
        if not handle.exists():
            logger.info("登录态文件缺失,探活返回 False: {}", handle.storage_state_path())
            return False
        try:
            cookie = handle.cookie_header()
        except Exception as exc:  # noqa: BLE001
            logger.info("登录态无可用 cookies: {}", exc)
            return False

        candidates = self.probe_candidates(platform)
        if not candidates:
            logger.warning("探活候选为空(api_rules 无 经营报告 的可用接口):按失效处理")
            return False

        client = self.http
        if client is None:
            from hoteldata.infra.http import HttpClient

            client = HttpClient(settings=self.settings)

        unusable: list[str] = []
        for cand in candidates:
            # ★★ 探活请求头必须与 **API 直连通道**一致 —— 平台网关凭
            #    ``x-requested-with: XMLHttpRequest`` 区分「API 调用」与「页面导航」:
            #    实测同一份**有效** cookie,不带它 → ``302 /login``;带上 → ``200``。
            headers = {
                "cookie": cookie,
                "user-agent": PROBE_USER_AGENT,
                **REQUIRED_API_HEADERS,
                "referer": cand.referer,
            }
            if cand.method == "POST":
                headers["content-type"] = "application/json"
            try:
                if self.limiter is not None:
                    async with self.limiter.request(platform, handle.alias):
                        attempt = await client.request(
                            cand.method,
                            cand.url,
                            headers=headers,
                            json_body=cand.json_body,
                            content=cand.content,
                            timeout=timeout_s,
                            follow_redirects=False,
                        )
                else:
                    attempt = await client.request(
                        cand.method,
                        cand.url,
                        headers=headers,
                        json_body=cand.json_body,
                        content=cand.content,
                        timeout=timeout_s,
                        follow_redirects=False,
                    )
            except Exception as exc:  # noqa: BLE001 - 网络异常 → 换下一个候选
                unusable.append(f"{cand.name}({type(exc).__name__})")
                continue

            verdict = _probe_verdict(attempt)
            if verdict == "valid":
                logger.debug("探活通过({} → HTTP 200)", cand.name)
                return True
            if verdict == "invalid":
                logger.info("探活判定失效({} → HTTP {})", cand.name, attempt.status_code)
                return False
            unusable.append(f"{cand.name}(HTTP {attempt.status_code or attempt.error})")

        logger.warning(
            "探活全部候选都不可用({});这**不能证明会话失效** —— 可能是平台改了这些接口。"
            "请核对 api_rules 或运行 scripts/record_apis.py 重新录制",
            ", ".join(unusable[:6]),
        )
        return False

    def probe_candidates(self, platform: str = "ctrip", *, limit: int = 6) -> list[ProbeCandidate]:
        """挑出**不需要窗口上下文**(url/params/body 无 ``{占位符}``)的接口做探活候选。

        排序:先 **POST + 静态 body**(SOA 查询类,实测最稳),再 GET,最后无 body 的 POST。
        """
        from hoteldata.domains.collect.api import PLACEHOLDER_RE

        try:
            page = self.rules.page("经营报告")
        except Exception:  # noqa: BLE001
            return []
        post_with_body: list[ProbeCandidate] = []
        gets: list[ProbeCandidate] = []
        posts: list[ProbeCandidate] = []
        for d in page.api_defs:
            blob = json.dumps({"u": d.url, "p": d.params, "b": d.body}, ensure_ascii=False, default=str)
            if PLACEHOLDER_RE.findall(blob):
                continue  # 含占位符 → 没有窗口上下文就渲染不出来,不能用作探活
            if not str(d.url or "").startswith("http"):
                continue
            if d.method == "POST":
                cand = ProbeCandidate(
                    name=d.name,
                    method="POST",
                    url=str(d.url),
                    json_body=d.body if isinstance(d.body, dict) else None,
                    content=d.body if isinstance(d.body, str) else None,
                    referer=page.url,
                )
                (post_with_body if (cand.json_body or cand.content) else posts).append(cand)
            else:
                gets.append(ProbeCandidate(name=d.name, method="GET", url=str(d.url), referer=page.url))
        ordered = [*post_with_body, *gets, *posts]
        return ordered[:limit]

    # ==================================================================
    # ② 状态判定
    # ==================================================================

    async def check(self, handle: SessionHandle, *, probe: bool = True) -> SessionState:
        """快速探测(**约 5 秒**,无头/无浏览器)。

        ``probe=False`` 时只按文件年龄推断,不发请求。
        """
        age = handle.age_days()
        if not handle.exists():
            state = SessionState.INVALID
        elif probe:
            ok = await self.probe_login_valid(handle)
            state = self._state_of(age, ok)
        else:
            state = self._state_of(age, None)
        await self._persist_state(handle, state)
        return state

    def _state_of(self, age: float | None, probe_valid: bool | None) -> SessionState:
        from hoteldata.domains.session.status import state_from_age

        return state_from_age(
            age,
            max_age_days=self.settings.login.max_age_days,
            probe_valid=probe_valid,
        )

    async def _persist_state(self, handle: SessionHandle, state: SessionState) -> None:
        try:
            await self.sessions.set_status(
                handle.key,
                str(state),
                last_check_at=datetime.now(),
                file_mtime=handle.mtime(),
            )
        except Exception as exc:  # noqa: BLE001 - 状态登记失败不影响主流程
            logger.debug("登记会话状态失败 {}: {}", handle, exc)

    def need_renewal(self, handle: SessionHandle) -> bool:
        """距上次登录 > **20 天** → 主动续登(B13 遗产)。"""
        return self.sessions.need_renewal(handle)

    # ==================================================================
    # ③ 自动重登(永不抛异常)
    # ==================================================================

    async def _commit_state(self, context: Any, handle: SessionHandle, *, reason: str) -> LoginAttempt | None:
        """把浏览器当前登录态落盘 —— ★ **先用轻量探活验证,验证不过就回滚**。

        为什么必须验证
        --------------
        旧写法只看"页面正文里有没有业务关键词"就宣布成功并落盘。而 eBooking 的登录是
        **多步**的(账号 → 验证码/滑块 → 平台下发 ``usertoken`` / ``usersign`` /
        ``randomkey`` / ``imislogin`` 等鉴权 cookie)。**中间态页面看起来也像登录了** ——
        于是会写出一个"有 ``w_tuid`` 却没有 ``usertoken``"的**假登录态**,
        还会把可能仍然有效的旧文件**覆盖掉**。

        这正是这次重写要消灭的「静默假成功」:命令报成功、文件看着有内容、
        数据一条都取不到,而且**没人知道**。

        做法:备份旧文件 → 写新文件 → 探活 → 通过则删备份;
        不通过则**回滚旧文件**并返回 ``None``(调用方继续等待或转人工)。
        """
        state = await context.storage_state()
        path = handle.storage_state_path()
        backup = path.with_name(path.name + ".bak")
        had_old = path.exists()
        if had_old:
            shutil.copy2(path, backup)
        handle.save(state)
        cookies = len(state.get("cookies") or [])

        if await self.probe_login_valid(handle):
            backup.unlink(missing_ok=True)
            return LoginAttempt(code=ActionCode.OK, detail=reason, cookies=cookies)

        logger.warning(
            "{}:页面看起来已登录,但**探活未通过** —— 登录未真正完成"
            "(常见原因:还差验证码/滑块,或账号受限);已回滚登录态文件,不写入假登录态",
            reason,
        )
        if had_old:
            shutil.move(str(backup), str(path))
        else:
            path.unlink(missing_ok=True)
        return None

    async def auto_login(
        self,
        handle: SessionHandle,
        credentials: tuple[str, str] | None = None,
        *,
        interactive: bool = False,
    ) -> LoginAttempt:
        """自动登录:**填表 → 滑块自动拖 → 人工兜底**。**永不抛异常。**

        返回 :class:`LoginAttempt`,其 ``code`` 是**动作结果码**之一。
        """
        at = datetime.now()
        if credentials is None:
            return LoginAttempt(
                code=ActionCode.MANUAL_REQUIRED,
                detail="未提供凭据(需人工登录)",
                at=at,
            )
        username, password = credentials
        if not username or not password:
            return LoginAttempt(
                code=ActionCode.MANUAL_REQUIRED,
                detail="凭据为空(需人工登录)",
                at=at,
            )
        if self.pool is None:
            return LoginAttempt(
                code=ActionCode.FAILED,
                detail="浏览器池未装配,无法自动登录",
                at=at,
            )

        entry = LOGIN_ENTRY_URLS.get(handle.platform, LOGIN_ENTRY_URLS["ctrip"])
        try:
            async with self.pool.page_session(handle, save_state_to=None) as (_browser, context, page):
                await page.goto(entry, wait_until="domcontentloaded")
                # ★ 登录页是 React 异步渲染,立即扫描会误判「未找到密码输入框」
                await page.wait_for_timeout(LOGIN_FORM_WAIT_S * 1000)
                if await self._page_is_logged_in(page):
                    attempt = await self._commit_state(context, handle, reason="复用既有会话已登录")
                    if attempt is not None:
                        return attempt
                    # 探活不过 → 说明并不是真的登录态,继续走填表流程
                    logger.info("既有会话其实不可用,继续尝试填表登录")

                filled = await self._fill_form(page, username, password)
                if not filled:
                    return await self._manual_fallback(
                        handle,
                        context,
                        page,
                        ActionCode.MANUAL_REQUIRED,
                        "未找到登录表单(需人工)",
                        interactive=interactive,
                    )

                await self._click_login(page)

                # 滑块(若出现)
                if await self._has_slider(page):
                    ok = await self._drag_slider(page)
                    if not ok:
                        logger.warning("滑块自动拖动失败,转人工")
                        return await self._manual_fallback(
                            handle,
                            context,
                            page,
                            ActionCode.CAPTCHA,
                            "滑块需人工拖动",
                            interactive=interactive,
                        )

                # 等跳转
                logged = await self._wait_logged_in(page)
                if logged:
                    attempt = await self._commit_state(context, handle, reason="自动登录成功(填充登录)")
                    if attempt is not None:
                        return attempt
                    return await self._manual_fallback(
                        handle,
                        context,
                        page,
                        ActionCode.MANUAL_REQUIRED,
                        "自动填表后探活未通过(登录未真正完成,可能还需验证码/滑块)",
                        interactive=interactive,
                    )
                return await self._manual_fallback(
                    handle,
                    context,
                    page,
                    ActionCode.MANUAL_REQUIRED,
                    "自动登录后仍在登录页(可能有验证码,需人工)",
                    interactive=interactive,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - ★ 永不抛异常
            logger.warning("自动登录异常(按失败处理): {}", exc)
            return LoginAttempt(code=ActionCode.FAILED, detail=str(exc), at=at)

    async def _manual_fallback(
        self,
        handle: SessionHandle,
        context: Any,
        page: Any,
        code: ActionCode,
        detail: str,
        *,
        interactive: bool,
    ) -> LoginAttempt:
        """人工兜底:最长等 ``MANUAL_LOGIN_WAIT_S``(**600 秒**)。"""
        at = datetime.now()
        if not interactive:
            return LoginAttempt(code=code, detail=detail + "(未开启人工等待)", at=at)
        logger.warning(
            "{} —— 请在浏览器中人工完成登录(最长等 {} 秒)", detail, self.settings.login.manual_wait_s
        )
        deadline = time.monotonic() + self.settings.login.manual_wait_s
        last_log = 0.0
        while time.monotonic() < deadline:
            if await self._page_is_logged_in(page):
                # ★ 页面像登录了还不够:必须**探活通过**才算真的登录完成
                attempt = await self._commit_state(context, handle, reason="人工登录成功")
                if attempt is not None:
                    return attempt
                # 页面像登录但探活不过 → 多半还差一步(验证码/滑块/二次确认),继续等
                logger.info("页面已进入业务页但探活未通过,继续等待人工完成剩余的登录步骤…")
            now = time.monotonic()
            if now - last_log >= 5.0:  # 每 ≥5 秒打一次诊断
                last_log = now
                logger.info(
                    "等待人工登录…剩余 {:.0f} 秒",
                    deadline - now,
                )
            await asyncio.sleep(2.0)
        return LoginAttempt(code=ActionCode.TIMEOUT, detail="人工登录等待超时", at=at)

    async def interactive_login(
        self, handle: SessionHandle, credentials: tuple[str, str] | None = None
    ) -> LoginAttempt:
        """人工登录(T2.5 的 ``hoteldata login``):**有头**打开登录页并等人工完成。"""
        return await self.auto_login(handle, credentials, interactive=True)

    # ==================================================================
    # ④ 页面级判定与交互
    # ==================================================================

    @staticmethod
    async def _page_is_logged_in(page: Any) -> bool:
        """URL 不在登录页 **且** 页面出现强数据类关键词 → 视为已登录。"""
        current = (page.url or "").lower()
        if any(m in current for m in LOGIN_URL_MARKERS):
            # 有些站点 URL 含 auth 但在业务域内,再按正文判定
            if not await LoginManager._has_business_text(page):
                return False
        return await LoginManager._has_business_text(page)

    @staticmethod
    async def _has_business_text(page: Any) -> bool:
        try:
            text = await page.locator("body").inner_text(timeout=5000)
        except Exception:  # noqa: BLE001
            return False
        return any(kw in (text or "") for kw in BUSINESS_TEXT_KEYWORDS)

    async def _wait_logged_in(self, page: Any, timeout_s: float = 45.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self._page_is_logged_in(page):
                return True
            await asyncio.sleep(1.0)
        return False

    async def _fill_form(self, page: Any, username: str, password: str) -> bool:
        """填表(用户名 11 候选 + xpath 兜底;密码框逐字等待)。"""
        frames = [page.main_frame, *page.frames]
        for frame in frames:
            try:
                await frame.wait_for_selector(
                    PASSWORD_SELECTOR,
                    state="visible",
                    timeout=LOGIN_FORM_WAIT_S * 1000,
                )
            except Exception:  # noqa: BLE001
                continue
            filled_user = await self._fill_username(frame, username)
            if not filled_user:
                continue
            try:
                pwd_box = frame.locator(PASSWORD_SELECTOR).first
                await pwd_box.fill(password)
            except Exception as exc:  # noqa: BLE001
                logger.debug("填密码失败: {}", exc)
                continue
            logger.info("已填充登录表单(frame={})", getattr(frame, "url", "?"))
            return True
        return False

    @staticmethod
    async def _fill_username(frame: Any, username: str) -> bool:
        for sel in USERNAME_SELECTORS:
            try:
                loc = frame.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.fill(username)
                    return True
            except Exception:  # noqa: BLE001
                continue
        # 兜底:密码框之前的第一个可见文本框
        try:
            pwd_box = frame.locator(PASSWORD_SELECTOR).first
            candidate = pwd_box.locator(USERNAME_XPATH_FALLBACK)
            if await candidate.count() > 0 and await candidate.is_visible():
                await candidate.fill(username)
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    async def _click_login(page: Any) -> bool:
        """点登录按钮。**找不到按钮仍视为填表成功**(逐字继承旧语义)。"""
        frames = [page.main_frame, *page.frames]
        for frame in frames:
            for sel in LOGIN_BUTTON_SELECTORS:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click()
                        logger.info("已点击登录按钮,等待跳转…")
                        return True
                except Exception:  # noqa: BLE001
                    continue
        logger.warning("未找到登录按钮,可能已提交或需手动点击")
        return True

    @staticmethod
    async def _has_slider(page: Any) -> bool:
        frames = [page.main_frame, *page.frames]
        for frame in frames:
            for sel in SLIDER_SELECTORS:
                try:
                    loc = frame.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    @staticmethod
    async def _drag_slider(page: Any) -> bool:
        """★ 拖拽滑块(**人类轨迹,8~14 步 + 中途回撤**)。失败/找不到返回 False,不抛错。

        逐字继承旧 ``crawl/login_manager.py:362-389``::

            steps       = random.randint(8, 14)
            step_px     = (target_x - start_x) / steps
            forward     = start_x + step_px * (i + 1) + random.uniform(-1, 2)
            回撤        = if i == steps // 2: move(forward - random.uniform(2, 5))
            每步间隔    = random.uniform(0.015, 0.040)
            终点        = box.x + box.width * 0.8
        """
        try:
            handle = None
            for frame in [page.main_frame, *page.frames]:
                for sel in SLIDER_SELECTORS:
                    loc = frame.locator(sel).first
                    if await loc.count() > 0 and await loc.is_visible():
                        handle = loc
                        break
                if handle is not None:
                    break
            if handle is None:
                logger.warning("未找到滑块,转人工")
                return False

            box = await handle.bounding_box()
            if not box or box.get("width", 0) <= 0:
                logger.warning("滑块无有效尺寸,无法拖拽(转人工)")
                return False

            start_x = box["x"] + box["width"] * 0.5
            start_y = box["y"] + box["height"] * 0.5
            target_x = box["x"] + box["width"] * 0.8

            mouse = page.mouse
            await mouse.move(start_x, start_y)
            await mouse.down()
            steps = random.randint(8, 14)
            step_px = (target_x - start_x) / steps
            for i in range(steps):
                forward = start_x + step_px * (i + 1) + random.uniform(-1, 2)
                await mouse.move(forward, start_y)
                await asyncio.sleep(random.uniform(0.015, 0.040))
                if i == steps // 2:
                    # 回撤一次:模拟手抖
                    await mouse.move(forward - random.uniform(2, 5), start_y)
                    await asyncio.sleep(random.uniform(0.015, 0.040))
            await mouse.up()
            logger.info("滑块已自动拖动(人类轨迹模拟,{} 步)", steps)
            return True
        except Exception as exc:  # noqa: BLE001 - ★ 不抛错
            logger.warning("滑块自动拖动异常: {}", exc)
            return False

    # ==================================================================
    # ⑤ 对外主入口(collect 域通过 Runtime 注入的回调间接用它)
    # ==================================================================

    async def ensure_valid(
        self,
        handle: SessionHandle,
        credentials: tuple[str, str] | None = None,
    ) -> bool:
        """确保登录态可用:探活 → 失效则自动重登 → 再探活。"""
        if handle.exists() and await self.probe_login_valid(handle):
            await self._persist_state(handle, SessionState.VALID)
            return True
        logger.warning("登录态不可用,尝试自动重登: {}", handle)
        attempt = await self.auto_login(handle, credentials, interactive=False)
        await self._record_event(handle, "relogin", attempt)
        if attempt.ok:
            return await self.probe_login_valid(handle)
        return False

    async def renew(self, handle: SessionHandle, credentials: tuple[str, str] | None = None) -> bool:
        """主动续登(20 天阈值)。"""
        if not self.need_renewal(handle):
            return True
        logger.info("登录态超过 {} 天,主动续登: {}", self.settings.login.max_age_days, handle)
        attempt = await self.auto_login(handle, credentials, interactive=False)
        await self._record_event(handle, "renew", attempt)
        return attempt.ok

    async def _record_event(self, handle: SessionHandle, action: str, attempt: LoginAttempt) -> None:
        """写 ``ops_login_events``(**动作结果码落在 ``detail``**)。"""
        db = self.sessions.db
        if db is None:
            return
        from hoteldata.infra.models import LoginEvent

        try:
            account_id = None
            for acc in await self.sessions.accounts():
                if acc.alias == handle.alias:
                    account_id = acc.id
                    break
            async with db.session() as s:
                s.add(
                    LoginEvent(
                        account_id=account_id,
                        action=action,
                        result="ok" if attempt.ok else "fail",
                        detail=f"{attempt.code}:{attempt.detail}",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("记录登录事件失败: {}", exc)


# 保留引用,避免被 ruff 当作未使用导入
_ = (Path, state_for_action)
