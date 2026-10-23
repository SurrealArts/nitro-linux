#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import dataclass
from typing import Optional
import pathlib


CAP_FAN = 1 << 0
CAP_FAN_MODE = 1 << 1
CAP_HWMON = 1 << 2
CAP_PLATFORM_PROFILE = 1 << 3
CAP_RGB = 1 << 4
CAP_RGB_DYNAMIC = 1 << 5
CAP_COOLBOOST = 1 << 6
CAP_LED_CLASS = 1 << 7

PROFILE_NONE = 0
PROFILE_FTBL = 1
PROFILE_OPMS = 2

COOLBOOST_NONE = 0
COOLBOOST_EC10 = 1
COOLBOOST_WMI = 2


@dataclass(frozen=True)
class NitroQuirks:
    capabilities: int
    rgb_zones: int
    dynamic_effects: int
    profile_backend: int
    coolboost_backend: int
    fan_count: int
    has_hwmon: bool
    has_platform_profile: bool
    has_led_class: bool
    board_name: Optional[str] = None
    tested_bios: Optional[str] = None
    probe: bool = False

    def has_cap(self, cap: int) -> bool:
        return bool(self.capabilities & cap)

    def capability_dict(self) -> dict:
        caps = {}
        if self.has_cap(CAP_FAN):
            caps["fan"] = 1
        if self.has_cap(CAP_FAN_MODE):
            caps["fan_mode"] = 1
        if self.has_cap(CAP_HWMON):
            caps["hwmon"] = 1
        if self.has_cap(CAP_PLATFORM_PROFILE):
            caps["profile"] = "ftbl" if self.profile_backend == PROFILE_FTBL else "opms"
        if self.has_cap(CAP_RGB):
            caps["rgb"] = self.rgb_zones
        if self.has_cap(CAP_RGB_DYNAMIC):
            caps["dyn"] = self.dynamic_effects
        if self.has_cap(CAP_COOLBOOST):
            caps["coolboost"] = "ec10" if self.coolboost_backend == COOLBOOST_EC10 else "wmi"
        if self.has_cap(CAP_LED_CLASS):
            caps["led"] = 1
        return caps

    def capabilities_string(self) -> str:
        d = self.capability_dict()
        return " ".join(f"{k}={v}" for k, v in sorted(d.items()))


MODEL_QUIRKS = {
    "Nitro AN515-57": NitroQuirks(
        capabilities=(
            CAP_FAN | CAP_FAN_MODE | CAP_HWMON | CAP_PLATFORM_PROFILE |
            CAP_RGB | CAP_RGB_DYNAMIC | CAP_COOLBOOST | CAP_LED_CLASS
        ),
        rgb_zones=4,
        dynamic_effects=5,
        profile_backend=PROFILE_FTBL,
        coolboost_backend=COOLBOOST_EC10,
        fan_count=2,
        has_hwmon=True,
        has_platform_profile=True,
        has_led_class=True,
        board_name="Scala_TLS",
        tested_bios="V1.22",
    ),
}


def _dmi(name: str) -> str:
    try:
        return pathlib.Path(f"/sys/class/dmi/id/{name}").read_text().strip()
    except OSError:
        return ""


def detect_quirks() -> Optional[NitroQuirks]:
    product = _dmi("product_name")
    board = _dmi("board_name")

    if product in MODEL_QUIRKS:
        q = MODEL_QUIRKS[product]
        if q.board_name and q.board_name != board:
            return None
        return q

    return None


def find_platform_profile() -> Optional[pathlib.Path]:
    base = pathlib.Path("/sys/class/platform-profile")
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return None
    for pp in entries:
        if not pp.name.startswith("platform-profile-"):
            continue
        try:
            owner = (pp / "device").resolve()
        except OSError:
            continue
        if owner.name == "nitro":
            return pp / "profile"
    return None


def read_kernel_capabilities() -> Optional[dict]:
    cap_path = pathlib.Path("/sys/devices/platform/nitro/capabilities")
    if not cap_path.is_file():
        return None
    try:
        text = cap_path.read_text().strip()
    except OSError:
        return None
    caps = {}
    for part in text.split():
        if "=" in part:
            k, v = part.split("=", 1)
            caps[k] = v
    return caps


