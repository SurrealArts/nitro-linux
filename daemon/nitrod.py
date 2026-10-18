#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
nitrod - daemon

State persists in /var/lib/nitro/state.json,
history in /var/lib/nitro/history.db (24h).

State layout (0.8.0)
    mode            "static" | "dynamic"          active keyboard display
    static          {"1": {"rgb": [r,g,b], "on": bool}, ...}
                    zones on EC keys 1,2,4,8; on/off is independent of the
                    stored rgb (an off zone keeps its colour for re-enable)
    dynamic         {"mode": 1-5, "effects": {"1": {"speed","direction","r","g","b"}, ...}}
                    config is preserved per effect; switching loads it back
    brightness      0-100, global (used by both static and dynamic)
    fan_cpu/fan_gpu 0-100 duty, remembered even while fans run auto/max
    fan_mode        {"cpu": "auto|max|custom", "gpu": ...}
    profile_ac      platform profile applied while on mains power
    profile_battery platform profile applied while on battery
"""
import json
import os
import socket
import subprocess
import sqlite3
import sys
import threading
import time
from glob import glob

UID = os.getuid()
RUNTIME = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{UID}")
SOCK = os.environ.get("NITRO_SOCK", "/run/nitro.sock")
SYS = os.environ.get("NITRO_SYS", "/sys/devices/platform/nitro")
PP = os.environ.get("NITRO_PP", "/sys/class/platform-profile/platform-profile-0/profile")
HWMON = os.environ.get("NITRO_HWMON", "/sys/class/hwmon")
POWER = os.environ.get("NITRO_POWER", "/sys/class/power_supply")
STATE = os.environ.get("NITRO_STATE", "/var/lib/nitro/state.json")
DBDIR = os.environ.get("NITRO_DBDIR", "/var/lib/nitro")
DB = os.path.join(DBDIR, "history.db")
POLL = 1.0
RETENTION = 86400

PROFILES = ("low-power", "quiet", "balanced", "balanced-performance", "performance")
ZONES = (1, 2, 4, 8)
EFFECT_DEFAULTS = {
    1: (4, (255, 0, 255)),   # breath magenta
    2: (3, (0, 255, 255)),   # neon cyan
    3: (5, (0, 128, 255)),   # wave blue
    4: (5, (128, 0, 255)),   # shifting purple
    5: (7, (0, 255, 0)),     # zoom green
}

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


def hwmon_find(name):
    try:
        for h in sorted(os.listdir(HWMON)):
            if read(os.path.join(HWMON, h, "name")) == name:
                return os.path.join(HWMON, h)
    except OSError:
        pass
    return None


def ac_source():
    """'ac' | 'battery' | None - derived from the Mains power supply."""
    for t in sorted(glob(f"{POWER}/*/type")):
        if read(t) == "Mains":
            online = read(os.path.join(os.path.dirname(t), "online"))
            return "ac" if online != "0" else "battery"
    return None


def snapshot():
    d = {}
    d["fan_cpu_duty"] = read(f"{SYS}/fan_cpu")
    d["fan_gpu_duty"] = read(f"{SYS}/fan_gpu")
    d["ftbl"] = read(f"{SYS}/ftbl")
    d["tkst"] = read(f"{SYS}/tkst")
    d["opms"] = read(f"{SYS}/opms")
    d["kbd_static"] = read(f"{SYS}/kbd_static")
    d["kbd_dynamic"] = read(f"{SYS}/kbd_dynamic")
    d["kbd_bright"] = read(f"{SYS}/kbd_bright")
    d["fan_mode"] = read(f"{SYS}/fan_mode")
    d["ec_cpu_temp"] = read(f"{SYS}/ec_cpu_temp")
    d["profile"] = read(PP)
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
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
            text=True, timeout=2, stderr=subprocess.DEVNULL).strip()
        t = out.splitlines()[0].strip() if out else ""
        d["gpu_c"] = float(t) if t.replace(".", "", 1).isdigit() else None
    except Exception:
        d["gpu_c"] = None
    d["driver"] = "ok" if d["fan_cpu_duty"] is not None else "missing"
    return d


def default_state():
    return {
        "mode": "static",
        "static": {str(z): {"rgb": [255, 255, 255], "on": True} for z in ZONES},
        "brightness": 100,
        "dynamic": {
            "mode": 1,
            "effects": {
                str(m): {"speed": sp, "direction": 1, "r": r, "g": g, "b": b}
                for m, (sp, (r, g, b)) in EFFECT_DEFAULTS.items()
            },
        },
        "fan_cpu": 50,
        "fan_gpu": 50,
        "fan_mode": {"cpu": "custom", "gpu": "custom"},
        "profile_ac": "balanced",
        "profile_battery": "low-power",
    }


def migrate(raw):
    """Normalise any older state layout into the current one."""
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
    if isinstance(dy, dict):
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
            s["dynamic"]["mode"] = m if 1 <= m <= 5 else 1
        else:
            try:
                cur = int(dy.get("mode", 1))
            except (TypeError, ValueError):
                cur = 1
            if cur not in EFFECT_DEFAULTS:
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
    fm = raw.get("fan_mode")
    if isinstance(fm, dict):
        for k in ("cpu", "gpu"):
            if fm.get(k) in ("auto", "max", "custom"):
                s["fan_mode"][k] = fm[k]
    if s.get("mode") not in ("static", "dynamic"):
        s["mode"] = "static"
    if s.get("profile_ac") not in PROFILES:
        s["profile_ac"] = "balanced"
    if s.get("profile_battery") not in PROFILES:
        s["profile_battery"] = "low-power"
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
    except (OSError, ValueError):
        return None


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, STATE)


def adopt_current(s):
    snap = snapshot()
    if snap["driver"] != "ok":
        return False
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
    try:
        s["fan_cpu"] = int(snap["fan_cpu_duty"])
        s["fan_gpu"] = int(snap["fan_gpu_duty"])
    except (TypeError, ValueError):
        pass
    for part in (snap.get("fan_mode") or "").split():
        k, _, v = part.partition("=")
        if k in ("cpu", "gpu") and v in ("auto", "max", "custom"):
            s["fan_mode"][k] = v
    try:
        s["brightness"] = int(snap["kbd_bright"])
    except (TypeError, ValueError):
        pass
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
    src = ac_source()
    prof = s["profile_battery"] if src == "battery" else s["profile_ac"]
    if prof in PROFILES:
        write(PP, prof)
        return True
    return False


def apply_fans(s):
    fm = s.get("fan_mode") or {}
    cpu_m = fm.get("cpu", "custom")
    gpu_m = fm.get("gpu", "custom")
    write(f"{SYS}/fan_mode", f"cpu={cpu_m} gpu={gpu_m}")
    if cpu_m == "custom":
        write(f"{SYS}/fan_cpu", s["fan_cpu"])
    if gpu_m == "custom":
        write(f"{SYS}/fan_gpu", s["fan_gpu"])


def apply_kbd(s):
    if s.get("mode") == "dynamic":
        write(f"{SYS}/kbd_dynamic", dyn_payload(s))
    else:
        write(f"{SYS}/kbd_bright", s["brightness"])
        for z in ZONES:
            zz = s["static"][str(z)]
            r, g, b = zz["rgb"] if zz["on"] else (0, 0, 0)
            write(f"{SYS}/kbd_static", f"{z} {r} {g} {b}")


def apply_all(s):
    if snapshot()["driver"] != "ok":
        return False
    try:
        apply_profile(s)
        apply_fans(s)
        apply_kbd(s)
        return True
    except (OSError, KeyError, ValueError):
        return False


def apply_changed_profile(s):
    """Called when the power source changes while running."""
    try:
        apply_profile(s)
    except OSError:
        pass


state = None
state_lock = threading.Lock()


def db():
    os.makedirs(DBDIR, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS samples(
        ts INTEGER PRIMARY KEY, package_c REAL, ec_c REAL, gpu_c REAL,
        cpu_rpm REAL, gpu_rpm REAL, cpu_duty REAL, gpu_duty REAL)""")
    try:
        con.execute("ALTER TABLE samples ADD COLUMN gpu_c REAL")
        con.commit()
    except sqlite3.Error:
        pass
    return con


