"""Codex Traffic Light MXP (Windows port) — Codex hook command.

Reads hook JSON from stdin; the hook event name is passed as argv[1]
(e.g. `codex-light-hook-mxp UserPromptSubmit`). Always prints {} and
exits 0 so Codex hook execution never fails because of this tool.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_light.core import StateStore  # noqa: E402
from codex_light.hook import (  # noqa: E402
    HookBridge,
    HookEvent,
    HookLogEntry,
    HookMapper,
    append_hook_log,
)
from codex_light.quota import QuotaExtractor  # noqa: E402


def main(argv: list) -> int:
    fallback_name = argv[0] if argv else None
    input_data = sys.stdin.buffer.read()

    try:
        result = HookBridge.apply(input_data=input_data, fallback_name=fallback_name,
                                  store=StateStore())
        append_hook_log(HookLogEntry(
            timestamp=__import__("time").time(),
            event_name=result.event_name,
            state=result.state,
            task_id=result.task_id,
            workspace=result.workspace,
            result="ok",
            detail=None,
            quota_summary=result.quota_summary,
        ))
        print("{}")
    except Exception as exc:  # noqa: BLE001 — hooks must never break Codex
        event = HookEvent.parse(input_data, fallback_name=fallback_name)
        state = HookMapper.state_for(event)
        workspace = event.workspace or event.cwd or os.getcwd()
        task_id = event.session_id and f"session:{event.session_id}" or workspace
        quota = QuotaExtractor.extract(input_data)
        append_hook_log(HookLogEntry(
            timestamp=__import__("time").time(),
            event_name=event.name,
            state=state,
            task_id=task_id,
            workspace=workspace,
            result="error",
            detail=str(exc),
            quota_summary=quota.summary if quota else None,
        ))
        sys.stderr.write(f"{exc}\n")
        print("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
