#!/usr/bin/env python3
"""Summarise Gradle JUnit XML results so the CI log states test counts.

Spring Framework configures no Gradle testLogging, so a green `check` prints no
test counts. This script reads every */build/test-results/**/TEST-*.xml, prints
a per-module TEST SUMMARY plus a TOTAL, every failing test with its message, and
every skipped test grouped by (class, reason). Gradle writes an empty <skipped/>
for @Disabled / assumption-based skips, so the reason is looked up in the test
source (method annotations, assume* calls, class annotations, superclasses).
"""
import glob
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import OrderedDict, defaultdict

LABEL = sys.argv[1] if len(sys.argv) > 1 else ""
SRC_DIRS = ("src/test/java", "src/test/kotlin", "src/testFixtures/java", "src/testFixtures/kotlin")
COND_ANN = re.compile(
    r"@(Disabled\w*|Enabled\w*|Ignore\w*|EnabledForTestGroups|DisabledIf\w*|EnabledIf\w*|"
    r"DisabledOn\w*|EnabledOn\w*|Assumptions?\w*|IfProfileValue|ProfileValueSourceConfiguration)\b[^\n]*"
)
ASSUME = re.compile(r"\b(assume\w*|Assume\.\w+|Assumptions\.\w+|assumingThat)\s*\(")
_file_cache = {}


def module_of(path):
    return path.split(os.sep, 1)[0]


def read(path):
    if path not in _file_cache:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                _file_cache[path] = f.read()
        except OSError:
            _file_cache[path] = None
    return _file_cache[path]


def find_source(fqcn, module=None):
    outer = fqcn.split("$", 1)[0]
    rel = outer.replace(".", "/")
    modules = [module] if module else []
    modules += [d for d in sorted(os.listdir(".")) if os.path.isdir(d) and d != module]
    for mod in modules:
        for sd in SRC_DIRS:
            for ext in (".java", ".kt"):
                p = os.path.join(mod, sd, rel + ext)
                if os.path.isfile(p):
                    return p
    return None


def call_text(text, start):
    """Return the full text of a call starting at `start` (balanced parens)."""
    depth = 0
    i = text.index("(", start)
    j = i
    while j < len(text):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return " ".join(text[start:j + 1].split())


def assumes_in(text):
    out = []
    for m in ASSUME.finditer(text):
        out.append(call_text(text, m.start()))
    return out


def clean_name(name):
    n = name.strip()
    n = re.sub(r"\[[^\]]*\]$", "", n).strip()
    n = re.sub(r"\([^)]*\)$", "", n).strip()
    return n.strip("`").strip()


def method_block(text, name):
    if not name:
        return None, None
    pat = re.compile(r"(?:void|fun)\s+`?" + re.escape(name) + r"`?\s*\(")
    m = pat.search(text)
    if not m:
        return None, None
    # annotations: the lines directly above the declaration
    before = text[:m.start()].rstrip().split("\n")
    anns = []
    for line in reversed(before[-12:]):
        s = line.strip()
        if not s or s.startswith("@") or s.startswith("*") or s.startswith("/") or s.startswith(")") or s.startswith('"'):
            if s.startswith("@"):
                anns.append(s)
            continue
        if s.endswith("}") or s.endswith(";") or s.endswith("{"):
            break
    # body
    brace = text.find("{", m.end())
    depth, j = 0, brace
    while 0 <= j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = text[brace:j] if brace >= 0 else ""
    return anns, body


def class_annotations(text, simple):
    decl = re.search(r"^[^\n*/]*\b(class|interface)\s+" + re.escape(simple) + r"\b", text, re.M)
    if not decl:
        return []
    before = text[:decl.start()].split("\n")
    anns = []
    for line in reversed(before[-15:]):
        s = line.strip()
        if s.startswith("@"):
            anns.append(s)
        elif not s or s.startswith("*") or s.startswith("/") or s.startswith(")"):
            continue
        else:
            break
    return [a for a in anns if COND_ANN.search(a)]