def cmd_status(_):
    with state_lock:
        s = json.loads(json.dumps(state))
    snap = snapshot()
    return {"ok": True, "data": snap, "desired": s, "source": ac_source(),
            "daemon": {"uptime_s": int(time.time() - start_ts)}}


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
        raise ValueError("zone must be 1,2,4,8")
    return z


def chk_rgb(v):
    v = int(v)
    if not 0 <= v <= 255:
        raise ValueError("rgb 0-255")
    return v


def cmd_set(arg):
    key = arg.get("key")
    with state_lock:
        if key in ("fan_cpu", "fan_gpu"):
            v = int(arg["value"])
            if not 0 <= v <= 100:
                return {"ok": False, "err": "duty 0-100"}
            write(f"{SYS}/{key}", v)
            state[key] = v
            state["fan_mode"]["cpu" if key == "fan_cpu" else "gpu"] = "custom"
        elif key == "fan_mode":
            m = str(arg["value"])
            if m not in ("auto", "max", "custom"):
                return {"ok": False, "err": "mode auto|max|custom"}
            write(f"{SYS}/fan_mode", f"cpu={m} gpu={m}")
            state["fan_mode"]["cpu"] = m
            state["fan_mode"]["gpu"] = m
        elif key in ("fan_cpu_mode", "fan_gpu_mode"):
            m = str(arg["value"])
            if m not in ("auto", "max", "custom"):
                return {"ok": False, "err": "mode auto|max|custom"}
            which, other = ("cpu", "gpu") if key == "fan_cpu_mode" else ("gpu", "cpu")
            write(f"{SYS}/fan_mode", f"{which}={m} {other}={state['fan_mode'][other]}")
            state["fan_mode"][which] = m
        elif key in ("brightness", "kbd_bright"):
            v = int(arg["value"])
            if not 0 <= v <= 100:
                return {"ok": False, "err": "brightness 0-100"}
            write(f"{SYS}/kbd_bright", v)
            state["brightness"] = v
        elif key == "static":
            z = chk_zone(arg["zone"])
            r, g, b = chk_rgb(arg["r"]), chk_rgb(arg["g"]), chk_rgb(arg["b"])
            zz = state["static"][str(z)]
            zz["rgb"] = [r, g, b]
            if zz["on"]:
                write(f"{SYS}/kbd_static", f"{z} {r} {g} {b}")
                state["mode"] = "static"
        elif key == "static_on":
            z = chk_zone(arg["zone"])
            on = bool(arg["on"])
            zz = state["static"][str(z)]
            if on and zz["rgb"] == [0, 0, 0]:
                zz["rgb"] = [255, 255, 255]
            zz["on"] = on
            r, g, b = zz["rgb"] if on else (0, 0, 0)
            write(f"{SYS}/kbd_static", f"{z} {r} {g} {b}")
            state["mode"] = "static"
        elif key == "dynamic":
            d = state["dynamic"]
            cur = str(d["mode"])
            e = d["effects"][cur]
            changed = False
            if "mode" in arg:
                m = int(arg["mode"])
                if not 1 <= m <= 5:
                    return {"ok": False, "err": "mode 1-5"}
                d["mode"] = m
                e = d["effects"][str(m)]
                changed = True
            if "speed" in arg:
                v = int(arg["speed"])
                if not 1 <= v <= 10:
                    return {"ok": False, "err": "speed 1-10"}
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
            state["mode"] = m
            apply_kbd(state)
        elif key in ("profile_ac", "profile_battery"):
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
                ok = apply_all(state)
            return {"ok": ok}
        if cmd == "stop":
            stop_event.set()
            return {"ok": True}
        return {"ok": False, "err": "unknown cmd"}
    except (OSError, ValueError, KeyError) as e:
        return {"ok": False, "err": str(e)}


