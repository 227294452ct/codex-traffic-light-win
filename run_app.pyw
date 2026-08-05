"""Codex Traffic Light MXP (Windows port) — GUI launcher.

Run with pythonw.exe (no console window):
  pythonw run_app.pyw
"""
import sys

sys.path.insert(0, __file__ and __file__.rsplit("\\", 1)[0] or ".")

from codex_light.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
