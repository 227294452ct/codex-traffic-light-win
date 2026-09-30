"""Native Windows per-pixel-alpha renderer for the floating traffic light.

Tk 8.6's ``-transparentcolor`` is a binary color key: a pixel is either fully
transparent or fully opaque.  That makes small rounded windows visibly jagged.
This module keeps Tk as the input/event host, but supplies the top-level window
surface through ``UpdateLayeredWindow`` using a premultiplied 32-bpp GDI+
bitmap.  GDI+ provides antialiased curves and grayscale-antialiased text while
remaining dependency-free on supported Windows versions.
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


if os.name == "nt":
    _ULONG_PTR = ctypes.c_size_t
else:  # lets documentation/static tooling import the module off Windows
    _ULONG_PTR = ctypes.c_size_t


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", wintypes.UINT),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Size(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


class _BlendFunction(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


def _argb(hex_color: str, alpha: int = 255) -> int:
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    return ((alpha & 0xFF) << 24) | (red << 16) | (green << 8) | blue


class LayeredWindowRenderer:
    """Render the 304x60 Dynamic Island directly into a layered HWND."""

    _PIXEL_FORMAT_32BPP_PARGB = 0x000E200B
    _WS_EX_LAYERED = 0x00080000
    _GWL_EXSTYLE = -20
    _ULW_ALPHA = 0x00000002
    _AC_SRC_OVER = 0
    _AC_SRC_ALPHA = 1
    _DIB_RGB_COLORS = 0

    def __init__(self, root, width: int, height: int):
        if os.name != "nt":
            raise RuntimeError("LayeredWindowRenderer is Windows-only")
        self.root = root
        self.width = int(width)
        self.height = int(height)
        self._closed = False
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._gdiplus = ctypes.WinDLL("gdiplus", use_last_error=True)
        self._configure_functions()
        self._token = _ULONG_PTR()
        startup = _GdiplusStartupInput(1, None, False, False)
        self._check(
            self._gdiplus.GdiplusStartup(
                ctypes.byref(self._token), ctypes.byref(startup), None),
            "GdiplusStartup",
        )
        self.root.update_idletasks()
        # Tk creates a child client HWND inside a native top-level wrapper.
        # UpdateLayeredWindow must target the wrapper (the handle returned by
        # FindWindow / Process.MainWindowHandle), not Tk's child winfo_id().
        self._client_hwnd = wintypes.HWND(self.root.winfo_id())
        wrapper_hwnd = self._user32.GetParent(self._client_hwnd)
        self._hwnd = wintypes.HWND(wrapper_hwnd or self._client_hwnd.value)
        self._reset_layered_style()

    # ------------------------------------------------------------- public

    def render(self, label: str, active_name: str | None,
               colors: dict[str, str]) -> None:
        """Draw one frame and upload it with premultiplied per-pixel alpha."""
        if self._closed:
            return
        screen_dc = self._user32.GetDC(None)
        if not screen_dc:
            raise ctypes.WinError(ctypes.get_last_error())
        memory_dc = self._gdi32.CreateCompatibleDC(screen_dc)
        if not memory_dc:
            self._user32.ReleaseDC(None, screen_dc)
            raise ctypes.WinError(ctypes.get_last_error())

        bitmap = None
        old_bitmap = None
        image = ctypes.c_void_p()
        graphics = ctypes.c_void_p()
        try:
            info = _BitmapInfo()
            info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.bmiHeader.biWidth = self.width
            info.bmiHeader.biHeight = -self.height  # top-down BGRA
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            bits = ctypes.c_void_p()
            bitmap = self._gdi32.CreateDIBSection(
                memory_dc, ctypes.byref(info), self._DIB_RGB_COLORS,
                ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise ctypes.WinError(ctypes.get_last_error())
            old_bitmap = self._gdi32.SelectObject(memory_dc, bitmap)
            ctypes.memset(bits, 0, self.width * self.height * 4)

            self._check(
                self._gdiplus.GdipCreateBitmapFromScan0(
                    self.width, self.height, self.width * 4,
                    self._PIXEL_FORMAT_32BPP_PARGB,
                    ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte)),
                    ctypes.byref(image)),
                "GdipCreateBitmapFromScan0",
            )
            self._check(
                self._gdiplus.GdipGetImageGraphicsContext(
                    image, ctypes.byref(graphics)),
                "GdipGetImageGraphicsContext",
            )
            self._configure_graphics(graphics)
            self._draw_frame(graphics, label, active_name, colors)
            self._gdiplus.GdipFlush(graphics, 0)
            if os.environ.get("CODEX_LIGHT_LAYERED_DEBUG"):
                raw = ctypes.string_at(bits, self.width * self.height * 4)
                alpha_values = raw[3::4]
                print({
                    "alpha_min": min(alpha_values),
                    "alpha_max": max(alpha_values),
                    "alpha_nonzero": sum(value != 0 for value in alpha_values),
                    "pixels": self.width * self.height,
                }, flush=True)
            self._gdiplus.GdipDeleteGraphics(graphics)
            graphics = ctypes.c_void_p()
            self._gdiplus.GdipDisposeImage(image)
            image = ctypes.c_void_p()

            rect = wintypes.RECT()
            if not self._user32.GetWindowRect(self._hwnd, ctypes.byref(rect)):
                raise ctypes.WinError(ctypes.get_last_error())
            destination = _Point(rect.left, rect.top)
            source = _Point(0, 0)
            size = _Size(self.width, self.height)
            blend = _BlendFunction(
                self._AC_SRC_OVER, 0, 255, self._AC_SRC_ALPHA)
            if not self._user32.UpdateLayeredWindow(
                    self._hwnd, screen_dc, ctypes.byref(destination),
                    ctypes.byref(size), memory_dc, ctypes.byref(source), 0,
                    ctypes.byref(blend), self._ULW_ALPHA):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if graphics.value:
                self._gdiplus.GdipDeleteGraphics(graphics)
            if image.value:
                self._gdiplus.GdipDisposeImage(image)
            if old_bitmap:
                self._gdi32.SelectObject(memory_dc, old_bitmap)
            if bitmap:
                self._gdi32.DeleteObject(bitmap)
            self._gdi32.DeleteDC(memory_dc)
            self._user32.ReleaseDC(None, screen_dc)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._token.value:
            self._gdiplus.GdiplusShutdown(self._token)
            self._token = _ULONG_PTR()

    # ---------------------------------------------------------- frame draw

    def _draw_frame(self, graphics, label: str, active_name: str | None,
                    colors: dict[str, str]) -> None:
        self._check(self._gdiplus.GdipGraphicsClear(graphics, 0),
                    "GdipGraphicsClear")

        # Genuine alpha shadows; unlike color-key shadows these blend into any
        # desktop background without leaving a dark rectangular fringe.
        self._fill_round_rect(graphics, 3, 4, 298, 54, 27,
                              _argb("#000000", 30))
        self._fill_round_rect(graphics, 2, 2, 300, 56, 28,
                              _argb("#000000", 42))

        # One continuous gradient path replaces the old eight individually
        # rounded bands, so the exterior contour is mathematically continuous.
        body_path = self._round_rect_path(2, 1, 300, 56, 28)
        body_brush = ctypes.c_void_p()
        body_rect = _RectF(2.0, 1.0, 300.0, 56.0)
        try:
            self._check(
                self._gdiplus.GdipCreateLineBrushFromRect(
                    ctypes.byref(body_rect), _argb("#17181b"),
                    _argb("#050506"), 1, 0, ctypes.byref(body_brush)),
                "GdipCreateLineBrushFromRect",
            )
            self._check(
                self._gdiplus.GdipFillPath(graphics, body_brush, body_path),
                "GdipFillPath(body)",
            )
        finally:
            if body_brush.value:
                self._gdiplus.GdipDeleteBrush(body_brush)
            self._gdiplus.GdipDeletePath(body_path)
        self._draw_round_rect(graphics, 2.5, 1.5, 299, 55, 27.5,
                              _argb("#ffffff", 34), 1.0)

        # Status marker and label.
        active_color = colors.get(active_name or "", "#242529")
        if active_name:
            self._fill_ellipse(graphics, 14, 21, 16, 16,
                               _argb(active_color, 35))
            self._fill_ellipse(graphics, 18, 25, 8, 8,
                               _argb(active_color))
        else:
            self._fill_ellipse(graphics, 18, 25, 8, 8,
                               _argb("#242529"))
        self._draw_text(graphics, label, 36, 15, 150, 28,
                        "Microsoft YaHei UI", 13.0,
                        _argb("#f8f9fc"), bold=True)
        self._draw_line(graphics, 198, 18, 198, 40,
                        _argb("#70737a", 60), 1.0)

        # Real-world left-to-right order: red, yellow, green.
        for name, cx in (("waiting", 220), ("working", 250), ("done", 280)):
            self._draw_lens(graphics, cx, 29, colors[name],
                            visible=name == active_name)

    def _draw_lens(self, graphics, cx: float, cy: float, color: str,
                   visible: bool) -> None:
        if visible:
            for radius, alpha in ((17, 14), (15, 24), (13, 42)):
                self._fill_ellipse(
                    graphics, cx - radius, cy - radius,
                    radius * 2, radius * 2, _argb(color, alpha))
            self._fill_ellipse(graphics, cx - 11, cy - 11, 22, 22,
                               _argb(color))
            self._draw_ellipse(graphics, cx - 10.5, cy - 10.5, 21, 21,
                               _argb("#ffffff", 94), 1.0)
            self._fill_ellipse(graphics, cx - 4.5, cy - 6.0, 5.5, 3.8,
                               _argb("#ffffff", 178))
        else:
            self._fill_ellipse(graphics, cx - 11, cy - 11, 22, 22,
                               _argb("#242529"))
            self._draw_ellipse(graphics, cx - 10.5, cy - 10.5, 21, 21,
                               _argb("#777b83", 72), 1.0)
            self._fill_ellipse(graphics, cx - 4.5, cy - 6.0, 5.5, 3.8,
                               _argb("#ffffff", 40))

    # -------------------------------------------------------- GDI+ helpers

    def _configure_graphics(self, graphics) -> None:
        self._check(self._gdiplus.GdipSetSmoothingMode(graphics, 4),
                    "GdipSetSmoothingMode")
        self._check(self._gdiplus.GdipSetPixelOffsetMode(graphics, 4),
                    "GdipSetPixelOffsetMode")
        self._check(self._gdiplus.GdipSetCompositingQuality(graphics, 2),
                    "GdipSetCompositingQuality")
        self._check(self._gdiplus.GdipSetTextRenderingHint(graphics, 4),
                    "GdipSetTextRenderingHint")

    def _round_rect_path(self, x: float, y: float, width: float,
                         height: float, radius: float):
        path = ctypes.c_void_p()
        self._check(self._gdiplus.GdipCreatePath(0, ctypes.byref(path)),
                    "GdipCreatePath")
        diameter = radius * 2
        arcs = (
            (x, y, 180.0),
            (x + width - diameter, y, 270.0),
            (x + width - diameter, y + height - diameter, 0.0),
            (x, y + height - diameter, 90.0),
        )
        try:
            for arc_x, arc_y, start in arcs:
                self._check(
                    self._gdiplus.GdipAddPathArc(
                        path, arc_x, arc_y, diameter, diameter, start, 90.0),
                    "GdipAddPathArc",
                )
            self._check(self._gdiplus.GdipClosePathFigure(path),
                        "GdipClosePathFigure")
            return path
        except BaseException:
            self._gdiplus.GdipDeletePath(path)
            raise

    def _fill_round_rect(self, graphics, x: float, y: float, width: float,
                         height: float, radius: float, color: int) -> None:
        path = self._round_rect_path(x, y, width, height, radius)
        brush = self._solid_brush(color)
        try:
            self._check(self._gdiplus.GdipFillPath(graphics, brush, path),
                        "GdipFillPath")
        finally:
            self._gdiplus.GdipDeleteBrush(brush)
            self._gdiplus.GdipDeletePath(path)

    def _draw_round_rect(self, graphics, x: float, y: float, width: float,
                         height: float, radius: float, color: int,
                         line_width: float) -> None:
        path = self._round_rect_path(x, y, width, height, radius)
        pen = self._pen(color, line_width)
        try:
            self._check(self._gdiplus.GdipDrawPath(graphics, pen, path),
                        "GdipDrawPath")
        finally:
            self._gdiplus.GdipDeletePen(pen)
            self._gdiplus.GdipDeletePath(path)

    def _fill_ellipse(self, graphics, x: float, y: float, width: float,
                      height: float, color: int) -> None:
        brush = self._solid_brush(color)
        try:
            self._check(self._gdiplus.GdipFillEllipse(
                graphics, brush, x, y, width, height), "GdipFillEllipse")
        finally:
            self._gdiplus.GdipDeleteBrush(brush)

    def _draw_ellipse(self, graphics, x: float, y: float, width: float,
                      height: float, color: int, line_width: float) -> None:
        pen = self._pen(color, line_width)
        try:
            self._check(self._gdiplus.GdipDrawEllipse(
                graphics, pen, x, y, width, height), "GdipDrawEllipse")
        finally:
            self._gdiplus.GdipDeletePen(pen)

    def _draw_line(self, graphics, x1: float, y1: float, x2: float,
                   y2: float, color: int, line_width: float) -> None:
        pen = self._pen(color, line_width)
        try:
            self._check(self._gdiplus.GdipDrawLine(
                graphics, pen, x1, y1, x2, y2), "GdipDrawLine")
        finally:
            self._gdiplus.GdipDeletePen(pen)

    def _draw_text(self, graphics, text: str, x: float, y: float,
                   width: float, height: float, family_name: str,
                   size: float, color: int, bold: bool) -> None:
        family = ctypes.c_void_p()
        font = ctypes.c_void_p()
        string_format = ctypes.c_void_p()
        brush = ctypes.c_void_p()
        try:
            self._check(
                self._gdiplus.GdipCreateFontFamilyFromName(
                    family_name, None, ctypes.byref(family)),
                "GdipCreateFontFamilyFromName",
            )
            self._check(
                self._gdiplus.GdipCreateFont(
                    family, size, 1 if bold else 0, 2, ctypes.byref(font)),
                "GdipCreateFont",
            )
            self._check(
                self._gdiplus.GdipCreateStringFormat(
                    0, 0, ctypes.byref(string_format)),
                "GdipCreateStringFormat",
            )
            brush = self._solid_brush(color)
            layout = _RectF(x, y, width, height)
            self._check(
                self._gdiplus.GdipDrawString(
                    graphics, text, len(text), font, ctypes.byref(layout),
                    string_format, brush),
                "GdipDrawString",
            )
        finally:
            if brush.value:
                self._gdiplus.GdipDeleteBrush(brush)
            if string_format.value:
                self._gdiplus.GdipDeleteStringFormat(string_format)
            if font.value:
                self._gdiplus.GdipDeleteFont(font)
            if family.value:
                self._gdiplus.GdipDeleteFontFamily(family)

    def _solid_brush(self, color: int):
        brush = ctypes.c_void_p()
        self._check(self._gdiplus.GdipCreateSolidFill(
            color, ctypes.byref(brush)), "GdipCreateSolidFill")
        return brush

    def _pen(self, color: int, width: float):
        pen = ctypes.c_void_p()
        self._check(self._gdiplus.GdipCreatePen1(
            color, width, 2, ctypes.byref(pen)), "GdipCreatePen1")
        return pen

    # ----------------------------------------------------------- Win32 API

    def _reset_layered_style(self) -> None:
        exstyle = self._user32.GetWindowLongW(self._hwnd, self._GWL_EXSTYLE)
        # Clearing then restoring WS_EX_LAYERED is important if an earlier
        # code path called SetLayeredWindowAttributes: Microsoft documents
        # that UpdateLayeredWindow otherwise fails until the bit is reset.
        self._user32.SetWindowLongW(
            self._hwnd, self._GWL_EXSTYLE, exstyle & ~self._WS_EX_LAYERED)
        self._user32.SetWindowLongW(
            self._hwnd, self._GWL_EXSTYLE, exstyle | self._WS_EX_LAYERED)

    def _configure_functions(self) -> None:
        gp = ctypes.c_void_p
        real = ctypes.c_float
        argb = ctypes.c_uint32

        self._user32.GetDC.restype = wintypes.HDC
        self._user32.GetParent.argtypes = [wintypes.HWND]
        self._user32.GetParent.restype = wintypes.HWND
        self._user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self._user32.GetWindowRect.argtypes = [wintypes.HWND,
                                               ctypes.POINTER(wintypes.RECT)]
        self._user32.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(_Point),
            ctypes.POINTER(_Size), wintypes.HDC, ctypes.POINTER(_Point),
            wintypes.COLORREF, ctypes.POINTER(_BlendFunction), wintypes.DWORD,
        ]
        self._user32.UpdateLayeredWindow.restype = wintypes.BOOL

        self._gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self._gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self._gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        self._gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self._gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self._gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self._gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self._gdi32.DeleteDC.argtypes = [wintypes.HDC]

        self._gdiplus.GdiplusStartup.argtypes = [
            ctypes.POINTER(_ULONG_PTR), ctypes.POINTER(_GdiplusStartupInput),
            ctypes.c_void_p]
        self._gdiplus.GdiplusShutdown.argtypes = [_ULONG_PTR]
        self._gdiplus.GdipCreateBitmapFromScan0.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte), ctypes.POINTER(gp)]
        self._gdiplus.GdipGetImageGraphicsContext.argtypes = [gp,
                                                               ctypes.POINTER(gp)]
        self._gdiplus.GdipDisposeImage.argtypes = [gp]
        self._gdiplus.GdipDeleteGraphics.argtypes = [gp]
        self._gdiplus.GdipFlush.argtypes = [gp, ctypes.c_int]
        self._gdiplus.GdipGraphicsClear.argtypes = [gp, argb]
        self._gdiplus.GdipSetSmoothingMode.argtypes = [gp, ctypes.c_int]
        self._gdiplus.GdipSetPixelOffsetMode.argtypes = [gp, ctypes.c_int]
        self._gdiplus.GdipSetCompositingQuality.argtypes = [gp, ctypes.c_int]
        self._gdiplus.GdipSetTextRenderingHint.argtypes = [gp, ctypes.c_int]

        self._gdiplus.GdipCreatePath.argtypes = [ctypes.c_int, ctypes.POINTER(gp)]
        self._gdiplus.GdipAddPathArc.argtypes = [
            gp, real, real, real, real, real, real]
        self._gdiplus.GdipClosePathFigure.argtypes = [gp]
        self._gdiplus.GdipDeletePath.argtypes = [gp]
        self._gdiplus.GdipFillPath.argtypes = [gp, gp, gp]
        self._gdiplus.GdipDrawPath.argtypes = [gp, gp, gp]

        self._gdiplus.GdipCreateSolidFill.argtypes = [argb, ctypes.POINTER(gp)]
        self._gdiplus.GdipCreateLineBrushFromRect.argtypes = [
            ctypes.POINTER(_RectF), argb, argb, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(gp)]
        self._gdiplus.GdipDeleteBrush.argtypes = [gp]
        self._gdiplus.GdipCreatePen1.argtypes = [
            argb, real, ctypes.c_int, ctypes.POINTER(gp)]
        self._gdiplus.GdipDeletePen.argtypes = [gp]
        self._gdiplus.GdipFillEllipse.argtypes = [gp, gp, real, real, real, real]
        self._gdiplus.GdipDrawEllipse.argtypes = [gp, gp, real, real, real, real]
        self._gdiplus.GdipDrawLine.argtypes = [gp, gp, real, real, real, real]

        self._gdiplus.GdipCreateFontFamilyFromName.argtypes = [
            wintypes.LPCWSTR, gp, ctypes.POINTER(gp)]
        self._gdiplus.GdipDeleteFontFamily.argtypes = [gp]
        self._gdiplus.GdipCreateFont.argtypes = [
            gp, real, ctypes.c_int, ctypes.c_int, ctypes.POINTER(gp)]
        self._gdiplus.GdipDeleteFont.argtypes = [gp]
        self._gdiplus.GdipCreateStringFormat.argtypes = [
            ctypes.c_int, wintypes.LANGID, ctypes.POINTER(gp)]
        self._gdiplus.GdipDeleteStringFormat.argtypes = [gp]
        self._gdiplus.GdipDrawString.argtypes = [
            gp, wintypes.LPCWSTR, ctypes.c_int, gp, ctypes.POINTER(_RectF), gp, gp]

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status != 0:
            raise OSError(f"{operation} failed with GDI+ status {status}")
