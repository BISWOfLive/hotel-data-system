"""登录巡检(T2.6)—— 02:30 逐个探测 → 失效入重登队列 + 写 ``ops_login_events``。

要点(段1 T2.6):

  * 活跃账号逐个 ``probe_valid``(**异常按失效处理**);
  * 失效 → 触发重登 + 写 ``ops_login_events``(``action='expire'`` 然后 ``'relogin'``);
  * **有效但 ``need_renewal`` 也入队**(主动续登,阈值 20 天);
  * ★ **串行执行** —— 登录动作**不能并发**(任务书的"串行约束");
  * 轻量 HTTP 探活免开浏览器:300 账号串行只需约 **25 分钟**
    (300 × (探活 ~5s + 限频 0.6s) ≈ 28 分钟,在 02:30–03:30 窗口内)。

> 批次 D 的 `collect.*` 任务与本模块共用同一套 `job_runs` + advisory lock 机制。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from hoteldata.domains.session.status import SessionState, account_status_for
from hoteldata.infra.models import Account, LoginEvent
from hoteldata.infra.session_store import SessionHandle

__all__ = ["PatrolReport", "patrol_once"]


@dataclass(slots=True)
class PatrolReport:
    """巡检结果。"""

    checked: int = 0
    valid: int = 0
    invalid: int = 0
    unknown: int = 0
    renewed: int = 0
    relogged: int = 0
    relogin_failed: int = 0
    missing: list[str] = field(default_factory=list)
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "valid": self.valid,
            "invalid": self.invalid,
            "unknown": self.unknown,
            "renewed": self.renewed,
            "relogged": self.relogged,
            "relogin_failed": self.relogin_failed,
            "missing": self.missing[:20],
            "details": self.details[:50],
        }


async def patrol_once(
    runtime: Any,
    *,
    alias: str | None = None,
    do_relogin: bool = True,
    do_renew: bool = True,
) -> PatrolReport:
    """★ 串行巡检一遍(登录动作不能并发)。"""
    report = PatrolReport()
    manager = runtime.login()
    db = runtime.db

    async with db.session() as s:
        from sqlalchemy import select

        stmt = select(Account).order_by(Account.alias)
        if alias:
            stmt = stmt.where(Account.alias == alias)
        accounts = list((await s.execute(stmt)).scalars().all())

    for acc in accounts:
        handle = runtime.sessions.handle(acc.platform, "ebooking", acc.alias)
        report.checked += 1
        if not handle.exists():
            report.missing.append(acc.alias)
            await _write_event(db, acc.id, "patrol", "fail", "登录态文件缺失")
            continue

        state = await manager.check(handle, probe=True)
        if state is SessionState.VALID:
            report.valid += 1
        elif state is SessionState.INVALID:
            report.invalid += 1
            await _write_event(db, acc.id, "expire", "fail", "探活判定失效")
        else:
            report.unknown += 1

        # 失效 → 重登
        if state is SessionState.INVALID and do_relogin:
            credentials = _credentials_for(runtime, acc)
            attempt = await manager.auto_login(handle, credentials, interactive=False)
            await _write_event(
                db,
                acc.id,
                "relogin",
                "ok" if attempt.ok else "fail",
                f"{attempt.code}:{attempt.detail}",
            )
            if attempt.ok:
                report.relogged += 1
                state = await manager.check(handle, probe=True)
            else:
                report.relogin_failed += 1

        # 有效但超期 → 续登
        elif state is SessionState.STALE and do_renew:
            credentials = _credentials_for(runtime, acc)
            ok = await manager.renew(handle, credentials)
            await _write_event(db, acc.id, "renew", "ok" if ok else "fail", "主动续登(超 20 天)")
            if ok:
                report.renewed += 1

        # 回写账号长期态
        new_status = account_status_for(state, acc.status)
        if new_status != acc.status:
            async with db.session() as s:
                row = await s.get(Account, acc.id)
                if row is not None:
                    row.status = new_status
                    row.last_check_at = datetime.now()

        report.details.append(
            {
                "alias": acc.alias,
                "platform": acc.platform,
                "state": str(state),
                "renew_needed": manager.need_renewal(handle),
            }
        )
        logger.info(
            "巡检 {}: state={} need_renewal={}",
            acc.alias,
            state,
            manager.need_renewal(handle),
        )

    logger.info(
        "巡检完成:检查 {} 个,有效 {},失效 {},续登 {},重登成功 {},重登失败 {}",
        report.checked,
        report.valid,
        report.invalid,
        report.renewed,
        report.relogged,
        report.relogin_failed,
    )
    return report


async def _write_event(db: Any, account_id: int | None, action: str, result: str, detail: str) -> None:
    try:
        async with db.session() as s:
            s.add(LoginEvent(account_id=account_id, action=action, result=result, detail=detail))
    except Exception as exc:  # noqa: BLE001
        logger.debug("写登录事件失败: {}", exc)


def _credentials_for(runtime: Any, account: Account) -> tuple[str, str] | None:
    """解密账号凭据(本模块只读;``crypto`` 是基础设施层)。"""
    try:
        from hoteldata.infra.crypto import get_cipher

        cipher = get_cipher(runtime.settings)
        return (cipher.decrypt(account.username_enc), cipher.decrypt(account.password_enc))
    except Exception as exc:  # noqa: BLE001
        logger.debug("账号 {} 凭据不可用: {}", account.alias, exc)
        return None


__all__ += ["SessionHandle", "SessionState"]
