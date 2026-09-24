#!/usr/bin/env python3
"""
Mini Panel für xfce4 - überarbeitete Version

Änderungen gegenüber der Vorversion (Zusammenfassung steht am Ende des Chats):
  - Compositor-Erkennung: Screen-Corners / Rundungen / Animationen werden nur
    aktiviert, wenn tatsächlich ein Compositor läuft -> kein Blackscreen mehr.
  - Compositor lässt sich direkt aus dem Panel an-/ausschalten (xfconf-query).
  - Popups blitzen nicht mehr oben links auf (Offscreen-Trick vor dem Positionieren).
  - Whisker-Menü (und alle einfachen Popups) schließen jetzt zuverlässig über
    einen Pointer-Grab beim Klick außerhalb, unabhängig von WM-Fokus-Bugs.
  - Sanftes Fade-In für Popups, wenn ein Compositor aktiv ist.
  - Eigene Taskleiste mit Rechtsklick-Kontextmenü: Aktivieren/Minimieren/
    Maximieren/Schließen und "Als Widget anpinnen".
  - Angepinnte Apps erscheinen als eigene Buttons im Panel (mit Icon), lassen
    sich per Rechtsklick wieder entfernen.
  - Neues Widget: Screenshot-Button.
  - Kleinere Fixes: Akku-Ladezustand, robustere Konfig-Migration.
"""
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HOTKEY_SOCK_PATH = os.path.expanduser("~/.cache/mini_panel_hotkeys.sock")


def _dispatch_hotkey_action_and_exit():
    """Wird von den XFCE-Tastenkürzeln aufgerufen: 'mini_panel.py --action X'.
    Schickt die Aktion an die bereits laufende Panel-Instanz über einen
    Unix-Socket und beendet sich sofort - ganz ohne GTK zu importieren, damit
    der Tastendruck ohne spürbare Verzögerung ankommt."""
    if len(sys.argv) >= 3 and sys.argv[1] == "--action":
        action = sys.argv[2]
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.5)
            sock.connect(HOTKEY_SOCK_PATH)
            sock.sendall(action.encode("utf-8"))
            sock.close()
        except Exception:
            pass
        sys.exit(0)


_dispatch_hotkey_action_and_exit()

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")

HAS_WNCK = False
try:
    gi.require_version("Wnck", "3.0")
    from gi.repository import Wnck

    HAS_WNCK = True
except Exception:
    HAS_WNCK = False

from gi.repository import Gdk, GdkX11, GLib, Gtk, GdkPixbuf, Pango, Gio

HAS_XLIB = False
try:
    from Xlib import X
    from Xlib.display import Display as XlibDisplay
    from Xlib.protocol import event as xlib_event

    HAS_XLIB = True
except Exception:
    HAS_XLIB = False
import psutil

CONFIG_FILE = os.path.expanduser("~/.config/mini_panel_config.json")
NOTES_FILE = os.path.expanduser("~/.config/mini_panel_notes.txt")

DEFAULT_CONFIG = {
    "position": "top",
    "panel_size": 36,
    "panel_radius": 0,
    "screen_corners": False,
    "corner_radius": 16,
    "animations": True,
    "animation_duration_ms": 200,
    "popup_grow_effect": True,
    "panel_pulse_effect": True,
    "panel_opacity": 95,
    "popup_radius": 14,
    "popup_ring_thickness": 10,
    "bg_color": "rgba(30, 30, 46, 1.00)",
    "fg_color": "#cdd6f4",
    "accent_color": "#89b4fa",
    "pinned_apps": [],
    "hotkeys": {},
    "taskbar_options": {
        "show_labels": True,
        "show_other_workspaces": True,
        "click_action": "switch",
    },
    "widget_display": {
        "whisker": "both",
        "pinned": "both",
        "notes": "both",
        "screenshot": "both",
        "settings": "both",
        "volume": "text",
        "brightness": "text",
        "battery": "text",
        "sysmon": "text",
        "disk": "text",
        "net": "text",
        "updates": "text",
        "weather": "text",
        "timer": "text",
        "clock": "text",
        "media": "both",
        "notifications": "both",
        "network_manager": "icon",
        "bluetooth": "icon",
    },
    "widget_sections": {
        "left": ["whisker", "pinned", "workspaces", "taskbar", "media"],
        "center": ["sysmon", "disk", "net", "brightness", "volume", "battery", "network_manager", "bluetooth"],
        "right": ["notes", "timer", "screenshot", "updates", "weather", "systray", "notifications", "clock", "settings"],
    },
    "widgets": {
        "whisker": True,
        "pinned": True,
        "workspaces": True,
        "taskbar": True,
        "media": True,
        "sysmon": True,
        "disk": True,
        "net": True,
        "brightness": True,
        "volume": True,
        "battery": True,
        "notes": True,
        "timer": True,
        "screenshot": True,
        "updates": True,
        "weather": False,
        "systray": False,
        "notifications": True,
        "network_manager": False,
        "bluetooth": False,
        "clock": True,
        "settings": True,
    },
    "notifications_enabled": True,
    "notification_settings": {
        "banner_position": "top-right",
        "banner_duration_ms": 5000,
        "max_history": 50,
        "do_not_disturb": False,
    },
    "systray_icon_size": 20,
}

ALL_WIDGET_KEYS = [
    k for section in DEFAULT_CONFIG["widget_sections"].values() for k in section
]


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def get_primary_geometry():
    """Holt die Monitor-Geometrie ohne Int-Attributfehler."""
    display = Gdk.Display.get_default()
    if display:
        monitor = display.get_primary_monitor()
        if not monitor and display.get_n_monitors() > 0:
            monitor = display.get_monitor(0)
        if monitor:
            return monitor.get_geometry()
    screen = Gdk.Screen.get_default()
    return Gdk.Rectangle(x=0, y=0, width=screen.get_width(), height=screen.get_height())


def is_composited():
    """Prüft, ob aktuell ein Compositor läuft (X11/GDK-Sicht)."""
    screen = Gdk.Screen.get_default()
    try:
        return bool(screen and screen.is_composited())
    except Exception:
        return False


