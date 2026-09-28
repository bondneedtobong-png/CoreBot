"""Read-only, bounded-window report over persisted warmup activity."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from bot.config import (
    WARMUP_BASE_DELAY_SEC, WARMUP_DAILY_ACTION_LIMIT, WARMUP_ENABLED,
    WARMUP_JITTER_SEC,
)
from control_plane.business.db import get_bot_db
from control_plane.deps import get_current_user
from control_plane.models import User
from database.models import (
    Account, AccountSafetyState, AccountStatus, WarmupLog, WarmupProfile,
)
from utils.time import utcnow_naive
from services.warmup_schedule import (
    DEFAULT_TIME_ZONE, DEFAULT_WORK_START, DEFAULT_WORK_END,
    MIN_INTERVAL_SECONDS, is_off_hours, next_off_hours, parse_read_targets,
)


router = APIRouter(prefix="/business/warmup", tags=["warmup"])

_KNOWN_SKIP_REASONS = frozenset({
    "account_missing", "account_not_active", "auth_invalid", "contact_permission",
    "cooling_down", "daily_limit", "flood_wait", "manual_pause", "needs_reauth",
    "no_client", "no_targets", "peer_flood", "platform_restricted",
    "profile_disabled", "resume_verification_pending", "review_required", "slow_mode",
    "worker_unavailable",
})
_STATUSES = ("ok", "skip", "fail")


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _counts() -> dict[str, int]:
    return {"total": 0, "ok": 0, "skip": 0, "fail": 0}


def _status(value: str) -> str:
    if value == "ok":
        return "ok"
    if value == "skip":
        return "skip"
    # Historical runner failures use "error"; normalize them with "fail".
    return "fail"


def _action(value: str) -> str:
    if value == "skip_reaction":
        return "set_reaction"
    for prefix in ("pause_floodwait_", "pause_auth_"):
        if value.startswith(prefix):
            return value[len(prefix):] or "scheduler"
    if value in ("skip_profile_disabled", "pause_daily_limit"):
        return "scheduler"
    return value


def _reason(action: str, status: str, details: str | None) -> str | None:
    if status == "ok":
        return None
    if action == "skip_profile_disabled":
        return "profile_disabled"
    if action == "pause_daily_limit":
        return "daily_limit"
    if action.startswith("pause_floodwait_"):
        return "flood_wait"
    if action.startswith("pause_auth_"):
        return "auth_invalid"
    if status == "fail":
        return "unexpected_error"

    # Details can include targets or exception text. Accept only known codes.
    suffix = (details or "").strip()
    if suffix.startswith("delay="):
        suffix = suffix.partition(" ")[2].strip()
    token = suffix.split(maxsplit=1)[0] if suffix else ""
    if token.startswith("empty:"):
        return "empty_target"
    if token in _KNOWN_SKIP_REASONS:
        return token
    return "other_skip"


def _pause_reason(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("floodwait:"):
        return "flood_wait"
    if value == "daily_limit_reached":
        return "daily_limit"
    return "other_pause"


def _target_count(raw: str | None) -> int:
    """Count entries that the runner's target parser can consume, without returning them."""
    return len(parse_read_targets(raw or ""))


def _safe_gate_reason(value: str | None) -> str:
    return value if value in _KNOWN_SKIP_REASONS else "safety_stop"


