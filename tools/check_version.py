#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
errors = []


def read(p):
    return (root / p).read_text()


def expect(label, value, want):
    if value != want:
        errors.append(f"{label}: {value!r} != {want!r}")


def expect_optional(label, pattern, text, want):
    m = re.search(pattern, text)
    if m:
        expect(label, m.group(1), want)


want = read("VERSION").strip()
if not re.fullmatch(r"\d+\.\d+\.\d+", want):
    errors.append(f"VERSION is not X.Y.Z: {want!r}")

# dkms.conf
m = re.search(r'PACKAGE_VERSION="([^"]+)"', read("dkms.conf"))
expect("dkms.conf PACKAGE_VERSION", m.group(1) if m else None, want)

# packaging/PKGBUILD
m = re.search(r"^pkgver=(.+)$", read("packaging/PKGBUILD"), re.M)
expect("PKGBUILD pkgver", m.group(1).strip() if m else None, want)

# packaging/.SRCINFO
m = re.search(r"^\s*pkgver = (.+)$", read("packaging/.SRCINFO"), re.M)
expect(".SRCINFO pkgver", m.group(1).strip() if m else None, want)

m = re.search(r"#tag=v([0-9.]+)$", read("packaging/.SRCINFO"), re.M)
expect(".SRCINFO source tag", m.group(1).strip() if m else None, want)

# src/nitro.c
nitro_c = read("src/nitro.c")
if "MODULE_VERSION(NITRO_VERSION)" not in nitro_c:
    errors.append("src/nitro.c: missing MODULE_VERSION(NITRO_VERSION)")
if 'pr_info("v%s loading' not in nitro_c:
    errors.append('src/nitro.c: missing pr_info("v%s loading')

# Makefile
makefile = read("Makefile")
if "NITRO_VERSION := $(shell cat $(src)/VERSION" not in makefile:
    errors.append("Makefile: missing NITRO_VERSION from VERSION")
if 'ccflags-y += -DNITRO_VERSION=' not in makefile:
    errors.append("Makefile: missing ccflags-y NITRO_VERSION define")

# daemon/nitrod.py
m = re.search(r'^VERSION = "([^"]+)"', read("daemon/nitrod.py"), re.M)
expect("daemon/nitrod.py VERSION", m.group(1) if m else None, want)

# tui/nitro-linux.py
m = re.search(r'^VERSION = "([^"]+)"', read("tui/nitro-linux.py"), re.M)
expect("tui/nitro-linux.py VERSION", m.group(1) if m else None, want)

# Documentation and issue-template hints
expect_optional(
    "docs/MODELS.md version",
    r"As of v([0-9.]+)",
    read("docs/MODELS.md"),
    want,
)
expect_optional(
    "bug-report.yml placeholder",
    r'placeholder: "(?:e\.g\., )?([0-9]+\.[0-9]+\.[0-9]+)-1"',
    read(".github/ISSUE_TEMPLATE/bug-report.yml"),
    want,
)

if errors:
    print("version check FAILED:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)

print(f"version OK: {want}")
