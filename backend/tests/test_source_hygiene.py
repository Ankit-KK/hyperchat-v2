"""Security audit F8 (Trojan Source, CVE-2021-42574): no literal Unicode bidirectional-control characters in the
repository's source and configuration files. Such characters can make code display differently from how it runs;
write them as \\u escapes instead. Emoji joiners / variation selectors and combining marks in text stay allowed."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
BIDI = {"\u061c", "\u200e", "\u200f", *map(chr, range(0x202A, 0x202F)), *map(chr, range(0x2066, 0x206A))}
SUFFIXES = {".py", ".sh", ".sql", ".conf", ".js", ".html", ".css", ".yml", ".yaml", ".toml", ".ini", ".cfg",
            ".service", ".timer", ".example", ".md", ".txt", ".json"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
# Verbatim scanner output kept as audit evidence (it quotes the offending lines as they were): never edited.
EVIDENCE = ROOT / "docs" / "security-evidence"


def source_files():
    for path in ROOT.rglob("*"):
        if (path.is_file() and path.suffix in SUFFIXES and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts)
                and EVIDENCE not in path.parents):
            yield path


def test_no_literal_bidirectional_control_characters():
    found = []
    for path in source_files():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for ch in sorted(set(line) & BIDI):
                found.append(f"{path.relative_to(ROOT).as_posix()}:{number}: U+{ord(ch):04X}")
    assert not found, "write these as \\u escapes:\n" + "\n".join(found)


def test_the_check_sees_the_whole_repository_and_catches_a_literal_character(tmp_path):
    names = {p.relative_to(ROOT).as_posix() for p in source_files()}
    assert {"backend/app/audio/text.py", "deploy/nginx/api.hyperchat.space.conf", ".github/workflows/ci.yml",
            "database/migrations/versions/0001_foundation.py"} <= names
    sample = tmp_path / "x.py"
    sample.write_text('s = "abc\u202edef"\n', encoding="utf-8")
    assert set(sample.read_text(encoding="utf-8")) & BIDI == {"\u202e"}
