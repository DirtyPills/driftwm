#!/usr/bin/env python3
"""Top Autohide Recent Apps & Files Panel for driftwm.

Anchors to the top edge of the screen using GTK3 + GtkLayerShell.
Single horizontal row layout:
[Clipboard Slot] | [Recent Apps & Files Icons] ... [Hover Name Label]
"""

import ctypes
import ctypes.util
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, GtkLayerShell, Pango

HISTORY_FILE = Path.home() / ".config" / "driftwm" / "recent_history.json"
CLIPBOARD_CACHE_DIR = Path.home() / ".cache" / "driftwm" / "clipboard"
MAX_HISTORY = 25
COLLAPSED_HEIGHT = 6

CLIPBOARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)


class DesktopAppIndexer:
    """Indexes .desktop files to resolve app_id to application names and icons."""

    def __init__(self):
        self.apps = {}
        self._index_desktop_files()

    def _index_desktop_files(self):
        dirs = [
            Path("/usr/share/applications"),
            Path("/usr/local/share/applications"),
            Path.home() / ".local" / "share" / "applications",
            Path.home() / ".local" / "share" / "applications" / "flatpak",
            Path("/var/lib/flatpak/exports/share/applications"),
        ]
        for d in dirs:
            if not d.exists():
                continue
            for path in d.glob("*.desktop"):
                try:
                    self._parse_desktop_file(path)
                except Exception:
                    pass

    def _parse_desktop_file(self, path: Path):
        app_id = path.stem.lower()
        name = None
        icon = None
        exec_cmd = None
        nodisplay = False

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            in_main = False
            for line in f:
                line = line.strip()
                if line == "[Desktop Entry]":
                    in_main = True
                    continue
                elif line.startswith("[") and line.endswith("]"):
                    in_main = False
                    continue
                if not in_main or "=" not in line:
                    continue

                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()

                if key == "Name" and not name:
                    name = val
                elif key == "Icon" and not icon:
                    icon = val
                elif key == "Exec" and not exec_cmd:
                    exec_cmd = re.sub(r"%[fFuUdDnNiImM]", "", val).strip()
                elif key == "NoDisplay" and val.lower() == "true":
                    nodisplay = True

        if nodisplay or not name:
            return

        entry = {
            "name": name,
            "icon": icon or "application-x-executable",
            "exec": exec_cmd,
            "desktop_file": str(path),
        }
        self.apps[app_id] = entry

        # Also map WM_CLASS variants
        if exec_cmd:
            binary_name = exec_cmd.split()[0].split("/")[-1].lower()
            if binary_name not in self.apps:
                self.apps[binary_name] = entry

    def find_app(self, app_id: str):
        if not app_id:
            return None
        app_id_lower = app_id.lower()

        # Direct match
        if app_id_lower in self.apps:
            return self.apps[app_id_lower]

        # Partial match
        for key, info in self.apps.items():
            if app_id_lower in key or key in app_id_lower:
                return info

        return {
            "name": app_id.capitalize(),
            "icon": "application-x-executable",
            "exec": app_id,
            "desktop_file": None,
        }


