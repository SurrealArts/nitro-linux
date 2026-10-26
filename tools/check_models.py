#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
src = (root / "src/nitro.c").read_text()
docs = (root / "docs/MODELS.md").read_text()
errors = []


def block(name):
    m = re.search(
        rf"static const struct dmi_system_id {name}\[\] __initconst = \{{(.*?)\n\}};",
        src, re.S)
    return m.group(1) if m else ""


def products(name):
    return re.findall(r'DMI_PRODUCT_NAME,\s*"([^"]+)"', block(name))


verified_kernel = products("nitro_dmi")
probe_kernel = products("nitro_probe_dmi")

doc_verified = re.findall(
    r"^\|\s*([^|]+?)\s*\|.*\|\s*\*\*Verified\*\*\s*\|", docs, re.M)
doc_candidates = re.findall(
    r"^\|\s*([^|]+?)\s*\|.*\|\s*probe shipped", docs, re.M | re.I)

doc_verified = [x.strip() for x in doc_verified]
doc_candidates = [x.strip() for x in doc_candidates]

def docs_matches_kernel(doc_set, kernel_list):
    kernel_set = set(kernel_list)
    if doc_set == kernel_set:
        return True, set()
    unmatched_docs = set()
    for d in doc_set:
        if not any(d == k or d in k or k in d for k in kernel_set):
            unmatched_docs.add(d)
    unmatched_kernel = set()
    for k in kernel_set:
        if not any(d == k or d in k or k in d for d in doc_set):
            unmatched_kernel.add(k)
    if unmatched_docs or unmatched_kernel:
        return False, (unmatched_docs, unmatched_kernel)
    return True, set()


ok_v, diff_v = docs_matches_kernel(set(doc_verified), verified_kernel)
if not ok_v:
    errors.append(
        f"Verified mismatch:\n  kernel={verified_kernel}\n  docs  ={doc_verified}\n  unmatched={diff_v}")

ok_p, diff_p = docs_matches_kernel(set(doc_candidates), probe_kernel)
if not ok_p:
    errors.append(
        f"Probe/Candidates mismatch:\n  kernel={probe_kernel}\n  docs  ={doc_candidates}\n  unmatched={diff_p}")

overlap = set(verified_kernel) & set(probe_kernel)
if overlap:
    errors.append(f"Model in both tables: {overlap}")

if errors:
    print("model check FAILED:")
    for e in errors:
        print(f"  {e}")
    sys.exit(1)

print(f"models OK: verified={verified_kernel}, probe={probe_kernel}")