def _quirks_from_kernel(caps: dict) -> NitroQuirks:
    fan = caps.get("fan") == "1"
    fan_mode = caps.get("fan_mode") == "1"
    hwmon = caps.get("hwmon") == "1"
    profile = caps.get("profile")
    rgb = int(caps.get("rgb", 0))
    dyn = int(caps.get("dyn", 0))
    coolboost = caps.get("coolboost")
    led = caps.get("led") == "1"

    if profile not in (None, "ftbl", "opms"):
        return UNKNOWN_QUIRKS
    if coolboost not in (None, "ec10", "wmi"):
        return UNKNOWN_QUIRKS
    if rgb < 0 or dyn < 0:
        return UNKNOWN_QUIRKS

    capabilities = 0
    if fan:
        capabilities |= CAP_FAN
    if fan_mode:
        capabilities |= CAP_FAN_MODE
    if hwmon:
        capabilities |= CAP_HWMON
    if profile:
        capabilities |= CAP_PLATFORM_PROFILE
    if rgb:
        capabilities |= CAP_RGB
    if dyn:
        capabilities |= CAP_RGB_DYNAMIC
    if coolboost:
        capabilities |= CAP_COOLBOOST
    if led:
        capabilities |= CAP_LED_CLASS

    try:
        fan_count = int(caps.get("fan_count", 1 if fan else 0))
    except ValueError:
        return UNKNOWN_QUIRKS
    if not 0 <= fan_count <= 2:
        return UNKNOWN_QUIRKS

    return NitroQuirks(
        capabilities=capabilities,
        rgb_zones=rgb,
        dynamic_effects=dyn,
        profile_backend=(
            PROFILE_NONE if not profile
            else PROFILE_FTBL if profile == "ftbl"
            else PROFILE_OPMS
        ),
        coolboost_backend=(
            COOLBOOST_NONE if not coolboost
            else COOLBOOST_EC10 if coolboost == "ec10"
            else COOLBOOST_WMI
        ),
        fan_count=fan_count,
        has_hwmon=hwmon,
        has_platform_profile=bool(profile),
        has_led_class=led,
        probe=caps.get("probe") == "1",
    )


UNKNOWN_QUIRKS = NitroQuirks(
    capabilities=0,
    rgb_zones=0,
    dynamic_effects=0,
    profile_backend=PROFILE_NONE,
    coolboost_backend=COOLBOOST_NONE,
    fan_count=0,
    has_hwmon=False,
    has_platform_profile=False,
    has_led_class=False,
)


def get_quirks() -> NitroQuirks:
    kernel_caps = read_kernel_capabilities()
    if kernel_caps is not None:
        try:
            return _quirks_from_kernel(kernel_caps)
        except (TypeError, ValueError):
            return UNKNOWN_QUIRKS

    q = detect_quirks()
    if q:
        return q

    return UNKNOWN_QUIRKS


def get_zones(quirks: NitroQuirks) -> tuple:
    if quirks.rgb_zones == 4:
        return (1, 2, 4, 8)
    elif quirks.rgb_zones == 1:
        return (1,)
    return ()


def get_effects(quirks: NitroQuirks) -> tuple:
    if quirks.dynamic_effects >= 5:
        return (1, 2, 3, 4, 5)
    return tuple(range(1, quirks.dynamic_effects + 1))


def get_effect_names(quirks: NitroQuirks) -> dict:
    base = {1: "breath", 2: "neon", 3: "wave", 4: "shifting", 5: "zoom"}
    return {k: v for k, v in base.items() if k <= quirks.dynamic_effects}


QUIRKS = get_quirks()
ZONES = get_zones(QUIRKS)
EFFECTS = get_effects(QUIRKS)
EFFECT_NAMES = get_effect_names(QUIRKS)

NITRO_NZONES = len(ZONES)
ZONE_IDS = ZONES
EFFECT_DEFAULTS = {
    1: (4, (255, 0, 255)),
    2: (3, (0, 255, 255)),
    3: (5, (0, 128, 255)),
    4: (5, (128, 0, 255)),
    5: (7, (0, 255, 0)),
}


def main():
    q = get_quirks()
    print(f"Product: {_dmi('product_name')}")
    print(f"Board: {_dmi('board_name')}")
    print(f"BIOS: {_dmi('bios_version')}")
    print(f"Capabilities: {q.capabilities_string()}")
    print(f"RGB zones: {q.rgb_zones}")
    print(f"Dynamic effects: {q.dynamic_effects}")
    print(f"Profile backend: {q.profile_backend}")
    print(f"CoolBoost backend: {q.coolboost_backend}")
    print(f"Fan count: {q.fan_count}")
    print(f"Has hwmon: {q.has_hwmon}")
    print(f"Has platform_profile: {q.has_platform_profile}")
    print(f"Has led_class: {q.has_led_class}")
    print(f"Zones: {ZONES}")
    print(f"Effects: {EFFECTS}")


if __name__ == "__main__":
    main()
