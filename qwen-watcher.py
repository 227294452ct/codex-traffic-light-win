"""Qwen Work (千问办公) -> Codex Traffic Light MXP bridge (Windows port).

Monitors the Qwen Work Electron app (QwenWorkCN.exe). While it is running,
samples its total CPU time (kernel+user, via GetProcessTimes) every couple of
seconds; sustained CPU consumption inside the activity window means the app's
agent is actually doing something -> working. A running-but-quiet app is
idle; once the app is closed the task is dropped from the shared store.

Pure stdlib (ctypes) so it runs anywhere pythonw exists, no psutil needed.

State rules (env-tunable):
  QWEN_LIGHT_ACTIVITY_WINDOW_SECONDS (60)  sliding window for CPU sampling
  QWEN_LIGHT_CPU_THRESHOLD (0.03)          avg CPU rate (fraction) that counts as working
  QWEN_LIGHT_HEAVY_THRESHOLD (0.35)        CPU rate that counts as working even when the
                                           app window is hidden (background heavy task)
  QWEN_LIGHT_POLL_SECONDS (2.0)            sampling interval

Working is only reported while the app window is actually on-screen (user is
using it) OR the CPU rate is heavy enough that a real background task must be
running. A hidden/minimized app burning modest CPU (Electron idle overhead,
sync, etc.) no longer keeps the light stuck on yellow.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from codex_light.core import LightState, StateStore  # noqa: E402

TASK_ID = "qwen:watcher"
TASK_SOURCE = "qwen-watcher"
PROCESS_NAME = "QwenWorkCN.exe"

# --- win32 API via ctypes -------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
psapi.EnumProcesses.restype = wintypes.BOOL
psapi.EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

MAX_PIDS = 4096
MAX_PATH_W = 32768


def _ft_seconds(ft: wintypes.FILETIME) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def _qwen_process_pids() -> list[int]:
    """PIDs of every running QwenWorkCN.exe process (empty when app is closed)."""
    pids = (wintypes.DWORD * MAX_PIDS)()
    needed = wintypes.DWORD()
    if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
        return []
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    found: list[int] = []
    for i in range(min(count, MAX_PIDS)):
        pid = pids[i]
        if pid == 0:
            continue
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue  # access denied / gone
        try:
            buf = ctypes.create_unicode_buffer(MAX_PATH_W)
            size = wintypes.DWORD(MAX_PATH_W)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                continue
            if os.path.basename(buf.value).lower() == PROCESS_NAME.lower():
                found.append(pid)
        finally:
            kernel32.CloseHandle(handle)
    return found


def _process_cpu_seconds(pids: list[int]) -> float | None:
    """Total kernel+user CPU seconds across the given QwenWorkCN.exe PIDs.

    Returns None when the app is not running at all (empty PID list).
    """
    total = 0.0
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue  # access denied / gone
        try:
            creation = wintypes.FILETIME()
            exit_t = wintypes.FILETIME()
            kernel_t = wintypes.FILETIME()
            user_t = wintypes.FILETIME()
            if kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_t),
                                        ctypes.byref(kernel_t), ctypes.byref(user_t)):
                total += _ft_seconds(kernel_t) + _ft_seconds(user_t)
        finally:
            kernel32.CloseHandle(handle)
    return total if pids else None


# --- window visibility ------------------------------------------------------
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _qwen_window_visible(pids: list[int]) -> bool:
    """True when at least one QwenWorkCN main window is on-screen.

    A hidden/minimized window lives at an offscreen rect (e.g. -25600,-25600),
    which means the user is not actively using the app; background CPU alone
    should not count as "working" then.
    """
    if not pids:
        return False
    pid_set = set(pids)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    visible = False

    @WNDENUMPROC
    def _cb(hwnd, _lp):
        nonlocal visible
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value not in pid_set:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w >= 100 and h >= 50 and r.left >= -50 and r.top >= -50:
            visible = True
            return False
        return True

    user32.EnumWindows(_cb, 0)
    return visible


# --- env helpers ----------------------------------------------------------
def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return default


def default_log_path() -> str:
    override = os.environ.get("QWEN_LIGHT_LOG_PATH", "")
    if override:
        return override
    from codex_light.core import default_support_dir
    return os.path.join(default_support_dir(), "qwen-watcher.log")


def append_log(line: str, log_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


# --- main loop ------------------------------------------------------------
def main(argv: list) -> int:
    window = _env_float("QWEN_LIGHT_ACTIVITY_WINDOW_SECONDS", 60)
    threshold = _env_float("QWEN_LIGHT_CPU_THRESHOLD", 0.03)
    heavy_threshold = _env_float("QWEN_LIGHT_HEAVY_THRESHOLD", 0.35)
    poll_seconds = _env_float("QWEN_LIGHT_POLL_SECONDS", 2.0)
    log_path = default_log_path()
    store = StateStore()

    samples: list[tuple[float, float]] = []  # (monotonic_time, cpu_seconds)

    def current_state() -> tuple[LightState, str]:
        pids = _qwen_process_pids()
        cpu = _process_cpu_seconds(pids)
        now = time.monotonic()
        if cpu is None:
            samples.clear()
            return LightState.idle, "app_not_running"
        samples.append((now, cpu))
        # keep only the sliding window
        while samples and now - samples[0][0] > window:
            samples.pop(0)
        if len(samples) < 3:
            return LightState.idle, f"booting cpu_so_far={cpu:.1f}s"
        t0, c0 = samples[0]
        dt = now - t0
        dc = cpu - c0
        rate = dc / dt if dt > 0 else 0.0
        if rate >= heavy_threshold:
            return LightState.working, f"cpu_rate={rate:.1%} over {dt:.0f}s (heavy)"
        if rate >= threshold and _qwen_window_visible(pids):
            return LightState.working, f"cpu_rate={rate:.1%} over {dt:.0f}s (visible)"
        return LightState.idle, f"cpu_rate={rate:.2%} over {dt:.0f}s"

    last_state: LightState | None = None
    consecutive_errors = 0
    while True:
        try:
            state, reason = current_state()
            snapshot = store.read()
            existing = snapshot.tasks.get(TASK_ID)
            existing_state = existing.state if existing else None

            needs_write = state != existing_state
            if state == LightState.idle and existing is None:
                needs_write = False  # nothing to clean up

            if needs_write:
                snapshot = store.update_task(
                    task_id=TASK_ID, state=state,
                    workspace=None, source=TASK_SOURCE,
                    hook_event_name=None,
                    message=f"QwenWork traffic light: {state.value} ({reason})",
                )
                append_log(
                    f"state={state.value} reason={reason} aggregate={snapshot.aggregate_state.value}",
                    log_path,
                )
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
                raise  # persistent failure: let the startup guard respawn us
        time.sleep(poll_seconds)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