class HistoryManager:
    """Manages persistent recent history for apps and files."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.history = []
        self.load()

    def load(self):
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
            except Exception as e:
                print(f"[recent_panel] Error loading history: {e}", file=sys.stderr)
                self.history = []

    def save(self):
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[recent_panel] Error saving history: {e}", file=sys.stderr)

    def add_item(self, item_dict: dict) -> bool:
        """Add or move item to the front (index 0). Returns True if history changed."""
        item_id = item_dict["id"]
        
        # Remove existing if present
        self.history = [x for x in self.history if x.get("id") != item_id]
        
        # Insert at top (most recent)
        item_dict["timestamp"] = time.time()
        self.history.insert(0, item_dict)

        # Trim
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[:MAX_HISTORY]

        self.save()
        return True


class RecentPanelWindow(Gtk.Window):
    """Top autohide panel GTK window."""

    def __init__(self, indexer: DesktopAppIndexer, history_mgr: HistoryManager):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.indexer = indexer
        self.history_mgr = history_mgr

        self._is_expanded = True
        self._hide_timer_id = None
        self._icon_theme = Gtk.IconTheme.get_default()
        self._last_img_hash = None
        self._last_txt_hash = None
        self.clipboard_state = {
            "type": None,
            "path": None,
            "name": "Буфер обмена пуст",
            "uri": None,
            "text": None,
        }

        # Setup Layer Shell
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)

        # Anchor to top, left, right
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.TOP, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.RIGHT, True)

        # Set 0 exclusive zone so windows go underneath or canvas remains untouched
        GtkLayerShell.set_exclusive_zone(self, 0)

        # CSS Styling
        self._load_css()

        # Window structure
        self.set_app_paintable(True)
        
        # Event box for catching mouse hover over panel
        self.event_box = Gtk.EventBox()
        self.event_box.set_visible_window(False)
        self.event_box.connect("enter-notify-event", self._on_enter_notify)
        self.event_box.connect("leave-notify-event", self._on_leave_notify)
        self.event_box.connect("motion-notify-event", self._on_motion_notify)
        self.add(self.event_box)

        # Main layout container
        self.main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.event_box.add(self.main_vbox)

        # Gtk.Revealer for smooth slide-down / slide-up animations
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_transition_duration(250)
        self.main_vbox.pack_start(self.revealer, True, True, 0)

        # 1. Expanded Panel Container (Single horizontal row!)
        self.panel_container = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.panel_container.get_style_context().add_class("top-panel-bar")
        self.revealer.add(self.panel_container)

        # Dedicated Leftmost Slot: Clipboard Content (Screenshot / Copy Data)
        self.clipboard_btn = Gtk.Button()
        self.clipboard_btn.get_style_context().add_class("clipboard-slot")
        self.clipboard_btn.connect("clicked", self._on_clipboard_btn_clicked)
        self.clipboard_btn.connect("enter-notify-event", lambda w, e: self._on_item_hover(self.clipboard_state["name"], "Буфер обмена"))
        self.clipboard_btn.connect("leave-notify-event", lambda w, e: self._on_item_unhover())
        
        # Setup DND for Clipboard Slot
        targets = [
            Gtk.TargetEntry.new("text/uri-list", 0, 0),
            Gtk.TargetEntry.new("image/png", 0, 1),
            Gtk.TargetEntry.new("text/plain", 0, 2),
        ]
        self.clipboard_btn.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, targets, Gdk.DragAction.COPY)
        self.clipboard_btn.connect("drag-data-get", self._on_clipboard_drag_data_get)

        self.panel_container.pack_start(self.clipboard_btn, False, False, 0)

        # Separator line
        self.sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        self.sep.get_style_context().add_class("panel-separator")
        self.panel_container.pack_start(self.sep, False, False, 2)

        # Direct horizontal icons box
        self.icons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.icons_box.get_style_context().add_class("icons-box")
        self.panel_container.pack_start(self.icons_box, False, False, 0)

        # Hover Name Label at right end of the SAME horizontal row!
        self.name_label = Gtk.Label()
        self.name_label.get_style_context().add_class("name-badge")
        self.name_label.set_use_markup(True)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.set_max_width_chars(30)
        self.panel_container.pack_end(self.name_label, False, False, 0)

        # 2. Collapsed Trigger Strip
        self.trigger_strip = Gtk.Box()
        self.trigger_strip.get_style_context().add_class("trigger-strip")
        self.trigger_strip.set_size_request(-1, COLLAPSED_HEIGHT)
        self.main_vbox.pack_end(self.trigger_strip, False, False, 0)

        # Load recent files from RecentManager & default desktop apps if history empty
        self.recent_mgr = Gtk.RecentManager.get_default()
        self.recent_mgr.connect("changed", self._on_recent_manager_changed)
        self._load_initial_items()

        # Build initial UI
        self.render_items()
        self.update_clipboard_slot()

        # Start EXPANDED for 5 seconds on launch so user sees the panel immediately!
        self._expand()
        GLib.timeout_add(5000, self._initial_autohide)

        # Connect driftwm IPC thread
        self.ipc_thread = threading.Thread(target=self._ipc_subscriber_loop, daemon=True)
        self.ipc_thread.start()

        # Connect Clipboard Monitor Thread
        self.clip_thread = threading.Thread(target=self._clipboard_monitor_loop, daemon=True)
        self.clip_thread.start()

        # Connect inotify Screenshot File Watcher Thread
        # xdg-desktop-portal-wlr saves screenshots to /tmp/out.png then deletes
        self.inotify_thread = threading.Thread(target=self._inotify_screenshot_watcher, daemon=True)
        self.inotify_thread.start()

    def _initial_autohide(self):
        self._collapse()
        return False

    def _load_initial_items(self):
        self._load_from_recent_manager()
        # If history is sparse, add common system applications
        if len(self.history_mgr.history) < 10:
            default_apps = [
                "alacritty", "firefox", "org.gnome.Nautilus", "thunar", "text-editor",
                "org.gnome.Terminal", "code", "google-chrome", "vlc", "gimp"
            ]
            for app_id in default_apps:
                if app_id.lower() in self.indexer.apps or any(app_id in k for k in self.indexer.apps):
                    self.add_app_event(app_id)

    def _load_css(self):
        provider = Gtk.CssProvider()
        css_file = Path(__file__).parent / "recent_panel.css"
        if css_file.exists():
            provider.load_from_path(str(css_file))
        else:
            provider.load_from_data(b"""
                .top-panel-bar {
                    background-color: rgba(0, 0, 0, 0.94);
                    border-bottom: 1px solid rgba(255, 255, 255, 0.12);
                    padding: 4px 10px;
                }
                .trigger-strip {
                    background-color: transparent;
                }
                .icons-box {
                    padding: 2px 4px;
                }
                .clipboard-slot {
                    background-color: rgba(255, 255, 255, 0.12);
                    border: 1px solid rgba(255, 255, 255, 0.3);
                    border-radius: 8px;
                    padding: 3px 6px;
                }
                .clipboard-slot:hover {
                    background-color: rgba(255, 255, 255, 0.25);
                    border-color: #80C0FF;
                }
                .panel-separator {
                    background-color: rgba(255, 255, 255, 0.2);
                    margin: 4px 2px;
                }
                .item-btn {
                    background: transparent;
                    border: none;
                    border-radius: 8px;
                    padding: 4px 8px;
                    margin: 0 2px;
                    transition: background 0.15s ease-in-out;
                }
                .item-btn:hover {
                    background-color: rgba(255, 255, 255, 0.25);
                }
                .item-btn:active {
                    background-color: rgba(255, 255, 255, 0.4);
                }
                .name-badge {
                    background-color: rgba(255, 255, 255, 0.12);
                    border: 1px solid rgba(255, 255, 255, 0.2);
                    border-radius: 6px;
                    padding: 3px 10px;
                    font-size: 12px;
                }
            """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _expand(self):
        if self._hide_timer_id:
            GLib.source_remove(self._hide_timer_id)
            self._hide_timer_id = None
        if not self._is_expanded:
            self.revealer.set_reveal_child(True)
            self._is_expanded = True

    def _collapse(self):
        if self._is_expanded:
            self.revealer.set_reveal_child(False)
            self._is_expanded = False

    def _on_enter_notify(self, widget, event):
        if self._hide_timer_id:
            GLib.source_remove(self._hide_timer_id)
            self._hide_timer_id = None
        self._expand()
        return False

    def _on_motion_notify(self, widget, event):
        if self._hide_timer_id:
            GLib.source_remove(self._hide_timer_id)
            self._hide_timer_id = None
        if not self._is_expanded:
            self._expand()
        return False

    def _on_leave_notify(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            if self._hide_timer_id is None:
                self._hide_timer_id = GLib.timeout_add(400, self._delayed_collapse)
        return False

    def _delayed_collapse(self):
        self._hide_timer_id = None
        self._collapse()
        return False

    def render_items(self):
        """Render all history items into the horizontal icon box."""
        for child in self.icons_box.get_children():
            self.icons_box.remove(child)

        for item in self.history_mgr.history[:MAX_HISTORY]:
            btn = self._create_item_button(item)
            self.icons_box.pack_start(btn, False, False, 0)

        self.icons_box.show_all()

    def update_clipboard_slot(self):
        """Update the leftmost dedicated clipboard button icon and state."""
        for child in self.clipboard_btn.get_children():
            self.clipboard_btn.remove(child)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        c_type = self.clipboard_state.get("type")

        if c_type == "image" and self.clipboard_state.get("path") and os.path.exists(self.clipboard_state["path"]):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(self.clipboard_state["path"], 32, 32, True)
                img = Gtk.Image.new_from_pixbuf(pixbuf)
            except Exception:
                img = Gtk.Image.new_from_icon_name("camera-photo", Gtk.IconSize.DND)
        elif c_type == "text":
            img = Gtk.Image.new_from_icon_name("text-x-generic", Gtk.IconSize.DND)
        else:
            img = Gtk.Image.new_from_icon_name("edit-paste", Gtk.IconSize.DND)

        box.pack_start(img, False, False, 0)
        self.clipboard_btn.add(box)
        self.clipboard_btn.set_tooltip_text(self.clipboard_state["name"])
        self.clipboard_btn.show_all()

    def _on_clipboard_btn_clicked(self, btn):
        c_path = self.clipboard_state.get("path")
        if c_path and os.path.exists(c_path):
            try:
                Gio.AppInfo.launch_default_for_uri(f"file://{c_path}")
            except Exception:
                os.system(f"xdg-open '{c_path}' &")

    def _on_clipboard_drag_data_get(self, widget, drag_context, selection_data, info, time_stamp):
        c_type = self.clipboard_state.get("type")
        c_path = self.clipboard_state.get("path")
        c_uri = self.clipboard_state.get("uri")
        target_name = selection_data.get_target().name()

        if target_name == "text/uri-list" and c_uri:
            selection_data.set_uris([c_uri])
        elif target_name == "image/png" and c_type == "image" and c_path and os.path.exists(c_path):
            try:
                with open(c_path, "rb") as f:
                    data = f.read()
                selection_data.set(selection_data.get_target(), 8, data)
            except Exception:
                pass
        elif target_name == "text/plain" and c_type == "text":
            text_val = self.clipboard_state.get("text") or ""
            selection_data.set_text(text_val, -1)

    def _inotify_screenshot_watcher(self):
        """Watch /tmp and ~/Pictures/Screenshots for screenshot files via Linux inotify.

        /tmp/out.png is created by xdg-desktop-portal-wlr as FULL SCREEN input
        for Flameshot's GUI — we must NOT grab it directly. Instead, when we see
        it, we know a Flameshot session started. We then poll xclip for the
        CROPPED selection that Flameshot writes to X11 clipboard.

        ~/Pictures/Screenshots/*.png are user-saved screenshots — grab directly.
        """
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        IN_CREATE = 0x00000100
        IN_CLOSE_WRITE = 0x00000008
        IN_MOVED_TO = 0x00000080
        IN_DELETE = 0x00000200
        EVENT_MASK = IN_CREATE | IN_CLOSE_WRITE | IN_MOVED_TO | IN_DELETE

        img_file = CLIPBOARD_CACHE_DIR / "clip_image.png"

        screenshots_dir = Path.home() / "Pictures" / "Screenshots"

        while True:
            try:
                fd = libc.inotify_init()
                if fd < 0:
                    time.sleep(5)
                    continue

                wd = libc.inotify_add_watch(fd, b"/tmp", EVENT_MASK)
                wd_map = {wd: Path("/tmp")}

                if screenshots_dir.exists():
                    wd2 = libc.inotify_add_watch(
                        fd, str(screenshots_dir).encode(), EVENT_MASK
                    )
                    if wd2 >= 0:
                        wd_map[wd2] = screenshots_dir

                buf_size = 4096
                while True:
                    data = os.read(fd, buf_size)
                    if not data:
                        break
                    offset = 0
                    while offset < len(data):
                        header = data[offset : offset + 16]
                        if len(header) < 16:
                            break
                        wd_ev, mask, cookie, name_len = struct.unpack("iIII", header)
                        name_bytes = data[
                            offset + 16 : offset + 16 + name_len
                        ].rstrip(b"\x00")
                        offset += 16 + name_len

                        ev_dir = wd_map.get(wd_ev, Path("/tmp"))
                        fname = name_bytes.decode("utf-8", errors="ignore")

                        if ev_dir == Path("/tmp") and fname == "out.png":
                            # Portal screenshot detected — Flameshot session active.
                            # /tmp/out.png is the FULL SCREEN input, NOT the selection.
                            # Wait for out.png to be DELETED (Flameshot read it),
                            # then poll xclip for the cropped image.
                            if mask & IN_DELETE:
                                # Flameshot finished reading — now poll for cropped selection
                                threading.Thread(
                                    target=self._poll_xclip_for_image_after_flameshot,
                                    daemon=True,
                                ).start()
                            continue

                        # Screenshots dir: grab new PNGs directly
                        if ev_dir == screenshots_dir and fname.endswith(".png"):
                            src = ev_dir / fname
                            for _ in range(20):
                                if src.exists() and src.stat().st_size > 100:
                                    break
                                time.sleep(0.1)
                            self._grab_screenshot_file(src)

                os.close(fd)
            except Exception:
                pass
            time.sleep(2)

    def _poll_xclip_for_image_after_flameshot(self):
        """After Flameshot reads /tmp/out.png, poll xclip for the cropped selection.

        Flameshot writes the cropped image to X11 clipboard after the user
        finishes selection. We poll for up to 30 seconds.
        """
        img_file = CLIPBOARD_CACHE_DIR / "clip_image.png"

        for _ in range(60):  # 30 seconds (0.5s intervals)
            time.sleep(0.5)
            img_bytes = None

            # Check xclip for image (X11 clipboard via xwayland-satellite)
            for mime in ["image/png", "image/bmp", "image/jpeg"]:
                try:
                    res = subprocess.run(
                        ["xclip", "-selection", "clipboard", "-t", mime, "-o"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2,
                    )
                    if res.returncode == 0 and res.stdout and len(res.stdout) > 100:
                        img_bytes = res.stdout
                        break
                except Exception:
                    pass

            # Also check wl-paste
            if not img_bytes:
                for mime in ["image/png", "image/bmp", "image/jpeg"]:
                    try:
                        res = subprocess.run(
                            ["wl-paste", "-t", mime],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2,
                        )
                        if res.returncode == 0 and res.stdout and len(res.stdout) > 100:
                            img_bytes = res.stdout
                            break
                    except Exception:
                        pass

            if img_bytes and self._is_valid_image_data(img_bytes):
                img_hash = hash(img_bytes)
                if img_hash != self._last_img_hash:
                    self._last_img_hash = img_hash
                    self._save_image_atomic(img_bytes, img_file)
                    self.clipboard_state = {
                        "type": "image",
                        "path": str(img_file),
                        "name": "Скриншот (выделенная область)",
                        "uri": f"file://{img_file}",
                        "text": None,
                    }
                    GLib.idle_add(self.update_clipboard_slot)
                return  # Found it, stop polling

    def _grab_screenshot_file(self, src: Path):
        """Grab a screenshot file from disk, validate, cache, and update clipboard slot."""
        img_file = CLIPBOARD_CACHE_DIR / "clip_image.png"

        if not src.exists():
            return

        try:
            with open(src, "rb") as f:
                shot_bytes = f.read()
        except Exception:
            return

        if not shot_bytes or not self._is_valid_image_data(shot_bytes):
            return

        shot_hash = hash(shot_bytes)
        if shot_hash == self._last_img_hash:
            return

        self._last_img_hash = shot_hash
        self._save_image_atomic(shot_bytes, img_file)

        # Also push into Wayland clipboard
        try:
            subprocess.Popen(
                ["wl-copy", "-t", "image/png"],
                stdin=subprocess.PIPE,
            ).communicate(input=shot_bytes, timeout=3)
        except Exception:
            pass

        self.clipboard_state = {
            "type": "image",
            "path": str(img_file),
            "name": f"Скриншот ({src.name})",
            "uri": f"file://{img_file}",
            "text": None,
        }
        GLib.idle_add(self.update_clipboard_slot)

    def _is_valid_image_data(self, data: bytes) -> bool:
        if not data or len(data) < 100:
            return False
        if data.startswith(b"\x89PNG"):
            return b"IEND" in data[-30:]
        elif data.startswith(b"\xff\xd8"):
            return data.endswith(b"\xff\xd9") or b"\xff\xd9" in data[-10:]
        elif data.startswith(b"BM"):
            return len(data) > 500
        return len(data) > 500

    def _save_image_atomic(self, img_bytes: bytes, target_path: Path):
        tmp_path = target_path.with_suffix(".tmp")
        with open(tmp_path, "wb") as f:
            f.write(img_bytes)
        os.replace(tmp_path, target_path)

    def _clipboard_monitor_loop(self):
        """Continuously monitors system clipboard & screenshot directories for new screenshots or text."""
        img_file = CLIPBOARD_CACHE_DIR / "clip_image.png"
        txt_file = CLIPBOARD_CACHE_DIR / "clip_text.txt"

        while True:
            try:
                # 1. Fetch types from wl-paste AND xclip
                types = []
                try:
                    res = subprocess.run(["wl-paste", "--list-types"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
                    if res.returncode == 0:
                        types.extend([t.strip() for t in res.stdout.splitlines()])
                except Exception:
                    pass

                try:
                    res = subprocess.run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
                    if res.returncode == 0:
                        types.extend([t.strip() for t in res.stdout.splitlines()])
                except Exception:
                    pass

                has_image_type = any("image" in t.lower() or "png" in t.lower() or "bmp" in t.lower() for t in types)

                if has_image_type:
                    img_bytes = None
                    for img_type in ["image/png", "image/jpeg", "image/bmp"]:
                        try:
                            res = subprocess.run(["wl-paste", "-t", img_type], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                            if res.returncode == 0 and res.stdout and len(res.stdout) > 10:
                                img_bytes = res.stdout
                                break
                        except Exception:
                            pass

                    if not img_bytes or not self._is_valid_image_data(img_bytes):
                        try:
                            res = subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                            if res.returncode == 0 and res.stdout and len(res.stdout) > 10:
                                img_bytes = res.stdout
                        except Exception:
                            pass

                    if img_bytes and self._is_valid_image_data(img_bytes):
                        img_hash = hash(img_bytes)
                        if img_hash != self._last_img_hash:
                            self._last_img_hash = img_hash
                            self._save_image_atomic(img_bytes, img_file)

                            self.clipboard_state = {
                                "type": "image",
                                "path": str(img_file),
                                "name": "Скриншот / Изображение из буфера",
                                "uri": f"file://{img_file}",
                                "text": None,
                            }
                            GLib.idle_add(self.update_clipboard_slot)
                        time.sleep(0.5)
                        continue

                # 2. Text check (ONLY if no image type was reported)
                has_text_type = any("text" in t.lower() or "utf8" in t.lower() or "string" in t.lower() for t in types)
                if has_text_type:
                    txt_val = None
                    try:
                        res = subprocess.run(["wl-paste", "-t", "text/plain"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
                        if res.returncode == 0 and res.stdout and res.stdout.strip():
                            txt_val = res.stdout.strip()
                    except Exception:
                        pass

                    if not txt_val:
                        try:
                            res = subprocess.run(["xclip", "-selection", "clipboard", "-o"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
                            if res.returncode == 0 and res.stdout and res.stdout.strip():
                                txt_val = res.stdout.strip()
                        except Exception:
                            pass

                    if txt_val:
                        txt_hash = hash(txt_val)
                        if txt_hash != self._last_txt_hash:
                            self._last_txt_hash = txt_hash
                            with open(txt_file, "w", encoding="utf-8") as f:
                                f.write(txt_val)

                            snippet = (txt_val[:22] + "...") if len(txt_val) > 25 else txt_val
                            self.clipboard_state = {
                                "type": "text",
                                "path": str(txt_file),
                                "name": f"Текст из буфера: {snippet}",
                                "uri": f"file://{txt_file}",
                                "text": txt_val,
                            }
                            GLib.idle_add(self.update_clipboard_slot)
            except Exception:
                pass
            time.sleep(0.5)

    def _create_item_button(self, item: dict) -> Gtk.Button:
        btn = Gtk.Button()
        btn.get_style_context().add_class("item-btn")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        
        icon_name = item.get("icon_name", "application-x-executable")
        img = self._get_icon_image(icon_name)
        box.pack_start(img, False, False, 0)

        btn.add(box)

        item_name = item.get("name", "Unknown")
        item_type_str = "Приложение" if item.get("type") == "app" else "Файл"

        # Standard Tooltip
        btn.set_tooltip_text(f"{item_name} ({item_type_str})")

        # Instant Hover Label Update
        btn.connect("enter-notify-event", lambda w, e, n=item_name, t=item_type_str: self._on_item_hover(n, t))
        btn.connect("leave-notify-event", lambda w, e: self._on_item_unhover())

        # Click handler
        btn.connect("clicked", lambda b, i=item: self._on_item_clicked(i))

        # Drag and Drop (DND) Setup
        target_entry = Gtk.TargetEntry.new("text/uri-list", 0, 0)
        btn.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [target_entry], Gdk.DragAction.COPY)
        btn.connect("drag-data-get", lambda w, ctx, sel, info, time, i=item: self._on_drag_data_get(sel, i))

        return btn

    def _on_item_hover(self, name: str, item_type: str):
        type_color = "#80C0FF" if item_type in ("Приложение", "Буфер обмена") else "#FFD080"
        markup = f"<b>{GLib.markup_escape_text(name)}</b>  <span foreground='{type_color}'>[{item_type}]</span>"
        self.name_label.set_markup(markup)
        self.name_label.show()

    def _on_item_unhover(self):
        self.name_label.hide()

    def _get_icon_image(self, icon_name: str) -> Gtk.Image:
        try:
            if "/" in icon_name and os.path.exists(icon_name):
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_name, 32, 32, True)
                return Gtk.Image.new_from_pixbuf(pixbuf)

            if self._icon_theme.has_icon(icon_name):
                return Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.DND)
        except Exception:
            pass
        return Gtk.Image.new_from_icon_name("application-x-executable", Gtk.IconSize.DND)

    def _on_item_clicked(self, item: dict):
        item_type = item.get("type")
        if item_type == "app":
            app_id = item.get("app_id")
            # Try bringing window to focus via driftwm IPC socket
            focused = self._focus_driftwm_window(app_id)
            if not focused:
                # Launch application
                exec_cmd = item.get("exec") or app_id
                if exec_cmd:
                    try:
                        Gio.Subprocess.new(exec_cmd.split(), Gio.SubprocessFlags.NONE)
                    except Exception:
                        os.system(f"{exec_cmd} &")
        elif item_type == "file":
            uri = item.get("uri_or_cmd")
            if uri:
                try:
                    Gio.AppInfo.launch_default_for_uri(uri)
                except Exception:
                    os.system(f"xdg-open '{uri}' &")

    def _on_drag_data_get(self, selection_data, item: dict):
        uri = item.get("uri_or_cmd")
        if item.get("type") == "app" and item.get("desktop_file"):
            uri = f"file://{item.get('desktop_file')}"
        if uri:
            selection_data.set_uris([uri])

    def _focus_driftwm_window(self, app_id: str) -> bool:
        socket_path = self._get_driftwm_socket()
        if not socket_path or not os.path.exists(socket_path):
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(socket_path)
            req = json.dumps({"Focus": app_id}) + "\n"
            sock.sendall(req.encode("utf-8"))
            resp = sock.recv(1024).decode("utf-8")
            sock.close()
            return '"Ok"' in resp
        except Exception:
            return False

    def _get_driftwm_socket(self) -> str:
        if os.environ.get("DRIFTWM_SOCKET"):
            return os.environ.get("DRIFTWM_SOCKET")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        return os.path.join(runtime_dir, "driftwm", f"ipc-{display}.sock")

    def add_app_event(self, app_id: str, title: str = ""):
        if not app_id or app_id.startswith("drift-") or app_id in ("panel", "waybar"):
            return

        app_info = self.indexer.find_app(app_id)
        item = {
            "id": f"app:{app_id.lower()}",
            "type": "app",
            "name": app_info.get("name", app_id.capitalize()),
            "app_id": app_id,
            "exec": app_info.get("exec", app_id),
            "desktop_file": app_info.get("desktop_file"),
            "icon_name": app_info.get("icon", "application-x-executable"),
            "uri_or_cmd": app_id,
        }
        if self.history_mgr.add_item(item):
            GLib.idle_add(self.render_items)

    def add_file_event(self, uri: str, name: str = None, mime_type: str = None):
        if not uri or not uri.startswith("file://"):
            return

        filepath = urllib.parse.unquote(urllib.parse.urlparse(uri).path)
        if not os.path.exists(filepath):
            return

        filename = name or os.path.basename(filepath)
        if not filename:
            return

        # Determine icon
        gfile = Gio.File.new_for_uri(uri)
        icon_name = "text-x-generic"
        try:
            info = gfile.query_info("standard::icon", Gio.FileQueryInfoFlags.NONE, None)
            gicon = info.get_icon()
            if gicon and hasattr(gicon, "get_names"):
                names = gicon.get_names()
                if names:
                    icon_name = names[0]
        except Exception:
            pass

        item = {
            "id": uri,
            "type": "file",
            "name": filename,
            "path": filepath,
            "icon_name": icon_name,
            "uri_or_cmd": uri,
        }
        if self.history_mgr.add_item(item):
            GLib.idle_add(self.render_items)

    def _on_recent_manager_changed(self, rm):
        self._load_from_recent_manager()

    def _load_from_recent_manager(self):
        items = self.recent_mgr.get_items()
        # Sort items by visited/modified time
        items = sorted(items, key=lambda x: x.get_visited(), reverse=True)
        for r_item in items[:25]:
            uri = r_item.get_uri()
            if uri and uri.startswith("file://"):
                self.add_file_event(uri, r_item.get_display_name(), r_item.get_mime_type())

    def _ipc_subscriber_loop(self):
        """Subscribe to driftwm IPC socket to observe NEW window launches (not focus changes)."""
        known_window_ids = set()
        initialized = False

        while True:
            socket_path = self._get_driftwm_socket()
            if not socket_path or not os.path.exists(socket_path):
                time.sleep(2)
                continue

            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.connect(socket_path)
                sock.sendall(b'"Subscribe"\n')

                f = sock.makefile("r", encoding="utf-8")
                for line in f:
                    if not line:
                        break
                    try:
                        data = json.loads(line)
                        state = data.get("State") or (data.get("Ok", {}).get("State"))
                        if state and "windows" in state:
                            current_ids = set()
                            for win in state["windows"]:
                                win_id = win.get("id")
                                app_id = win.get("app_id")
                                if win_id is not None:
                                    current_ids.add(win_id)
                                    # Trigger ONLY for brand new window launches
                                    if initialized and win_id not in known_window_ids and app_id:
                                        self.add_app_event(app_id, win.get("title", ""))

                            known_window_ids = current_ids
                            initialized = True
                    except Exception:
                        pass
                sock.close()
            except Exception:
                pass
            time.sleep(2)


def main():
    indexer = DesktopAppIndexer()
    history_mgr = HistoryManager(HISTORY_FILE)

    win = RecentPanelWindow(indexer, history_mgr)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()

    Gtk.main()


if __name__ == "__main__":
    main()
