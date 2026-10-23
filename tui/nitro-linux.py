#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""nitro-linux - single-screen control panel for the nitro daemon.

Interactive TUI.

? shows the full legend
R re-applies the saved state
q / Esc / Ctrl+Q close the UI
Q stops daemon

keys
    fans        A Auto, S Max, D Custom     custom:  (. /) cpu -+5, (; ') gpu -+5
                i CoolBoost (Auto only)
    power       Z X C V B                   power plan
    thermal     F                           switch °C / °F

    keyboard    Static / Dynamic            switches the display and loads it
                static:  1-4 zone, enter colour, x on/off
                dynamic: 1-5 effect, t/T speed, f direction
                brightness (l/L), picker (Enter/#), click swatches

    refresh     - faster, + slower (100 ms per step)
"""
import curses
import json
import os
import socket
import subprocess
import sys
import syslog
import time
from collections import deque

VERSION = "2.3.0"


def project_version():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (
        os.path.join(here, "VERSION"),
        os.path.join(here, os.pardir, "VERSION")):
        try:
            with open(p) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    return VERSION


PROJECT_VERSION = project_version()

for _model_path in (
    "/usr/local/lib/nitro",
    "/usr/lib/nitro",
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "tools"),
):
    if os.path.isdir(_model_path) and _model_path not in sys.path:
        sys.path.insert(0, _model_path)

from model import (
    QUIRKS,
    get_zones,
    get_effects,
    get_effect_names,
    CAP_FAN,
    CAP_FAN_MODE,
    CAP_HWMON,
    CAP_PLATFORM_PROFILE,
    CAP_RGB,
    CAP_RGB_DYNAMIC,
    CAP_COOLBOOST,
    CAP_LED_CLASS,
)

SOCK = os.environ.get("NITRO_SOCK", "/run/nitro.sock")
CFG = os.environ.get("NITRO_TUI_CFG", os.path.expanduser("~/.config/nitro/tui.json"))
ZONE_KEYS = get_zones(QUIRKS)
EFFECTS = get_effects(QUIRKS)
EFFECT_NAMES = get_effect_names(QUIRKS)
EFFECT_NEEDS = {
    1: ("speed", "bright", "colour"),
    2: ("speed", "bright"),
    3: ("speed", "bright", "direction"),
    4: ("speed", "bright", "colour", "direction"),
    5: ("speed", "bright", "colour"),
}
EFFECT_NEEDS = {k: v for k, v in EFFECT_NEEDS.items() if k in EFFECTS}
PLANS = [
    ("Power Saver", "low-power"),
    ("Quiet", "quiet"),
    ("Balance", "balanced"),
    ("Balance [Acer Optimized]", "balanced-performance"),
    ("High-Performance", "performance"),
]
PLAN_KEYS = "ZXCVB"
FAN_MODES = (("auto", "Auto"), ("max", "Max"), ("custom", "Custom"))
FAN_KEYS = "ASD"
SWATCHES = [
    (255, 0, 0), (255, 128, 0), (255, 255, 0), (0, 255, 0), (0, 255, 128),
    (0, 255, 255), (0, 128, 255), (128, 0, 255), (255, 0, 255), (255, 255, 255),
]
REFRESH_MIN, REFRESH_MAX, REFRESH_STEP = 100, 5000, 100
PLAN_NAMES = dict((val, name) for name, val in PLANS)


def log_unavailable(feature: str, reason: str = "capability missing"):
    try:
        syslog.syslog(syslog.LOG_INFO, f"nitro-linux: {feature} unavailable - {reason}")
    except Exception:
        pass


def clamp_refresh(ms):
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        ms = 500
    ms = int(round(ms / float(REFRESH_STEP))) * REFRESH_STEP
    return max(REFRESH_MIN, min(REFRESH_MAX, ms))

THEME = {
    "fg": "#ece0d9",
    "dim": "#958a80",
    "acc": "#e8563f",
    "ok": "#b8d97a",
    "mid": "#ffb878",
    "hot": "#ff5f5f",
}


def hex_to_256(h):
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    if r == g == b:
        return 232 + min(23, r * 24 // 256)
    return 16 + min(5, r * 6 // 256) * 36 + min(5, g * 6 // 256) * 6 + min(5, b * 6 // 256)


class C:
    FG = 1
    DIM = 2
    ACC = 3
    OK = 4
    MID = 5
    HOT = 6
    G_OK = 7
    G_B = 8


_pair_cache = {}
_next_pair = [10]


def init_theme():
    curses.start_color()
    curses.use_default_colors()
    if curses.COLORS >= 256:
        for pair, key in (
            (C.FG, "fg"), (C.DIM, "dim"), (C.ACC, "acc"),
            (C.OK, "ok"), (C.MID, "mid"), (C.HOT, "hot")):
            try:
                curses.init_pair(pair, hex_to_256(THEME[key]), -1)
            except curses.error:
                pass
        for pair, hexc in ((C.G_OK, "#c6cc7a"), (C.G_B, "#a9c98f")):
            try:
                curses.init_pair(pair, hex_to_256(hexc), -1)
            except curses.error:
                pass
    else:
        for pair, col in (
            (C.FG, curses.COLOR_WHITE), (C.DIM, curses.COLOR_BLACK),
            (C.ACC, curses.COLOR_RED), (C.OK, curses.COLOR_GREEN),
            (C.MID, curses.COLOR_YELLOW), (C.HOT, curses.COLOR_RED)):
            try:
                curses.init_pair(pair, col, -1)
            except curses.error:
                pass
        for pair, col in ((C.G_OK, curses.COLOR_GREEN), (C.G_B, curses.COLOR_GREEN)):
            try:
                curses.init_pair(pair, col, -1)
            except curses.error:
                pass


def _quant(r, g, b):
    if curses.COLORS >= 256:
        return hex_to_256(f"#{r:02x}{g:02x}{b:02x}")
    return (curses.COLOR_WHITE if max(r, g, b) < 64 else
            curses.COLOR_RED if r >= g and r >= b else
            curses.COLOR_GREEN if g >= r and g >= b else curses.COLOR_BLUE)


def rgb_attr(r, g, b, bold=False):
    key = (r // 32, g // 32, b // 32, "fg")
    if key not in _pair_cache:
        col = _quant(r, g, b)
        p = _next_pair[0]
        if p > 250:
            return curses.color_pair(C.FG)
        _next_pair[0] += 1
        try:
            curses.init_pair(p, col, -1)
        except curses.error:
            return curses.color_pair(C.FG)
        _pair_cache[key] = p
    return curses.color_pair(_pair_cache[key]) | (curses.A_BOLD if bold else 0)


def rgb_bg(r, g, b, fg=None, bold=False):
    key = (r // 32, g // 32, b // 32, fg)
    if key not in _pair_cache:
        col = _quant(r, g, b)
        p = _next_pair[0]
        if p > 250:
            return curses.color_pair(C.FG)
        _next_pair[0] += 1
        try:
            curses.init_pair(p, col if fg is None else fg, col)
        except curses.error:
            return curses.color_pair(C.FG)
        _pair_cache[key] = p
    return curses.color_pair(_pair_cache[key]) | (curses.A_BOLD if bold else 0)


GREY = (150, 150, 150)


def grey_fill():
    return rgb_bg(*GREY)


SHIFT_ENTER = 0x110000


def rpc(msg, timeout=3.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode())
    finally:
        s.close()


def braille(vals, width, height, anchor="bottom"):
    if width <= 0 or height <= 0:
        return []
    vals = [v for v in vals if v is not None][-width * 2:]
    if not vals:
        return [" " * width] * height
    if anchor == "bottom":
        L, R = (0x40, 0x04, 0x02, 0x01), (0x80, 0x20, 0x10, 0x08)
    else:
        L, R = (0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80)
    lo, hi = min(vals), max(vals)
    span = max(hi - lo, 0.05 * max(abs(hi), abs(lo), 1.0))
    levels = [max(1, min(height * 4, int(round((v - lo) / span * height * 4)))) for v in vals]
    levels = [0] * (width * 2 - len(levels)) + levels
    rows = []
    for r in range(height):
        line = ""
        for c in range(width):
            lv = levels[2 * c] if 2 * c < len(levels) else 0
            rv = levels[2 * c + 1] if 2 * c + 1 < len(levels) else 0
            if anchor == "bottom":
                dl = min(4, max(0, lv - (height - 1 - r) * 4))
                dr = min(4, max(0, rv - (height - 1 - r) * 4))
            else:
                dl = min(4, max(0, lv - r * 4))
                dr = min(4, max(0, rv - r * 4))
            m = sum(L[:dl]) + sum(R[:dr])
            line += chr(0x2800 + m) if m else " "
        rows.append(line)
    return rows


def temp_color(t, warn=80, crit=95):
    if t is None:
        return C.FG
    if t >= crit:
        return C.HOT
    if t >= warn:
        return C.MID
    return C.OK


def fmt_temp(t, fahrenheit):
    if t is None:
        return "--"
    v = t * 9.0 / 5.0 + 32.0 if fahrenheit else t
    return f"{v:.1f}°{'F' if fahrenheit else 'C'}"


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def hexname(r, g, b):
    return f"#{r:02X}{g:02X}{b:02X}"


def parse_fan_modes(text):
    out = {"cpu": None, "gpu": None}
    for part in (text or "").split():
        k, _, v = part.partition("=")
        if k in out and v in ("auto", "max", "custom"):
            out[k] = v
    return out


def load_cfg():
    d = {}
    try:
        with open(CFG) as f:
            d = json.load(f)
    except (OSError, ValueError):
        pass
    return {"refresh_ms": clamp_refresh(d.get("refresh_ms", 500)),
            "temp_f": d.get("temp_unit") == "F"}


def save_cfg(ms, temp_f):
    try:
        os.makedirs(os.path.dirname(CFG), exist_ok=True)
        tmp = CFG + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"refresh_ms": ms, "temp_unit": "F" if temp_f else "C"}, f)
        os.replace(tmp, CFG)
    except OSError:
        pass


class UI:
    def __init__(self, stdscr):
        self.s = stdscr
        self.snap = {"data": {}, "desired": {}, "source": None, "daemon": {}}
        self.series = deque(maxlen=800)
        self.plan_tab = None
        self.last_press = 0.0
        self.help_t = 0.0
        self.resize_t = 0.0
        cfg = load_cfg()
        self.refresh_ms = cfg["refresh_ms"]
        self.temp_f = cfg["temp_f"]
        self.sel_zone = 0
        self.editor = None
        self.msg = ""
        self.msg_time = 0.0
        self.confirm_stop = False
        self.show_help = False
        self.clicks = []
        self.eclicks = []
        self.swatches = []
        self._mx = 0
        self._my = 0

    def addstr(self, y, x, text, attr=0):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x >= w or not text:
            return
        try:
            self.s.addstr(y, x, text[:max(0, w - x)], attr)
        except curses.error:
            pass

    def click(self, y, x, h, w, action):
        self.clicks.append((y, x, h, w, action))

    def divider(self, y, x, wbox, label, right=None):
        iw = wbox - 4
        if iw < len(label) + 2:
            return
        self.addstr(y, x + 2, "─" * iw, curses.color_pair(C.DIM))
        lx = x + 2 + (iw - len(label)) // 2
        self.addstr(y, lx, label, curses.color_pair(C.DIM) | curses.A_BOLD)
        if right:
            rx = x + 2 + iw
            for text, action in reversed(right):
                rx -= len(text)
                if rx <= lx + len(label):
                    break
                self.addstr(y, rx - 1, " " * (len(text) + 2), curses.color_pair(C.DIM))
                self.addstr(y, rx, text, curses.color_pair(C.ACC))
                self.click(y, rx, 1, len(text), action)
                rx -= 2

    def at_click(self, my, mx):
        for lst in (self.eclicks, self.clicks):
            for y, x, h, w, action in lst:
                if y <= my < y + h and x <= mx < x + w:
                    return action
        return None

    def box(self, y, x, h, w, title="", active=False):
        if w < 4 or h < 3:
            return
        bp = curses.color_pair(C.ACC) | curses.A_BOLD if active else curses.color_pair(C.DIM)
        self.addstr(y, x, "╭" + "─" * (w - 2) + "╮", bp)
        if title:
            t = f" {title} "[:max(0, w - 3)]
            ta = curses.color_pair(C.ACC) | curses.A_BOLD if active else curses.color_pair(C.FG)
            self.addstr(y, x + 1, "─", bp)
            self.addstr(y, x + 2, t, ta)
            self.addstr(y, x + 2 + len(t), "─" * max(0, w - 3 - len(t)), bp)
        for i in range(1, h - 1):
            self.addstr(y + i, x, "│", bp)
            self.addstr(y + i, x + w - 1, "│", bp)
        self.addstr(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", bp)

    def chip(self, y, x, text, attr, action=None):
        self.addstr(y, x, text, attr)
        if action is not None:
            self.click(y, x, 1, len(text), action)
        return x + len(text)

    def slider(self, y, x, width, frac, attr):
        inner = max(4, width - 2)
        pos = int(round(max(0.0, min(1.0, frac)) * (inner - 1)))
        self.addstr(y, x, "├" + "━" * pos + "●" + "─" * (inner - pos - 1) + "┤", attr)
        return x + 1, inner

    def button(self, y, x, w, label, active=False, action=None, h=3):
        if w < 4 or h < 3:
            return y + h
        bp = curses.color_pair(C.ACC) | curses.A_BOLD if active else curses.color_pair(C.DIM)
        lp = curses.color_pair(C.ACC) | curses.A_BOLD if active else curses.color_pair(C.FG)
        self.addstr(y, x, "╭" + "─" * (w - 2) + "╮", bp)
        for i in range(1, h - 1):
            self.addstr(y + i, x, "│" + " " * (w - 2) + "│", bp)
        self.addstr(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", bp)
        lab = label[:max(0, w - 2)]
        self.addstr(y + (h - 1) // 2, x + max(1, (w - len(lab)) // 2), lab, lp)
        if action is not None:
            self.click(y, x, h, w, action)
        return y + h

    def seg_tabs(self, y, x, w, items, cur):
        acc = curses.color_pair(C.ACC) | curses.A_BOLD
        dim = curses.color_pair(C.DIM)
        fg = curses.color_pair(C.FG)
        (k0, t0, a0), (k1, t1, a1) = items[0], items[1]
        s0, s1 = k0 == cur, k1 == cur
        w1 = w // 2 + 1
        w2 = w + 1 - w1
        xd = x + w1 - 1
        e0 = acc if s0 else dim
        e1 = acc if s1 else dim
        l0 = acc if s0 else fg
        l1 = acc if s1 else fg
        self.addstr(y, x, "╭" + "─" * max(0, w1 - 2), e0)
        self.addstr(y, xd, "┬", acc)
        self.addstr(y, x + w1, "─" * max(0, w2 - 2), e1)
        self.addstr(y, x + w - 1, "╮", e1)
        self.addstr(y + 1, x, "│", e0)
        self.addstr(y + 1, x + 1 + max(0, (w1 - 2 - len(t0)) // 2), t0, l0)
        self.addstr(y + 1, xd, "│", acc)
        self.addstr(y + 1, x + w1 + max(0, (w2 - 2 - len(t1)) // 2), t1, l1)
        self.addstr(y + 1, x + w - 1, "│", e1)
        self.addstr(y + 2, x, "╰" + "─" * max(0, w1 - 2), e0)
        self.addstr(y + 2, xd, "┴", acc)
        self.addstr(y + 2, x + w1, "─" * max(0, w2 - 2), e1)
        self.addstr(y + 2, x + w - 1, "╯", e1)
        self.click(y, x, 3, w1, a0)
        self.click(y, xd, 3, w2, a1)

    def swatch(self, y, x, w, r, g, b, sel, highlight=False):
        if w < 1:
            return
        self.swatches.append((y, x, w))
        a = rgb_bg(r, g, b)
        mid = w // 2
        if not sel and not highlight:
            self.addstr(y, x, " " * w, a)
            return
        self.addstr(y, x, " " * mid, a)
        star = (curses.COLOR_BLACK if 0.299 * r + 0.587 * g + 0.114 * b >= 128
                else curses.COLOR_WHITE)
        if highlight:
            sym = "◉" if sel else "○"
        else:
            sym = "●"
        self.addstr(y, x + mid, sym, rgb_bg(r, g, b, fg=star))
        self.addstr(y, x + mid + 1, " " * (w - mid - 1), a)

    def rich(self, y, x, text, base, maxw=None):
        keyattr = curses.color_pair(C.ACC) | curses.A_BOLD
        cx = x
        for i, part in enumerate(text.split("*")):
            if not part:
                continue
            attr = keyattr if i % 2 else base
            if maxw is not None and cx - x + len(part) > maxw:
                part = part[:max(0, maxw - (cx - x))]
            self.addstr(y, cx, part, attr)
            cx += len(part)
            if maxw is not None and cx - x >= maxw:
                break
        return cx

    def tips(self, y, x, ylim, lines, maxw):
        if y + len(lines) > ylim:
            return y
        base = curses.color_pair(C.DIM)
        for i, ln in enumerate(lines):
            self.rich(y + i, x, ln, base, maxw=maxw)
        return y + len(lines)

    @property
    def live(self):
        return self.snap.get("data", {}) or {}

    @property
    def wanted(self):
        return self.snap.get("desired", {}) or {}

    @property
    def source(self):
        return self.snap.get("source")

    def plan_src(self):
        return self.plan_tab or ("battery" if self.source == "battery" else "ac")

    def open_help(self):
        self.show_help = True
        self.help_t = time.time()

    def open_url(self, url):
        try:
            subprocess.Popen(
                ["xdg-open", url], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            self.msg = f"» opening {url}"
        except OSError as ex:
            self.msg = f"!! {ex}"
        self.msg_time = time.time()

    def kbd_mode(self):
        m = self.wanted.get("mode")
        return m if m in ("static", "dynamic") else "static"

    def fan_sel(self):
        fm = parse_fan_modes(self.live.get("fan_mode"))
        if fm["cpu"] and (QUIRKS.fan_count < 2 or fm["cpu"] == fm["gpu"]):
            return fm["cpu"]
        return None

    def state_fan_mode(self, which):
        fm = self.wanted.get("fan_mode") or {}
        return fm.get(which)

    def coolboost_live(self):
        v = self.live.get("coolboost")
        if v is None:
            return None
        return str(v).strip() in ("1", "on", "true")

    def toggle_coolboost(self):
        if self.fan_sel() != "auto":
            self.msg = "» CoolBoost is only available in Auto mode"
            self.msg_time = time.time()
            return
        on = self.coolboost_live()
        if on is None:
            self.msg = "!! CoolBoost unavailable (driver 2.1.0 required)"
            self.msg_time = time.time()
            return
        self.send(
            {"cmd": "set", "key": "coolboost", "value": not on},
            f"CoolBoost → {'ON' if not on else 'OFF'}")

    def duty(self, which):
        try:
            v = self.live.get(f"fan_{which}_duty")
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def rpm(self, which):
        try:
            v = self.live.get(f"{which}_rpm")
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def state_zone(self, z):
        st = self.wanted.get("static") or {}
        v = st.get(str(z))
        if isinstance(v, dict):
            rgb = v.get("rgb") or [0, 0, 0]
            return tuple(int(x) for x in rgb[:3]), bool(v.get("on"))
        if isinstance(v, list) and len(v) == 3:
            rgb = tuple(int(x) for x in v)
            return rgb, any(rgb)
        return (0, 0, 0), False

    def dyn(self):
        d = self.wanted.get("dynamic") or {}
        try:
            cur = int(d.get("mode", 1))
        except (TypeError, ValueError):
            cur = 1
        if cur not in EFFECTS:
            cur = 1
        effs = {}
        table = d.get("effects") if isinstance(d.get("effects"), dict) else {}
        for m in EFFECTS:
            e = table.get(str(m)) or {}
            effs[m] = {
                "speed": int(e.get("speed", 4)),
                "direction": int(e.get("direction", 1)),
                "r": int(e.get("r", 255)), "g": int(e.get("g", 0)), "b": int(e.get("b", 255)),
            }
        return {"mode": cur, "effects": effs}

    def brightness(self):
        try:
            return max(0, min(100, int(self.wanted.get("brightness", 100))))
        except (TypeError, ValueError):
            return 100

    def state_plan(self):
        src = self.source
        key = "profile_battery" if src == "battery" else "profile_ac"
        v = self.wanted.get(key)
        if v in [p[1] for p in PLANS]:
            return v
        return None

    def send(self, msg, note):
        try:
            r = rpc(msg)
            if r.get("ok"):
                if isinstance(r.get("desired"), dict):
                    self.snap = r
                self.msg = f"» {note}"
            else:
                self.msg = f"!! {r.get('err', 'failed')}"
            self.msg_time = time.time()
        except OSError as e:
            self.msg = f"!! daemon: {e}"
            self.msg_time = time.time()

    def set_fan_mode(self, mode):
        self.send({"cmd": "set", "key": "fan_mode", "value": mode}, f"fans → {mode}")

    def set_duty(self, which, val):
        if self.state_fan_mode(which) != "custom":
            self.msg = "» set the fans to custom first  (D)"
            self.msg_time = time.time()
            return
        val = max(0, min(100, int(val)))
        self.send(
            {"cmd": "set", "key": f"fan_{which}", "value": val},
            f"{which} fan duty → {val}%")

    def set_plan(self, i):
        name, val = PLANS[i]
        key = "profile_battery" if self.plan_src() == "battery" else "profile_ac"
        self.send(
            {"cmd": "set", "key": key, "value": val},
            f"{key[8:]} plan → {name}")

    def set_kbd_mode(self, mode):
        self.send(
            {"cmd": "set", "key": "mode", "value": mode},
            f"keyboard → {mode}")

    def set_effect(self, m):
        self.send(
            {"cmd": "set", "key": "dynamic", "mode": m},
            f"effect → {EFFECT_NAMES[m]}")

    def set_dyn(self, note, **kw):
        self.send({"cmd": "set", "key": "dynamic", **kw}, note)

    def set_brightness(self, val):
        val = max(0, min(100, int(val)))
        self.send(
            {"cmd": "set", "key": "brightness", "value": val},
            f"brightness → {val}")

    def set_zone_color(self, z, r, g, b):
        self.send(
            {"cmd": "set", "key": "static", "zone": z, "r": r, "g": g, "b": b},
            f"zone Z{ZONE_KEYS.index(z) + 1} → {hexname(r, g, b)}")

    def toggle_zone(self, i):
        z = ZONE_KEYS[i]
        _, on = self.state_zone(z)
        self.send(
            {"cmd": "set", "key": "static_on", "zone": z, "on": not on},
            f"zone Z{i + 1} → {'off' if on else 'on'}")

    def set_refresh(self, step):
        ms = clamp_refresh(self.refresh_ms + step * REFRESH_STEP)
        if ms != self.refresh_ms:
            self.refresh_ms = ms
            save_cfg(self.refresh_ms, self.temp_f)

    def topbar(self):
        w = self.w
        self.addstr(0, 1, "N I T R O - L I N U X", curses.color_pair(C.ACC) | curses.A_BOLD)
        x = 23
        if QUIRKS.probe:
            self.addstr(
                0, x, "PROBE / READ-ONLY",
                curses.color_pair(C.HOT) | curses.A_BOLD)
            x += 18
        if self.live.get("driver") not in (None, "ok"):
            self.addstr(0, x, "driver missing", curses.color_pair(C.HOT) | curses.A_BOLD)
        if self.msg:
            txt = f" {self.msg} "
            tx = max(x + 16, (w - len(txt)) // 2)
            attr = (rgb_attr(255, 95, 95, bold=True) if self.msg.startswith("!!")
                    else rgb_attr(184, 217, 122, bold=True))
            self.addstr(0, tx, txt[:max(0, w - tx - 18)], attr)
        grp = f"- {self.refresh_ms}ms +"
        xg = w - 2 - len(grp)
        xq = xg - 4
        self.addstr(0, xq, " ? ", curses.color_pair(C.DIM) | curses.A_BOLD)
        self.click(0, xq, 1, 3, ("help",))
        self.addstr(0, xg, grp, curses.color_pair(C.DIM) | curses.A_BOLD)
        self.addstr(0, xg + 2, f"{self.refresh_ms}ms", curses.color_pair(C.FG) | curses.A_BOLD)
        self.click(0, xg, 1, 2, ("refresh", -1))
        self.click(0, xg + len(grp) - 2, 1, 2, ("refresh", 1))

    def confirm_modal(self):
        h, w = self.h, self.w
        txt = "stop daemon?   y / n"
        bw = len(txt) + 4
        y0, x0 = max(0, h // 2 - 1), max(0, w // 2 - bw // 2)
        for i in range(3):
            self.addstr(y0 + i, x0, " " * bw, 0)
        self.box(y0, x0, 3, bw, "", active=True)
        self.addstr(y0 + 1, x0 + 2, txt, curses.color_pair(C.FG) | curses.A_BOLD)

    def draw(self):
        s = self.s
        s.erase()
        self.clicks = []
        self.eclicks = []
        self.swatches = []
        h, w = s.getmaxyx()
        if w < 120 or h < 47:
            self.need_msg(120, 47)
            return
        self.h, self.w = h, w
        ym = 1 + (h - 1) // 2
        lw = max(34, min(38, w // 3))
        rw = w - 1 - lw
        w2 = max(46, rw * 11 // 20)
        w2 = min(w2, max(30, rw - 27))
        w1 = rw - 1 - w2
        ht = ym - 1
        hb = h - ym
        has_fan = QUIRKS.has_cap(CAP_FAN)
        has_fan_mode = QUIRKS.has_cap(CAP_FAN_MODE)
        has_profile = QUIRKS.has_cap(CAP_PLATFORM_PROFILE)
        has_thermal = QUIRKS.has_cap(CAP_HWMON)
        has_rgb = QUIRKS.has_cap(CAP_RGB)
        has_dyn = QUIRKS.has_cap(CAP_RGB_DYNAMIC)
        has_cool = QUIRKS.has_cap(CAP_COOLBOOST)

        self.box(1, 0, ht, lw, "FAN CONTROL")
        self.box(1, lw + 1, ht, rw, "FAN MONITOR")

        self.box(ym, 0, hb, lw, "POWER PLAN")

        right_x = lw + 1
        right_w = rw
        w2 = max(46, right_w * 11 // 20)
        w2 = min(w2, max(30, right_w - 27))
        w1 = right_w - 1 - w2
        self.box(ym, right_x, hb, w1, "THERMAL")
        self.box(ym, right_x + 1 + w1, hb, w2, "KEYBOARD")

        self.draw_fan_control(1, 0, ht, lw)
        self.draw_fan_monitor(1, lw + 1, ht, rw)
        self.draw_power(ym, 0, hb, lw)
        self.draw_thermal(ym, right_x, hb, w1)
        self.draw_keyboard(ym, right_x + 1 + w1, hb, w2)

        if not hasattr(self, '_unavailable_logged'):
            self._unavailable_logged = True
            if not has_fan:
                log_unavailable("Fan Control", "CAP_FAN missing")
            if not has_fan_mode:
                log_unavailable("Fan Monitor", "CAP_FAN_MODE missing")
            if not has_profile:
                log_unavailable("Power Plan", "CAP_PLATFORM_PROFILE missing")
            if not has_thermal:
                log_unavailable("Thermal Monitoring", "CAP_HWMON missing")
            if not has_rgb:
                log_unavailable("Keyboard RGB", "CAP_RGB missing")
            if not has_dyn:
                log_unavailable("Dynamic Effects", "CAP_RGB_DYNAMIC missing")
            if not has_cool:
                log_unavailable("CoolBoost", "CAP_COOLBOOST missing")
        self.topbar()
        if self.editor or self.show_help or self.confirm_stop:
            try:
                for yy in range(h):
                    s.chgat(yy, 0, w, curses.color_pair(C.DIM))
            except curses.error:
                pass
            for (sy, sx, sw) in self.swatches:
                self.addstr(sy, sx, " " * sw, grey_fill())
        if self.editor:
            base = self.clicks
            self.clicks = []
            self.draw_editor()
            self.eclicks = self.clicks
            self.clicks = base
        if self.show_help:
            self.help_overlay()
        if self.confirm_stop:
            self.confirm_modal()
        if os.environ.get("NITRO_TUI_DUMP_RECTS"):
            try:
                with open(os.environ["NITRO_TUI_DUMP_RECTS"], "w") as f:
                    json.dump(
                        {"clicks": self.clicks,
                        "eclicks": self.eclicks}, f)
            except OSError:
                pass
        s.refresh()

    def draw_coolboost(self, y, px, cur, pw=None):
        if not QUIRKS.has_cap(CAP_COOLBOOST):
            text = "CoolBoost unavailable"
            x = px + 1
            if pw:
                inner_w = pw - 2
                x = px + 1 + max(0, (inner_w - len(text)) // 2)
            self.addstr(y, x, text, curses.color_pair(C.DIM))
            return
        on = self.coolboost_live()
        if on is None:
            label = "(i) CoolBoost  --"
            suffix = " [N/A]"
            enabled = False
        else:
            label = f"(i) CoolBoost  {'● ON' if on else '○ OFF'}"
            enabled = cur == "auto"
            suffix = ""
            if not enabled:
                suffix = f"  [{cur.title() if cur else 'Mode'}]"
        text = f"{label}{suffix} "
        x = px + 2
        if enabled:
            attr = curses.color_pair(C.FG) | curses.A_BOLD
        else:
            attr = curses.color_pair(C.DIM)
        self.addstr(y, x, text, attr)
        self.click(y, x, 1, len(text), ("coolboost",))

    def draw_fan_control(self, py, px, ph, pw):
        if not QUIRKS.has_cap(CAP_FAN):
            text = "Fan control unavailable"
            inner_w = pw - 2
            x = px + 1 + max(0, (inner_w - len(text)) // 2)
            y = py + ph // 2
            self.addstr(y, x, text, curses.color_pair(C.DIM))
            return
        x = px + 2
        ylim = py + ph - 1
        bw = pw - 4
        cur = self.fan_sel()
        y = py + 2
        for i, (m, name) in enumerate(FAN_MODES):
            if y + 3 > ylim:
                break
            self.button(
                y, x, bw, f"({FAN_KEYS[i]}/{FAN_KEYS[i].lower()}) {name}",
                active=(cur == m), action=("fanmode", m))
            y += 4
        self.draw_coolboost(py + 15, px, cur, pw)
        cy = ylim - 1
        if cy - 1 > y:
            pre = "Created by "
            self.addstr(cy - 1, x, pre, curses.color_pair(C.DIM))
            link = "gh@SurrealArts"
            self.addstr(cy - 1, x + len(pre), link,
                        curses.color_pair(C.ACC) | curses.A_BOLD | curses.A_UNDERLINE)
            self.click(
                cy - 1, x + len(pre), 1, len(link),
                ("url", "https://github.com/SurrealArts"))
            self.addstr(cy, x, f"v{PROJECT_VERSION}", curses.color_pair(C.DIM))

    def draw_power(self, py, px, ph, pw):
        if not QUIRKS.has_cap(CAP_PLATFORM_PROFILE):
            text = "Power profiles unavailable"
            inner_w = pw - 2
            x = px + 1 + max(0, (inner_w - len(text)) // 2)
            y = py + ph // 2
            self.addstr(y, x, text, curses.color_pair(C.DIM))
            return
        x = px + 2
        ylim = py + ph - 1
        bw = pw - 4
        lt = self.plan_src()
        self.seg_tabs(
            py + 1, px + 1, pw - 2,
            [("ac", "(o) AC Power", ("plantab", "ac")),
            ("battery", "(p) Battery", ("plantab", "battery"))], lt)
        src = {"ac": "Active: On AC Power", "battery": "Active: On Battery"}.get(self.source, "Active: On AC Power ?")
        self.addstr(py + 4, x, src, curses.color_pair(C.DIM))
        cur = self.wanted.get("profile_battery" if lt == "battery" else "profile_ac")
        y = py + 5
        pitch = 4 if y + 16 + 3 <= ylim else 3
        for i, (name, val) in enumerate(PLANS):
            if y + 3 > ylim:
                break
            self.button(y, x, bw, f"({PLAN_KEYS[i]}) {name}", active=(cur == val),
                        action=("plan", i))
            y += pitch
        ac, bat = self.wanted.get("profile_ac"), self.wanted.get("profile_battery")

    def bil_section(self,
            ytop, x1, ta, tb, x0, series, ktop, kbot,
            ntop, vtop, atop, nbot, vbot, abot, divlabel):
        ybot = ytop + ta + tb + 2
        w = x1 - x0 + 1
        self.addstr(ytop, x0, ntop, curses.color_pair(C.DIM))
        self.addstr(ytop, x0 + len(ntop) + 1, vtop, atop)
        topv = [num(s.get(ktop)) for s in series]
        botv = [num(s.get(kbot)) for s in series]
        for i, row in enumerate(braille(topv, w, ta, anchor="bottom")):
            self.addstr(ytop + 1 + i, x0, row, atop)
        dvy = ytop + 1 + ta
        self.addstr(dvy, x0, "─" * w, curses.color_pair(C.DIM))
        lbl = f" {divlabel} "
        if w > len(lbl) + 2:
            self.addstr(dvy, x0 + (w - len(lbl)) // 2, lbl, curses.color_pair(C.DIM))
        for i, row in enumerate(braille(botv, w, tb, anchor="top")):
            self.addstr(dvy + 1 + i, x0, row, abot)
        self.addstr(ybot, x0, nbot, curses.color_pair(C.DIM))
        self.addstr(ybot, x0 + len(nbot) + 1, vbot, abot)

    def draw_fan_monitor(self, py, px, ph, pw):
        if not QUIRKS.has_cap(CAP_FAN_MODE):
            text = "Fan monitor unavailable"
            inner_w = pw - 2
            x = px + 1 + max(0, (inner_w - len(text)) // 2)
            y = py + ph // 2
            self.addstr(y, x, text, curses.color_pair(C.DIM))
            return
        x0, x1 = px + 1, px + pw - 2
        y0, y1 = py + 1, py + ph - 2
        cpu_m = self.state_fan_mode("cpu")
        gpu_m = self.state_fan_mode("gpu")
        gx0 = x0
        if cpu_m == "custom" or gpu_m == "custom":
            gx0 = min(x0 + 20, max(x0, x1 - 20))
            self.draw_sliders(x0 + 3, y0 + 1, y1, cpu_m, gpu_m)
        R = y1 - y0 + 1
        ta = max(1, (R - 3) // 2)
        tb = max(1, R - 3 - ta)
        top = self.rpm("cpu")
        bot = self.rpm("gpu")
        self.bil_section(
            y0, x1, ta, tb, gx0, list(self.series),
            "cpu_rpm", "gpu_rpm",
            "CPU", f"{top if top is not None else '--'} rpm",
            curses.color_pair(C.ACC) | curses.A_BOLD,
            "GPU", f"{bot if bot is not None else '--'} rpm",
            curses.color_pair(C.MID) | curses.A_BOLD,
            "rpm")

    def draw_sliders(self, x0, ytop, ybot, cpu_m, gpu_m):
        ty0, ty1 = ytop + 2, ybot - 3
        if ty1 - ty0 < 3:
            return
        cols = [("CPU", x0 + 3, "cpu", "/", ".", cpu_m, C.ACC)]
        if QUIRKS.fan_count >= 2:
            cols.append(("GPU", x0 + 12, "gpu", "'", ";", gpu_m, C.MID))
        for name, cx, which, kup, kdn, mode, pair in cols:
            active = mode == "custom"
            duty = self.duty(which)
            duty = 0 if duty is None else max(0, min(100, duty))
            d = curses.color_pair(C.DIM)
            a = curses.color_pair(pair) | curses.A_BOLD if active else d
            self.addstr(ytop, cx - 1, name, d | curses.A_BOLD)
            self.addstr(ytop + 1, cx - 1, f"({kup})", d)
            knob = ty1 - int(round(duty / 100.0 * (ty1 - ty0)))
            for yy in range(ty0, ty1 + 1):
                self.addstr(yy, cx, "●" if yy == knob else "┃", a if yy == knob else d)
            self.addstr(ybot - 2, cx - 1, f"{duty:3d}%", a)
            self.addstr(ybot - 1, cx - 1, f"({kdn})", d)
            if active:
                self.click(ty0, cx - 1, ty1 - ty0 + 1, 3, ("duty_abs", which, ty0, ty1))

    def _thermal_models(self):
        import re as _re
        if getattr(self, "_cpu_model", None) is None:
            try:
                with open("/proc/cpuinfo") as f:
                    base = next(
                        line.split(":", 1)[1].strip()
                        for line in f if line.startswith("model name"))
                short = None
                m = _re.search(r'i[3579]-\d{4,5}[A-Za-z]*', base)
                if m:
                    short = m.group(0)
                if short is None:
                    m = _re.search(r'Ryzen\s+\d\s+\d{4}[A-Za-z]*', base)
                    if m:
                        short = m.group(0)
                if short is None:
                    short = base.split(" @ ")[0].strip().split()[-1]
                self._cpu_model = short
            except Exception:
                self._cpu_model = "CPU"
        if getattr(self, "_gpu_model", None) is None:
            try:
                import subprocess
                try:
                    out = subprocess.check_output(
                        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                        text=True, timeout=2, stderr=subprocess.DEVNULL).strip()
                    name = out.splitlines()[0].strip() if out else ""
                except Exception:
                    name = ""
                if not name:
                    result = subprocess.run(
                        ["lspci"], capture_output=True, text=True, timeout=2)
                    for line in result.stdout.splitlines():
                        has_vga = "VGA" in line
                        has_nvidia = "NVIDIA" in line or "GeForce" in line
                        if has_vga and has_nvidia:
                            has_brackets = "[" in line and "]" in line
                            if has_brackets:
                                name = line.split("[", 1)[1].split("]", 1)[0].strip()
                            else:
                                name = line.split(":")[-1].strip()
                            break
                m = _re.search(r'(RTX|GTX)\s*\d{3,4}(?:\s*Ti)?', name, _re.IGNORECASE)
                if m:
                    self._gpu_model = m.group(0).upper().replace("  ", " ")
                elif name:
                    self._gpu_model = name
                else:
                    self._gpu_model = "GPU"
            except Exception:
                self._gpu_model = "GPU"
        return self._cpu_model, self._gpu_model

    def draw_thermal(self, py, px, ph, pw):
        if not QUIRKS.has_cap(CAP_HWMON):
            text = "Thermal monitoring unavailable"
            inner_w = pw - 2
            x = px + 1 + max(0, (inner_w - len(text)) // 2)
            y = py + ph // 2
            self.addstr(y, x, text, curses.color_pair(C.DIM))
            return
        x0, x1 = px + 1, px + pw - 2
        y0, y1 = py + 1, py + ph - 2
        d = self.snap.get("data", {}) or {}
        series = list(self.series)
        cpus = [x.get("package_c") for x in series]
        gpus = [x.get("gpu_c") for x in series]
        cpu_model, gpu_model = self._thermal_models()
        unit = "F" if self.temp_f else "C"
        deg = " \u00b0" + unit + " "
        cpu_t = d.get("package_c")
        gpu_t = d.get("gpu_c")
        cpu_col = (
            C.HOT if (cpu_t is not None and cpu_t >= 80) else
            C.MID if (cpu_t is not None and cpu_t >= 65) else C.G_OK)
        gpu_col = (
            C.HOT if (gpu_t is not None and gpu_t >= 80) else
            C.MID if (gpu_t is not None and gpu_t >= 65) else C.G_B)
        g = max(1, (ph - 7) // 2)
        cpu_disp = cpu_model if len(cpu_model) <= pw - 12 else cpu_model[:pw - 13] + "\u2026"
        self.addstr(
            y0, x0, f"CPU : {cpu_disp}",
            curses.color_pair(C.ACC) | curses.A_BOLD)
        self.addstr(
            y0 + 1, x0, f"cpu {fmt_temp(cpu_t, self.temp_f)}",
            curses.color_pair(temp_color(cpu_t)) | curses.A_BOLD)
        yy = y0 + 2
        for row in braille(cpus, pw - 2, g, anchor="bottom"):
            if yy >= y1 - 4:
                break
            self.addstr(yy, x0, row, curses.color_pair(cpu_col))
            yy += 1
        if yy < y1 - 4:
            self.addstr(yy, x0, "─" * (pw - 2), curses.color_pair(C.DIM))
            lx = x0 + ((pw - 2) - len(deg)) // 2
            self.addstr(yy, lx, deg, curses.color_pair(C.DIM) | curses.A_BOLD)
            yy += 1
        gg = max(1, y1 - yy - 1)
        for row in braille(gpus, pw - 2, gg, anchor="top"):
            if yy >= y1 - 1:
                break
            self.addstr(yy, x0, row, curses.color_pair(gpu_col))
            yy += 1
        if yy < y1:
            self.addstr(
                yy, x0, f"gpu {fmt_temp(gpu_t, self.temp_f)}",
                curses.color_pair(temp_color(gpu_t)))
            yy += 1
        if yy <= y1:
            gpu_disp = gpu_model if len(gpu_model) <= pw - 8 else gpu_model[:pw - 9] + "\u2026"
            self.addstr(
                yy, x0, f"GPU : {gpu_disp}",
                curses.color_pair(C.MID) | curses.A_BOLD)
        kx = max(x0, x1 - 2)
        self.addstr(y0, kx, "(F)", curses.color_pair(C.DIM))
        self.click(y0, kx, 1, 3, ("temp",))

    def draw_keyboard(self, py, px, ph, pw):
        if not QUIRKS.has_cap(CAP_RGB):
            text = "Keyboard RGB unavailable"
            inner_w = pw - 2
            x = px + 1 + max(0, (inner_w - len(text)) // 2)
            y = py + ph // 2
            self.addstr(y, x, text, curses.color_pair(C.DIM))
            return
        x0, x1 = px + 1, px + pw - 2
        y0, y1 = py + 1, py + ph - 2
        mode = self.kbd_mode()
        tabs = [("static", "([) Static", ("tab", "static"))]
        if QUIRKS.has_cap(CAP_RGB_DYNAMIC):
            tabs.append(("dynamic", "(]) Dynamic", ("tab", "dynamic")))
        else:
            tabs.append(("dynamic", "(]) Dynamic (unavailable)", ("tab", "dynamic")))
        self.seg_tabs(y0, x0, x1 - x0 + 1, tabs, mode)
        if mode == "static":
            self.draw_static(x0, y0 + 4, x1, y1)
        else:
            if QUIRKS.has_cap(CAP_RGB_DYNAMIC):
                self.draw_dynamic(x0, y0 + 4, x1, y1)
            else:
                text = "Dynamic effects unavailable"
                inner_w = x1 - x0 + 1
                x = x0 + max(0, (inner_w - len(text)) // 2)
                y = y0 + (y1 - y0) // 2
                self.addstr(y, x, text, curses.color_pair(C.DIM))

    def zone_button(self, y, x, w, i, rgb, on, sel):
        lab = f"({i + 1}) Z{i + 1}"
        if len(ZONE_KEYS) == 1:
            lab = f"(1) Zone {ZONE_KEYS[0]}"
        la = (
            curses.color_pair(C.ACC) | curses.A_BOLD if sel
            else curses.color_pair(C.FG))
        self.addstr(y, x + max(0, (w - len(lab)) // 2), lab, la)
        glyph = "●" if on else "○"
        ga = rgb_attr(*rgb, bold=True) if on else curses.color_pair(C.DIM)
        state = " on" if on else " off"
        sa = curses.color_pair(C.FG if on else C.DIM)
        tx = x + max(0, (w - (1 + len(state))) // 2)
        self.addstr(y + 1, tx, glyph, ga)
        self.addstr(y + 1, tx + 1, state, sa)
        if on:
            self.addstr(y + 2, x + max(0, (w - 4) // 2), "▇▇▇▇", rgb_attr(*rgb))
        self.click(y + 1, tx, 1, max(3, 1 + len(state) + 1), ("ztoggle", i))
        self.click(y, x, 3, w, ("zedit", i))

    def draw_static(self, x0, y, x1, y1):
        W = x1 - x0 + 1
        n_zones = len(ZONE_KEYS)
        bw = max(9, (W - (n_zones - 1)) // n_zones)
        for i in range(n_zones):
            bx = x0 + i * (bw + 1)
            w = min(bw, x1 - bx + 1)
            if w < 8:
                break
            rgb, on = self.state_zone(ZONE_KEYS[i])
            self.zone_button(y, bx, w, i, rgb, on, i == self.sel_zone)
        by = y + 4
        if by <= y1:
            bri = self.brightness()
            self.addstr(by, x0, "bright ", curses.color_pair(C.FG))
            self.addstr(by, x0 + 7, "(l) ", curses.color_pair(C.DIM))
            x3 = x0 + 11
            vtxt = f"(L) {bri:3d}%"
            tw = max(8, min(30, x1 - x3 - len(vtxt)))
            t0, ti = self.slider(by, x3, tw, bri / 100.0, curses.color_pair(C.ACC))
            self.addstr(by, x3 + tw + 1, vtxt, curses.color_pair(C.FG))
            self.click(by, t0, 1, ti, ("bright_abs", t0, ti))
        hy = y + 6
        if hy <= y1:
            self.rich(
                hy, x0, "*(x)* on/off    *(enter/#)* custom colour",
                curses.color_pair(C.DIM), maxw=W)
            self.click(hy, x0 + 14, 1, min(max(0, W - 14), 23), ("colourpicker",))

    def chip_flow(self, y, x, W, ylim, cur):
        cx = x
        for m, name in EFFECT_NAMES.items():
            lab = f"({m}) {name}"
            cw = len(lab) + 2
            if cx > x and cx + cw > x + W:
                y += 2
                cx = x
            if y > ylim or cx + cw > x + W:
                break
            active = m == cur
            attr = (
                curses.color_pair(C.ACC) | curses.A_BOLD | curses.A_REVERSE
                if active else curses.color_pair(C.FG))
            self.addstr(y, cx, f" {lab} ", attr)
            self.click(y, cx, 1, cw, ("effect", m))
            cx += cw + 1
        return y + 1

    def draw_dynamic(self, x0, y, x1, y1):
        d = self.dyn()
        cur = d["mode"]
        e = d["effects"][cur]
        needs = EFFECT_NEEDS[cur]
        W = x1 - x0 + 1
        y = self.chip_flow(y, x0, W, y1, cur)
        sy = y + 1
        if sy <= y1:
            self.row_slider(
                sy, x0, x1, "speed", "(t)", "(T)", e["speed"] / 10.0,
                f"{e['speed']}", "speed" in needs, C.ACC, ("dyn_speed",))
        by = sy + 1
        if by <= y1:
            self.row_slider(
                by, x0, x1, "bright", "(l)", "(L)", self.brightness() / 100.0,
                f"{self.brightness()}", True, C.ACC, ("bright_abs",))
        dy = by + 1
        if dy <= y1:
            dir_on = "direction" in needs
            self.addstr(dy, x0, "direction", curses.color_pair(C.FG if dir_on else C.DIM))
            x = self.chip(
                dy, x0 + 10, "(f)", curses.color_pair(C.DIM),
                ("dyn_dirflip",) if dir_on else None) + 2
            for label, val in (("←", 2), ("→", 1)):
                sel = e["direction"] == val
                if not dir_on:
                    attr = curses.color_pair(C.DIM)
                elif sel:
                    attr = curses.color_pair(C.ACC) | curses.A_BOLD | curses.A_REVERSE
                else:
                    attr = curses.color_pair(C.FG)
                x = self.chip(
                    dy, x, f" {label} ", attr,
                    ("dyn_dir", val) if dir_on else None) + 1
        cy = dy + 2
        col_on = "colour" in needs
        if cy <= y1:
            self.addstr(cy, x0, "colour ", curses.color_pair(C.FG if col_on else C.DIM))
            avail = W - 12
            pitch = 7 if avail >= 71 else 5 if avail >= 51 else 4
            block = max(2, pitch - 2)
            x = x0 + 8
            cur_rgb = (e["r"], e["g"], e["b"])
            for (r, g, b) in SWATCHES:
                if x + block > x1 + 1:
                    break
                sel = col_on and (r, g, b) == cur_rgb
                if col_on:
                    self.swatch(cy, x, block, r, g, b, sel, highlight=False)
                    self.click(cy, x, 1, block, ("dyn_colour", r, g, b))
                else:
                    self.addstr(cy, x, " " * block, grey_fill())
                x += pitch
        hy = cy + 2
        if hy <= y1:
            self.rich(
                hy, x0, "*(enter/#)* custom colour",
                curses.color_pair(C.DIM), maxw=W)
            if col_on:
                self.click(hy, x0, 1, min(W, 23), ("colourpicker",))

    def row_slider(self,
        y, x, x1, label, klo, khi,
        frac, val, active, pair, action):
        la = curses.color_pair(C.FG if active else C.DIM)
        self.addstr(y, x, f"{label:<9} ", la)
        x2 = x + 10
        self.addstr(y, x2, f"{klo} ", curses.color_pair(C.DIM))
        x3 = x2 + len(klo) + 1
        vtxt = f" {khi} {val:>3}"
        tw = max(8, min(24, x1 - x3 - len(vtxt)))
        t0, ti = self.slider(
            y, x3, tw, frac,
            curses.color_pair(pair if active else C.DIM))
        self.addstr(y, x3 + tw + 1, vtxt, la)
        if active and action:
            self.click(y, t0, 1, ti, action + (t0, ti))
        return t0, ti

    def draw_editor(self):
        e = self.editor
        h, w = self.h, self.w
        bw, bh = 54, 16
        y0 = max(1, h // 2 - bh // 2)
        x0 = max(2, w // 2 - bw // 2)
        for i in range(bh):
            self.addstr(y0 + i, x0, " " * bw, 0)
        title = (
            f"colour - zone Z{ZONE_KEYS.index(e['zone']) + 1}"
            if e["kind"] == "zone" else f"colour - {EFFECT_NAMES[e['eff']]}")
        self.box(y0, x0, bh, bw, title, active=True)
        foc = e.get("focus")
        ring = e["ring"]
        hl = curses.color_pair(C.ACC) | curses.A_BOLD | curses.A_REVERSE
        for i, k in enumerate("rgb"):
            yy = y0 + 2 + i
            self.addstr(
                yy, x0 + 2, f" {'RGB'[i]} ",
                hl if ring == i else curses.color_pair(C.FG))
            self.click(yy, x0 + 2, 1, 3, ("edch", i))
            if foc == ("num", i):
                vt = (e["buf"] + ("_" if len(e["buf"]) < 3 else ""))[:3]
                va = hl
            else:
                vt = f"{e[k]:3d}"
                va = curses.color_pair(C.FG if ring == i else C.DIM)
            self.addstr(yy, x0 + 8, vt, va)
            self.click(yy, x0 + 8, 1, 3, ("ednum", i))
        hy = y0 + 6
        self.addstr(
            hy, x0 + 7, "hex",
            hl if ring == 3 else curses.color_pair(C.FG))
        self.click(hy, x0 + 7, 1, 3, ("edhex",))
        vy = y0 + 7
        if foc == "hex":
            ht = f"#{e['buf'].upper()}" + ("_" if len(e["buf"]) < 6 else " ")
            ha = hl
        else:
            ht = hexname(e["r"], e["g"], e["b"])
            ha = curses.color_pair(C.FG if ring == 3 else C.DIM)
        hx = x0 + 5
        self.addstr(vy, hx, ht, ha)
        self.click(vy, x0 + 2, 1, 13, ("edhex",))
        self.addstr(y0 + 1, x0 + 20, "previous", curses.color_pair(C.DIM))
        self.addstr(y0 + 1, x0 + 42, "new", curses.color_pair(C.DIM))
        for i in range(6):
            self.addstr(
                y0 + 2 + i, x0 + 16, " " * 17,
                rgb_bg(*e["was"]))
            self.addstr(
                y0 + 2 + i, x0 + 35, " " * 17,
                rgb_bg(e["r"], e["g"], e["b"]))
        sy = y0 + 9
        self.addstr(sy, x0 + 2, "swatch", curses.color_pair(C.DIM))
        x = x0 + 9
        for idx, (r, g, b) in enumerate(SWATCHES):
            sel = (r, g, b) == (e["r"], e["g"], e["b"])
            is_hl = (4 <= ring <= 14) and (ring - 4 == idx)
            self.swatch(sy, x, 3, r, g, b, sel, highlight=is_hl)
            self.click(sy, x, 1, 3, ("edsw", r, g, b))
            x += 4
        self.addstr(
            y0 + 11, x0 + 2, "↑↓←→ move · # hex",
            curses.color_pair(C.DIM))
        self.addstr(
            y0 + 12, x0 + 2, "shift+enter apply · esc cancel",
            curses.color_pair(C.DIM))
        self.chip(
            y0 + 14, x0 + 13, " [ apply ] ",
            hl if ring == 15 else curses.color_pair(C.OK) | curses.A_BOLD,
            ("edapply",))
        self.chip(
            y0 + 14, x0 + 28, " [ cancel ] ",
            hl if ring == 16 else curses.color_pair(C.DIM),
            ("edcancel",))

    def help_overlay(self):
        h, w = self.h, self.w
        body = [
            ("h", "fans"),
            ("b", "  *(a/A)* auto    *(s/S)* max    *(d/D)* custom"),
            ("b", "  custom duty:  *(. /)* cpu -/+5     *(; ')* gpu -/+5"),
            ("b", "  *(i)* CoolBoost toggle   (Auto only; disabled on Max/Custom)"),
            ("s", ""),
            ("h", "power"),
            ("b", "  *(Z)* power saver    *(X)* quiet    *(C)* balance"),
            ("b", "  *(V)* balance acer    *(B)* high-performance"),
            ("b", "  *(o)* AC Power tab   *(p)* Battery tab   (or click the tabs)"),
            ("b", "  the grey line under the tabs shows the live power source"),
            ("s", ""),
            ("h", "keyboard"),
            ("b", "  *([)* static      *(])* dynamic      (tab bar, clickable)"),
            ("b", "  static    *(1-4)* zone   *(x)* on/off   *(enter/#)* custom colour"),
            ("b", "  dynamic   *(1-5)* effect   *(t)* speed   *(f)* direction   *(#)* custom colour"),
            ("b", "  brightness   *(l)*  *(L)*"),
            ("s", ""),
            ("h", "thermal"),
            ("b", "  *(F)*  switch °C / °F"),
            ("h", "refresh"),
            ("b", "  *(-)* faster    *(+)* slower      (top right, clickable)"),
            ("h", "global"),
            ("b", "  *(R)* reapply saved state"),
            ("b", "  *(q)* close the ui    *(Q)* stop the daemon"),
            ("s", ""),
            ("d", "every control is clickable and shows its key; any key closes"),
        ]
        width = min(max(len(t.replace("*", "")) for _, t in body) + 6, w - 4)
        bh = len(body) + 2
        y0 = max(0, (h - bh) // 2)
        x0 = max(0, (w - width) // 2)
        for i in range(bh):
            self.addstr(y0 + i, x0, " " * width, 0)
        self.box(y0, x0, bh, width, "help")
        for i, (kind, text) in enumerate(body):
            if kind == "s":
                continue
            base = (
                curses.color_pair(C.FG) | curses.A_BOLD if kind == "h"
                else curses.color_pair(C.DIM))
            self.rich(y0 + 1 + i, x0 + 2, text, base, maxw=width - 4)

    def need_msg(self, need_w, need_h):
        h, w = self.s.getmaxyx()
        self.s.erase()
        lines = [
            "terminal too small:",
            f"have {w}x{h}, need ≥ {need_w}x{need_h}",
            "enlarge the window (q quits)"]
        y0 = max(0, h // 2 - len(lines) // 2)
        for i, ln in enumerate(lines):
            self.addstr(
                y0 + i, max(0, w // 2 - len(ln) // 2), ln,
                curses.color_pair(C.ACC) | curses.A_BOLD)
        self.s.refresh()

    def open_editor(self, kind, zone=None, eff=None, rgb=(255, 255, 255)):
        self.editor = {
            "kind": kind, "zone": zone, "eff": eff,
            "r": rgb[0], "g": rgb[1], "b": rgb[2],
            "ring": 0, "focus": None, "buf": "", "fresh": False,
            "was": (rgb[0], rgb[1], rgb[2])}

    def _swatch_entry(self, e):
        for i, (r, g, b) in enumerate(SWATCHES):
            if (r, g, b) == (e["r"], e["g"], e["b"]):
                return 4 + i
        return 4

    def editor_focus(self, foc):
        e = self.editor
        e["focus"] = foc
        e["fresh"] = True
        if foc == "hex":
            e["ring"] = 3
            e["buf"] = f"{e['r']:02x}{e['g']:02x}{e['b']:02x}"
        else:
            e["ring"] = foc[1]
            e["buf"] = str(e["rgb"[foc[1]]])

    def editor_focus_key(self, foc, ch):
        e = self.editor
        buf = e["buf"]
        if ch == 27:
            e["focus"] = None
            e["buf"] = ""
            e["fresh"] = False
            return
        if ch in (10, 13):
            if foc == "hex":
                if len(buf) == 6:
                    e["r"] = int(buf[0:2], 16)
                    e["g"] = int(buf[2:4], 16)
                    e["b"] = int(buf[4:6], 16)
                    e["focus"] = None
                    e["buf"] = ""
                    e["fresh"] = False
                else:
                    self.msg = "» hex needs 6 digits (0-9a-f)"
                    self.msg_time = time.time()
            else:
                if buf:
                    e["rgb"[foc[1]]] = max(0, min(255, int(buf)))
                e["focus"] = None
                e["buf"] = ""
                e["fresh"] = False
            return
        if ch in (curses.KEY_BACKSPACE, 8, 127, 263):
            e["fresh"] = False
            e["buf"] = buf[:-1]
            return
        if ch > 0x10FFFF:
            return
        c = chr(ch).lower()
        if foc == "hex":
            if c in "0123456789abcdef":
                if e["fresh"]:
                    buf, e["fresh"] = "", False
                if len(buf) < 6:
                    e["buf"] = buf + c
            return
        if c.isdigit():
            if e["fresh"]:
                buf, e["fresh"] = "", False
            if len(buf) < 3:
                e["buf"] = buf + c

    def pick_colour(self):
        if self.kbd_mode() == "static":
            rgb, _ = self.state_zone(ZONE_KEYS[self.sel_zone])
            self.open_editor("zone", zone=ZONE_KEYS[self.sel_zone], rgb=rgb)
        else:
            d = self.dyn()
            if "colour" not in EFFECT_NEEDS[d["mode"]]:
                self.msg = f"» {EFFECT_NAMES[d['mode']]} has no colour control"
                self.msg_time = time.time()
                return
            e = d["effects"][d["mode"]]
            self.open_editor("effect", eff=d["mode"], rgb=(e["r"], e["g"], e["b"]))

    def editor_apply(self):
        e = self.editor
        if not e:
            return
        if e["kind"] == "zone":
            self.set_zone_color(e["zone"], e["r"], e["g"], e["b"])
        else:
            self.set_dyn(
                f"colour → {hexname(e['r'], e['g'], e['b'])}",
                r=e["r"], g=e["g"], b=e["b"])
        self.editor = None

    def act(self, action):
        if action is None:
            return
        if QUIRKS.probe and isinstance(action, tuple):
            if action[0] not in ("help", "url", "refresh", "temp", "plantab"):
                self.msg = "!! probe mode: read-only"
                self.msg_time = time.time()
                return
        if self.editor is not None:
            self.act_editor(action)
            return
        try:
            if not isinstance(action, tuple):
                return
            kind = action[0]
            if kind == "help":
                self.open_help()
            elif kind == "plantab":
                self.plan_tab = action[1]
            elif kind == "url":
                self.open_url(action[1])
            elif kind == "refresh":
                self.set_refresh(action[1])
            elif kind == "fanmode":
                self.set_fan_mode(action[1])
            elif kind == "coolboost":
                self.toggle_coolboost()
            elif kind == "plan":
                self.set_plan(action[1])
            elif kind == "tab":
                self.set_kbd_mode(action[1])
            elif kind == "temp":
                self.toggle_temp()
            elif kind == "duty_abs":
                _, which, ty0, ty1 = action
                frac = (ty1 - self._my) / max(1, ty1 - ty0)
                self.set_duty(which, int(round(frac * 100)))
            elif kind == "ztoggle":
                self.toggle_zone(action[1])
            elif kind == "zedit":
                i = action[1]
                self.sel_zone = i
                rgb, _ = self.state_zone(ZONE_KEYS[i])
                self.open_editor("zone", zone=ZONE_KEYS[i], rgb=rgb)
            elif kind == "effect":
                self.set_effect(action[1])
            elif kind == "dyn_speed":
                _, t0, tw = action
                val = max(1, min(10, int(round((self._mx - t0) / max(1, tw - 1) * 10))))
                self.set_dyn(f"speed → {val}", speed=val)
            elif kind == "dyn_dirflip":
                d = self.dyn()
                v = 2 if d["effects"][d["mode"]]["direction"] == 1 else 1
                self.set_dyn(f"direction → {'←' if v == 2 else '→'}", direction=v)
            elif kind == "dyn_dir":
                v = action[1]
                self.set_dyn(f"direction → {'←' if v == 2 else '→'}", direction=v)
            elif kind == "dyn_colour":
                self.set_dyn(
                    f"colour → {hexname(action[1], action[2], action[3])}",
                    r=action[1], g=action[2], b=action[3])
            elif kind == "colourpicker":
                self.pick_colour()
            elif kind == "bright_abs":
                _, t0, tw = action
                val = int(round((self._mx - t0) / max(1, tw - 1) * 100))
                self.set_brightness(val)
        except (OSError, ValueError, KeyError, TypeError) as ex:
            self.msg = f"!! {ex}"
            self.msg_time = time.time()

    def act_editor(self, action):
        e = self.editor
        if not isinstance(action, tuple):
            return
        if e.get("focus") is not None:
            return
        kind = action[0]
        if kind == "edch":
            e["ring"] = action[1]
        elif kind == "ednum":
            self.editor_focus(("num", action[1]))
        elif kind == "edsw":
            e["r"], e["g"], e["b"] = action[1], action[2], action[3]
            for i, (r, g, b) in enumerate(SWATCHES):
                if (r, g, b) == (action[1], action[2], action[3]):
                    e["ring"] = 4 + i
                    break
        elif kind == "edhex":
            self.editor_focus("hex")
        elif kind == "edapply":
            self.editor_apply()
        elif kind == "edcancel":
            self.editor = None

    def toggle_temp(self):
        self.temp_f = not self.temp_f
        save_cfg(self.refresh_ms, self.temp_f)
        self.msg = f"» temperature → °{'F' if self.temp_f else 'C'}"
        self.msg_time = time.time()

    def key(self, ch):
        if self.editor is not None:
            self.editor_key(ch)
            return
        if QUIRKS.probe and ch not in (
            ord("?"), ord("F"), ord("-"), ord("="), ord("+"),
            ord("o"), ord("O"), ord("p"), ord("P")):
            self.msg = "!! probe mode: read-only"
            self.msg_time = time.time()
            return
        if ch == ord("?"):
            self.open_help()
        elif ch == ord("R"):
            self.send({"cmd": "reapply"}, "reapplied saved state")
        elif ch == ord("F"):
            self.toggle_temp()
        elif ch == ord("-"):
            self.set_refresh(-1)
        elif ch in (ord("="), ord("+")):
            self.set_refresh(1)
        elif ch in (ord("a"), ord("A")):
            self.set_fan_mode("auto")
        elif ch in (ord("s"), ord("S")):
            self.set_fan_mode("max")
        elif ch in (ord("d"), ord("D")):
            self.set_fan_mode("custom")
        elif ch in (ord("i"), ord("I")):
            self.toggle_coolboost()
        elif ch in (ord("."), ord("/"), ord(";"), ord("'")):
            which = "cpu" if ch in (ord("."), ord("/")) else "gpu"
            cur = self.duty(which)
            cur = 0 if cur is None else cur
            step = 5 if ch in (ord("/"), ord("'")) else -5
            self.set_duty(which, cur + step)
        elif chr(ch) in PLAN_KEYS:
            self.set_plan(PLAN_KEYS.index(chr(ch)))
        elif ch in (ord("o"), ord("O")):
            self.plan_tab = "ac"
        elif ch in (ord("p"), ord("P")):
            self.plan_tab = "battery"
        elif ch == ord("["):
            self.set_kbd_mode("static")
        elif ch == ord("]"):
            self.set_kbd_mode("dynamic")
        elif ch == ord("#"):
            self.pick_colour()
        elif ord("1") <= ch <= ord("5"):
            is_static = self.kbd_mode() == "static"
            key_is_zone = ch <= ord("4")
            if is_static and key_is_zone:
                self.sel_zone = ch - ord("1")
            elif self.kbd_mode() == "dynamic":
                self.set_effect(ch - ord("0"))
        elif ch == ord("x"):
            if self.kbd_mode() == "static":
                self.toggle_zone(self.sel_zone)
        elif ch in (10, 13):
            self.pick_colour()
        elif ch in (ord("t"), ord("T")):
            if self.kbd_mode() == "dynamic":
                d = self.dyn()
                e = d["effects"][d["mode"]]
                v = max(1, min(10, e["speed"] + (1 if ch == ord("T") else -1)))
                self.set_dyn(f"speed → {v}", speed=v)
        elif ch == ord("f"):
            if self.kbd_mode() == "dynamic":
                d = self.dyn()
                if "direction" not in EFFECT_NEEDS[d["mode"]]:
                    self.msg = f"» {EFFECT_NAMES[d['mode']]} has no direction control"
                    self.msg_time = time.time()
                else:
                    e = d["effects"][d["mode"]]
                    v = 2 if e["direction"] == 1 else 1
                    self.set_dyn(f"direction → {'←' if v == 2 else '→'}", direction=v)
        elif ch in (ord("l"), ord("L")):
            v = self.brightness() + (5 if ch == ord("L") else -5)
            self.set_brightness(v)

    def sniff_esc(self):
        s = self.s
        s.timeout(5)
        try:
            c = s.getch()
            if c != ord("["):
                if c != -1:
                    curses.ungetch(c)
                return None
            buf = ""
            while len(buf) < 14:
                c = s.getch()
                if c == -1:
                    break
                buf += chr(c)
                if chr(c) in "uABCDZ~":
                    break
            if not buf:
                curses.ungetch(ord("["))
                return None
            return self.csi_key(buf)
        finally:
            s.timeout(max(20, self.refresh_ms))

    @staticmethod
    def csi_key(buf):
        final = buf[-1]
        body = buf[:-1]
        parts = body.split(";")
        code = int(parts[0]) if parts and parts[0].isdigit() else -1
        mods = int(parts[1]) - 1 if len(parts) > 1 and parts[1].isdigit() else 0
        shift, alt, ctrl = mods & 1, mods & 2, mods & 4
        if final == "u" and not alt:
            if code == 13 and shift and not ctrl:
                return SHIFT_ENTER
            if code == 27 and not ctrl:
                return 27
            if code == 9 and shift and not ctrl:
                return curses.KEY_BTAB
            if ctrl and 97 <= code <= 122:
                return code & 0x1F
        elif not alt and not ctrl and final in "ABCD":
            return {
                "A": curses.KEY_UP, "B": curses.KEY_DOWN,
                "C": curses.KEY_RIGHT, "D": curses.KEY_LEFT}[final]
        elif not alt and not ctrl and final == "Z":
            return curses.KEY_BTAB
        return 0

    def editor_key(self, ch):
        e = self.editor
        if e.get("focus") is not None:
            self.editor_focus_key(e["focus"], ch)
            return
        if ch == SHIFT_ENTER:
            if e["ring"] != 15:
                e["ring"] = 15
            else:
                self.editor_apply()
            return
        if ch == 27:
            if e["ring"] == 16:
                self.editor = None
            else:
                e["ring"] = 16
            return
        if ch in (curses.KEY_DOWN, ord("\t")):
            r = e["ring"]
            if 4 <= r <= 14:
                e["ring"] = 15
            elif r == 16:
                e["ring"] = 0
            elif r == 3:
                e["ring"] = self._swatch_entry(e)
            else:
                e["ring"] = r + 1
            return
        if ch in (curses.KEY_UP, curses.KEY_BTAB):
            r = e["ring"]
            if 4 <= r <= 14:
                e["ring"] = 3
            elif r == 0:
                e["ring"] = 16
            elif r in (15, 16):
                e["ring"] = self._swatch_entry(e)
            else:
                e["ring"] = r - 1
            return
        if ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            r = e["ring"]
            if 4 <= r <= 14:
                if ch == curses.KEY_LEFT:
                    e["ring"] = 14 if r == 4 else r - 1
                else:
                    e["ring"] = 4 if r == 14 else r + 1
            elif r in (15, 16):
                e["ring"] = 16 if r == 15 else 15
            return
        if ch < 0x110000 and chr(ch).lower() in "rgb":
            i = "rgb".index(chr(ch).lower())
            if e["ring"] == i:
                self.editor_focus(("num", i))
            else:
                e["ring"] = i
            return
        if ch == ord("#"):
            self.editor_focus("hex")
            return
        if ch in (10, 13):
            r = e["ring"]
            if r in (0, 1, 2):
                self.editor_focus(("num", r))
            elif r == 3:
                self.editor_focus("hex")
            elif 4 <= r <= 14:
                sw = SWATCHES[r - 4]
                e["r"], e["g"], e["b"] = sw
            elif r == 15:
                self.editor_apply()
            else:
                self.editor = None
            return


def main(stdscr):
    curses.curs_set(0)
    init_theme()
    try:
        curses.set_escdelay(25)
    except AttributeError:
        pass
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
    except curses.error:
        pass
    ui = UI(stdscr)
    stdscr.refresh()
    proto = sys.stdout.isatty()
    if proto:
        sys.stdout.write("\x1b[>1u")
        sys.stdout.flush()
    try:
        return runloop(ui)
    finally:
        if proto:
            sys.stdout.write("\x1b[<u")
            sys.stdout.flush()


def runloop(ui):
    last = 0.0
    last_sample = 0.0
    while True:
        now = time.time()
        if now - last >= ui.refresh_ms / 1000.0:
            try:
                ui.snap = rpc({"cmd": "status"})
                if now - last_sample >= ui.refresh_ms / 2000.0:
                    data = ui.snap.get("data") or {}
                    ui.series.append({
                        k: num(data.get(k)) for k in
                        ("cpu_rpm", "gpu_rpm", "package_c", "gpu_c")})
                    last_sample = now
                if ui.msg and not ui.msg.startswith("!!") and now - ui.msg_time > 5:
                    ui.msg = ""
            except OSError:
                ui.snap = {"data": {}, "desired": {}, "source": None, "daemon": {}}
                if now - last_sample >= ui.refresh_ms / 2000.0:
                    ui.series.append(
                        {"cpu_rpm": None, "gpu_rpm": None,
                        "package_c": None, "gpu_c": None})
                    last_sample = now
                ui.msg = "!! daemon down: sudo systemctl start nitrod"
            last = now
        ui.draw()
        ui.s.timeout(max(20, ui.refresh_ms))
        try:
            ch = ui.s.getch()
        except curses.error:
            continue
        if ch == -1:
            continue
        if ch == 27:
            alt = ui.sniff_esc()
            if alt is None:
                pass
            elif alt == 0:
                continue
            else:
                ch = alt
        if ch == curses.KEY_RESIZE:
            ui.resize_t = time.time()
            last = 0.0
            continue
        if ui.show_help:
            if ch != curses.KEY_MOUSE or now - ui.help_t > 0.3:
                ui.show_help = False
            continue
        if ui.confirm_stop:
            if ch in (ord("y"), ord("Y")):
                try:
                    rpc({"cmd": "stop"})
                except OSError:
                    pass
                return 0
            ui.confirm_stop = False
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bstate = curses.getmouse()
            except curses.error:
                continue
            if bstate & curses.BUTTON1_PRESSED and now - ui.last_press > 0.15 and now - ui.resize_t > 0.25:
                ui.last_press = now
                ui._mx, ui._my = mx, my
                ui.act(ui.at_click(my, mx))
                last = 0.0
            continue
        if ui.editor is not None:
            ui.editor_key(ch)
            last = 0.0
            continue
        if ch in (ord("q"), 27, 17):
            return 0
        if ch == ord("Q"):
            ui.confirm_stop = True
            continue
        ui.key(ch)
        last = 0.0


if __name__ == "__main__":
    try:
        sys.exit(curses.wrapper(main))
    except KeyboardInterrupt:
        sys.exit(0)
