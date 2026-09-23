"""Minimal CoreBot skill validator (task 10).

``skills/corebot-vps-deploy/quick_validate.py`` referenced by older docs does
not exist in this repo (recorded in task 06) — this module is its replacement
and the ``skill`` gate used by CI (``.github/workflows/ci.yml``) and the
local full-gate command in README.

SKILL.md checklist enforced (stdlib only, no extra deps):
  1. ``SKILL.md`` exists with YAML frontmatter containing ``name`` and
     ``description`` (both non-empty).
  2. Frontmatter ``name`` matches the skill directory name.
  3. Body has at least one Markdown heading and is non-trivial (>= 10 lines).
  4. Every ``scripts/*.sh`` referenced from ``SKILL.md`` exists either in
     the skill dir or in the app-checkout ``scripts/`` (skill commands run
     from the app checkout, e.g. ``bash scripts/update_corebot.sh``).
  5. No secret-looking material inside the skill: real bot tokens
     (``NNNN:AAA...``), 64-hex keys, private-key blocks, or ``vault_password``
     values. Synthetic placeholders (``<...>``, ``example``, ``SYNTHETIC``,
     ``change-me``) are allowed.

Usage:
    python -m tools.validate_skill            # validates skills/ (exit 0/1)
    python -m tools.validate_skill --skills-dir skills

Exit codes: 0 = all skills valid, 1 = any violation, 2 = usage error.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
FRONTMATTER_KV_RE = re.compile(r"^(name|description)\s*:\s*(.+?)\s*$", re.MULTILINE)
SCRIPT_REF_RE = re.compile(r"scripts/[A-Za-z0-9_.-]+\.sh")
REAL_TOKEN_RE = re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b")
HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:RSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
# `vault_password: hunter2`-style real values; placeholders stay allowed.
VAULT_SECRET_RE = re.compile(
    r"(?im)^\s*(?:vault_password|vault_pass|admin_password|bot_token)\s*:\s*"
    r"(?![\"']?(?:<|example|synthetic|change-me|TODO))(\S+)"
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def validate_skill(skill_dir: Path) -> list[str]:
    """Return a list of violation strings (empty = valid)."""
    errors: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return [f"{skill_dir.name}: missing SKILL.md"]

    try:
        text = _read_text(skill_md)
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{skill_dir.name}: cannot read SKILL.md: {exc}"]

    fm = FRONTMATTER_RE.match(text)
    if not fm:
        errors.append(f"{skill_dir.name}: SKILL.md has no YAML frontmatter (--- block)")
        return errors
    kv = dict(FRONTMATTER_KV_RE.findall(fm.group(1)))
    name = kv.get("name", "").strip().strip("\"'")
    description = kv.get("description", "").strip().strip("\"'")
    if not name:
        errors.append(f"{skill_dir.name}: frontmatter missing non-empty 'name'")
    elif name != skill_dir.name:
        errors.append(f"{skill_dir.name}: frontmatter name {name!r} != directory name")
    if not description:
        errors.append(f"{skill_dir.name}: frontmatter missing non-empty 'description'")

    body = text[fm.end() :]
    if len(body.splitlines()) < 10 or not re.search(r"(?m)^#{1,3}\s+\S", body):
        errors.append(f"{skill_dir.name}: SKILL.md body too short or has no headings")

    for ref in sorted(set(SCRIPT_REF_RE.findall(body))):
        if (skill_dir / ref).is_file():
            continue
        # Skill commands run from the app checkout: scripts/*.sh may live
        # in the repo-root scripts/ (e.g. update_corebot.sh,
        # release_status.sh) rather than inside the skill.
        app_root = skill_dir.parent.parent
        if (app_root / ref).is_file():
            continue
        errors.append(f"{skill_dir.name}: referenced {ref} does not exist")

    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.suffix in {".pyc"}:
            continue
        try:
            content = _read_text(path)
        except (OSError, UnicodeDecodeError):
            continue  # binary artefacts are not scanned
        rel = path.relative_to(skill_dir).as_posix()
        if REAL_TOKEN_RE.search(content):
            errors.append(f"{skill_dir.name}: {rel} looks like a real bot token")
        if HEX64_RE.search(content):
            errors.append(f"{skill_dir.name}: {rel} contains a 64-hex secret")
        if PRIVATE_KEY_RE.search(content):
            errors.append(f"{skill_dir.name}: {rel} contains a private key block")
        if VAULT_SECRET_RE.search(content):
            errors.append(f"{skill_dir.name}: {rel} contains a real secret value")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate CoreBot skills (SKILL.md checklist)."
    )
    parser.add_argument(
        "--skills-dir",
        default=str(REPO_ROOT / "skills"),
        help="Directory containing skill subdirectories (default: <repo>/skills).",
    )
    args = parser.parse_args(argv)

    skills_dir = Path(args.skills_dir)
    if not skills_dir.is_dir():
        print(f"ERROR: skills dir not found: {skills_dir}", file=sys.stderr)
        return 2

    failures: list[str] = []
    checked = 0
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        checked += 1
        failures.extend(validate_skill(entry))

    if checked == 0:
        print("ERROR: no skills found", file=sys.stderr)
        return 2
    if failures:
        print(f"SKILL VALIDATION FAILED ({checked} skill(s) checked):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"SKILL OK: {checked} skill(s) valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
