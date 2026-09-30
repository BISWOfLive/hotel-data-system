"""SQLAlchemy 2 模型包。

段1 建表(7 张,首迁移 ``0001_segment1_init``):

  ① :class:`~hoteldata.infra.models.core.Account`      ``core_accounts``
  ② :class:`~hoteldata.infra.models.core.Hotel`        ``core_hotels``
  ③ :class:`~hoteldata.infra.models.core.Session`      ``sessions``   ★ 登录态唯一索引
  ④ :class:`~hoteldata.infra.models.collect.CollectReport`  ``collect_reports``
  ⑤ :class:`~hoteldata.infra.models.collect.CollectModule`  ``collect_modules``  ★ 核心产出
  ⑥ :class:`~hoteldata.infra.models.ops.LoginEvent`    ``ops_login_events``
  ⑦ :class:`~hoteldata.infra.models.ops.JobRun`        ``job_runs``   ★ 可观测地基

批次 D 追加 4 张(迁移 ``0002_batch_d_extractors``):见 :mod:`hoteldata.infra.models.extractors`。

段2 追加 6 张 + 1 列(迁移 ``0003_segment2_push``):

  ⑧ :class:`~hoteldata.infra.models.push.Bot`            ``core_bots``
  ⑨ :class:`~hoteldata.infra.models.push.GroupBinding`   ``core_group_bindings``
  ⑩ :class:`~hoteldata.infra.models.push.PushLog`        ``push_logs``  ★ ``bot_id`` 为 Text(D17)
  ⑪ :class:`~hoteldata.infra.models.alert.AlertState`    ``alert_states``  ★ streak 仅展示
  ⑫ :class:`~hoteldata.infra.models.alert.AlertLog`      ``alert_logs``  ★ 行身份 delivery_key(UNIQUE)+ 触发身份 trigger_key
  ⑬ :class:`~hoteldata.infra.models.review.ReviewReply`  ``review_replies``  ★ append-only
  ＋ ``core_hotels.review_policy``(群命令「点评策略」的落库处)

段3 追加 3 张(迁移 ``0005_segment3_compare``):

  ⑭ :class:`~hoteldata.infra.models.compare.CmpPriceTarget`      ``cmp_price_targets``
     ★ 取代旧系统两个 txt(``compare_hotels.txt`` + ``price_targets.txt``)
  ⑮ :class:`~hoteldata.infra.models.compare.CmpPriceComparison`  ``cmp_price_comparisons``
     ★ D8 修复:UNIQUE(含 slot)+ ``distance_km`` 真填 + ``price_scope`` + ``is_demo``
  ⑯ :class:`~hoteldata.infra.models.compare.CmpBatchRun`         ``cmp_batch_runs``
     ★ 每日一行 + UPSERT(旧系统同日 2 条 ``done``)
"""

from __future__ import annotations

from .admin import AUDIT_ACTIONS, AdminAudit
from .alert import (
    ALERT_ALL_RULE,
    ALERT_HOTEL_ENTITY,
    ALERT_STATE_STATUSES,
    AlertLog,
    AlertState,
)
from .base import Base
from .collect import EXTRACT_CHANNELS, EXTRACT_STATUSES, CollectModule, CollectReport
from .compare import (
    BATCH_STATUSES,
    TARGET_MODES,
    CmpBatchRun,
    CmpPriceComparison,
    CmpPriceTarget,
)
from .core import Account, Hotel, Session
from .extractors import (
    PORTAL_PAGES,
    REVIEW_KINDS,
    REVIEW_SENTIMENTS,
    AlertPortalColumn,
    AlertRoomState,
    ReviewMaterial,
    ReviewReview,
)
from .ops import (
    JOB_STATUSES,
    JOB_TRIGGERS,
    LOGIN_ACTIONS,
    LOGIN_RESULTS,
    JobRun,
    LoginEvent,
)
from .push import (
    BOT_STATUSES,
    PUSH_LOG_STATUSES,
    Bot,
    GroupBinding,
    PushLog,
)
from .review import (
    REPLY_EXECUTORS,
    REPLY_STATUSES,
    ReviewReply,
)

__all__ = [
    "ALERT_ALL_RULE",
    "ALERT_HOTEL_ENTITY",
    "ALERT_STATE_STATUSES",
    "AUDIT_ACTIONS",
    "AdminAudit",
    "BATCH_STATUSES",
    "BOT_STATUSES",
    "EXTRACT_CHANNELS",
    "EXTRACT_STATUSES",
    "JOB_STATUSES",
    "JOB_TRIGGERS",
    "LOGIN_ACTIONS",
    "LOGIN_RESULTS",
    "PORTAL_PAGES",
    "PUSH_LOG_STATUSES",
    "REPLY_EXECUTORS",
    "REPLY_STATUSES",
    "REVIEW_KINDS",
    "REVIEW_SENTIMENTS",
    "TARGET_MODES",
    "Account",
    "AlertLog",
    "AlertPortalColumn",
    "AlertRoomState",
    "AlertState",
    "Base",
    "Bot",
    "CmpBatchRun",
    "CmpPriceComparison",
    "CmpPriceTarget",
    "CollectModule",
    "CollectReport",
    "GroupBinding",
    "Hotel",
    "JobRun",
    "LoginEvent",
    "PushLog",
    "ReviewMaterial",
    "ReviewReply",
    "ReviewReview",
    "Session",
]