def get_compositor_enabled():
    """Fragt den xfwm4-Compositor-Status per xfconf ab (fällt auf is_composited zurück)."""
    try:
        out = subprocess.run(
            ["xfconf-query", "-c", "xfwm4", "-p", "/general/use_compositing"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip().lower() == "true"
    except Exception:
        pass
    return is_composited()


def set_compositor_enabled(enabled):
    """Schaltet den xfwm4-Compositor per xfconf um."""
    try:
        subprocess.run(
            ["xfconf-query", "-c", "xfwm4", "-p", "/general/use_compositing",
             "-s", "true" if enabled else "false"],
            check=False, timeout=2,
        )
        return True
    except Exception:
        return False


def parse_rgba_str(color_str):
    rgba = Gdk.RGBA()
    if not rgba.parse(color_str):
        rgba.parse("#1e1e2e")
    return rgba


def rgba_to_css_str(rgba):
    return f"rgba({int(rgba.red * 255)}, {int(rgba.green * 255)}, {int(rgba.blue * 255)}, {rgba.alpha:.2f})"


def lighten_css_rgb(r, g, b, alpha, amount=0.10):
    """Blendet r,g,b (0-255) leicht Richtung Weiß, für einen dezent sichtbaren
    Ring statt eines komplett unsichtbaren (exakt gleichfarbigen) Randes."""
    lr = int(r + (255 - r) * amount)
    lg = int(g + (255 - g) * amount)
    lb = int(b + (255 - b) * amount)
    return f"rgba({lr}, {lg}, {lb}, {alpha:.2f})"


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                merged = {**DEFAULT_CONFIG, **data}
                merged["widgets"] = {
                    **DEFAULT_CONFIG["widgets"],
                    **data.get("widgets", {}),
                }

                if "widget_sections" in data:
                    sections = {
                        sect: list(data["widget_sections"].get(sect, []))
                        for sect in ("left", "center", "right")
                    }
                elif "widget_order" in data:
                    # Alte Einzel-Reihenfolge (vor den 3 Sektionen) migrieren:
                    # bekannte Positionen aus dem Default beibehalten, Rest -> Mitte.
                    sections = {"left": [], "center": [], "right": []}
                    for key in data["widget_order"]:
                        target = "center"
                        for sect, keys in DEFAULT_CONFIG["widget_sections"].items():
                            if key in keys:
                                target = sect
                                break
                        sections[target].append(key)
                else:
                    sections = {
                        sect: list(keys) for sect, keys in DEFAULT_CONFIG["widget_sections"].items()
                    }

                # Neue Widgets aus Updates in eine Sektion einsortieren, ohne die
                # persönliche Anordnung zu verwerfen.
                placed = {k for keys in sections.values() for k in keys}
                for sect, keys in DEFAULT_CONFIG["widget_sections"].items():
                    for key in keys:
                        if key not in placed:
                            sections[sect].append(key)
                            placed.add(key)
                merged["widget_sections"] = sections
                merged.pop("widget_order", None)

                merged["pinned_apps"] = data.get("pinned_apps", [])
                merged["hotkeys"] = dict(data.get("hotkeys", {}))
                merged["notification_settings"] = {
                    **DEFAULT_CONFIG["notification_settings"],
                    **data.get("notification_settings", {}),
                }
                merged["taskbar_options"] = {
                    **DEFAULT_CONFIG["taskbar_options"],
                    **data.get("taskbar_options", {}),
                }
                merged["widget_display"] = {
                    **DEFAULT_CONFIG["widget_display"],
                    **data.get("widget_display", {}),
                }
                return merged
        except Exception:
            pass
    return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)


def get_brightness_percent():
    tool = shutil.which("brightnessctl")
    if not tool:
        return None
    try:
        cur = subprocess.run([tool, "get"], capture_output=True, text=True, timeout=2)
        maxv = subprocess.run([tool, "max"], capture_output=True, text=True, timeout=2)
        cur_v = int(cur.stdout.strip())
        max_v = int(maxv.stdout.strip())
        if max_v <= 0:
            return None
        return int(cur_v / max_v * 100)
    except Exception:
        return None


def set_brightness_percent(percent):
    tool = shutil.which("brightnessctl")
    if not tool:
        return False
    try:
        subprocess.run([tool, "set", f"{max(1, min(100, int(percent)))}%"], capture_output=True, timeout=2)
        return True
    except Exception:
        return False


def get_updates_count():
    """Anzahl verfügbarer apt-Updates. Kann ein paar Sekunden dauern -> in Thread aufrufen."""
    if not shutil.which("apt"):
        return None
    try:
        out = subprocess.run(
            ["apt", "list", "--upgradable"], capture_output=True, text=True, timeout=15
        )
        lines = [l for l in out.stdout.splitlines() if l and not l.startswith("Listing...")]
        return len(lines)
    except Exception:
        return None


def get_media_status():
    """Liefert (status, titel) via playerctl, oder None wenn kein Player laeuft."""
    if not shutil.which("playerctl"):
        return None
    try:
        status = subprocess.run(
            ["playerctl", "status"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        if not status:
            return None
        title = subprocess.run(
            ["playerctl", "metadata", "--format", "{{artist}} - {{title}}"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        return status, (title or "...")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Netzwerk-Verwaltung (nmcli) - Ersatz für nm-applet
# ---------------------------------------------------------------------------

def nmcli_available():
    return shutil.which("nmcli") is not None


def get_network_status():
    """Kurzstatus für den Panel-Button: (Anzeigetext, Icon-Name) oder None."""
    if not nmcli_available():
        return None
    try:
        out = subprocess.run(
            ["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device", "status"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        active = []
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[1] == "connected":
                active.append((parts[0], parts[2]))
        if not active:
            return ("Offline", "network-wireless-disconnected-symbolic")
        for typ, name in active:
            if typ == "wifi":
                return (name, "network-wireless-symbolic")
        typ, name = active[0]
        icon = "network-wired-symbolic" if typ == "ethernet" else "network-wireless-symbolic"
        return (name, icon)
    except Exception:
        return None


def get_wifi_radio_enabled():
    try:
        out = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True, timeout=3).stdout.strip()
        return out == "enabled"
    except Exception:
        return True


def set_wifi_radio(enabled):
    try:
        subprocess.run(["nmcli", "radio", "wifi", "on" if enabled else "off"], capture_output=True, timeout=5)
    except Exception:
        pass


def list_wifi_networks(rescan=True):
    if not nmcli_available():
        return []
    try:
        args = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "device", "wifi", "list"]
        if rescan:
            args += ["--rescan", "yes"]
        out = subprocess.run(args, capture_output=True, text=True, timeout=12).stdout
        nets, seen = [], set()
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            ssid, signal, security, in_use = parts[0], parts[1], parts[2], parts[3]
            if not ssid or ssid in seen:
                continue
            seen.add(ssid)
            nets.append({
                "ssid": ssid,
                "signal": int(signal) if signal.isdigit() else 0,
                "secure": bool(security.strip()),
                "active": in_use.strip() == "*",
            })
        nets.sort(key=lambda n: (-n["active"], -n["signal"]))
        return nets
    except Exception:
        return []


def nmcli_connect_wifi(ssid, password=None):
    args = ["nmcli", "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=25)
        return res.returncode == 0, (res.stderr or res.stdout).strip()
    except Exception as e:
        return False, str(e)


def nmcli_disconnect(ssid):
    try:
        res = subprocess.run(["nmcli", "connection", "down", ssid], capture_output=True, text=True, timeout=10)
        return res.returncode == 0, (res.stderr or res.stdout).strip()
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Bluetooth-Verwaltung (bluetoothctl) - Ersatz für blueman-applet
# ---------------------------------------------------------------------------

def bluetoothctl_available():
    return shutil.which("bluetoothctl") is not None


def _bt_run(args, timeout=8):
    try:
        return subprocess.run(["bluetoothctl"] + args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def get_bluetooth_powered():
    if not bluetoothctl_available():
        return None
    out = _bt_run(["show"])
    for line in out.splitlines():
        if "Powered:" in line:
            return "yes" in line.lower()
    return None


def set_bluetooth_power(enabled):
    try:
        subprocess.run(["bluetoothctl", "power", "on" if enabled else "off"], capture_output=True, timeout=6)
    except Exception:
        pass


def list_bt_devices():
    out = _bt_run(["devices"])
    devices = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 3 and parts[0] == "Device":
            mac, name = parts[1], parts[2]
            info = _bt_run(["info", mac])
            devices.append({
                "mac": mac,
                "name": name,
                "connected": "Connected: yes" in info,
                "paired": "Paired: yes" in info,
            })
    return devices


def bt_connect(mac):
    subprocess.run(["bluetoothctl", "connect", mac], capture_output=True, timeout=15)


def bt_disconnect(mac):
    subprocess.run(["bluetoothctl", "disconnect", mac], capture_output=True, timeout=10)


def bt_scan(timeout_s=8):
    try:
        subprocess.run(
            ["bluetoothctl", "--timeout", str(timeout_s), "scan", "on"],
            capture_output=True, timeout=timeout_s + 5,
        )
    except Exception:
        pass


def bt_pair(mac):
    subprocess.run(["bluetoothctl", "pair", mac], capture_output=True, timeout=15)
    subprocess.run(["bluetoothctl", "trust", mac], capture_output=True, timeout=10)


HOTKEY_ACTIONS = [
    ("toggle-whisker", "Anwendungsmenü öffnen/schließen"),
    ("toggle-volume", "Lautstärke-Regler öffnen/schließen"),
    ("volume-up", "Lautstärke +5%"),
    ("volume-down", "Lautstärke -5%"),
    ("mute-toggle", "Stummschalten umschalten"),
    ("toggle-brightness", "Helligkeit-Regler öffnen/schließen"),
    ("brightness-up", "Helligkeit +5%"),
    ("brightness-down", "Helligkeit -5%"),
    ("toggle-notes", "Notizblock öffnen/schließen"),
    ("toggle-timer", "Timer öffnen/schließen"),
    ("toggle-calendar", "Kalender öffnen/schließen"),
    ("toggle-settings", "Einstellungen öffnen/schließen"),
    ("take-screenshot", "Screenshot aufnehmen"),
    ("media-play-pause", "Medien: Play/Pause"),
    ("media-next", "Medien: Nächster Titel"),
    ("media-prev", "Medien: Vorheriger Titel"),
    ("restart-panel", "Panel neu starten"),
    ("toggle-notifications", "Benachrichtigungscenter öffnen/schließen"),
    ("toggle-dnd", "Nicht stören umschalten"),
    ("toggle-network", "Netzwerk-Menü öffnen/schließen"),
    ("toggle-wifi", "WLAN an/aus"),
    ("toggle-bluetooth-menu", "Bluetooth-Menü öffnen/schließen"),
    ("toggle-bluetooth-power", "Bluetooth an/aus"),
]


def get_hotkey_command(action_id):
    """Der Shell-Befehl, den XFCE beim Drücken der Tastenkombination ausführt."""
    script_path = os.path.abspath(sys.argv[0])
    python_exe = sys.executable or "python3"
    return f"{python_exe} {script_path} --action {action_id}"


def set_xfce_hotkey(accel, command):
    prop = f"/commands/custom/{accel}"
    subprocess.run(
        ["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", prop, "-n", "-t", "string", "-s", command],
        capture_output=True,
    )


def remove_xfce_hotkey(accel):
    prop = f"/commands/custom/{accel}"
    subprocess.run(
        ["xfconf-query", "-c", "xfce4-keyboard-shortcuts", "-p", prop, "-r"],
        capture_output=True,
    )


WIDGET_ICON_NAMES = {
    "whisker": ["start-here", "distributor-logo", "applications-system", "view-app-grid-symbolic"],
    "notes": ["accessories-text-editor", "text-editor", "document-edit"],
    "screenshot": ["applets-screenshooter", "camera-photo", "image-x-generic"],
    "settings": ["preferences-system", "emblem-system", "applications-system"],
    "volume": ["audio-volume-high", "audio-volume-medium", "multimedia-volume-control"],
    "brightness": ["display-brightness", "video-display", "preferences-desktop-display"],
    "battery": ["battery", "battery-good-symbolic", "battery-full"],
    "sysmon": ["utilities-system-monitor", "org.gnome.SystemMonitor"],
    "disk": ["drive-harddisk", "harddisk", "drive-harddisk-symbolic"],
    "net": ["network-wired", "network-transmit-receive", "network-wired-symbolic"],
    "updates": ["software-update-available", "system-software-update"],
    "weather": ["weather-few-clouds", "weather-clear", "weather-overcast"],
    "timer": ["chronometer", "appointment-soon", "alarm-symbolic", "preferences-system-time"],
    "clock": ["office-calendar", "x-office-calendar", "preferences-system-time"],
    "media": ["multimedia-player", "audio-x-generic", "applications-multimedia"],
    "notifications": ["preferences-desktop-notification", "notification-symbolic", "mail-message-new"],
    "network_manager": ["network-wireless-symbolic", "network-wired-symbolic", "network-transmit-receive"],
    "bluetooth": ["bluetooth-active-symbolic", "bluetooth-symbolic", "bluetooth"],
}


def make_labeled_icon_button(icon_names, label_text, show_label=True):
    """Baut einen Button mit echtem Theme-Icon (kein Emoji-Font-Glyph, der auf
    manchen Systemen nicht rendert) plus optionalem Text daneben."""
    btn = Gtk.Button()
    hbox = Gtk.Box(spacing=4)
    theme = Gtk.IconTheme.get_default()
    chosen = None
    for name in icon_names:
        try:
            if theme.has_icon(name):
                chosen = name
                break
        except Exception:
            pass
    img = Gtk.Image.new_from_icon_name(chosen or "application-x-executable", Gtk.IconSize.SMALL_TOOLBAR)
    hbox.pack_start(img, False, False, 0)
    if show_label and label_text:
        hbox.pack_start(Gtk.Label(label=label_text), False, False, 0)
    btn.add(hbox)
    return btn


def make_icon_image(icon_name, fallback="application-x-executable"):
    theme = Gtk.IconTheme.get_default()
    try:
        if icon_name:
            if os.path.isabs(icon_name) and os.path.exists(icon_name):
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(icon_name, 16, 16)
                return Gtk.Image.new_from_pixbuf(pixbuf)
            if theme.has_icon(icon_name):
                return Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.SMALL_TOOLBAR)
    except Exception:
        pass
    try:
        return Gtk.Image.new_from_icon_name(fallback, Gtk.IconSize.SMALL_TOOLBAR)
    except Exception:
        return Gtk.Image()


def lookup_app_info_for_window(win):
    """Versucht, zu einem Wnck-Fenster den passenden .desktop-Eintrag (Name/Exec/Icon) zu finden."""
    wm_class = ""
    try:
        wm_class = win.get_class_group_name() or ""
    except Exception:
        pass
    name = win.get_name() or wm_class or "App"

    dirs = ["/usr/share/applications", os.path.expanduser("~/.local/share/applications")]
    candidates = []
    for d in dirs:
        for filepath in glob.glob(os.path.join(d, "*.desktop")):
            try:
                entry_name = entry_exec = entry_icon = entry_wmclass = None
                with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("Name=") and not entry_name:
                            entry_name = line.split("=", 1)[1].strip()
                        elif line.startswith("Exec=") and not entry_exec:
                            entry_exec = line.split("=", 1)[1].strip().split("%")[0].strip()
                        elif line.startswith("Icon=") and not entry_icon:
                            entry_icon = line.split("=", 1)[1].strip()
                        elif line.startswith("StartupWMClass=") and not entry_wmclass:
                            entry_wmclass = line.split("=", 1)[1].strip()
                if entry_name and entry_exec:
                    candidates.append((entry_name, entry_exec, entry_icon, entry_wmclass))
            except Exception:
                pass

    def result(n, e, i):
        return {"name": n, "exec": e, "icon": i}

    if wm_class:
        for n, e, i, wmc in candidates:
            if wmc and wmc.lower() == wm_class.lower():
                return result(n, e, i)
        for n, e, i, wmc in candidates:
            if wm_class.lower() in n.lower() or n.lower() in wm_class.lower():
                return result(n, e, i)
    for n, e, i, wmc in candidates:
        if name.lower() in n.lower() or n.lower() in name.lower():
            return result(n, e, i)

    fallback_cmd = (wm_class or name).lower().split()[0] if (wm_class or name) else "true"
    return result(name, fallback_cmd, None)


# ---------------------------------------------------------------------------
# Screen-Corners Overlay (nur mit aktivem Compositor sicher benutzbar)
# ---------------------------------------------------------------------------

class ScreenCornersOverlay(Gtk.Window):
    """Zeichnet abgerundete Bildschirm-Ecken (klickdurchlässig, RGBA-transparent)."""

    def __init__(self, radius=16):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.radius = radius
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_can_focus(False)

        screen = Gdk.Screen.get_default()
        visual = screen.get_rgba_visual() if screen else None
        if visual:
            self.set_visual(visual)

        geom = get_primary_geometry()
        self.set_default_size(geom.width, geom.height)
        self.move(geom.x, geom.y)

        self.set_app_paintable(True)
        self.connect("draw", self.on_draw)
        self.connect("realize", self.make_click_through)

    def make_click_through(self, widget):
        gdk_win = self.get_window()
        if gdk_win:
            region = cairo.Region()
            gdk_win.input_shape_combine_region(region, 0, 0)

    def on_draw(self, widget, cr):
        # Erst vollständig transparent löschen (nur mit RGBA-Visual sichtbar),
        # sonst würde hier ein undurchsichtiger schwarzer Hintergrund landen
        # -> das war der Grund für den "ganzer Bildschirm schwarz"-Bug.
        cr.save()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.restore()

        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_source_rgba(0, 0, 0, 1)
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        r = self.radius

        cr.move_to(0, 0)
        cr.line_to(r, 0)
        cr.arc(r, r, r, 1.5 * 3.14159, 3.14159)
        cr.line_to(0, 0)
        cr.fill()

        cr.move_to(w, 0)
        cr.line_to(w - r, 0)
        cr.arc(w - r, r, r, 1.5 * 3.14159, 0)
        cr.line_to(w, 0)
        cr.fill()

        cr.move_to(0, h)
        cr.line_to(r, h)
        cr.arc(r, h - r, r, 0.5 * 3.14159, 3.14159)
        cr.line_to(0, h)
        cr.fill()

        cr.move_to(w, h)
        cr.line_to(w - r, h)
        cr.arc(w - r, h - r, r, 0.5 * 3.14159, 0)
        cr.line_to(w, h)
        cr.fill()
        return False


# ---------------------------------------------------------------------------
# Popup-Basisklasse mit Fix für Aufblitzen + zuverlässigem Schließen
# ---------------------------------------------------------------------------

class PopupWindow(Gtk.Window):

    def __init__(self, parent_panel, anchor_widget, title="Popup", click_outside_close=True):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.parent_panel = parent_panel
        self.anchor_widget = anchor_widget
        self.click_outside_close = click_outside_close
        self._grabbed = False
        self._fade_val = 0.0
        self._closing = False

        self.set_title(title)
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_resizable(True)  # nötig, damit der Wachsen-Effekt per resize() greift

        if is_composited():
            screen = Gdk.Screen.get_default()
            visual = screen.get_rgba_visual() if screen else None
            if visual:
                self.set_visual(visual)

        cfg = parent_panel.config
        # Exakt dieselbe Farb-/Transparenzberechnung wie das Panel selbst
        # (update_styles()), damit Popups "aus einem Guss" wirken.
        base_rgba = parse_rgba_str(cfg.get("bg_color", "rgba(30,30,46,1.0)"))
        opacity_pct = cfg.get("panel_opacity", 95)
        alpha = (opacity_pct / 100.0) if is_composited() else 1.0
        fg = cfg.get("fg_color", "#cdd6f4")
        accent = cfg.get("accent_color", "#89b4fa")
        radius = cfg.get("popup_radius", 14)
        ring = cfg.get("popup_ring_thickness", 10)

        # WICHTIG: CSS "border"/"border-radius" auf einem undekorierten
        # (decorated=False) Gtk.Window wird von vielen GTK-Themes schlicht
        # ignoriert, egal welcher Wert eingestellt ist - das war der Grund,
        # warum der Ring nie sichtbar war. Hintergrund + Ring werden jetzt
        # deshalb selbst per Cairo gezeichnet (dieselbe Technik wie beim
        # Panel-Bulge), das funktioniert garantiert.
        self._popup_bg_rgba = (base_rgba.red, base_rgba.green, base_rgba.blue, alpha)
        amount = 0.10
        self._popup_ring_rgba = (
            base_rgba.red + (1.0 - base_rgba.red) * amount,
            base_rgba.green + (1.0 - base_rgba.green) * amount,
            base_rgba.blue + (1.0 - base_rgba.blue) * amount,
            alpha,
        )
        self._popup_radius = radius
        self._popup_ring = max(0, ring)

        self.set_app_paintable(True)
        self.connect("draw", self._on_draw_background)

        css = f"""
        window {{
            background-color: transparent;
            color: {fg};
            padding: {self._popup_ring + 8}px;
        }}
        label, button, spinbutton, checkbutton, entry, textview {{
            color: {fg};
            font-size: 11px;
        }}
        entry {{
            background: rgba(0,0,0,0.3);
            border: 1px solid {accent};
            border-radius: 4px;
            padding: 4px;
        }}
        button {{
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid transparent;
            padding: 4px 8px;
            border-radius: {max(0, radius - 6)}px;
        }}
        button:hover {{
            background: rgba(255, 255, 255, 0.2);
            border-color: {accent};
        }}
        calendar {{
            background-color: transparent;
            color: {fg};
        }}
        calendar:selected {{
            background-color: {accent};
            color: #1e1e2e;
            border-radius: 4px;
        }}
        calendar:indeterminate {{
            color: rgba({int(base_rgba.red*255)}, {int(base_rgba.green*255)}, {int(base_rgba.blue*255)}, 0.5);
        }}
        calendar.header {{
            background-color: transparent;
            color: {fg};
        }}
        calendar.button {{
            background-color: transparent;
            color: {fg};
        }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        self.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        if self.click_outside_close:
            self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.KEY_PRESS_MASK)
            self.connect("button-press-event", self._on_button_press_self)
            self.connect("key-press-event", self._on_key_press)
        else:
            # Fallback für Popups mit verschachtelten Dialogen (z.B. Farbauswahl),
            # bei denen ein harter Pointer-Grab die Unterdialoge stören würde.
            self.connect("focus-out-event", self._on_focus_out)

        self.connect("destroy", lambda w: self._end_grab())

    @staticmethod
    def _rounded_rect_path(cr, x, y, w, h, r):
        r = max(0, min(r, w / 2, h / 2))
        pi = 3.14159
        cr.new_path()
        if r <= 0:
            cr.rectangle(x, y, w, h)
            return
        cr.arc(x + r, y + r, r, pi, 1.5 * pi)
        cr.arc(x + w - r, y + r, r, 1.5 * pi, 2 * pi)
        cr.arc(x + w - r, y + h - r, r, 0, 0.5 * pi)
        cr.arc(x + r, y + h - r, r, 0.5 * pi, pi)
        cr.close_path()

    @staticmethod
    def _directional_rounded_rect_path(cr, x, y, w, h, r, panel_pos):
        """Wie _rounded_rect_path, aber nur an der Seite abgerundet, die vom
        Panel WEG zeigt - die Seite, die am Panel anliegt, bleibt eckig/voll
        breit, damit das Popup nahtlos (ohne Rundungs-Lücke) ins Panel
        übergeht, statt an der Anschlussstelle eine kleine Kerbe zu zeigen."""
        r = max(0, min(r, w / 2, h / 2))
        if r <= 0:
            cr.new_path()
            cr.rectangle(x, y, w, h)
            return
        pi = 3.14159
        cr.new_path()
        if panel_pos == "top":
            # Eckig oben (am Panel), rund unten
            cr.move_to(x, y)
            cr.line_to(x + w, y)
            cr.line_to(x + w, y + h - r)
            cr.arc(x + w - r, y + h - r, r, 0, 0.5 * pi)
            cr.line_to(x + r, y + h)
            cr.arc(x + r, y + h - r, r, 0.5 * pi, pi)
            cr.close_path()
        elif panel_pos == "bottom":
            # Eckig unten (am Panel), rund oben
            cr.move_to(x, y + r)
            cr.arc(x + r, y + r, r, pi, 1.5 * pi)
            cr.line_to(x + w - r, y)
            cr.arc(x + w - r, y + r, r, 1.5 * pi, 2 * pi)
            cr.line_to(x + w, y + h)
            cr.line_to(x, y + h)
            cr.close_path()
        elif panel_pos == "left":
            # Eckig links (am Panel), rund rechts
            cr.move_to(x, y)
            cr.line_to(x + w - r, y)
            cr.arc(x + w - r, y + r, r, 1.5 * pi, 2 * pi)
            cr.line_to(x + w, y + h - r)
            cr.arc(x + w - r, y + h - r, r, 0, 0.5 * pi)
            cr.line_to(x, y + h)
            cr.close_path()
        else:
            # Eckig rechts (am Panel), rund links
            cr.move_to(x, y + r)
            cr.arc(x + r, y + r, r, pi, 1.5 * pi)
            cr.line_to(x + w, y)
            cr.line_to(x + w, y + h)
            cr.line_to(x + r, y + h)
            cr.arc(x + r, y + h - r, r, 0.5 * pi, pi)
            cr.close_path()

    def _on_draw_background(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w <= 0 or h <= 0:
            return False

        composited = is_composited()
        if composited:
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.restore()
            cr.set_operator(cairo.OPERATOR_OVER)

        radius = self._popup_radius if composited else 0
        ring = self._popup_ring
        panel_pos = self.parent_panel.config.get("position", "top")

        # Äußere Form = Ring-Farbe (etwas heller als der Hintergrund)
        self._directional_rounded_rect_path(cr, 0, 0, w, h, radius, panel_pos)
        cr.set_source_rgba(*self._popup_ring_rgba)
        cr.fill()

        # Innerer Inhaltsbereich, um "ring" Pixel eingerückt
        inner_w = w - 2 * ring
        inner_h = h - 2 * ring
        if inner_w > 0 and inner_h > 0:
            inner_r = max(0, radius - ring)
            self._directional_rounded_rect_path(cr, ring, ring, inner_w, inner_h, inner_r, panel_pos)
            cr.set_source_rgba(*self._popup_bg_rgba)
            cr.fill()
        return False

    # -- Schließen bei Klick außerhalb (robust, unabhängig vom WM-Fokus) --

    def _start_grab(self):
        if not self.click_outside_close:
            return
        win = self.get_window()
        if not win:
            return
        display = Gdk.Display.get_default()
        seat = display.get_default_seat()
        try:
            status = seat.grab(win, Gdk.SeatCapabilities.ALL, True, None, None, None, None)
            self._grabbed = (status == Gdk.GrabStatus.SUCCESS)
        except Exception:
            self._grabbed = False
        self.grab_focus()

    def _end_grab(self):
        if self._grabbed:
            try:
                Gdk.Display.get_default().get_default_seat().ungrab()
            except Exception:
                pass
            self._grabbed = False

    def _on_button_press_self(self, widget, event):
        alloc = self.get_allocation()
        if event.x < 0 or event.y < 0 or event.x > alloc.width or event.y > alloc.height:
            self.close_popup()
            return True
        return False

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.close_popup()
            return True
        return False

    def _anim_duration_us(self):
        ms = self.parent_panel.config.get("animation_duration_ms", 200)
        return max(30, int(ms)) * 1000

    def _anchor_rect(self):
        """Position + Größe des Anchor-Buttons in Bildschirmkoordinaten - der
        Punkt, aus dem das Popup optisch 'herauswachsen' soll."""
        try:
            root_x, root_y = self.parent_panel.get_position()
            coords = self.anchor_widget.translate_coordinates(self.parent_panel, 0, 0)
            wx, wy = coords if coords else (0, 0)
            alloc = self.anchor_widget.get_allocation()
            return (
                root_x + wx,
                root_y + wy,
                max(20, alloc.width),
                max(20, alloc.height),
            )
        except Exception:
            return None

    def close_popup(self):
        if self._closing:
            return
        self._closing = True
        # Grab sofort loslassen, damit die Bedienung währenddessen nicht blockiert.
        self._end_grab()

        animate = self.parent_panel.config.get("animations", True)
        if not animate:
            self.destroy()
            return

        cur_x, cur_y = self.get_position()
        cur_w, cur_h = self.get_size()
        grow = self.parent_panel.config.get("popup_grow_effect", True)
        anchor = self._anchor_rect() if grow else None

        if anchor:
            ax, ay, aw, ah = anchor
            end_x, end_y, end_w, end_h = ax, ay, aw, ah
        else:
            p_pos = self.parent_panel.config.get("position", "top")
            offset = 14
            end_w, end_h = cur_w, cur_h
            if p_pos == "top":
                end_x, end_y = cur_x, cur_y - offset
            elif p_pos == "bottom":
                end_x, end_y = cur_x, cur_y + offset
            elif p_pos == "left":
                end_x, end_y = cur_x - offset, cur_y
            else:
                end_x, end_y = cur_x + offset, cur_y

        self._close_from = (cur_x, cur_y)
        self._close_to = (end_x, end_y)
        self._close_size_from = (cur_w, cur_h)
        self._close_size_to = (end_w, end_h)
        self._close_grow = bool(anchor)
        self._close_fade = is_composited()
        self._close_start = GLib.get_monotonic_time()
        GLib.timeout_add(10, self._anim_close_step)

    def _anim_close_step(self):
        duration_us = int(self._anim_duration_us() * 0.7)  # Schließen etwas fixer
        elapsed = GLib.get_monotonic_time() - self._close_start
        t = min(1.0, elapsed / duration_us)
        eased = t * t  # ease-in: wird zum Verschwinden hin schneller
        fx, fy = self._close_from
        tx, ty = self._close_to
        x = int(fx + (tx - fx) * eased)
        y = int(fy + (ty - fy) * eased)
        fw, fh = self._close_size_from
        tw, th = self._close_size_to
        w = max(1, int(fw + (tw - fw) * eased))
        h = max(1, int(fh + (th - fh) * eased))
        if self._close_grow:
            self._move_resize(x, y, w, h)
        else:
            self.move(x, y)
        if self._close_fade:
            self.set_opacity(1.0 - eased)
        if t >= 1.0:
            self.destroy()
            return False
        return True

    # -- Alter Fokus-basierter Fallback (Einstellungen mit Farbauswahl-Dialogen) --

    def _on_focus_out(self, widget, event):
        GLib.timeout_add(150, self._check_focus)
        return False

    def _check_focus(self):
        for win in Gtk.Window.list_toplevels():
            if win != self and win.get_visible() and win.is_active():
                return False
        if not self.is_active():
            self.close_popup()
        return False

    # -- Positionierung ohne Aufblitzen + "wächst aus dem Panel"-Animation --

    def show_adjacent(self):
        animate = self.parent_panel.config.get("animations", True)
        grow = self.parent_panel.config.get("popup_grow_effect", True)
        # Fade braucht Alpha-Blending vom Compositor, Slide/Wachsen (reines
        # Verschieben/Resizen) funktioniert dagegen IMMER, auch ganz ohne Compositor.
        fade = animate and is_composited()

        self.set_opacity(0.0 if fade else 1.0)
        # Zuerst weit außerhalb des sichtbaren Bereichs zeigen, damit die
        # anfängliche WM-Platzierung (meist oben links) nicht sichtbar aufblitzt.
        self.move(-10000, -10000)
        self.show_all()

        def reposition():
            root_x, root_y = self.parent_panel.get_position()
            coords = self.anchor_widget.translate_coordinates(self.parent_panel, 0, 0)
            wx, wy = coords if coords else (0, 0)

            abs_x = root_x + wx
            abs_y = root_y + wy

            pw, ph = self.get_size()
            panel_alloc = self.parent_panel.get_allocation()
            p_pos = self.parent_panel.config.get("position", "top")

            if p_pos == "top":
                target_x = abs_x
                target_y = root_y + panel_alloc.height
            elif p_pos == "bottom":
                target_x = abs_x
                target_y = root_y - ph
            elif p_pos == "left":
                target_x = root_x + panel_alloc.width
                target_y = abs_y
            else:
                target_x = root_x - pw
                target_y = abs_y

            geom = get_primary_geometry()
            target_x = max(geom.x + 4, min(target_x, geom.x + geom.width - pw - 4))
            target_y = max(geom.y + 4, min(target_y, geom.y + geom.height - ph - 4))

            self._start_grab()
            # Auch Popups ohne Pointer-Grab (z.B. Einstellungen) sollen Tastatur-
            # Fokus bekommen - u.a. nötig für die Hotkey-Aufnahme im Einstellungsdialog.
            self.present()
            self.grab_focus()

            if animate:
                try:
                    self.parent_panel.pulse_button(self.anchor_widget)
                except Exception:
                    pass
                anchor = self._anchor_rect() if grow else None
                if anchor:
                    start_x, start_y, start_w, start_h = anchor
                else:
                    offset = 18
                    start_w, start_h = pw, ph
                    if p_pos == "top":
                        start_x, start_y = target_x, target_y - offset
                    elif p_pos == "bottom":
                        start_x, start_y = target_x, target_y + offset
                    elif p_pos == "left":
                        start_x, start_y = target_x - offset, target_y
                    else:
                        start_x, start_y = target_x + offset, target_y

                if anchor:
                    self._move_resize(int(start_x), int(start_y), int(start_w), int(start_h))
                else:
                    self.move(int(start_x), int(start_y))
                self._anim_from = (start_x, start_y)
                self._anim_to = (target_x, target_y)
                self._anim_size_from = (start_w, start_h)
                self._anim_size_to = (pw, ph)
                self._anim_grow = bool(anchor)
                self._anim_fade = fade
                self._anim_start = GLib.get_monotonic_time()
                GLib.timeout_add(10, self._anim_step)
            else:
                self.move(int(target_x), int(target_y))
                self.set_opacity(1.0)
            return False

        GLib.idle_add(reposition)

    def _anim_step(self):
        duration_us = self._anim_duration_us()
        elapsed = GLib.get_monotonic_time() - self._anim_start
        t = min(1.0, elapsed / duration_us)
        eased = 1.0 - pow(1.0 - t, 3)  # ease-out cubic - fühlt sich "weich" an
        fx, fy = self._anim_from
        tx, ty = self._anim_to
        x = int(fx + (tx - fx) * eased)
        y = int(fy + (ty - fy) * eased)
        fw, fh = self._anim_size_from
        tw, th = self._anim_size_to
        w = max(1, int(fw + (tw - fw) * eased))
        h = max(1, int(fh + (th - fh) * eased))
        if self._anim_grow:
            self._move_resize(x, y, w, h)
        else:
            self.move(x, y)
        if self._anim_fade:
            self.set_opacity(eased)
        if t >= 1.0:
            if self._anim_grow:
                self._move_resize(int(tx), int(ty), int(tw), int(th))
            else:
                self.move(int(tx), int(ty))
            if self._anim_fade:
                self.set_opacity(1.0)
            return False
        return True

    def _move_resize(self, x, y, w, h):
        """Position UND Größe in einem einzigen X11-Request setzen (statt
        move() + resize() getrennt) - deutlich zuverlässiger über
        verschiedene Fenstermanager hinweg fürs 'Wachsen aus dem Anker'."""
        gdk_win = self.get_window()
        if gdk_win:
            gdk_win.move_resize(x, y, w, h)
        else:
            self.move(x, y)
            self.resize(w, h)
        self.queue_draw()


class WhiskerMenuPopup(PopupWindow):
    """Startmenü mit App-Suche, Anpinnen per Rechtsklick und Aktionsknöpfen."""

    def __init__(self, parent_panel, anchor_widget):
        super().__init__(parent_panel, anchor_widget, "Anwendungen", click_outside_close=True)
        self.set_default_size(260, 350)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=4)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Anwendung suchen...")
        self.search_entry.connect("search-changed", self.on_search)
        vbox.pack_start(self.search_entry, False, False, 0)

        hint = Gtk.Label(label="Rechtsklick: zum Panel anpinnen")
        hint.set_opacity(0.6)
        vbox.pack_start(hint, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.listbox.connect("row-activated", self.on_launch)
        self.listbox.connect("button-press-event", self.on_list_button_press)
        scrolled.add(self.listbox)
        vbox.pack_start(scrolled, True, True, 0)

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        action_box.set_halign(Gtk.Align.CENTER)
        action_box.set_margin_top(4)

        actions = [
            ("system-lock-screen", "Bildschirm sperren", "xflock4 &"),
            ("system-log-out", "Abmelden", "xfce4-session-logout &"),
            ("system-shutdown", "Herunterfahren", "xfce4-session-logout --halt &"),
        ]

        for icon, tooltip, cmd in actions:
            btn = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.BUTTON)
            btn.set_tooltip_text(tooltip)
            btn.connect("clicked", lambda w, c=cmd: (subprocess.Popen(c, shell=True), self.close_popup()))
            action_box.pack_start(btn, False, False, 0)

        btn_restart_panel = Gtk.Button.new_from_icon_name("view-refresh", Gtk.IconSize.BUTTON)
        btn_restart_panel.set_tooltip_text("Panel neu starten")
        btn_restart_panel.connect("clicked", lambda w: parent_panel.restart_panel())
        action_box.pack_start(btn_restart_panel, False, False, 0)

        vbox.pack_start(action_box, False, False, 0)

        self.add(vbox)
        self.apps = self.load_applications()
        self.populate_list(self.apps)

    def load_applications(self):
        apps = []
        dirs = ["/usr/share/applications", os.path.expanduser("~/.local/share/applications")]
        for d in dirs:
            for filepath in glob.glob(os.path.join(d, "*.desktop")):
                try:
                    name, exec_cmd, icon = None, None, None
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.startswith("Name=") and not name:
                                name = line.split("=", 1)[1].strip()
                            elif line.startswith("Exec=") and not exec_cmd:
                                exec_cmd = line.split("=", 1)[1].strip().split("%")[0].strip()
                            elif line.startswith("Icon=") and not icon:
                                icon = line.split("=", 1)[1].strip()
                    if name and exec_cmd:
                        apps.append((name, exec_cmd, icon))
                except Exception:
                    pass
        seen = set()
        unique = []
        for a in sorted(apps, key=lambda x: x[0].lower()):
            if a[0] not in seen:
                seen.add(a[0])
                unique.append(a)
        return unique

    def populate_list(self, app_list):
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        for name, cmd, icon in app_list[:30]:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(spacing=6)
            hbox.set_margin_top(3)
            hbox.set_margin_bottom(3)
            hbox.set_margin_start(6)
            hbox.pack_start(make_icon_image(icon), False, False, 0)
            lbl = Gtk.Label(label=name, xalign=0)
            hbox.pack_start(lbl, True, True, 0)
            row.cmd = cmd
            row.app_name = name
            row.icon_name = icon
            row.add(hbox)
            self.listbox.add(row)
        self.listbox.show_all()

    def on_search(self, entry):
        query = entry.get_text().lower()
        filtered = [a for a in self.apps if query in a[0].lower()]
        self.populate_list(filtered)

    def on_launch(self, listbox, row):
        if hasattr(row, "cmd"):
            subprocess.Popen(row.cmd, shell=True)
            self.close_popup()

    def on_list_button_press(self, listbox, event):
        if event.button == 3:
            row = listbox.get_row_at_y(int(event.y))
            if row is not None and hasattr(row, "cmd"):
                self.show_row_context_menu(row, event)
                return True
        return False

    def show_row_context_menu(self, row, event):
        menu = Gtk.Menu()
        item = Gtk.MenuItem(label=f"'{row.app_name}' zum Panel anpinnen")

        def do_pin(_w):
            pinned = self.parent_panel.config.setdefault("pinned_apps", [])
            if not any(p.get("exec") == row.cmd for p in pinned):
                pinned.append({"name": row.app_name, "exec": row.cmd, "icon": row.icon_name})
                save_config(self.parent_panel.config)
                self.parent_panel.apply_layout()

        item.connect("activate", do_pin)
        menu.append(item)
        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(event)


# ---------------------------------------------------------------------------
# Animierter "Pill"-Indikator (Caelestia/Quickshell-Stil). Reine Cairo-Füllung
# ohne Alpha-Blending -> läuft butterweich auch OHNE Compositor.
# ---------------------------------------------------------------------------

class SlidingIndicator(Gtk.DrawingArea):

    def __init__(self, accent_color="#89b4fa", vertical=False, thickness=3):
        super().__init__()
        self.vertical = vertical
        self.accent = parse_rgba_str(accent_color)
        self.cur = 0.0
        self.cur_size = 0.0
        self.target = 0.0
        self.target_size = 0.0
        self._anim_id = None
        if vertical:
            self.set_size_request(thickness, -1)
        else:
            self.set_size_request(-1, thickness)
        self.connect("draw", self.on_draw)

    def set_target(self, pos, size, instant=False):
        self.target, self.target_size = pos, size
        if instant:
            self.cur, self.cur_size = pos, size
            self.queue_draw()
            return
        if self._anim_id is None:
            self._anim_id = GLib.timeout_add(16, self._step)

    def _step(self):
        ease = 0.28
        self.cur += (self.target - self.cur) * ease
        self.cur_size += (self.target_size - self.cur_size) * ease
        self.queue_draw()
        if abs(self.cur - self.target) < 0.5 and abs(self.cur_size - self.target_size) < 0.5:
            self.cur, self.cur_size = self.target, self.target_size
            self.queue_draw()
            self._anim_id = None
            return False
        return True

    def on_draw(self, widget, cr):
        alloc = self.get_allocation()
        cr.set_source_rgba(self.accent.red, self.accent.green, self.accent.blue, 1.0)
        radius = 1.5
        if self.vertical:
            x, y, w, h = 0, self.cur, alloc.width, self.cur_size
        else:
            x, y, w, h = self.cur, 0, self.cur_size, alloc.height
        if w <= 0 or h <= 0:
            return False
        cr.new_sub_path()
        cr.arc(x + radius, y + radius, radius, 3.14159, 1.5 * 3.14159)
        cr.arc(x + w - radius, y + radius, radius, 1.5 * 3.14159, 0)
        cr.arc(x + w - radius, y + h - radius, radius, 0, 0.5 * 3.14159)
        cr.arc(x + radius, y + h - radius, radius, 0.5 * 3.14159, 3.14159)
        cr.close_path()
        cr.fill()
        return False


# ---------------------------------------------------------------------------
# Eigene Taskleiste mit Rechtsklick-Kontextmenü (ersetzt Wnck.Tasklist)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Hover-Vorschaukarte für ein Taskleisten-Fenster (Windows-Stil: kleine Karte
# mit Titel + Schließen-Kreuz oben rechts). Kein echtes Live-Thumbnail (das
# bräuchte XComposite-Pixmap-Zugriff), aber optisch dasselbe Prinzip.
# ---------------------------------------------------------------------------

class TaskbarPreview(Gtk.Window):

    def __init__(self, panel, win, taskbar):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.panel = panel
        self.win = win
        self.taskbar = taskbar
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        self.set_accept_focus(False)
        self.set_can_focus(False)

        if is_composited():
            screen = Gdk.Screen.get_default()
            visual = screen.get_rgba_visual() if screen else None
            if visual:
                self.set_visual(visual)

        cfg = panel.config
        base_rgba = parse_rgba_str(cfg.get("bg_color", "rgba(30,30,46,1.0)"))
        opacity_pct = cfg.get("panel_opacity", 95)
        alpha = (opacity_pct / 100.0) if is_composited() else 1.0
        fg = cfg.get("fg_color", "#cdd6f4")
        accent = cfg.get("accent_color", "#89b4fa")
        radius = cfg.get("popup_radius", 14)
        ring = cfg.get("popup_ring_thickness", 10)

        self._popup_bg_rgba = (base_rgba.red, base_rgba.green, base_rgba.blue, alpha)
        amount = 0.10
        self._popup_ring_rgba = (
            base_rgba.red + (1.0 - base_rgba.red) * amount,
            base_rgba.green + (1.0 - base_rgba.green) * amount,
            base_rgba.blue + (1.0 - base_rgba.blue) * amount,
            alpha,
        )
        self._popup_radius = radius
        self._popup_ring = max(0, ring)
        self.set_app_paintable(True)
        self.connect("draw", self._on_draw_background)

        css = f"""
        window {{ background-color: transparent; color: {fg}; padding: {self._popup_ring + 6}px; }}
        label {{ color: {fg}; font-size: 11px; }}
        button {{ background: rgba(255,255,255,0.08); border-radius: {max(0, radius - 8)}px;
                  padding: 1px 6px; border: 1px solid transparent; color: {fg}; }}
        button:hover {{ background: rgba(220, 60, 60, 0.65); border-color: rgba(220,60,60,0.8); }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        self.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _on_draw_background(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w <= 0 or h <= 0:
            return False
        composited = is_composited()
        if composited:
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.restore()
            cr.set_operator(cairo.OPERATOR_OVER)
        radius = self._popup_radius if composited else 0
        ring = self._popup_ring
        PopupWindow._rounded_rect_path(cr, 0, 0, w, h, radius)
        cr.set_source_rgba(*self._popup_ring_rgba)
        cr.fill()
        inner_w, inner_h = w - 2 * ring, h - 2 * ring
        if inner_w > 0 and inner_h > 0:
            PopupWindow._rounded_rect_path(cr, ring, ring, inner_w, inner_h, max(0, radius - ring))
            cr.set_source_rgba(*self._popup_bg_rgba)
            cr.fill()
        return False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        header = Gtk.Box(spacing=6)
        icon_pixbuf = None
        try:
            icon_pixbuf = win.get_mini_icon() or win.get_icon()
        except Exception:
            pass
        if icon_pixbuf:
            header.pack_start(Gtk.Image.new_from_pixbuf(icon_pixbuf), False, False, 0)
        title = "Fenster"
        try:
            title = win.get_name() or title
        except Exception:
            pass
        lbl_title = Gtk.Label(label=title, xalign=0)
        lbl_title.set_max_width_chars(26)
        lbl_title.set_ellipsize(Pango.EllipsizeMode.END)
        header.pack_start(lbl_title, True, True, 0)

        btn_close = Gtk.Button(label="X")
        btn_close.set_tooltip_text("Fenster schließen")

        def on_close(_b):
            try:
                win.close(Gtk.get_current_event_time())
            except Exception:
                pass
            taskbar._cancel_hide_preview()
            taskbar._close_preview()

        btn_close.connect("clicked", on_close)
        header.pack_start(btn_close, False, False, 0)
        outer.pack_start(header, False, False, 0)

        self.add(outer)

        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("enter-notify-event", lambda w, e: taskbar._cancel_hide_preview())
        self.connect("leave-notify-event", lambda w, e: taskbar._schedule_hide_preview())

    def show_near(self, anchor_widget):
        self.show_all()

        def reposition():
            root_x, root_y = self.panel.get_position()
            coords = anchor_widget.translate_coordinates(self.panel, 0, 0)
            wx, wy = coords if coords else (0, 0)
            alloc = anchor_widget.get_allocation()
            abs_x = root_x + wx
            abs_y = root_y + wy

            pw, ph = self.get_size()
            panel_alloc = self.panel.get_allocation()
            pos = self.panel.config.get("position", "top")

            if pos == "top":
                target_x, target_y = abs_x, root_y + panel_alloc.height
            elif pos == "bottom":
                target_x, target_y = abs_x, root_y - ph
            elif pos == "left":
                target_x, target_y = root_x + panel_alloc.width, abs_y
            else:
                target_x, target_y = root_x - pw, abs_y

            geom = get_primary_geometry()
            target_x = max(geom.x + 4, min(target_x, geom.x + geom.width - pw - 4))
            target_y = max(geom.y + 4, min(target_y, geom.y + geom.height - ph - 4))
            self.move(int(target_x), int(target_y))
            return False

        GLib.idle_add(reposition)


class CustomTaskbar(Gtk.Overlay):

    def __init__(self, panel, orientation):
        super().__init__()
        self.panel = panel
        self.orientation = orientation
        self.is_vert = orientation == Gtk.Orientation.VERTICAL
        self.buttons = {}
        self.rows = {}
        self.opts = panel.config.get("taskbar_options", {})
        self.current_preview = None
        self._hide_preview_id = None

        self.button_box = Gtk.Box(orientation=orientation, spacing=0)
        self.add(self.button_box)

        self.indicator = SlidingIndicator(
            accent_color=panel.config.get("accent_color", "#89b4fa"),
            vertical=self.is_vert,
        )
        if self.is_vert:
            self.indicator.set_halign(Gtk.Align.START)
            self.indicator.set_valign(Gtk.Align.FILL)
        else:
            self.indicator.set_halign(Gtk.Align.FILL)
            self.indicator.set_valign(Gtk.Align.END)
        self.add_overlay(self.indicator)
        self.set_overlay_pass_through(self.indicator, True)

        if not HAS_WNCK:
            self.button_box.pack_start(Gtk.Label(label="(kein Wnck)"), False, False, 0)
            return

        self.screen = Wnck.Screen.get_default()
        self.screen.force_update()
        self.screen.connect("window-opened", self.on_window_opened)
        self.screen.connect("window-closed", self.on_window_closed)
        self.screen.connect("active-window-changed", self.on_active_changed)
        self.screen.connect("active-workspace-changed", self.on_workspace_changed)

        for w in self.screen.get_windows():
            self.add_window_button(w)

        self.refresh_visibility()
        GLib.idle_add(self.update_indicator, True)

    def _short_title(self, name, maxlen=18):
        name = name or "..."
        return name if len(name) <= maxlen else name[: maxlen - 1] + "..."

    def add_window_button(self, win):
        if win in self.buttons:
            return
        try:
            if win.is_skip_tasklist():
                return
        except Exception:
            pass

        # Zeile (EventBox fürs Hover) mit dem Klick-Button. Der Schließen-
        # Button ist nicht mehr dauerhaft eingeblendet (auch bei Opacity 0
        # hat er Platz beansprucht) - stattdessen erscheint bei Hover eine
        # kleine Vorschaukarte mit Titel + Kreuz oben rechts (Windows-Stil).
        row = Gtk.EventBox()
        row.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        row_box = Gtk.Box(spacing=0)
        row.add(row_box)

        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        btn.get_style_context().add_class("taskbar-btn")

        # Im vertikalen Panel nur das Icon zeigen (Titeltext würde das schmale
        # Panel quer aufreißen) - vollen Namen gibt's dafür als Tooltip.
        # Zusätzlich per Einstellung "Namen anzeigen" steuerbar.
        show_label = (not self.is_vert) and self.opts.get("show_labels", True)

        hbox = Gtk.Box(spacing=4)
        icon_pixbuf = None
        try:
            icon_pixbuf = win.get_mini_icon() or win.get_icon()
        except Exception:
            pass
        if icon_pixbuf:
            hbox.pack_start(Gtk.Image.new_from_pixbuf(icon_pixbuf), False, False, 0)
        elif self.is_vert or not show_label:
            hbox.pack_start(Gtk.Label(label="[]"), False, False, 0)

        label = None
        if show_label:
            label = Gtk.Label(label=self._short_title(win.get_name()))
            hbox.pack_start(label, False, False, 0)
        btn.add(hbox)
        row_box.pack_start(btn, True, True, 0)

        row.connect("enter-notify-event", lambda w, e: self._on_row_enter(win, row))
        row.connect("leave-notify-event", lambda w, e: self._on_row_leave())

        try:
            if win.is_active():
                btn.get_style_context().add_class("taskbar-active-window")
        except Exception:
            pass

        btn.connect("button-press-event", self.on_button_press, win)
        btn.connect("size-allocate", lambda w, a: self.update_indicator())
        self.button_box.pack_start(row, False, False, 0)
        row.show_all()
        self.buttons[win] = btn
        self.rows[win] = row
        self.refresh_visibility()

        def set_active_class(active):
            ctx = btn.get_style_context()
            if active:
                ctx.add_class("taskbar-active-window")
            else:
                ctx.remove_class("taskbar-active-window")

        self._set_active_fns = getattr(self, "_set_active_fns", {})
        self._set_active_fns[win] = set_active_class

        try:
            if label is not None:
                win.connect("name-changed", lambda w: label.set_text(self._short_title(w.get_name())))
            win.connect("state-changed", lambda w, *a: (set_active_class(w.is_active()), self.update_indicator()))
            win.connect("workspace-changed", lambda w: self.refresh_visibility())
        except Exception:
            pass

    def _on_row_enter(self, win, anchor_widget):
        self._cancel_hide_preview()
        if self.current_preview is not None and self.current_preview.win is win:
            return
        self._close_preview()
        self.current_preview = TaskbarPreview(self.panel, win, self)
        self.current_preview.show_near(anchor_widget)

    def _on_row_leave(self):
        self._schedule_hide_preview()

    def _cancel_hide_preview(self):
        if self._hide_preview_id is not None:
            GLib.source_remove(self._hide_preview_id)
            self._hide_preview_id = None

    def _schedule_hide_preview(self):
        self._cancel_hide_preview()
        self._hide_preview_id = GLib.timeout_add(180, self._do_hide_preview)

    def _do_hide_preview(self):
        self._hide_preview_id = None
        self._close_preview()
        return False

    def _close_preview(self):
        if self.current_preview is not None:
            try:
                self.current_preview.destroy()
            except Exception:
                pass
            self.current_preview = None

    def on_window_opened(self, screen, win):
        GLib.idle_add(self.add_window_button, win)

    def on_window_closed(self, screen, win):
        self.buttons.pop(win, None)
        row = self.rows.pop(win, None)
        if row:
            row.destroy()
        if self.current_preview is not None and self.current_preview.win is win:
            self._cancel_hide_preview()
            self._close_preview()
        GLib.idle_add(self.update_indicator)

    def on_active_changed(self, screen, prev):
        active = screen.get_active_window()
        fns = getattr(self, "_set_active_fns", {})
        for w, fn in fns.items():
            try:
                fn(w == active)
            except Exception:
                pass
        self.update_indicator()

    def on_workspace_changed(self, screen, prev):
        self.refresh_visibility()
        GLib.idle_add(self.update_indicator)

    def refresh_visibility(self):
        if self.opts.get("show_other_workspaces", True):
            for btn in self.buttons.values():
                btn.set_visible(True)
            return
        try:
            active_ws = self.screen.get_active_workspace()
        except Exception:
            active_ws = None
        for win, btn in self.buttons.items():
            visible = True
            try:
                if active_ws is not None:
                    visible = win.is_pinned() or win.is_on_workspace(active_ws)
            except Exception:
                visible = True
            btn.set_visible(visible)

    def update_indicator(self, instant=False):
        try:
            active = self.screen.get_active_window()
        except Exception:
            active = None
        btn = self.buttons.get(active)
        if not btn or not btn.get_realized():
            self.indicator.set_target(0, 0, instant=True)
            return False
        coords = btn.translate_coordinates(self, 0, 0)
        if not coords:
            return False
        x, y = coords
        alloc = btn.get_allocation()
        animate = self.panel.config.get("animations", True)
        if self.is_vert:
            self.indicator.set_target(y, alloc.height, instant=instant or not animate)
        else:
            self.indicator.set_target(x, alloc.width, instant=instant or not animate)
        return False

    def on_button_press(self, btn, event, win):
        if event.button == 1:
            try:
                active_ws = self.screen.get_active_workspace()
                win_ws = win.get_workspace()
                if win_ws and active_ws and win_ws != active_ws and not win.is_pinned():
                    if self.opts.get("click_action", "switch") == "bring":
                        win.move_to_workspace(active_ws)
                    else:
                        win_ws.activate(event.time)
                    win.unminimize(event.time)
                    win.activate(event.time)
                elif win.is_active() and not win.is_minimized():
                    win.minimize()
                else:
                    win.unminimize(event.time)
                    win.activate(event.time)
            except Exception:
                pass
            GLib.idle_add(self.update_indicator)
            return True
        elif event.button == 3:
            self.show_context_menu(win, event)
            return True
        return False

    def show_context_menu(self, win, event):
        menu = Gtk.Menu()

        item_open = Gtk.MenuItem(label="Öffnen / Aktivieren")
        item_open.connect("activate", lambda w: (win.unminimize(event.time), win.activate(event.time)))
        menu.append(item_open)

        item_min = Gtk.MenuItem(label="Minimieren")
        item_min.connect("activate", lambda w: win.minimize())
        menu.append(item_min)

        item_max = Gtk.MenuItem(label="Maximieren / Wiederherstellen")

        def toggle_max(_w):
            if win.is_maximized():
                win.unmaximize()
            else:
                win.maximize()

        item_max.connect("activate", toggle_max)
        menu.append(item_max)

        menu.append(Gtk.SeparatorMenuItem())

        item_pin = Gtk.MenuItem(label="Als Widget anpinnen")
        item_pin.connect("activate", lambda w: self.panel.pin_window_as_widget(win))
        menu.append(item_pin)

        menu.append(Gtk.SeparatorMenuItem())

        item_close = Gtk.MenuItem(label="Schließen")
        item_close.connect("activate", lambda w: win.close(event.time))
        menu.append(item_close)

        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(event)


class WorkspacePager(Gtk.Overlay):
    """Kleiner Arbeitsflächen-Umschalter mit animiertem Indikator."""

    def __init__(self, panel, orientation):
        super().__init__()
        self.panel = panel
        self.orientation = orientation
        self.is_vert = orientation == Gtk.Orientation.VERTICAL
        self.buttons = {}

        self.button_box = Gtk.Box(orientation=orientation, spacing=2)
        self.add(self.button_box)

        self.indicator = SlidingIndicator(
            accent_color=panel.config.get("accent_color", "#89b4fa"),
            vertical=self.is_vert,
        )
        if self.is_vert:
            self.indicator.set_halign(Gtk.Align.START)
            self.indicator.set_valign(Gtk.Align.FILL)
        else:
            self.indicator.set_halign(Gtk.Align.FILL)
            self.indicator.set_valign(Gtk.Align.END)
        self.add_overlay(self.indicator)
        self.set_overlay_pass_through(self.indicator, True)

        if not HAS_WNCK:
            return
        self.screen = Wnck.Screen.get_default()
        self.screen.force_update()
        self.screen.connect("active-workspace-changed", self.on_active_changed)
        self.screen.connect("workspace-created", lambda s, w: self.rebuild())
        self.screen.connect("workspace-destroyed", lambda s, w: self.rebuild())
        self.rebuild()

    def rebuild(self):
        for c in self.button_box.get_children():
            self.button_box.remove(c)
        self.buttons = {}
        active = self.screen.get_active_workspace()
        for ws in self.screen.get_workspaces():
            btn = Gtk.ToggleButton(label=str(ws.get_number() + 1))
            btn.set_relief(Gtk.ReliefStyle.NONE)
            btn.set_active(ws == active)
            btn.connect("toggled", self.on_toggle, ws)
            btn.connect("size-allocate", lambda w, a: self.update_indicator())
            self.button_box.pack_start(btn, False, False, 0)
            self.buttons[ws] = btn
        self.show_all()
        GLib.idle_add(self.update_indicator, True)

    def on_toggle(self, btn, ws):
        if btn.get_active() and self.screen.get_active_workspace() != ws:
            ws.activate(Gdk.CURRENT_TIME)

    def on_active_changed(self, screen, prev):
        active = screen.get_active_workspace()
        for ws, btn in self.buttons.items():
            btn.set_active(ws == active)
        self.update_indicator()

    def update_indicator(self, instant=False):
        active = self.screen.get_active_workspace()
        btn = self.buttons.get(active)
        if not btn or not btn.get_realized():
            return False
        coords = btn.translate_coordinates(self, 0, 0)
        if not coords:
            return False
        x, y = coords
        alloc = btn.get_allocation()
        animate = self.panel.config.get("animations", True)
        if self.is_vert:
            self.indicator.set_target(y, alloc.height, instant=instant or not animate)
        else:
            self.indicator.set_target(x, alloc.width, instant=instant or not animate)
        return False


# ---------------------------------------------------------------------------
# Umsortierbare Widget-Liste für die Einstellungen: Checkbox + Auf/Ab-Buttons
# + Drag-Handle zum Umsortieren per Maus. Eine Instanz pro Sektion
# (Links/Mitte/Rechts).
# ---------------------------------------------------------------------------

_DND_ROW_TARGET = Gtk.TargetEntry.new("GTK_LIST_BOX_ROW", Gtk.TargetFlags.SAME_APP, 0)


# ---------------------------------------------------------------------------
# Notification Center: implementiert den org.freedesktop.Notifications-
# D-Bus-Dienst selbst (über Gio/GDBus, keine Zusatz-Abhängigkeit nötig).
# Das ersetzt den bisherigen Benachrichtigungsdaemon (z.B. xfce4-notifyd) -
# die meisten Daemons erlauben das explizit (deshalb REPLACE-Flag).
# ---------------------------------------------------------------------------

NOTIFICATIONS_XML = """
<node>
  <interface name="org.freedesktop.Notifications">
    <method name="GetCapabilities">
      <arg direction="out" name="capabilities" type="as"/>
    </method>
    <method name="Notify">
      <arg direction="in"  name="app_name" type="s"/>
      <arg direction="in"  name="replaces_id" type="u"/>
      <arg direction="in"  name="app_icon" type="s"/>
      <arg direction="in"  name="summary" type="s"/>
      <arg direction="in"  name="body" type="s"/>
      <arg direction="in"  name="actions" type="as"/>
      <arg direction="in"  name="hints" type="a{sv}"/>
      <arg direction="in"  name="expire_timeout" type="i"/>
      <arg direction="out" name="id" type="u"/>
    </method>
    <method name="CloseNotification">
      <arg direction="in" name="id" type="u"/>
    </method>
    <method name="GetServerInformation">
      <arg direction="out" name="name" type="s"/>
      <arg direction="out" name="vendor" type="s"/>
      <arg direction="out" name="version" type="s"/>
      <arg direction="out" name="spec_version" type="s"/>
    </method>
    <signal name="NotificationClosed">
      <arg name="id" type="u"/>
      <arg name="reason" type="u"/>
    </signal>
    <signal name="ActionInvoked">
      <arg name="id" type="u"/>
      <arg name="action_key" type="s"/>
    </signal>
  </interface>
</node>
"""


class NotificationManager:
    """Übernimmt org.freedesktop.Notifications und reicht eingehende
    Benachrichtigungen ans Panel weiter (Banner + Verlauf)."""

    def __init__(self, panel):
        self.panel = panel
        self.next_id = 1
        self.history = []  # neueste zuerst
        self.connection = None
        self.reg_id = None
        self.owner_id = Gio.bus_own_name(
            Gio.BusType.SESSION,
            "org.freedesktop.Notifications",
            Gio.BusNameOwnerFlags.REPLACE,
            self._on_bus_acquired,
            None,
            self._on_name_lost,
        )

    def _on_bus_acquired(self, connection, name):
        self.connection = connection
        try:
            node_info = Gio.DBusNodeInfo.new_for_xml(NOTIFICATIONS_XML)
            iface = node_info.interfaces[0]
            self.reg_id = connection.register_object(
                "/org/freedesktop/Notifications",
                iface,
                self._handle_method_call,
                None,
                None,
            )
        except Exception as e:
            print("Notifications-D-Bus-Dienst konnte nicht registriert werden:", e)

    def _on_name_lost(self, connection, name):
        # Ein anderer Daemon hat den Namen (evtl. exklusiv) - dann läuft
        # unser Notification Center einfach leer mit, statt abzustürzen.
        print("org.freedesktop.Notifications konnte nicht übernommen werden "
              "(evtl. läuft ein anderer Benachrichtigungsdienst exklusiv).")

    def _handle_method_call(self, connection, sender, path, iface, method, params, invocation):
        try:
            if method == "GetCapabilities":
                invocation.return_value(GLib.Variant("(as)", [["body", "actions", "icon-static", "persistence"]]))

            elif method == "Notify":
                app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout = params.unpack()
                nid = replaces_id if replaces_id else self.next_id
                if not replaces_id:
                    self.next_id += 1
                entry = {
                    "id": nid,
                    "app": app_name or "Anwendung",
                    "icon": app_icon,
                    "summary": summary,
                    "body": body,
                    "actions": actions or [],
                    "time": time.strftime("%H:%M"),
                }
                self.history = [e for e in self.history if e["id"] != nid]
                self.history.insert(0, entry)
                max_hist = self.panel.config.get("notification_settings", {}).get("max_history", 50)
                self.history = self.history[:max_hist]
                GLib.idle_add(self.panel.on_notification_received, entry, expire_timeout)
                invocation.return_value(GLib.Variant("(u)", [nid]))

            elif method == "CloseNotification":
                (nid,) = params.unpack()
                GLib.idle_add(self.panel.on_notification_closed_externally, nid)
                invocation.return_value(None)

            elif method == "GetServerInformation":
                invocation.return_value(GLib.Variant("(ssss)", ["MiniPanel", "MiniPanel", "1.0", "1.2"]))
            else:
                invocation.return_error_literal(Gio.dbus_error_quark(), 0, "Unbekannte Methode")
        except Exception as e:
            print("Fehler bei D-Bus-Notifications-Aufruf:", method, e)
            try:
                invocation.return_error_literal(Gio.dbus_error_quark(), 0, str(e))
            except Exception:
                pass

    def emit_closed(self, nid, reason):
        if self.connection:
            try:
                self.connection.emit_signal(
                    None, "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                    "NotificationClosed", GLib.Variant("(uu)", [nid, reason]),
                )
            except Exception:
                pass

    def emit_action(self, nid, action_key):
        if self.connection:
            try:
                self.connection.emit_signal(
                    None, "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
                    "ActionInvoked", GLib.Variant("(us)", [nid, action_key]),
                )
            except Exception:
                pass

    def shutdown(self):
        try:
            if self.owner_id:
                Gio.bus_unown_name(self.owner_id)
        except Exception:
            pass


class NotificationBanner(Gtk.Window):
    """Kurzzeitiges Popup für eine einzelne Benachrichtigung - optisch wie
    die anderen Widget-Fenster (gleiche Farbe/Transparenz/Rundung), aber an
    einer Bildschirmecke statt an einem Panel-Button verankert."""

    def __init__(self, panel, entry, expire_timeout):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.panel = panel
        self.entry = entry
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self._closing = False

        if is_composited():
            screen = Gdk.Screen.get_default()
            visual = screen.get_rgba_visual() if screen else None
            if visual:
                self.set_visual(visual)

        cfg = panel.config
        base_rgba = parse_rgba_str(cfg.get("bg_color", "rgba(30,30,46,1.0)"))
        opacity_pct = cfg.get("panel_opacity", 95)
        alpha = (opacity_pct / 100.0) if is_composited() else 1.0
        fg = cfg.get("fg_color", "#cdd6f4")
        accent = cfg.get("accent_color", "#89b4fa")
        radius = cfg.get("popup_radius", 14)
        ring = cfg.get("popup_ring_thickness", 10)

        self._popup_bg_rgba = (base_rgba.red, base_rgba.green, base_rgba.blue, alpha)
        amount = 0.10
        self._popup_ring_rgba = (
            base_rgba.red + (1.0 - base_rgba.red) * amount,
            base_rgba.green + (1.0 - base_rgba.green) * amount,
            base_rgba.blue + (1.0 - base_rgba.blue) * amount,
            alpha,
        )
        self._popup_radius = radius
        self._popup_ring = max(0, ring)
        self.set_app_paintable(True)
        self.connect("draw", self._on_draw_background)

        css = f"""
        window {{ background-color: transparent; color: {fg}; padding: {self._popup_ring + 10}px; }}
        label {{ color: {fg}; }}
        .notif-summary {{ font-weight: bold; font-size: 12px; }}
        .notif-body {{ font-size: 11px; }}
        .notif-app {{ font-size: 9px; opacity: 0.7; }}
        button {{ background: rgba(255,255,255,0.08); border-radius: {max(0, radius - 6)}px;
                  padding: 3px 8px; border: 1px solid transparent; color: {fg}; }}
        button:hover {{ background: rgba(255,255,255,0.2); border-color: {accent}; }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        self.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _on_draw_background(self, widget, cr):
        w = self.get_allocated_width()
        h = self.get_allocated_height()
        if w <= 0 or h <= 0:
            return False
        composited = is_composited()
        if composited:
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.restore()
            cr.set_operator(cairo.OPERATOR_OVER)
        radius = self._popup_radius if composited else 0
        ring = self._popup_ring
        PopupWindow._rounded_rect_path(cr, 0, 0, w, h, radius)
        cr.set_source_rgba(*self._popup_ring_rgba)
        cr.fill()
        inner_w, inner_h = w - 2 * ring, h - 2 * ring
        if inner_w > 0 and inner_h > 0:
            PopupWindow._rounded_rect_path(cr, ring, ring, inner_w, inner_h, max(0, radius - ring))
            cr.set_source_rgba(*self._popup_bg_rgba)
            cr.fill()
        return False

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_size_request(280, -1)
        header = Gtk.Box(spacing=6)
        header.pack_start(make_icon_image(entry.get("icon"), fallback="dialog-information"), False, False, 0)
        lbl_summary = Gtk.Label(label=entry.get("summary", ""), xalign=0)
        lbl_summary.get_style_context().add_class("notif-summary")
        lbl_summary.set_line_wrap(True)
        header.pack_start(lbl_summary, True, True, 0)
        vbox.pack_start(header, False, False, 0)

        if entry.get("body"):
            lbl_body = Gtk.Label(label=entry["body"], xalign=0)
            lbl_body.get_style_context().add_class("notif-body")
            lbl_body.set_line_wrap(True)
            vbox.pack_start(lbl_body, False, False, 0)

        lbl_app = Gtk.Label(label=entry.get("app", ""), xalign=0)
        lbl_app.get_style_context().add_class("notif-app")
        vbox.pack_start(lbl_app, False, False, 0)

        actions = entry.get("actions", [])
        if actions:
            action_box = Gtk.Box(spacing=4)
            action_box.set_halign(Gtk.Align.END)
            for i in range(0, len(actions) - 1, 2):
                action_key, action_label = actions[i], actions[i + 1]

                def on_action(_b, k=action_key):
                    self.panel.notif_manager.emit_action(self.entry["id"], k)
                    self.close_banner(reason=2)

                btn = Gtk.Button(label=action_label)
                btn.connect("clicked", on_action)
                action_box.pack_start(btn, False, False, 0)
            vbox.pack_start(action_box, False, False, 0)

        eventbox = Gtk.EventBox()
        eventbox.add(vbox)
        eventbox.connect("button-press-event", lambda w, e: self.close_banner(reason=2))
        self.add(eventbox)

        self._timeout_id = None
        duration = expire_timeout if expire_timeout and expire_timeout > 0 else \
            cfg.get("notification_settings", {}).get("banner_duration_ms", 5000)
        self._timeout_id = GLib.timeout_add(duration, lambda: self.close_banner(reason=1))

    def show_banner(self):
        self.set_opacity(0.0)
        self.show_all()

        def reposition():
            geom = get_primary_geometry()
            pw, ph = self.get_size()
            pos = self.panel.config.get("notification_settings", {}).get("banner_position", "top-right")
            margin = 12
            existing_offset = self.panel.get_banner_stack_offset(self)
            if pos == "top-right":
                target_x = geom.x + geom.width - pw - margin
                target_y = geom.y + margin + existing_offset
                start_x, start_y = target_x + 30, target_y
            elif pos == "top-left":
                target_x = geom.x + margin
                target_y = geom.y + margin + existing_offset
                start_x, start_y = target_x - 30, target_y
            elif pos == "bottom-left":
                target_x = geom.x + margin
                target_y = geom.y + geom.height - ph - margin - existing_offset
                start_x, start_y = target_x - 30, target_y
            else:  # bottom-right
                target_x = geom.x + geom.width - pw - margin
                target_y = geom.y + geom.height - ph - margin - existing_offset
                start_x, start_y = target_x + 30, target_y

            animate = self.panel.config.get("animations", True)
            self.move(int(start_x if animate else target_x), int(start_y if animate else target_y))
            if not animate:
                self.set_opacity(1.0)
                return False

            self._anim_from = (start_x, start_y)
            self._anim_to = (target_x, target_y)
            self._anim_start = GLib.get_monotonic_time()
            GLib.timeout_add(10, self._anim_step)
            return False

        GLib.idle_add(reposition)

    def _anim_step(self):
        duration_us = max(30, self.panel.config.get("animation_duration_ms", 200)) * 1000
        elapsed = GLib.get_monotonic_time() - self._anim_start
        t = min(1.0, elapsed / duration_us)
        eased = 1.0 - pow(1.0 - t, 3)
        fx, fy = self._anim_from
        tx, ty = self._anim_to
        self.move(int(fx + (tx - fx) * eased), int(fy + (ty - fy) * eased))
        if is_composited():
            self.set_opacity(eased)
        if t >= 1.0:
            self.move(int(tx), int(ty))
            self.set_opacity(1.0)
            return False
        return True

    def close_banner(self, reason=1):
        if self._closing:
            return False
        self._closing = True
        if self._timeout_id:
            try:
                GLib.source_remove(self._timeout_id)
            except Exception:
                pass
        self.panel.notif_manager.emit_closed(self.entry["id"], reason)
        self.panel.on_banner_closed(self)
        self.destroy()
        return False


# ---------------------------------------------------------------------------
# System Tray (XEmbed) - benötigt python-xlib. Ohne das Paket zeigt der
# Bereich nur einen Hinweis, statt das Panel zum Absturz zu bringen.
# ---------------------------------------------------------------------------

class SystemTray(Gtk.Box):

    def __init__(self, panel, orientation):
        super().__init__(orientation=orientation, spacing=4)
        self.panel = panel
        self.sockets = {}
        self.xlib_display = None
        self._dock_attempts = {}  # xid -> (Anzahl, letzter Zeitpunkt) gegen Endlosschleifen

        if not HAS_XLIB:
            hint = Gtk.Label(label="[Tray: python-xlib fehlt]")
            hint.set_tooltip_text(
                "Für den System-Tray wird das Paket 'python-xlib' benötigt.\n"
                "Installation z.B.: pip install python-xlib, apt install python3-xlib,\n"
                "oder pacman -S python-xlib."
            )
            self.pack_start(hint, False, False, 0)
            return

        try:
            self.xlib_display = XlibDisplay()
            screen_num = self.xlib_display.get_default_screen()
            self.tray_atom = self.xlib_display.intern_atom(f"_NET_SYSTEM_TRAY_S{screen_num}")
            self.manager_atom = self.xlib_display.intern_atom("MANAGER")
            self.opcode_atom = self.xlib_display.intern_atom("_NET_SYSTEM_TRAY_OPCODE")

            root = self.xlib_display.screen().root
            self.owner_win = root.create_window(
                -1, -1, 1, 1, 0, self.xlib_display.screen().root_depth,
            )
            self.owner_win.set_wm_name("MiniPanelTray")

            existing_owner = self.xlib_display.get_selection_owner(self.tray_atom)
            self.owner_win.set_selection_owner(self.tray_atom, X.CurrentTime)
            self.xlib_display.flush()

            if self.xlib_display.get_selection_owner(self.tray_atom) != self.owner_win:
                raise RuntimeError("Es läuft bereits ein anderer System-Tray-Manager")

            ev = xlib_event.ClientMessage(
                window=root,
                client_type=self.manager_atom,
                data=(32, [X.CurrentTime, self.tray_atom, self.owner_win.id, 0, 0]),
            )
            root.send_event(ev, event_mask=X.StructureNotifyMask)
            self.xlib_display.flush()

            GLib.io_add_watch(self.xlib_display, GLib.IO_IN, self._on_xlib_event)
            self.available = True

            # Sichtbarer Platzhalter, solange (noch) nichts angedockt ist -
            # sonst ist die Box 0px breit und man sieht nicht, ob der Tray
            # überhaupt läuft oder schlicht fehlt.
            self.status_label = Gtk.Label(label="Tray: 0")
            self.status_label.set_tooltip_text(
                "System-Tray aktiv, 0 Icons angedockt.\n"
                "Das ist normal, wenn gerade keine App mit Tray-Icon läuft\n"
                "(z.B. nm-applet, blueman-applet, Discord, ...)."
            )
            self.pack_start(self.status_label, False, False, 0)
        except Exception as e:
            print("System-Tray konnte nicht gestartet werden:", e)
            hint = Gtk.Label(label="[Tray: n/v]")
            hint.set_tooltip_text(f"Tray konnte nicht gestartet werden: {e}")
            self.pack_start(hint, False, False, 0)
            self.available = False

    def _update_status_label(self):
        if getattr(self, "status_label", None):
            count = len(self.sockets)
            self.status_label.set_text(f"Tray: {count}" if count else "Tray: 0")
            self.status_label.set_visible(count == 0)

    def _on_xlib_event(self, display_obj, condition):
        try:
            while self.xlib_display.pending_events():
                ev = self.xlib_display.next_event()
                if ev.type == X.ClientMessage and ev.client_type == self.opcode_atom:
                    data = ev.data[1]
                    opcode = data[1]
                    if opcode == 0:  # SYSTEM_TRAY_REQUEST_DOCK
                        xid = data[2]
                        GLib.idle_add(self._dock_window, xid)
        except Exception as e:
            print("Tray-Event-Fehler:", e)
        return True

    def _dock_window(self, xid):
        if xid in self.sockets:
            return False

        # Schutz gegen Endlosschleifen: manche Tray-Clients docken sofort neu
        # an, wenn das Einbetten fehlschlägt (Visual-Mismatch o.ä.) - das hat
        # bei dir das Fenstermanagement lahmgelegt. Nach ein paar erfolglosen
        # Versuchen innerhalb kurzer Zeit geben wir für dieses Icon auf.
        now = GLib.get_monotonic_time()
        count, last = self._dock_attempts.get(xid, (0, 0))
        count = count + 1 if (now - last) < 2_000_000 else 1
        self._dock_attempts[xid] = (count, now)
        if count > 3:
            print(f"System-Tray: Icon {xid} dockt wiederholt fehlerhaft an - "
                  "gebe auf, um eine Endlosschleife zu vermeiden.")
            return False

        size = self.panel.config.get("systray_icon_size", 20)
        sock = Gtk.Socket()
        # Häufigste Ursache für sofort fehlschlagendes Einbetten: der Socket
        # erbt vom (RGBA-)Panel-Fenster ein Visual, das die andockende App
        # nicht erwartet. Explizit auf das normale System-Visual zwingen.
        try:
            screen = Gdk.Screen.get_default()
            sys_visual = screen.get_system_visual() if screen else None
            if sys_visual:
                sock.set_visual(sys_visual)
        except Exception:
            pass
        sock.set_size_request(size, size)
        sock.connect("plug-removed", lambda s, x=xid: self._undock(x) or True)
        self.pack_start(sock, False, False, 0)
        sock.show()
        try:
            sock.add_id(xid)
        except Exception as e:
            print("Konnte Tray-Icon nicht einbetten:", e)
            sock.destroy()
            return False
        self.sockets[xid] = sock
        self._update_status_label()
        return False

    def _undock(self, xid):
        sock = self.sockets.pop(xid, None)
        if sock:
            sock.destroy()
        self._update_status_label()
        return False


class ReorderableWidgetList(Gtk.Box):

    def __init__(self, order_list, names_map, widgets_state, other_sections):
        """order_list: Liste von Widget-Keys dieser Sektion (wird in-place mutiert).
        widgets_state: dict Key->bool (enabled), wird in-place aktualisiert.
        other_sections: [(section_key, label, ReorderableWidgetList_oder_None), ...]
        für den "Verschieben nach"-Menüpunkt - wird nach dem Bauen aller drei
        Listen per set_other_sections() nachgetragen (gegenseitige Referenz)."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.order_list = order_list
        self.names_map = names_map
        self.widgets_state = widgets_state
        self.other_sections = other_sections or []
        self.checks = {}

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.drag_dest_set(
            Gtk.DestDefaults.MOTION | Gtk.DestDefaults.DROP,
            [_DND_ROW_TARGET],
            Gdk.DragAction.MOVE,
        )
        self.listbox.connect("drag-motion", self._on_drag_motion)
        self.listbox.connect("drag-drop", self._on_drag_drop)
        self.listbox.connect("drag-data-received", self._on_drag_data_received)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, 150)
        scrolled.add(self.listbox)
        self.pack_start(scrolled, True, True, 0)

        self.refresh()

    def set_other_sections(self, other_sections):
        self.other_sections = other_sections
        self.refresh()

    def refresh(self):
        for c in self.listbox.get_children():
            self.listbox.remove(c)

        for key in self.order_list:
            row = Gtk.ListBoxRow()
            row.widget_key = key
            hbox = Gtk.Box(spacing=4)
            hbox.set_margin_top(2)
            hbox.set_margin_bottom(2)

            handle = Gtk.EventBox()
            handle_lbl = Gtk.Label(label=":::")
            handle_lbl.set_tooltip_text("Zum Umsortieren ziehen")
            handle.add(handle_lbl)
            handle.drag_source_set(Gdk.ModifierType.BUTTON1_MASK, [_DND_ROW_TARGET], Gdk.DragAction.MOVE)
            handle.connect("drag-begin", self._on_drag_begin, row)
            handle.connect("drag-data-get", self._on_drag_data_get, row)
            hbox.pack_start(handle, False, False, 2)

            chk = Gtk.CheckButton(label=self.names_map.get(key, key))
            chk.set_active(self.widgets_state.get(key, True))
            chk.connect("toggled", lambda c, k=key: self.widgets_state.__setitem__(k, c.get_active()))
            self.checks[key] = chk
            hbox.pack_start(chk, True, True, 0)

            btn_up = Gtk.Button(label="^")
            btn_up.set_tooltip_text("Nach oben")
            btn_down = Gtk.Button(label="v")
            btn_down.set_tooltip_text("Nach unten")
            btn_up.connect("clicked", self._move, key, -1)
            btn_down.connect("clicked", self._move, key, 1)
            hbox.pack_start(btn_up, False, False, 0)
            hbox.pack_start(btn_down, False, False, 0)

            if self.other_sections:
                btn_move = Gtk.Button(label="->")
                btn_move.set_tooltip_text("In andere Sektion verschieben")
                btn_move.connect("clicked", self._show_move_menu, key)
                hbox.pack_start(btn_move, False, False, 0)

            row.add(hbox)
            self.listbox.add(row)
        self.listbox.show_all()

    def _move(self, _btn, key, direction):
        idx = self.order_list.index(key)
        new_idx = idx + direction
        if 0 <= new_idx < len(self.order_list):
            self.order_list[idx], self.order_list[new_idx] = self.order_list[new_idx], self.order_list[idx]
            self.refresh()

    def _on_drag_begin(self, _widget, _ctx, row):
        # Symbolbild fürs Ziehen: einfach die ganze Zeile als Vorschau nehmen.
        try:
            Gtk.drag_set_icon_widget(_ctx, row, 0, 0)
        except Exception:
            pass

    def _on_drag_data_get(self, _widget, _ctx, data, _info, _time, row):
        idx = self.order_list.index(row.widget_key)
        data.set(_DND_ROW_TARGET, 32, str(idx).encode())

    def _on_drag_motion(self, _widget, ctx, _x, _y, time):
        Gdk.drag_status(ctx, Gdk.DragAction.MOVE, time)
        return True

    def _on_drag_drop(self, widget, ctx, _x, y, time):
        target = self.listbox.drag_dest_find_target(ctx, None)
        if target:
            self.listbox.drag_get_data(ctx, target, time)
        return True

    def _on_drag_data_received(self, _widget, ctx, _x, y, data, _info, time):
        try:
            src_idx = int(bytes(data.get_data()).decode())
        except Exception:
            Gtk.drag_finish(ctx, False, False, time)
            return
        if not (0 <= src_idx < len(self.order_list)):
            Gtk.drag_finish(ctx, False, False, time)
            return
        row = self.listbox.get_row_at_y(y)
        dest_idx = row.get_index() if row else len(self.order_list) - 1
        key = self.order_list.pop(src_idx)
        if dest_idx > src_idx:
            dest_idx -= 1
        dest_idx = max(0, min(dest_idx, len(self.order_list)))
        self.order_list.insert(dest_idx, key)
        self.refresh()
        Gtk.drag_finish(ctx, True, False, time)

    def _show_move_menu(self, _btn, key):
        menu = Gtk.Menu()
        for section_key, label, target_list in self.other_sections:
            item = Gtk.MenuItem(label=f"Nach {label} verschieben")

            def do_move(_w, k=key, tl=target_list):
                if k in self.order_list:
                    self.order_list.remove(k)
                tl.order_list.append(k)
                self.refresh()
                tl.refresh()

            item.connect("activate", do_move)
            menu.append(item)
        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(None)


# ---------------------------------------------------------------------------
# Hauptpanel
# ---------------------------------------------------------------------------

class MiniPanel(Gtk.Window):

    def __init__(self):
        super().__init__(title="Mini Panel")
        self.config = load_config()

        self.timer_seconds = 0
        self.timer_running = False
        self.last_net_bytes = psutil.net_io_counters().bytes_recv
        self.corners_overlay = None
        self._last_composited_state = is_composited()
        self._open_popups = {}

        # Muss vor apply_layout() stehen: das Notifications-Widget greift
        # beim Bauen schon auf notif_manager zu.
        self.active_banners = []
        self.notif_manager = None
        if self.config.get("notifications_enabled", True):
            self.notif_manager = NotificationManager(self)
        self.connect("destroy", lambda w: self.notif_manager.shutdown() if self.notif_manager else None)

        self.set_type_hint(Gdk.WindowTypeHint.DOCK)
        self.set_decorated(False)
        self.set_keep_above(True)

        if is_composited():
            screen = Gdk.Screen.get_default()
            visual = screen.get_rgba_visual() if screen else None
            if visual:
                self.set_visual(visual)

        self.style_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            self.style_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self.connect("realize", self.reserve_screen_space)
        self.apply_layout()

        GLib.timeout_add_seconds(1, self.update_loop)
        self.update_loop()

        # Langsamer laufende / netzwerk- oder prozess-lastige Widgets bekommen
        # eigene, seltenere Timer statt die 1-Sekunden-Schleife zu belasten.
        GLib.timeout_add_seconds(2, self.refresh_media)
        GLib.timeout_add_seconds(300, self._updates_timer_tick)
        GLib.timeout_add_seconds(900, self._weather_timer_tick)
        GLib.timeout_add_seconds(10, self._network_timer_tick)
        GLib.timeout_add_seconds(10, self._bluetooth_timer_tick)
        GLib.idle_add(self.refresh_updates)
        GLib.idle_add(self.refresh_weather)
        GLib.idle_add(self.refresh_network_status)
        GLib.idle_add(self.refresh_bluetooth_status)

        self._setup_hotkey_socket()
        self.connect("destroy", lambda w: self._teardown_hotkey_socket())

    def _updates_timer_tick(self):
        self.refresh_updates()
        return True

    def _weather_timer_tick(self):
        if self.config.get("widgets", {}).get("weather", False):
            self.refresh_weather()
        return True

    def _network_timer_tick(self):
        if self.config.get("widgets", {}).get("network_manager", False):
            self.refresh_network_status()
        return True

    def _bluetooth_timer_tick(self):
        if self.config.get("widgets", {}).get("bluetooth", False):
            self.refresh_bluetooth_status()
        return True

    # -- Hotkeys: Unix-Socket, über den die XFCE-Tastenkürzel Aktionen anstoßen --

    def _setup_hotkey_socket(self):
        try:
            os.makedirs(os.path.dirname(HOTKEY_SOCK_PATH), exist_ok=True)
            if os.path.exists(HOTKEY_SOCK_PATH):
                os.remove(HOTKEY_SOCK_PATH)
            self._hotkey_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._hotkey_sock.bind(HOTKEY_SOCK_PATH)
            self._hotkey_sock.listen(5)
            self._hotkey_sock.setblocking(False)
            GLib.io_add_watch(self._hotkey_sock, GLib.IO_IN, self._on_hotkey_connection)
        except Exception as e:
            print("Hotkey-Socket konnte nicht eingerichtet werden:", e)
            self._hotkey_sock = None

    def _teardown_hotkey_socket(self):
        try:
            if getattr(self, "_hotkey_sock", None):
                self._hotkey_sock.close()
            if os.path.exists(HOTKEY_SOCK_PATH):
                os.remove(HOTKEY_SOCK_PATH)
        except Exception:
            pass

    def _on_hotkey_connection(self, sock, condition):
        try:
            conn, _ = sock.accept()
            data = conn.recv(256)
            conn.close()
            action = data.decode("utf-8").strip()
            if action:
                GLib.idle_add(self.handle_hotkey_action, action)
        except Exception:
            pass
        return True  # weiter auf Verbindungen lauschen

    def _get_hotkey_anchor(self, attr_name):
        """Für per Hotkey ausgelöste Popups: den echten Panel-Button als Anker
        nehmen (exakt gleiches Verhalten wie ein Klick), sonst das Panel
        selbst als Ersatz-Anker."""
        return getattr(self, attr_name, None) or self

    def handle_hotkey_action(self, action):
        try:
            if action == "toggle-whisker":
                self.open_whisker_popup(self._get_hotkey_anchor("btn_whisker"))
            elif action == "toggle-volume":
                self.open_volume_popup(self._get_hotkey_anchor("btn_vol"))
            elif action == "volume-up":
                subprocess.run(["amixer", "set", "Master", "5%+"], capture_output=True)
                self._sync_volume_label()
            elif action == "volume-down":
                subprocess.run(["amixer", "set", "Master", "5%-"], capture_output=True)
                self._sync_volume_label()
            elif action == "mute-toggle":
                subprocess.run(["amixer", "set", "Master", "toggle"], capture_output=True)
            elif action == "toggle-brightness":
                self.open_brightness_popup(self._get_hotkey_anchor("btn_bright"))
            elif action == "brightness-up":
                self._adjust_brightness(5)
            elif action == "brightness-down":
                self._adjust_brightness(-5)
            elif action == "toggle-notes":
                self.open_notes_popup(self._get_hotkey_anchor("btn_notes"))
            elif action == "toggle-timer":
                self.open_timer_popup(self._get_hotkey_anchor("btn_timer"))
            elif action == "toggle-calendar":
                self.open_calendar_popup(self._get_hotkey_anchor("btn_clock"))
            elif action == "toggle-settings":
                self.open_settings_popup(self._get_hotkey_anchor("btn_settings"))
            elif action == "take-screenshot":
                self.take_screenshot(None)
            elif action == "media-play-pause":
                if shutil.which("playerctl"):
                    subprocess.run(["playerctl", "play-pause"], capture_output=True)
            elif action == "media-next":
                if shutil.which("playerctl"):
                    subprocess.run(["playerctl", "next"], capture_output=True)
            elif action == "media-prev":
                if shutil.which("playerctl"):
                    subprocess.run(["playerctl", "previous"], capture_output=True)
            elif action == "restart-panel":
                self.restart_panel()
            elif action == "toggle-notifications":
                self.open_notifications_popup(self._get_hotkey_anchor("btn_notifications"))
            elif action == "toggle-dnd":
                ns = self.config.setdefault("notification_settings", {})
                ns["do_not_disturb"] = not ns.get("do_not_disturb", False)
                save_config(self.config)
                self._update_notification_badge()
            elif action == "toggle-network":
                self.open_network_popup(self._get_hotkey_anchor("btn_network"))
            elif action == "toggle-wifi":
                enabled = get_wifi_radio_enabled()
                set_wifi_radio(not enabled)
                GLib.timeout_add(800, lambda: (self.refresh_network_status(), False)[1])
            elif action == "toggle-bluetooth-menu":
                self.open_bluetooth_popup(self._get_hotkey_anchor("btn_bluetooth"))
            elif action == "toggle-bluetooth-power":
                powered = bool(get_bluetooth_powered())
                set_bluetooth_power(not powered)
                GLib.timeout_add(800, lambda: (self.refresh_bluetooth_status(), False)[1])
        except Exception as e:
            print("Hotkey-Aktion fehlgeschlagen:", action, e)
        return False

    def _sync_volume_label(self):
        try:
            out = subprocess.run(["amixer", "get", "Master"], capture_output=True, text=True, timeout=2)
            m = re.search(r"\[(\d+)%\]", out.stdout)
            if m and getattr(self, "lbl_vol", None):
                self.lbl_vol.set_text(f"{m.group(1)}%")
        except Exception:
            pass

    def _adjust_brightness(self, delta):
        cur = get_brightness_percent()
        if cur is None:
            return
        new_val = max(1, min(100, cur + delta))
        set_brightness_percent(new_val)
        if getattr(self, "lbl_bright", None):
            self.lbl_bright.set_text(f"{new_val}%")

    # -- Anpinnen von Fenstern als Widget --

    # -- Icon/Text/Beides pro Widget - jedes Widget bekommt sein eigenes Menü --

    def build_display_widget(self, key, default_text="", tooltip=None):
        """Baut einen Button, dessen Anzeige (Icon/Text/Beides) über die
        Einstellung für dieses Widget gesteuert wird. Rechtsklick auf den
        Button öffnet ein eigenes kleines Menü zum Umschalten.
        Gibt (button, label_or_None) zurück - label ist None im Icon-Modus."""
        mode = self.config.get("widget_display", {}).get(key, "text")
        show_icon = mode in ("icon", "both")
        show_text = mode in ("text", "both") or not show_icon  # nie ganz leer

        btn = Gtk.Button()
        hbox = Gtk.Box(spacing=4)

        if show_icon:
            theme = Gtk.IconTheme.get_default()
            chosen = None
            for name in WIDGET_ICON_NAMES.get(key, []):
                try:
                    if theme.has_icon(name):
                        chosen = name
                        break
                except Exception:
                    pass
            img = Gtk.Image.new_from_icon_name(chosen or "application-x-executable", Gtk.IconSize.SMALL_TOOLBAR)
            hbox.pack_start(img, False, False, 0)

        label = None
        if show_text:
            label = Gtk.Label(label=default_text)
            hbox.pack_start(label, False, False, 0)

        btn.add(hbox)
        full_tooltip = (tooltip + "\n" if tooltip else "") + "(Rechtsklick: Icon/Text umschalten)"
        btn.set_tooltip_text(full_tooltip)

        btn.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        btn.connect("button-press-event", self._on_display_widget_press, key)
        return btn, label

    def _on_display_widget_press(self, btn, event, key):
        if event.button != 3:
            return False
        self.show_widget_display_menu(key, event)
        return True

    def show_widget_display_menu(self, key, event):
        menu = Gtk.Menu()
        current = self.config.get("widget_display", {}).get(key, "text")
        group = None
        for value, label_text in (("icon", "Nur Icon"), ("text", "Nur Text"), ("both", "Icon + Text")):
            item = Gtk.RadioMenuItem.new_with_label(None, label_text) if group is None \
                else Gtk.RadioMenuItem.new_with_label_from_widget(group, label_text)
            if group is None:
                group = item
            item.set_active(value == current)
            item.connect("toggled", self._on_display_choice, key, value)
            menu.append(item)
        menu.show_all()
        menu.attach_to_widget(self, None)
        menu.popup_at_pointer(event)

    def _on_display_choice(self, item, key, value):
        if not item.get_active():
            return
        if self.config.get("widget_display", {}).get(key) == value:
            return
        self.config.setdefault("widget_display", {})[key] = value
        save_config(self.config)
        self.apply_layout()

    def pin_window_as_widget(self, win):
        info = lookup_app_info_for_window(win)
        pinned = self.config.setdefault("pinned_apps", [])
        if any(p.get("exec") == info["exec"] for p in pinned):
            return
        pinned.append({"name": info["name"], "exec": info["exec"], "icon": info["icon"]})
        save_config(self.config)
        self.apply_layout()

    def on_pinned_click(self, button, event, app):
        if event.button == 1:
            try:
                subprocess.Popen(app["exec"], shell=True)
            except Exception:
                pass
            return True
        elif event.button == 3:
            menu = Gtk.Menu()
            item_remove = Gtk.MenuItem(label=f"'{app.get('name', 'App')}' entfernen")

            def do_remove(_w):
                try:
                    self.config["pinned_apps"].remove(app)
                except ValueError:
                    pass
                save_config(self.config)
                self.apply_layout()

            item_remove.connect("activate", do_remove)
            menu.append(item_remove)

            menu.append(Gtk.SeparatorMenuItem())
            current = self.config.get("widget_display", {}).get("pinned", "both")
            group = None
            for value, label_text in (
                ("icon", "Alle angepinnten: Nur Icon"),
                ("text", "Alle angepinnten: Nur Text"),
                ("both", "Alle angepinnten: Icon + Text"),
            ):
                item = (
                    Gtk.RadioMenuItem.new_with_label(None, label_text)
                    if group is None
                    else Gtk.RadioMenuItem.new_with_label_from_widget(group, label_text)
                )
                if group is None:
                    group = item
                item.set_active(value == current)
                item.connect("toggled", self._on_display_choice, "pinned", value)
                menu.append(item)

            menu.show_all()
            menu.attach_to_widget(self, None)
            menu.popup_at_pointer(event)
            return True
        return False

    def reserve_screen_space(self, widget=None):
        gdk_win = self.get_window()
        if not gdk_win or not hasattr(gdk_win, "get_xid"):
            return

        xid = gdk_win.get_xid()
        pos = self.config.get("position", "top")
        size = self.config.get("panel_size", 36)
        geom = get_primary_geometry()

        strut = [0] * 12
        if pos == "top":
            strut[2] = size
            strut[8], strut[9] = geom.x, geom.x + geom.width
        elif pos == "bottom":
            strut[3] = size
            strut[10], strut[11] = geom.x, geom.x + geom.width
        elif pos == "left":
            strut[0] = size
            strut[4], strut[5] = geom.y, geom.y + geom.height
        elif pos == "right":
            strut[1] = size
            strut[6], strut[7] = geom.y, geom.y + geom.height

        strut_str = ", ".join(map(str, strut))
        strut_simple = ", ".join(map(str, strut[:4]))

        try:
            subprocess.run(
                ["xprop", "-id", str(xid), "-f", "_NET_WM_STRUT_PARTIAL", "32c", "-set", "_NET_WM_STRUT_PARTIAL", strut_str],
                check=False,
            )
            subprocess.run(
                ["xprop", "-id", str(xid), "-f", "_NET_WM_STRUT", "32c", "-set", "_NET_WM_STRUT", strut_simple],
                check=False,
            )
        except Exception:
            pass

    def update_styles(self):
        base_rgba = parse_rgba_str(self.config.get("bg_color", "rgba(30,30,46,1.0)"))
        opacity_pct = self.config.get("panel_opacity", 95)
        # Transparenz nur sinnvoll mit Compositor - ohne ihn wird das Panel
        # sonst je nach WM entweder ganz opak oder schwarz dargestellt.
        alpha = (opacity_pct / 100.0) if is_composited() else 1.0
        bg = (
            f"rgba({int(base_rgba.red * 255)}, {int(base_rgba.green * 255)}, "
            f"{int(base_rgba.blue * 255)}, {alpha:.2f})"
        )
        fg = self.config.get("fg_color", "#cdd6f4")
        accent = self.config.get("accent_color", "#89b4fa")
        radius = self.config.get("panel_radius", 0)

        css = f"""
        window {{
            background-color: {bg};
            color: {fg};
            border-radius: {radius}px;
            font-family: sans-serif;
        }}
        button {{
            background: rgba(255, 255, 255, 0.05);
            color: {fg};
            border: 1px solid transparent;
            border-radius: 6px;
            padding: 2px 6px;
            margin: 0px;
            transition: background 150ms ease, border-color 150ms ease,
                        margin 160ms cubic-bezier(0.34, 1.56, 0.64, 1);
        }}
        button:hover {{
            background: rgba(255, 255, 255, 0.15);
            border-color: {accent};
        }}
        button.taskbar-active-window {{
            background: rgba(255, 255, 255, 0.14);
            border-color: rgba(255, 255, 255, 0.25);
        }}
        button.taskbar-btn {{
            padding: 2px 4px;
            margin: 0px;
            border-radius: 4px;
        }}
        button.panel-launch-up {{ margin-top: -5px; margin-bottom: 5px; }}
        button.panel-launch-down {{ margin-bottom: -5px; margin-top: 5px; }}
        button.panel-launch-left {{ margin-left: -5px; margin-right: 5px; }}
        button.panel-launch-right {{ margin-right: -5px; margin-left: 5px; }}
        label {{ font-size: 11px; color: {fg}; }}
        """
        self.style_provider.load_from_data(css.encode())

    def apply_layout(self):
        if self.get_child():
            self.remove(self.get_child())

        self.update_styles()
        pos = self.config.get("position", "top")
        size = self.config.get("panel_size", 36)
        is_vert = pos in ["left", "right"]
        self.is_vert = is_vert

        geom = get_primary_geometry()

        if is_vert:
            box_orientation = Gtk.Orientation.VERTICAL
            self.set_default_size(size, geom.height)
            # Harte Größenbegrenzung: ohne die kann GTK das Fenster breiter
            # ziehen, sobald ein Widget-Label mehr Platz will als "size" Pixel
            # -> genau das führte zu den "queren", überlangen Panels.
            self.set_size_request(size, geom.height)
            self.move(geom.x if pos == "left" else geom.x + geom.width - size, geom.y)
        else:
            box_orientation = Gtk.Orientation.HORIZONTAL
            self.set_default_size(geom.width, size)
            self.set_size_request(geom.width, size)
            self.move(geom.x, geom.y if pos == "top" else geom.y + geom.height - size)

        outer = Gtk.Box(orientation=box_orientation, spacing=4)
        outer.set_margin_start(4)
        outer.set_margin_end(4)
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)
        self.add(outer)

        # Drei Sektionen wie bei den meisten Shells: Links/Anfang - Mitte
        # (zentriert im verbleibenden Platz) - Rechts/Ende.
        left_box = Gtk.Box(orientation=box_orientation, spacing=4)
        center_box = Gtk.Box(orientation=box_orientation, spacing=4)
        right_box = Gtk.Box(orientation=box_orientation, spacing=4)

        center_wrapper = Gtk.Box(orientation=box_orientation)
        center_wrapper.set_halign(Gtk.Align.CENTER)
        center_wrapper.set_valign(Gtk.Align.CENTER)
        center_wrapper.pack_start(center_box, False, False, 0)

        outer.pack_start(left_box, False, False, 0)
        outer.pack_start(center_wrapper, True, True, 0)
        outer.pack_end(right_box, False, False, 0)

        cfg_widgets = self.config.get("widgets", {})
        sections = self.config.get("widget_sections", DEFAULT_CONFIG["widget_sections"])

        def compact_label(lbl):
            """Sicherheitsnetz: egal was reinkommt, das Label darf das schmale
            vertikale Panel nie aufsprengen."""
            if is_vert:
                lbl.set_max_width_chars(6)
                lbl.set_justify(Gtk.Justification.CENTER)
                lbl.set_line_wrap(True)
                lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            return lbl

        for section_box, section_key in ((left_box, "left"), (center_box, "center"), (right_box, "right")):
            for w_key in sections.get(section_key, []):
                if not cfg_widgets.get(w_key, True):
                    continue
                self._build_widget(w_key, section_box, is_vert, compact_label)

        # Bildschirm-Ecken Overlay: nur mit aktivem Compositor, sonst Blackscreen-Gefahr.
        if self.config.get("screen_corners", False) and is_composited():
            if not self.corners_overlay:
                self.corners_overlay = ScreenCornersOverlay(self.config.get("corner_radius", 16))
            self.corners_overlay.show_all()
        elif self.corners_overlay:
            self.corners_overlay.hide()

        self.show_all()
        if self.get_realized():
            self.reserve_screen_space()

    def _build_widget(self, w_key, box, is_vert, compact_label):
        """Erzeugt genau ein Widget und packt es in die übergebene Sektions-Box."""
        if w_key == "whisker":
            self.btn_whisker, _lbl = self.build_display_widget("whisker", "Menu", "Anwendungsmenü")
            self.btn_whisker.connect("clicked", self.open_whisker_popup)
            box.pack_start(self.btn_whisker, False, False, 0)

        elif w_key == "pinned":
            pinned_mode = self.config.get("widget_display", {}).get("pinned", "both")
            show_icon = pinned_mode in ("icon", "both")
            show_text = (not is_vert) and pinned_mode in ("text", "both")
            for app in self.config.get("pinned_apps", []):
                pbtn = Gtk.Button()
                pbtn.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
                phbox = Gtk.Box(
                    orientation=Gtk.Orientation.VERTICAL if is_vert else Gtk.Orientation.HORIZONTAL,
                    spacing=2,
                )
                if show_icon:
                    phbox.pack_start(make_icon_image(app.get("icon")), False, False, 0)
                if show_text:
                    phbox.pack_start(Gtk.Label(label=app.get("name", "App")), False, False, 0)
                if not show_icon and not show_text:
                    # Sicherheitsnetz: nie ganz leerer Button.
                    phbox.pack_start(make_icon_image(app.get("icon")), False, False, 0)
                pbtn.add(phbox)
                pbtn.set_tooltip_text(app.get("name", "App") + "\n(Rechtsklick: Optionen)")
                pbtn.connect("button-press-event", self.on_pinned_click, app)
                box.pack_start(pbtn, False, False, 0)

        elif w_key == "workspaces":
            pager = WorkspacePager(self, Gtk.Orientation.VERTICAL if is_vert else Gtk.Orientation.HORIZONTAL)
            box.pack_start(pager, False, False, 0)

        elif w_key == "taskbar":
            tasklist = CustomTaskbar(
                self, Gtk.Orientation.VERTICAL if is_vert else Gtk.Orientation.HORIZONTAL
            )
            box.pack_start(tasklist, is_vert, is_vert, 0)

        elif w_key == "media":
            media_mode = self.config.get("widget_display", {}).get("media", "both")
            media_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL if is_vert else Gtk.Orientation.HORIZONTAL,
                spacing=2,
            )
            btn_prev = Gtk.Button(label="<<")
            btn_playpause = Gtk.Button(label="||>")
            btn_next = Gtk.Button(label=">>")
            self.lbl_media = compact_label(Gtk.Label(label=""))

            def _playerctl(cmd):
                if shutil.which("playerctl"):
                    subprocess.run(["playerctl", cmd], capture_output=True)

            btn_prev.connect("clicked", lambda b: _playerctl("previous"))
            btn_playpause.connect("clicked", lambda b: _playerctl("play-pause"))
            btn_next.connect("clicked", lambda b: _playerctl("next"))
            for w in (btn_prev, btn_playpause, btn_next):
                w.set_relief(Gtk.ReliefStyle.NONE)
                media_box.pack_start(w, False, False, 0)
            show_media_text = (not is_vert) and media_mode in ("text", "both")
            if show_media_text:
                media_box.pack_start(self.lbl_media, False, False, 0)
            media_box.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
            media_box.connect("button-press-event", self._on_display_widget_press, "media")
            self.media_box = media_box
            box.pack_start(media_box, False, False, 0)

        elif w_key == "sysmon":
            self.btn_sysmon, self.lbl_sysmon = self.build_display_widget(
                "sysmon", "", "CPU / RAM-Auslastung"
            )
            if self.lbl_sysmon:
                compact_label(self.lbl_sysmon)
            self.btn_sysmon.connect("clicked", self.launch_sysmon)
            box.pack_start(self.btn_sysmon, False, False, 0)

        elif w_key == "disk":
            btn_disk, self.lbl_disk = self.build_display_widget("disk", "", "Festplattenbelegung")
            if self.lbl_disk:
                compact_label(self.lbl_disk)
            box.pack_start(btn_disk, False, False, 0)

        elif w_key == "net":
            btn_net, self.lbl_net = self.build_display_widget("net", "", "Netzwerk-Durchsatz")
            if self.lbl_net:
                compact_label(self.lbl_net)
            box.pack_start(btn_net, False, False, 0)

        elif w_key == "brightness":
            bright = get_brightness_percent()
            if bright is not None:
                label_txt = f"{bright}%"
                tooltip = "Bildschirmhelligkeit - Scrollen ändert sie"
            else:
                label_txt = "N/V"
                tooltip = (
                    "Kein Helligkeits-Gerät gefunden. Üblich in VMs, Containern oder\n"
                    "VNC/noVNC-Sitzungen ohne echten Bildschirm-Backlight (z.B. GitHub Codespaces).\n"
                    "Prüfe: 'brightnessctl -l' auf der Konsole."
                )
            self.btn_bright, self.lbl_bright = self.build_display_widget(
                "brightness", label_txt, tooltip
            )
            self.btn_bright.add_events(Gdk.EventMask.SCROLL_MASK)
            self.btn_bright.connect("clicked", self.open_brightness_popup)
            self.btn_bright.connect("scroll-event", self.on_brightness_scroll)
            if bright is None:
                self.btn_bright.set_sensitive(False)
            box.pack_start(self.btn_bright, False, False, 0)

        elif w_key == "volume":
            self.btn_vol, self.lbl_vol = self.build_display_widget(
                "volume", "50%", "Lautstärke - Scrollen ändert sie, Mittelklick = Stumm"
            )
            self.btn_vol.add_events(Gdk.EventMask.SCROLL_MASK)
            self.btn_vol.connect("button-press-event", self.on_volume_button_press)
            self.btn_vol.connect("scroll-event", self.on_volume_scroll)
            box.pack_start(self.btn_vol, False, False, 0)

        elif w_key == "battery":
            btn_bat, self.lbl_bat = self.build_display_widget("battery", "", "Akkustatus")
            if self.lbl_bat:
                compact_label(self.lbl_bat)
            box.pack_start(btn_bat, False, False, 0)

        elif w_key == "network_manager":
            if nmcli_available():
                status = get_network_status()
                text = status[0] if status else "?"
                self.btn_network, self.lbl_network = self.build_display_widget(
                    "network_manager", text, "Netzwerk - Klick öffnet WLAN-Liste"
                )
                self.btn_network.connect("clicked", self.open_network_popup)
                box.pack_start(self.btn_network, False, False, 0)
            else:
                self.lbl_network = None

        elif w_key == "bluetooth":
            if bluetoothctl_available():
                powered = get_bluetooth_powered()
                text = "An" if powered else ("Aus" if powered is False else "?")
                self.btn_bluetooth, self.lbl_bluetooth = self.build_display_widget(
                    "bluetooth", text, "Bluetooth - Klick öffnet Geräteliste"
                )
                self.btn_bluetooth.connect("clicked", self.open_bluetooth_popup)
                box.pack_start(self.btn_bluetooth, False, False, 0)
            else:
                self.lbl_bluetooth = None

        elif w_key == "notes":
            self.btn_notes, _lbl = self.build_display_widget("notes", "Notizen", "Notizblock")
            self.btn_notes.connect("clicked", self.open_notes_popup)
            box.pack_start(self.btn_notes, False, False, 0)

        elif w_key == "timer":
            self.btn_timer, self.lbl_timer = self.build_display_widget(
                "timer", "00:00", "Kurzzeitwecker"
            )
            self.btn_timer.connect("clicked", self.open_timer_popup)
            box.pack_start(self.btn_timer, False, False, 0)

        elif w_key == "screenshot":
            btn_shot, _lbl = self.build_display_widget("screenshot", "Screenshot", "Screenshot aufnehmen")
            btn_shot.connect("clicked", self.take_screenshot)
            box.pack_start(btn_shot, False, False, 0)

        elif w_key == "updates":
            btn_upd, self.lbl_updates = self.build_display_widget("updates", "...", "Verfügbare System-Updates")
            if self.lbl_updates:
                compact_label(self.lbl_updates)
            btn_upd.connect("clicked", lambda b: self.refresh_updates(force=True))
            box.pack_start(btn_upd, False, False, 0)

        elif w_key == "weather":
            btn_weather, self.lbl_weather = self.build_display_widget("weather", "...", "Wetter")
            if self.lbl_weather:
                self.lbl_weather.set_max_width_chars(9 if not is_vert else 6)
                self.lbl_weather.set_ellipsize(Pango.EllipsizeMode.END)
                if is_vert:
                    self.lbl_weather.set_line_wrap(True)
                    self.lbl_weather.set_justify(Gtk.Justification.CENTER)
            btn_weather.connect("clicked", lambda b: self.refresh_weather(force=True))
            box.pack_start(btn_weather, False, False, 0)

        elif w_key == "systray":
            tray = SystemTray(self, Gtk.Orientation.VERTICAL if is_vert else Gtk.Orientation.HORIZONTAL)
            self.systray_widget = tray
            box.pack_start(tray, False, False, 0)

        elif w_key == "notifications":
            self.btn_notifications, self.lbl_notifications = self.build_display_widget(
                "notifications", "0", "Benachrichtigungscenter"
            )
            self.btn_notifications.connect("clicked", self.open_notifications_popup)
            self._update_notification_badge()
            box.pack_start(self.btn_notifications, False, False, 0)

        elif w_key == "clock":
            self.btn_clock, self.lbl_clock = self.build_display_widget("clock", "", "Kalender öffnen")
            if self.lbl_clock is None:
                # Icon-only ergibt für die Uhrzeit wenig Sinn - zur Sicherheit
                # trotzdem ein Text-Label anhängen.
                self.lbl_clock = Gtk.Label()
                self.btn_clock.get_child().pack_start(self.lbl_clock, False, False, 0)
                self.lbl_clock.show()
            self.btn_clock.connect("clicked", self.open_calendar_popup)
            box.pack_start(self.btn_clock, False, False, 0)

        elif w_key == "settings":
            self.btn_settings, _lbl = self.build_display_widget("settings", "Optionen", "Einstellungen")
            self.btn_settings.connect("clicked", self.open_settings_popup)
            box.pack_start(self.btn_settings, False, False, 0)

    # --- Popups ---
    # -- Popups toggeln: nochmal auf den Panel-Button tippen schließt sie wieder --

    def _maybe_close_existing(self, key):
        existing = self._open_popups.get(key)
        if existing and not getattr(existing, "_closing", False):
            existing.close_popup()
            return True
        return False

    def _register_popup(self, key, popup):
        self._open_popups[key] = popup
        popup.connect("destroy", lambda w: self._open_popups.pop(key, None) if self._open_popups.get(key) is w else None)

    def open_whisker_popup(self, button):
        if self._maybe_close_existing("whisker"):
            return
        popup = WhiskerMenuPopup(self, button)
        self._register_popup("whisker", popup)
        popup.show_adjacent()

    def open_volume_popup(self, button):
        if self._maybe_close_existing("volume"):
            return
        popup = PopupWindow(self, button, "Lautstaerke")
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        scale.set_value(50)

        def set_vol(slider):
            val = int(slider.get_value())
            if getattr(self, "lbl_vol", None):
                self.lbl_vol.set_text(f"{val}%")
            else:
                self.btn_vol.set_tooltip_text(f"Lautstärke: {val}% (Rechtsklick: Icon/Text umschalten)")
            subprocess.run(["amixer", "set", "Master", f"{val}%"], capture_output=True)

        scale.connect("value-changed", set_vol)
        vbox.pack_start(Gtk.Label(label="Lautstaerke:"), False, False, 0)
        vbox.pack_start(scale, True, True, 0)
        popup.add(vbox)
        self._register_popup("volume", popup)
        popup.show_adjacent()

    def open_notes_popup(self, button):
        if self._maybe_close_existing("notes"):
            return
        popup = PopupWindow(self, button, "Notizblock")
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        textview = Gtk.TextView()
        textview.set_size_request(200, 120)
        buffer = textview.get_buffer()

        if os.path.exists(NOTES_FILE):
            try:
                with open(NOTES_FILE, "r") as f:
                    buffer.set_text(f.read())
            except Exception:
                pass

        def save_notes(btn):
            start, end = buffer.get_bounds()
            text = buffer.get_text(start, end, True)
            with open(NOTES_FILE, "w") as f:
                f.write(text)
            popup.close_popup()

        btn_save = Gtk.Button(label="Speichern")
        btn_save.connect("clicked", save_notes)
        vbox.pack_start(textview, True, True, 0)
        vbox.pack_start(btn_save, False, False, 0)
        popup.add(vbox)
        self._register_popup("notes", popup)
        popup.show_adjacent()

    def open_calendar_popup(self, button):
        if self._maybe_close_existing("calendar"):
            return
        popup = PopupWindow(self, button, "Kalender")
        cal = Gtk.Calendar()
        # Gtk.Calendar ist ein natives Widget mit eigenem Theme-Hintergrund
        # und würde ohne eigene Box das Padding/den Ring des Popups
        # verdrängen - deshalb explizit mit Rand versehen statt direkt
        # als einziges Kind einzuhängen.
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrap.set_margin_start(2)
        wrap.set_margin_end(2)
        wrap.set_margin_top(2)
        wrap.set_margin_bottom(2)
        wrap.pack_start(cal, True, True, 0)
        popup.add(wrap)
        self._register_popup("calendar", popup)
        popup.show_adjacent()

    def open_timer_popup(self, button):
        if self._maybe_close_existing("timer"):
            return
        popup = PopupWindow(self, button, "Timer")
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        spin = Gtk.SpinButton.new_with_range(1, 60, 1)
        spin.set_value(5)
        vbox.pack_start(Gtk.Label(label="Minuten:"), False, False, 0)
        vbox.pack_start(spin, False, False, 0)

        btn_box = Gtk.Box(spacing=4)
        btn_start = Gtk.Button(label="Start")
        btn_reset = Gtk.Button(label="Reset")

        def start_t(x):
            self.timer_seconds = int(spin.get_value()) * 60
            self.timer_running = True
            popup.close_popup()

        def reset_t(x):
            self.timer_seconds = 0
            self.timer_running = False
            if getattr(self, "lbl_timer", None):
                self.lbl_timer.set_text("00:00")
            popup.close_popup()

        btn_start.connect("clicked", start_t)
        btn_reset.connect("clicked", reset_t)
        btn_box.pack_start(btn_start, True, True, 0)
        btn_box.pack_start(btn_reset, True, True, 0)
        vbox.pack_start(btn_box, False, False, 0)
        popup.add(vbox)
        self._register_popup("timer", popup)
        popup.show_adjacent()

    def open_brightness_popup(self, button):
        if self._maybe_close_existing("brightness"):
            return
        popup = PopupWindow(self, button, "Helligkeit")
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=8)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 100, 1)
        cur = get_brightness_percent()
        scale.set_value(cur if cur is not None else 100)

        def set_b(slider):
            val = int(slider.get_value())
            set_brightness_percent(val)
            if getattr(self, "lbl_bright", None):
                self.lbl_bright.set_text(f"{val}%")

        scale.connect("value-changed", set_b)
        vbox.pack_start(Gtk.Label(label="Helligkeit:"), False, False, 0)
        vbox.pack_start(scale, True, True, 0)
        popup.add(vbox)
        self._register_popup("brightness", popup)
        popup.show_adjacent()

    def on_brightness_scroll(self, button, event):
        delta = 5 if event.direction == Gdk.ScrollDirection.UP else -5
        self._adjust_brightness(delta)
        return True

    def on_volume_button_press(self, button, event):
        if event.button == 1:
            self.open_volume_popup(button)
        elif event.button == 2:
            subprocess.run(["amixer", "set", "Master", "toggle"], capture_output=True)
        return True

    def on_volume_scroll(self, button, event):
        cmd = "5%+" if event.direction == Gdk.ScrollDirection.UP else "5%-"
        subprocess.run(["amixer", "set", "Master", cmd], capture_output=True)
        self._sync_volume_label()
        return True

    def refresh_updates(self, force=False):
        if not getattr(self, "lbl_updates", None):
            return

        def worker():
            count = get_updates_count()
            GLib.idle_add(self._set_updates_label, count)

        threading.Thread(target=worker, daemon=True).start()

    def _set_updates_label(self, count):
        if getattr(self, "lbl_updates", None):
            if count is None:
                self.lbl_updates.set_text("n/v" if self.is_vert else "UPD: n/v")
            else:
                self.lbl_updates.set_text(str(count) if self.is_vert else f"UPD: {count}")
        return False

    def refresh_weather(self, force=False):
        if not getattr(self, "lbl_weather", None):
            return

        def worker():
            try:
                req = urllib.request.Request(
                    "https://wttr.in/?format=3", headers={"User-Agent": "curl"}
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    text = resp.read().decode("utf-8", errors="ignore").strip()
            except Exception:
                text = None
            GLib.idle_add(self._set_weather_label, text)

        threading.Thread(target=worker, daemon=True).start()

    def _set_weather_label(self, text):
        if getattr(self, "lbl_weather", None):
            self.lbl_weather.set_text(text if text else "n/v")
        return False

    def refresh_media(self):
        if not hasattr(self, "lbl_media"):
            return True
        info = get_media_status()
        if info is None:
            self.lbl_media.set_text("")
            if hasattr(self, "media_box"):
                self.media_box.set_opacity(0.4)
        else:
            status, title = info
            icon = ">" if status == "Playing" else "||"
            self.lbl_media.set_text(f"{icon} {title}"[:40])
            if hasattr(self, "media_box"):
                self.media_box.set_opacity(1.0)
        return True

    def pulse_button(self, widget):
        """Lässt genau den Button, aus dem ein Popup 'herauswächst', kurz in
        Gegenrichtung ausschlagen und zurückfedern (CSS-Margin-Animation) -
        anders als ein globales Verschieben ist das direkt an der Position
        des jeweiligen Widgets verankert."""
        if not self.config.get("animations", True) or not self.config.get("panel_pulse_effect", True):
            return
        if widget is None or widget is self or not isinstance(widget, Gtk.Widget):
            return

        pos = self.config.get("position", "top")
        css_class = {
            "top": "panel-launch-up",
            "bottom": "panel-launch-down",
            "left": "panel-launch-left",
            "right": "panel-launch-right",
        }.get(pos, "panel-launch-up")

        try:
            ctx = widget.get_style_context()
            ctx.add_class(css_class)
            GLib.timeout_add(160, lambda: (ctx.remove_class(css_class), False)[1])
        except Exception:
            pass

    def restart_panel(self, *_args):
        """Startet das Panel-Skript neu - z.B. nötig, damit ein frisch aktivierter
        Compositor für echte Transparenz/abgerundete Ecken greift."""
        save_config(self.config)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # -- Notification Center --

    def _update_notification_badge(self):
        if not getattr(self, "lbl_notifications", None) or not self.notif_manager:
            return
        count = len(self.notif_manager.history)
        self.lbl_notifications.set_text(str(count))
        dnd = self.config.get("notification_settings", {}).get("do_not_disturb", False)
        tooltip = f"{count} Benachrichtigung(en)" + (" - Nicht stören aktiv" if dnd else "")
        if hasattr(self, "btn_notifications"):
            self.btn_notifications.set_tooltip_text(tooltip)

    def on_notification_received(self, entry, expire_timeout):
        self._update_notification_badge()
        if getattr(self, "notif_popup_listbox", None):
            self._refresh_notifications_popup_list()

        dnd = self.config.get("notification_settings", {}).get("do_not_disturb", False)
        if not dnd and self.config.get("widgets", {}).get("notifications", True):
            banner = NotificationBanner(self, entry, expire_timeout)
            self.active_banners.append(banner)
            banner.show_banner()
        return False

    def on_notification_closed_externally(self, nid):
        for banner in list(self.active_banners):
            if banner.entry.get("id") == nid:
                banner.close_banner(reason=3)
        return False

    def on_banner_closed(self, banner):
        if banner in self.active_banners:
            self.active_banners.remove(banner)
        # Verbleibende Banner nachrücken lassen.
        for b in self.active_banners:
            try:
                b.show_banner()
            except Exception:
                pass

    def get_banner_stack_offset(self, for_banner):
        offset = 0
        margin = 8
        for b in self.active_banners:
            if b is for_banner:
                break
            try:
                _, h = b.get_size()
                offset += h + margin
            except Exception:
                pass
        return offset

    def open_notifications_popup(self, button):
        if self._maybe_close_existing("notifications"):
            return
        popup = PopupWindow(self, button, "Benachrichtigungen")
        popup.set_default_size(300, 350)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)

        top_box = Gtk.Box(spacing=6)
        chk_dnd = Gtk.CheckButton(label="Nicht stören")
        chk_dnd.set_active(self.config.get("notification_settings", {}).get("do_not_disturb", False))

        def toggle_dnd(chk):
            self.config.setdefault("notification_settings", {})["do_not_disturb"] = chk.get_active()
            save_config(self.config)
            self._update_notification_badge()

        chk_dnd.connect("toggled", toggle_dnd)
        top_box.pack_start(chk_dnd, False, False, 0)

        btn_clear = Gtk.Button(label="Alle löschen")

        def clear_all(_b):
            if self.notif_manager:
                self.notif_manager.history = []
            self._update_notification_badge()
            self._refresh_notifications_popup_list()

        btn_clear.connect("clicked", clear_all)
        top_box.pack_end(btn_clear, False, False, 0)
        vbox.pack_start(top_box, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.notif_popup_listbox = Gtk.ListBox()
        self.notif_popup_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.add(self.notif_popup_listbox)
        vbox.pack_start(scrolled, True, True, 0)

        def on_destroy(_w):
            self.notif_popup_listbox = None

        popup.connect("destroy", on_destroy)
        self._refresh_notifications_popup_list()

        popup.add(vbox)
        self._register_popup("notifications", popup)
        popup.show_adjacent()

    def _refresh_notifications_popup_list(self):
        listbox = getattr(self, "notif_popup_listbox", None)
        if not listbox:
            return
        for c in listbox.get_children():
            listbox.remove(c)
        history = self.notif_manager.history if self.notif_manager else []
        if not history:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="Keine Benachrichtigungen"))
            listbox.add(row)
        for entry in history:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(spacing=6)
            hbox.set_margin_top(3)
            hbox.set_margin_bottom(3)
            hbox.pack_start(make_icon_image(entry.get("icon"), fallback="dialog-information"), False, False, 0)
            textbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            lbl_top = Gtk.Label(label=f"{entry.get('summary', '')}  -  {entry.get('time', '')}", xalign=0)
            textbox.pack_start(lbl_top, False, False, 0)
            if entry.get("body"):
                lbl_body = Gtk.Label(label=entry["body"], xalign=0)
                lbl_body.set_line_wrap(True)
                lbl_body.set_max_width_chars(28)
                textbox.pack_start(lbl_body, False, False, 0)
            hbox.pack_start(textbox, True, True, 0)
            row.add(hbox)
            listbox.add(row)
        listbox.show_all()

    # -- Netzwerk (nmcli) --

    def open_network_popup(self, button):
        if self._maybe_close_existing("network"):
            return
        popup = PopupWindow(self, button, "Netzwerk")
        popup.set_default_size(280, 340)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)

        top = Gtk.Box(spacing=6)
        chk_wifi = Gtk.CheckButton(label="WLAN aktiviert")
        chk_wifi.set_active(get_wifi_radio_enabled())

        def toggle_wifi(chk):
            enabled = chk.get_active()
            threading.Thread(target=lambda: set_wifi_radio(enabled), daemon=True).start()
            GLib.timeout_add(800, lambda: (refresh_list(), False)[1])

        chk_wifi.connect("toggled", toggle_wifi)
        top.pack_start(chk_wifi, False, False, 0)

        btn_refresh = Gtk.Button(label="Aktualisieren")
        top.pack_end(btn_refresh, False, False, 0)
        vbox.pack_start(top, False, False, 0)

        lbl_status = Gtk.Label(label="Suche...", xalign=0)
        vbox.pack_start(lbl_status, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.add(listbox)
        vbox.pack_start(scrolled, True, True, 0)

        def build_row(net):
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(spacing=6)
            hbox.set_margin_top(3)
            hbox.set_margin_bottom(3)
            icon_name = "network-wireless-signal-excellent-symbolic" if net["signal"] > 75 else \
                "network-wireless-signal-good-symbolic" if net["signal"] > 50 else \
                "network-wireless-signal-ok-symbolic" if net["signal"] > 25 else \
                "network-wireless-signal-weak-symbolic"
            hbox.pack_start(make_icon_image(icon_name, fallback="network-wireless-symbolic"), False, False, 0)
            label_txt = net["ssid"] + (" (verbunden)" if net["active"] else "")
            hbox.pack_start(Gtk.Label(label=label_txt, xalign=0), True, True, 0)
            if net["secure"]:
                hbox.pack_start(make_icon_image("changes-prevent-symbolic", fallback="dialog-password"), False, False, 0)

            if net["active"]:
                btn = Gtk.Button(label="Trennen")
                btn.connect("clicked", lambda b, s=net["ssid"]: do_disconnect(s))
            else:
                btn = Gtk.Button(label="Verbinden")
                btn.connect("clicked", lambda b, n=net: do_connect(n))
            hbox.pack_start(btn, False, False, 0)
            row.add(hbox)
            return row

        def refresh_list(rescan=True):
            lbl_status.set_text("Suche..." if rescan else "Aktualisiere...")

            def worker():
                nets = list_wifi_networks(rescan=rescan)
                GLib.idle_add(apply_list, nets)

            threading.Thread(target=worker, daemon=True).start()

        def apply_list(nets):
            for c in listbox.get_children():
                listbox.remove(c)
            if not nets:
                r = Gtk.ListBoxRow()
                r.add(Gtk.Label(label="Keine Netzwerke gefunden"))
                listbox.add(r)
            for net in nets:
                listbox.add(build_row(net))
            listbox.show_all()
            lbl_status.set_text(f"{len(nets)} Netzwerk(e) gefunden")
            return False

        def do_connect(net):
            if not net["secure"]:
                lbl_status.set_text(f"Verbinde mit {net['ssid']}...")
                threading.Thread(target=lambda: _connect_and_report(net["ssid"], None), daemon=True).start()
                return
            # Passwort-Eingabe für gesicherte, noch unbekannte Netzwerke.
            pw_popup = PopupWindow(self, button, "WLAN-Passwort", click_outside_close=True)
            pw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
            pw_box.pack_start(Gtk.Label(label=f"Passwort für '{net['ssid']}':"), False, False, 0)
            entry = Gtk.Entry()
            entry.set_visibility(False)
            pw_box.pack_start(entry, False, False, 0)
            btn_ok = Gtk.Button(label="Verbinden")

            def submit(_w=None):
                pw = entry.get_text()
                pw_popup.close_popup()
                lbl_status.set_text(f"Verbinde mit {net['ssid']}...")
                threading.Thread(target=lambda: _connect_and_report(net["ssid"], pw), daemon=True).start()

            entry.connect("activate", submit)
            btn_ok.connect("clicked", submit)
            pw_box.pack_start(btn_ok, False, False, 0)
            pw_popup.add(pw_box)
            pw_popup.show_adjacent()

        def _connect_and_report(ssid, pw):
            ok, msg = nmcli_connect_wifi(ssid, pw)
            GLib.idle_add(_after_connect, ok, msg)

        def _after_connect(ok, msg):
            lbl_status.set_text("Verbunden." if ok else f"Fehlgeschlagen: {msg[:60]}")
            refresh_list(rescan=False)
            self.refresh_network_status()
            return False

        def do_disconnect(ssid):
            lbl_status.set_text("Trenne...")

            def worker():
                nmcli_disconnect(ssid)
                GLib.idle_add(lambda: (refresh_list(rescan=False), self.refresh_network_status(), False)[2])

            threading.Thread(target=worker, daemon=True).start()

        btn_refresh.connect("clicked", lambda b: refresh_list(rescan=True))
        refresh_list(rescan=True)

        popup.add(vbox)
        self._register_popup("network", popup)
        popup.show_adjacent()

    def refresh_network_status(self):
        if not getattr(self, "lbl_network", None):
            return

        def worker():
            status = get_network_status()
            GLib.idle_add(self._set_network_label, status)

        threading.Thread(target=worker, daemon=True).start()

    def _set_network_label(self, status):
        if getattr(self, "lbl_network", None):
            self.lbl_network.set_text(status[0] if status else "?")
        return False

    # -- Bluetooth (bluetoothctl) --

    def open_bluetooth_popup(self, button):
        if self._maybe_close_existing("bluetooth"):
            return
        popup = PopupWindow(self, button, "Bluetooth")
        popup.set_default_size(280, 320)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)

        top = Gtk.Box(spacing=6)
        chk_power = Gtk.CheckButton(label="Bluetooth aktiviert")
        chk_power.set_active(bool(get_bluetooth_powered()))

        def toggle_power(chk):
            enabled = chk.get_active()
            threading.Thread(target=lambda: set_bluetooth_power(enabled), daemon=True).start()
            GLib.timeout_add(800, lambda: (refresh_list(), False)[1])

        chk_power.connect("toggled", toggle_power)
        top.pack_start(chk_power, False, False, 0)

        btn_scan = Gtk.Button(label="Suchen")
        top.pack_end(btn_scan, False, False, 0)
        vbox.pack_start(top, False, False, 0)

        lbl_status = Gtk.Label(label="", xalign=0)
        vbox.pack_start(lbl_status, False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.add(listbox)
        vbox.pack_start(scrolled, True, True, 0)

        def build_row(dev):
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(spacing=6)
            hbox.set_margin_top(3)
            hbox.set_margin_bottom(3)
            hbox.pack_start(make_icon_image("bluetooth-symbolic", fallback="bluetooth"), False, False, 0)
            label_txt = dev["name"] + (" (verbunden)" if dev["connected"] else "")
            hbox.pack_start(Gtk.Label(label=label_txt, xalign=0), True, True, 0)

            if dev["connected"]:
                btn = Gtk.Button(label="Trennen")
                btn.connect("clicked", lambda b, m=dev["mac"]: do_disconnect(m))
            elif dev["paired"]:
                btn = Gtk.Button(label="Verbinden")
                btn.connect("clicked", lambda b, m=dev["mac"]: do_connect(m))
            else:
                btn = Gtk.Button(label="Koppeln")
                btn.connect("clicked", lambda b, m=dev["mac"]: do_pair(m))
            hbox.pack_start(btn, False, False, 0)
            row.add(hbox)
            return row

        def refresh_list():
            def worker():
                devices = list_bt_devices()
                GLib.idle_add(apply_list, devices)

            threading.Thread(target=worker, daemon=True).start()

        def apply_list(devices):
            for c in listbox.get_children():
                listbox.remove(c)
            if not devices:
                r = Gtk.ListBoxRow()
                r.add(Gtk.Label(label="Keine Geräte bekannt"))
                listbox.add(r)
            for dev in devices:
                listbox.add(build_row(dev))
            listbox.show_all()
            return False

        def do_scan(_b=None):
            lbl_status.set_text("Suche 8 Sekunden lang...")

            def worker():
                bt_scan(8)
                GLib.idle_add(lambda: (lbl_status.set_text(""), refresh_list(), False)[2])

            threading.Thread(target=worker, daemon=True).start()

        def do_connect(mac):
            lbl_status.set_text("Verbinde...")

            def worker():
                bt_connect(mac)
                GLib.idle_add(lambda: (refresh_list(), self.refresh_bluetooth_status(), lbl_status.set_text(""), False)[3])

            threading.Thread(target=worker, daemon=True).start()

        def do_disconnect(mac):
            def worker():
                bt_disconnect(mac)
                GLib.idle_add(lambda: (refresh_list(), self.refresh_bluetooth_status(), False)[2])

            threading.Thread(target=worker, daemon=True).start()

        def do_pair(mac):
            lbl_status.set_text("Kopple...")

            def worker():
                bt_pair(mac)
                bt_connect(mac)
                GLib.idle_add(lambda: (refresh_list(), self.refresh_bluetooth_status(), lbl_status.set_text(""), False)[3])

            threading.Thread(target=worker, daemon=True).start()

        btn_scan.connect("clicked", do_scan)
        refresh_list()

        popup.add(vbox)
        self._register_popup("bluetooth", popup)
        popup.show_adjacent()

    def refresh_bluetooth_status(self):
        if not getattr(self, "lbl_bluetooth", None):
            return

        def worker():
            powered = get_bluetooth_powered()
            GLib.idle_add(self._set_bluetooth_label, powered)

        threading.Thread(target=worker, daemon=True).start()

    def _set_bluetooth_label(self, powered):
        if getattr(self, "lbl_bluetooth", None):
            self.lbl_bluetooth.set_text("An" if powered else ("Aus" if powered is False else "?"))
        return False

    def take_screenshot(self, button):
        tool = (
            shutil.which("xfce4-screenshooter")
            or shutil.which("gnome-screenshot")
            or shutil.which("scrot")
        )
        if tool:
            subprocess.Popen([tool])

    def open_settings_popup(self, button):
        if self._maybe_close_existing("settings"):
            return
        # Fokus-basiertes Schließen (statt Pointer-Grab), da hier Farbwahl-Dialoge
        # als eigene Fenster aufgehen und ein harter Grab die stören würde.
        popup = PopupWindow(self, button, "Einstellungen", click_outside_close=False)

        # Nie mehr als ein Viertel der Bildschirmfläche einnehmen (halbe
        # Breite x halbe Höhe) - passt sich also automatisch an kleine wie
        # große Bildschirme an, statt eine feste Pixelgröße zu erzwingen.
        geom = get_primary_geometry()
        max_w = max(360, int(geom.width * 0.5))
        max_h = max(320, int(geom.height * 0.5))
        popup.set_default_size(max_w, max_h)
        appearance_scroll_height = max_h - 90  # Platz für Tabs + Speichern-Button

        notebook = Gtk.Notebook()

        # Tab 1: Aussehen & Color Picker
        grid = Gtk.Grid(row_spacing=6, column_spacing=8, margin=8)

        grid.attach(Gtk.Label(label="Position:", xalign=0), 0, 0, 1, 1)
        combo_pos = Gtk.ComboBoxText()
        positions = ["top", "bottom", "left", "right"]
        for p in positions:
            combo_pos.append_text(p)
        combo_pos.set_active(positions.index(self.config.get("position", "top")))
        grid.attach(combo_pos, 1, 0, 1, 1)

        grid.attach(Gtk.Label(label="Groesse (px):", xalign=0), 0, 1, 1, 1)
        spin_size = Gtk.SpinButton.new_with_range(24, 80, 2)
        spin_size.set_value(self.config.get("panel_size", 36))
        grid.attach(spin_size, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Panel Rundung:", xalign=0), 0, 2, 1, 1)
        spin_p_radius = Gtk.SpinButton.new_with_range(0, 30, 1)
        spin_p_radius.set_value(self.config.get("panel_radius", 0))
        grid.attach(spin_p_radius, 1, 2, 1, 1)

        grid.attach(Gtk.Label(label="Popup Rundung:", xalign=0), 0, 3, 1, 1)
        spin_popup_radius = Gtk.SpinButton.new_with_range(0, 30, 1)
        spin_popup_radius.set_value(self.config.get("popup_radius", 14))
        grid.attach(spin_popup_radius, 1, 3, 1, 1)

        grid.attach(Gtk.Label(label="Transparenz:", xalign=0), 0, 4, 1, 1)
        scale_opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 10, 100, 1)
        scale_opacity.set_value(self.config.get("panel_opacity", 95))
        scale_opacity.set_hexpand(True)
        scale_opacity.set_digits(0)
        grid.attach(scale_opacity, 1, 4, 1, 1)

        lbl_opacity_note = Gtk.Label()
        lbl_opacity_note.set_markup(
            "<span size='small' foreground='#f9a03f'>Braucht aktiven Compositor - sonst bleibt's opak.\n"
            "Gilt für Panel UND alle Widget-Fenster (gleiche Farbe/Transparenz).</span>"
        )
        lbl_opacity_note.set_xalign(0)
        grid.attach(lbl_opacity_note, 0, 5, 2, 1)

        grid.attach(Gtk.Label(label="Hintergrund Farbe:", xalign=0), 0, 6, 1, 1)
        color_btn_bg = Gtk.ColorButton.new_with_rgba(parse_rgba_str(self.config.get("bg_color")))
        grid.attach(color_btn_bg, 1, 6, 1, 1)

        grid.attach(Gtk.Label(label="Text Farbe:", xalign=0), 0, 7, 1, 1)
        color_btn_fg = Gtk.ColorButton.new_with_rgba(parse_rgba_str(self.config.get("fg_color")))
        grid.attach(color_btn_fg, 1, 7, 1, 1)

        grid.attach(Gtk.Label(label="Akzent Farbe:", xalign=0), 0, 8, 1, 1)
        color_btn_accent = Gtk.ColorButton.new_with_rgba(parse_rgba_str(self.config.get("accent_color")))
        grid.attach(color_btn_accent, 1, 8, 1, 1)

        grid.attach(Gtk.Label(label="Bildschirm-Ecken:", xalign=0), 0, 9, 1, 1)
        chk_corners = Gtk.CheckButton(label="Abrunden")
        chk_corners.set_active(self.config.get("screen_corners", False))
        grid.attach(chk_corners, 1, 9, 1, 1)

        lbl_corner_warning = Gtk.Label()
        lbl_corner_warning.set_markup(
            "<span size='small' foreground='#f9a03f'>Benötigt aktiven Compositor,\nsonst schwarzer Bildschirm!</span>"
        )
        lbl_corner_warning.set_xalign(0)
        grid.attach(lbl_corner_warning, 0, 10, 2, 1)

        grid.attach(Gtk.Label(label="Animationen:", xalign=0), 0, 11, 1, 1)
        chk_anim = Gtk.CheckButton(label="Aktiviert")
        chk_anim.set_active(self.config.get("animations", True))
        grid.attach(chk_anim, 1, 11, 1, 1)

        grid.attach(Gtk.Label(label="Animationsgeschwindigkeit:", xalign=0), 0, 12, 1, 1)
        scale_anim_speed = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 60, 500, 10)
        scale_anim_speed.set_value(self.config.get("animation_duration_ms", 200))
        scale_anim_speed.set_hexpand(True)
        scale_anim_speed.set_digits(0)
        scale_anim_speed.add_mark(60, Gtk.PositionType.BOTTOM, "Schnell")
        scale_anim_speed.add_mark(500, Gtk.PositionType.BOTTOM, "Langsam")
        grid.attach(scale_anim_speed, 1, 12, 1, 1)

        chk_grow = Gtk.CheckButton(label="Popups wachsen aus dem Panel-Button heraus (Caelestia-Stil)")
        chk_grow.set_active(self.config.get("popup_grow_effect", True))
        grid.attach(chk_grow, 0, 13, 2, 1)

        chk_pulse = Gtk.CheckButton(label="Angeklickter Button hüpft kurz beim Öffnen (Bounce-Effekt)")
        chk_pulse.set_active(self.config.get("panel_pulse_effect", True))
        grid.attach(chk_pulse, 0, 14, 2, 1)

        grid.attach(Gtk.Label(label="Ring-Dicke (Popup wächst wie ein Ring aus dem Panel):", xalign=0), 0, 15, 2, 1)
        scale_ring = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 30, 1)
        scale_ring.set_value(self.config.get("popup_ring_thickness", 10))
        scale_ring.set_hexpand(True)
        scale_ring.set_digits(0)
        grid.attach(scale_ring, 0, 16, 2, 1)

        grid.attach(Gtk.Label(label="Compositor:", xalign=0), 0, 17, 1, 1)
        comp_box = Gtk.Box(spacing=6)
        chk_compositor = Gtk.CheckButton(label="Aktiviert")
        chk_compositor.set_active(get_compositor_enabled())
        comp_status = Gtk.Label(label="(läuft)" if is_composited() else "(läuft nicht)")
        comp_box.pack_start(chk_compositor, False, False, 0)
        comp_box.pack_start(comp_status, False, False, 0)
        grid.attach(comp_box, 1, 17, 1, 1)

        btn_restart = Gtk.Button(label="Panel neu starten")
        btn_restart.set_tooltip_text(
            "Nötig, damit Transparenz/Rundungen greifen, nachdem der\nCompositor gerade erst aktiviert wurde."
        )
        btn_restart.connect("clicked", self.restart_panel)
        grid.attach(btn_restart, 0, 18, 2, 1)

        def toggle_compositor(chk):
            set_compositor_enabled(chk.get_active())

            def refresh_status():
                comp_status.set_text("(läuft)" if is_composited() else "(läuft nicht)")
                return False

            GLib.timeout_add(1000, refresh_status)

        chk_compositor.connect("toggled", toggle_compositor)

        # Grid in einen Scrollbereich packen, dessen Höhe sich am Bildschirm
        # orientiert - so wird das Fenster auf kleinen Displays nie zu groß,
        # sondern es scrollt stattdessen (siehe auch die Größenlogik unten).
        scrolled_appearance = Gtk.ScrolledWindow()
        scrolled_appearance.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_appearance.set_size_request(-1, appearance_scroll_height)
        scrolled_appearance.add(grid)
        notebook.append_page(scrolled_appearance, Gtk.Label(label="Aussehen"))

        # Tab 2: Widgets & Anordnung - jetzt mit 3 Sektionen (Links/Mitte/Rechts)
        box_w = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)

        sections_cfg = self.config.get("widget_sections", DEFAULT_CONFIG["widget_sections"])
        left_order = sections_cfg.get("left", []).copy()
        center_order = sections_cfg.get("center", []).copy()
        right_order = sections_cfg.get("right", []).copy()
        widgets_state = dict(self.config.get("widgets", {}))

        names_map = {
            "whisker": "Whisker Menue",
            "pinned": "Angepinnte Apps",
            "workspaces": "Arbeitsflächen",
            "taskbar": "Taskleiste",
            "media": "Medien-Steuerung",
            "sysmon": "CPU / RAM",
            "disk": "Festplatte",
            "net": "Netzwerk",
            "brightness": "Helligkeit",
            "volume": "Lautstaerke",
            "battery": "Akku Status",
            "notes": "Notizblock",
            "timer": "Timer",
            "screenshot": "Screenshot",
            "updates": "System-Updates",
            "weather": "Wetter",
            "systray": "System-Tray",
            "notifications": "Benachrichtigungscenter",
            "network_manager": "Netzwerk-Verwaltung",
            "bluetooth": "Bluetooth-Verwaltung",
            "clock": "Uhrzeit",
            "settings": "Einstellungen",
        }

        box_w.pack_start(
            Gtk.Label(label="Ziehen am \":::\"-Griff sortiert um, \"->\" verschiebt in eine andere Sektion:", xalign=0),
            False, False, 0,
        )

        sections_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, homogeneous=True)

        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        left_col.pack_start(Gtk.Label(label="Links"), False, False, 0)
        left_list = ReorderableWidgetList(left_order, names_map, widgets_state, [])
        left_col.pack_start(left_list, True, True, 0)
        sections_row.pack_start(left_col, True, True, 0)

        center_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        center_col.pack_start(Gtk.Label(label="Mitte"), False, False, 0)
        center_list = ReorderableWidgetList(center_order, names_map, widgets_state, [])
        center_col.pack_start(center_list, True, True, 0)
        sections_row.pack_start(center_col, True, True, 0)

        right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        right_col.pack_start(Gtk.Label(label="Rechts"), False, False, 0)
        right_list = ReorderableWidgetList(right_order, names_map, widgets_state, [])
        right_col.pack_start(right_list, True, True, 0)
        sections_row.pack_start(right_col, True, True, 0)

        # Jetzt, wo alle drei existieren, gegenseitig als "Verschieben nach"-Ziele eintragen.
        left_list.set_other_sections([("center", "Mitte", center_list), ("right", "Rechts", right_list)])
        center_list.set_other_sections([("left", "Links", left_list), ("right", "Rechts", right_list)])
        right_list.set_other_sections([("left", "Links", left_list), ("center", "Mitte", center_list)])

        box_w.pack_start(sections_row, True, True, 0)

        # Taskleisten-Feineinstellungen
        tb_opts = self.config.get("taskbar_options", {})
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box_w.pack_start(sep, False, False, 4)
        box_w.pack_start(Gtk.Label(label="Taskleiste:", xalign=0), False, False, 0)

        chk_tb_labels = Gtk.CheckButton(label="Fensternamen anzeigen (statt nur Icons)")
        chk_tb_labels.set_active(tb_opts.get("show_labels", True))
        box_w.pack_start(chk_tb_labels, False, False, 0)

        chk_tb_other_ws = Gtk.CheckButton(label="Fenster von anderen Workspaces anzeigen")
        chk_tb_other_ws.set_active(tb_opts.get("show_other_workspaces", True))
        box_w.pack_start(chk_tb_other_ws, False, False, 0)

        hbox_tb_click = Gtk.Box(spacing=6)
        hbox_tb_click.pack_start(Gtk.Label(label="Klick auf Fenster von anderem Workspace:"), False, False, 0)
        combo_tb_click = Gtk.ComboBoxText()
        combo_tb_click.append("switch", "Zu Workspace wechseln")
        combo_tb_click.append("bring", "Fenster hierher holen")
        combo_tb_click.set_active_id(tb_opts.get("click_action", "switch"))
        hbox_tb_click.pack_start(combo_tb_click, False, False, 0)
        box_w.pack_start(hbox_tb_click, False, False, 0)

        # System-Tray-Feineinstellungen
        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box_w.pack_start(sep2, False, False, 4)
        box_w.pack_start(Gtk.Label(label="System-Tray:", xalign=0), False, False, 0)
        lbl_tray_experimental = Gtk.Label()
        lbl_tray_experimental.set_markup(
            "<span size='small' foreground='#f9a03f'>Experimentell - kann bei manchen Tray-Apps "
            "Fenster wiederholt auf-/zuklappen lassen.\nStandardmäßig deaktiviert; nur bei Bedarf "
            "in \"Widgets &amp; Anordnung\" aktivieren und beobachten.</span>"
        )
        lbl_tray_experimental.set_xalign(0)
        box_w.pack_start(lbl_tray_experimental, False, False, 0)
        hbox_tray_size = Gtk.Box(spacing=6)
        hbox_tray_size.pack_start(Gtk.Label(label="Icon-Größe:"), False, False, 0)
        spin_tray_size = Gtk.SpinButton.new_with_range(12, 48, 2)
        spin_tray_size.set_value(self.config.get("systray_icon_size", 20))
        hbox_tray_size.pack_start(spin_tray_size, False, False, 0)
        box_w.pack_start(hbox_tray_size, False, False, 0)
        if not HAS_XLIB:
            lbl_tray_hint = Gtk.Label()
            lbl_tray_hint.set_markup(
                "<span size='small' foreground='#f9a03f'>Braucht 'python-xlib' (nicht installiert) - "
                "Tray bleibt sonst leer.</span>"
            )
            lbl_tray_hint.set_xalign(0)
            box_w.pack_start(lbl_tray_hint, False, False, 0)

        # Benachrichtigungs-Feineinstellungen
        sep3 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        box_w.pack_start(sep3, False, False, 4)
        box_w.pack_start(Gtk.Label(label="Benachrichtigungscenter:", xalign=0), False, False, 0)

        chk_notif_enabled = Gtk.CheckButton(label="Aktiviert (übernimmt den System-Benachrichtigungsdienst)")
        chk_notif_enabled.set_active(self.config.get("notifications_enabled", True))
        box_w.pack_start(chk_notif_enabled, False, False, 0)

        lbl_notif_warn = Gtk.Label()
        lbl_notif_warn.set_markup(
            "<span size='small' foreground='#f9a03f'>Ersetzt den bisherigen Benachrichtigungsdaemon "
            "(z.B. xfce4-notifyd).\nÄnderung wird erst nach 'Panel neu starten' wirksam.</span>"
        )
        lbl_notif_warn.set_xalign(0)
        box_w.pack_start(lbl_notif_warn, False, False, 0)

        ns_cfg = self.config.get("notification_settings", {})

        hbox_notif_pos = Gtk.Box(spacing=6)
        hbox_notif_pos.pack_start(Gtk.Label(label="Position:"), False, False, 0)
        combo_notif_pos = Gtk.ComboBoxText()
        for pid, plabel in (
            ("top-right", "Oben rechts"), ("top-left", "Oben links"),
            ("bottom-right", "Unten rechts"), ("bottom-left", "Unten links"),
        ):
            combo_notif_pos.append(pid, plabel)
        combo_notif_pos.set_active_id(ns_cfg.get("banner_position", "top-right"))
        hbox_notif_pos.pack_start(combo_notif_pos, False, False, 0)
        box_w.pack_start(hbox_notif_pos, False, False, 0)

        hbox_notif_dur = Gtk.Box(spacing=6)
        hbox_notif_dur.pack_start(Gtk.Label(label="Anzeigedauer (ms):"), False, False, 0)
        spin_notif_dur = Gtk.SpinButton.new_with_range(1000, 20000, 500)
        spin_notif_dur.set_value(ns_cfg.get("banner_duration_ms", 5000))
        hbox_notif_dur.pack_start(spin_notif_dur, False, False, 0)
        box_w.pack_start(hbox_notif_dur, False, False, 0)

        hbox_notif_hist = Gtk.Box(spacing=6)
        hbox_notif_hist.pack_start(Gtk.Label(label="Max. Verlauf:"), False, False, 0)
        spin_notif_hist = Gtk.SpinButton.new_with_range(10, 500, 10)
        spin_notif_hist.set_value(ns_cfg.get("max_history", 50))
        hbox_notif_hist.pack_start(spin_notif_hist, False, False, 0)
        box_w.pack_start(hbox_notif_hist, False, False, 0)

        chk_notif_dnd = Gtk.CheckButton(label="Nicht stören")
        chk_notif_dnd.set_active(ns_cfg.get("do_not_disturb", False))
        box_w.pack_start(chk_notif_dnd, False, False, 0)

        notebook.append_page(box_w, Gtk.Label(label="Widgets & Anordnung"))

        # Tab 3: Angepinnte Apps verwalten
        box_pin = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)
        pin_list = Gtk.ListBox()

        def refresh_pin_list():
            for c in pin_list.get_children():
                pin_list.remove(c)
            for app in self.config.get("pinned_apps", []):
                row = Gtk.ListBoxRow()
                hbox = Gtk.Box(spacing=6)
                hbox.pack_start(make_icon_image(app.get("icon")), False, False, 0)
                hbox.pack_start(Gtk.Label(label=app.get("name", "App"), xalign=0), True, True, 0)
                btn_del = Gtk.Button(label="Entfernen")

                def do_del(b, a=app):
                    try:
                        self.config["pinned_apps"].remove(a)
                    except ValueError:
                        pass
                    save_config(self.config)
                    self.apply_layout()
                    refresh_pin_list()

                btn_del.connect("clicked", do_del)
                hbox.pack_start(btn_del, False, False, 0)
                row.add(hbox)
                pin_list.add(row)
            pin_list.show_all()

        refresh_pin_list()
        scrolled_pin = Gtk.ScrolledWindow()
        scrolled_pin.set_size_request(240, min(150, appearance_scroll_height))
        scrolled_pin.add(pin_list)
        box_pin.pack_start(Gtk.Label(label="Über Rechtsklick auf ein Fenster in der\nTaskleiste oder eine App im Whisker-\nMenü kannst du neue Apps anpinnen."), False, False, 0)
        box_pin.pack_start(scrolled_pin, True, True, 0)

        notebook.append_page(box_pin, Gtk.Label(label="Angepinnt"))

        # Tab 4: Globale Tastenkürzel (über XFCE-Shortcuts umgesetzt)
        box_hk = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=8)
        box_hk.pack_start(
            Gtk.Label(
                label="Auf 'Aufnehmen' klicken, dann die gewünschte Tastenkombination drücken:",
                xalign=0,
            ),
            False, False, 0,
        )

        hotkeys_cfg = dict(self.config.get("hotkeys", {}))  # action_id -> accelerator-string
        capture_state = {"action": None, "grabbed": False}
        hotkey_rows = {}

        def start_keyboard_grab():
            win = popup.get_window()
            if not win:
                return
            seat = Gdk.Display.get_default().get_default_seat()
            try:
                status = seat.grab(win, Gdk.SeatCapabilities.KEYBOARD, True, None, None, None, None)
                capture_state["grabbed"] = (status == Gdk.GrabStatus.SUCCESS)
            except Exception:
                capture_state["grabbed"] = False

        def end_keyboard_grab():
            if capture_state["grabbed"]:
                try:
                    Gdk.Display.get_default().get_default_seat().ungrab()
                except Exception:
                    pass
                capture_state["grabbed"] = False

        def refresh_hotkey_row(action_id):
            lbl, btn_rec = hotkey_rows[action_id]
            if capture_state["action"] == action_id:
                lbl.set_text("Drücke eine Tastenkombination... (Esc = abbrechen)")
                btn_rec.set_label("...")
                return
            btn_rec.set_label("Aufnehmen")
            accel = hotkeys_cfg.get(action_id)
            if accel:
                try:
                    keyval, mods = Gtk.accelerator_parse(accel)
                    lbl.set_text(Gtk.accelerator_get_label(keyval, mods))
                except Exception:
                    lbl.set_text(accel)
            else:
                lbl.set_text("(nicht gesetzt)")

        listbox_hk = Gtk.ListBox()
        listbox_hk.set_selection_mode(Gtk.SelectionMode.NONE)

        for action_id, action_label in HOTKEY_ACTIONS:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(spacing=6)
            hbox.set_margin_top(2)
            hbox.set_margin_bottom(2)
            hbox.pack_start(Gtk.Label(label=action_label, xalign=0), True, True, 0)

            lbl_current = Gtk.Label(label="")
            hbox.pack_start(lbl_current, False, False, 0)

            btn_record = Gtk.Button(label="Aufnehmen")
            btn_clear = Gtk.Button(label="X")
            btn_clear.set_tooltip_text("Tastenkürzel löschen")

            def start_capture(_b, a=action_id):
                # Falls gerade eine andere Aufnahme läuft, deren Grab zuerst lösen.
                end_keyboard_grab()
                capture_state["action"] = a
                for aid, _ in HOTKEY_ACTIONS:
                    refresh_hotkey_row(aid)
                popup.grab_focus()
                # Echter Tastatur-Grab: verhindert, dass ein gerade fokussiertes
                # Eingabefeld im Dialog Tasten/Kombinationen vor uns abfängt -
                # das war der Grund, warum bisher oft nur Einzeltasten ankamen.
                start_keyboard_grab()

            def clear_hotkey(_b, a=action_id):
                hotkeys_cfg.pop(a, None)
                refresh_hotkey_row(a)

            btn_record.connect("clicked", start_capture)
            btn_clear.connect("clicked", clear_hotkey)
            hbox.pack_start(btn_record, False, False, 0)
            hbox.pack_start(btn_clear, False, False, 0)

            row.add(hbox)
            listbox_hk.add(row)
            hotkey_rows[action_id] = (lbl_current, btn_record)

        for action_id, _ in HOTKEY_ACTIONS:
            refresh_hotkey_row(action_id)
        listbox_hk.show_all()

        scrolled_hk = Gtk.ScrolledWindow()
        scrolled_hk.set_size_request(-1, min(220, appearance_scroll_height))
        scrolled_hk.add(listbox_hk)
        box_hk.pack_start(scrolled_hk, True, True, 0)

        lbl_hk_note = Gtk.Label()
        lbl_hk_note.set_markup(
            "<span size='small' foreground='#f9a03f'>Wird als globaler XFCE-Tastenkürzel registriert (xfconf) - "
            "überschreibt ggf.\neine bereits vorhandene Belegung dieser Kombination. Verschiebst du diese "
            "Datei später,\nmüssen die Kürzel neu gespeichert werden (Pfad wird beim Speichern fest hinterlegt).\n"
            "Kombinationen aus mehreren Tasten (z.B. Strg+Alt+V) werden voll unterstützt - "
            "während der Aufnahme\nhält das Panel die Tastatur fest, damit kein Eingabefeld dazwischenfunkt.</span>"
        )
        lbl_hk_note.set_xalign(0)
        box_hk.pack_start(lbl_hk_note, False, False, 0)

        def on_popup_key_press_for_hotkeys(widget, event):
            action = capture_state["action"]
            if action is None:
                return False
            if event.keyval == Gdk.KEY_Escape:
                capture_state["action"] = None
                end_keyboard_grab()
                refresh_hotkey_row(action)
                return True
            if event.is_modifier:
                return True  # nur ein Modifier gedrückt - noch warten
            mods = event.state & Gtk.accelerator_get_default_mod_mask()
            accel = Gtk.accelerator_name(event.keyval, mods)
            if accel:
                # Diese Kombination ggf. von einer anderen Aktion entfernen,
                # damit nicht zwei Aktionen denselben Shortcut haben.
                for other_id, other_accel in list(hotkeys_cfg.items()):
                    if other_accel == accel and other_id != action:
                        hotkeys_cfg.pop(other_id, None)
                        refresh_hotkey_row(other_id)
                hotkeys_cfg[action] = accel
            capture_state["action"] = None
            end_keyboard_grab()
            refresh_hotkey_row(action)
            return True

        popup.connect("key-press-event", on_popup_key_press_for_hotkeys)
        popup.connect("destroy", lambda w: end_keyboard_grab())

        notebook.append_page(box_hk, Gtk.Label(label="Hotkeys"))

        btn_save = Gtk.Button(label="Speichern")

        def save_and_apply(x):
            self.config["position"] = combo_pos.get_active_text()
            self.config["panel_size"] = int(spin_size.get_value())
            self.config["panel_radius"] = int(spin_p_radius.get_value())
            self.config["bg_color"] = rgba_to_css_str(color_btn_bg.get_rgba())
            self.config["fg_color"] = rgba_to_css_str(color_btn_fg.get_rgba())
            self.config["accent_color"] = rgba_to_css_str(color_btn_accent.get_rgba())
            self.config["screen_corners"] = chk_corners.get_active()
            self.config["animations"] = chk_anim.get_active()
            self.config["animation_duration_ms"] = int(scale_anim_speed.get_value())
            self.config["popup_grow_effect"] = chk_grow.get_active()
            self.config["panel_pulse_effect"] = chk_pulse.get_active()
            self.config["popup_ring_thickness"] = int(scale_ring.get_value())
            self.config["popup_radius"] = int(spin_popup_radius.get_value())
            self.config["panel_opacity"] = int(scale_opacity.get_value())
            self.config["widget_sections"] = {
                "left": left_list.order_list,
                "center": center_list.order_list,
                "right": right_list.order_list,
            }
            self.config["widgets"] = widgets_state
            self.config["taskbar_options"] = {
                "show_labels": chk_tb_labels.get_active(),
                "show_other_workspaces": chk_tb_other_ws.get_active(),
                "click_action": combo_tb_click.get_active_id() or "switch",
            }
            self.config["systray_icon_size"] = int(spin_tray_size.get_value())
            self.config["notifications_enabled"] = chk_notif_enabled.get_active()
            self.config["notification_settings"] = {
                "banner_position": combo_notif_pos.get_active_id() or "top-right",
                "banner_duration_ms": int(spin_notif_dur.get_value()),
                "max_history": int(spin_notif_hist.get_value()),
                "do_not_disturb": chk_notif_dnd.get_active(),
            }

            old_hotkeys = self.config.get("hotkeys", {})
            for action_id, old_accel in old_hotkeys.items():
                if old_accel and old_accel != hotkeys_cfg.get(action_id):
                    remove_xfce_hotkey(old_accel)
            for action_id, accel in hotkeys_cfg.items():
                if accel:
                    set_xfce_hotkey(accel, get_hotkey_command(action_id))
            self.config["hotkeys"] = hotkeys_cfg

            save_config(self.config)
            popup.close_popup()
            self.apply_layout()

        btn_save.connect("clicked", save_and_apply)

        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        main_box.pack_start(notebook, True, True, 0)
        main_box.pack_start(btn_save, False, False, 0)

        popup.add(main_box)
        self._register_popup("settings", popup)
        popup.show_adjacent()

    def launch_sysmon(self, button):
        term = shutil.which("xfce4-terminal") or shutil.which("x-terminal-emulator")
        cmd = shutil.which("btop") or shutil.which("htop") or shutil.which("top")
        if term and cmd:
            subprocess.Popen([term, "-e", cmd])

    def update_loop(self):
        is_vert = self.config.get("position") in ["left", "right"]
        cfg = self.config.get("widgets", {})

        if cfg.get("sysmon", True) and getattr(self, "lbl_sysmon", None):
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
            self.lbl_sysmon.set_text(f"CPU {cpu}%\nRAM {ram}%" if is_vert else f"CPU: {cpu}%  RAM: {ram}%")
            if hasattr(self, "btn_sysmon"):
                self.btn_sysmon.set_tooltip_text(f"CPU: {cpu}%  RAM: {ram}%")
        elif cfg.get("sysmon", True) and hasattr(self, "btn_sysmon"):
            cpu = psutil.cpu_percent()
            ram = psutil.virtual_memory().percent
            self.btn_sysmon.set_tooltip_text(f"CPU: {cpu}%  RAM: {ram}%\n(Rechtsklick: Icon/Text umschalten)")

        if cfg.get("disk", True) and getattr(self, "lbl_disk", None):
            disk = psutil.disk_usage("/").percent
            self.lbl_disk.set_text(f"DISK\n{disk}%" if is_vert else f"DISK: {disk}%")

        if cfg.get("net", True) and getattr(self, "lbl_net", None):
            net_now = psutil.net_io_counters().bytes_recv
            speed_kb = (net_now - self.last_net_bytes) / 1024
            self.last_net_bytes = net_now
            self.lbl_net.set_text(f"NET\n{int(speed_kb)}K" if is_vert else f"NET: {speed_kb:.0f} KB/s")
        elif cfg.get("net", True):
            net_now = psutil.net_io_counters().bytes_recv
            self.last_net_bytes = net_now

        if cfg.get("battery", True) and getattr(self, "lbl_bat", None):
            bat = psutil.sensors_battery()
            if bat:
                charge_icon = "+" if bat.power_plugged else ""
                pct = f"{int(bat.percent)}%{charge_icon}"
                self.lbl_bat.set_text(f"BAT\n{pct}" if is_vert else f"BAT: {pct}")
            else:
                self.lbl_bat.set_text("N/A" if is_vert else "BAT: N/A")

        if self.timer_running and self.timer_seconds > 0:
            self.timer_seconds -= 1
            m, s = divmod(self.timer_seconds, 60)
            t = f"{m:02d}:{s:02d}"
            if getattr(self, "lbl_timer", None):
                self.lbl_timer.set_text(t)
            if self.timer_seconds == 0:
                self.timer_running = False
                if getattr(self, "lbl_timer", None):
                    self.lbl_timer.set_text("FERTIG")

        if cfg.get("clock", True) and getattr(self, "lbl_clock", None):
            self.lbl_clock.set_text(time.strftime("%H\n%M") if is_vert else time.strftime("%H:%M:%S"))

        # Selbstheilung: falls sich der Compositor-Status ändert, das Ecken-
        # Overlay sofort korrekt an-/abschalten statt Blackscreen zu riskieren.
        composited_now = is_composited()
        if composited_now != self._last_composited_state:
            self._last_composited_state = composited_now
            if self.config.get("screen_corners", False):
                if composited_now and self.corners_overlay is None:
                    self.corners_overlay = ScreenCornersOverlay(self.config.get("corner_radius", 16))
                    self.corners_overlay.show_all()
                elif not composited_now and self.corners_overlay is not None:
                    self.corners_overlay.hide()

        return True


if __name__ == "__main__":
    win = MiniPanel()
    win.connect("destroy", Gtk.main_quit)
    Gtk.main()
