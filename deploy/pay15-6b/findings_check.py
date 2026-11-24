"""PAY-15 6b gates R2 / S2: the read-only verifier's findings must be EXACTLY the packet's expected set (FU-2).

Run on the host with the system python3 (stdlib only):  python3 -I -B findings_check.py VERIFIER_JSON EXPECTED_FILE
    [--known-orphans "ID ..."] [--orphans-before F --orphans-after F --since EPOCH --until EPOCH]

A finding's key is invariant|class|entity_kind|entity_id. EXPECTED_FILE lists one key per line (multiset; blank lines
and '#' comments ignored). Erratum-1 excluded_bucket_net_credit findings are not listed there: each is allowed only for
a deleted creator that is either a known orphan (packet pin KNOWN_ORPHANS) or, in S, a creator whose whole credit
history was written inside this run's smoke window (FU-1 A). The orphan files are 'streamer_id|first_epoch|last_epoch'
lines from donation_credit_events whose creator no longer exists. Exit 0 = exactly as expected, 1 = not, 2 = unusable.
"""
import argparse
import json
import sys
from collections import Counter

SMOKE_ORPHAN = ("Erratum-1", "excluded_bucket_net_credit", "bucket")


def orphans(path):
    out = {}
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.strip():
            sid, first, last = line.split("|")
            out[sid] = (int(float(first)), int(float(last)))
    return out


def check(findings, expected, known, before=None, after=None, since=None, until=None):
    """-> list of problems (empty = exactly as expected)."""
    problems, allowed = [], set(known)
    if after is not None:
        if set(before) != allowed:
            problems.append(f"orphan creators before the smoke {sorted(before)} != known {sorted(allowed)}")
        for sid, span in sorted(after.items()):
            if sid in before:
                if span != before[sid]:
                    problems.append(f"orphan creator {sid} got new credit history during S")
            elif since <= span[0] and span[1] <= until:
                allowed.add(sid)  # its whole history is from this smoke run
            else:
                problems.append(f"new orphan creator {sid} has history outside the smoke window")
    rest = Counter()
    for f in findings:
        key = (f["invariant"], f["class"], f["entity_kind"], f["entity_id"])
        if key[:3] == SMOKE_ORPHAN and key[3].split(":")[0] in allowed:
            continue
        rest["|".join(key)] += 1
    for k in sorted((rest - expected).elements()):
        problems.append(f"unexpected finding {k}")
    for k in sorted((expected - rest).elements()):
        problems.append(f"expected finding missing {k}")
    return problems


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("verifier_json")
    p.add_argument("expected_file")
    p.add_argument("--known-orphans", default="")
    p.add_argument("--orphans-before")
    p.add_argument("--orphans-after")
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    a = p.parse_args(argv)
    window = (a.orphans_before, a.orphans_after, a.since, a.until)
    if any(v is not None for v in window) and None in window:
        print("STOP: the orphan files and the smoke window go together"); return 2
    try:
        findings = json.load(open(a.verifier_json, encoding="utf-8"))["findings"]
        expected = Counter(l.strip() for l in open(a.expected_file, encoding="utf-8")
                           if l.strip() and not l.lstrip().startswith("#"))
        before = orphans(a.orphans_before) if a.orphans_before else None
        after = orphans(a.orphans_after) if a.orphans_after else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"STOP: unreadable input: {type(exc).__name__}"); return 2
    problems = check(findings, expected, a.known_orphans.split(), before, after, a.since, a.until)
    for line in problems:
        print("   " + line)
    print(f"findings {len(findings)}: " + ("exactly as expected" if not problems else f"{len(problems)} difference(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
