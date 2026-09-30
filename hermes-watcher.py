"""Hermes -> Codex Traffic Light MXP bridge (Windows port).

Watches the Hermes desktop agent's session database (state.db) and mirrors
its live activity into the shared traffic-light state.json under task id
`hermes:watcher`, so the traffic light shows BOTH Codex and Hermes states,
aggregated with the standard priority waiting > working > done > idle.

State rules (all tunable via env vars):

  working   any tool / assistant(tool_calls) message in the last
            HERMES_LIGHT_ACTIVITY_WINDOW_SECONDS (60) — agent is executing
  waiting   last message is an assistant message that asks the user
            something (same Chinese "等你回复" heuristics as the Codex hook)
            and no activity for HERMES_LIGHT_WAIT_CONFIRM_SECONDS (15)
  done      last message is a plain assistant reply, delivered within
            HERMES_LIGHT_DONE_WINDOW_SECONDS (120); the shared store then
            auto-idles it after the usual done TTL (10 min)
  idle      nothing recent at all -> task removed from the store

The watcher never writes when the DB is unavailable (Hermes upgrade/lock):
it keeps the last known state instead. DB is opened read-only per probe.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_light.core import LightState, StateStore  # noqa: E402
from codex_light.hook import HookMapper  # noqa: E402

TASK_ID = "hermes:watcher"
TASK_SOURCE = "hermes-watcher"


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default


def default_db_path() -> str:
    override = os.environ.get("HERMES_LIGHT_DB_PATH", "")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA", "")
    base = local if local else os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return os.path.join(base, "hermes", "state.db")


def default_log_path() -> str:
    override = os.environ.get("HERMES_LIGHT_LOG_PATH", "")
    if override:
        return override
    from codex_light.core import default_support_dir
    return os.path.join(default_support_dir(), "hermes-watcher.log")


def probe(db_path: str, activity_window: float, wait_confirm: float,
          done_window: float) -> tuple[LightState, str]:
    """Return (state, reason). 'unknown' means the DB could not be read."""
    now = time.time()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        return LightState.idle, "db_unavailable"
    try:
        # recent execution activity (tool calls or assistant looping on tools)
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE timestamp > ? "
            "AND (role = 'tool' OR (role = 'assistant' AND finish_reason = 'tool_calls'))",
            (now - activity_window,),
        ).fetchone()
        if row and row[0] > 0:
            return LightState.working, "tool_activity"

        # most recent message of any kind
        last = conn.execute(
            "SELECT role, content, timestamp FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last is None:
            return LightState.idle, "no_messages"
        role, content, ts = last
        age = now - (ts or 0)

        if role == "tool":
            return LightState.working, "tool_message"
        if role == "user":
            return LightState.working if age <= activity_window else LightState.idle, \
                f"user_message age={age:.0f}s"
        # assistant
        if age <= wait_confirm:
            return LightState.done, f"assistant_reply age={age:.0f}s"
        if HookMapper.looks_waiting(content or ""):
            return LightState.waiting, f"asks_user age={age:.0f}s"
        if age <= done_window:
            return LightState.done, f"assistant_reply age={age:.0f}s"
        return LightState.idle, f"stale age={age:.0f}s"
    except sqlite3.Error:
        return LightState.idle, "db_error"
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def append_log(line: str, log_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def run_once(db_path: str) -> tuple[LightState, str]:
    return probe(
        db_path,
        activity_window=_env_float("HERMES_LIGHT_ACTIVITY_WINDOW_SECONDS", 60),
        wait_confirm=_env_float("HERMES_LIGHT_WAIT_CONFIRM_SECONDS", 15),
        done_window=_env_float("HERMES_LIGHT_DONE_WINDOW_SECONDS", 120),
    )


def main(argv: list) -> int:
    db_path = default_db_path()
    poll_seconds = _env_float("HERMES_LIGHT_POLL_SECONDS", 2.0)
    activity_window = _env_float("HERMES_LIGHT_ACTIVITY_WINDOW_SECONDS", 60)
    wait_confirm = _env_float("HERMES_LIGHT_WAIT_CONFIRM_SECONDS", 15)
    done_window = _env_float("HERMES_LIGHT_DONE_WINDOW_SECONDS", 120)
    log_path = default_log_path()
    store = StateStore()

    if "--once" in argv:
        state, reason = probe(db_path, activity_window, wait_confirm, done_window)
        print(f"{state.value} ({reason})")
        return 0

    last_state: LightState | None = None
    consecutive_errors = 0
    while True:
        try:
            state, reason = probe(db_path, activity_window, wait_confirm, done_window)
            snapshot = store.read()
            existing = snapshot.tasks.get(TASK_ID)
            existing_state = existing.state if existing else None

            needs_write = state != existing_state
            if state == LightState.idle and existing is None:
                needs_write = False  # nothing to clean up

            if needs_write:
                try:
                    snapshot = store.update_task(
                        task_id=TASK_ID, state=state,
                        workspace=None, source=TASK_SOURCE,
                        hook_event_name=None,
                        message=f"Hermes traffic light: {state.value} ({reason})",
                    )
                    append_log(
                        f"state={state.value} reason={reason} aggregate={snapshot.aggregate_state.value}",
                        log_path,
                    )
                except OSError as exc:
                    append_log(f"write_failed={exc}", log_path)
                last_state = state
            consecutive_errors = 0
        except BaseException as exc:  # never die silently: log & keep polling
            import traceback
            consecutive_errors += 1
            append_log(
                f"CRASH_GUARD error#{consecutive_errors} {type(exc).__name__}: {exc} "
                f"traceback={traceback.format_exc()!r}",
                log_path,
            )
            if consecutive_errors >= 5:
                # persistent failure: exit so the startup guard can respawn us
                raise
        time.sleep(poll_seconds)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
