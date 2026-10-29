#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import pathlib
import re
import shutil
import subprocess
import sys

root = pathlib.Path(__file__).resolve().parent.parent
version_re = re.compile(r"^\d+\.\d+\.\d+$")


def read(path):
    return (root / path).read_text()


def write(path, text):
    (root / path).write_text(text)


def replace(path, pattern, replacement, count=1, flags=0):
    text = read(path)
    new, found = re.subn(pattern, replacement, text, count=count, flags=flags)
    if found != count:
        raise SystemExit(
            f"{path}: expected {count} match(es) for {pattern!r}, found {found}"
        )
    write(path, new)


def replace_optional(path, pattern, replacement, flags=0):
    text = read(path)
    new, found = re.subn(pattern, replacement, text, flags=flags)
    if found:
        write(path, new)
    return found


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version", help="new version, for example 2.3.2")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="allow tracked files to have uncommitted changes",
    )
    args = parser.parse_args()

    version = args.version.lstrip("v")
    if not version_re.fullmatch(version):
        raise SystemExit(f"invalid version {version!r}; expected X.Y.Z")

    if not args.allow_dirty:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if status.returncode != 0:
            raise SystemExit("git status failed; is this a git checkout?")
        if status.stdout.strip():
            raise SystemExit(
                "working tree has tracked changes; commit or stash them, "
                "or pass --allow-dirty"
            )

    tag = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/v{version}"],
        cwd=root,
    )
    if tag.returncode == 0:
        raise SystemExit(f"tag v{version} already exists")

    write("VERSION", version + "\n")
    replace("dkms.conf", r'PACKAGE_VERSION="[^"]+"', f'PACKAGE_VERSION="{version}"')
    replace("packaging/PKGBUILD", r"^pkgver=.*$", f"pkgver={version}", flags=re.M)
    replace("daemon/nitrod.py", r'^VERSION = "[^"]+"', f'VERSION = "{version}"', flags=re.M)
    replace("tui/nitro-linux.py", r'^VERSION = "[^"]+"', f'VERSION = "{version}"', flags=re.M)
    replace("packaging/.SRCINFO", r"^[ \t]*pkgver = .+$", f"\tpkgver = {version}", flags=re.M)
    replace("packaging/.SRCINFO", r"#tag=v[0-9.]+$", f"#tag=v{version}", flags=re.M)

    nitro_c = read("src/nitro.c")
    if "MODULE_VERSION(NITRO_VERSION)" not in nitro_c:
        replace("src/nitro.c", r'MODULE_VERSION\("[^"]+"\)', f'MODULE_VERSION("{version}")')
    if 'pr_info("v%s loading' not in nitro_c:
        replace("src/nitro.c", r'pr_info\("v[0-9.]+ loading', f'pr_info("v{version} loading')
    replace_optional("src/nitro.c", r"driver v[0-9.]+", f"driver v{version}")

    replace_optional(
        ".github/ISSUE_TEMPLATE/bug-report.yml",
        r'(placeholder: "(?:e\.g\., )?)[0-9]+\.[0-9]+\.[0-9]+-1"',
        rf'\g<1>{version}-1"',
    )
    replace_optional("docs/MODELS.md", r"As of v[0-9.]+", f"As of v{version}")

    makepkg = shutil.which("makepkg")
    if makepkg:
        result = subprocess.run(
            [makepkg, "--printsrcinfo"],
            cwd=root / "packaging",
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            write("packaging/.SRCINFO", result.stdout)
        else:
            print(
                "warning: makepkg --printsrcinfo failed; kept regex-updated .SRCINFO",
                file=sys.stderr,
            )

    check = subprocess.run(
        [sys.executable, "tools/check_version.py"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        print(check.stdout, end="")
        print(check.stderr, end="", file=sys.stderr)
        raise SystemExit("version check failed after bump")

    print(check.stdout, end="")
    print("next: review the diff, commit as 'chore(release): " + version + "',")
    print(f"      then create and push the signed tag v{version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
