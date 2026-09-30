"""``domains/review`` —— 点评交互域(段2 批次 F,T2F.1–T2F.4)。

**职责边界(计划书 §1.2 / §1.3)**
================================

段2 **只消费**段1 取进来的点评数据(``review_reviews`` / ``review_materials``),
**不重写任何提取逻辑**。本域唯一的"取数"动作是"刷新待回复列表",
它惰性调用段1 的 :class:`~hoteldata.domains.collect.review.ReviewExtractor`
(见 :meth:`hoteldata.domains.review.service.ReviewService.realtime`)。

**模块地图**
============

==========================  ==========================================================
模块                         职责
==========================  ==========================================================
:mod:`~hoteldata.domains.review.policy`      T2F.1 决策:三层优先级 / 情感阈值 / 门控 / ``submit_ready``
:mod:`~hoteldata.domains.review.templates`   T2F.1 渲染:占位符填充 + 草稿文案(残留花括号清空)
:mod:`~hoteldata.domains.review.draft`       T2F.2 建议草稿 + **append-only 审计的唯一写入口**
:mod:`~hoteldata.domains.review.autoreply`   T2F.3 自动回复门控 + ``mark_replied``(**唯一**写 ``replied`` 处)
:mod:`~hoteldata.domains.review.analysis`    T2F.4 点评分析日报(四块;无素材跳过)
:mod:`~hoteldata.domains.review.service`     编排:任务与群命令的唯一入口
==========================  ==========================================================

**本域必须守住的口径(写错就全盘错,V55–V58 专测)**
==================================================

① **append-only**:``review_replies`` 状态流转**新增一行,不 UPDATE**;
   ★ **先落审计行再推送**(崩溃时至少留下"打算做什么"的证据)。
   实现:``draft.write_audit_row``(只有 INSERT)、``draft.draft_for_hotel_detailed``、
   ``autoreply.autorun``。
② **两套口径**:差评店级 ``silent`` → ``status='ignored'`` + ``reviews.replied=1``
   (**业务决定"不回复"= 已处理完**);自动回复失败 → ``status='failed'`` +
   ``strategy='auto_failed'`` + ``reviews.replied=0``(**技术失败,仍在待回复池**)。
   把 ``failed`` 标成 ``replied=1`` 会让点评**永远消失**。
③ **情感阈值**:``star>=4`` → good、``star<=3`` → bad、**无星级 → unknown**(可用
   ``score.commentLevel`` 兜底);★ ``unknown`` **永不自动回复**。
④ **策略优先级**:``core_hotels.review_policy``(群命令写)> ``review_templates.json``
   的 ``overrides`` > 默认(好评 ``g01``;差评 ``silent`` = 统一不回复,``template`` = 出草稿);
   ★ **差评一律不进自动**。
⑤ **自动回复门控** = ``settings.review.auto_enabled`` + ``auto.enabled`` + 灰度白名单 +
   ``submit.ready``/``api.url``;★ **``submit.ready`` 当前必然为 false**(提交接口从未被捕获,
   旧 ``docs/回复通道研究结论.md``)→ 自动回复长期走「未就绪 → 人工队列」,
   **明确提示 + 绝不伪造成功**(V57)。
⑥ **整店回复间隔 ≥ 120 秒**(``rules.reply_interval_s``);**失败当日不重试**。

> 另:点评/酒店的策略与状态是**可变状态**,一律落 PostgreSQL 或 ``var/``,
> 🚫 **绝不写回 ``config/``**(``settings.py`` 禁令 2)。
"""

from __future__ import annotations

__all__: list[str] = []
