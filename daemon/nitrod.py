#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
nitrod - daemon

State persists in /var/lib/nitro/state.json,
history in /var/lib/nitro/history.db (24h).

State layout:
    mode            "static" | "dynamic"          active keyboard display
    static          {"1": {"rgb": [r,g,b], "on": bool}, ...}
                    zones on EC keys 1,2,4,8; on/off is independent of the
                    stored rgb (an off zone keeps its colour for re-enable)
    dynamic         {"mode": 1-5, "effects": {"1": {"speed","direction","r","g","b"}, ...}}
                    config is preserved per effect; switching loads it back
    brightness      0-100, global (used by both static and dynamic)
    fan_cpu/fan_gpu 0-100 duty, remembered even while fans run auto/max
    fan_mode        {"cpu": "auto|max|custom", "gpu": ...}
    coolboost       bool, retained while fans are not in auto
    profile_ac      platform profile applied while on mains power
    profile_battery platform profile applied while on battery
"""
import fcntl
import grp
import json
import os
import pwd
import shutil
import signal
import socket
import subprocess
import sqlite3
import sys
import threading
import time
from glob import glob

MAX_CLIENTS = 32
client_slots = threading.Semaphore(MAX_CLIENTS)

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
    find_platform_profile,
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
LOCK = SOCK + ".lock"
SOCK_GROUP = os.environ.get("NITRO_SOCK_GROUP", "nitro")
SYS = os.environ.get("NITRO_SYS", "/sys/devices/platform/nitro")
VERSION = "2.3.3"

_PP = find_platform_profile()
PP = str(_PP) if _PP else None

HWMON = os.environ.get("NITRO_HWMON", "/sys/class/hwmon")
POWER = os.environ.get("NITRO_POWER", "/sys/class/power_supply")
STATE = os.environ.get("NITRO_STATE", "/var/lib/nitro/state.json")
DBDIR = os.environ.get("NITRO_DBDIR", "/var/lib/nitro")
DB = os.path.join(DBDIR, "history.db")
POLL = 1.0
RETENTION = 86400

PROFILES = ("low-power", "quiet", "balanced", "balanced-performance", "performance")
ZONES = get_zones(QUIRKS)
EFFECTS = get_effects(QUIRKS)
EFFECT_NAMES = get_effect_names(QUIRKS)
EFFECT_DEFAULTS = {
    1: (4, (255, 0, 255)),
    2: (3, (0, 255, 255)),
    3: (5, (0, 128, 255)),
    4: (5, (128, 0, 255)),
    5: (7, (0, 255, 0)),
}
EFFECT_DEFAULTS = {k: v for k, v in EFFECT_DEFAULTS.items() if k in EFFECTS}

def pr_err(msg):
    print(f"ERROR: {msg}", file=sys.stderr)

stop_event = threading.Event()
start_ts = time.time()


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def write(path, text):
    with open(path, "w") as f:
        f.write(str(text))


def coerce_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "on", "true", "yes"):
            return True
        if v in ("0", "off", "false", "no"):
            return False
    return None


def parse_coolboost(value):
    return coerce_bool(value)


def hwmon_find(name):
    try:
        for h in sorted(os.listdir(HWMON)):
            if read(os.path.join(HWMON, h, "name")) == name:
                return os.path.join(HWMON, h)
    except OSError:
        pass
    return None


def ac_source():
    for t in sorted(glob(f"{POWER}/*/type")):
        if read(t) == "Mains":
            online = read(os.path.join(os.path.dirname(t), "online"))
            return "ac" if online != "0" else "battery"
    return None


def snapshot():
    d = {}
    has_fan = QUIRKS.has_cap(CAP_FAN)
    has_fan_mode = QUIRKS.has_cap(CAP_FAN_MODE)
    has_profile = QUIRKS.has_cap(CAP_PLATFORM_PROFILE)
    has_rgb = QUIRKS.has_cap(CAP_RGB)
    has_dyn = QUIRKS.has_cap(CAP_RGB_DYNAMIC)
    has_cool = QUIRKS.has_cap(CAP_COOLBOOST)
    has_hwmon = QUIRKS.has_cap(CAP_HWMON)

    if has_fan:
        d["fan_cpu_duty"] = read(f"{SYS}/fan_cpu")
        if QUIRKS.fan_count >= 2:
            d["fan_gpu_duty"] = read(f"{SYS}/fan_gpu")
    if has_fan_mode:
        d["fan_mode"] = read(f"{SYS}/fan_mode")
    if has_cool:
        d["coolboost"] = read(f"{SYS}/coolboost")
    if has_profile:
        d["ftbl"] = read(f"{SYS}/ftbl")
        d["profile"] = read(PP) if PP else None
    if os.path.exists(f"{SYS}/tkst"):
        d["tkst"] = read(f"{SYS}/tkst")
    if os.path.exists(f"{SYS}/opms"):
        d["opms"] = read(f"{SYS}/opms")
    if has_rgb:
        d["kbd_static"] = read(f"{SYS}/kbd_static")
        d["kbd_bright"] = read(f"{SYS}/kbd_bright")
    if has_dyn:
        d["kbd_dynamic"] = read(f"{SYS}/kbd_dynamic")
    if has_hwmon:
        d["ec_cpu_temp"] = read(f"{SYS}/ec_cpu_temp")

    n = None
    if has_hwmon:
        n = hwmon_find("nitro")
        if n:
            d["cpu_rpm"] = read(f"{n}/fan1_input")
            d["gpu_rpm"] = read(f"{n}/fan2_input")
            t = read(f"{n}/temp1_input")
            d["ec_c"] = (int(t) / 1000.0) if t and t.lstrip("-").isdigit() else None
    c = hwmon_find("coretemp")
    if c:
        t = read(f"{c}/temp1_input")
        d["package_c"] = (int(t) / 1000.0) if t and t.lstrip("-").isdigit() else None
    gpu_temp = None
    nv_hwmon = hwmon_find("nvidia") or hwmon_find("nouveau")
    if nv_hwmon:
        t = read(f"{nv_hwmon}/temp1_input")
        if t and t.lstrip("-").isdigit():
            gpu_temp = int(t) / 1000.0
    if gpu_temp is None:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                text=True, timeout=2, stderr=subprocess.DEVNULL).strip()
            t = out.splitlines()[0].strip() if out else ""
            gpu_temp = float(t) if t.replace(".", "", 1).isdigit() else None
        except Exception:
            gpu_temp = None
    d["gpu_c"] = gpu_temp
    has_fan_data = has_fan and d.get("fan_cpu_duty") is not None
    has_hwmon_data = has_hwmon and n
    d["driver"] = "ok" if has_fan_data or has_hwmon_data else "missing"
    return d


_cached_snapshot = None
_cached_snapshot_lock = threading.Lock()
_cached_snapshot_ts = 0.0


def get_snapshot(max_age=POLL):
    global _cached_snapshot, _cached_snapshot_ts
    now = time.time()
    with _cached_snapshot_lock:
        if _cached_snapshot is not None and (now - _cached_snapshot_ts) < max_age:
            return dict(_cached_snapshot)
    snap = snapshot()
    ts = time.time()
    snap["ts"] = ts
    with _cached_snapshot_lock:
        _cached_snapshot = snap
        _cached_snapshot_ts = ts
        return dict(snap)


def default_state():
    has_rgb = QUIRKS.has_cap(CAP_RGB)
    has_dyn = QUIRKS.has_cap(CAP_RGB_DYNAMIC)
    has_cool = QUIRKS.has_cap(CAP_COOLBOOST)
    has_profile = QUIRKS.has_cap(CAP_PLATFORM_PROFILE)

    static = {}
    if has_rgb:
        static = {str(z): {"rgb": [255, 255, 255], "on": True} for z in ZONES}

    dynamic = {}
    if has_dyn:
        dynamic = {
            "mode": 1,
            "effects": {
                str(m): {"speed": sp, "direction": 1, "r": r, "g": g, "b": b}
                for m, (sp, (r, g, b)) in EFFECT_DEFAULTS.items()
                if m in EFFECTS
            },
        }

    return {
        "mode": "dynamic" if has_dyn else "static",
        "static": static,
        "brightness": 100,
        "dynamic": dynamic,
        "fan_cpu": 50,
        "fan_gpu": 50,
        "fan_mode": {"cpu": "custom", "gpu": "custom"},
        "coolboost": False if has_cool else None,
        "profile_ac": "balanced" if has_profile else None,
        "profile_battery": "low-power" if has_profile else None,
    }


def migrate(raw):
    s = default_state()
    if "profile_ac" not in raw and "profile" in raw:
        raw["profile_ac"] = raw["profile_battery"] = raw["profile"]
    st = raw.get("static")
    if isinstance(st, dict):
        for z in ZONES:
            v = st.get(str(z))
            if isinstance(v, list) and len(v) == 3:
                rgb = [int(x) for x in v]
                st[str(z)] = {"rgb": rgb, "on": any(rgb)}
            elif isinstance(v, dict):
                rgb = [int(x) for x in v.get("rgb", [0, 0, 0])]
                st[str(z)] = {"rgb": rgb, "on": bool(v.get("on", any(rgb)))}
        s["static"] = st
    dy = raw.get("dynamic")
    if isinstance(dy, dict) and s["dynamic"].get("effects"):
        if isinstance(dy.get("effects"), dict):
            for k, v in dy["effects"].items():
                if k in s["dynamic"]["effects"] and isinstance(v, dict):
                    for kk in ("speed", "direction", "r", "g", "b"):
                        if kk in v:
                            s["dynamic"]["effects"][k][kk] = int(v[kk])
            try:
                m = int(dy.get("mode", 1))
            except (TypeError, ValueError):
                m = 1
            s["dynamic"]["mode"] = m if m in EFFECTS else 1
        else:
            try:
                cur = int(dy.get("mode", 1))
            except (TypeError, ValueError):
                cur = 1
            if cur not in EFFECTS:
                cur = 1
            e = s["dynamic"]["effects"][str(cur)]
            for kk in ("speed", "direction", "r", "g", "b"):
                if kk in dy:
                    e[kk] = int(dy[kk])
            s["dynamic"]["mode"] = cur
            if "brightness" not in raw and isinstance(dy.get("brightness"), int):
                s["brightness"] = dy["brightness"]
    for k in ("mode", "brightness", "fan_cpu", "fan_gpu", "profile_ac", "profile_battery"):
        if k in raw:
            s[k] = raw[k]
    if not QUIRKS.has_cap(CAP_RGB_DYNAMIC):
        s["mode"] = "static"
    fm = raw.get("fan_mode")
    if isinstance(fm, dict):
        for k in ("cpu", "gpu"):
            if fm.get(k) in ("auto", "max", "custom"):
                s["fan_mode"][k] = fm[k]
    if QUIRKS.has_cap(CAP_COOLBOOST):
        cb = coerce_bool(raw.get("coolboost"))
        if cb is not None:
            s["coolboost"] = cb
    else:
        s["coolboost"] = None
    if s.get("mode") not in ("static", "dynamic"):
        s["mode"] = "static"
    if QUIRKS.has_cap(CAP_PLATFORM_PROFILE):
        if s.get("profile_ac") not in PROFILES:
            s["profile_ac"] = "balanced"
        if s.get("profile_battery") not in PROFILES:
            s["profile_battery"] = "low-power"
    else:
        s["profile_ac"] = s["profile_battery"] = None
    try:
        s["brightness"] = max(0, min(100, int(s.get("brightness", 100))))
    except (TypeError, ValueError):
        s["brightness"] = 100
    for k in ("fan_cpu", "fan_gpu"):
        try:
            s[k] = max(0, min(100, int(s.get(k, 50))))
        except (TypeError, ValueError):
            s[k] = 50
    return s


def load_state():
    try:
        with open(STATE) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return None
        return migrate(raw)
    except (OSError, ValueError, TypeError):
        return None


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE)
        os.chmod(STATE, 0o600)
    except OSError as e:
        pr_err(f"save_state failed: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass


def adopt_current(s):
    snap = snapshot()
    if snap["driver"] != "ok":
        return False
    if QUIRKS.has_cap(CAP_RGB):
        try:
            parts = snap["kbd_static"].replace(":", " ").split()
            vals = [int(x) for x in parts if x.lstrip("-").isdigit()]
            for i in range(0, len(vals) - 3, 4):
                z = vals[i]
                if z in ZONES:
                    rgb = vals[i + 1:i + 4]
                    s["static"][str(z)] = {"rgb": rgb, "on": any(rgb)}
        except (TypeError, ValueError, IndexError):
            pass
    if QUIRKS.has_cap(CAP_FAN):
        try:
            s["fan_cpu"] = int(snap["fan_cpu_duty"])
        except (TypeError, ValueError):
            pass
        if QUIRKS.fan_count >= 2:
            try:
                s["fan_gpu"] = int(snap["fan_gpu_duty"])
            except (TypeError, ValueError):
                pass
    if QUIRKS.has_cap(CAP_FAN_MODE):
        for part in (snap.get("fan_mode") or "").split():
            k, _, v = part.partition("=")
            if k in ("cpu", "gpu") and v in ("auto", "max", "custom"):
                s["fan_mode"][k] = v
    if QUIRKS.has_cap(CAP_COOLBOOST):
        cb = coerce_bool(snap.get("coolboost"))
        if cb is not None:
            s["coolboost"] = cb
    if QUIRKS.has_cap(CAP_RGB):
        try:
            s["brightness"] = int(snap["kbd_bright"])
        except (TypeError, ValueError):
            pass
    if QUIRKS.has_cap(CAP_PLATFORM_PROFILE):
        if snap["profile"] in PROFILES:
            s["profile_ac"] = snap["profile"]
            s["profile_battery"] = snap["profile"]
    return True


def dyn_payload(s):
    d = s["dynamic"]
    e = d["effects"][str(d["mode"])]
    return (f"{d['mode']} {e['speed']} {s['brightness']} {e['direction']} "
            f"{e['r']} {e['g']} {e['b']}")


def apply_profile(s):
    if QUIRKS.probe:
        return True
    if not QUIRKS.has_cap(CAP_PLATFORM_PROFILE):
        return True
    if not PP:
        return False
    src = ac_source()
    prof = s["profile_battery"] if src == "battery" else s["profile_ac"]
    if prof in PROFILES:
        write(PP, prof)
        return True
    return False


def apply_fans(s):
    if QUIRKS.probe:
        return True
    if not QUIRKS.has_cap(CAP_FAN) or not QUIRKS.has_cap(CAP_FAN_MODE):
        return True
    fm = s.get("fan_mode") or {}
    cpu_m = fm.get("cpu", "custom")
    gpu_m = fm.get("gpu", "custom")
    if QUIRKS.fan_count >= 2:
        write(f"{SYS}/fan_mode", f"cpu={cpu_m} gpu={gpu_m}")
    else:
        write(f"{SYS}/fan_mode", f"cpu={cpu_m}")
    if cpu_m == "custom":
        write(f"{SYS}/fan_cpu", s["fan_cpu"])
    if QUIRKS.fan_count >= 2 and gpu_m == "custom":
        write(f"{SYS}/fan_gpu", s["fan_gpu"])


def apply_coolboost(s):
    if QUIRKS.probe:
        return True
    if not QUIRKS.has_cap(CAP_COOLBOOST):
        return True
    path = f"{SYS}/coolboost"
    if not os.path.exists(path):
        return False
    fm = s.get("fan_mode") or {}
    if fm.get("cpu") != "auto":
        return False
    if QUIRKS.fan_count >= 2 and fm.get("gpu") != "auto":
        return False
    write(path, 1 if s.get("coolboost") else 0)
    return True


def apply_kbd(s):
    if QUIRKS.probe:
        return True
    if not QUIRKS.has_cap(CAP_RGB):
        return True
    if s.get("mode") == "dynamic" and QUIRKS.has_cap(CAP_RGB_DYNAMIC):
        write(f"{SYS}/kbd_dynamic", dyn_payload(s))
    else:
        write(f"{SYS}/kbd_bright", s["brightness"])
        for z in ZONES:
            zz = s["static"][str(z)]
            r, g, b = zz["rgb"] if zz["on"] else (0, 0, 0)
            write(f"{SYS}/kbd_static", f"{z} {r} {g} {b}")


def apply_all(s):
    if QUIRKS.probe:
        return True
    if snapshot()["driver"] != "ok":
        return False
    results = []
    if QUIRKS.has_cap(CAP_PLATFORM_PROFILE):
        try:
            apply_profile(s)
            results.append(("profile", True))
        except (OSError, KeyError, ValueError) as e:
            results.append(("profile", False))
            pr_err(f"apply_profile failed: {e}")
    if QUIRKS.has_cap(CAP_FAN) and QUIRKS.has_cap(CAP_FAN_MODE):
        try:
            apply_fans(s)
            results.append(("fans", True))
        except (OSError, KeyError, ValueError) as e:
            results.append(("fans", False))
            pr_err(f"apply_fans failed: {e}")
    if QUIRKS.has_cap(CAP_COOLBOOST):
        try:
            results.append(("coolboost", apply_coolboost(s)))
        except (OSError, KeyError, ValueError) as e:
            results.append(("coolboost", False))
            pr_err(f"apply_coolboost failed: {e}")
    if QUIRKS.has_cap(CAP_RGB):
        try:
            apply_kbd(s)
            results.append(("keyboard", True))
        except (OSError, KeyError, ValueError) as e:
            results.append(("keyboard", False))
            pr_err(f"apply_kbd failed: {e}")
    return any(ok for _, ok in results)


def apply_changed_profile(s):
    try:
        apply_profile(s)
    except OSError:
        pass


state = None
state_lock = threading.Lock()


def db():
    os.makedirs(DBDIR, exist_ok=True)
    con = sqlite3.connect(DB)
    try:
        os.chmod(DB, 0o600)
    except OSError:
        pass
    con.execute("""CREATE TABLE IF NOT EXISTS samples(
        ts INTEGER PRIMARY KEY, package_c REAL, ec_c REAL, gpu_c REAL,
        cpu_rpm REAL, gpu_rpm REAL, cpu_duty REAL, gpu_duty REAL)""")
    try:
        cur = con.execute("PRAGMA table_info(samples)")
        cols = [row[1] for row in cur.fetchall()]
        if "gpu_c" not in cols:
            con.execute("ALTER TABLE samples ADD COLUMN gpu_c REAL")
            con.commit()
    except sqlite3.Error:
        pass
    return con


def cmd_status(_):
    with state_lock:
        s = json.loads(json.dumps(state))
    snap = get_snapshot(max_age=POLL)
    return {
        "ok": True,
        "data": snap,
        "desired": s,
        "source": ac_source(),
        "ts": snap.get("ts", time.time()),
        "daemon": {"uptime_s": int(time.time() - start_ts)},
    }


def cmd_history(arg):
    n = max(1, min(3600, int(arg.get("n", 120))))
    con = db()
    rows = con.execute(
        "SELECT ts,package_c,ec_c,gpu_c,cpu_rpm,gpu_rpm,cpu_duty,gpu_duty "
        "FROM samples ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    con.close()
    rows.reverse()
    return {"ok": True, "series": [
        {
            "ts": r[0],
            "package_c": r[1],
            "ec_c": r[2],
            "gpu_c": r[3],
            "cpu_rpm": r[4],
            "gpu_rpm": r[5],
            "cpu_duty": r[6],
            "gpu_duty": r[7]
        }
        for r in rows]}


def chk_zone(z):
    z = int(z)
    if z not in ZONES:
        raise ValueError(f"zone must be one of {list(ZONES)}")
    return z


def chk_rgb(v):
    v = int(v)
    if not 0 <= v <= 255:
        raise ValueError("rgb 0-255")
    return v


def cmd_set(arg):
    key = arg.get("key")
    if QUIRKS.probe:
        return {"ok": False, "err": "driver is in probe mode (read-only)"}
    with state_lock:
        if key in ("fan_cpu", "fan_gpu"):
            if not QUIRKS.has_cap(CAP_FAN):
                return {"ok": False, "err": "fan control not supported on this model"}
            if key == "fan_gpu" and QUIRKS.fan_count < 2:
                return {"ok": False, "err": "GPU fan not supported on this model"}
            v = int(arg["value"])
            if not 0 <= v <= 100:
                return {"ok": False, "err": "duty 0-100"}
            write(f"{SYS}/{key}", v)
            state[key] = v
            state["fan_mode"]["cpu" if key == "fan_cpu" else "gpu"] = "custom"
        elif key == "fan_mode":
            if not QUIRKS.has_cap(CAP_FAN_MODE):
                return {"ok": False, "err": "fan mode not supported on this model"}
            m = str(arg["value"])
            if m not in ("auto", "max", "custom"):
                return {"ok": False, "err": "mode auto|max|custom"}
            write(f"{SYS}/fan_mode", f"cpu={m} gpu={m}")
            state["fan_mode"]["cpu"] = m
            state["fan_mode"]["gpu"] = m
        elif key in ("fan_cpu_mode", "fan_gpu_mode"):
            if not QUIRKS.has_cap(CAP_FAN_MODE):
                return {"ok": False, "err": "fan mode not supported on this model"}
            if key == "fan_gpu_mode" and QUIRKS.fan_count < 2:
                return {"ok": False, "err": "GPU fan not supported on this model"}
            m = str(arg["value"])
            if m not in ("auto", "max", "custom"):
                return {"ok": False, "err": "mode auto|max|custom"}
            which, other = ("cpu", "gpu") if key == "fan_cpu_mode" else ("gpu", "cpu")
            write(f"{SYS}/fan_mode", f"{which}={m} {other}={state['fan_mode'][other]}")
            state["fan_mode"][which] = m
        elif key == "coolboost":
            if not QUIRKS.has_cap(CAP_COOLBOOST):
                return {"ok": False, "err": "coolboost not supported on this model"}
            if not QUIRKS.has_cap(CAP_FAN_MODE):
                return {"ok": False, "err": "CoolBoost requires fan mode support"}
            on = parse_coolboost(arg.get("value"))
            if on is None:
                return {"ok": False, "err": "coolboost on|off"}
            write(f"{SYS}/coolboost", 1 if on else 0)
            state["coolboost"] = on
        elif key in ("brightness", "kbd_bright"):
            if not QUIRKS.has_cap(CAP_RGB):
                return {"ok": False, "err": "RGB not supported on this model"}
            v = int(arg["value"])
            if not 0 <= v <= 100:
                return {"ok": False, "err": "brightness 0-100"}
            write(f"{SYS}/kbd_bright", v)
            state["brightness"] = v
        elif key == "static":
            if not QUIRKS.has_cap(CAP_RGB):
                return {"ok": False, "err": "RGB not supported on this model"}
            z = chk_zone(arg["zone"])
            r, g, b = chk_rgb(arg["r"]), chk_rgb(arg["g"]), chk_rgb(arg["b"])
            zz = state["static"][str(z)]
            zz["rgb"] = [r, g, b]
            if zz["on"]:
                write(f"{SYS}/kbd_static", f"{z} {r} {g} {b}")
                state["mode"] = "static"
        elif key == "static_on":
            if not QUIRKS.has_cap(CAP_RGB):
                return {"ok": False, "err": "RGB not supported on this model"}
            z = chk_zone(arg["zone"])
            on = coerce_bool(arg.get("on"))
            if on is None:
                return {"ok": False, "err": "on must be a boolean"}
            zz = state["static"][str(z)]
            if on and zz["rgb"] == [0, 0, 0]:
                zz["rgb"] = [255, 255, 255]
            zz["on"] = on
            r, g, b = zz["rgb"] if on else (0, 0, 0)
            write(f"{SYS}/kbd_static", f"{z} {r} {g} {b}")
            state["mode"] = "static"
        elif key == "dynamic":
            if not QUIRKS.has_cap(CAP_RGB_DYNAMIC):
                return {"ok": False, "err": "dynamic RGB not supported on this model"}
            d = state["dynamic"]
            cur = str(d["mode"])
            e = d["effects"][cur]
            changed = False
            if "mode" in arg:
                m = int(arg["mode"])
                if m not in EFFECTS:
                    return {"ok": False, "err": f"mode {min(EFFECTS)}-{max(EFFECTS)}"}
                d["mode"] = m
                e = d["effects"][str(m)]
                changed = True
            if "speed" in arg:
                v = int(arg["speed"])
                if not 0 <= v <= 255:
                    return {"ok": False, "err": "speed 0-255"}
                e["speed"] = v
                changed = True
            if "direction" in arg:
                v = int(arg["direction"])
                if v not in (1, 2):
                    return {"ok": False, "err": "direction 1-2"}
                e["direction"] = v
                changed = True
            if any(c in arg for c in ("r", "g", "b")):
                e["r"] = chk_rgb(arg.get("r", e["r"]))
                e["g"] = chk_rgb(arg.get("g", e["g"]))
                e["b"] = chk_rgb(arg.get("b", e["b"]))
                changed = True
            if changed:
                write(f"{SYS}/kbd_dynamic", dyn_payload(state))
                state["mode"] = "dynamic"
        elif key == "mode":
            m = str(arg["value"])
            if m not in ("static", "dynamic"):
                return {"ok": False, "err": "mode static|dynamic"}
            if m == "dynamic" and not QUIRKS.has_cap(CAP_RGB_DYNAMIC):
                return {"ok": False, "err": "dynamic RGB not supported on this model"}
            state["mode"] = m
            apply_kbd(state)
        elif key in ("profile_ac", "profile_battery"):
            if not QUIRKS.has_cap(CAP_PLATFORM_PROFILE):
                return {"ok": False, "err": "platform profile not supported on this model"}
            if not PP:
                return {"ok": False, "err": "platform profile not found"}
            v = str(arg["value"])
            if v not in PROFILES:
                return {"ok": False, "err": "bad profile"}
            state[key] = v
            src = ac_source()
            if (src == "battery") == (key == "profile_battery"):
                write(PP, v)
        else:
            return {"ok": False, "err": "unknown key"}
        save_state(state)
    return cmd_status(None)


def handle(line):
    try:
        msg = json.loads(line)
    except ValueError:
        return {"ok": False, "err": "bad json"}
    cmd = msg.get("cmd")
    try:
        if cmd == "status":
            return cmd_status(msg)
        if cmd == "history":
            return cmd_history(msg)
        if cmd == "set":
            return cmd_set(msg)
        if cmd == "reapply":
            with state_lock:
                apply_all(state)
            return cmd_status(None)
    except Exception as e:
        return {"ok": False, "err": str(e)}
    return {"ok": False, "err": "unknown command"}


def acquire_lock():
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise SystemExit("another nitrod instance is already running")
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _nitro_group():
    try:
        return grp.getgrnam(SOCK_GROUP)
    except KeyError:
        return None


def _secure_sock():
    os.chmod(SOCK, 0o660)
    g = _nitro_group()
    if g is None:
        pr_err(f"group {SOCK_GROUP} missing; socket is root-only until it exists")
        return
    try:
        os.chown(SOCK, 0, g.gr_gid)
    except OSError as e:
        pr_err(f"socket chown failed: {e}")
        return
    setfacl = shutil.which("setfacl")
    if not setfacl:
        return
    names = set(g.gr_mem)
    try:
        for p in pwd.getpwall():
            if p.pw_gid == g.gr_gid:
                names.add(p.pw_name)
    except OSError:
        pass
    for name in names:
        if not name or name == "root":
            continue
        subprocess.run(
            [setfacl, "-m", f"u:{name}:rw", SOCK],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def serve():
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    old_umask = os.umask(0o077)
    try:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCK)
    finally:
        os.umask(old_umask)
    _secure_sock()
    server.listen(5)
    server.settimeout(1.0)
    print(f"nitrod listening on {SOCK}")
    while not stop_event.is_set():
        try:
            client, _ = server.accept()
            if not client_slots.acquire(blocking=False):
                pr_err("too many clients; rejecting connection")
                client.close()
                continue
            try:
                threading.Thread(
                    target=handle_client, args=(client,), daemon=True
                ).start()
            except RuntimeError as e:
                pr_err(f"client thread failed: {e}")
                client_slots.release()
                client.close()
        except socket.timeout:
            continue
        except OSError:
            if stop_event.is_set():
                break
            raise
    server.close()
    try:
        os.unlink(SOCK)
    except OSError:
        pass


def main():
    global state
    os.umask(0o077)
    signal.signal(signal.SIGTERM, lambda s, f: stop_event.set())
    signal.signal(signal.SIGINT, lambda s, f: stop_event.set())
    lock_fd = acquire_lock()
    con = db()
    try:
        try:
            con.execute("DELETE FROM samples WHERE ts < ?",
                        (int(time.time()) - RETENTION,))
            con.commit()
        except sqlite3.Error:
            pass
        if not QUIRKS.probe:
            for _ in range(30):
                if get_snapshot(max_age=0)["driver"] == "ok":
                    break
                time.sleep(1)
        state = load_state()
        if state is None:
            state = default_state()
            if not adopt_current(state):
                apply_all(state)
        else:
            apply_all(state)
        save_state(state)

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        last_prune = time.time()
        last_src = ac_source()
        while not stop_event.is_set():
            src = ac_source()
            if src and last_src and src != last_src:
                with state_lock:
                    apply_changed_profile(state)
                last_src = src
            s = get_snapshot(max_age=0)
            if s["driver"] == "ok":
                try:
                    con.execute(
                        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?)",
                        (
                            int(s.get("ts", time.time())),
                            s.get("package_c"),
                            s.get("ec_c"),
                            s.get("gpu_c"),
                            float(s["cpu_rpm"]) if s.get("cpu_rpm") else None,
                            float(s["gpu_rpm"]) if s.get("gpu_rpm") else None,
                            float(s["fan_cpu_duty"]) if s.get("fan_cpu_duty") else None,
                            float(s["fan_gpu_duty"]) if s.get("fan_gpu_duty") else None)
                        )
                    con.commit()
                except (sqlite3.Error, ValueError, TypeError):
                    pass
            if time.time() - last_prune > 3600:
                try:
                    con.execute("DELETE FROM samples WHERE ts < ?",
                                (int(time.time()) - RETENTION,))
                    con.commit()
                except sqlite3.Error:
                    pass
                last_prune = time.time()
            stop_event.wait(POLL)
        server_thread.join(timeout=2.0)
    finally:
        con.close()
        os.close(lock_fd)


MAX_PAYLOAD = 65536

def handle_client(client):
    try:
        client.settimeout(5.0)
        data = b""
        while not data.endswith(b"\n"):
            if len(data) > MAX_PAYLOAD:
                return
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
        if not data:
            return
        msg = json.loads(data.decode())
        response = handle(json.dumps(msg))
        client.sendall((json.dumps(response) + "\n").encode())
    except Exception:
        pass
    finally:
        client.close()
        client_slots.release()


def project_version():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (
        os.path.join(here, "VERSION"),
        os.path.join(here, os.pardir, "VERSION"),
        "/usr/lib/nitro/VERSION",
        "/usr/local/lib/nitro/VERSION",
    ):
        try:
            with open(p) as f:
                v = f.read().strip()
            if v:
                return v
        except OSError:
            pass
    return VERSION


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(project_version())
        sys.exit(0)
    if os.getuid() != 0:
        pr_err("nitrod must run as root; use: sudo systemctl start nitrod")
        sys.exit(1)
    main()
