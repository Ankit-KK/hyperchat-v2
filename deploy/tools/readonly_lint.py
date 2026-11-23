#!/usr/bin/env python3
"""Refuse writing constructs in shell scripts meant to be read-only (OPS-2 safeguard S3; incident 2026-10-28).

    python deploy/tools/readonly_lint.py SCRIPT [SCRIPT ...]

Prints `file:line: <construct>: <line>` for every finding and exits 1 if any; exit 0 when clean; exit 2 when a
file cannot be read (never a pass). A line ending in `# ro-lint: allow` is a reviewed exception: its findings are
printed as `allowed` and do not fail the run.

What it refuses, outside quoted strings (command substitutions `$(...)` inside double quotes count as code):
- `>` / `>>` / `>|` redirects, except fd duplication (`2>&1`, `>&2`) and `/dev/null` (`>/dev/null`, `2>/dev/null`);
- `tee`, `sed -i`, `mv`, `rm`, `cp`, `install` (any destination: they are usually variables), `chmod`, `chown`,
  `chgrp`, `truncate`, `dd of=`, `nginx -s`;
- `systemctl` state changes (start, stop, restart, reload, enable, disable, daemon-reload, ...);
- package managers: `apt`, `apt-get`, `aptitude`, `dpkg`, `snap`, `pip install`.

Limits: static text only. It cannot see writes inside strings run later (`bash -c "..."`, `eval`, `ssh host "..."`);
it fails closed on some read-only text: heredoc bodies are scanned as code, and `${x:->y}`, `$(( a > b ))` or
`[[ a > b ]]` count as redirects (rewrite them, or mark the line `# ro-lint: allow`). Words with a `/` are matched by
basename (`/bin/rm` is `rm`, so is `ls /opt/x/rm`). It is a review aid, not a sandbox (that is S2).
"""

import re
import sys

ALLOW = re.compile(r"#\s*ro-lint:\s*allow\s*$")
WORD_START = set(" \t\n;&|()")  # a `#` after one of these starts a comment
SEPARATORS = re.compile(r"[;&|(){}`\n]")
REDIRECT = re.compile(r"(?:\d+|&)?>>?\|?")
ALWAYS = {"tee", "mv", "rm", "cp", "install", "chmod", "chown", "chgrp", "truncate",
          "apt", "apt-get", "aptitude", "dpkg", "snap"}
SYSTEMCTL_VERBS = {"start", "stop", "restart", "reload", "enable", "disable", "daemon-reload", "try-restart",
                   "reload-or-restart", "try-reload-or-restart", "mask", "unmask", "kill", "isolate", "edit",
                   "set-property", "link", "preset", "revert"}


def code_text(src: str) -> str:
    """The script with quoted text and comments blanked (newlines kept, so line numbers stay aligned)."""
    out = []
    stack = ["code"]  # contexts: code (paren depth tracked per frame), dq, sq, ansi ($'...'), bt (backticks)
    depth = [0]
    i, n = 0, len(src)
    while i < n:
        c, top = src[i], stack[-1]
        nxt = src[i + 1] if i + 1 < n else ""
        if top == "sq":
            if c == "'":
                stack.pop(); depth.pop(); out.append(c)
            else:
                out.append("\n" if c == "\n" else " ")
        elif top in ("dq", "ansi"):
            end = '"' if top == "dq" else "'"
            if c == "\\" and nxt:
                out.append("  " if nxt != "\n" else " \n"); i += 2; continue
            if c == end:
                stack.pop(); depth.pop(); out.append(c)
            elif top == "dq" and c == "$" and nxt == "(":
                stack.append("code"); depth.append(0); out.append("$("); i += 2; continue
            elif top == "dq" and c == "`":
                stack.append("bt"); depth.append(0); out.append(c)
            else:
                out.append("\n" if c == "\n" else " ")
        else:  # code or bt
            if c == "\\" and nxt:
                out.append(" " + ("\n" if nxt == "\n" else " ")); i += 2; continue
            if c == "#" and (i == 0 or src[i - 1] in WORD_START):
                while i < n and src[i] != "\n":
                    out.append(" "); i += 1
                continue
            out.append(c)
            if c == "'" and i > 0 and src[i - 1] == "$":
                stack.append("ansi"); depth.append(0)
            elif c == "'":
                stack.append("sq"); depth.append(0)
            elif c == '"':
                stack.append("dq"); depth.append(0)
            elif c == "`":
                if top == "bt":
                    stack.pop(); depth.pop()
                else:
                    stack.append("bt"); depth.append(0)
            elif c == "(":
                depth[-1] += 1
            elif c == ")":
                if depth[-1] > 0:
                    depth[-1] -= 1
                elif len(stack) > 1:  # closes a $( ) opened inside double quotes
                    stack.pop(); depth.pop()
        i += 1
    return "".join(out)


def redirects(line: str):
    for m in REDIRECT.finditer(line):
        op, rest = m.group(0), line[m.end():]
        if rest.startswith("&") and re.match(r"&(\d+|-)", rest):  # fd duplication: 2>&1, >&2, >&-
            continue
        if rest.startswith("(") and not op.endswith(">>"):  # >(cmd) process substitution; its body is code
            continue
        if re.match(r"\s*/dev/null(?![\w/.-])", rest):
            continue
        yield f"redirect `{op}`"


def commands(line: str):
    for segment in SEPARATORS.split(line):
        words = [w.rsplit("/", 1)[-1] if "/" in w and not w.endswith("/") else w for w in segment.split()]
        found = set(words)
        for w in sorted(found & ALWAYS):
            yield w
        if "sed" in found and any(re.match(r"-[A-Za-z]*i|--in-place", w) for w in words):
            yield "sed -i"
        if "systemctl" in found and found & SYSTEMCTL_VERBS:
            yield "systemctl " + "/".join(sorted(found & SYSTEMCTL_VERBS))
        if "nginx" in found and "-s" in found:
            yield "nginx -s"
        if "dd" in found and any(w.startswith("of=") for w in segment.split()):  # raw words: of=/dev/x has a /
            yield "dd of="
        if found & {"pip", "pip3"} and found & {"install", "uninstall"}:
            yield "pip install"


def lint(path: str):
    """(line number, construct, allowed, raw line) for every finding in one script."""
    with open(path, encoding="utf-8") as f:
        src = f.read()
    raw = src.split("\n")
    for number, line in enumerate(code_text(src).split("\n"), 1):
        allowed = bool(ALLOW.search(raw[number - 1]))
        for construct in [*redirects(line), *commands(line)]:
            yield number, construct, allowed, raw[number - 1].strip()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: readonly_lint.py SCRIPT [SCRIPT ...]", file=sys.stderr)
        return 2
    failed = unreadable = False
    for path in argv:
        try:
            findings = list(lint(path))
        except (OSError, UnicodeDecodeError) as exc:
            print(f"{path}: cannot read: {exc}", file=sys.stderr)
            unreadable = True
            continue
        for number, construct, allowed, text in findings:
            print(f"{path}:{number}: {'allowed: ' if allowed else ''}{construct}: {text}")
            failed |= not allowed
        if not findings:
            print(f"{path}: ok")
    return 2 if unreadable else 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
