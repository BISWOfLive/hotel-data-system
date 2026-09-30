"""配置层 —— 替代旧系统 181 行手搓 ``os.getenv``。

**配置的三处来源边界**(总纲 7.6,段1 只碰前两处)
================================================================

===============  ==========================================  ==========================
来源             只放什么                                    例子
===============  ==========================================  ==========================
``.env``         部署态:连接串、端口、时区、总开关、限频参数     ``DB_URL`` ``TZ``
``config/*.json``  领域规则(只增不改义)                        ``api_rules.json``
PostgreSQL       业务实体与可变状态                            账号、酒店、登录态、job_runs
===============  ==========================================  ==========================

**三条禁令**
  1. 🚫 时刻表不进 ``.env`` —— 收进 ``infra/tasks.py`` 的任务注册表;
     ``.env`` 只保留 ``*_ENABLED``。段1 阶段 ``*_TIME`` 临时保留(附录 B 的
     刻意过渡安排),**M6 收尾时全部迁入任务注册表**。
  2. 🚫 可变状态不进 ``config/`` —— 落库。
  3. 🚫 诊断证据不进 ``config/`` —— 归 ``docs/参考/旧系统/``。

**启动即校验**:字段值在构造时校验(DB URL 必须是 asyncpg 方言、时区必须可解析、
重试退避必须是递增正数),连不上 DB 由 :meth:`hoteldata.runtime.Runtime.create` 直接失败,
**不静默降级**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cached_property, lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# 项目根定位
# ---------------------------------------------------------------------------

_ROOT_MARKERS = ("pyproject.toml", "alembic.ini")


def find_project_root(start: Path | None = None) -> Path:
    """向上查找项目根(含 ``pyproject.toml``),找不到则退回当前工作目录。"""
    cur = (start or Path(__file__).resolve()).resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in (cur, *cur.parents):
        if any((candidate / m).exists() for m in _ROOT_MARKERS):
            return candidate
    return Path.cwd().resolve()


PROJECT_ROOT: Path = find_project_root()
ENV_FILE: Path = PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# 分组视图(冻结 dataclass —— 只读,不可运行期改配置)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DbSettings:
    """数据层。"""

    url: str
    echo: bool
    pool_size: int
    max_overflow: int
    pool_timeout_s: float
    statement_timeout_ms: int


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Web 接入层。"""

    host: str
    port: int
    #: ★ 后台总开关(关掉后所有 /admin 路由 404,只留 /healthz 与 /status)
    admin_enabled: bool
    #: 统一口令的 **PBKDF2 哈希**(不是明文、不是明文摘要)
    admin_password_hash: str
    #: 会话 cookie 签名密钥(留空则退回 config/secret.key)
    session_secret: str
    #: 会话有效期(小时)
    session_hours: int
    #: 是否允许非本机访问(默认 False = 只监听 127.0.0.1 且拒绝外部 Host)
    allow_remote: bool

    @property
    def has_password(self) -> bool:
        """是否已设口令。**没设口令时后台拒绝一切登录**(不是"默认放行")。"""
        return bool(self.admin_password_hash.strip())


@dataclass(frozen=True, slots=True)
class CollectSettings:
    """提取域。

    ⚠️ 9 个窗口名的**单一事实源**在 :mod:`hoteldata.domains.collect.windows`
    (A12 级遗产),这里只保留"取哪些窗口"的行为开关,不重复定义窗口名
    —— 否则立刻产生第二个来源。
    """

    rotation_enabled: bool
    all_windows: bool

    @property
    def window_mode(self) -> str:
        return "all" if self.all_windows else "default"


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    """两层限频(风控核心)。"""

    interval_s: float
    max_concurrent_accounts: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class RetrySettings:
    """重试策略:仅幂等 GET 重试,POST(SOA 查询)不重试。"""

    times: int
    backoff_s: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """浏览器与风控参数组(B3 遗产)。"""

    headless: bool
    max_contexts: int
    chrome_cdp_url: str
    cdp_connect_timeout_s: float
    channel: str
    nav_timeout_s: float
    page_ready_timeout_s: float
    chart_ready_wait_s: float
    viewport_width: int
    viewport_height: int
    user_agent: str
    locale: str
    timezone_id: str
    disabled_features: str
    slow_mo_ms: int


@dataclass(frozen=True, slots=True)
class LoginSettings:
    """登录管家。"""

    max_age_days: int
    manual_wait_s: float
    check_timeout_s: float
    renew_enabled: bool


