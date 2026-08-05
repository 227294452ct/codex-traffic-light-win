"""Codex Traffic Light MXP (Windows port) — floating traffic light GUI.

tkinter implementation of the macOS AppKit UI:
  * borderless always-on-top floating window, draggable, double-click hides
  * dark body with three lenses (red/yellow/green), status label, quota bars
  * polls state.json every 250ms; waiting => blinking red + alert sound,
    done => green + 3s chime, auto-idle after CODEX_LIGHT_DONE_IDLE_SECONDS
  * right-click context menu mirrors the macOS menu-bar menu
  * quota refreshed from `codex app-server --stdio` every 5 minutes
  * single instance (binds a loopback port)

Requires: Python 3 (tkinter + winsound, both part of the standard install).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from codex_light.core import (  # noqa: E402
    Defaults,
    LightState,
    QuotaSnapshot,
    StateStore,
    default_preferences_path,
    resolve_task_id,
    resolve_workspace,
)  # noqa: E402
from codex_light.quota import (  # noqa: E402
    CodexAppServerQuotaCollector,
    QuotaRefreshCoordinator,
    append_quota_log,
)

SINGLE_INSTANCE_PORT = 47780

# Palette (from the macOS TrafficLightView)
BODY_TOP = "#34383d"
BODY_BOTTOM = "#171a1e"
RED = "#f3423b"
YELLOW = "#ffd441"
GREEN = "#55d34d"
TEAL = "#61d6c7"
LIME = "#8bd96b"
TITLE_COLOR = "#8f949b"
STATUS_COLOR = "#a8adb4"
FONT_FAMILY = "Microsoft YaHei UI"

# Layout (from TrafficLightLayout.default, macOS parity)
WINDOW_W, WINDOW_H = 116, 388
LENS_CENTERS = {  # slot -> (x, y)
    LightState.waiting: (58, 294),  # red
    LightState.working: (58, 214),  # yellow
    LightState.done: (58, 134),     # green
}
LENS_GLOW_RADIUS = 42
LENS_BULB_RADIUS = 29
TITLE_RECT = (0, 346, WINDOW_W, 24)
STATUS_RECT = (8, 62, WINDOW_W - 16, 22)
QUOTA_ROWS = [
    {"label": "5小时", "base_y": 42, "accent": TEAL},
    {"label": "1周", "base_y": 22, "accent": LIME},
]
QUOTA_CONTENT_X = 24
QUOTA_CONTENT_W = 68
QUOTA_LABEL_W = 30
QUOTA_GAP = 4


def blend(hex_color: str, alpha: float, background: str = "#1b1e23") -> str:
    """Blend hex color over background at alpha -> CSS hex (no tkinter color ops)."""
    def parse(h: str) -> tuple:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    fg, bg = parse(hex_color), parse(background)
    mixed = tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))
    return "#%02x%02x%02x" % mixed


class Preferences:
    FILE_DEFAULTS = {
        "muted": False,
        "show_floating_window": True,
        "auto_show_on_done": True,
        "auto_show_on_waiting": True,
        "updated_at": 0.0,
    }

    def __init__(self, path: str):
        self.path = path
        self.values = dict(self.FILE_DEFAULTS)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in self.FILE_DEFAULTS:
                    if key in data:
                        self.values[key] = data[key]
        except (OSError, ValueError):
            pass

    @property
    def muted(self) -> bool:
        return bool(self.values["muted"])

    @muted.setter
    def muted(self, value: bool):
        self.values["muted"] = value
        self.save()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.values["updated_at"] = time.time()
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump(self.values, fh, ensure_ascii=False, indent=2, sort_keys=True)
        except OSError:
            pass


class SoundController:
    """winsound-based prompts: green = SystemAsterisk loop 3s, red = SystemHand loop."""

    def __init__(self, muted: bool):
        self.muted = muted
        self._green_timer = None
        self._red_timer = None
        self._root: tk.Tk | None = None

    def attach_root(self, root: tk.Tk) -> None:
        self._root = root

    @staticmethod
    def _play_loop(alias: str) -> None:
        try:
            import winsound
            winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_LOOP)
        except (ImportError, RuntimeError):
            pass

    @staticmethod
    def _stop_sound() -> None:
        try:
            import winsound
            winsound.PlaySound(None, 0)
        except (ImportError, RuntimeError):
            pass

    def apply(self, state: LightState, play_prompt: bool) -> None:
        if self.muted:
            self.stop_all()
            return
        if state != LightState.done:
            self.stop_green()
        if state != LightState.waiting:
            self.stop_red()
        if not play_prompt:
            return
        if state == LightState.done:
            self.play_green_for_three_seconds()
        elif state == LightState.waiting:
            self.play_red_for_alert_window()

    def play_green_for_three_seconds(self) -> None:
        self.stop_green()
        self._play_loop("SystemAsterisk")
        root = self._root
        if root is not None:
            self._green_timer = root.after(3000, self.stop_green)

    def play_red_for_alert_window(self) -> None:
        self.stop_red()
        self._play_loop("SystemHand")
        root = self._root
        if root is not None:
            self._red_timer = root.after(
                int(Defaults.waiting_alert_seconds * 1000), self.stop_red
            )

    def stop_green(self) -> None:
        if self._green_timer is not None:
            root = self._root
            if root is not None:
                try:
                    root.after_cancel(self._green_timer)
                except (tk.TclError, ValueError):
                    pass
            self._green_timer = None
        self._stop_sound()

    def stop_red(self) -> None:
        if self._red_timer is not None:
            root = self._root
            if root is not None:
                try:
                    root.after_cancel(self._red_timer)
                except (tk.TclError, ValueError):
                    pass
            self._red_timer = None
        self._stop_sound()

    def stop_all(self) -> None:
        self.stop_green()
        self.stop_red()


class TrafficLightApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.store = StateStore()
        self.preferences = Preferences(default_preferences_path())
        self.sound = SoundController(self.preferences.muted)
        self.sound.attach_root(root)
        self.quota_coordinator = QuotaRefreshCoordinator()

        self.current_state: LightState = LightState.idle
        self.current_quota: QuotaSnapshot | None = None
        self.last_modified = 0.0
        self.blink_on = True
        self.waiting_alert_active = False
        self._waiting_blink_stop = None
        self._idle_timer = None
        self._quota_in_flight = False

        self._build_window()
        snapshot = self.store.read()
        self.current_quota = snapshot.quota
        self.current_state = (
            LightState.idle if snapshot.aggregate_state == LightState.quit
            else snapshot.aggregate_state
        )
        self.apply_state(self.current_state, play_prompt=False, source="startup")

        root.after(250, self._poll_state)
        root.after(520, self._blink_tick)
        root.after(1500, self._refresh_quota)
        root.after(int(Defaults.app_server_quota_refresh_seconds * 1000),
                   self._schedule_quota_loop)

    # ------------------------------------------------------------------ UI

    def _build_window(self) -> None:
        root = self.root
        root.title("Codex Traffic Light MXP")
        root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg="#1b1e23")
        root.resizable(False, False)

        self.canvas = tk.Canvas(root, width=WINDOW_W, height=WINDOW_H,
                                bg="#1b1e23", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<Button-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.canvas.bind("<Button-3>", self._on_context_menu)
        root.bind("<Button-3>", self._on_context_menu)

        # remember position between runs
        self._geometry_file = os.path.join(
            os.path.dirname(self.store.state_path), "window.json"
        )
        try:
            with open(self._geometry_file, "r", encoding="utf-8") as fh:
                geo = json.load(fh)
            x, y = int(geo["x"]), int(geo["y"])
            screen_w = root.winfo_screenwidth()
            screen_h = root.winfo_screenheight()
            if 0 <= x <= screen_w - 40 and 0 <= y <= screen_h - 40:
                root.geometry(f"+{x}+{y}")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_offset = (event.x_root - self.root.winfo_x(),
                             event.y_root - self.root.winfo_y())

    def _on_drag_move(self, event: tk.Event) -> None:
        if getattr(self, "_drag_offset", None) is None:
            return
        dx, dy = self._drag_offset
        x, y = event.x_root - dx, event.y_root - dy
        self.root.geometry(f"+{x}+{y}")

    def _on_double_click(self, _event: tk.Event) -> None:
        self.root.withdraw()
        self._drag_offset = None

    def _on_context_menu(self, event: tk.Event) -> None:
        menu = tk.Menu(self.root, tearoff=0, font=(FONT_FAMILY, 9))
        menu.add_command(label=f"当前：{self.current_state.label}", state="disabled")
        menu.add_command(label=f"额度：{self._quota_text()}", state="disabled")
        menu.add_separator()
        menu.add_command(label="显示/隐藏红绿灯", command=self._toggle_window)
        menu.add_command(
            label="静音提示音" if not self.preferences.muted else "恢复提示音",
            command=self._toggle_mute,
        )
        menu.add_separator()
        menu.add_command(label="黄灯：正在干活", command=lambda: self._manual_state(LightState.working))
        menu.add_command(label="绿灯：完成验收", command=lambda: self._manual_state(LightState.done))
        menu.add_command(label="红灯：等你回复", command=lambda: self._manual_state(LightState.waiting))
        menu.add_command(label="全暗：空闲", command=lambda: self._manual_state(LightState.idle))
        menu.add_command(label="清空失联任务", command=self._clear_tasks)
        menu.add_separator()
        menu.add_command(label="退出", command=self._quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_window(self) -> None:
        if self.root.state() == "withdrawn":
            self.root.deiconify()
        else:
            self.root.withdraw()

    def _toggle_mute(self) -> None:
        self.preferences.muted = not self.preferences.muted
        self.sound.muted = self.preferences.muted
        self.sound.stop_all()

    def _manual_state(self, state: LightState) -> None:
        workspace = resolve_workspace(None)
        task_id = resolve_task_id("manual", workspace)
        try:
            snapshot = self.store.update_task(
                task_id=task_id, state=state, workspace=workspace,
                source="menu", hook_event_name=None,
                message=f"Codex traffic light: {state.value}",
            )
        except OSError:
            return
        self.current_quota = snapshot.quota
        self.apply_state(snapshot.aggregate_state, play_prompt=True, source="user")

    def _clear_tasks(self) -> None:
        try:
            snapshot = self.store.clear()
        except OSError:
            return
        self.current_quota = snapshot.quota
        self.apply_state(LightState.idle, play_prompt=False, source="user")

    def _quit(self) -> None:
        self.root.destroy()

    # ------------------------------------------------------------ state loop

    def _poll_state(self) -> None:
        try:
            modified = os.path.getmtime(self.store.state_path)
        except OSError:
            modified = 0.0
        if modified != self.last_modified:
            self.last_modified = modified
            snapshot = self.store.read()
            self.current_quota = snapshot.quota
            if snapshot.aggregate_state == LightState.quit:
                self.root.destroy()
                return
            if snapshot.aggregate_state != self.current_state:
                self.apply_state(snapshot.aggregate_state, play_prompt=True, source="file")
            else:
                self.update_display()
        self.root.after(250, self._poll_state)

    def _blink_tick(self) -> None:
        if self.current_state == LightState.waiting and self.waiting_alert_active:
            self.blink_on = not self.blink_on
            self._draw()
        self.root.after(520, self._blink_tick)

    def apply_state(self, state: LightState, play_prompt: bool, source: str) -> None:
        self.current_state = state
        self.sound.apply(state, play_prompt)
        if state == LightState.waiting:
            self._start_waiting_blink()
        else:
            self._stop_waiting_blink()
        if state == LightState.done:
            self._start_idle_timer()
        else:
            if self._idle_timer is not None:
                try:
                    self.root.after_cancel(self._idle_timer)
                except (tk.TclError, ValueError):
                    pass
                self._idle_timer = None
        self.update_display()

    def _start_waiting_blink(self) -> None:
        if self._waiting_blink_stop is not None:
            try:
                self.root.after_cancel(self._waiting_blink_stop)
            except (tk.TclError, ValueError):
                pass
        self.waiting_alert_active = True
        self.blink_on = True
        self._waiting_blink_stop = self.root.after(
            int(Defaults.waiting_alert_seconds * 1000),
            self._waiting_blink_stop_fired,
        )

    def _waiting_blink_stop_fired(self) -> None:
        self.waiting_alert_active = False
        self.blink_on = True
        self._waiting_blink_stop = None
        self._draw()

    def _stop_waiting_blink(self) -> None:
        if self._waiting_blink_stop is not None:
            try:
                self.root.after_cancel(self._waiting_blink_stop)
            except (tk.TclError, ValueError):
                pass
            self._waiting_blink_stop = None
        self.waiting_alert_active = False
        self.blink_on = True

    def _start_idle_timer(self) -> None:
        if self._idle_timer is not None:
            try:
                self.root.after_cancel(self._idle_timer)
            except (tk.TclError, ValueError):
                pass
        self._idle_timer = self.root.after(
            int(Defaults.done_auto_idle_seconds * 1000), self._idle_timer_fired
        )

    def _idle_timer_fired(self) -> None:
        self._idle_timer = None
        if self.current_state != LightState.done:
            return
        try:
            snapshot = self.store.clear()
        except OSError:
            return
        self.current_quota = snapshot.quota
        self.apply_state(LightState.idle, play_prompt=False, source="timer")

    # -------------------------------------------------------------- drawing

    def update_display(self) -> None:
        if self.root.state() != "withdrawn":
            self._draw()
        else:
            # redraw anyway to keep canvas state fresh for when it reappears
            self._draw()

    def _active_slot(self):
        return {
            LightState.waiting: LightState.waiting,
            LightState.working: LightState.working,
            LightState.done: LightState.done,
        }.get(self.current_state)

    def _slot_color(self, slot: LightState) -> str:
        return {LightState.waiting: RED, LightState.working: YELLOW,
                LightState.done: GREEN}[slot]

    def _draw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")

        # body
        body_x, body_y = 16, 12
        body_w, body_h = WINDOW_W - 32, WINDOW_H - 24
        canvas.create_rectangle(body_x, body_y, body_x + body_w, body_y + body_h,
                                fill="#1f2329", outline="#3a3f46", width=1)
        canvas.create_rectangle(body_x + 1, body_y + 1, body_x + body_w - 1,
                                body_y + 10, fill="#2e3238", outline="")

        # title
        title_x, title_y, title_w, title_h = TITLE_RECT
        canvas.create_text(title_x + title_w / 2, title_y + title_h / 2,
                           text="Codex", fill=TITLE_COLOR,
                           font=(FONT_FAMILY, 12, "bold"))

        # lenses
        active_slot = self._active_slot()
        for slot, (cx, cy) in LENS_CENTERS.items():
            color = self._slot_color(slot)
            active = slot == active_slot
            if active and slot == LightState.waiting and self.waiting_alert_active:
                visible = self.blink_on
            else:
                visible = active
            # glow
            canvas.create_oval(cx - LENS_GLOW_RADIUS, cy - LENS_GLOW_RADIUS,
                               cx + LENS_GLOW_RADIUS, cy + LENS_GLOW_RADIUS,
                               fill=blend(color, 0.30 if visible else 0.04),
                               outline="")
            # bulb
            r = LENS_BULB_RADIUS
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                               fill=blend(color, 1.0 if visible else 0.20),
                               outline=blend(color, 0.42 if visible else 0.14), width=6)
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                               outline=blend("#000000", 0.23), width=1)
            # highlight
            canvas.create_oval(cx - 12, cy + 12, cx + 10, cy + 20,
                               fill=blend("#ffffff", 0.24 if visible else 0.08),
                               outline="")

        # status label
        sx, sy, sw, sh = STATUS_RECT
        canvas.create_text(sx + sw / 2, sy + sh / 2, text=self.current_state.label,
                           fill=STATUS_COLOR, font=(FONT_FAMILY, 9))

        # quota rows
        for row in QUOTA_ROWS:
            self._draw_quota_row(row)

    def _draw_quota_row(self, row: dict) -> None:
        canvas = self.canvas
        label = row["label"]
        base_y = row["base_y"]
        accent = row["accent"]
        percent = None
        if self.current_quota is not None:
            percent = self.current_quota.five_hour_remaining_percent if label == "5小时" \
                else self.current_quota.weekly_remaining_percent
        clamped = min(100, max(0, percent)) if percent is not None else None
        value = f"{clamped}%" if clamped is not None else "--"

        # progress bar
        bar_x, bar_y = QUOTA_CONTENT_X, base_y
        bar_w, bar_h = QUOTA_CONTENT_W, 3
        canvas.create_rectangle(bar_x, bar_y, bar_x + bar_w, bar_y + bar_h,
                                fill="#2b3036", outline="")
        if clamped is not None and clamped > 0:
            fill_w = bar_w * clamped / 100
            canvas.create_rectangle(bar_x, bar_y, bar_x + fill_w, bar_y + bar_h,
                                    fill=blend(accent, 0.72), outline="")

        # label + value
        text_y = base_y + 5
        canvas.create_text(bar_x, text_y, text=label, anchor="w", fill="#8f949b",
                           font=(FONT_FAMILY, 7))
        canvas.create_text(bar_x + QUOTA_CONTENT_W, text_y, text=value, anchor="e",
                           fill="#d6dae0" if clamped is not None else "#6a7078",
                           font=(FONT_FAMILY, 8, "bold"))

    # --------------------------------------------------------------- quota

    def _schedule_quota_loop(self) -> None:
        self._refresh_quota()
        self.root.after(int(Defaults.app_server_quota_refresh_seconds * 1000),
                        self._schedule_quota_loop)

    def _refresh_quota(self) -> None:
        if self._quota_in_flight:
            return
        if not self.quota_coordinator.begin_refresh():
            return
        self._quota_in_flight = True

        def worker() -> None:
            try:
                snapshot = CodexAppServerQuotaCollector().fetch_and_update(self.store)
                self.root.after(0, lambda: self._handle_quota_snapshot(snapshot))
                self.root.after(0, lambda: self.quota_coordinator.end_refresh(True))
            except Exception as exc:  # noqa: BLE001
                line = self.quota_coordinator.failure_log_line(exc)
                if line:
                    append_quota_log(line)
                self.root.after(0, lambda: self.quota_coordinator.end_refresh(False))
            finally:
                self._quota_in_flight = False

        threading.Thread(target=worker, daemon=True).start()

    def _handle_quota_snapshot(self, snapshot) -> None:
        self.current_quota = snapshot.quota
        if snapshot.aggregate_state != LightState.quit:
            self.current_state = snapshot.aggregate_state
        self.update_display()

    def _quota_text(self) -> str:
        if self.current_quota is None:
            return "暂无数据"
        return (f"5小时 {self.current_quota.five_hour_remaining_percent}% · "
                f"1周 {self.current_quota.weekly_remaining_percent}%")

    # ------------------------------------------------------------- teardown

    def on_close(self) -> None:
        try:
            with open(self._geometry_file, "w", encoding="utf-8") as fh:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, fh)
        except OSError:
            pass
        self.sound.stop_all()


def _enable_dpi_awareness() -> None:
    """Make Tk render at native resolution instead of bitmap-scaled (blurry)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
    except (AttributeError, OSError):
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main() -> int:
    _enable_dpi_awareness()
    # single instance guard
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        guard.listen(1)
    except OSError:
        return 0  # another instance is already running

    root = tk.Tk()
    app = TrafficLightApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    try:
        root.mainloop()
    finally:
        app.on_close()
        guard.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