def superclass(text, simple, fqcn):
    m = re.search(r"\b(class)\s+" + re.escape(simple) + r"\b[^{]*?\bextends\s+([\w.]+)", text, re.S)
    if not m:
        m = re.search(r"\bclass\s+" + re.escape(simple) + r"\b[^{]*?:\s*([\w.]+)\s*\(", text, re.S)
        if not m:
            return None
        sup = m.group(1)
    else:
        sup = m.group(2)
    if "." in sup:
        return sup
    imp = re.search(r"^import\s+([\w.]+\." + re.escape(sup) + r")\s*;?\s*$", text, re.M)
    if imp:
        return imp.group(1)
    pkg = fqcn.rsplit(".", 1)[0] if "." in fqcn else ""
    return pkg + "." + sup if pkg else sup


def reason_for(fqcn, name, module, depth=0):
    src = find_source(fqcn, module)
    if not src or depth > 4:
        return None
    text = read(src)
    if text is None:
        return None
    simple = fqcn.split("$")[-1].split(".")[-1]
    mname = clean_name(name)
    anns, body = method_block(text, mname)
    if anns:
        cond = [a for a in anns if COND_ANN.search(a)]
        if cond:
            return "; ".join(cond)
    if body:
        a = assumes_in(body)
        if a:
            return "; ".join(a)
    cls = class_annotations(text, simple)
    if cls:
        return "; ".join(cls)
    # assumptions in @Before*/@BeforeEach/@BeforeAll/setUp or anywhere in the file
    a = assumes_in(text)
    if a:
        return "class-level assumption(s): " + "; ".join(sorted(set(a))[:4])
    sup = superclass(text, simple, fqcn)
    if sup:
        r = reason_for(sup, name, None, depth + 1)
        if r:
            return r + " (inherited from " + sup.split(".")[-1] + ")"
    return None


def main():
    files = sorted(glob.glob("*/build/test-results/**/TEST-*.xml", recursive=True))
    files += sorted(glob.glob("buildSrc/build/test-results/**/TEST-*.xml", recursive=True))
    files = list(OrderedDict.fromkeys(files))
    per_mod = OrderedDict()
    failures = []
    skips = defaultdict(list)
    for f in files:
        mod = module_of(f)
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError as e:
            print("WARN: cannot parse %s: %s" % (f, e))
            continue
        stats = per_mod.setdefault(mod, [0, 0, 0, 0])
        for tc in root.iter("testcase"):
            stats[0] += 1
            cls = tc.get("classname") or root.get("name") or "?"
            name = tc.get("name") or "?"
            fail = tc.find("failure")
            err = tc.find("error")
            skip = tc.find("skipped")
            if fail is not None or err is not None:
                stats[2] += 1
                el = fail if fail is not None else err
                failures.append((mod, cls, name, (el.get("message") or "")[:300], (el.text or "")[:3000]))
            elif skip is not None:
                stats[3] += 1
                msg = (skip.get("message") or "").strip()
                reason = msg or reason_for(cls, name, mod) or (
                    "skipped by a JUnit assumption/condition evaluated at runtime (no static source marker found)")
                skips[(cls, reason)].append(name)
            else:
                stats[1] += 1
    print("=" * 78)
    print("SEAL TEST REPORT %s" % LABEL)
    if not files:
        print("No JUnit XML test results found.")
    tot = [0, 0, 0, 0]
    for mod, s in per_mod.items():
        for i in range(4):
            tot[i] += s[i]
    if failures:
        print("-" * 78)
        for mod, cls, name, msg, trace in failures:
            print("FAILED TEST [%s] %s.%s -- %s" % (mod, cls, name, msg))
            print(trace)
    if skips:
        print("-" * 78)
        print("SKIPPED TESTS (grouped by class and reason):")
        for (cls, reason), names in skips.items():
            short = ", ".join(sorted(set(names))[:6]) + (" ..." if len(set(names)) > 6 else "")
            print("SKIPPED %d x %s [%s] -- reason: %s" % (len(names), cls, short, reason))
    print("-" * 78)
    for mod, s in per_mod.items():
        print("TEST SUMMARY [%s]: %d tests, %d passed, %d failed, %d skipped" % (mod, s[0], s[1], s[2], s[3]))
    print("TEST SUMMARY TOTAL %s: %d tests, %d passed, %d failed, %d skipped" % (LABEL, tot[0], tot[1], tot[2], tot[3]))
    print("=" * 78)


if __name__ == "__main__":
    main()
