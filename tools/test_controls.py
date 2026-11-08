#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the persistent control policy (tools/controls.py)."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NITRO = ROOT / "tools" / "nitro.py"

DEFAULT_POLICY = """\
schema=1
probe=off
profile=on
fan=on
hwmon=on
rgb=on
coolboost=on
kbd_timeout=on
"""


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)
        self.conf = self.dir / "etc" / "nitro" / "controls.conf"
        self.modprobe = self.dir / "modprobe.d"
        self.modprobe.mkdir()
        self.params = self.dir / "module" / "nitro" / "parameters"
        self.caps = self.dir / "capabilities"
        self.caps.write_text(
            "probe=0 fan=1 fan_count=2 fan_mode=1 hwmon=1 rgb=4 dyn=5 "
            "led=1 kbd_timeout=1\n"
        )
        self.env = dict(os.environ)
        self.env.update(
            NITRO_CONTROLS_CONF=str(self.conf),
            NITRO_MODPROBE_DIR=str(self.modprobe),
            NITRO_MODULE_PARAMS=str(self.params),
            NITRO_CAP_FILE=str(self.caps),
        )

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(NITRO), "controls", *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def write_conf(self, text):
        self.conf.parent.mkdir(parents=True, exist_ok=True)
        self.conf.write_text(text)

    def read_conf(self):
        return self.conf.read_text()

    def options(self):
        path = self.modprobe / "nitro-controls.conf"
        return path.read_text() if path.exists() else ""

    def write_params(self, **overrides):
        values = {
            "probe": False,
            "disable_profile": False,
            "disable_fan": False,
            "disable_hwmon": False,
            "disable_rgb": False,
            "disable_coolboost": False,
            "disable_kbd_timeout": False,
        }
        values.update(overrides)
        self.params.mkdir(parents=True, exist_ok=True)
        for name, value in values.items():
            (self.params / name).write_text("Y\n" if value else "N\n")

    # defaults -----------------------------------------------------------

    def test_show_succeeds_without_policy(self):
        result = self.run_cli("show")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile", result.stdout)
        self.assertIn("on", result.stdout)

    def test_install_creates_default_policy(self):
        result = self.run_cli("install", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.read_conf()
        self.assertIn("profile=on", text)
        self.assertIn("probe=off", text)
        self.assertEqual(self.options(), "")

    # invalid policy -----------------------------------------------------

    def test_unknown_key_fails(self):
        self.write_conf("bogus=on\n")
        result = self.run_cli("show")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bogus", result.stderr)

    def test_duplicate_key_fails(self):
        self.write_conf("profile=on\nprofile=off\n")
        result = self.run_cli("sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate", result.stderr)

    def test_bad_value_fails(self):
        self.write_conf("profile=maybe\n")
        result = self.run_cli("sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("on or off", result.stderr)

    def test_future_schema_fails(self):
        self.write_conf("schema=2\nprofile=off\n")
        result = self.run_cli("sync")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("schema", result.stderr)

    # set ----------------------------------------------------------------

    def test_set_preserves_comments(self):
        self.write_conf("# my note\nschema=1\nprofile=on\n# tail\n")
        result = self.run_cli("set", "profile", "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.read_conf()
        self.assertIn("# my note", text)
        self.assertIn("# tail", text)
        self.assertIn("profile=off", text)

    def test_set_creates_policy_when_missing(self):
        result = self.run_cli("set", "profile", "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.read_conf()
        self.assertIn("profile=off", text)
        self.assertIn("fan=on", text)
        self.assertIn("disable_profile=1", self.options())

    def test_set_fan_off_disables_coolboost(self):
        self.write_conf(DEFAULT_POLICY)
        result = self.run_cli("set", "fan", "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.read_conf()
        self.assertIn("fan=off", text)
        self.assertIn("coolboost=off", text)
        self.assertIn("disable_fan=1", self.options())
        self.assertIn("disable_coolboost=1", self.options())

    def test_explicit_contradiction_fails(self):
        self.write_conf("schema=1\nfan=off\ncoolboost=on\n")
        result = self.run_cli("set", "profile", "off")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("coolboost", result.stderr)

    # install overrides --------------------------------------------------

    def test_install_without_persists(self):
        result = self.run_cli("install", "--without=profile", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile=off", self.read_conf())
        self.assertIn("disable_profile=1", self.options())

    def test_install_without_fan_implies_coolboost(self):
        self.write_conf(DEFAULT_POLICY)
        result = self.run_cli("install", "--without=fan", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.read_conf()
        self.assertIn("fan=off", text)
        self.assertIn("coolboost=off", text)
        self.assertIn("disable_fan=1", self.options())
        self.assertIn("disable_coolboost=1", self.options())

    def test_no_save_leaves_policy(self):
        self.write_conf(DEFAULT_POLICY)
        result = self.run_cli(
            "install", "--without=profile", "--no-save", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("profile=on", self.read_conf())
        self.assertIn("disable_profile=1", self.options())
        # The next normal install re-syncs from the untouched policy.
        result = self.run_cli("install", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.options(), "")

    def test_dry_run_writes_nothing(self):
        result = self.run_cli("install", "--without=profile", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.conf.exists())
        self.assertEqual(self.options(), "")
        self.assertIn("profile", result.stdout)

    def test_overlapping_flags_fail(self):
        result = self.run_cli(
            "install", "--with=profile", "--without=profile", "--dry-run")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("profile", result.stderr)

    def test_probe_persists(self):
        result = self.run_cli("install", "--with=probe", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("probe=on", self.read_conf())
        self.assertIn("probe=1", self.options())

    # migration and foreign files ---------------------------------------

    def test_legacy_probe_migrates(self):
        legacy = self.modprobe / "nitro-linux.conf"
        legacy.write_text(
            "# nitro-linux: read-only probe mode, added by "
            "deploy/install.sh --probe\noptions nitro probe=1\n"
        )
        result = self.run_cli("install", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("probe=on", self.read_conf())
        self.assertFalse(legacy.exists())
        self.assertIn("probe=1", self.options())

    def test_foreign_options_warn(self):
        foreign = self.modprobe / "nitro-no-profile.conf"
        foreign.write_text("options nitro disable_profile=1\n")
        result = self.run_cli("install", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nitro-no-profile.conf", result.stderr)
        self.assertTrue(foreign.exists())

    def test_sync_removes_stale_options(self):
        self.write_conf(DEFAULT_POLICY)
        stale = self.modprobe / "nitro-controls.conf"
        stale.write_text("options nitro disable_profile=1\n")
        result = self.run_cli("sync", "--quiet")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(stale.exists())

    # check --------------------------------------------------------------

    def test_check_in_sync(self):
        self.write_conf("schema=1\nprofile=off\n")
        self.assertEqual(
            self.run_cli("install", "--quiet").returncode, 0)
        self.write_params(disable_profile=True)
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_detects_pending_reload(self):
        self.write_conf("schema=1\nprofile=off\n")
        self.assertEqual(
            self.run_cli("install", "--quiet").returncode, 0)
        self.write_params(disable_profile=False)
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("disable_profile", result.stdout)

    def test_check_detects_stale_options_file(self):
        self.write_conf(DEFAULT_POLICY)
        self.write_params()
        (self.modprobe / "nitro-controls.conf").write_text(
            "options nitro disable_profile=1\n")
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nitro-controls.conf", result.stdout)

    def test_check_detects_empty_stale_options_file(self):
        self.write_conf(DEFAULT_POLICY)
        self.write_params()
        (self.modprobe / "nitro-controls.conf").write_text("# stale\n")
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nitro-controls.conf", result.stdout)

    def test_check_unknown_without_module(self):
        self.write_conf(DEFAULT_POLICY)
        result = self.run_cli("check")
        self.assertEqual(result.returncode, 2)

    def test_show_reports_module_absent(self):
        result = self.run_cli("show")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not loaded", result.stdout)


if __name__ == "__main__":
    unittest.main()
