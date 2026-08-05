"""Codex Traffic Light MXP — Windows port.

Hooks bridge: parse Codex hook JSON from stdin, map events to light states,
update the state store, append structured logs.

Faithful port of HookMapper.swift, HookBridge.swift, HookLogger.swift.
"""
from __future__ import annotations

import json
import re
import time
from typing import Optional

from .core import (
    LightState,
    StateStore,
    StateSnapshot,
    QuotaSnapshot,
    resolve_workspace,
    resolve_task_id,
    default_hook_log_path,
)
from .quota import QuotaExtractor, QuotaValues


class HookEvent:
    def __init__(self, name: str, last_assistant_message: Optional[str] = None,
                 cwd: Optional[str] = None, workspace: Optional[str] = None,
                 session_id: Optional[str] = None, thread_id: Optional[str] = None,
                 raw: Optional[dict] = None):
        self.name = name
        self.last_assistant_message = last_assistant_message
        self.cwd = cwd
        self.workspace = workspace
        self.session_id = session_id
        self.thread_id = thread_id
        self.raw = raw or {}

    @staticmethod
    def parse(json_data: bytes, fallback_name: Optional[str] = None) -> "HookEvent":
        if not json_data:
            return HookEvent(name=fallback_name or "")
        try:
            obj = json.loads(json_data.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return HookEvent(name=fallback_name or "")
        if not isinstance(obj, dict):
            return HookEvent(name=fallback_name or "")

        def string_value(key: str) -> Optional[str]:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
            return None

        flattened = {
            str(key): str(value)
            for key, value in obj.items()
            if isinstance(value, str)
        }

        return HookEvent(
            name=string_value("hook_event_name") or fallback_name or "",
            last_assistant_message=string_value("last_assistant_message"),
            cwd=string_value("cwd") or string_value("current_dir"),
            workspace=string_value("workspace") or string_value("workspace_root"),
            session_id=string_value("session_id") or string_value("conversation_id"),
            thread_id=string_value("thread_id") or string_value("turn_id"),
            raw=flattened,
        )


QUOTA_ONLY_EVENTS = {"RateLimitsUpdated", "account/rateLimits/updated", "AccountRateLimitsUpdated"}


class HookMapper:
    @staticmethod
    def is_quota_only_event(name: str) -> bool:
        return name in QUOTA_ONLY_EVENTS

    @staticmethod
    def state_for(event: HookEvent) -> LightState:
        if event.name in ("UserPromptSubmit", "PreToolUse"):
            return LightState.working
        if event.name == "PermissionRequest":
            return LightState.waiting
        if event.name in ("Stop", "SubagentStop"):
            return LightState.waiting if HookMapper.looks_waiting(event.last_assistant_message) \
                else LightState.done
        return LightState.idle

    _WAITING_PATTERNS = [
        r"等你",
        r"需要.{0,20}(回复|确认|授权|登录|验证码|文件|截图|选择|提供|补充)",
        r"请.{0,20}(回复|确认|授权|登录|提供|发我|补充|选择)",
        r"你.{0,20}(确认|选择|提供|发我|补充)",
        r"要不要|可以吗|行不行|是否",
        r"我需要.{0,20}(你|确认|授权|文件|截图|验证码)",
        r"blocked|waiting for user|permission|approval",
        r"\?",
        r"？",
    ]

    @staticmethod
    def looks_waiting(message: Optional[str]) -> bool:
        if not message or not message.strip():
            return False
        for pattern in HookMapper._WAITING_PATTERNS:
            if re.search(pattern, message, re.IGNORECASE):
                return True
        return False


class HookBridgeResult:
    def __init__(self, event_name: str, state: LightState, task_id: str,
                 workspace: Optional[str], quota_summary: Optional[str], updated_task: bool):
        self.event_name = event_name
        self.state = state
        self.task_id = task_id
        self.workspace = workspace
        self.quota_summary = quota_summary
        self.updated_task = updated_task


class HookBridge:
    @staticmethod
    def apply(input_data: bytes, fallback_name: Optional[str], store: StateStore,
              now: Optional[float] = None) -> HookBridgeResult:
        event = HookEvent.parse(input_data, fallback_name=fallback_name)
        quota: Optional[QuotaValues] = QuotaExtractor.extract(input_data)
        quota_only = HookMapper.is_quota_only_event(event.name)
        workspace = resolve_workspace(None, hook_event=event)
        task_id = resolve_task_id(None, workspace, hook_event=event)
        snapshot: StateSnapshot = store.read()

        if quota is not None:
            snapshot = store.update_quota(
                five_hour_percent=quota.five_hour_remaining_percent,
                weekly_percent=quota.weekly_remaining_percent,
                source="codex-hook",
                now=now,
            )

        if quota_only:
            return HookBridgeResult(
                event_name=event.name,
                state=LightState.idle if snapshot.aggregate_state == LightState.quit
                else snapshot.aggregate_state,
                task_id=task_id,
                workspace=workspace,
                quota_summary=quota.summary if quota else None,
                updated_task=False,
            )

        state = HookMapper.state_for(event)
        message = event.last_assistant_message or f"Codex traffic light: {state.value}"
        snapshot = store.update_task(
            task_id=task_id,
            state=state,
            workspace=workspace,
            source="codex-hook",
            hook_event_name=event.name,
            message=message,
            now=now,
        )

        return HookBridgeResult(
            event_name=event.name,
            state=state,
            task_id=task_id,
            workspace=workspace,
            quota_summary=quota.summary if quota else None,
            updated_task=True,
        )


# ---------------------------------------------------------------------------
# Hook logger (same line format as the macOS version)
# ---------------------------------------------------------------------------

class HookLogEntry:
    def __init__(self, timestamp: float, event_name: str, state: LightState, task_id: str,
                 workspace: Optional[str], result: str, detail: Optional[str],
                 quota_summary: Optional[str] = None):
        self.timestamp = timestamp
        self.event_name = event_name
        self.state = state
        self.task_id = task_id
        self.workspace = workspace
        self.result = result
        self.detail = detail
        self.quota_summary = quota_summary


def _iso8601_fractional(timestamp: float) -> str:
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(timestamp, tz=_dt.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _field(value: str) -> str:
    return value.replace("\n", "\\n").replace("\r", "\\r")


def format_hook_log_line(entry: HookLogEntry) -> str:
    parts = [
        _iso8601_fractional(entry.timestamp),
        f"event={_field(entry.event_name)}",
        f"state={entry.state.value}",
        f"task={_field(entry.task_id)}",
        f"workspace={_field(entry.workspace or '-')}",
        f"result={_field(entry.result)}",
        f"quota={_field(entry.quota_summary or 'none')}",
    ]
    if entry.detail:
        parts.append(f"detail={_field(entry.detail)}")
    return " ".join(parts) + "\n"


def append_hook_log(entry: HookLogEntry, log_path: Optional[str] = None) -> None:
    try:
        log_path = log_path or default_hook_log_path()
        import os
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(format_hook_log_line(entry))
    except OSError:
        pass