@router.get("/report")
def warmup_report(
    days: int = Query(default=7, ge=7, le=30),
    account_id: int | None = Query(default=None, ge=1),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Count logged events in a rolling UTC interval [since, until)."""
    if days not in (7, 30):
        raise HTTPException(status_code=422, detail="days must be 7 or 30")
    until = utcnow_naive()
    since = until - timedelta(days=days)
    account_query = select(Account).order_by(Account.id)
    if account_id is not None:
        account_query = account_query.where(Account.id == account_id)
    accounts = db.execute(account_query).scalars().all()
    if account_id is not None and not accounts:
        raise HTTPException(status_code=404, detail="account not found")

    profiles = db.execute(select(WarmupProfile).order_by(WarmupProfile.name)).scalars().all()
    profiles_by_name = {profile.name: profile for profile in profiles}
    totals = _counts()
    by_action: dict[str, dict[str, int]] = {}
    by_reason: dict[str, dict[str, int]] = {}
    by_account = {account.id: _counts() for account in accounts}
    log_query = select(
        WarmupLog.account_id, WarmupLog.action, WarmupLog.status, WarmupLog.details,
    ).where(
        WarmupLog.created_at >= since,
        WarmupLog.created_at < until,
        WarmupLog.account_id.in_(by_account),
    ).execution_options(yield_per=1000)
    for logged_account_id, raw_action, raw_status, details in db.execute(log_query):
        status = _status(raw_status)
        action = _action(raw_action)
        for bucket in (totals, by_action.setdefault(action, _counts())):
            bucket["total"] += 1
            bucket[status] += 1
        by_account[logged_account_id]["total"] += 1
        by_account[logged_account_id][status] += 1
        reason = _reason(raw_action, status, details)
        if reason is not None:
            reason_bucket = by_reason.setdefault(reason, {"total": 0, "skip": 0, "fail": 0})
            reason_bucket["total"] += 1
            reason_bucket[status] += 1

    account_rows = []
    for account in accounts:
        selected_name = (account.warmup_profile or "safe").strip() or "safe"
        effective = profiles_by_name.get(selected_name) or profiles_by_name.get("safe")
        account_rows.append({
            "account_id": account.id,
            "title": account.display_title,
            "warmup_enabled": bool(account.warmup_enabled),
            "profile_name": selected_name,
            "effective_profile_name": effective.name if effective else None,
            "profile_enabled": bool(effective.enabled) if effective else None,
            "paused_until_utc": _utc_iso(account.warmup_paused_until),
            "pause_reason": _pause_reason(account.warmup_pause_reason),
            "totals": by_account[account.id],
        })

    return {
        "days": days,
        "since_utc": _utc_iso(since),
        "until_utc": _utc_iso(until),
        "totals": totals,
        "by_status": {status: totals[status] for status in _STATUSES},
        "by_action": dict(sorted(by_action.items())),
        "skip_fail_reasons": dict(sorted(by_reason.items())),
        "accounts": account_rows,
        "profiles": [
            {
                "name": profile.name,
                "enabled": bool(profile.enabled),
                "daily_action_limit": profile.daily_action_limit,
                "base_delay_sec": profile.base_delay_sec,
                "jitter_sec": profile.jitter_sec,
                "target_count": _target_count(profile.target_chats_text),
            }
            for profile in profiles
        ],
        "note": "Logged CoreBot events only; no live Telegram check or ban forecast.",
    }


@router.get("/preview")
def warmup_preview(
    account_id: int = Query(ge=1),
    db: Session = Depends(get_bot_db),
    _user: User = Depends(get_current_user),
):
    """Explain local eligibility without scheduling or performing an action."""
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    now = utcnow_naive()
    selected_name = (account.warmup_profile or "safe").strip() or "safe"
    profiles = db.execute(
        select(WarmupProfile).where(WarmupProfile.name.in_({selected_name, "safe"}))
    ).scalars().all()
    profiles_by_name = {profile.name: profile for profile in profiles}
    profile = profiles_by_name.get(selected_name) or profiles_by_name.get("safe")
    profile_source = (
        "selected" if selected_name in profiles_by_name else
        "safe_fallback" if profile else "config_defaults"
    )
    target_count = _target_count(profile.target_chats_text if profile else None)
    warmup_limit = min(12, max(1, int(
        (profile.daily_action_limit if profile else None) or WARMUP_DAILY_ACTION_LIMIT
    )))
    persisted_warmup_used = max(0, int(account.warmup_actions_today or 0))
    reset_on_next_tick = (
        account.warmup_last_action_at is None
        or account.warmup_last_action_at.date() != now.date()
    )
    warmup_used = 0 if reset_on_next_tick else persisted_warmup_used
    warmup_remaining = max(0, warmup_limit - warmup_used)

    state = db.get(AccountSafetyState, account_id)
    outbound_limit = max(0, int(account.daily_limit or 0))
    outbound_used = max(0, int(state.attempts_today or 0)) if (
        state is not None and state.day_utc == now.date().isoformat()
    ) else 0
    outbound_remaining = max(0, outbound_limit - outbound_used)
    safety_state = state.state if state else "ready"
    if state is not None and state.state != "ready":
        gate_reason = _safe_gate_reason(state.reason_code or state.state)
    elif account.status != AccountStatus.ACTIVE:
        gate_reason = "account_not_active"
    elif account.is_spam_blocked or (
        account.flood_wait_until and account.flood_wait_until > now
    ):
        gate_reason = "platform_restricted"
    elif outbound_limit == 0:
        gate_reason = "daily_limit"
    else:
        gate_reason = "ok"

    blockers = []
    if not WARMUP_ENABLED:
        blockers.append("scheduler_config_disabled")
    if not account.warmup_enabled:
        blockers.append("warmup_disabled")
    if profile is not None and not profile.enabled:
        blockers.append("profile_disabled")
    if account.status != AccountStatus.ACTIVE:
        blockers.append("account_not_active")
    if state is not None and state.state != "ready":
        blockers.append(gate_reason)
    if account.is_spam_blocked:
        blockers.append("platform_restricted")
    if outbound_limit == 0:
        blockers.append("daily_limit")
    if warmup_remaining == 0:
        blockers.append("warmup_daily_limit")
    time_zone = (profile.time_zone if profile else None) or DEFAULT_TIME_ZONE
    work_start = (profile.work_start_hour if profile else None)
    work_end = (profile.work_end_hour if profile else None)
    work_start = DEFAULT_WORK_START if work_start is None else int(work_start)
    work_end = DEFAULT_WORK_END if work_end is None else int(work_end)
    off_hours = is_off_hours(now, time_zone, work_start, work_end)
    blockers = list(dict.fromkeys(blockers))

    # A persistent stop or exhausted persisted budget has no automatic release
    # time. Timed fields only establish the earliest local scheduling instant.
    times = [now]
    # Candidate selection uses strict `< now` for warmup timestamps.
    for value in (account.warmup_paused_until, account.warmup_next_run_at):
        if value is not None and value >= now:
            times.append(value + timedelta(microseconds=1))
    # The account gate releases FloodWait at `<= now`.
    if account.flood_wait_until is not None and account.flood_wait_until > now:
        times.append(account.flood_wait_until)
    if account.warmup_last_action_at is not None:
        times.append(account.warmup_last_action_at + timedelta(seconds=MIN_INTERVAL_SECONDS))
    next_eligible = next_off_hours(max(times), time_zone, work_start, work_end) if not blockers else None
    ready_now = next_eligible is not None and next_eligible == now
    if not off_hours:
        blockers.append("work_hours")

    actions = []
    allowed_actions = set((profile.allowed_actions if profile else "read_dialogs,read_channels").split(","))
    for name, needs_target, needs_outbound_budget, executed_as in (
        ("read_dialogs", False, False, "read_dialogs"),
        ("read_channels", True, False, "read_channels"),
        ("set_reaction", True, True, "set_reaction"),
    ):
        reasons = list(blockers)
        if next_eligible is not None and next_eligible > now and off_hours:
            reasons.append("scheduled_later")
        if name not in allowed_actions:
            reasons.append("action_not_allowed")
        if needs_target and target_count == 0:
            reasons.append("no_targets")
        if needs_outbound_budget and outbound_remaining == 0:
            reasons.append("outbound_daily_limit")
        actions.append({
            "name": name,
            "executed_as": executed_as,
            "requires_target": needs_target,
            "available_now": not reasons,
            "unavailable_reasons": reasons,
        })

    return {
        "account_id": account.id,
        "title": account.display_title,
        "account_status": account.status.value if account.status else "unknown",
        "observed_at_utc": _utc_iso(now),
        "scheduler_config_enabled": bool(WARMUP_ENABLED),
        "warmup_enabled": bool(account.warmup_enabled),
        "profile": {
            "selected_name": selected_name,
            "effective_name": profile.name if profile else None,
            "source": profile_source,
            "enabled": bool(profile.enabled) if profile else None,
            "base_delay_sec": float(
                (profile.base_delay_sec if profile else None) or WARMUP_BASE_DELAY_SEC
            ),
            "jitter_sec": float(
                (profile.jitter_sec if profile else None) or WARMUP_JITTER_SEC
            ),
            "target_count": target_count,
            "time_zone": time_zone,
            "work_start_hour": work_start,
            "work_end_hour": work_end,
            "allowed_actions": sorted(allowed_actions),
        },
        "safety_gate": {
            "allowed": gate_reason == "ok",
            "reason": gate_reason,
            "state": safety_state,
            "spam_blocked": bool(account.is_spam_blocked),
            "resume_at_utc": _utc_iso(state.resume_at) if state else None,
            "flood_wait_until_utc": _utc_iso(account.flood_wait_until),
        },
        "warmup_daily": {
            "limit": warmup_limit,
            "used": warmup_used,
            "remaining": warmup_remaining,
            "persisted_used": persisted_warmup_used,
            "reset_on_next_tick": reset_on_next_tick,
        },
        "outbound_daily": {
            "limit": outbound_limit,
            "used": outbound_used,
            "remaining": outbound_remaining,
        },
        "paused_until_utc": _utc_iso(account.warmup_paused_until),
        "pause_reason": _pause_reason(account.warmup_pause_reason),
        "next_run_at_utc": _utc_iso(account.warmup_next_run_at),
        "next_eligible_at_utc": _utc_iso(next_eligible),
        "ready_now": ready_now,
        "blocking_reasons": blockers,
        "actions": actions,
        "note": "Local stored state and off-hours schedule only. No live Telegram check or outcome forecast.",
    }
