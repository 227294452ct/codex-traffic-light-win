"""Codex Traffic Light MXP — Windows port.

Core: states, state store (JSON file), defaults driven by environment variables.

Faithful port of Sources/CodexTrafficLightCore (Models.swift, StateStore.swift,
ContextResolver.swift, TrafficLightLayout.swift) from the macOS Swift version.
The state file format is byte-for-byte compatible with the macOS version.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Environment-driven defaults (same env vars as the macOS version)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        value = float(raw)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


class Defaults:
    """Environment-variable-tunable defaults, mirroring Swift Defaults."""

    done_auto_idle_seconds: float = _env_float("CODEX_LIGHT_DONE_IDLE_SECONDS", 10 * 60)
    waiting_alert_seconds: float = _env_float("CODEX_LIGHT_WAITING_ALERT_SECONDS", 10)
    app_server_quota_refresh_seconds: float = _env_float(
        "CODEX_LIGHT_APP_SERVER_QUOTA_REFRESH_SECONDS", 5 * 60
    )


class CommandContract:
    light_command_name = "codex-light-mxp"
    hook_command_name = "codex-light-hook-mxp"
    quota_command_name = "quota"


# ---------------------------------------------------------------------------
# Path resolution (Windows-first, macOS fallback for parity)
# ---------------------------------------------------------------------------

def default_support_dir() -> str:
    """Default data directory.

    Windows: %LOCALAPPDATA%\\CodexTrafficLight
    macOS:   ~/Library/Application Support/CodexTrafficLight (parity)
    Overridable with CODEX_TRAFFIC_LIGHT_DATA_DIR (portable installs).
    """
    override = os.environ.get("CODEX_TRAFFIC_LIGHT_DATA_DIR", "")
    if override:
        return override
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        base = local if local else os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "CodexTrafficLight")
    return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "CodexTrafficLight")


def default_state_path() -> str:
    override = os.environ.get("CODEX_TRAFFIC_LIGHT_STATE_PATH", "")
    if override:
        return override
    return os.path.join(default_support_dir(), "state.json")


def default_hook_log_path() -> str:
    override = os.environ.get("CODEX_TRAFFIC_LIGHT_HOOK_LOG_PATH", "")
    if override:
        return override
    return os.path.join(default_support_dir(), "hook-mxp.log")


def default_quota_log_path() -> str:
    override = os.environ.get("CODEX_TRAFFIC_LIGHT_QUOTA_LOG_PATH", "")
    if override:
        return override
    return os.path.join(default_support_dir(), "quota-mxp.log")


def default_preferences_path() -> str:
    override = os.environ.get("CODEX_TRAFFIC_LIGHT_PREFERENCES_PATH", "")
    if override:
        return override
    return os.path.join(default_support_dir(), "preferences.json")


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class LightState(str, Enum):
    idle = "idle"
    working = "working"
    done = "done"
    waiting = "waiting"
    quit = "quit"

    @property
    def label(self) -> str:
        return {
            LightState.idle: "空闲",
            LightState.working: "正在干活",
            LightState.done: "可以验收",
            LightState.waiting: "等你回复",
            LightState.quit: "退出",
        }[self]

    @property
    def sort_priority(self) -> int:
        return {
            LightState.waiting: 3,
            LightState.working: 2,
            LightState.done: 1,
            LightState.idle: 0,
            LightState.quit: 0,
        }[self]


class TaskState:
    def __init__(
        self,
        state: LightState,
        workspace: Optional[str],
        source: str,
        hook_event_name: Optional[str],
        message: Optional[str],
        updated_at: float,
    ):
        self.state = state
        self.workspace = workspace
        self.source = source
        self.hook_event_name = hook_event_name
        self.message = message
        self.updated_at = updated_at

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "workspace": self.workspace,
            "source": self.source,
            "hook_event_name": self.hook_event_name,
            "message": self.message,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "TaskState":
        return TaskState(
            state=LightState(str(data.get("state", "idle"))),
            workspace=data.get("workspace"),
            source=str(data.get("source", "")),
            hook_event_name=data.get("hook_event_name"),
            message=data.get("message"),
            updated_at=float(data.get("updated_at", 0.0)),
        )


class QuotaSnapshot:
    def __init__(self, five_hour_remaining_percent: int, weekly_remaining_percent: int,
                 source: str, updated_at: float):
        self.five_hour_remaining_percent = min(100, max(0, five_hour_remaining_percent))
        self.weekly_remaining_percent = min(100, max(0, weekly_remaining_percent))
        self.source = source
        self.updated_at = updated_at

    def to_dict(self) -> dict:
        return {
            "five_hour_remaining_percent": self.five_hour_remaining_percent,
            "weekly_remaining_percent": self.weekly_remaining_percent,
            "source": self.source,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: dict) -> "QuotaSnapshot":
        return QuotaSnapshot(
            five_hour_remaining_percent=int(data.get("five_hour_remaining_percent", 0)),
            weekly_remaining_percent=int(data.get("weekly_remaining_percent", 0)),
            source=str(data.get("source", "")),
            updated_at=float(data.get("updated_at", 0.0)),
        )


class StateSnapshot:
    def __init__(self, aggregate_state: LightState, updated_at: float,
                 quota: Optional[QuotaSnapshot] = None, tasks: Optional[dict] = None):
        self.aggregate_state = aggregate_state
        self.updated_at = updated_at
        self.quota = quota
        self.tasks: dict[str, TaskState] = tasks or {}

    @staticmethod
    def empty(now: Optional[float] = None) -> "StateSnapshot":
        return StateSnapshot(LightState.idle, now if now is not None else time.time(), tasks={})

    def computed_aggregate(self, now: Optional[float] = None,
                           done_ttl: float = Defaults.done_auto_idle_seconds) -> LightState:
        now = now if now is not None else time.time()
        if any(t.state == LightState.waiting for t in self.tasks.values()):
            return LightState.waiting
        if any(t.state == LightState.working for t in self.tasks.values()):
            return LightState.working
        has_recent_done = any(
            t.state == LightState.done and now - t.updated_at <= done_ttl
            for t in self.tasks.values()
        )
        return LightState.done if has_recent_done else LightState.idle

    def pruning_expired_done(self, now: Optional[float] = None,
                             done_ttl: float = Defaults.done_auto_idle_seconds) -> "StateSnapshot":
        now = now if now is not None else time.time()
        active = {
            tid: t
            for tid, t in self.tasks.items()
            if not (t.state == LightState.done and now - t.updated_at > done_ttl)
        }
        snapshot = StateSnapshot(self.aggregate_state, self.updated_at, self.quota, active)
        snapshot.aggregate_state = snapshot.computed_aggregate(now=now, done_ttl=done_ttl)
        snapshot.updated_at = now
        return snapshot

    def to_dict(self) -> dict:
        return {
            "aggregate_state": self.aggregate_state.value,
            "updated_at": self.updated_at,
            "quota": self.quota.to_dict() if self.quota else None,
            "tasks": {tid: task.to_dict() for tid, task in self.tasks.items()},
        }

    @staticmethod
    def from_dict(data: dict) -> "StateSnapshot":
        quota_data = data.get("quota")
        quota = QuotaSnapshot.from_dict(quota_data) if isinstance(quota_data, dict) else None
        tasks = {
            str(tid): TaskState.from_dict(td)
            for tid, td in (data.get("tasks") or {}).items()
            if isinstance(td, dict)
        }
        return StateSnapshot(
            aggregate_state=LightState(str(data.get("aggregate_state", "idle"))),
            updated_at=float(data.get("updated_at", 0.0)),
            quota=quota,
            tasks=tasks,
        )


# ---------------------------------------------------------------------------
# State store (file-backed, atomic writes; format compatible with macOS version)
# ---------------------------------------------------------------------------

class StateStoreError(Exception):
    pass


class StateStore:
    def __init__(self, state_path: Optional[str] = None):
        self.state_path = state_path or default_state_path()

    # -- io ----------------------------------------------------------------

    def read(self) -> StateSnapshot:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            snapshot = StateSnapshot.from_dict(raw)
        except (OSError, ValueError, KeyError, TypeError):
            return StateSnapshot.empty()
        if snapshot.aggregate_state == LightState.quit:
            return snapshot
        return snapshot.pruning_expired_done()

    def write(self, snapshot: StateSnapshot) -> None:
        directory = os.path.dirname(self.state_path)
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, self.state_path)  # atomic on Windows (same volume)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- mutations ---------------------------------------------------------

    def update_task(self, task_id: str, state: LightState, workspace: Optional[str],
                    source: str, hook_event_name: Optional[str], message: Optional[str],
                    now: Optional[float] = None) -> StateSnapshot:
        now = now if now is not None else time.time()
        snapshot = self.read().pruning_expired_done(now=now)
        if state == LightState.idle:
            snapshot.tasks.pop(task_id, None)
        elif state == LightState.quit:
            snapshot.aggregate_state = LightState.quit
            snapshot.updated_at = now
            self.write(snapshot)
            return snapshot
        else:
            snapshot.tasks[task_id] = TaskState(
                state=state,
                workspace=workspace,
                source=source,
                hook_event_name=hook_event_name,
                message=message,
                updated_at=now,
            )
        snapshot.aggregate_state = snapshot.computed_aggregate(now=now)
        snapshot.updated_at = now
        self.write(snapshot)
        return snapshot

    def clear(self, now: Optional[float] = None) -> StateSnapshot:
        now = now if now is not None else time.time()
        snapshot = StateSnapshot(LightState.idle, now, quota=self.read().quota, tasks={})
        self.write(snapshot)
        return snapshot

    def update_quota(self, five_hour_percent: int, weekly_percent: int, source: str,
                     now: Optional[float] = None) -> StateSnapshot:
        now = now if now is not None else time.time()
        snapshot = self.read().pruning_expired_done(now=now)
        snapshot.quota = QuotaSnapshot(five_hour_percent, weekly_percent, source, now)
        snapshot.aggregate_state = snapshot.computed_aggregate(now=now)
        snapshot.updated_at = now
        self.write(snapshot)
        return snapshot


# ---------------------------------------------------------------------------
# Context resolution (task id / workspace), mirrors ContextResolver.swift
# ---------------------------------------------------------------------------

def current_directory() -> str:
    return os.getcwd()


def resolve_workspace(explicit_workspace: Optional[str], hook_event=None) -> str:
    if explicit_workspace:
        return explicit_workspace
    if hook_event is not None and hook_event.workspace:
        return hook_event.workspace
    if hook_event is not None and hook_event.cwd:
        return hook_event.cwd
    return current_directory()


def resolve_task_id(explicit_task_id: Optional[str], workspace: Optional[str],
                    hook_event=None) -> str:
    if explicit_task_id:
        return explicit_task_id
    if hook_event is not None and hook_event.session_id:
        return "session:" + hook_event.session_id
    if hook_event is not None and hook_event.thread_id:
        return "thread:" + hook_event.thread_id
    workspace_value = workspace or (hook_event.workspace if hook_event else None) \
        or (hook_event.cwd if hook_event else None) or current_directory()
    return "workspace:" + workspace_value + ":default"


# ---------------------------------------------------------------------------
# Command parsing (mirrors CommandParser)
# ---------------------------------------------------------------------------

def parse_state(command: str) -> LightState:
    if command == "quit":
        return LightState.quit
    try:
        return LightState(command)
    except ValueError:
        raise StateStoreError("Unknown state: " + command)
