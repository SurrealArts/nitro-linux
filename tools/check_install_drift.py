#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import difflib
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
files = [
    root / "deploy/install.sh",
    root / "packaging/nitro-linux.install",
]
shared = (
    "invoking_user",
    "ensure_nitro_group",
    "tighten_state",
    "add_user_nitro",
    "run_as_user",
    "auto_key",
)


def extract_functions(text):
    funcs = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{", lines[i])
        if not match:
            i += 1
            continue
        name = match.group(1)
        key = name[1:] if name.startswith("_") and name[1:] in shared else name
        start = i
        depth = lines[i].count("{") - lines[i].count("}")
        i += 1
        while i < len(lines) and depth > 0:
            depth += lines[i].count("{") - lines[i].count("}")
            i += 1
        funcs[key] = "\n".join(lines[start:i])
    return funcs


def normalize(body):
    body = re.sub(r"^\s*echo\b.*$", "", body, flags=re.M)
    for name in shared:
        body = re.sub(rf"\b_{name}\b", name, body)
    body = body.replace("/usr/local/bin/", "/usr/bin/")
    return re.sub(r"\s+", " ", body).strip()


def main():
    funcs = [extract_functions(path.read_text()) for path in files]
    errors = []

    for name in shared:
        bodies = [table.get(name) for table in funcs]
        if any(body is None for body in bodies):
            missing = [str(files[i]) for i, body in enumerate(bodies) if body is None]
            errors.append(f"{name}: missing from {', '.join(missing)}")
            continue
        normalized = [normalize(body) for body in bodies]
        if normalized[0] != normalized[1]:
            diff = "\n".join(
                difflib.unified_diff(
                    normalized[0].splitlines(),
                    normalized[1].splitlines(),
                    fromfile=str(files[0]),
                    tofile=str(files[1]),
                    lineterm="",
                )
            )
            errors.append(f"{name}: logic differs\n{diff}")

    if errors:
        print("install drift check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"  {error}", file=sys.stderr)
        return 1

    print(f"install drift OK: {len(shared)} shared helpers match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