@dataclass(frozen=True, slots=True)
class ScreenshotSettings:
    """截图通道。"""

    jpeg_quality: int
    max_bytes: int
    enabled: bool


@dataclass(frozen=True, slots=True)
class OpsSettings:
    """运维域。"""

    backup_retention_days: int
    data_retention_days: int
    screenshot_retention_days: int
    disk_min_free_gb: float


@dataclass(frozen=True, slots=True)
class VisionSettings:
    """视觉模型(**默认关闭**)。

    🔴 合规前置条件:GLM-4.6V 是第三方云服务,启用 = 把客户酒店经营数据截图
    外发到智谱服务器。300 家代运营场景下**必须取得客户书面授权**后才能启用
    (总纲 R11)。
    """

    enabled: bool
    api_key: str
    model: str
    daily_call_limit: int
    #: 视觉价与 DOM 价偏差超过这个比例 → 标"待人工确认"(段3 T3C.5;默认 0.2 = 20%)
    price_tolerance: float


@dataclass(frozen=True, slots=True)
class PathSettings:
    """运行期目录。"""

    project_root: Path
    config_dir: Path
    var_dir: Path
    secret_key_file: Path


# ---------------------------------------------------------------------------
# 段2 分组视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotSettings:
    """企微智能机器人(协议常量,**逐字继承 A1 级遗产,不许调整**)。"""

    enabled: bool
    ws_url: str
    subscribe_timeout_s: float
    heartbeat_s: float
    heartbeat_timeout_s: float
    max_miss: int
    ack_timeout_s: float
    recv_timeout_s: float
    chunk_size: int
    max_chunks: int
    capacity_per_bot: int
    health_interval_s: float


@dataclass(frozen=True, slots=True)
class PushSettings:
    """推送派发器(限频 / 重试 / 去重 / 图片上限 / 长文拆分)。"""

    min_interval_s: float
    retry_times: int
    retry_backoff_s: tuple[float, ...]
    max_images: int
    merge_limit_chars: int
    manage_chatids: tuple[str, ...]
    ops_chatid: str

    def is_manage(self, chatid: str) -> bool:
        """管理群判定。**未配置 MANAGE_CHATIDS 时一律拒绝**(不是放行)。"""
        return bool(chatid) and chatid in self.manage_chatids


@dataclass(frozen=True, slots=True)
class AlertSettings:
    """预警域总开关。

    🚫 时刻表**不在**这里:``alert.room`` / ``alert.data`` / ``alert.summary``
    的 cron 全部写在 :mod:`hoteldata.jobs` 的任务注册表里(总纲 7.5「时刻表唯一来源」)。
    """

    enabled: bool
    room_slot_names: tuple[str, ...]
    data_slot_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    """点评交互域开关。"""

    suggest_enabled: bool
    analysis_enabled: bool
    auto_enabled: bool
    realtime_enabled: bool
    realtime_interval_min: int


@dataclass(frozen=True, slots=True)
class OpsPushSettings:
    """段2 运维推送(自检推送 / 违约实时监听)。"""

    selfcheck_push_enabled: bool
    violation_enabled: bool
    violation_interval_min: int


# ---------------------------------------------------------------------------
# 段3 分组视图
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompareSettings:
    """比价域(段3)。

    🚫 **时刻表不在**这里:``compare.batch`` / ``compare.collect`` / ``compare.push``
    的 cron 全部写在 :mod:`hoteldata.jobs` 的任务注册表里(总纲 §7.5「时刻表唯一来源」)。

    ★ **只有一个 ``headless``**(段3 P4 修订)
    ==========================================

    计划书附录 A 原本同时列了 ``HOTEL_HEADLESS``(交互有头)与
    ``HOTEL_BATCH_HEADLESS``(批量无头)。**后者无法实现**:
    ``infra/browser.py:146/158`` 的 ``headless`` 是 **Chromium 进程级**
    (``launch(headless=...)``),**不是 context 级** —— 一个 :class:`BrowserPool`
    只有一个 headless 值,两种模式在同一个进程里不可共存。

    所以段3 **只保留一个开关**。这不是妥协:定时任务跑在 ``serve`` 进程里,
    天然与交互式 CLI 分离,实际使用中不需要同时有头又无头。
    """

    enabled: bool
    platforms: tuple[str, ...]
    nearby_count: int
    quote_count: int
    rank_mode: str
    headless: bool
    city: str
    coord_fallback_max: int
    timeout_s: float
    batch_push: bool
    demo_prefix: str

    @property
    def by_geo(self) -> bool:
        """``HOTEL_RANK_MODE=geo`` → 按距离升序(★ D13 的语义所在)。"""
        return self.rank_mode == "geo"



# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """全量配置。

    字段名与 ``.env`` 中的键名**逐字一致**(大小写不敏感),
    分组视图由同名 property 暴露 —— 既满足"分组子模型",又不引入
    嵌套前缀,保证 ``.env`` 键名与段1 附录 B 完全对齐。
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # ---- 通用 ----
    tz: str = Field(default="Asia/Shanghai", description="进程时区(Windows 需 tzdata)")
    debug: bool = Field(default=False, description="调试模式(日志更详细)")

    # ---- 数据库 ----
    # ★ 必填:删掉即启动报错并指出字段名(V1 验收点)
    db_url: str = Field(
        ...,
        description="PostgreSQL 连接串,必须是 postgresql+asyncpg 方言",
    )
    db_echo: bool = Field(default=False, description="SQLAlchemy 回显 SQL")
    db_pool_size: int = Field(default=10, ge=1, le=100, description="连接池大小")
    db_max_overflow: int = Field(default=20, ge=0, le=200, description="连接池溢出上限")
    db_pool_timeout_s: float = Field(default=30.0, gt=0, description="取连接超时(秒)")
    db_statement_timeout_ms: int = Field(default=60_000, gt=0, description="单语句超时(毫秒)")

    # ---- Web ----
    host: str = Field(default="127.0.0.1", description="Web 监听地址")
    port: int = Field(default=8000, ge=1, le=65535, description="Web 监听端口")

    # ---- Web 后台(Phase 3;总纲 §7.8)----
    admin_enabled: bool = Field(default=True, description="后台总开关(0 = /admin 全部 404)")
    admin_password_hash: str = Field(
        default="",
        description=(
            "★ 后台统一口令的 PBKDF2 哈希(不是明文)。"
            "生成:`hoteldata admin passwd --set <新口令>`。"
            "**留空 = 后台拒绝一切登录**(安全默认,不是放行)"
        ),
    )
    admin_session_secret: str = Field(
        default="",
        description="会话 cookie 签名密钥;留空则退回 config/secret.key(Fernet 密钥)",
    )
    admin_session_hours: int = Field(default=12, ge=1, le=24 * 30, description="会话有效期(小时)")
    admin_allow_remote: bool = Field(
        default=False,
        description="★ 是否允许非本机访问后台。默认 0 = 只允许 127.0.0.1/::1(与本机部署形态一致)",
    )

    # ---- 路径 ----
    config_dir: Path = Field(default=PROJECT_ROOT / "config", description="config/ 目录")
    var_dir: Path = Field(default=PROJECT_ROOT / "var", description="运行时产物目录")
    secret_key_file: Path = Field(
        default=PROJECT_ROOT / "config" / "secret.key",
        description="Fernet 密钥文件(不进 git)",
    )

    # ---- 提取 ----
    collect_rotation_enabled: bool = Field(default=True, description="轮换提取总开关")
    collect_all_windows: bool = Field(default=False, description="默认是否取全部 9 窗口")

    # ---- 风控 / 限频(B3 参数组,逐字继承)----
    rate_limit_enabled: bool = Field(default=True, description="平台级全局限频开关")
    rate_limit_interval_s: float = Field(
        default=0.6, gt=0, description="单请求最小间隔(秒)—— 风控红线,唯一消费点 infra/rate_limit.py"
    )
    max_concurrent_accounts: int = Field(default=4, ge=1, le=32, description="同时在跑的账号数上限")
    api_timeout_s: float = Field(default=20.0, gt=0, description="单接口超时(秒)")
    retry_times: int = Field(default=3, ge=0, le=10, description="重试次数(共 times+1 次尝试)")
    retry_backoff_s: str = Field(default="2,8,30", description="重试退避秒数,逗号分隔")

    # ---- 浏览器 ----
    headless: bool = Field(default=False, description="无头模式;交互默认有头")
    max_contexts: int = Field(default=4, ge=1, le=32, description="同时浏览器 context 上限")
    chrome_cdp_url: str = Field(default="", description="CDP 地址,空则本地启动")
    cdp_connect_timeout_s: float = Field(default=15.0, gt=0, description="connect_over_cdp 超时(秒)")
    browser_channel: str = Field(default="chrome", description="优先本机 Chrome,失败回退 Playwright Chromium")
    nav_timeout_s: float = Field(default=60.0, gt=0, description="页面导航超时(秒)")
    page_ready_timeout_s: float = Field(default=120.0, gt=0, description="数据就绪等待上限(秒)")
    chart_ready_wait_s: float = Field(default=8.0, gt=0, description="图表渲染等待(秒)")
    browser_viewport: str = Field(default="1600x1000", description="采集视口 WxH")
    browser_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        description="UA,与浏览器一致(Chrome/125)",
    )
    browser_locale: str = Field(default="zh-CN", description="locale")
    browser_slow_mo_ms: int = Field(default=0, ge=0, description="Playwright slow_mo(毫秒)")

    # ---- 登录 ----
    login_max_age_days: int = Field(default=20, ge=1, description="主动续登阈值(天)")
    manual_login_wait_s: float = Field(default=600.0, gt=0, description="人工登录/拖滑块等待上限(秒)")
    login_check_timeout_s: float = Field(default=45.0, gt=0, description="登录态探测超时(秒)")
    login_renew_enabled: bool = Field(default=True, description="主动续登开关")

    # ---- 截图 ----
    screenshot_enabled: bool = Field(default=True, description="截图任务总开关")
    screenshot_jpeg_quality: int = Field(default=70, ge=1, le=100, description="JPEG quality")
    screenshot_max_bytes: int = Field(default=102_400, description="单张告警阈值(字节,默认 100KB)")

    # ---- 运维 ----
    backup_retention_days: int = Field(default=7, ge=1, description="冷备保留天数")
    data_retention_days: int = Field(default=30, ge=1, description="raw/html 保留天数")
    screenshot_retention_days: int = Field(default=90, ge=1, description="截图保留天数(整目录删除线)")
    disk_min_free_gb: float = Field(default=20.0, ge=0, description="磁盘余量告警线(GB)")

    # ---- 视觉(默认关闭)----
    vision_enabled: bool = Field(default=False, description="★ 默认 0;启用前须客户书面授权")
    zhipu_api_key: str = Field(default="", description="智谱 API Key")
    zhipu_model: str = Field(default="glm-4.6v", description="模型名走配置,不硬编码")
    vision_daily_call_limit: int = Field(default=200, ge=0, description="日调用上限(R12 成本控制)")
    vision_price_tolerance: float = Field(
        default=0.2, gt=0, le=1, description="★ 视觉价与 DOM 价偏差 >此值 → 标待人工确认(段3 V75)"
    )

    # ==================================================================
    # 段2 · 企业微信推送
    # ==================================================================

    # ---- 机器人(协议常量,A1 级遗产逐字继承,不许调整默认值)----
    aibot_enabled: bool = Field(default=True, description="机器人长连接总开关")
    aibot_ws_url: str = Field(
        default="wss://openws.work.weixin.qq.com", description="企微智能机器人 WS 地址"
    )
    aibot_subscribe_timeout_s: float = Field(default=10.0, gt=0, description="订阅帧超时(秒)")
    aibot_heartbeat_s: float = Field(default=30.0, gt=0, description="心跳间隔(秒)")
    aibot_heartbeat_timeout_s: float = Field(default=6.0, gt=0, description="单次心跳超时(秒)")
    aibot_max_miss: int = Field(
        default=2, ge=1, le=10, description="★ 连丢几次判死强断重连(丢 1 次不重连)"
    )
    aibot_ack_timeout_s: float = Field(default=15.0, gt=0, description="ack 等待超时(秒)")
    aibot_recv_timeout_s: float = Field(default=20.0, gt=0, description="收帧超时(秒)")
    aibot_chunk_size: int = Field(default=512 * 1024, gt=0, description="素材分片大小(512KB)")
    aibot_max_chunks: int = Field(default=100, gt=0, description="素材分片上限(>100 片报错)")
    aibot_capacity_per_bot: int = Field(default=10, ge=1, description="每机器人建议承载群数(仅告警)")
    aibot_health_interval_s: float = Field(default=300.0, gt=0, description="健康巡检间隔(秒)")

    # ---- 推送 ----
    push_min_interval_s: float = Field(
        default=2.0, gt=0, description="★ 单机器人相邻发送最小间隔(秒);群级另有二次节流"
    )
    push_retry_times: int = Field(default=3, ge=0, le=10, description="推送重试次数(共 4 次尝试)")
    push_retry_backoff_s: str = Field(default="2,8,30", description="推送重试退避秒数,逗号分隔")
    push_max_images: int = Field(default=5, ge=0, le=20, description="每群每次附图上限")
    push_merge_limit_chars: int = Field(
        default=3500, ge=200, description="单条消息字数上限,超限**按店对半拆 ≤2 条**"
    )
    push_group_min_interval_s: float = Field(
        default=2.0, gt=0, description="群级二次节流(秒);与机器人级限频叠加"
    )
    manage_chatids: str = Field(default="", description="管理群 chatid,逗号分隔;未配置时管理命令一律拒绝")
    ops_chatid: str = Field(default="", description="运维告警群 chatid")

    # ---- 预警 ----
    alert_enabled: bool = Field(default=True, description="预警总开关")
    alert_room_slots: str = Field(
        default="09:00,14:30,19:00",
        description="★ 关房预警的**名义时刻**(业务语义层);调度层 cron 已 +4 分钟错峰",
    )
    alert_data_slots: str = Field(default="09:00", description="数据类预警的名义时刻")

    # ---- 点评 ----
    review_suggest_enabled: bool = Field(default=True, description="点评建议草稿")
    review_analysis_enabled: bool = Field(default=True, description="点评分析日报")
    review_auto_enabled: bool = Field(
        default=True, description="点评自动回复(★ 仍需 submit.ready + 灰度白名单双重门控)"
    )
    review_realtime_enabled: bool = Field(default=True, description="点评实时轮询回复")
    review_realtime_interval_min: int = Field(default=60, ge=1, description="实时轮询间隔(分钟)")

    # ---- 违约实时 ----
    ops_selfcheck_push: bool = Field(default=True, description="自检结果推运维群")
    violation_realtime_enabled: bool = Field(default=True, description="违约实时监听")
    violation_interval_min: int = Field(default=60, ge=1, description="违约监听间隔(分钟)")

    # ==================================================================
    # 段3 · 比价(迁移 0005;计划书附录 A + 段3 修订清单 P4/P5)
    # ==================================================================

    compare_enabled: bool = Field(default=True, description="比价域总开关")
    hotel_platforms: str = Field(
        default="ctrip,meituan", description="参与比价的平台,逗号分隔(未注册的平台名会直接报错)"
    )
    hotel_nearby_count: int = Field(
        default=3, ge=1, le=20, description="★ 锚点附近取几家酒店(HOTEL_NEARBY_COUNT)"
    )
    hotel_quote_count: int = Field(
        default=3,
        ge=1,
        le=10,
        description="每个平台展示/存档几条报价(旧 PRICE_ROOM_COUNT 的**真正含义**;见下方说明)",
    )
    hotel_rank_mode: str = Field(
        default="geo",
        description="★ geo=按距离升序(D13 的语义)/ platform=平台「附近酒店」原始顺序",
    )
    hotel_headless: bool = Field(
        default=False,
        description="★ 比价专用 headless(计划书 HOTEL_BATCH_HEADLESS 已删 —— 见 CompareSettings 文档)",
    )
    hotel_city: str = Field(default="", description="默认城市(清单行内 city 优先)")
    hotel_coord_fallback_max: int = Field(
        default=6,
        ge=0,
        le=50,
        description="★ 坐标缺失时逐个打开候选页的上限(旧 HOTEL_TOP_N_OPEN=6,旧系统零引用)",
    )
    hotel_compare_timeout_s: float = Field(
        default=90.0, gt=0, description="单店比价(双平台)超时(秒)—— 计划书 §2.3 的性能目标"
    )
    hotel_batch_push: bool = Field(
        default=False, description="批量跑完推汇总(旧 HOTEL_BATCH_PUSH,默认 0)"
    )
    compare_demo_prefix: str = Field(
        default="验收3-",
        description="演示模式酒店名前缀(与段1/段2 的 verify2- 同一纪律,便于识别与清理)",
    )


    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    @field_validator("db_url")
    @classmethod
    def _check_db_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("DB_URL 不能为空(参考 .env.example)")
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError(
                f"DB_URL 必须是 postgresql+asyncpg:// 方言,实际为 {v.split('://', 1)[0]}://..."
                "(同步驱动 psycopg2 会在 async 引擎下直接崩)"
            )
        return v

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - 环境相关
            raise ValueError(
                f"时区 {v!r} 无法解析。Windows 自带时区库不含 Asia/Shanghai 完整规则,"
                "请确认已安装 tzdata(`poetry install` 会带上)"
            ) from exc
        return v

    @field_validator("browser_viewport")
    @classmethod
    def _check_viewport(cls, v: str) -> str:
        parts = v.lower().replace("×", "x").split("x")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValueError(f"BROWSER_VIEWPORT 必须形如 1600x1000,实际为 {v!r}")
        return v

    @field_validator("retry_backoff_s", "push_retry_backoff_s")
    @classmethod
    def _check_backoff(cls, v: str) -> str:
        try:
            values = [float(x) for x in v.replace(" ", "").split(",") if x]
        except ValueError as exc:
            raise ValueError(f"RETRY_BACKOFF_S 必须是逗号分隔数字,实际为 {v!r}") from exc
        if any(x <= 0 for x in values):
            raise ValueError(f"RETRY_BACKOFF_S 必须全为正数,实际为 {v!r}")
        if values != sorted(values):
            raise ValueError(f"RETRY_BACKOFF_S 必须递增,实际为 {v!r}")
        return v

    @field_validator("alert_room_slots", "alert_data_slots")
    @classmethod
    def _check_slot_list(cls, v: str) -> str:
        """名义时刻列表:``09:00,14:30,19:00``。

        ★ 这是**业务语义层**的时刻(写进 ``alert_rules.json`` 的 ``check_times``),
        与调度层 cron(09:04/14:34/19:04)是两层 —— 合并成一层 slot 匹配会失效(V49)。
        """
        parts = [p.strip() for p in v.replace("，", ",").split(",") if p.strip()]
        if not parts:
            raise ValueError("预警名义时刻列表不能为空")
        for p in parts:
            hh, _, mm = p.partition(":")
            if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError(f"名义时刻必须是 HH:MM,实际为 {p!r}")
        return ",".join(parts)


    @field_validator("hotel_rank_mode")
    @classmethod
    def _check_rank_mode(cls, v: str) -> str:
        """★ 排序模式只有两个值。

        旧系统 ``HOTEL_RANK_MODE`` 拼错时**静默退化为平台顺序**
        (而且旧系统连"平台顺序"都没排,``distance_km`` 恒为 ``None``)——
        用户以为在看"距离最近",实际是平台推荐。这里**启动即失败**。
        """
        mode = (v or "").strip().lower()
        if mode not in ("geo", "platform"):
            raise ValueError(
                f"HOTEL_RANK_MODE 只能是 geo(按距离升序)或 platform(平台推荐顺序),实际为 {v!r}"
            )
        return mode

    @field_validator("hotel_platforms")
    @classmethod
    def _check_platforms(cls, v: str) -> str:
        """平台列表不能为空 —— 空 = 比价什么都不跑,是配置事故不是"没数据"。"""
        if not _split_csv(v):
            raise ValueError("HOTEL_PLATFORMS 不能为空(至少一个平台,如 ctrip 或 ctrip,meituan)")
        return v

    @model_validator(mode="after")
    def _check_cross(self) -> Settings:
        if self.screenshot_retention_days < self.data_retention_days:
            raise ValueError(
                "SCREENSHOT_RETENTION_DAYS 必须 >= DATA_RETENTION_DAYS:"
                "双保留期的语义是「[-90,-30) 天只删 raw/html、保留截图」,"
                "截图保留期短于数据保留期会让该语义失去意义"
            )
        if self.vision_enabled and not self.zhipu_api_key:
            raise ValueError(
                "VISION_ENABLED=1 但未配置 ZHIPU_API_KEY。"
                "⚠️ 启用视觉前必须取得客户书面授权(经营数据截图外发第三方云)"
            )
        return self

    # ------------------------------------------------------------------
    # 分组视图
    # ------------------------------------------------------------------

    @cached_property
    def db(self) -> DbSettings:
        return DbSettings(
            url=self.db_url,
            echo=self.db_echo,
            pool_size=self.db_pool_size,
            max_overflow=self.db_max_overflow,
            pool_timeout_s=self.db_pool_timeout_s,
            statement_timeout_ms=self.db_statement_timeout_ms,
        )

    @cached_property
    def web(self) -> WebSettings:
        return WebSettings(
            host=self.host,
            port=self.port,
            admin_enabled=self.admin_enabled,
            admin_password_hash=self.admin_password_hash.strip(),
            session_secret=self.admin_session_secret.strip(),
            session_hours=self.admin_session_hours,
            allow_remote=self.admin_allow_remote,
        )

    @cached_property
    def collect(self) -> CollectSettings:
        return CollectSettings(
            rotation_enabled=self.collect_rotation_enabled,
            all_windows=self.collect_all_windows,
        )

    @cached_property
    def rate_limit(self) -> RateLimitSettings:
        return RateLimitSettings(
            interval_s=self.rate_limit_interval_s,
            max_concurrent_accounts=self.max_concurrent_accounts,
            enabled=self.rate_limit_enabled,
        )

    @cached_property
    def retry(self) -> RetrySettings:
        backoff = tuple(float(x) for x in self.retry_backoff_s.replace(" ", "").split(",") if x)
        return RetrySettings(times=self.retry_times, backoff_s=backoff)

    @cached_property
    def browser(self) -> BrowserSettings:
        w, _, h = self.browser_viewport.lower().replace("×", "x").partition("x")
        return BrowserSettings(
            headless=self.headless,
            max_contexts=self.max_contexts,
            chrome_cdp_url=self.chrome_cdp_url.strip(),
            cdp_connect_timeout_s=self.cdp_connect_timeout_s,
            channel=self.browser_channel,
            nav_timeout_s=self.nav_timeout_s,
            page_ready_timeout_s=self.page_ready_timeout_s,
            chart_ready_wait_s=self.chart_ready_wait_s,
            viewport_width=int(w),
            viewport_height=int(h),
            user_agent=self.browser_user_agent,
            locale=self.browser_locale,
            timezone_id=self.tz,
            disabled_features="AutomationControlled",
            slow_mo_ms=self.browser_slow_mo_ms,
        )

    @cached_property
    def login(self) -> LoginSettings:
        return LoginSettings(
            max_age_days=self.login_max_age_days,
            manual_wait_s=self.manual_login_wait_s,
            check_timeout_s=self.login_check_timeout_s,
            renew_enabled=self.login_renew_enabled,
        )

    @cached_property
    def screenshot(self) -> ScreenshotSettings:
        return ScreenshotSettings(
            jpeg_quality=self.screenshot_jpeg_quality,
            max_bytes=self.screenshot_max_bytes,
            enabled=self.screenshot_enabled,
        )

    @cached_property
    def ops(self) -> OpsSettings:
        return OpsSettings(
            backup_retention_days=self.backup_retention_days,
            data_retention_days=self.data_retention_days,
            screenshot_retention_days=self.screenshot_retention_days,
            disk_min_free_gb=self.disk_min_free_gb,
        )

    @cached_property
    def vision(self) -> VisionSettings:
        return VisionSettings(
            enabled=self.vision_enabled,
            api_key=self.zhipu_api_key,
            model=self.zhipu_model,
            daily_call_limit=self.vision_daily_call_limit,
            price_tolerance=self.vision_price_tolerance,
        )

    @cached_property
    def paths(self) -> PathSettings:
        return PathSettings(
            project_root=PROJECT_ROOT,
            config_dir=Path(self.config_dir),
            var_dir=Path(self.var_dir),
            secret_key_file=Path(self.secret_key_file),
        )

    @cached_property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    # ---- 段2 ----

    @cached_property
    def bot(self) -> BotSettings:
        return BotSettings(
            enabled=self.aibot_enabled,
            ws_url=self.aibot_ws_url.strip(),
            subscribe_timeout_s=self.aibot_subscribe_timeout_s,
            heartbeat_s=self.aibot_heartbeat_s,
            heartbeat_timeout_s=self.aibot_heartbeat_timeout_s,
            max_miss=self.aibot_max_miss,
            ack_timeout_s=self.aibot_ack_timeout_s,
            recv_timeout_s=self.aibot_recv_timeout_s,
            chunk_size=self.aibot_chunk_size,
            max_chunks=self.aibot_max_chunks,
            capacity_per_bot=self.aibot_capacity_per_bot,
            health_interval_s=self.aibot_health_interval_s,
        )

    @cached_property
    def push(self) -> PushSettings:
        return PushSettings(
            min_interval_s=self.push_min_interval_s,
            retry_times=self.push_retry_times,
            retry_backoff_s=tuple(
                float(x) for x in self.push_retry_backoff_s.replace(" ", "").split(",") if x
            ),
            max_images=self.push_max_images,
            merge_limit_chars=self.push_merge_limit_chars,
            manage_chatids=_split_csv(self.manage_chatids),
            ops_chatid=self.ops_chatid.strip(),
        )

    @cached_property
    def alert(self) -> AlertSettings:
        return AlertSettings(
            enabled=self.alert_enabled,
            room_slot_names=_split_csv(self.alert_room_slots),
            data_slot_names=_split_csv(self.alert_data_slots),
        )

    @cached_property
    def review(self) -> ReviewSettings:
        return ReviewSettings(
            suggest_enabled=self.review_suggest_enabled,
            analysis_enabled=self.review_analysis_enabled,
            auto_enabled=self.review_auto_enabled,
            realtime_enabled=self.review_realtime_enabled,
            realtime_interval_min=self.review_realtime_interval_min,
        )

    @cached_property
    def ops_push(self) -> OpsPushSettings:
        return OpsPushSettings(
            selfcheck_push_enabled=self.ops_selfcheck_push,
            violation_enabled=self.violation_realtime_enabled,
            violation_interval_min=self.violation_interval_min,
        )

    # ---- 段3 ----

    @cached_property
    def compare(self) -> CompareSettings:
        return CompareSettings(
            enabled=self.compare_enabled,
            platforms=_split_csv(self.hotel_platforms),
            nearby_count=self.hotel_nearby_count,
            quote_count=self.hotel_quote_count,
            rank_mode=self.hotel_rank_mode.strip().lower(),
            headless=self.hotel_headless,
            city=self.hotel_city.strip(),
            coord_fallback_max=self.hotel_coord_fallback_max,
            timeout_s=self.hotel_compare_timeout_s,
            batch_push=self.hotel_batch_push,
            demo_prefix=self.compare_demo_prefix.strip(),
        )

    # ------------------------------------------------------------------

    def ensure_dirs(self) -> None:
        """创建运行期目录(幂等)。``config/`` 必须已存在,缺失即报错。"""
        if not self.paths.config_dir.exists():
            raise FileNotFoundError(
                f"config/ 目录不存在: {self.paths.config_dir}\n"
                "config/ 只放「运行规则」(api_rules.json / push_rotation.json),"
                "缺失说明项目未正确初始化"
            )
        for d in (
            self.paths.var_dir,
            self.paths.var_dir / "states",
            self.paths.var_dir / "raw",
            self.paths.var_dir / "screenshots",
            self.paths.var_dir / "logs",
            self.paths.var_dir / "backup",
            self.paths.var_dir / "reports",
        ):
            d.mkdir(parents=True, exist_ok=True)

    def safe_repr(self) -> str:
        """脱敏后的配置摘要(供启动日志)。"""
        return (
            f"db_url={self.db_url.split('@')[-1]} tz={self.tz} "
            f"rate_limit={self.rate_limit_interval_s}s headless={self.headless} "
            f"max_contexts={self.max_contexts} vision={'on' if self.vision_enabled else 'off'}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例(测试可用 ``get_settings.cache_clear()`` 重置)。"""
    return Settings()


def _split_csv(raw: str) -> tuple[str, ...]:
    """逗号分隔串 → 去空去重的元组(兼容中文逗号)。"""
    parts = [p.strip() for p in (raw or "").replace("，", ",").split(",") if p.strip()]
    seen: dict[str, None] = {}
    for p in parts:
        seen.setdefault(p, None)
    return tuple(seen)


def reload_settings(**overrides: object) -> Settings:
    """按需构造一份带覆盖的配置(测试/CLI 用),不影响单例。"""
    if overrides:
        return Settings(**overrides)  # type: ignore[arg-type]
    get_settings.cache_clear()
    return get_settings()


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "ENV_FILE",
    "PROJECT_ROOT",
    "AlertSettings",
    "BotSettings",
    "BrowserSettings",
    "CollectSettings",
    "CompareSettings",
    "DbSettings",
    "LoginSettings",
    "OpsPushSettings",
    "OpsSettings",
    "PathSettings",
    "PushSettings",
    "RateLimitSettings",
    "RetrySettings",
    "ReviewSettings",
    "ScreenshotSettings",
    "Settings",
    "VisionSettings",
    "WebSettings",
    "find_project_root",
    "get_settings",
    "reload_settings",
]
