"""Codex Traffic Light MXP (Windows port) — floating traffic light GUI.

tkinter implementation of the macOS AppKit UI:
  * borderless always-on-top floating window, draggable, double-click hides
  * compact Dynamic Island body with three lenses and a status label
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
    default_support_dir,
    resolve_task_id,
    resolve_workspace,
)  # noqa: E402
from codex_light.quota import (  # noqa: E402
    CodexAppServerQuotaCollector,
    QuotaRefreshCoordinator,
    append_quota_log,
)

SINGLE_INSTANCE_PORT = 47780


def light_off_path() -> str:
    """Flag file meaning 'user turned the light off'; guard + main() respect it."""
    return os.path.join(default_support_dir(), "light-off.json")

# Palette - "动态岛 Balanced": compact near-black capsule with a quiet rim,
# one state dot, and three small traffic-light lenses. WIN_BG sits outside the
# card gradient range so the transparency key cannot punch through the card.
RED = "#ff3b30"       # iOS system red
YELLOW = "#ffd60a"    # iOS system yellow
GREEN = "#34c759"     # iOS system green
FONT_FAMILY = "Segoe UI"

GLASS_TOP = "#17181b"        # card gradient (top)
GLASS_BOTTOM = "#050506"     # card gradient (bottom)
WIN_BG = "#070a16"           # window bg / transparency color key
CARD_RIM = "#303238"         # quiet capsule rim
CARD_SHEEN = "#ffffff"       # top inner highlight (unused when SHEEN_ALPHA=0)
SHEEN_ALPHA = 0.0
CORNER_RADIUS = 27           # continuous capsule curvature
SHADOW_COLOR = "#000000"     # base color for the drop-shadow layers
LENS_OFF_FILL = "#242529"    # unlit lens glass
LENS_OFF_RIM = "#34373c"     # unlit lens rim
LENS_ON_HILITE = "#ffffff"   # lens glass highlight
STATUS_TEXT = "#f8f9fc"      # primary text
NEON = False                 # Balanced keeps the body rim understated
RIM_WIDTHS = (2, 1)

# Layout: A / Balanced Dynamic Island. State marker + text on the left,
# divider in the middle, compact red/yellow/green lenses on the right.
WINDOW_W, WINDOW_H = 304, 60
LENS_CENTERS = {  # slot -> (x, y)
    LightState.waiting: (220, 29),  # red (left)
    LightState.working: (250, 29),  # yellow (middle)
    LightState.done: (280, 29),     # green (right)
}
LENS_BULB_RADIUS = 11
STATUS_DOT_CENTER = (22, 29)
STATUS_RECT = (36, 16, 150, 26)
DIVIDER_X = 198


def blend(hex_color: str, alpha: float, background: str = "#1b1e23") -> str:
    """Blend hex color over background at alpha -> CSS hex (no tkinter color ops)."""
    def parse(h: str) -> tuple:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    fg, bg = parse(hex_color), parse(background)
    mixed = tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))
    return "#%02x%02x%02x" % mixed


def lerp_color(c1: str, c2: str, t: float) -> str:
    """Linear interpolation between two hex colors at t in [0, 1]."""
    def parse(h: str) -> tuple:
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    a, b = parse(c1), parse(c2)
    return "#%02x%02x%02x" % tuple(
        round(x + (y - x) * t) for x, y in zip(a, b))


def rounded_rect(canvas, x1: float, y1: float, x2: float, y2: float,
                 r: float, **kwargs):
    """Rounded rectangle via smooth polygon (canvas has no native roundrect)."""
    if r <= 0:
        return canvas.create_rectangle(x1, y1, x2, y2, **kwargs)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, splinesteps=20, **kwargs)


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
        self._static_items = {}
        self._lens_items = {}
        self._layered_renderer = None

        self._build_window()
        snapshot = self.store.read()
        self.current_quota = snapshot.quota
        self.current_state = (
            LightState.idle if snapshot.aggregate_state == LightState.quit
            else snapshot.aggregate_state
        )
        self.apply_state(self.current_state, play_prompt=False, source="startup")

        # Tk may issue one native paint when the first mainloop starts. Reapply
        # the layered surface immediately afterwards so it remains authoritative.
        root.after_idle(self._draw)
        root.after(250, self._poll_state)
        root.after(520, self._blink_tick)
        root.after(1500, self._refresh_quota)
        root.after(2000, self._heal_dpi)
        root.after(6000, self._heal_dpi)
        root.after(int(Defaults.app_server_quota_refresh_seconds * 1000),
                   self._schedule_quota_loop)

    # ------------------------------------------------------------------ UI

    def _build_window(self) -> None:
        root = self.root
        root.title("Codex Traffic Light MXP")
        root.geometry(f"{WINDOW_W}x{WINDOW_H}")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=WIN_BG)
        root.resizable(False, False)

        self.canvas = None
        if os.name == "nt":
            try:
                from codex_light.layered import LayeredWindowRenderer
                root.update_idletasks()
                self._layered_renderer = LayeredWindowRenderer(
                    root, WINDOW_W, WINDOW_H)
            except Exception:
                self._layered_renderer = None

        if self._layered_renderer is None:
            # Compatibility fallback. Color-key transparency is binary and can
            # look jagged, but keeps the app usable if GDI+ is unavailable.
            try:
                root.attributes("-transparentcolor", WIN_BG)
            except tk.TclError:
                pass
            self.canvas = tk.Canvas(root, width=WINDOW_W, height=WINDOW_H,
                                    bg=WIN_BG, highlightthickness=0)
            self.canvas.pack(fill="both", expand=True)
            input_target = self.canvas
        else:
            input_target = root

        input_target.bind("<Button-1>", self._on_drag_start)
        input_target.bind("<B1-Motion>", self._on_drag_move)
        input_target.bind("<Double-Button-1>", self._on_double_click)
        input_target.bind("<Button-3>", self._on_context_menu)
        root.bind("<Button-3>", self._on_context_menu)

        # remember position between runs; default = top-center of screen.
        # a saved position from a different layout (e.g. the old vertical
        # build) is dropped via the stored w/h tag.
        self._geometry_file = os.path.join(
            os.path.dirname(self.store.state_path), "window.json"
        )
        screen_w = root.winfo_screenwidth()
        x, y = (screen_w - WINDOW_W) // 2, 8
        try:
            with open(self._geometry_file, "r", encoding="utf-8") as fh:
                geo = json.load(fh)
            if (int(geo.get("w", -1)), int(geo.get("h", -1))) == (WINDOW_W, WINDOW_H):
                sx, sy = int(geo["x"]), int(geo["y"])
                if 0 <= sx <= screen_w - 40 and 0 <= sy <= root.winfo_screenheight() - 40:
                    x, y = sx, sy
        except (OSError, ValueError, KeyError, TypeError):
            pass
        root.geometry(f"+{x}+{y}")

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
        menu.add_command(label="退出并不再自动启动", command=self._quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_window(self) -> None:
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.after_idle(self._draw)
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
        # user intentionally turned the light off: drop a flag so the
        # autostart guard does not respawn us (light-on.vbs clears it)
        try:
            with open(light_off_path(), "w", encoding="utf-8") as fh:
                json.dump({"off": True, "at": time.time()}, fh)
        except OSError:
            pass
        self.root.destroy()

    # ------------------------------------------------------------ state loop

    def _poll_state(self) -> None:
        self._heal_dpi()
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
        self._heal_dpi()
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

    # ---------------------------------------------------- DPI self-healing

    def _dpi_mismatch(self) -> bool:
        """True when the physical client size or tk scaling drifted from design."""
        try:
            import ctypes
            import ctypes.wintypes
            user32 = ctypes.windll.user32
            hwnd = self.root.winfo_id()
            rect = ctypes.wintypes.RECT()
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            if (rect.right - rect.left, rect.bottom - rect.top) != (WINDOW_W, WINDOW_H):
                return True
            dpi = user32.GetDpiForWindow(hwnd) or 96
            return abs(float(self.root.tk.call("tk", "scaling")) - dpi / 72.0) > 0.01
        except Exception:
            return False

    def _heal_dpi(self) -> None:
        """Restore physical size + scaling and rebuild the canvas after drift.

        Some launch contexts (login-time scaling broadcast, Tk 8.6 unit/pixel
        confusion on 125% displays) silently resize the overrideredirect
        window to logical*96/DPI and corrupt already-drawn canvas items,
        leaving black regions on screen while PrintWindow still looks fine.
        Forcing the Win32 size and rebuilding every item from scratch always
        converges to a correct surface; on a healthy instance this is a
        cheap no-op (one GetClientRect per poll).
        """
        if not self._dpi_mismatch():
            return
        root = self.root
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = root.winfo_id()
            dpi = user32.GetDpiForWindow(hwnd) or 96
            try:
                with open(os.path.join(os.path.dirname(self.store.state_path),
                                       "dpi-heal.log"), "a", encoding="utf-8") as fh:
                    fh.write("%s heal fired scaling=%s dpi=%d\n"
                             % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                self.root.tk.call("tk", "scaling"), dpi))
            except OSError:
                pass
            root.tk.call("tk", "scaling", dpi / 72.0)
            pos = ""
            try:
                with open(self._geometry_file, "r", encoding="utf-8") as fh:
                    geo = json.load(fh)
                pos = "+%d+%d" % (int(geo["x"]), int(geo["y"]))
            except (OSError, ValueError, KeyError, TypeError):
                pass
            root.geometry("%dx%d%s" % (WINDOW_W, WINDOW_H, pos))
            root.update_idletasks()
            # belt-and-braces: force the physical size even if Tk's unit
            # conversion is still confused
            SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0002, 0x0004, 0x0010
            user32.SetWindowPos(hwnd, 0, 0, 0, WINDOW_W, WINDOW_H,
                                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE)
            root.update_idletasks()
            root.update()
            if self._layered_renderer is not None:
                self._draw()
                return
            self.canvas.delete("all")
            self._static_items = {}
            self._lens_items = {}
            self._build_static_items()
            self._update_lights()
            self._update_texts()
            self.canvas.update_idletasks()
        except Exception:
            pass

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
        if self._layered_renderer is not None:
            active_slot = self._active_slot()
            if (active_slot == LightState.waiting
                    and self.waiting_alert_active and not self.blink_on):
                active_slot = None
            self._layered_renderer.render(
                self.current_state.label,
                active_slot.value if active_slot is not None else None,
                {
                    LightState.waiting.value: RED,
                    LightState.working.value: YELLOW,
                    LightState.done.value: GREEN,
                },
            )
            return
        canvas = self.canvas
        # Qwen 3.8Max fix: reuse canvas items (itemconfig) instead of
        # delete("all")+rebuild. Tk 8.6 on Windows only repaints dirty rects
        # of overrideredirect windows; delete/create leaves stale black
        # regions on screen even though PrintWindow renders the full surface.
        if not self._static_items:
            self._build_static_items()
        self._update_lights()
        self._update_texts()

        # belt-and-braces: force a full-window WM_PAINT after every redraw
        try:
            import ctypes
            hwnd = canvas.winfo_id()
            ctypes.windll.user32.InvalidateRect(hwnd, None, False)
            top = ctypes.windll.user32.GetParent(hwnd)
            if top:
                ctypes.windll.user32.InvalidateRect(top, None, False)
            ctypes.windll.user32.UpdateWindow(hwnd)
        except Exception:
            pass

    def _build_static_items(self) -> None:
        """Create every canvas item once; later redraws only itemconfig()."""
        canvas = self.canvas
        self._static_items = {}
        body_x, body_y = 2, 2
        body_w, body_h = WINDOW_W - 4, WINDOW_H - 6
        r = CORNER_RADIUS

        # soft iOS drop shadow (stacked rounded rects; tight offsets so the
        # shadow stays inside the slim horizontal window)
        shadows = []
        for dy, alpha in ((3, 0.10), (2, 0.14), (1, 0.18)):
            shadows.append(rounded_rect(canvas, body_x + 1, body_y + dy,
                                        body_x + body_w + 1, body_y + body_h + dy,
                                        r, fill=blend(SHADOW_COLOR, alpha, WIN_BG),
                                        outline=""))
        self._static_items["shadow"] = shadows

        # glass gradient bands
        bands = []
        steps = 8
        for i in range(steps):
            t0, t1 = i / steps, (i + 1) / steps
            y0 = body_y + body_h * t0
            y1 = body_y + body_h * t1 + 0.5
            col = lerp_color(GLASS_TOP, GLASS_BOTTOM, (t0 + t1) / 2)
            bands.append(rounded_rect(canvas, body_x, y0, body_x + body_w, y1,
                                      r, fill=col, outline=""))
        self._static_items["bands"] = bands

        # rim glow stack + optional top sheen  (fill="" is critical:
        # create_polygon defaults to a black fill, which would paint the
        # whole card black)
        self._static_items["rim"] = [
            rounded_rect(canvas, body_x, body_y, body_x + body_w, body_y + body_h,
                         r, outline=CARD_RIM, width=w, fill="")
            for w in RIM_WIDTHS
        ]
        if SHEEN_ALPHA > 0:
            self._static_items["sheen"] = rounded_rect(
                canvas, body_x + 1.5, body_y + 1.5, body_x + body_w - 1.5,
                body_y + 10, r - 6, fill=blend(CARD_SHEEN, SHEEN_ALPHA, GLASS_TOP),
                outline="")

        # lenses: plain solid disc + small glass hilite (no ripple rings)
        self._lens_items = {}
        for slot, (cx, cy) in LENS_CENTERS.items():
            self._lens_items[slot] = self._create_lens_items(cx, cy)

        # State dot + divider + status text. Quota remains available to the
        # CLI/state store but is intentionally absent from the floating UI.
        dot_x, dot_y = STATUS_DOT_CENTER
        self._static_items["status_dot_glow"] = canvas.create_oval(
            dot_x - 7, dot_y - 7, dot_x + 7, dot_y + 7,
            fill="", outline="", width=2)
        self._static_items["status_dot"] = canvas.create_oval(
            dot_x - 4, dot_y - 4, dot_x + 4, dot_y + 4,
            fill=LENS_OFF_FILL, outline="")
        self._static_items["divider"] = canvas.create_line(
            DIVIDER_X, 18, DIVIDER_X, 40, fill="#282a2f", width=1)
        sx, sy, sw, sh = STATUS_RECT
        self._static_items["status_text"] = canvas.create_text(
            sx, sy + sh / 2, text="", anchor="w", fill=STATUS_TEXT,
            font=(FONT_FAMILY, 10, "bold"))

    def _create_lens_items(self, cx: float, cy: float) -> list:
        """Three items per lens: glow outline, bulb disc, and glass hilite.
        Colours are updated in place each redraw."""
        canvas = self.canvas
        r = LENS_BULB_RADIUS
        glow = canvas.create_oval(cx - r - 3, cy - r - 3,
                                  cx + r + 3, cy + r + 3,
                                  fill="", outline="", width=2)
        bulb = canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                  fill=LENS_OFF_FILL, outline=LENS_OFF_RIM, width=1)
        hilite = canvas.create_oval(cx - r * 0.42, cy - r * 0.55,
                                    cx + r * 0.10, cy - r * 0.18,
                                    fill=blend("#ffffff", 0.16, LENS_OFF_FILL),
                                    outline="")
        return [glow, bulb, hilite]

    def _update_lights(self) -> None:
        active_slot = self._active_slot()
        for slot, (cx, cy) in LENS_CENTERS.items():
            color = self._slot_color(slot)
            active = slot == active_slot
            if active and slot == LightState.waiting and self.waiting_alert_active:
                visible = self.blink_on
            else:
                visible = active
            self._update_lens(slot, color, visible)
        rim_slot = active_slot
        if (rim_slot == LightState.waiting and self.waiting_alert_active
                and not self.blink_on):
            rim_slot = None  # blink-off phase: rim dims with the lens
        self._update_status_dot(rim_slot)
        self._update_rim(rim_slot)

    def _update_status_dot(self, slot) -> None:
        dot = self._static_items.get("status_dot")
        glow = self._static_items.get("status_dot_glow")
        if dot is None or glow is None:
            return
        if slot is None:
            self.canvas.itemconfig(dot, fill=LENS_OFF_FILL)
            self.canvas.itemconfig(glow, outline="")
            return
        color = self._slot_color(slot)
        self.canvas.itemconfig(dot, fill=color)
        self.canvas.itemconfig(
            glow, outline=blend(color, 0.42, GLASS_TOP))

    def _update_rim(self, slot) -> None:
        """Neon rim: three stacked strokes glow with the active state color."""
        items = self._static_items.get("rim")
        if not items:
            return
        if NEON and slot is not None:
            c = self._slot_color(slot)
            cols = [blend(c, a, GLASS_TOP) for a in (0.15, 0.30, 0.85)]
        else:
            cols = [CARD_RIM] * len(RIM_WIDTHS)
        for item, col in zip(items, cols):
            self.canvas.itemconfig(item, outline=col)

    def _update_lens(self, slot, color: str, visible: bool) -> None:
        canvas = self.canvas
        glow, bulb, hilite = self._lens_items[slot]
        if visible:
            canvas.itemconfig(
                glow, outline=blend(color, 0.52, GLASS_TOP))
            canvas.itemconfig(
                bulb, fill=color,
                outline=blend("#ffffff", 0.34, color))
            canvas.itemconfig(hilite, fill=blend("#ffffff", 0.65, color))
        else:
            canvas.itemconfig(glow, outline="")
            canvas.itemconfig(
                bulb, fill=LENS_OFF_FILL, outline=LENS_OFF_RIM)
            canvas.itemconfig(
                hilite, fill=blend("#ffffff", 0.16, LENS_OFF_FILL))

    def _update_texts(self) -> None:
        canvas = self.canvas
        canvas.itemconfig(self._static_items["status_text"],
                          text=self.current_state.label)

    def _draw_shadow(self, x: float, y: float, w: float, h: float,
                     r: float) -> None:
        """Legacy full-redraw shadow (kept for API compat; unused)."""
        pass

    def _draw_glass_body(self, x: float, y: float, w: float, h: float,
                         r: float) -> None:
        """Legacy full-redraw gradient (kept for API compat; unused)."""
        pass

    def _draw_lens(self, cx: float, cy: float, color: str, visible: bool) -> None:
        """Legacy full-redraw lens (kept for API compat; unused)."""
        pass

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
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                           "w": WINDOW_W, "h": WINDOW_H}, fh)
        except OSError:
            pass
        if self._layered_renderer is not None:
            self._layered_renderer.close()
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


def _pin_tk_scaling(root: tk.Tk) -> None:
    """Pin `tk scaling` to the monitor's real DPI so 1 Tk unit == 1 physical px.

    Tk 8.6's auto-detected scaling can disagree with the process DPI
    awareness (96 vs 120 on a 125% display); the window then ends up sized
    logical*96/120 and the canvas pixmap no longer lines up with the client
    area (black regions on screen). Setting it explicitly right after Tk()
    keeps units and pixels 1:1 in every launch context.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        dpi = user32.GetDpiForWindow(root.winfo_id())
        if not dpi:
            hdc = user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
        if dpi:
            root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass


def main() -> int:
    _enable_dpi_awareness()
    if os.path.exists(light_off_path()):
        return 0  # user turned the light off; light-on.vbs re-enables it
    # single instance guard
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        guard.listen(1)
    except OSError:
        return 0  # another instance is already running

    root = tk.Tk()
    _pin_tk_scaling(root)
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
