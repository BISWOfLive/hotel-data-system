"""API 直连通道(T3.3)—— 双通道的主通道。

流程(段1 §5.5 / 旧 ``api_collector.py``)::

    cookie 头(session_store) → 展开窗口 → 日期区间
      → ★ 占位符守卫  ← 绝不携带空日期或占位符原文请求平台
      → 逐接口重放(限频 0.6s + 超时 20s + 仅 GET 重试)
      → 按 fields[].path 提取(json_get)
      → 逐接口原始响应落盘
      → 汇总四态

**逐字继承的 A 级常量**(改一个就断)
------------------------------------
* UA ``Chrome/125``;``x-requested-with: XMLHttpRequest``;
* ``trace`` = ``09031123114691476238-{毫秒:03d}-{6位随机:06d}``;
  ``rand`` = ``random.random()`` **浮点数**(不是整数、无前缀);
* 登录失效**三路检测**的字符串常量;
* POST 且 body 为字符串 → ``application/x-www-form-urlencoded; charset=UTF-8``,
  否则 ``application/json``;**GET 不带 content-type**;
* 超时 **20 秒**。

**★ 占位符守卫(★ 最精妙的设计,B2)**
-------------------------------------
重放前检查 url / params / body 模板里的 ``{xxx}``:

  1. 未知占位符(token 不在 9 个静态键里)→ 抛错;
  2. ``ctx.get(token) is None``(**严格 ``is None``**,空串不拦)→ 抛错;
  3. 渲染后仍残留 ``{xxx}`` → 抛错。

这条守卫防的是「采回来一堆错数据还显示成功」——**绝不携带空日期或占位符原文请求平台**。
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from loguru import logger

from hoteldata.domains.collect.contract import (
    ExtractContext,
    ExtractResult,
    LoginExpiredError,
    NeedRecordError,
    PlaceholderError,
    is_empty_value,
)
from hoteldata.domains.collect.jsonpath import json_get
from hoteldata.domains.collect.rules import (
    ApiDefCfg,
    ApiMatchCfg,
    ApiRules,
    PageCfg,
    SubModuleCfg,
    match_resolves,
    page_url,
)
from hoteldata.domains.collect.windows import normalize_window, window_date_ctx
from hoteldata.domains.session.status import looks_like_auth_error
from hoteldata.infra.http import HttpAttempt

__all__ = [
    "LOGIN_BODY_MARKERS",
    "LOGIN_URL_MARKERS",
    "PLACEHOLDER_RE",
    "SUB_KEYS",
    "USER_AGENT",
    "ApiChannel",
    "gen_rand",
    "gen_trace",
]

# ---------------------------------------------------------------------------
# A 级常量(逐字继承,不许改)
# ---------------------------------------------------------------------------

#: URL 中出现这些标记视为登录页(用于 302 重定向判定)
LOGIN_URL_MARKERS: tuple[str, ...] = ("login", "passport", "signin", "sign_in", "auth", "sso")

#: json body / 响应体中出现这些中文标记视为登录态失效
LOGIN_BODY_MARKERS: tuple[str, ...] = ("未登录", "请重新登录", "登录失效")

#: 统一 User-Agent(与真实浏览器一致,降低风控)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125 Safari/537.36"
)

#: 占位符 token(用于残留守卫)
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

#: POST body 为非空字符串(x-www-form-urlencoded 表单模板)时的 Content-Type
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded; charset=UTF-8"

#: 可替换占位符键:静态四件套 + 窗口日期参数(见 ``window_date_ctx``)
SUB_KEYS: tuple[str, ...] = (
    "date",
    "rand",
    "trace",
    "hotel_id",
    "startDate",
    "endDate",
    "statDate",
    "yesterday",
    "today",
)

#: trace 前缀(字面量,20 位数字 + 连字符)
TRACE_PREFIX = "09031123114691476238-"


def gen_trace() -> str:
    """``09031123114691476238-{毫秒:03d}-{6位随机:06d}``。

    **毫秒段** = ``time.time()`` 的**小数部分 × 1000 后截断取整**(不是 ``ms`` 字段、
    也不是 ``% 1000`` 的整数毫秒),取值 0~999,**零填充 3 位**。
    """
    ms = int((time.time() % 1) * 1000)
    r6 = random.randint(0, 999999)
    return f"{TRACE_PREFIX}{ms:03d}-{r6:06d}"


def gen_rand() -> float:
    """``random.random()`` —— **浮点数**,``[0.0, 1.0)``。

    ``str()`` 后形如 ``"0.7384123456789012"``(Python repr 风格,**不是整数**)
    —— 与 ``trace`` 是两个格式完全不同的字段,不要混淆。
    """
    return random.random()


# ---------------------------------------------------------------------------
# 接口级返回记录
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApiResponse:
    """一次接口重放的记录(供 ``detail`` 与原始落盘用)。"""

    name: str
    ok: bool
    url: str = ""
    method: str = ""
    status_code: int | None = None
    body: Any = None
    text: str = ""
    error: str | None = None
    skipped: str | None = None
    elapsed_ms: float = 0.0
    raw_path: str | None = None

    def brief(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "skipped": self.skipped,
            "error": self.error,
            "raw_path": self.raw_path,
        }


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ApiChannel:
    """API 直连通道(单账号内**串行**使用,不要跨协程共享实例)。"""

    ctx: ExtractContext
    rules: ApiRules
    _current_page_url: str = ""
    _current_multi_param: str | None = None
    _raw_written: list[str] = field(default_factory=list)

    # ==================================================================
    # 占位符
    # ==================================================================

    @staticmethod
    def build_subs(values: dict[str, Any]) -> dict[str, str]:
        """替换表:缺失键 → 空串(**键集固定为 9 个静态键**)。"""
        return {key: ("" if values.get(key) is None else str(values.get(key))) for key in SUB_KEYS}

    @staticmethod
    def apply_placeholders(text: str, subs: dict[str, str]) -> str:
        for key, val in subs.items():
            text = text.replace("{" + key + "}", val)
        return text

    @classmethod
    def substitute_deep(cls, obj: Any, subs: dict[str, str]) -> Any:
        """递归替换:``str`` → 替换;``list`` → 逐元素;``dict`` → 逐值;其余原样。"""
        if isinstance(obj, str):
            return cls.apply_placeholders(obj, subs)
        if isinstance(obj, list):
            return [cls.substitute_deep(x, subs) for x in obj]
        if isinstance(obj, dict):
            return {k: cls.substitute_deep(v, subs) for k, v in obj.items()}
        return obj

    def guard_placeholders(self, api_def: ApiDefCfg, values: dict[str, Any]) -> None:
        """★ 占位符守卫(三步判定,顺序固定)。

        抛 :class:`PlaceholderError` → 上游降级浏览器通道。
        """
        parts = [str(api_def.url or "")]
        if isinstance(api_def.params, dict):
            parts.append(json.dumps(api_def.params, ensure_ascii=False))
        body = api_def.body
        if body is not None:
            parts.append(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
        template = "\n".join(parts)

        # ① 未知占位符(去重且保序)
        for token in dict.fromkeys(PLACEHOLDER_RE.findall(template)):
            if token not in SUB_KEYS:
                raise PlaceholderError(f"api_def 含未知占位符 {{{token}}}(视为未校准,将降级浏览器兜底)")
            # ② 缺少上下文值 —— ★ 严格 is None,空串不拦
            if values.get(token) is None:
                raise PlaceholderError(f"占位符 {{{token}}} 缺少窗口/上下文值(视为未校准,将降级浏览器兜底)")

        # ③ 渲染后残留
        rendered = self.apply_placeholders(template, self.build_subs(values))
        residual = PLACEHOLDER_RE.findall(rendered)
        if residual:
            raise PlaceholderError(
                f"api_def 占位符 {{{residual[0]}}} 渲染后仍残留(视为未校准,将降级浏览器兜底)"
            )

    # ==================================================================
    # 请求构造
    # ==================================================================

    def page_referer(self, page_cfg: PageCfg) -> str:
        """referer **按页选**;多店账号 + 门店 id → 追加门店参数。"""
        return page_url(
            page_cfg,
            hotel_id=self.ctx.multi_store_hotel_id(),
            is_multi=self.ctx.account.is_multi,
        )

    def build_headers(self, api_def: ApiDefCfg) -> dict[str, str]:
        headers = {
            "cookie": self.ctx.session.cookie_header(),
            "user-agent": USER_AGENT,
            "referer": self._current_page_url or "",
            "x-requested-with": "XMLHttpRequest",
        }
        ctype = api_def.content_type  # ★ GET → None(不带 content-type)
        if ctype:
            headers["content-type"] = ctype
        return headers

    def build_kwargs(self, api_def: ApiDefCfg, values: dict[str, Any]) -> dict[str, Any]:
        subs = self.build_subs(values)
        url = self.apply_placeholders(str(api_def.url), subs)
        params = self.substitute_deep(api_def.params, subs) if isinstance(api_def.params, dict) else None

        kwargs: dict[str, Any] = {"params": params}
        if api_def.method == "POST":
            body = self.substitute_deep(api_def.body, subs) if api_def.body is not None else None
            # ★ body_by_window:按窗口覆盖 body 字段(键 = 窗口中文名,浅覆盖顶层)
            win_override = (api_def.body_by_window or {}).get(values.get("window")) or {}
            if win_override and isinstance(body, dict):
                body = {**body, **win_override}
            if body is None:
                pass
            elif isinstance(body, str):
                kwargs["content"] = body
            else:
                kwargs["json_body"] = body
        return {"url": url, **kwargs}

    # ==================================================================
    # 登录失效三路检测
    # ==================================================================

    @staticmethod
    def detect_login_failure(attempt: HttpAttempt, api_name: str) -> None:
        """登录失效检测。前两路**逐字继承**,第三路在中文标记之外补了**鉴权错误标记**。

        三路:

          1. ``401/403``;
          2. ``301/302/303/307/308`` 且 ``Location`` 含登录页标记;
          3. 响应体含失效标记 —— 中文三连(未登录/请重新登录/登录失效)
             **外加** :data:`~hoteldata.domains.session.status.AUTH_ERROR_MARKERS`。

        ★ 为什么第 3 路必须扩:平台**会话失效时返回 HTTP 200**,正文是
        ``{"error": "invalid_grant", "error_description": "authorized fail!"}``。
        中文标记一个都不匹配 → 这条响应会被当成"成功响应但没提取到字段",
        最终落 ``degraded`` 而不是 ``failed`` / 不触发重登 —— 又是一次静默降级。
        """
        code = attempt.status_code or 0
        if code in (401, 403):
            raise LoginExpiredError(f"接口 {api_name} 返回 {code},登录态失效")
        if code in (301, 302, 303, 307, 308):
            location = (attempt.location() or "").lower()
            if any(m in location for m in LOGIN_URL_MARKERS):
                raise LoginExpiredError(f"接口 {api_name} 被重定向到登录页({attempt.location()}),登录态失效")
        text = attempt.text or ""
        if text:
            for marker in LOGIN_BODY_MARKERS:
                if marker in text:
                    raise LoginExpiredError(f"接口 {api_name} 响应体含「{marker}」,登录态失效")
            hit = looks_like_auth_error(text)
            if hit is not None:
                raise LoginExpiredError(f"接口 {api_name} 响应体含鉴权失败标记「{hit}」,登录态失效")

    # ==================================================================
    # 单接口重放
    # ==================================================================

    async def request_api(
        self,
        api_def: ApiDefCfg,
        values: dict[str, Any],
        *,
        page_name: str,
        module: str,
        window: str,
    ) -> ApiResponse:
        """重放一条接口。

        ★ **限频消费点在这里、且在守卫之后**:守卫失败 / ``need_record`` 跳过
        **都不消耗** 0.6s 配额(与旧系统"只有真发请求才 sleep"语义一致)。
        """
        rec = ApiResponse(name=api_def.name, ok=False)
        if api_def.need_record:
            rec.skipped = "need_record"
            rec.error = "接口未校准(need_record=true),按规则直接降级浏览器"
            return rec

        # ---- ★ 守卫(先于任何请求) ----
        self.guard_placeholders(api_def, values)

        built = self.build_kwargs(api_def, values)
        rec.url = built["url"]
        rec.method = api_def.method
        headers = self.build_headers(api_def)

        # ---- 限频(账号级串行 + 平台级 0.6s)----
        async with self.ctx.limiter.request(self.ctx.platform, self.ctx.account.alias):
            attempt = await self.ctx.http.request(
                api_def.method,
                built["url"],
                headers=headers,
                params=built.get("params"),
                json_body=built.get("json_body"),
                content=built.get("content"),
                timeout=self.ctx.api_timeout_s,
                follow_redirects=False,  # ★ 必须:要看到 302 才能判登录失效
            )

        rec.status_code = attempt.status_code
        rec.elapsed_ms = attempt.elapsed_ms
        rec.text = attempt.text or ""

        # ---- 登录失效检测(抛 LoginExpiredError → 上游重登 + 降级) ----
        self.detect_login_failure(attempt, api_def.name)

        if not attempt.is_success:
            rec.error = attempt.error or f"HTTP {attempt.status_code}"
            return rec

        rec.body = attempt.json()
        if rec.body is None:
            rec.error = "响应不是合法 JSON"
            return rec
        rec.ok = True

        # ---- 逐接口原始响应落盘(便于以后校准规则) ----
        if self.ctx.persist_raw:
            rec.raw_path = self._persist_raw(page_name, module, window, api_def.name, rec.body)
        return rec

    def _persist_raw(self, page_name: str, module: str, window: str, api_name: str, body: Any) -> str | None:
        try:
            path = self.ctx.layout.raw_api_path(
                self.ctx.hotel.name,
                self.ctx.collect_date,
                page_name,
                module,
                window,
                api_name,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            from hoteldata.infra.atomic import atomic_write_json

            atomic_write_json(path, body)
            rel = self.ctx.layout.to_relative(path)
            self._raw_written.append(rel)
            return rel
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响采集
            logger.warning("原始响应落盘失败 {}/{}: {}", module, api_name, exc)
            return None

    # ==================================================================
    # 子模块级重放
    # ==================================================================

    def _merge_window_ctx(self, window: str) -> dict[str, Any]:
        """★ 窗口上下文的**唯一合并点**。

        ``{**ctx, **window_date_ctx(window, ctx["date"])}`` —— 后者覆盖前者,
        所以 **``date`` 会被窗口末日覆盖**(采集日只在 ``today`` 键体现)。
        """
        base: dict[str, Any] = {
            "date": self.ctx.collect_date.strftime("%Y-%m-%d"),
            "rand": gen_rand(),
            "trace": gen_trace(),
            "hotel_id": self.ctx.multi_store_hotel_id(),
        }
        return {**base, **window_date_ctx(window, self.ctx.collect_date)}

    def _collect_matched_defs(self, page_cfg: PageCfg, entry: ApiMatchCfg) -> list[ApiDefCfg]:
        """★ ``apis[].match`` → 匹配的 api_defs。

        ``match`` 有**两种写法**(实测):**定义名**(大小写不敏感、双向包含)
        或 **URL 片段**。只按名字查会漏掉后者 —— 统一交给
        :func:`~hoteldata.domains.collect.rules.match_resolves` 判。
        """
        return [d for d in page_cfg.api_defs if match_resolves(entry.match, [d])]

    async def collect_sub_module(
        self,
        page_name: str,
        page_cfg: PageCfg,
        sub_module: SubModuleCfg,
        window: str,
    ) -> ExtractResult:
        """★ 采集一个「模块 × 窗口」。**API 通道只产 ``ok`` / ``degraded`` / ``no_data``。**

        ``failed`` **仅**由浏览器兜底通道产出(继承语义)。
        """
        window = normalize_window(window)
        self._current_page_url = self.page_referer(page_cfg)
        self._current_multi_param = (
            (page_cfg.multi_store_param or "hotelId") if self.ctx.account.is_multi else None
        )
        values = self._merge_window_ctx(window)

        matched_any = False
        need_record_only = True
        responses: list[ApiResponse] = []
        payload: dict[str, Any] = {}
        ok_responses: list[ApiResponse] = []

        for entry in sub_module.apis:
            defs = self._collect_matched_defs(page_cfg, entry)
            if not defs:
                continue
            matched_any = True
            for api_def in defs:
                if not api_def.need_record:
                    need_record_only = False
                rec = await self.request_api(
                    api_def,
                    values,
                    page_name=page_name,
                    module=sub_module.name,
                    window=window,
                )
                responses.append(rec)
                if rec.ok:
                    ok_responses.append(rec)
                    for fld in entry.fields:
                        value = json_get(rec.body, fld.path)
                        if value is not None and fld.label not in payload:
                            payload[fld.label] = value

        # ---- 硬失败三条(降级浏览器) ----
        if not matched_any:
            raise _no_match_error(page_name, sub_module, page_cfg)
        if need_record_only:
            raise NeedRecordError(
                f"[{page_name}|{sub_module.name}|{window}] 全部接口 need_record=true"
                "(未校准),直接降级浏览器兜底"
            )
        if not ok_responses:
            raise _all_failed_error(page_name, sub_module, window, responses)

        failed_count = sum(1 for r in responses if not r.ok and not r.skipped)
        no_data_paths = _no_data_paths(sub_module)

        # ---- 四态判定(逐字继承) ----
        status: str
        note = ""
        if payload:
            status = "ok"
            if failed_count:
                status = "degraded"
                note = f"{failed_count} 个接口请求失败(部分降级)"
        else:
            no_data = False
            if no_data_paths:
                for r in ok_responses:
                    for p in no_data_paths:
                        value = json_get(r.body, p)
                        if value is not None and is_empty_value(value):
                            no_data = True
                            break
                    if no_data:
                        break
            else:
                # 兜底:所有成功响应体都为空才算「明确无数据」
                no_data = all(is_empty_value(r.body) for r in ok_responses)
            if no_data:
                status, note = "no_data", "接口正常返回但无数据(明确无数据)"
            else:
                status, note = "degraded", "接口已响应但未提取到字段(疑似结构变动)"

        raw_path = await self._persist_module_raw(page_name, sub_module.name, window, responses)
        if status != "ok":
            logger.warning("[{}|{}|{}] API 通道 {}: {}", page_name, sub_module.name, window, status, note)

        return ExtractResult(
            status=status,  # type: ignore[arg-type]
            channel="api",
            payload=payload,
            error=note or None,
            raw_path=raw_path,
            target=_target(page_name, sub_module.name, window),
            detail={
                "responses": [r.brief() for r in responses],
                "failed_count": failed_count,
                "apifields_extracted": len(payload),
                "note": note,
            },
        )

    async def _persist_module_raw(
        self,
        page_name: str,
        module: str,
        window: str,
        responses: list[ApiResponse],
    ) -> str | None:
        """模块级原始响应落盘(把逐接口记录汇总成一份,便于人工核对)。"""
        if not self.ctx.persist_raw:
            return None
        try:
            path = self.ctx.layout.raw_json_path(
                self.ctx.hotel.name, self.ctx.collect_date, page_name, module, window
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            from hoteldata.infra.atomic import atomic_write_json

            atomic_write_json(
                path,
                {
                    "hotel": self.ctx.hotel.name,
                    "hotel_id": self.ctx.hotel_id,
                    "account": self.ctx.account.alias,
                    "page": page_name,
                    "module": module,
                    "window": window,
                    "collect_date": self.ctx.collect_date.isoformat(),
                    "collected_at": datetime.now().isoformat(timespec="seconds"),
                    "responses": [{**r.brief(), "body": r.body if r.ok else None} for r in responses],
                },
            )
            return self.ctx.layout.to_relative(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("模块原始响应落盘失败 {}/{}: {}", module, window, exc)
            return None

    # ==================================================================
    # 上下文构造
    # ==================================================================

    def window_ctx_preview(self, window: str) -> dict[str, Any]:
        """给 CLI/测试看窗口展开结果。"""
        return self._merge_window_ctx(window)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _target(page: str, module: str, window: str):  # noqa: ANN202
    from hoteldata.domains.collect.contract import ExtractTarget

    return ExtractTarget(page=page, module=module, window=window)


def _no_data_paths(sub_module: SubModuleCfg) -> list[str]:
    """``no_data_path`` 支持字符串或字符串列表(**当前 24 个模块均未使用**)。

    保留支持是为了新模块可以声明"这类空值是明确无数据"。
    """
    raw = getattr(sub_module, "no_data_path", None) or []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw]


def _no_match_error(page_name: str, sub_module: SubModuleCfg, page_cfg: PageCfg) -> Exception:
    from hoteldata.domains.collect.contract import ApiCollectError

    return ApiCollectError(
        f"[{page_name}|{sub_module.name}] 未匹配到任何 api_def:"
        f"match 列表={[a.match for a in sub_module.apis]},"
        f"该页共 {len(page_cfg.api_defs)} 条定义(视为未校准,降级浏览器兜底)"
    )


def _all_failed_error(
    page_name: str,
    sub_module: SubModuleCfg,
    window: str,
    responses: list[ApiResponse],
) -> Exception:
    from hoteldata.domains.collect.contract import ApiCollectError

    detail = "; ".join(f"{r.name}:{r.error or r.status_code}" for r in responses[:4] if not r.ok)
    return ApiCollectError(
        f"[{page_name}|{sub_module.name}|{window}] 接口全部失败({len(responses)} 条):{detail}(降级浏览器兜底)"
    )


def collect_date_of(value: str | date | None = None) -> date:
    from hoteldata.domains.collect.windows import parse_collect_date

    return parse_collect_date(value)