def serve():
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    try:
        os.chmod(SOCK, 0o666)
    except OSError:
        pass
    srv.listen(8)
    srv.settimeout(0.5)
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        with conn:
            try:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    conn.sendall((json.dumps(handle(data.decode())) + "\n").encode())
            except OSError:
                pass
    srv.close()
    try:
        os.unlink(SOCK)
    except OSError:
        pass


def main():
    global state
    con = db()
    con.execute("DELETE FROM samples WHERE ts < ?", (int(time.time()) - RETENTION,))
    con.commit()
    for _ in range(30):
        if snapshot()["driver"] == "ok":
            break
        time.sleep(1)
    state = load_state()
    if state is None:
        state = default_state()
        if adopt_current(state):
            save_state(state)
        else:
            apply_all(state)
            save_state(state)
    else:
        save_state(state)
        apply_all(state)
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    last_prune = time.time()
    last_src = ac_source()
    while not stop_event.is_set():
        src = ac_source()
        if src and last_src and src != last_src:
            with state_lock:
                apply_changed_profile(state)
            last_src = src
        s = snapshot()
        if s["driver"] == "ok":
            try:
                con.execute(
                    "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?)", (
                        int(time.time()), s.get("package_c"), s.get("ec_c"),
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
                con.execute("DELETE FROM samples WHERE ts < ?", (int(time.time()) - RETENTION,))
                con.commit()
            except sqlite3.Error:
                pass
            last_prune = time.time()
        stop_event.wait(POLL)
    con.close()


if __name__ == "__main__":
    sys.exit(main())
