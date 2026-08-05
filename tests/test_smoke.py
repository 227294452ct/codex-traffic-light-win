"""Codex Traffic Light MXP (Windows port) — test suite.

Plain-assert tests (no pytest needed): `python tests/test_smoke.py`
Ports the upstream `swift run codex-light-mxp-tests` coverage plus the
README smoke test, all against a temp state file via environment vars.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

PORT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PORT_DIR)

from codex_light.core import (  # noqa: E402
    Defaults,
    LightState,
    StateSnapshot,
    StateStore,
    default_support_dir,
)
from codex_light.hook import (  # noqa: E402
    HookBridge,
    HookEvent,
    HookMapper,
    append_hook_log,
    format_hook_log_line,
    HookLogEntry,
)
from codex_light.quota import (  # noqa: E402
    QuotaExtractor,
    QuotaValues,
    quota_values_from_app_server_response,
    _result_for_id,
    _line_decode_messages,
    CodexAppServerQuotaError,
)

FAILURES = []


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"  ok  {name}")
    except AssertionError as exc:
        FAILURES.append(name)
        print(f"FAIL  {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        print(f"ERROR {name}: {exc!r}")


def with_env(**env):
    def decorator(fn):
        def wrapper():
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                fn()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# unit tests
# ---------------------------------------------------------------------------

def test_aggregate_priority():
    now = time.time()
    TaskStateCls = __import__("codex_light.core", fromlist=["TaskState"]).TaskState
    s = StateSnapshot(LightState.idle, now, tasks={})
    assert s.computed_aggregate(now=now) == LightState.idle
    s.tasks["a"] = TaskStateCls(LightState.working, None, "t", None, None, now)
    assert s.computed_aggregate(now=now) == LightState.working
    s.tasks["b"] = TaskStateCls(LightState.waiting, None, "t", None, None, now)
    assert s.computed_aggregate(now=now) == LightState.waiting
    del s.tasks["a"]
    assert s.computed_aggregate(now=now) == LightState.waiting  # b still waiting
    del s.tasks["b"]
    s.tasks["c"] = TaskStateCls(LightState.done, None, "t", None, None, now)
    assert s.computed_aggregate(now=now) == LightState.done
    assert s.computed_aggregate(now=now + 601) == LightState.idle  # done expired


def test_done_ttl_pruning():
    now = time.time()
    store = StateStore()
    snapshot = StateSnapshot(LightState.done, now, tasks={
        "old": __import__("codex_light.core", fromlist=["TaskState"]).TaskState(
            LightState.done, None, "t", None, None, now - 700),
        "fresh": __import__("codex_light.core", fromlist=["TaskState"]).TaskState(
            LightState.done, None, "t", None, None, now),
    })
    pruned = snapshot.pruning_expired_done(now=now)
    assert "old" not in pruned.tasks
    assert "fresh" in pruned.tasks
    assert pruned.aggregate_state == LightState.done


def test_hook_mapping():
    def event(name, message=None):
        return HookEvent(name=name, last_assistant_message=message)

    assert HookMapper.state_for(event("UserPromptSubmit")) == LightState.working
    assert HookMapper.state_for(event("PreToolUse")) == LightState.working
    assert HookMapper.state_for(event("PermissionRequest")) == LightState.waiting
    assert HookMapper.state_for(event("Stop")) == LightState.done
    assert HookMapper.state_for(event("SubagentStop")) == LightState.done
    assert HookMapper.state_for(event("Stop", "需要你确认授权后我才能继续。")) == LightState.waiting
    assert HookMapper.state_for(event("Stop", "任务完成，结果如下。")) == LightState.done
    assert HookMapper.is_quota_only_event("RateLimitsUpdated")
    assert HookMapper.is_quota_only_event("account/rateLimits/updated")
    assert not HookMapper.is_quota_only_event("Stop")


def test_looks_waiting():
    assert HookMapper.looks_waiting("需要你确认后继续")
    assert HookMapper.looks_waiting("请提供截图")
    assert HookMapper.looks_waiting("要不要继续？")
    assert HookMapper.looks_waiting("等你回复")
    assert HookMapper.looks_waiting("This is blocked waiting for user input")
    assert HookMapper.looks_waiting("请选择方案")
    assert not HookMapper.looks_waiting("任务已完成")
    assert not HookMapper.looks_waiting(None)
    assert not HookMapper.looks_waiting("   ")


def test_hook_event_parse():
    payload = json.dumps({
        "hook_event_name": "Stop",
        "last_assistant_message": "完成",
        "cwd": "C:/proj",
        "session_id": "sess-1",
        "thread_id": "turn-1",
    }).encode()
    event = HookEvent.parse(payload, fallback_name="Stop")
    assert event.name == "Stop"
    assert event.cwd == "C:/proj"
    assert event.session_id == "sess-1"
    assert event.thread_id == "turn-1"
    assert event.raw["cwd"] == "C:/proj"
    # fallback name when payload has no hook_event_name
    event2 = HookEvent.parse(b'{"cwd":"/x"}', fallback_name="PreToolUse")
    assert event2.name == "PreToolUse"


def test_quota_extractor():
    assert QuotaExtractor.extract(b'{"five_hour_remaining_percent":72,"weekly_remaining_percent":48}') \
        == QuotaValues(72, 48)
    assert QuotaExtractor.extract(b'{"quota":{"fiveHourRemainingPercent":71,"weeklyRemainingPercent":47}}') \
        == QuotaValues(71, 47)
    assert QuotaExtractor.extract(b'{"a":[{"x":{"weekly_remaining_percent":"50","five_hour_remaining_percent":12}}]}') \
        == QuotaValues(12, 50)
    assert QuotaExtractor.extract(b'{"a":1}') is None
    assert QuotaExtractor.extract(b'') is None
    # clamping
    assert QuotaExtractor.extract(b'{"five_hour_remaining_percent":150,"weekly_remaining_percent":-3}') \
        == QuotaValues(100, 0)


def test_quota_mapping_from_app_server_response():
    data = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 28.0, "windowDurationMins": 300},
                "secondary": {"usedPercent": 52.0, "windowDurationMins": 10080},
            }
        }
    }
    values = quota_values_from_app_server_response(json.dumps(data).encode())
    assert values == QuotaValues(72, 48)
    # camelCase containers
    data2 = {
        "rateLimits": {
            "primary": {"usedPercent": 50.0, "windowDurationMins": 300},
            "secondary": {"usedPercent": 90.0, "windowDurationMins": 10080},
        }
    }
    values2 = quota_values_from_app_server_response(json.dumps(data2).encode())
    assert values2 == QuotaValues(50, 10)
    # missing quota -> error
    try:
        quota_values_from_app_server_response(b'{"foo":1}')
        raise AssertionError("expected missingQuota")
    except CodexAppServerQuotaError as exc:
        assert exc.summary_key == "missingQuota"


def test_jsonrpc_line_codec():
    from codex_light.quota import _line_encode_message
    notif = _line_encode_message({"method": "initialized"})
    assert json.loads(notif)["method"] == "initialized"
    response = _line_encode_message({"id": 2, "result": {"ok": True}})
    decoded = _line_decode_messages(response)
    assert _result_for_id(2, decoded) == {"ok": True}


def test_hook_log_format():
    entry = HookLogEntry(
        timestamp=time.time(), event_name="Stop", state=LightState.done,
        task_id="session:abc", workspace="C:/proj", result="ok",
        detail=None, quota_summary="72/48")
    line = format_hook_log_line(entry)
    assert "event=Stop" in line
    assert "state=done" in line
    assert "task=session:abc" in line
    assert "workspace=C:/proj" in line
    assert "result=ok" in line
    assert "quota=72/48" in line


def test_windows_default_paths():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LOCALAPPDATA"] = tmp
        assert default_support_dir() == os.path.join(tmp, "CodexTrafficLight")
        os.environ["CODEX_TRAFFIC_LIGHT_DATA_DIR"] = tmp + "\\custom"
        assert default_support_dir() == tmp + "\\custom"
        os.environ["CODEX_TRAFFIC_LIGHT_STATE_PATH"] = tmp + "\\s.json"
        assert StateStore().state_path == tmp + "\\s.json"
        del os.environ["CODEX_TRAFFIC_LIGHT_DATA_DIR"]
        del os.environ["CODEX_TRAFFIC_LIGHT_STATE_PATH"]


def test_env_tunables():
    assert Defaults.done_auto_idle_seconds == 600
    assert Defaults.waiting_alert_seconds == 10
    assert Defaults.app_server_quota_refresh_seconds == 300


# ---------------------------------------------------------------------------
# end-to-end CLI smoke (same steps as the upstream README)
# ---------------------------------------------------------------------------

def run_cli(state_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CODEX_TRAFFIC_LIGHT_STATE_PATH"] = state_path
    return subprocess.run(
        [sys.executable, os.path.join(PORT_DIR, "codex-light-mxp.py"), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def run_hook(state_path: str, event_name: str, payload: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CODEX_TRAFFIC_LIGHT_STATE_PATH"] = state_path
    return subprocess.run(
        [sys.executable, os.path.join(PORT_DIR, "codex-light-hook-mxp.py"), event_name],
        input=payload, capture_output=True, text=True, env=env, timeout=60,
    )


def test_cli_smoke():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")

        # clear
        r = run_cli(state_path, "clear")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "idle"

        # manual quota
        r = run_cli(state_path, "quota", "--five-hour", "72", "--weekly", "48")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "idle"

        # quota via stdin (pipe JSON like the macOS smoke test)
        r = subprocess.run(
            [sys.executable, os.path.join(PORT_DIR, "codex-light-mxp.py"),
             "quota", "--stdin", "--json"],
            input='{"quota":{"fiveHourRemainingPercent":71,"weeklyRemainingPercent":47}}',
            capture_output=True, text=True,
            env={**os.environ, "CODEX_TRAFFIC_LIGHT_STATE_PATH": state_path}, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        assert data["quota"]["five_hour_remaining_percent"] == 71
        assert data["quota"]["weekly_remaining_percent"] == 47

        # set working
        r = run_cli(state_path, "--task", "demo-a", "working")
        assert r.stdout.strip() == "working"

        # status json
        r = run_cli(state_path, "--json", "status")
        data = json.loads(r.stdout)
        assert data["aggregate_state"] == "working"
        assert data["tasks"]["demo-a"]["state"] == "working"
        assert data["quota"]["five_hour_remaining_percent"] == 71

        # hook: Stop that looks waiting -> aggregate waiting
        payload = json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": "需要你确认授权后我才能继续。",
            "cwd": "C:/demo",
            "session_id": "demo-b",
        })
        r = run_hook(state_path, "Stop", payload)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "{}"
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "waiting"

        # retire the manual task so the hook task controls the aggregate
        r = run_cli(state_path, "--task", "demo-a", "idle")
        assert r.stdout.strip() == "waiting"

        # hook: PreToolUse starts working again
        r = run_hook(state_path, "PreToolUse", json.dumps({
            "hook_event_name": "PreToolUse", "cwd": "C:/demo", "session_id": "demo-b"}))
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "working"

        # hook: Stop without waiting markers -> done
        r = run_hook(state_path, "Stop", json.dumps({
            "hook_event_name": "Stop",
            "last_assistant_message": "任务完成，可以直接验收。",
            "cwd": "C:/demo",
            "session_id": "demo-b",
        }))
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "done"

        # quota-only event keeps task state
        r = run_hook(state_path, "RateLimitsUpdated", json.dumps({
            "hook_event_name": "RateLimitsUpdated",
            "quota": {"five_hour_remaining_percent": 66, "weekly_remaining_percent": 44},
        }))
        assert r.returncode == 0
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "done"  # task untouched
        r = run_cli(state_path, "--json", "status")
        assert json.loads(r.stdout)["quota"]["five_hour_remaining_percent"] == 66

        # clear keeps quota
        r = run_cli(state_path, "clear")
        assert r.stdout.strip() == "idle"
        r = run_cli(state_path, "--json", "status")
        data = json.loads(r.stdout)
        assert data["quota"]["five_hour_remaining_percent"] == 66
        assert data["tasks"] == {}

        # quit
        r = run_cli(state_path, "quit")
        assert r.stdout.strip() == "quit"
        r = run_cli(state_path, "--json", "status")
        assert json.loads(r.stdout)["aggregate_state"] == "quit"

        # hook log written
        log_path = os.path.join(tmp, "hook-mxp.log")
        os.environ["CODEX_TRAFFIC_LIGHT_HOOK_LOG_PATH"] = log_path
        try:
            run_hook(state_path, "Stop", json.dumps({
                "hook_event_name": "Stop", "session_id": "demo-c"}))
            with open(log_path, "r", encoding="utf-8") as fh:
                log = fh.read()
            assert "event=Stop" in log
            assert "state=done" in log
            assert "result=ok" in log
        finally:
            os.environ.pop("CODEX_TRAFFIC_LIGHT_HOOK_LOG_PATH", None)

        # error handling: bad command
        r = run_cli(state_path, "bogus")
        assert r.returncode == 2
        r = run_cli(state_path, "quota")
        assert r.returncode == 2


def test_multi_task_aggregation_via_cli():
    with tempfile.TemporaryDirectory() as tmp:
        state_path = os.path.join(tmp, "state.json")
        run_cli(state_path, "clear")
        run_cli(state_path, "--task", "a", "working")
        run_cli(state_path, "--task", "b", "done")
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "working"  # working > done
        run_cli(state_path, "--task", "a", "idle")
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "done"
        run_cli(state_path, "--task", "b", "waiting")
        r = run_cli(state_path, "status")
        assert r.stdout.strip() == "waiting"  # waiting wins


def test_hermes_watcher_probe():
    """hermes-watcher state decisions against a synthetic state.db."""
    import importlib.util
    import sqlite3
    from codex_light.core import LightState

    # hermes-watcher.py has a hyphen -> load via importlib
    watcher_path = os.path.join(PORT_DIR, "hermes-watcher.py")
    spec = importlib.util.spec_from_file_location("hermes_watcher_mod", watcher_path)
    watcher_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watcher_mod)
    probe = watcher_mod.probe

    def make_db(path, messages):
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
            "role TEXT, content TEXT, timestamp REAL, finish_reason TEXT)"
        )
        for role, content, ts, finish in messages:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, finish_reason) "
                "VALUES (?,?,?,?,?)",
                ("s", role, content, ts, finish),
            )
        conn.commit()
        conn.close()

    now = time.time()
    cases = [
        ([("user", "go", now - 40, None), ("tool", '{"output":"x"}', now - 10, None)],
         LightState.working),
        ([("assistant", "需要你确认授权后我才能继续。", now - 25, "stop")],
         LightState.waiting),
        ([("assistant", "任务已完成，结果如上。", now - 30, "stop")],
         LightState.done),
        ([("assistant", "旧消息", now - 900, "stop")],
         LightState.idle),
        ([("assistant", "思考中", now - 5, "tool_calls")],
         LightState.working),
        ([("user", "新指令", now - 5, None)],
         LightState.working),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for i, (messages, expected) in enumerate(cases):
            db = os.path.join(tmp, f"case{i}.db")
            make_db(db, messages)
            state, _reason = probe(db, activity_window=60, wait_confirm=15, done_window=120)
            assert state == expected, f"case{i}: expected {expected.value}, got {state.value}"
    # unavailable DB -> idle without raising
    state, reason = probe(os.path.join(tmp := tempfile.mkdtemp(), "missing.db"),
                          activity_window=60, wait_confirm=15, done_window=120)
    assert state == LightState.idle and reason == "db_unavailable"


def main() -> None:
    names = sorted(k for k in globals() if k.startswith("test_"))
    print(f"Codex Traffic Light MXP (Windows) — {len(names)} tests")
    for name in names:
        check(name, globals()[name])
    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s): {FAILURES}")
        sys.exit(1)
    print("\nAll tests passed.")


if __name__ == "__main__":
    main()
