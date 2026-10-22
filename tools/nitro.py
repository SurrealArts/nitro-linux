#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import glob, json, os, pathlib, re, select, shlex, shutil, socket, struct, subprocess, sys, time

BASE = pathlib.Path("/sys/devices/platform/nitro")
SOCK = os.environ.get("NITRO_SOCK", "/run/nitro.sock")

KEY_UNIT = "nitro-key.service"
EV_KEY = 0x01
EV_MSC = 0x04
MSC_SCAN = 0x04
INPUT_EVENT = struct.Struct("@llHHi")
INPUT_EVENT_CODES = pathlib.Path("/usr/include/linux/input-event-codes.h")
MODIFIER_KEYS = {29, 42, 54, 56, 97, 100, 125, 126, 464}
_KEY_NAMES = None

def read(name):
    try:
        return (BASE / name).read_text().strip()
    except Exception as e:
        return f"MISSING ({e})"

def daemon_set(key, value):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(SOCK)
        s.sendall((json.dumps({"cmd": "set", "key": key, "value": value}) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        return json.loads(data.decode())
    except (OSError, ValueError):
        return None
    finally:
        s.close()

def status():
    for name in [
        "fan_cpu",
        "fan_gpu",
        "ftbl",
        "tkst",
        "opms",
        "kbd_static",
        "kbd_dynamic",
        "kbd_zone1",
        "coolboost",
        "ec_cpu_temp"]:
        print(f"{name}: {read(name)}")
    for p in ["/sys/class/platform-profile/platform-profile-0/profile"]:
        try:
            print(f"platform_profile: {open(p).read().strip()}")
        except Exception as e:
            print(f"platform_profile: MISSING ({e})")
    print("--- dmesg nitro ---")
    try:
        out = subprocess.run(["sudo", "dmesg"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            if "nitro" in line.lower():
                print(line[-220:])
    except Exception as e:
        print(f"dmesg failed: {e}")

def set_static(zone, r, g, b):
    zone = int(zone)
    assert zone in (1, 2, 4, 8), "zone must be 1,2,4,8"
    for v in (r, g, b):
        assert 0 <= int(v) <= 255
    (BASE / "kbd_static").write_text(f"{zone} {r} {g} {b}")
    print(f"set zone {zone} to {r} {g} {b}: {read('kbd_static')}")

def set_fan(which, duty):
    assert which in ("cpu", "gpu"), "which cpu|gpu"
    duty = int(duty)
    assert 0 <= duty <= 100
    if duty < 20:
        print("WARNING: very low duty, overheat risk")
    (BASE / f"fan_{which}").write_text(str(duty))
    print(f"fan_{which}: {read(f'fan_{which}')}")

def set_coolboost(value):
    v = str(value).lower()
    assert v in ("on", "off", "1", "0"), "coolboost on|off"
    on = v in ("on", "1")
    # Keep the daemon's desired state in sync when it is running.  The daemon
    # is the source of truth for reapply/restart; fall back to raw sysfs when
    # it is not available.
    r = daemon_set("coolboost", on)
    if r is not None:
        if not r.get("ok"):
            raise SystemExit(f"coolboost: {r.get('err', 'failed')}")
    else:
        try:
            (BASE / "coolboost").write_text("1" if on else "0")
        except OSError as e:
            raise SystemExit(f"coolboost: {e.strerror or e}")
    print(f"coolboost: {read('coolboost')}")

def set_dyn(mode, speed, brightness, direction, r, g, b):
    for name, v, lo, hi in [
        ("mode", int(mode), 1, 5),
        ("speed", int(speed), 0, 255),
        ("brightness", int(brightness), 0, 100),
        ("direction", int(direction), 1, 2),
        ("r", int(r), 0, 255),
        ("g", int(g), 0, 255),
        ("b", int(b), 0, 255)]:
        assert lo <= v <= hi, f"{name} out of range"
    (BASE / "kbd_dynamic").write_text(f"{mode} {speed} {brightness} {direction} {r} {g} {b}")
    print(f"kbd_dynamic: {read('kbd_dynamic')}")

def key_config_path():
    override = os.environ.get("NITRO_KEY_CONFIG")
    if override:
        return pathlib.Path(override).expanduser()
    base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config"))
    return base / "nitro" / "key.json"


def load_key_config():
    path = key_config_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SystemExit(f"cannot read {path}: {e}")
    if not isinstance(data, dict):
        raise SystemExit(f"invalid config {path}: expected a JSON object")
    return data


def save_key_config(cfg):
    path = key_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def key_entry(cfg):
    entry = cfg.get("key")
    return entry if isinstance(entry, dict) else None


def _key_names():
    global _KEY_NAMES
    if _KEY_NAMES is not None:
        return _KEY_NAMES
    names = {
        1: "KEY_ESC",
        28: "KEY_ENTER",
        57: "KEY_SPACE",
        190: "KEY_F20",
        272: "BTN_LEFT",
        273: "BTN_RIGHT",
        425: "KEY_PRESENTATION",
    }
    try:
        for line in INPUT_EVENT_CODES.read_text().splitlines():
            m = re.match(r"#define\s+((?:KEY|BTN)_[A-Z0-9_]+)\s+(0x[0-9a-fA-F]+|\d+)", line)
            if m:
                try:
                    names[int(m.group(2), 0)] = m.group(1)
                except ValueError:
                    pass
    except OSError:
        pass
    _KEY_NAMES = names
    return names


def key_name(code):
    return _key_names().get(int(code), f"KEY_{code}")


def key_code(name):
    wanted = name.upper()
    if not wanted.startswith(("KEY_", "BTN_")):
        wanted = "KEY_" + wanted
    for code, key in _key_names().items():
        if key == wanted:
            return code
    return None


def _run_cmd(args, timeout=3):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def udev_props(path):
    if not path:
        return {}
    r = _run_cmd(["udevadm", "info", "--query=property", f"--name={path}"])
    if not r or r.returncode:
        return {}
    props = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            props[key] = value
    return props


def _by_path(props):
    links = props.get("DEVLINKS", "").split()
    for link in links:
        if link.endswith("-event-kbd"):
            return link
    for link in links:
        if "event" in link:
            return link
    return None


def _event_number(path):
    m = re.search(r"(\d+)$", path)
    return int(m.group(1)) if m else 0


def list_input_devices(keyboards_only=False):
    devices = []
    for path in sorted(glob.glob("/dev/input/event*"), key=_event_number):
        sysname = pathlib.Path(path).name
        try:
            name = (pathlib.Path("/sys/class/input") / sysname / "device/name").read_text().strip()
        except OSError:
            name = sysname
        props = udev_props(path)
        if keyboards_only:
            has_key = (
                props.get("ID_INPUT_KEYBOARD") == "1" or
                props.get("ID_INPUT_KEY") == "1" or
                "keyboard" in name.lower()
            )
            mouse_only = (
                props.get("ID_INPUT_MOUSE") == "1" and
                props.get("ID_INPUT_KEYBOARD") != "1" and
                "keyboard" not in name.lower()
            )
            if not has_key or mouse_only:
                continue
        devices.append({
            "path": path,
            "name": name,
            "props": props,
            "by_path": _by_path(props),
        })
    return devices


def format_scan(scan):
    if scan is None:
        return None
    return f"{scan:02x}" if scan <= 0xff else f"{scan:x}"


def scan_int(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 16)
    except ValueError:
        return None


def udev_key_mapping(props, scan):
    scan = scan_int(scan)
    if scan is None:
        return None
    candidates = {f"{scan:02x}", f"{scan:04x}", f"{scan:06x}", f"{scan:08x}"}
    for key, value in props.items():
        if key.startswith("KEYBOARD_KEY_"):
            suffix = key[len("KEYBOARD_KEY_"):].lower()
            if suffix in candidates:
                return value
    return None


def _make_key(dev, scan, code):
    entry = {
        "device": dev["name"],
        "path": dev["path"],
        "keycode": int(code),
        "key_name": key_name(code),
        "scancode": format_scan(scan),
    }
    if dev.get("by_path"):
        entry["by_path"] = dev["by_path"]
    return entry


def detect_known_key():
    for dev in list_input_devices(keyboards_only=True):
        for key, value in dev["props"].items():
            if not key.startswith("KEYBOARD_KEY_") or value != "presentation":
                continue
            scancode = key[len("KEYBOARD_KEY_"):]
            try:
                scan = int(scancode, 16)
            except ValueError:
                continue
            code = key_code("KEY_PRESENTATION")
            if code is None:
                code = 425
            return _make_key(dev, scan, code)
    return None


def _read_events(fd, buffer):
    try:
        data = os.read(fd, INPUT_EVENT.size * 64)
    except BlockingIOError:
        return []
    if not data:
        raise OSError("device closed")
    buffer.extend(data)
    events = []
    while len(buffer) >= INPUT_EVENT.size:
        _, _, etype, code, value = INPUT_EVENT.unpack_from(buffer)
        del buffer[:INPUT_EVENT.size]
        events.append((etype, code, value))
    return events


def find_key(timeout=15.0):
    devices = list_input_devices(keyboards_only=True)
    poller = select.poll()
    fds = {}
    unreadable = []
    for dev in devices:
        try:
            fd = os.open(dev["path"], os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            unreadable.append((dev["path"], e))
            continue
        poller.register(fd, select.POLLIN)
        fds[fd] = dev

    if not fds:
        detail = ""
        if unreadable:
            detail = f" ({unreadable[0][1].strerror})"
        raise SystemExit(
            "no readable keyboard input devices found" + detail + "\n"
            "Add your user to the input group, then log out and back in:\n"
            "  sudo usermod -aG input \"$USER\"")

    print(f"Listening for a key for {timeout:g}s.", file=sys.stderr)
    print("Press the Nitro key now.", file=sys.stderr, flush=True)
    deadline = time.monotonic() + timeout
    scans = {fd: None for fd in fds}
    modifiers = {fd: set() for fd in fds}
    ignored = {"KEY_POWER", "KEY_SLEEP", "KEY_WAKEUP", "KEY_SUSPEND"}
    buffers = {fd: bytearray() for fd in fds}
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for fd, mask in poller.poll(max(1, min(250, int(remaining * 1000)))):
                if mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                    poller.unregister(fd)
                    os.close(fd)
                    fds.pop(fd, None)
                    scans.pop(fd, None)
                    modifiers.pop(fd, None)
                    continue
                if not mask & select.POLLIN:
                    continue
                try:
                    events = _read_events(fd, buffers[fd])
                except OSError:
                    poller.unregister(fd)
                    os.close(fd)
                    fds.pop(fd, None)
                    scans.pop(fd, None)
                    modifiers.pop(fd, None)
                    continue
                dev = fds.get(fd)
                if dev is None:
                    continue
                for etype, code, value in events:
                    if etype == EV_MSC and code == MSC_SCAN:
                        scans[fd] = value
                        continue
                    if etype != EV_KEY:
                        continue
                    scan = scans.get(fd)
                    scans[fd] = None
                    if code in MODIFIER_KEYS:
                        if value == 1:
                            modifiers[fd].add(code)
                        elif value == 0:
                            modifiers[fd].discard(code)
                        continue
                    if value != 1:
                        continue
                    name = key_name(code)
                    if not name.startswith("KEY_") or name in ignored:
                        continue
                    if any(modifiers.values()):
                        continue
                    return _make_key(dev, scan, code)
    finally:
        for fd in list(fds):
            try:
                os.close(fd)
            except OSError:
                pass
    raise SystemExit(f"no key press detected in {timeout:g}s")


def resolve_device_path(entry):
    by_path = entry.get("by_path")
    if by_path and os.path.exists(by_path):
        return by_path
    path = entry.get("path")
    if path and os.path.exists(path):
        return path
    name = entry.get("device")
    if name:
        for dev in list_input_devices():
            if dev["name"] == name:
                return dev["path"]
    return None


def _key_readable(entry):
    path = resolve_device_path(entry)
    return path, bool(path and os.access(path, os.R_OK))


def systemd_running():
    return pathlib.Path("/run/systemd/system").is_dir()


def autostart_path():
    base = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or (pathlib.Path.home() / ".config"))
    return base / "autostart" / "nitro-key.desktop"


def write_autostart():
    exe = shutil.which("nitro")
    if not exe:
        candidate = pathlib.Path(sys.argv[0]).resolve()
        if not candidate.is_file():
            raise SystemExit("cannot find the nitro command; install it or add it to PATH")
        exe = str(candidate)
    path = autostart_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Nitro key shortcut\n"
        "Comment=Run a command when the Acer NitroSense key is pressed\n"
        f"Exec={exe} key listen\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    os.chmod(path, 0o644)


def remove_autostart():
    try:
        autostart_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"cannot remove {autostart_path()}: {e}", file=sys.stderr)


def _systemctl_user(*args):
    if not systemd_running() or not shutil.which("systemctl"):
        return None
    return _run_cmd(["systemctl", "--user", *args], timeout=5)


def user_service_state():
    if os.geteuid() == 0:
        return {"load": "root", "active": "unknown", "enabled": "unknown"}
    if not systemd_running():
        return {"load": "unavailable", "active": "unknown", "enabled": "unknown"}
    r = _systemctl_user("show", "--property=LoadState", "--value", KEY_UNIT)
    if r is None:
        return {"load": "unavailable", "active": "unknown", "enabled": "unknown"}
    load = r.stdout.strip() or "unknown"
    if load == "not-found":
        return {"load": load, "active": "not-running", "enabled": "not-installed"}
    active = _systemctl_user("is-active", KEY_UNIT)
    enabled = _systemctl_user("is-enabled", KEY_UNIT)
    return {
        "load": load,
        "active": (active.stdout.strip() if active else "unknown") or "unknown",
        "enabled": (enabled.stdout.strip() if enabled else "unknown") or "unknown",
    }


def _user_environment():
    env = dict(os.environ)
    if os.geteuid() == 0 or not shutil.which("systemctl"):
        return env
    r = _systemctl_user("show-environment")
    if r and r.returncode == 0:
        for line in r.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env[key] = value
    return env


def _launch_command(command):
    subprocess.Popen(
        command,
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=_user_environment(),
    )


def _listen_key(entry, command, once=False):
    want_scan = scan_int(entry.get("scancode"))
    want_code = entry.get("keycode")
    try:
        want_code = int(want_code) if want_code is not None else None
    except (TypeError, ValueError):
        want_code = None
    if want_scan is None and want_code is None:
        raise SystemExit("key config has no scancode or keycode; run 'nitro key find' again")

    label = entry.get("key_name") or "Nitro key"
    print(f"Listening for {label} -> {command}", file=sys.stderr, flush=True)
    missing_reported = False
    while True:
        path = resolve_device_path(entry)
        if not path:
            if not missing_reported:
                print(f"Waiting for {entry.get('device') or 'keyboard'}...", file=sys.stderr, flush=True)
                missing_reported = True
            time.sleep(2)
            continue
        missing_reported = False
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f"cannot open {path}: {e.strerror or e}", file=sys.stderr, flush=True)
            time.sleep(2)
            continue

        poller = select.poll()
        poller.register(fd, select.POLLIN)
        buffer = bytearray()
        pending_scan = None
        modifiers = set()
        last_trigger = 0.0
        print(f"Listening on {path}", file=sys.stderr, flush=True)
        try:
            while True:
                try:
                    ready = poller.poll(1000)
                except OSError:
                    break
                if not ready:
                    continue
                for ready_fd, mask in ready:
                    if ready_fd != fd:
                        continue
                    disconnected = bool(mask & (select.POLLERR | select.POLLHUP | select.POLLNVAL))
                    if mask & select.POLLIN:
                        try:
                            events = _read_events(fd, buffer)
                        except OSError:
                            disconnected = True
                            events = []
                        for etype, code, value in events:
                            if etype == EV_MSC and code == MSC_SCAN:
                                pending_scan = value
                                continue
                            if etype != EV_KEY:
                                continue
                            scan = pending_scan
                            pending_scan = None
                            if code in MODIFIER_KEYS:
                                if value == 1:
                                    modifiers.add(code)
                                elif value == 0:
                                    modifiers.discard(code)
                                continue
                            if value != 1:
                                continue
                            matched = (
                                (want_scan is not None and scan == want_scan) or
                                (want_code is not None and code == want_code)
                            )
                            if not matched or modifiers:
                                continue
                            now = time.monotonic()
                            if now - last_trigger < 0.6:
                                continue
                            last_trigger = now
                            print(f"triggered: {command}", file=sys.stderr, flush=True)
                            try:
                                _launch_command(command)
                            except OSError as e:
                                print(f"cannot run command: {e}", file=sys.stderr, flush=True)
                            if once:
                                return 0
                    if disconnected:
                        print(f"device disconnected: {path}", file=sys.stderr, flush=True)
                        break
                else:
                    continue
                break
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        if once:
            return 1
        time.sleep(1)


def key_usage():
    prog = pathlib.Path(sys.argv[0]).name
    print("usage:")
    print(f"    {prog} key [status]")
    print(f"    {prog} key auto")
    print(f"    {prog} key find [--timeout SECONDS] [--no-save]")
    print(f"    {prog} key bind [--no-service] [--] [command ...]")
    print(f"    {prog} key unbind")
    print(f"    {prog} key listen [--once]")
    print("")
    print("The Nitro key is captured from /dev/input without changing the udev")
    print("mapping.  auto/find/status/bind run as your user; do not use sudo.")
    print("If access is denied, add your user to the input group:")
    print("    sudo usermod -aG input \"$USER\"")
    print("then log out and back in.")
    print(f"Config: {key_config_path()}")


def key_status():
    cfg = load_key_config()
    entry = key_entry(cfg)
    command = cfg.get("command")
    print("Nitro key")
    if entry:
        code = entry.get("keycode")
        scan = entry.get("scancode")
        name = entry.get("key_name") or (key_name(code) if code is not None else "unknown")
        details = []
        if code is not None:
            details.append(f"code {code}")
        if scan:
            details.append(f"scancode {scan}")
        print(f"  key:       {name}" + (f" ({', '.join(details)})" if details else ""))
        print(f"  device:    {entry.get('device') or 'unknown'}")
        path = resolve_device_path(entry)
        shown_path = path or entry.get("by_path") or entry.get("path")
        print(f"  path:      {shown_path or 'not found'}")
        props = udev_props(shown_path)
        mapping = udev_key_mapping(props, scan)
        print(f"  udev:      KEYBOARD_KEY_{scan}={mapping}" if mapping and scan else "  udev:      not mapped")
        readable_path, readable = _key_readable(entry)
        if not readable_path:
            print("  access:    device not found")
        elif readable:
            print("  access:    readable")
        else:
            print("  access:    NOT readable (add your user to the input group)")
    else:
        print("  key:       not configured")
        detected = detect_known_key()
        if detected:
            print(f"  detected:  {detected.get('key_name')} (scancode {detected.get('scancode')})")
            print(f"  device:    {detected.get('device')}")
            print(f"  path:      {detected.get('by_path') or detected.get('path')}")
            print("             run 'nitro key find' to confirm and save it")
        else:
            print("             run 'nitro key find' and press the Nitro key")

    if command:
        print(f"  binding:   {command}")
    else:
        print("  binding:   none (run 'nitro key bind')")

    state = user_service_state()
    if state["load"] == "not-found":
        print("  service:   not installed")
        print(f"  autostart: {'installed' if autostart_path().is_file() else 'not installed'}")
    elif state["load"] == "root":
        print("  service:   unknown (run as your user, not root)")
    elif state["load"] in ("unknown", "unavailable"):
        print("  service:   unavailable (systemd user services not found)")
        print(f"  autostart: {'installed' if autostart_path().is_file() else 'not installed'}")
    else:
        print(f"  service:   {state['active']} ({state['enabled']})")
        if command and state["active"] != "active":
            print("             run 'nitro key bind' to enable/restart it")
    print(f"  config:    {key_config_path()}")


def key_find(args):
    if os.geteuid() == 0:
        raise SystemExit("run 'nitro key find' as your user, not with sudo")
    timeout = 15.0
    save = True
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            key_usage()
            return 0
        if arg in ("-t", "--timeout"):
            i += 1
            if i >= len(args):
                raise SystemExit("--timeout needs a value")
            try:
                timeout = float(args[i])
            except ValueError:
                raise SystemExit("--timeout must be a number")
            if timeout <= 0:
                raise SystemExit("--timeout must be positive")
        elif arg == "--no-save":
            save = False
        else:
            raise SystemExit(f"unknown option: {arg}")
        i += 1

    cfg = load_key_config()
    restart_after = bool(cfg.get("command")) and user_service_state().get("active") == "active"
    if restart_after:
        print("Stopping the old listener while capturing.", file=sys.stderr)
        _systemctl_user("stop", KEY_UNIT)
    try:
        entry = find_key(timeout)
    except BaseException:
        if restart_after:
            _systemctl_user("start", KEY_UNIT)
        raise

    print(f"Found {entry['key_name']} (code {entry['keycode']}, scancode {entry['scancode'] or 'none'})")
    print(f"  device:    {entry['device']}")
    print(f"  path:      {entry.get('by_path') or entry['path']}")
    props = udev_props(entry.get("by_path") or entry.get("path"))
    mapping = udev_key_mapping(props, entry.get("scancode"))
    if mapping:
        print(f"  udev:      KEYBOARD_KEY_{entry['scancode']}={mapping}")
    if save:
        cfg["version"] = 1
        cfg["key"] = entry
        save_key_config(cfg)
        print(f"Saved to {key_config_path()}")
        if not cfg.get("command"):
            print("Next: nitro key bind")
        elif restart_after or user_service_state().get("active") == "active":
            restarted = _systemctl_user("restart", KEY_UNIT)
            if restarted is not None and restarted.returncode == 0:
                print("Restarted nitro-key.service with the new key.")
            else:
                print("Saved, but nitro-key.service could not be restarted.")
        else:
            print("Config saved. Run 'nitro key bind' to start the service.")
    elif restart_after:
        _systemctl_user("start", KEY_UNIT)
    return 0


def default_key_command():
    exe = shutil.which("nitro-linux") or "nitro-linux"
    quoted = shlex.quote(exe)
    terminal = shutil.which("xdg-terminal-exec")
    if terminal:
        return f"{shlex.quote(terminal)} {quoted}"
    terminal = shutil.which("kitty")
    if terminal:
        return (
            f"{shlex.quote(terminal)} "
            "--override remember_window_size=no "
            "--override initial_window_width=120c "
            "--override initial_window_height=47c "
            f"{quoted}"
        )
    terminal = shutil.which("alacritty")
    if terminal:
        return (
            f"{shlex.quote(terminal)} "
            "-o window.dimensions.columns=120 "
            "-o window.dimensions.lines=47 "
            f"-e {quoted}"
        )
    terminal = shutil.which("foot")
    if terminal:
        return f"{shlex.quote(terminal)} -w 120x47 {quoted}"
    terminal = shutil.which("wezterm")
    if terminal:
        return f"{shlex.quote(terminal)} start -- {quoted}"
    terminal = shutil.which("konsole")
    if terminal:
        return f"{shlex.quote(terminal)} --geometry 120x47 -e {quoted}"
    terminal = shutil.which("gnome-terminal")
    if terminal:
        return f"{shlex.quote(terminal)} --window -- {quoted}"
    terminal = shutil.which("xfce4-terminal")
    if terminal:
        return f"{shlex.quote(terminal)} --geometry=120x47 -x {quoted}"
    terminal = shutil.which("mate-terminal")
    if terminal:
        return f"{shlex.quote(terminal)} --geometry=120x47 -x {quoted}"
    terminal = shutil.which("x-terminal-emulator")
    if terminal:
        return f"{shlex.quote(terminal)} -geometry 120x47 -e {quoted}"
    terminal = shutil.which("xterm")
    if terminal:
        return f"{shlex.quote(terminal)} -geometry 120x47 -e {quoted}"
    return quoted


def activate_listener():
    if systemd_running() and shutil.which("systemctl"):
        _systemctl_user("daemon-reload")
        state = user_service_state()
        if state["load"] not in ("not-found", "unavailable", "unknown"):
            enabled = _systemctl_user("enable", KEY_UNIT)
            if enabled is None or enabled.returncode:
                print("The user service could not be enabled.")
                if enabled and enabled.stderr.strip():
                    print(enabled.stderr.strip())
                return 1
            restarted = _systemctl_user("restart", KEY_UNIT)
            if restarted is None or restarted.returncode:
                print("The listener did not start.")
                if restarted and restarted.stderr.strip():
                    print(restarted.stderr.strip())
                print("Check: journalctl --user -u nitro-key.service -n 20 --no-pager")
                return 1
            remove_autostart()
            print("Service: active (enabled). Press the Nitro key to run the command.")
            print("Desktop shortcuts are unchanged; if another shortcut uses this key, remove it in your desktop settings and run 'nitro key auto' again.")
            return 0
        print("The user service is not available; using XDG autostart.")
    write_autostart()
    print(f"Autostart: {autostart_path()} (starts at next login)")
    print("Start now: nitro key listen")
    print("Desktop shortcuts are unchanged; if another shortcut uses this key, remove it in your desktop settings and run 'nitro key auto' again.")
    return 0


def key_bind(args):
    if os.geteuid() == 0:
        raise SystemExit("run 'nitro key bind' as your user, not with sudo")
    words = []
    use_service = True
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-h", "--help"):
            key_usage()
            return 0
        if arg == "--no-service":
            use_service = False
        elif arg == "--command":
            i += 1
            if i >= len(args):
                raise SystemExit("--command needs a value")
            words.append(args[i])
        elif arg == "--":
            words.extend(args[i + 1:])
            break
        elif arg.startswith("-"):
            raise SystemExit(f"unknown option: {arg}")
        else:
            words.append(arg)
        i += 1

    command = " ".join(words).strip() or default_key_command()
    cfg = load_key_config()
    entry = key_entry(cfg)
    if entry is None:
        entry = detect_known_key()
        if entry:
            print(f"Detected {entry['key_name']} (scancode {entry.get('scancode')})")
        else:
            if not sys.stdin.isatty():
                raise SystemExit("no key configured; run 'nitro key find' first")
            entry = find_key()
    cfg["version"] = 1
    cfg["key"] = entry
    cfg["command"] = command
    save_key_config(cfg)

    print(f"Bound {entry.get('key_name') or 'Nitro key'} to:")
    print(f"    {command}")
    print(f"Config: {key_config_path()}")
    path, readable = _key_readable(entry)
    if not path:
        print(f"Warning: device not found ({entry.get('device') or 'unknown'})")
    elif not readable:
        print("Warning: cannot read the input device yet.")
        print("Add your user to the input group, then log out and back in:")
        print("    sudo usermod -aG input \"$USER\"")

    if not use_service:
        if user_service_state().get("active") == "active":
            print("Warning: nitro-key.service is still running with the old config.")
        elif autostart_path().is_file():
            print("Warning: the XDG autostart listener is still running with the old config.")
        print("Service unchanged (--no-service).")
        return 0
    if path and not readable:
        print("User service not started until input access is fixed.")
        return 1
    return activate_listener()


def key_auto(args):
    for arg in args:
        if arg in ("-h", "--help"):
            key_usage()
            return 0
        raise SystemExit(f"unknown option: {arg}")
    if os.geteuid() == 0:
        raise SystemExit("run 'nitro key auto' as your user, not with sudo")
    cfg = load_key_config()
    entry = key_entry(cfg)
    changed = False
    if entry is None:
        entry = detect_known_key()
        if entry is None:
            raise SystemExit("could not detect the Nitro key; run 'nitro key find' as your user")
        cfg["version"] = 1
        cfg["key"] = entry
        changed = True
        print(f"Detected {entry['key_name']} (scancode {entry.get('scancode')})")
    else:
        print(f"Key: {entry.get('key_name') or 'Nitro key'} (saved)")
    command = cfg.get("command")
    if command:
        print(f"Binding kept: {command}")
    else:
        command = default_key_command()
        cfg["version"] = 1
        cfg["command"] = command
        changed = True
        print(f"Binding: {command}")
    if changed:
        save_key_config(cfg)
    print(f"Config: {key_config_path()}")
    path, readable = _key_readable(entry)
    if not path:
        print(f"Warning: device not found ({entry.get('device') or 'unknown'})")
        return 1
    if not readable:
        print("Warning: cannot read the input device yet.")
        print("Add your user to the input group, then log out and back in:")
        print("    sudo usermod -aG input \"$USER\"")
        return 1
    return activate_listener()


def key_unbind(args):
    if args:
        raise SystemExit("usage: nitro key unbind")
    if os.geteuid() == 0:
        raise SystemExit("run 'nitro key unbind' as your user, not with sudo")
    cfg = load_key_config()
    had_command = bool(cfg.get("command"))
    cfg["command"] = None
    if cfg:
        save_key_config(cfg)
    state = user_service_state()
    if state["load"] in ("not-found", "unavailable", "unknown"):
        had_autostart = autostart_path().is_file()
        remove_autostart()
        if had_autostart:
            print("Unbound and removed the autostart entry.")
        elif state["load"] == "not-found":
            print("Unbound. The user service is not installed.")
        else:
            print("Unbound. No listener was installed.")
    else:
        _systemctl_user("disable", "--now", KEY_UNIT)
        remove_autostart()
        print("Unbound and stopped nitro-key.service.")
    if not had_command:
        print("There was no command bound.")
    return 0


def key_listen(args):
    if os.geteuid() == 0:
        raise SystemExit("run 'nitro key listen' as your user, not with sudo")
    once = False
    for arg in args:
        if arg in ("-h", "--help"):
            key_usage()
            return 0
        if arg == "--once":
            once = True
        else:
            raise SystemExit(f"unknown option: {arg}")
    cfg = load_key_config()
    entry = key_entry(cfg)
    if entry is None:
        raise SystemExit("no key configured; run 'nitro key find' first")
    command = cfg.get("command")
    if not command:
        raise SystemExit("no command bound; run 'nitro key bind' first")
    try:
        return _listen_key(entry, command, once=once)
    except KeyboardInterrupt:
        return 0


def usage():
    prog = pathlib.Path(sys.argv[0]).name
    print("usage:")
    print(f"    {prog} [status]")
    print("               | rgb <zone 1|2|4|8> <r> <g> <b>")
    print("               | fan <cpu|gpu> <0-100>")
    print("               | coolboost <on|off>          (fans must be auto)")
    print("               | dyn <mode 1-5> <speed> <brightness 0-100> <dir 1-2> <r> <g> <b>")
    print("               | profile <low-power|quiet|balanced|balanced-performance|performance>")
    print("               | key [status|auto|find|bind|unbind|listen]")
    print("modes:")
    print("    1 breath")
    print("    2 neon")
    print("    3 wave")
    print("    4 shifting")
    print("    5 zoom")
    print("Note:")
    print("    sysfs is root-owned, run with sudo for writes,")
    print(f"    e.g. sudo {prog} rgb 1 255 0 0")
    print("    key commands are user-level; do not run them with sudo")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("status",):
        status()
    elif sys.argv[1] in ("help", "-h", "--help"):
        usage()
    elif sys.argv[1] == "rgb" and len(sys.argv) == 6:
        set_static(*sys.argv[2:6])
    elif sys.argv[1] == "fan" and len(sys.argv) == 4:
        set_fan(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "coolboost" and len(sys.argv) == 3:
        set_coolboost(sys.argv[2])
    elif sys.argv[1] == "dyn" and len(sys.argv) == 9:
        set_dyn(*sys.argv[2:9])
    elif sys.argv[1] == "profile" and len(sys.argv) == 3:
        assert sys.argv[2] in ("low-power", "quiet", "balanced", "balanced-performance", "performance"), "bad profile"
        pathlib.Path("/sys/class/platform-profile/platform-profile-0/profile").write_text(sys.argv[2])
        print(f"platform_profile: {open('/sys/class/platform-profile/platform-profile-0/profile').read().strip()}")
    elif sys.argv[1] == "key":
        if len(sys.argv) < 3 or sys.argv[2] in ("status",):
            key_status()
        elif sys.argv[2] in ("help", "-h", "--help"):
            key_usage()
        elif sys.argv[2] == "auto":
            key_auto(sys.argv[3:])
        elif sys.argv[2] in ("find", "capture"):
            key_find(sys.argv[3:])
        elif sys.argv[2] == "bind":
            key_bind(sys.argv[3:])
        elif sys.argv[2] == "unbind":
            key_unbind(sys.argv[3:])
        elif sys.argv[2] == "listen":
            key_listen(sys.argv[3:])
        else:
            key_usage()
            raise SystemExit(2)
    else:
        usage()
