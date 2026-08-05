"""Codex Traffic Light MXP — Windows port.

Quota: extraction from hook payloads + Codex app-server (JSON-RPC over stdio)
collection. Faithful port of QuotaExtractor.swift and CodexAppServerQuota.swift.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Optional

from .core import StateStore, QuotaSnapshot, StateSnapshot, default_quota_log_path

FIVE_HOUR_DURATION_MINS = 300
WEEKLY_DURATION_MINS = 10_080


class QuotaValues:
    def __init__(self, five_hour_remaining_percent: int, weekly_remaining_percent: int):
        self.five_hour_remaining_percent = min(100, max(0, five_hour_remaining_percent))
        self.weekly_remaining_percent = min(100, max(0, weekly_remaining_percent))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, QuotaValues)
            and self.five_hour_remaining_percent == other.five_hour_remaining_percent
            and self.weekly_remaining_percent == other.weekly_remaining_percent
        )

    def __repr__(self) -> str:
        return f"QuotaValues({self.five_hour_remaining_percent}, {self.weekly_remaining_percent})"

    @property
    def summary(self) -> str:
        return f"{self.five_hour_remaining_percent}/{self.weekly_remaining_percent}"


class QuotaExtractor:
    """Recursively find five-hour / weekly remaining percent in arbitrary JSON.

    Mirrors QuotaExtractor.swift: prefers keys five_hour_remaining_percent /
    fiveHourRemainingPercent and weekly_remaining_percent / weeklyRemainingPercent,
    searches preferred containers (quota / rate_limits / rateLimits) first, then
    every other key, then array elements.
    """

    _five_hour_keys = ["five_hour_remaining_percent", "fiveHourRemainingPercent"]
    _weekly_keys = ["weekly_remaining_percent", "weeklyRemainingPercent"]
    _preferred_container_keys = ["quota", "rate_limits", "rateLimits"]

    @classmethod
    def extract(cls, data: bytes) -> Optional[QuotaValues]:
        if not data:
            return None
        try:
            obj = json.loads(data.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return None
        return cls._extract_object(obj)

    @classmethod
    def _extract_object(cls, obj: Any) -> Optional[QuotaValues]:
        if isinstance(obj, dict):
            values = cls._values_in(obj)
            if values is not None:
                return values
            for key in cls._preferred_container_keys:
                if key in obj:
                    values = cls._extract_object(obj[key])
                    if values is not None:
                        return values
            for key in sorted(obj.keys()):
                if key in cls._preferred_container_keys:
                    continue
                values = cls._extract_object(obj[key])
                if values is not None:
                    return values
        elif isinstance(obj, list):
            for child in obj:
                values = cls._extract_object(child)
                if values is not None:
                    return values
        return None

    @classmethod
    def _values_in(cls, dictionary: dict) -> Optional[QuotaValues]:
        five_hour = cls._first_percent(dictionary, cls._five_hour_keys)
        weekly = cls._first_percent(dictionary, cls._weekly_keys)
        if five_hour is None or weekly is None:
            return None
        return QuotaValues(five_hour, weekly)

    @classmethod
    def _first_percent(cls, dictionary: dict, keys: list) -> Optional[int]:
        for key in keys:
            if key in dictionary:
                percent = cls._percent(dictionary[key])
                if percent is not None:
                    return percent
        return None

    @staticmethod
    def _percent(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value.strip()))
            except ValueError:
                return None
        return None


# ---------------------------------------------------------------------------
# Codex app-server JSON-RPC (line-delimited framing, mirrors Swift version)
# ---------------------------------------------------------------------------

class CodexAppServerQuotaError(Exception):
    def __init__(self, summary_key: str, message: str):
        super().__init__(message)
        self.summary_key = summary_key


def _line_encode_request(req_id: int, method: str, params: Any = None) -> bytes:
    obj: dict = {"id": req_id, "method": method}
    if params is not None:
        obj["params"] = params
    return _line_encode_message(obj)


def _line_encode_message(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _line_decode_messages(data: bytes) -> list[dict]:
    text = data.decode("utf-8", errors="replace")
    messages = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except ValueError:
            continue
    return messages


def _result_for_id(req_id: int, messages: list[dict]) -> Any:
    for message in messages:
        if message.get("id") != req_id:
            continue
        if "error" in message:
            raise CodexAppServerQuotaError(
                "appServerReturnedError", f"App-server error: {message['error']}"
            )
        if "result" not in message:
            raise CodexAppServerQuotaError("invalidJSON", "Invalid app-server JSON")
        return message["result"]
    raise CodexAppServerQuotaError("responseNotFound", f"App-server response id {req_id} was not found")


def _resolve_codex_binary() -> str:
    override = os.environ.get("CODEX_TRAFFIC_LIGHT_CODEX_BIN", "")
    if override:
        return override
    found = shutil.which("codex")
    return found or "codex"


def _spawn_app_server(codex_binary: str):
    """Spawn `codex app-server --stdio`. Returns (proc, write_fn)."""
    args = [codex_binary, "app-server", "--stdio"]
    creation_flags = 0
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # npm shims on Windows are .cmd scripts — must go through cmd.exe
    if os.name == "nt" and codex_binary.lower().endswith((".cmd", ".bat")):
        proc = subprocess.Popen(
            ["cmd", "/c"] + args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    else:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
    return proc


def read_rate_limits_from_app_server(
    codex_binary: Optional[str] = None,
    initialize_timeout: float = 50.0,
    rate_limits_timeout: float = 20.0,
) -> bytes:
    """Run the experimental `codex app-server --stdio` flow and return the raw
    `account/rateLimits/read` result JSON. Mirrors ProcessCodexAppServerTransport."""
    codex_binary = codex_binary or _resolve_codex_binary()
    try:
        proc = _spawn_app_server(codex_binary)
    except OSError as exc:
        raise CodexAppServerQuotaError("launchFailed", f"could not launch codex app-server: {exc}")

    buffer = bytearray()

    def pump() -> None:
        assert proc.stdout is not None
        chunk = proc.stdout.read(4096)
        if chunk:
            buffer.extend(chunk)

    try:
        # -- initialize (id 1) ------------------------------------------------
        init_payload = _line_encode_request(
            1,
            "initialize",
            {
                "clientInfo": {"name": "codex-light-mxp", "version": "1"},
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                    "optOutNotificationMethods": [],
                },
            },
        )
        assert proc.stdin is not None
        proc.stdin.write(init_payload)
        proc.stdin.flush()

        deadline = time.monotonic() + initialize_timeout
        initialized = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            pump()
            try:
                _result_for_id(1, _line_decode_messages(bytes(buffer)))
                initialized = True
                break
            except CodexAppServerQuotaError:
                pass
            time.sleep(0.05)
        if not initialized:
            raise CodexAppServerQuotaError(
                "initializeTimedOut", f"initialize timed out after {initialize_timeout}s"
            )

        # -- initialized notification + account/rateLimits/read (id 2) --------
        proc.stdin.write(_line_encode_message({"method": "initialized"}))
        proc.stdin.write(_line_encode_request(2, "account/rateLimits/read"))
        proc.stdin.flush()

        deadline = time.monotonic() + rate_limits_timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            pump()
            try:
                result = _result_for_id(2, _line_decode_messages(bytes(buffer)))
                return json.dumps(result, sort_keys=True).encode("utf-8")
            except CodexAppServerQuotaError:
                pass
            time.sleep(0.05)

        stderr = ""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
            except OSError:
                pass
        if stderr:
            raise CodexAppServerQuotaError("processFailed", f"App-server process failed: {stderr}")
        raise CodexAppServerQuotaError(
            "rateLimitsTimedOut", f"rate limits read timed out after {rate_limits_timeout}s"
        )
    finally:
        try:
            proc.terminate()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Response mapping (mirrors CodexAppServerQuotaMapper.swift)
# ---------------------------------------------------------------------------

def quota_values_from_app_server_response(data: bytes) -> QuotaValues:
    try:
        response = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CodexAppServerQuotaError("invalidJSON", "Invalid app-server JSON")

    snapshot = _codex_snapshot(response)
    if snapshot is None:
        raise CodexAppServerQuotaError(
            "missingQuota", "App-server response did not include Codex 5-hour and weekly quota"
        )
    windows = [snapshot.get("primary"), snapshot.get("secondary")]
    windows = [w for w in windows if isinstance(w, dict)]

    def find(duration: int) -> Optional[dict]:
        for w in windows:
            if w.get("windowDurationMins") == duration:
                return w
        # fallback: window without explicit duration in that slot position
        return None

    five_hour_window = find(FIVE_HOUR_DURATION_MINS)
    weekly_window = find(WEEKLY_DURATION_MINS)
    if five_hour_window is None and isinstance(snapshot.get("primary"), dict) \
            and snapshot["primary"].get("windowDurationMins") is None:
        five_hour_window = snapshot["primary"]
    if weekly_window is None and isinstance(snapshot.get("secondary"), dict) \
            and snapshot["secondary"].get("windowDurationMins") is None:
        weekly_window = snapshot["secondary"]

    if five_hour_window is None or weekly_window is None:
        raise CodexAppServerQuotaError(
            "missingQuota", "App-server response did not include Codex 5-hour and weekly quota"
        )

    def remaining(used: Any) -> int:
        try:
            value = round(100 - float(used))
        except (TypeError, ValueError):
            value = 0
        return min(100, max(0, int(value)))

    return QuotaValues(
        five_hour_remaining_percent=remaining(five_hour_window.get("usedPercent")),
        weekly_remaining_percent=remaining(weekly_window.get("usedPercent")),
    )


def _codex_snapshot(response: dict) -> Optional[dict]:
    by_limit_id = response.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, dict) and isinstance(by_limit_id.get("codex"), dict):
        return by_limit_id["codex"]
    if isinstance(by_limit_id, dict):
        for snapshot in by_limit_id.values():
            if isinstance(snapshot, dict) and snapshot.get("limitId") == "codex":
                return snapshot
    rate_limits = response.get("rateLimits")
    return rate_limits if isinstance(rate_limits, dict) else None


# ---------------------------------------------------------------------------
# Collector with retry + failure log throttling (mirrors QuotaRefreshCoordinator)
# ---------------------------------------------------------------------------

class CodexAppServerQuotaCollector:
    source = "codex-app-server"

    def __init__(self, retries: int = 2, backoff_seconds: Optional[list] = None):
        self.retries = max(0, retries)
        self.backoff_seconds = backoff_seconds if backoff_seconds is not None else [1, 3]

    def fetch_quota(self) -> QuotaValues:
        last_error: Optional[CodexAppServerQuotaError] = None
        max_attempts = self.retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                data = read_rate_limits_from_app_server()
                return quota_values_from_app_server_response(data)
            except CodexAppServerQuotaError as exc:
                last_error = exc
                if attempt < max_attempts:
                    backoff = self.backoff_seconds[attempt - 1] \
                        if attempt - 1 < len(self.backoff_seconds) else 0
                    if backoff > 0:
                        time.sleep(backoff)
        raise CodexAppServerQuotaError(
            "retryExhausted:" + (last_error.summary_key if last_error else "missingQuota"),
            f"App-server quota failed after {max_attempts} attempts: {last_error}",
        )

    def fetch_and_update(self, store: StateStore) -> StateSnapshot:
        quota = self.fetch_quota()
        return store.update_quota(
            five_hour_percent=quota.five_hour_remaining_percent,
            weekly_percent=quota.weekly_remaining_percent,
            source=self.source,
        )


class QuotaRefreshCoordinator:
    """Prevents overlapping refreshes; throttles repeated failure logs (10 min)."""

    def __init__(self, log_throttle_seconds: float = 10 * 60):
        self.log_throttle_seconds = log_throttle_seconds
        self.refresh_in_flight = False
        self.last_failure_key: Optional[str] = None
        self.last_failure_logged_at: Optional[float] = None

    def begin_refresh(self) -> bool:
        if self.refresh_in_flight:
            return False
        self.refresh_in_flight = True
        return True

    def end_refresh(self, success: bool) -> None:
        self.refresh_in_flight = False
        if success:
            self.last_failure_key = None
            self.last_failure_logged_at = None

    def failure_log_line(self, error: Exception) -> Optional[str]:
        key = error.summary_key if isinstance(error, CodexAppServerQuotaError) \
            else type(error).__name__
        now = time.time()
        if key == self.last_failure_key and self.last_failure_logged_at is not None \
                and now - self.last_failure_logged_at < self.log_throttle_seconds:
            return None
        self.last_failure_key = key
        self.last_failure_logged_at = now
        return f"quota app-server failed: {error}"


def append_quota_log(line: str, log_path: Optional[str] = None) -> None:
    """Append a timestamped line to quota-mxp.log (best effort)."""
    try:
        log_path = log_path or default_quota_log_path()
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + \
            f".{int(time.time() * 1000) % 1000:03d}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} {line}\n")
    except OSError:
        pass
