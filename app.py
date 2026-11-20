"""LCSC Part Importer & Integrator for Altium Designer (.IntLib / .LibPkg).

Styled after Altium Designer's "Altium Dark Gray" UI theme (Properties,
Manufacturer Part Search, and Footprint 3D/2D Preview panels).
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageTk

from altium_builder import (
    BuildResult,
    Model3DMetrics,
    build_intlib,
    inspect_step_metrics,
    suggested_intlib_filename,
)
from integrator import (
    IntegrateResult,
    IntLibFileInfo,
    inspect_intlib,
    integrate_intlibs,
    sanitize_library_name,
)
from lcsc_api import (
    ComponentBundle,
    SearchHit,
    extract_lcsc_id,
    fetch_component,
    search_lcsc_keyword,
)
from viewer3d import (
    PreviewAsset,
    load_preview_from_intlib,
    render_2d_footprint_preview,
    render_3d_preview,
)

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
DEFAULT_OUTPUT_DIR = APP_DIR / "output"
ICON_ICO_PATH = APP_DIR / "assets" / "app_icon_v3.ico"
ICON_PNG_PATH = APP_DIR / "assets" / "app_icon_v3.png"

# Detach Windows taskbar identity from pythonw.exe so our custom icon appears on the taskbar
if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LCSC.Altium.PartImporter.3")
    except Exception:
        pass

# Altium Designer 26 "Altium Dark Gray" UI Theme Palette (pixel-matched neutral charcoal)
BG_TITLEBAR = "#474747"
BG_MENUBAR = "#393939"
BG_MENUBAR_ACTIVE = "#4E4E4E"
BG_MENUBAR_HOVER = "#4A4A4A"
BG_WINDOW = "#333333"
BG_PANEL = "#3B3B3B"
BG_HEADER = "#4A4A4A"
BG_STATUSBAR = "#404040"
BG_INPUT = "#2E2E2E"
BG_ROW_ALT = "#353535"
BG_BTN = "#4A4A4A"
BG_BTN_HOVER = "#5A5A5A"
BG_BTN_DISABLED = "#454545"

BORDER_PANEL = "#525252"
BORDER_INPUT = "#585858"

TEXT_PRIMARY = "#EAEAEA"
TEXT_SECONDARY = "#B4B4B4"
TEXT_MUTED = "#888888"

ALTIUM_BLUE = "#007ACC"
ALTIUM_BLUE_HOVER = "#1C97EA"
ALTIUM_GOLD = "#D4A359"

STATUS_OK = "#4EC978"
STATUS_WARN = "#E5A93C"
STATUS_ERR = "#E05A5A"

FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "output_dir": str(DEFAULT_OUTPUT_DIR),
        "integrate_output_dir": str(DEFAULT_OUTPUT_DIR),
        "auto_clipboard": True,
        "save_sources": False,
        "altium_native_style": True,
    }


def _save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


class AltiumPreviewPane(ctk.CTkFrame):
    """Altium-style collapsible 3D STEP / 2D Footprint preview pane."""

    def __init__(self, master: ctk.CTkBaseClass, on_hide) -> None:
        super().__init__(
            master,
            fg_color=BG_PANEL,
            corner_radius=0,
            border_width=0,
            width=285,
        )
        self.grid_propagate(False)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._on_hide = on_hide
        self._asset: PreviewAsset | None = None
        self._mode = "3D"  # "3D" or "2D"
        self._yaw = -38.0
        self._pitch = 32.0
        self._zoom = 1.0
        self._drag_last: tuple[int, int] | None = None
        self._photo_ref: ImageTk.PhotoImage | None = None
        self._hq_after_id: str | None = None
        self._loading = False

        # Header strip (exact Altium Footprint panel style: "Footprint | <name> | Hide")
        hdr = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=24)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        hdr.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            hdr,
            text="Footprint",
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            text_color=TEXT_PRIMARY,
            fg_color="#4C566A",
            corner_radius=0,
            padx=8,
        ).grid(row=0, column=0, sticky="ns")

        self.hdr_name = ctk.CTkLabel(
            hdr,
            text="—",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=TEXT_SECONDARY,
            anchor="w",
        )
        self.hdr_name.grid(row=0, column=1, padx=6, sticky="ew")

        hide_btn = ctk.CTkButton(
            hdr,
            text="Hide",
            width=36,
            height=20,
            corner_radius=1,
            fg_color="transparent",
            hover_color=BG_BTN_HOVER,
            text_color="#6CB4EE",
            font=ctk.CTkFont(family=FONT_UI, size=10, underline=True),
            command=self._on_hide,
        )
        hide_btn.grid(row=0, column=2, padx=(0, 4))

        # Canvas viewport
        self.canvas_host = ctk.CTkFrame(self, fg_color="#F7F7F7", corner_radius=0, border_width=1, border_color=BORDER_PANEL)
        self.canvas_host.grid(row=1, column=0, padx=6, pady=(6, 4), sticky="nsew")
        self.canvas_host.grid_columnconfigure(0, weight=1)
        self.canvas_host.grid_rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(
            self.canvas_host,
            bg="#F7F7F7",
            highlightthickness=0,
            bd=0,
            cursor="fleur",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")

        # Floating bottom-left 2D / 3D toggle button (exact Altium style!)
        self.mode_btn = ctk.CTkButton(
            self.canvas_host,
            text="2D",
            width=32,
            height=26,
            corner_radius=2,
            border_width=1,
            border_color="#8C99AD",
            fg_color="#FFFFFF",
            hover_color="#E8EEF7",
            text_color="#232325",
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            command=self._toggle_mode,
        )
        self.mode_btn.place(relx=0.0, rely=1.0, x=8, y=-8, anchor="sw")

        # Footer info
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, padx=6, pady=(0, 6), sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        self.lbl_fp = ctk.CTkLabel(
            footer,
            text="Select an export to preview",
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        )
        self.lbl_fp.grid(row=0, column=0, sticky="ew")

        self.lbl_dims = ctk.CTkLabel(
            footer,
            text="Drag to rotate  •  Scroll to zoom  •  Dbl-click reset",
            font=ctk.CTkFont(family=FONT_UI, size=9),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.lbl_dims.grid(row=1, column=0, sticky="ew")

        # Mouse bindings
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_reset_view)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Configure>", lambda _e: self._render(hq=True))

    def show_loading(self, label: str) -> None:
        self._loading = True
        self._asset = None
        self.hdr_name.configure(text=label[:22])
        self.lbl_fp.configure(text=label)
        self.lbl_dims.configure(text="Loading 3D STEP mesh...")
        self.canvas.configure(bg="#F7F7F7")
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 200)
        h = max(self.canvas.winfo_height(), 140)
        self.canvas.create_text(
            w // 2,
            h // 2,
            text="Loading 3D model...",
            fill="#66666C",
            font=(FONT_UI, 10),
        )

    def show_error(self, msg: str) -> None:
        self._loading = False
        self.canvas.delete("all")
        w = max(self.canvas.winfo_width(), 200)
        h = max(self.canvas.winfo_height(), 140)
        self.canvas.create_text(
            w // 2,
            h // 2,
            text=msg,
            fill=STATUS_ERR,
            font=(FONT_UI, 9),
            width=w - 24,
        )

    def set_asset(self, asset: PreviewAsset) -> None:
        self._loading = False
        self._asset = asset
        self._yaw = -38.0
        self._pitch = 32.0
        self._zoom = 1.0
        self._mode = "3D" if asset.mesh_3d is not None else "2D"
        self._sync_mode_button()

        short_fp = asset.footprint_name
        if len(short_fp) > 22:
            short_fp = short_fp[:20] + "..."
        self.hdr_name.configure(text=short_fp)
        self.lbl_fp.configure(text=asset.footprint_name)

        if asset.mesh_3d is not None:
            sx, sy, sz = asset.mesh_3d.size_mm
            self.lbl_dims.configure(
                text=f"{sx:.2f} × {sy:.2f} × {sz:.2f} mm  •  Drag to rotate"
            )
        else:
            self.lbl_dims.configure(text="2D Footprint only (no embedded STEP)")

        self._render(hq=True)

    def _sync_mode_button(self) -> None:
        if self._mode == "3D":
            # Button shows the mode you can switch TO (just like Altium's "2D" button while in 3D view!)
            self.mode_btn.configure(
                text="2D",
                fg_color="#FFFFFF",
                hover_color="#E8EEF7",
                text_color="#232325",
                border_color="#8C99AD",
            )
        else:
            self.mode_btn.configure(
                text="3D",
                fg_color=BG_BTN,
                hover_color=ALTIUM_BLUE,
                text_color=TEXT_PRIMARY,
                border_color=BORDER_INPUT,
            )

    def _toggle_mode(self) -> None:
        if self._asset is None:
            return
        if self._mode == "3D":
            self._mode = "2D"
        else:
            if self._asset.mesh_3d is not None:
                self._mode = "3D"
            else:
                return
        self._sync_mode_button()
        self._render(hq=True)

    def _on_press(self, event) -> None:
        self._drag_last = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if self._asset is None or self._mode != "3D" or self._drag_last is None:
            return
        dx = event.x - self._drag_last[0]
        dy = event.y - self._drag_last[1]
        self._drag_last = (event.x, event.y)
        self._yaw = (self._yaw + dx * 0.75) % 360.0
        self._pitch = max(-85.0, min(85.0, self._pitch + dy * 0.65))
        self._render(hq=False)

    def _on_release(self, _event) -> None:
        self._drag_last = None
        if self._asset is not None and self._mode == "3D":
            if self._hq_after_id:
                self.after_cancel(self._hq_after_id)
            self._hq_after_id = self.after(90, lambda: self._render(hq=True))

    def _on_reset_view(self, _event) -> None:
        self._yaw = -38.0
        self._pitch = 32.0
        self._zoom = 1.0
        self._render(hq=True)

    def _on_wheel(self, event) -> None:
        if self._asset is None:
            return
        factor = 1.12 if event.delta > 0 else 0.89
        self._zoom = max(0.35, min(3.5, self._zoom * factor))
        self._render(hq=True)

    def _render(self, *, hq: bool = True) -> None:
        if self._loading or self._asset is None:
            return
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w < 40 or h < 40:
            return

        if self._mode == "3D" and self._asset.mesh_3d is not None:
            self.canvas.configure(bg="#F7F7F7")
            self.canvas_host.configure(fg_color="#F7F7F7")
            img = render_3d_preview(
                self._asset.mesh_3d,
                w,
                h,
                yaw_deg=self._yaw,
                pitch_deg=self._pitch,
                zoom=self._zoom,
                bg_rgb=(247, 247, 247),
                supersample=2 if hq else 1,
            )
        else:
            self.canvas.configure(bg="#1C1C1E")
            self.canvas_host.configure(fg_color="#1C1C1E")
            img = render_2d_footprint_preview(
                self._asset,
                w,
                h,
                zoom=self._zoom,
            )

        self._photo_ref = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo_ref)


class LcscToIntLibApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title("LCSC Part Importer — Altium Designer")
        self.geometry("920x700")
        self.minsize(780, 620)
        self.configure(fg_color=BG_WINDOW)
        self._apply_window_icon()
        # CustomTkinter overrides iconbitmap ~200ms after init; re-apply cleanly
        self.after(250, self._apply_window_icon)

        self.cfg = _load_config()
        self.output_dir = Path(self.cfg.get("output_dir") or DEFAULT_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.integrate_output_dir = Path(
            self.cfg.get("integrate_output_dir") or self.output_dir
        )
        self.integrate_output_dir.mkdir(parents=True, exist_ok=True)

        self.current_bundle: ComponentBundle | None = None
        self.current_metrics: Model3DMetrics | None = None
        self.current_thumb_ctk: ctk.CTkImage | None = None
        self.is_busy = False
        self._last_clipboard_raw = ""
        self._last_auto_lcsc = ""
        self._history_count = 0
        self._preview_visible = False
        self._active_tab = "search"

        # Integrate tab state
        self._integrate_items: list[IntLibFileInfo] = []
        self._integrate_log_count = 0

        self._build_ui()
        self._switch_tab("search")
        self._init_clipboard_baseline()
        self.after(650, self._poll_clipboard)

    def _apply_window_icon(self) -> None:
        """Apply crisp multi-resolution .ico icon to Windows title bar and taskbar."""
        try:
            if ICON_ICO_PATH.exists():
                ico_str = str(ICON_ICO_PATH)
                self.iconbitmap(default=ico_str)
                self.wm_iconbitmap(ico_str)

                if sys.platform == "win32":
                    # Explicitly load exact DPI-matched ICON_BIG (taskbar/Alt-Tab)
                    # and ICON_SMALL (title bar) frames from the multi-res .ico so
                    # Windows never downscales a single bitmap blurrily.
                    user32 = ctypes.windll.user32
                    user32.LoadImageW.restype = ctypes.c_void_p
                    user32.LoadImageW.argtypes = [
                        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                        ctypes.c_int, ctypes.c_int, ctypes.c_uint,
                    ]
                    user32.SendMessageW.argtypes = [
                        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p,
                    ]
                    user32.GetParent.restype = ctypes.c_void_p
                    user32.GetParent.argtypes = [ctypes.c_void_p]

                    IMAGE_ICON = 1
                    LR_LOADFROMFILE = 0x0010
                    WM_SETICON = 0x0080
                    ICON_SMALL = 0
                    ICON_BIG = 1
                    SM_CXICON = 11
                    SM_CYICON = 12
                    SM_CXSMICON = 49
                    SM_CYSMICON = 50

                    big_w = max(48, user32.GetSystemMetrics(SM_CXICON))
                    big_h = max(48, user32.GetSystemMetrics(SM_CYICON))
                    sm_w = max(16, user32.GetSystemMetrics(SM_CXSMICON))
                    sm_h = max(16, user32.GetSystemMetrics(SM_CYSMICON))

                    hicon_big = user32.LoadImageW(None, ico_str, IMAGE_ICON, big_w, big_h, LR_LOADFROMFILE)
                    hicon_sm = user32.LoadImageW(None, ico_str, IMAGE_ICON, sm_w, sm_h, LR_LOADFROMFILE)

                    hwnd_inner = self.winfo_id()
                    hwnd_top = user32.GetParent(hwnd_inner) or hwnd_inner
                    for hwnd in {hwnd_inner, hwnd_top}:
                        if hwnd:
                            if hicon_big:
                                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                            if hicon_sm:
                                user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_sm)
                    self._win_hicons = (hicon_big, hicon_sm)
                    # Match Windows 11 native title bar tone to Altium Designer 26 (#474747)
                    try:
                        dwmapi = ctypes.windll.dwmapi
                        DWMWA_BORDER_COLOR = 34
                        DWMWA_CAPTION_COLOR = 35
                        DWMWA_TEXT_COLOR = 36
                        # COLORREF is 0x00BBGGRR
                        caption_color = ctypes.c_int(0x00474747)
                        text_color = ctypes.c_int(0x00EAEAEA)
                        border_color = ctypes.c_int(0x00525252)
                        if hwnd_top:
                            dwmapi.DwmSetWindowAttribute(
                                hwnd_top, DWMWA_CAPTION_COLOR, ctypes.byref(caption_color), 4
                            )
                            dwmapi.DwmSetWindowAttribute(
                                hwnd_top, DWMWA_TEXT_COLOR, ctypes.byref(text_color), 4
                            )
                            dwmapi.DwmSetWindowAttribute(
                                hwnd_top, DWMWA_BORDER_COLOR, ctypes.byref(border_color), 4
                            )
                    except Exception:
                        pass
                    return

            # Non-Windows fallback: supply multiple sharp resolutions to wm_iconphoto
            if ICON_PNG_PATH.exists():
                base = Image.open(ICON_PNG_PATH).convert("RGBA")
                self._win_icon_photos = [
                    ImageTk.PhotoImage(base.resize((s, s), Image.Resampling.LANCZOS))
                    for s in (256, 64, 48, 32, 24, 16)
                ]
                self.wm_iconphoto(True, *self._win_icon_photos)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Altium Panel Section Helper
    # ------------------------------------------------------------------
    def _make_section(
        self, parent: ctk.CTkBaseClass, title: str
    ) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
        outer = ctk.CTkFrame(
            parent,
            fg_color=BG_PANEL,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_PANEL,
        )
        outer.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(outer, fg_color=BG_HEADER, corner_radius=0, height=24)
        bar.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        bar.grid_propagate(False)

        ctk.CTkLabel(
            bar,
            text=title.upper(),
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            text_color=TEXT_SECONDARY,
        ).pack(side="left", padx=8, pady=2)

        body = ctk.CTkFrame(outer, fg_color="transparent", corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        body.grid_columnconfigure(0, weight=1)
        return outer, body

    # ------------------------------------------------------------------
    # Root Layout (Top Bar with Tabs + View Container + Status Strip)
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # 0. Slim Altium Designer Menu Bar (Search  Integrate  ...  Auto-Clipboard)
        topbar = ctk.CTkFrame(self, fg_color=BG_MENUBAR, corner_radius=0, height=28)
        topbar.grid(row=0, column=0, sticky="ew")
        topbar.grid_propagate(False)
        topbar.grid_columnconfigure(1, weight=1)

        tabs_box = ctk.CTkFrame(topbar, fg_color="transparent", corner_radius=0)
        tabs_box.grid(row=0, column=0, padx=(4, 0), sticky="ns")

        self._tab_btns: dict[str, ctk.CTkButton] = {}
        for col_i, (tab_key, tab_label, tab_w) in enumerate(
            [("search", "Search", 62), ("integrate", "Integrate", 72)]
        ):
            btn = ctk.CTkButton(
                tabs_box,
                text=tab_label,
                width=tab_w,
                height=24,
                corner_radius=2,
                fg_color="transparent",
                hover_color=BG_MENUBAR_HOVER,
                text_color="#D0D0D0",
                font=ctk.CTkFont(family=FONT_UI, size=11),
                command=lambda k=tab_key: self._switch_tab(k),
            )
            btn.grid(row=0, column=col_i, padx=1, pady=2)
            self._tab_btns[tab_key] = btn

        # Compact Auto-Clipboard toggle on the right of menu bar
        self.auto_clip_var = ctk.BooleanVar(value=bool(self.cfg.get("auto_clipboard", True)))
        ctk.CTkSwitch(
            topbar,
            text="Auto-Clipboard",
            variable=self.auto_clip_var,
            command=self._on_toggle_clipboard,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=TEXT_SECONDARY,
            progress_color=ALTIUM_BLUE,
            button_color="#DCDCDC",
            button_hover_color="#FFFFFF",
            fg_color="#5A5A5A",
            switch_width=30,
            switch_height=14,
        ).grid(row=0, column=2, padx=10, sticky="e")

        # 1px crisp divider in its own row (never overlaps menu buttons)
        ctk.CTkFrame(self, fg_color=BORDER_PANEL, height=1, corner_radius=0).grid(
            row=1, column=0, sticky="ew"
        )

        # 2. Main Page Container
        self.page_container = ctk.CTkFrame(self, fg_color=BG_WINDOW, corner_radius=0)
        self.page_container.grid(row=2, column=0, sticky="nsew")
        self.page_container.grid_columnconfigure(0, weight=1)
        self.page_container.grid_rowconfigure(0, weight=1)

        self.search_page = ctk.CTkFrame(self.page_container, fg_color=BG_WINDOW, corner_radius=0)
        self.integrate_page = ctk.CTkFrame(self.page_container, fg_color=BG_WINDOW, corner_radius=0)

        self._build_search_page(self.search_page)
        self._build_integrate_page(self.integrate_page)

        # 3. Bottom Altium Status Strip
        status_bar = ctk.CTkFrame(self, fg_color=BG_STATUSBAR, corner_radius=0, height=24)
        status_bar.grid(row=3, column=0, sticky="ew")
        status_bar.grid_propagate(False)
        status_bar.grid_columnconfigure(0, weight=1)

        ctk.CTkFrame(status_bar, fg_color=BORDER_PANEL, height=1, corner_radius=0).place(
            relx=0, rely=0, relwidth=1
        )

        self.status_label = ctk.CTkLabel(
            status_bar,
            text="Ready",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=TEXT_SECONDARY,
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, padx=10, pady=(2, 0), sticky="w")

        ctk.CTkLabel(
            status_bar,
            text="Altium Designer 26  •  IntLib / LibPkg",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=TEXT_MUTED,
            anchor="e",
        ).grid(row=0, column=1, padx=10, pady=(2, 0), sticky="e")

    def _switch_tab(self, tab_key: str) -> None:
        self._active_tab = tab_key
        for k, btn in self._tab_btns.items():
            if k == tab_key:
                btn.configure(fg_color=BG_MENUBAR_ACTIVE, text_color="#FFFFFF")
            else:
                btn.configure(fg_color="transparent", text_color="#D0D0D0")

        self.search_page.grid_forget()
        self.integrate_page.grid_forget()
        if tab_key == "search":
            self.search_page.grid(row=0, column=0, sticky="nsew")
        else:
            self.integrate_page.grid(row=0, column=0, sticky="nsew")

    # ==================================================================
    # TAB 1: SEARCH & SINGLE .IntLib DOWNLOAD + 3D/2D SPLIT PREVIEW
    # ==================================================================
    def _build_search_page(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)

        # 1. Search Bar Section
        search_outer, search_body = self._make_section(parent, "Search")
        search_outer.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="ew")
        search_body.grid_columnconfigure(0, weight=1)

        search_row = ctk.CTkFrame(search_body, fg_color="transparent")
        search_row.grid(row=0, column=0, padx=8, pady=7, sticky="ew")
        search_row.grid_columnconfigure(0, weight=1)

        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(
            search_row,
            textvariable=self.search_var,
            placeholder_text="LCSC Part # (e.g. C465406), URL, or MPN...",
            height=29,
            corner_radius=2,
            border_width=1,
            fg_color=BG_INPUT,
            border_color=BORDER_INPUT,
            text_color=TEXT_PRIMARY,
            placeholder_text_color=TEXT_MUTED,
            font=ctk.CTkFont(family=FONT_UI, size=12),
        )
        self.search_entry.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.search_entry.bind("<Return>", lambda _e: self._start_search())

        self.paste_btn = ctk.CTkButton(
            search_row,
            text="Paste",
            width=64,
            height=29,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._paste_and_search,
        )
        self.paste_btn.grid(row=0, column=1, padx=(0, 6))

        self.search_btn = ctk.CTkButton(
            search_row,
            text="Search",
            width=78,
            height=29,
            corner_radius=2,
            fg_color=ALTIUM_BLUE,
            hover_color=ALTIUM_BLUE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=FONT_UI, size=11, weight="bold"),
            command=self._start_search,
        )
        self.search_btn.grid(row=0, column=2)

        self.hits_frame = ctk.CTkScrollableFrame(
            search_body,
            height=105,
            corner_radius=2,
            fg_color=BG_INPUT,
            border_width=1,
            border_color=BORDER_PANEL,
        )
        self.hits_frame.grid_columnconfigure(0, weight=1)

        # 2. Component Properties Section
        comp_outer, comp_body = self._make_section(parent, "Component Properties")
        comp_outer.grid(row=1, column=0, padx=8, pady=4, sticky="ew")
        comp_body.grid_columnconfigure(1, weight=1)

        self.thumb_box = ctk.CTkFrame(
            comp_body,
            width=152,
            height=152,
            corner_radius=2,
            fg_color="#FFFFFF",
            border_width=1,
            border_color=BORDER_INPUT,
        )
        self.thumb_box.grid(row=0, column=0, padx=(10, 14), pady=10, sticky="nw")
        self.thumb_box.grid_propagate(False)

        self.thumb_label = ctk.CTkLabel(
            self.thumb_box,
            text="No Preview",
            text_color="#7A7A80",
            font=ctk.CTkFont(family=FONT_UI, size=10),
        )
        self.thumb_label.place(relx=0.5, rely=0.5, anchor="center")

        props = ctk.CTkFrame(comp_body, fg_color="transparent")
        props.grid(row=0, column=1, padx=(0, 10), pady=8, sticky="nsew")
        props.grid_columnconfigure(1, weight=1)

        title_row = ctk.CTkFrame(props, fg_color="transparent")
        title_row.grid(row=0, column=0, columnspan=2, pady=(0, 5), sticky="ew")
        title_row.grid_columnconfigure(0, weight=1)

        self.lbl_title = ctk.CTkLabel(
            title_row,
            text="No component selected",
            font=ctk.CTkFont(family=FONT_UI, size=14, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        )
        self.lbl_title.grid(row=0, column=0, sticky="w")

        links_row = ctk.CTkFrame(title_row, fg_color="transparent")
        links_row.grid(row=0, column=1, sticky="e")

        self.lbl_lcsc_code = ctk.CTkLabel(
            links_row,
            text="",
            font=ctk.CTkFont(family=FONT_MONO, size=11, weight="bold"),
            text_color=ALTIUM_GOLD,
            padx=6,
        )
        self.lbl_lcsc_code.pack(side="left", padx=(0, 6))

        self.btn_datasheet = ctk.CTkButton(
            links_row,
            text="Datasheet",
            width=72,
            height=23,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            state="disabled",
            command=self._open_datasheet,
        )
        self.btn_datasheet.pack(side="left", padx=(0, 4))

        self.btn_lcsc = ctk.CTkButton(
            links_row,
            text="LCSC",
            width=52,
            height=23,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            state="disabled",
            command=self._open_lcsc,
        )
        self.btn_lcsc.pack(side="left")

        self._prop_labels: dict[str, ctk.CTkLabel] = {}
        for row_i, (key, caption) in enumerate(
            [
                ("mfr", "Manufacturer"),
                ("mpn", "MPN / Package"),
                ("supply", "Price / Stock"),
                ("desc", "Description"),
                ("model3d", "3D STEP Model"),
            ],
            start=1,
        ):
            ctk.CTkLabel(
                props,
                text=caption,
                width=96,
                anchor="w",
                font=ctk.CTkFont(family=FONT_UI, size=11),
                text_color=TEXT_SECONDARY,
            ).grid(row=row_i, column=0, padx=(0, 8), pady=1, sticky="nw")

            val_lbl = ctk.CTkLabel(
                props,
                text="—",
                anchor="w",
                justify="left",
                wraplength=580,
                font=ctk.CTkFont(family=FONT_UI, size=11),
                text_color=TEXT_PRIMARY,
            )
            val_lbl.grid(row=row_i, column=1, pady=1, sticky="ew")
            self._prop_labels[key] = val_lbl

        # 3. Export Section
        exp_outer, exp_body = self._make_section(parent, "Export")
        exp_outer.grid(row=2, column=0, padx=8, pady=4, sticky="ew")
        exp_body.grid_columnconfigure(0, weight=1)

        dir_row = ctk.CTkFrame(exp_body, fg_color="transparent")
        dir_row.grid(row=0, column=0, padx=8, pady=(7, 5), sticky="ew")
        dir_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            dir_row,
            text="Output",
            width=50,
            anchor="w",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_SECONDARY,
        ).grid(row=0, column=0, padx=(0, 6), sticky="w")

        self.dir_var = ctk.StringVar(value=str(self.output_dir))
        ctk.CTkEntry(
            dir_row,
            textvariable=self.dir_var,
            height=27,
            corner_radius=2,
            border_width=1,
            state="readonly",
            fg_color=BG_INPUT,
            border_color=BORDER_INPUT,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_MONO, size=11),
        ).grid(row=0, column=1, padx=(0, 6), sticky="ew")

        ctk.CTkButton(
            dir_row,
            text="...",
            width=34,
            height=27,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._choose_output_dir,
        ).grid(row=0, column=2, padx=(0, 4))

        ctk.CTkButton(
            dir_row,
            text="Open",
            width=56,
            height=27,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._open_output_dir,
        ).grid(row=0, column=3)

        opt_row = ctk.CTkFrame(exp_body, fg_color="transparent")
        opt_row.grid(row=1, column=0, padx=8, pady=(0, 7), sticky="ew")
        opt_row.grid_columnconfigure(2, weight=1)

        self.altium_style_var = ctk.BooleanVar(
            value=bool(self.cfg.get("altium_native_style", True))
        )
        ctk.CTkCheckBox(
            opt_row,
            text="Altium Native Style",
            variable=self.altium_style_var,
            command=self._on_toggle_altium_style,
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=2,
            border_width=1,
            fg_color=ALTIUM_BLUE,
            hover_color=ALTIUM_BLUE_HOVER,
            border_color=BORDER_INPUT,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_PRIMARY,
        ).grid(row=0, column=0, padx=(0, 16), sticky="w")

        self.save_sources_var = ctk.BooleanVar(
            value=bool(self.cfg.get("save_sources", False))
        )
        ctk.CTkCheckBox(
            opt_row,
            text="Save Sources (.SchLib / .PcbLib / .STEP)",
            variable=self.save_sources_var,
            command=self._on_toggle_sources,
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=2,
            border_width=1,
            fg_color=ALTIUM_BLUE,
            hover_color=ALTIUM_BLUE_HOVER,
            border_color=BORDER_INPUT,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_SECONDARY,
        ).grid(row=0, column=1, sticky="w")

        rot_box = ctk.CTkFrame(opt_row, fg_color="transparent")
        rot_box.grid(row=0, column=2, sticky="e")

        ctk.CTkLabel(
            rot_box,
            text="3D Rot Z",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_SECONDARY,
        ).pack(side="left", padx=(0, 6))

        self.rot_z_var = ctk.StringVar(value="0°")
        ctk.CTkOptionMenu(
            rot_box,
            variable=self.rot_z_var,
            values=["0°", "90°", "180°", "270°"],
            width=72,
            height=24,
            corner_radius=2,
            fg_color=BG_INPUT,
            button_color=BG_BTN,
            button_hover_color=BG_BTN_HOVER,
            dropdown_fg_color=BG_PANEL,
            dropdown_hover_color=ALTIUM_BLUE,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
        ).pack(side="left")

        self.download_btn = ctk.CTkButton(
            exp_body,
            text="Download .IntLib",
            height=33,
            corner_radius=2,
            fg_color=ALTIUM_BLUE,
            hover_color=ALTIUM_BLUE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=FONT_UI, size=12, weight="bold"),
            state="disabled",
            command=self._start_download,
        )
        self.download_btn.grid(row=2, column=0, padx=8, pady=(0, 7), sticky="ew")

        # 4. Recent Exports + Split 3D/2D Preview Pane (Altium style!)
        hist_outer, hist_body = self._make_section(parent, "Recent Exports")
        hist_outer.grid(row=3, column=0, padx=8, pady=(4, 6), sticky="nsew")
        hist_outer.grid_rowconfigure(1, weight=1)
        hist_body.grid_rowconfigure(0, weight=1)
        hist_body.grid_columnconfigure(0, weight=1)
        hist_body.grid_columnconfigure(1, weight=0)
        hist_body.grid_columnconfigure(2, weight=0)

        self.history_scroll = ctk.CTkScrollableFrame(
            hist_body,
            fg_color=BG_INPUT,
            corner_radius=0,
        )
        self.history_scroll.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.history_scroll.grid_columnconfigure(0, weight=1)

        self.empty_hist_lbl = ctk.CTkLabel(
            self.history_scroll,
            text="No exports yet. Copy an LCSC part # (e.g. C465406) to begin.",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_MUTED,
        )
        self.empty_hist_lbl.pack(pady=18)

        # Vertical split divider bar + Collapsible Altium 3D/2D Footprint Preview Pane
        self.preview_divider = ctk.CTkFrame(
            hist_body, fg_color=BORDER_PANEL, width=2, corner_radius=0
        )
        self.preview_pane = AltiumPreviewPane(hist_body, on_hide=self._hide_preview_pane)

    def _show_preview_pane(self) -> None:
        if not self._preview_visible:
            self._preview_visible = True
            self.preview_divider.grid(row=0, column=1, pady=4, sticky="ns")
            self.preview_pane.grid(row=0, column=2, padx=(0, 6), pady=6, sticky="nsew")

    def _hide_preview_pane(self) -> None:
        if self._preview_visible:
            self._preview_visible = False
            self.preview_divider.grid_forget()
            self.preview_pane.grid_forget()

    def _open_3d_preview_for_intlib(self, intlib_path: Path) -> None:
        self._show_preview_pane()
        self.preview_pane.show_loading(intlib_path.stem)
        self._set_status(f"Loading 3D preview for {intlib_path.name}...", ALTIUM_BLUE_HOVER)

        def worker() -> None:
            try:
                asset = load_preview_from_intlib(intlib_path)
                self.after(0, lambda: self._on_preview_loaded(asset))
            except Exception as exc:
                self.after(0, lambda: self._on_preview_error(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_preview_loaded(self, asset: PreviewAsset) -> None:
        self.preview_pane.set_asset(asset)
        self._set_status(f"Previewing {asset.footprint_name}", STATUS_OK)

    def _on_preview_error(self, msg: str) -> None:
        self.preview_pane.show_error(f"Preview failed:\n{msg}")
        self._set_status(f"Preview error: {msg}", STATUS_ERR)

    # ==================================================================
    # TAB 2: INTEGRATE (MULTI-.IntLib -> CUSTOM .LibPkg + .IntLib)
    # ==================================================================
    def _build_integrate_page(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=0)

        # 1. Source Libraries Selection Section
        src_outer, src_body = self._make_section(parent, "Source Libraries (.IntLib)")
        src_outer.grid(row=0, column=0, padx=8, pady=(8, 4), sticky="nsew")
        src_outer.grid_rowconfigure(1, weight=1)
        src_body.grid_rowconfigure(1, weight=1)
        src_body.grid_columnconfigure(0, weight=1)

        tb = ctk.CTkFrame(src_body, fg_color="transparent")
        tb.grid(row=0, column=0, padx=8, pady=(7, 5), sticky="ew")
        tb.grid_columnconfigure(4, weight=1)

        self.btn_int_add = ctk.CTkButton(
            tb,
            text="+ Add .IntLib Files...",
            width=136,
            height=28,
            corner_radius=2,
            fg_color=ALTIUM_BLUE,
            hover_color=ALTIUM_BLUE_HOVER,
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=FONT_UI, size=11, weight="bold"),
            command=self._integrate_add_files,
        )
        self.btn_int_add.grid(row=0, column=0, padx=(0, 6))

        self.btn_int_folder = ctk.CTkButton(
            tb,
            text="Add Folder...",
            width=96,
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._integrate_add_folder,
        )
        self.btn_int_folder.grid(row=0, column=1, padx=(0, 6))

        self.btn_int_clear = ctk.CTkButton(
            tb,
            text="Clear",
            width=62,
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._integrate_clear,
        )
        self.btn_int_clear.grid(row=0, column=2)

        self.lbl_int_summary = ctk.CTkLabel(
            tb,
            text="0 libraries selected",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_SECONDARY,
            anchor="e",
        )
        self.lbl_int_summary.grid(row=0, column=4, sticky="e")

        self.int_list_scroll = ctk.CTkScrollableFrame(
            src_body,
            fg_color=BG_INPUT,
            corner_radius=0,
        )
        self.int_list_scroll.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.int_list_scroll.grid_columnconfigure(0, weight=1)

        # 2. Integrated Library Output Config
        out_outer, out_body = self._make_section(parent, "Integrated Library Output")
        out_outer.grid(row=1, column=0, padx=8, pady=4, sticky="ew")
        out_body.grid_columnconfigure(0, weight=1)

        form = ctk.CTkFrame(out_body, fg_color="transparent")
        form.grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        form.grid_columnconfigure(1, weight=1)

        # Custom Output Name
        ctk.CTkLabel(
            form,
            text="Library Name",
            width=86,
            anchor="w",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_SECONDARY,
        ).grid(row=0, column=0, padx=(0, 8), pady=(0, 6), sticky="w")

        self.int_name_var = ctk.StringVar(value="Custom_Integrated_Lib")
        self.int_name_var.trace_add("write", lambda *_a: self._update_integrate_preview_text())
        ctk.CTkEntry(
            form,
            textvariable=self.int_name_var,
            placeholder_text="Enter custom library name (e.g. Wattmeter_CT)...",
            height=28,
            corner_radius=2,
            border_width=1,
            fg_color=BG_INPUT,
            border_color=BORDER_INPUT,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=12),
        ).grid(row=0, column=1, columnspan=3, pady=(0, 6), sticky="ew")

        # Output Folder
        ctk.CTkLabel(
            form,
            text="Output Folder",
            width=86,
            anchor="w",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_SECONDARY,
        ).grid(row=1, column=0, padx=(0, 8), pady=(0, 6), sticky="w")

        self.int_dir_var = ctk.StringVar(value=str(self.integrate_output_dir))
        ctk.CTkEntry(
            form,
            textvariable=self.int_dir_var,
            height=28,
            corner_radius=2,
            border_width=1,
            state="readonly",
            fg_color=BG_INPUT,
            border_color=BORDER_INPUT,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_MONO, size=11),
        ).grid(row=1, column=1, padx=(0, 6), pady=(0, 6), sticky="ew")

        ctk.CTkButton(
            form,
            text="...",
            width=34,
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._choose_integrate_dir,
        ).grid(row=1, column=2, padx=(0, 4), pady=(0, 6))

        ctk.CTkButton(
            form,
            text="Open",
            width=56,
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=11),
            command=self._open_integrate_dir,
        ).grid(row=1, column=3, pady=(0, 6))

        # Live generated filenames preview
        self.lbl_int_files_preview = ctk.CTkLabel(
            form,
            text="",
            font=ctk.CTkFont(family=FONT_MONO, size=10),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.lbl_int_files_preview.grid(row=2, column=1, columnspan=3, pady=(0, 8), sticky="ew")
        self._update_integrate_preview_text()

        # Generate Button
        self.btn_int_generate = ctk.CTkButton(
            form,
            text="Generate Integrated Library  (.LibPkg + .IntLib)",
            height=34,
            corner_radius=2,
            fg_color=BG_BTN_DISABLED,
            hover_color=ALTIUM_BLUE_HOVER,
            text_color=TEXT_MUTED,
            font=ctk.CTkFont(family=FONT_UI, size=12, weight="bold"),
            state="disabled",
            command=self._start_integrate,
        )
        self.btn_int_generate.grid(row=3, column=0, columnspan=4, sticky="ew")

        # Extraction & Build Progress Bar + Live Stage Label
        prog_wrap = ctk.CTkFrame(form, fg_color="transparent")
        prog_wrap.grid(row=4, column=0, columnspan=4, pady=(8, 0), sticky="ew")
        prog_wrap.grid_columnconfigure(0, weight=1)

        self.int_progress_lbl = ctk.CTkLabel(
            prog_wrap,
            text="Ready — Add .IntLib files above to integrate",
            font=ctk.CTkFont(family=FONT_UI, size=10),
            text_color=TEXT_MUTED,
            anchor="w",
        )
        self.int_progress_lbl.grid(row=0, column=0, sticky="ew")

        self.int_progress_pct = ctk.CTkLabel(
            prog_wrap,
            text="0%",
            font=ctk.CTkFont(family=FONT_MONO, size=10, weight="bold"),
            text_color=TEXT_MUTED,
            anchor="e",
        )
        self.int_progress_pct.grid(row=0, column=1, padx=(8, 0), sticky="e")

        self.int_progress_bar = ctk.CTkProgressBar(
            prog_wrap,
            height=8,
            corner_radius=1,
            fg_color=BG_INPUT,
            progress_color=ALTIUM_BLUE,
            border_width=1,
            border_color=BORDER_PANEL,
        )
        self.int_progress_bar.set(0.0)
        self.int_progress_bar.grid(row=1, column=0, columnspan=2, pady=(3, 0), sticky="ew")

        # 3. Integration Output Log
        log_outer, log_body = self._make_section(parent, "Generated Packages")
        log_outer.grid(row=2, column=0, padx=8, pady=(4, 6), sticky="ew")
        log_body.grid_columnconfigure(0, weight=1)

        self.int_log_scroll = ctk.CTkScrollableFrame(
            log_body,
            height=110,
            fg_color=BG_INPUT,
            corner_radius=0,
        )
        self.int_log_scroll.grid(row=0, column=0, padx=6, pady=6, sticky="ew")
        self.int_log_scroll.grid_columnconfigure(0, weight=1)

        self.empty_int_log = ctk.CTkLabel(
            self.int_log_scroll,
            text="Generated .LibPkg and .IntLib packages will appear here.",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_MUTED,
        )
        self.empty_int_log.pack(pady=24)

        self._render_integrate_list()

    def _update_integrate_preview_text(self) -> None:
        clean = sanitize_library_name(self.int_name_var.get(), fallback="Integrated_Library")
        self.lbl_int_files_preview.configure(
            text=f"Outputs:  {clean}.LibPkg  •  {clean}.IntLib  •  {clean}.SchLib  •  {clean}.PcbLib"
        )

    def _integrate_add_files(self) -> None:
        chosen = filedialog.askopenfilenames(
            initialdir=str(self.output_dir),
            title="Select .IntLib Files to Integrate",
            filetypes=[("Altium Integrated Library", "*.IntLib;*.intlib"), ("All Files", "*.*")],
        )
        if not chosen:
            return
        self._load_intlib_paths([Path(p) for p in chosen])

    def _integrate_add_folder(self) -> None:
        folder = filedialog.askdirectory(
            initialdir=str(self.output_dir),
            title="Select Folder Containing .IntLib Files",
        )
        if not folder:
            return
        folder_path = Path(folder)
        files = sorted(
            p for p in folder_path.iterdir()
            if p.is_file() and p.suffix.lower() == ".intlib"
        )
        if not files:
            self._set_status(f"No .IntLib files found in {folder_path.name}", STATUS_WARN)
            return
        # Auto-suggest folder name as custom library name if still default
        if self.int_name_var.get().strip() in ("", "Custom_Integrated_Lib"):
            self.int_name_var.set(sanitize_library_name(folder_path.name))
        self._load_intlib_paths(files)

    def _load_intlib_paths(self, new_paths: list[Path]) -> None:
        existing = {item.path.resolve() for item in self._integrate_items}
        to_inspect = [p for p in new_paths if p.resolve() not in existing]
        if not to_inspect:
            self._set_status("Selected files are already in the list", STATUS_WARN)
            return

        self._set_status(f"Inspecting {len(to_inspect)} .IntLib file(s)...", ALTIUM_BLUE_HOVER)

        def worker() -> None:
            infos = [inspect_intlib(p) for p in to_inspect]
            self.after(0, lambda: self._on_intlibs_inspected(infos))

        threading.Thread(target=worker, daemon=True).start()

    def _on_intlibs_inspected(self, infos: list[IntLibFileInfo]) -> None:
        self._integrate_items.extend(infos)
        self._render_integrate_list()
        total_c = sum(i.component_count for i in self._integrate_items)
        self._set_status(
            f"Loaded {len(self._integrate_items)} libraries ({total_c} components total)",
            STATUS_OK,
        )

    def _integrate_remove(self, target_path: Path) -> None:
        self._integrate_items = [i for i in self._integrate_items if i.path != target_path]
        self._render_integrate_list()

    def _integrate_clear(self) -> None:
        self._integrate_items.clear()
        self._render_integrate_list()

    def _render_integrate_list(self) -> None:
        for w in self.int_list_scroll.winfo_children():
            w.destroy()

        valid_items = [i for i in self._integrate_items if not i.error]
        total_comps = sum(i.component_count for i in valid_items)
        self.lbl_int_summary.configure(
            text=f"{len(self._integrate_items)} files  •  {total_comps} components"
        )
        can_gen = bool(valid_items) and not self.is_busy
        self.btn_int_generate.configure(
            state="normal" if can_gen else "disabled",
            fg_color=ALTIUM_BLUE if can_gen else BG_BTN_DISABLED,
            text_color="#FFFFFF" if can_gen else TEXT_MUTED,
        )
        if valid_items and not self.is_busy:
            self.int_progress_lbl.configure(
                text=f"Ready — {len(valid_items)} libraries ({total_comps} components) queued",
                text_color=TEXT_SECONDARY,
            )
            self.int_progress_pct.configure(text="0%", text_color=TEXT_MUTED)
            self.int_progress_bar.configure(progress_color=ALTIUM_BLUE)
            self.int_progress_bar.set(0.0)
        elif not self._integrate_items:
            self.int_progress_lbl.configure(
                text="Ready — Add .IntLib files above to integrate",
                text_color=TEXT_MUTED,
            )
            self.int_progress_pct.configure(text="0%", text_color=TEXT_MUTED)
            self.int_progress_bar.set(0.0)

        if not self._integrate_items:
            ctk.CTkLabel(
                self.int_list_scroll,
                text="Click '+ Add .IntLib Files...' or 'Add Folder...' to select libraries to merge.",
                font=ctk.CTkFont(family=FONT_UI, size=11),
                text_color=TEXT_MUTED,
            ).pack(pady=36)
            return

        for idx, info in enumerate(self._integrate_items):
            bg = BG_PANEL if idx % 2 == 0 else BG_ROW_ALT
            row = ctk.CTkFrame(self.int_list_scroll, fg_color=bg, corner_radius=1, height=28)
            row.pack(fill="x", padx=1, pady=1)
            row.grid_columnconfigure(1, weight=1)

            status_dot = "●" if not info.error else "✕"
            dot_col = STATUS_OK if not info.error else STATUS_ERR

            ctk.CTkLabel(
                row,
                text=status_dot,
                font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
                text_color=dot_col,
                width=18,
            ).grid(row=0, column=0, padx=(6, 2), pady=3)

            if info.error:
                detail = f"{info.path.name}   —   Error: {info.error}"
            else:
                names_preview = ", ".join(info.component_names[:3])
                if len(info.component_names) > 3:
                    names_preview += f" (+{len(info.component_names) - 3})"
                detail = f"{info.path.name}   —   {info.component_count} comp ({names_preview})"

            ctk.CTkLabel(
                row,
                text=detail,
                font=ctk.CTkFont(family=FONT_UI, size=11),
                text_color=TEXT_PRIMARY if not info.error else STATUS_ERR,
                anchor="w",
            ).grid(row=0, column=1, padx=4, pady=3, sticky="ew")

            ctk.CTkLabel(
                row,
                text=f"{info.size_kb:.0f} KB",
                font=ctk.CTkFont(family=FONT_MONO, size=10),
                text_color=TEXT_MUTED,
            ).grid(row=0, column=2, padx=8, pady=3)

            ctk.CTkButton(
                row,
                text="×",
                width=24,
                height=20,
                corner_radius=2,
                fg_color="transparent",
                hover_color="#5A2A2A",
                text_color=TEXT_SECONDARY,
                font=ctk.CTkFont(family=FONT_UI, size=12, weight="bold"),
                command=lambda p=info.path: self._integrate_remove(p),
            ).grid(row=0, column=3, padx=(0, 6), pady=3)

    def _choose_integrate_dir(self) -> None:
        chosen = filedialog.askdirectory(
            initialdir=str(self.integrate_output_dir),
            title="Select Integrated Library Output Folder",
        )
        if chosen:
            self.integrate_output_dir = Path(chosen)
            self.integrate_output_dir.mkdir(parents=True, exist_ok=True)
            self.int_dir_var.set(str(self.integrate_output_dir))
            self.cfg["integrate_output_dir"] = str(self.integrate_output_dir)
            _save_config(self.cfg)

    def _open_integrate_dir(self) -> None:
        self.integrate_output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.integrate_output_dir))

    def _start_integrate(self) -> None:
        if self.is_busy:
            return
        valid_paths = [i.path for i in self._integrate_items if not i.error]
        if not valid_paths:
            self._set_status("No valid .IntLib files selected", STATUS_WARN)
            return
        custom_name = self.int_name_var.get().strip()
        if not custom_name:
            self._set_status("Enter a custom library name first", STATUS_WARN)
            return

        out_dir = self.integrate_output_dir
        self._set_busy(True, f"Integrating {len(valid_paths)} libraries into '{custom_name}'...")
        self.btn_int_generate.configure(state="disabled", fg_color=BG_BTN_DISABLED, text_color=TEXT_MUTED)
        self.int_progress_bar.configure(progress_color=ALTIUM_BLUE)
        self.int_progress_bar.set(0.02)
        self.int_progress_pct.configure(text="2%", text_color=ALTIUM_BLUE_HOVER)
        self.int_progress_lbl.configure(
            text=f"Starting integration of {len(valid_paths)} libraries...",
            text_color=ALTIUM_BLUE_HOVER,
        )

        def on_progress(frac: float, msg: str) -> None:
            self.after(0, lambda f=frac, m=msg: self._on_integrate_progress(f, m))

        def worker() -> None:
            try:
                res = integrate_intlibs(
                    valid_paths,
                    out_dir,
                    custom_name,
                    progress=on_progress,
                )
                self.after(0, lambda: self._on_integrate_done(res))
            except Exception as exc:
                self.after(0, lambda e=str(exc): self._on_integrate_error(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_integrate_progress(self, frac: float, msg: str) -> None:
        self.int_progress_bar.set(frac)
        self.int_progress_pct.configure(text=f"{int(round(frac * 100))}%", text_color=ALTIUM_BLUE_HOVER)
        self.int_progress_lbl.configure(text=msg, text_color=TEXT_PRIMARY)
        self._set_status(msg, ALTIUM_BLUE_HOVER)

    def _on_integrate_done(self, res: IntegrateResult) -> None:
        self._set_busy(False)
        self.btn_int_generate.configure(state="normal", fg_color=ALTIUM_BLUE, text_color="#FFFFFF")
        size_kb = res.intlib_path.stat().st_size / 1024.0
        done_msg = (
            f"✓ Completed: {res.intlib_path.name} + {res.libpkg_path.name} "
            f"({res.component_count} comp, {res.footprint_count} fp, {res.model_3d_count} 3D, {size_kb:.0f} KB)"
        )
        self.int_progress_bar.configure(progress_color=STATUS_OK)
        self.int_progress_bar.set(1.0)
        self.int_progress_pct.configure(text="100%", text_color=STATUS_OK)
        self.int_progress_lbl.configure(text=done_msg, text_color=STATUS_OK)
        self._set_status(done_msg, STATUS_OK)

        if self.empty_int_log.winfo_exists():
            self.empty_int_log.destroy()

        bg = BG_PANEL if self._integrate_log_count % 2 == 0 else BG_ROW_ALT
        self._integrate_log_count += 1

        row = ctk.CTkFrame(self.int_log_scroll, fg_color=bg, corner_radius=1, height=28)
        row.pack(fill="x", padx=1, pady=1)
        row.grid_columnconfigure(1, weight=1)

        ts = datetime.now().strftime("%H:%M:%S")
        ctk.CTkLabel(
            row,
            text=ts,
            font=ctk.CTkFont(family=FONT_MONO, size=10),
            text_color=TEXT_MUTED,
            width=58,
            anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=4)

        ctk.CTkLabel(
            row,
            text=(
                f"{res.intlib_path.name} + {res.libpkg_path.name}   "
                f"({res.component_count} Comp / {res.footprint_count} FP / {res.model_3d_count} 3D, {size_kb:.0f} KB)"
            ),
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_PRIMARY,
            anchor="w",
        ).grid(row=0, column=1, padx=4, pady=4, sticky="ew")

        ctk.CTkButton(
            row,
            text="LibPkg",
            width=54,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=ALTIUM_BLUE,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            command=lambda p=res.libpkg_path: self._reveal_in_explorer(p),
        ).grid(row=0, column=2, padx=(4, 4), pady=3)

        ctk.CTkButton(
            row,
            text="IntLib",
            width=54,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=ALTIUM_BLUE,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            command=lambda p=res.intlib_path: self._reveal_in_explorer(p),
        ).grid(row=0, column=3, padx=(0, 6), pady=3)

    def _on_integrate_error(self, msg: str) -> None:
        self._set_busy(False)
        valid_items = [i for i in self._integrate_items if not i.error]
        can_gen = bool(valid_items)
        self.btn_int_generate.configure(
            state="normal" if can_gen else "disabled",
            fg_color=ALTIUM_BLUE if can_gen else BG_BTN_DISABLED,
            text_color="#FFFFFF" if can_gen else TEXT_MUTED,
        )
        self.int_progress_bar.configure(progress_color=STATUS_ERR)
        self.int_progress_pct.configure(text="ERR", text_color=STATUS_ERR)
        self.int_progress_lbl.configure(text=f"✕ Failed: {msg}", text_color=STATUS_ERR)
        self._set_status(f"Integration failed: {msg}", STATUS_ERR)

    # ------------------------------------------------------------------
    # Clipboard Monitoring
    # ------------------------------------------------------------------
    def _read_clipboard_safe(self) -> str:
        try:
            return str(self.clipboard_get())
        except Exception:
            return ""

    def _init_clipboard_baseline(self) -> None:
        self._last_clipboard_raw = self._read_clipboard_safe()

    def _poll_clipboard(self) -> None:
        try:
            if self.auto_clip_var.get() and not self.is_busy and self._active_tab == "search":
                raw = self._read_clipboard_safe()
                if raw and raw != self._last_clipboard_raw:
                    self._last_clipboard_raw = raw
                    cid = extract_lcsc_id(raw)
                    if cid and cid != self._last_auto_lcsc:
                        self._last_auto_lcsc = cid
                        self.search_var.set(cid)
                        self._start_fetch_lcsc(cid)
        finally:
            self.after(650, self._poll_clipboard)

    def _on_toggle_clipboard(self) -> None:
        self.cfg["auto_clipboard"] = bool(self.auto_clip_var.get())
        _save_config(self.cfg)

    def _on_toggle_sources(self) -> None:
        self.cfg["save_sources"] = bool(self.save_sources_var.get())
        _save_config(self.cfg)

    def _on_toggle_altium_style(self) -> None:
        self.cfg["altium_native_style"] = bool(self.altium_style_var.get())
        _save_config(self.cfg)

    # ------------------------------------------------------------------
    # Search & Fetch
    # ------------------------------------------------------------------
    def _paste_and_search(self) -> None:
        raw = self._read_clipboard_safe().strip()
        if not raw:
            self._set_status("Clipboard is empty", STATUS_WARN)
            return
        cid = extract_lcsc_id(raw)
        self.search_var.set(cid or raw)
        self._start_search()

    def _start_search(self) -> None:
        if self.is_busy:
            return
        query = self.search_var.get().strip()
        if not query:
            self._set_status("Enter an LCSC part # or keyword", STATUS_WARN)
            return

        cid = extract_lcsc_id(query)
        if cid:
            self._hide_hits()
            self._last_auto_lcsc = cid
            self.search_var.set(cid)
            self._start_fetch_lcsc(cid)
        else:
            self._start_keyword_search(query)

    def _start_keyword_search(self, keyword: str) -> None:
        self._set_busy(True, f"Searching '{keyword}'...")

        def worker() -> None:
            try:
                hits = search_lcsc_keyword(keyword, limit=12)
                self.after(0, lambda: self._on_keyword_hits(keyword, hits))
            except Exception as exc:
                self.after(0, lambda: self._on_error(f"Search failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_keyword_hits(self, keyword: str, hits: list[SearchHit]) -> None:
        self._set_busy(False)
        for widget in self.hits_frame.winfo_children():
            widget.destroy()

        if not hits:
            self._hide_hits()
            self._set_status(f"No matches for '{keyword}'", STATUS_WARN)
            return

        self.hits_frame.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        for idx, hit in enumerate(hits):
            bg = BG_PANEL if idx % 2 == 0 else BG_ROW_ALT
            row_btn = ctk.CTkButton(
                self.hits_frame,
                text=f" {hit.lcsc_id:<11}  {hit.title}   ({hit.package or '-'})",
                anchor="w",
                height=24,
                corner_radius=1,
                fg_color=bg,
                hover_color=ALTIUM_BLUE,
                text_color=TEXT_PRIMARY,
                font=ctk.CTkFont(family=FONT_MONO, size=11),
                command=lambda c=hit.lcsc_id: self._select_hit(c),
            )
            row_btn.grid(row=idx, column=0, pady=1, sticky="ew")

        self._set_status(f"Found {len(hits)} parts for '{keyword}'", TEXT_PRIMARY)

    def _select_hit(self, lcsc_id: str) -> None:
        self._hide_hits()
        self.search_var.set(lcsc_id)
        self._last_auto_lcsc = lcsc_id
        self._start_fetch_lcsc(lcsc_id)

    def _hide_hits(self) -> None:
        self.hits_frame.grid_forget()

    def _start_fetch_lcsc(self, lcsc_id: str) -> None:
        self._set_busy(True, f"Fetching {lcsc_id} from LCSC...")
        self.download_btn.configure(state="disabled")
        self.rot_z_var.set("0°")

        def worker() -> None:
            try:
                bundle = fetch_component(lcsc_id, fetch_step=True, fetch_thumb=True)
                metrics = (
                    inspect_step_metrics(bundle.step_bytes, bundle.step_filename)
                    if bundle.step_bytes
                    else None
                )
                self.after(0, lambda: self._on_component_loaded(bundle, metrics))
            except Exception as exc:
                self.after(0, lambda: self._on_error(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_component_loaded(
        self, bundle: ComponentBundle, metrics: Model3DMetrics | None
    ) -> None:
        self._set_busy(False)
        self.current_bundle = bundle
        self.current_metrics = metrics
        meta = bundle.meta

        self.lbl_title.configure(text=meta.title or meta.mpn or meta.lcsc_id)
        self.lbl_lcsc_code.configure(text=meta.lcsc_id)

        self._prop_labels["mfr"].configure(text=meta.manufacturer or "—")
        pkg = meta.packaging or meta.package_name or "—"
        self._prop_labels["mpn"].configure(text=f"{meta.mpn or '—'}   •   {pkg}")

        supply_parts = []
        if meta.unit_price_usd is not None:
            supply_parts.append(f"${meta.unit_price_usd:.4f} USD")
        if meta.stock_qty is not None:
            supply_parts.append(f"{meta.stock_qty:,} in stock")
        if meta.jlcpcb_class:
            supply_parts.append(meta.jlcpcb_class)
        self._prop_labels["supply"].configure(
            text="   •   ".join(supply_parts) if supply_parts else "—"
        )

        self._prop_labels["desc"].configure(text=meta.description or "—")

        # Thumbnail
        if bundle.thumb_bytes:
            try:
                img = Image.open(io.BytesIO(bundle.thumb_bytes)).convert("RGBA")
                img.thumbnail((142, 142))
                self.current_thumb_ctk = ctk.CTkImage(
                    light_image=img, dark_image=img, size=img.size
                )
                self.thumb_label.configure(image=self.current_thumb_ctk, text="")
            except Exception:
                self.thumb_label.configure(image=None, text="No Preview")
        else:
            self.thumb_label.configure(image=None, text="No Preview")

        # Clean 3D STEP status (without "(No PCB)")
        if bundle.step_bytes:
            if metrics:
                dim_txt = (
                    f"● STEP Ready  —  "
                    f"{metrics.width_mm:.2f} × {metrics.depth_mm:.2f} × {metrics.height_mm:.2f} mm  "
                    f"({metrics.file_size_kb:.0f} KB)"
                )
            else:
                dim_txt = f"● STEP Ready  —  {len(bundle.step_bytes) / 1024:.0f} KB"
            self._prop_labels["model3d"].configure(text=dim_txt, text_color=STATUS_OK)
        else:
            self._prop_labels["model3d"].configure(
                text="● 2D Only (No STEP model)", text_color=STATUS_WARN
            )

        self.btn_datasheet.configure(state="normal" if meta.datasheet_url else "disabled")
        self.btn_lcsc.configure(state="normal" if meta.lcsc_url else "disabled")

        out_name = suggested_intlib_filename(meta.title or meta.mpn, meta.lcsc_id)
        self.download_btn.configure(state="normal", text=f"Download  {out_name}")
        self._set_status(f"Loaded {meta.title} ({meta.lcsc_id})", STATUS_OK)

    # ------------------------------------------------------------------
    # Export / Build .IntLib
    # ------------------------------------------------------------------
    def _start_download(self) -> None:
        if self.is_busy or self.current_bundle is None:
            return
        bundle = self.current_bundle
        out_dir = self.output_dir
        save_sources = bool(self.save_sources_var.get())
        altium_style = bool(self.altium_style_var.get())
        rot_str = self.rot_z_var.get().replace("°", "").strip()
        extra_rot_z = float(rot_str or 0.0)

        self._set_busy(True, f"Building {bundle.meta.title}.IntLib...")

        def worker() -> None:
            try:
                res = build_intlib(
                    bundle,
                    out_dir,
                    save_sources=save_sources,
                    altium_native_style=altium_style,
                    extra_rot_z_deg=extra_rot_z,
                )
                self.after(0, lambda: self._on_build_done(res))
            except Exception as exc:
                self.after(0, lambda: self._on_error(f"Build failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_build_done(self, res: BuildResult) -> None:
        self._set_busy(False)
        self.download_btn.configure(state="normal")
        size_kb = res.intlib_path.stat().st_size / 1024.0
        self._set_status(f"Saved {res.intlib_path.name} ({size_kb:.1f} KB)", STATUS_OK)
        self._add_history_entry(res, size_kb)
        if self._preview_visible:
            self._open_3d_preview_for_intlib(res.intlib_path)

    def _add_history_entry(self, res: BuildResult, size_kb: float) -> None:
        if self.empty_hist_lbl.winfo_exists():
            self.empty_hist_lbl.destroy()

        bg = BG_PANEL if self._history_count % 2 == 0 else BG_ROW_ALT
        self._history_count += 1

        row = ctk.CTkFrame(self.history_scroll, fg_color=bg, corner_radius=1, height=28)
        row.pack(fill="x", padx=1, pady=1)
        row.grid_columnconfigure(1, weight=1)

        ts = datetime.now().strftime("%H:%M:%S")
        step_dot = "3D STEP" if res.has_3d_step else "2D"
        dot_color = STATUS_OK if res.has_3d_step else STATUS_WARN

        ctk.CTkLabel(
            row,
            text=ts,
            font=ctk.CTkFont(family=FONT_MONO, size=10),
            text_color=TEXT_MUTED,
            width=58,
            anchor="w",
        ).grid(row=0, column=0, padx=(8, 4), pady=4)

        name_lbl = ctk.CTkLabel(
            row,
            text=f"{res.intlib_path.name}   ({res.pin_count}P / {res.pad_count}Pad, {size_kb:.0f} KB)",
            font=ctk.CTkFont(family=FONT_UI, size=11),
            text_color=TEXT_PRIMARY,
            anchor="w",
            cursor="hand2",
        )
        name_lbl.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        name_lbl.bind("<Button-1>", lambda _e, p=res.intlib_path: self._open_3d_preview_for_intlib(p))

        ctk.CTkLabel(
            row,
            text=step_dot,
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            text_color=dot_color,
        ).grid(row=0, column=2, padx=6, pady=4)

        ctk.CTkButton(
            row,
            text="Show",
            width=50,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=ALTIUM_BLUE,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_UI, size=10, weight="bold"),
            command=lambda p=res.intlib_path: self._open_3d_preview_for_intlib(p),
        ).grid(row=0, column=3, padx=(2, 4), pady=3)

        ctk.CTkButton(
            row,
            text="File",
            width=42,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_INPUT,
            fg_color=BG_BTN,
            hover_color=BG_BTN_HOVER,
            text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(family=FONT_UI, size=10),
            command=lambda p=res.intlib_path: self._reveal_in_explorer(p),
        ).grid(row=0, column=4, padx=(0, 6), pady=3)

    # ------------------------------------------------------------------
    # Shared Helpers
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool, status_msg: str | None = None) -> None:
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        self.search_btn.configure(state=state)
        self.paste_btn.configure(state=state)
        if busy:
            self.download_btn.configure(state="disabled")
        if status_msg:
            self._set_status(status_msg, ALTIUM_BLUE_HOVER)

    def _set_status(self, msg: str, color: str = TEXT_SECONDARY) -> None:
        self.status_label.configure(text=msg, text_color=color)

    def _on_error(self, msg: str) -> None:
        self._set_busy(False)
        if self.current_bundle is not None:
            self.download_btn.configure(state="normal")
        self._set_status(f"Error: {msg}", STATUS_ERR)

    def _choose_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            initialdir=str(self.output_dir), title="Select Output Folder"
        )
        if chosen:
            self.output_dir = Path(chosen)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.dir_var.set(str(self.output_dir))
            self.cfg["output_dir"] = str(self.output_dir)
            _save_config(self.cfg)

    def _open_output_dir(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(str(self.output_dir))

    @staticmethod
    def _reveal_in_explorer(file_path: Path) -> None:
        if file_path.exists():
            subprocess.Popen(["explorer", f"/select,{file_path}"])
        else:
            os.startfile(str(file_path.parent))

    def _open_datasheet(self) -> None:
        if self.current_bundle and self.current_bundle.meta.datasheet_url:
            webbrowser.open(self.current_bundle.meta.datasheet_url)

    def _open_lcsc(self) -> None:
        if self.current_bundle and self.current_bundle.meta.lcsc_url:
            webbrowser.open(self.current_bundle.meta.lcsc_url)


def main() -> None:
    app = LcscToIntLibApp()
    app.mainloop()


if __name__ == "__main__":
    main()
