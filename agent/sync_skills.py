"""Expose the host's Claude Code, pi and opencode skills to Open Terminal.

Open Terminal only looks one level deep in ~/{.agents,.cptr,.claude,.codex}/skills, while plugin,
synced and pi package skills live deeper. This links every found skill directory into
~/.cptr/skills/<name> (the sources are read-only mounts under /mnt/skills, see compose.yaml).
Runs at container start; earlier links are replaced, real directories there are left alone.
Skills the agent writes itself (/skills:create) go to ~/.agents/skills, which Open Terminal reads first.
"""
import json
import os
import re
import sys
from pathlib import Path

SRC = Path(os.getenv("SKILLS_SRC", "/mnt/skills"))
DEST = Path(os.getenv("SKILLS_DEST", Path.home() / ".cptr" / "skills"))
HOST_HOME = os.getenv("HOST_HOME", "")


def frontmatter(path):
    """name and description from SKILL.md; empty when missing (Open Terminal then skips the skill)."""
    text = path.read_text(errors="replace")
    if not text.startswith("---\n") or (end := text.find("\n---", 4)) < 0:
        return {}
    values, lines, i = {}, text[4:end].splitlines(), 0
    while i < len(lines):
        key, sep, value = lines[i].partition(":")
        i += 1
        if not sep or lines[i - 1][:1] in (" ", "\t", "#"):
            continue
        value = value.strip()
        if value in (">", "|", ">-", "|-"):
            block = []
            while i < len(lines) and lines[i][:1] in (" ", "\t"):
                block.append(lines[i].strip())
                i += 1
            value = " ".join(block)
        values[key.strip()] = value.strip("\"'")
    return values


def skill_dirs(root, depth=1):
    """Directories <root>/*/SKILL.md (or <root>/*/*/SKILL.md for depth 2), sorted for stable priority."""
    pattern = "/".join(["*"] * depth) + "/SKILL.md"
    return sorted(p.parent for p in root.glob(pattern)) if root.is_dir() else []


def claude_plugins():
    """Skills of the installed Claude Code plugins only (the cache also keeps old versions)."""
    try:
        installed = json.loads((SRC / "claude-plugins.json").read_text())["plugins"]
    except (OSError, ValueError, KeyError):
        return []
    prefix = f"{HOST_HOME}/.claude/plugins/cache/"
    found = []
    for name, entries in sorted(installed.items()):
        for entry in entries:
            path = entry.get("installPath", "")
            if path.startswith(prefix):
                found += [("claude-plugin:" + name.split("@")[0], p)
                          for p in skill_dirs(SRC / "claude-plugins" / path[len(prefix):] / "skills")]
    return found


def pi_packages():
    """Skills shipped by the pi packages listed in ~/.pi/agent/settings.json (npm:<name>)."""
    try:
        packages = json.loads((SRC / "pi-settings.json").read_text()).get("packages", [])
    except (OSError, ValueError):
        return []
    found = []
    for package in packages:
        source = package.get("source", "") if isinstance(package, dict) else package
        if source.startswith("npm:"):
            name = source[4:]
            found += [("pi-package:" + name, p) for p in skill_dirs(SRC / "pi-packages" / name / "skills")]
    return found


def sources():
    """(origin, skill dir) in priority order: the first skill with a given name wins."""
    return ([("claude", p) for p in skill_dirs(SRC / "claude")]
            + [("claude-synced", p) for p in skill_dirs(SRC / "claude" / "synced", depth=2)]
            + claude_plugins()
            + [("agents", p) for p in skill_dirs(SRC / "agents")]
            + [("opencode", p) for p in skill_dirs(SRC / "opencode")]
            + [("pi", p) for p in skill_dirs(SRC / "pi")]
            + [("pi", p) for p in skill_dirs(SRC / "pi-agent")]
            + pi_packages())


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    for entry in DEST.iterdir():
        if entry.is_symlink():
            entry.unlink()
    linked, skipped = {}, []
    for origin, directory in sources():
        meta = frontmatter(directory / "SKILL.md")
        name = (meta.get("name") or directory.name).strip()
        if not meta.get("description"):
            skipped.append(f"{origin}/{directory.name} (no description)")
            continue
        if name in linked:
            continue  # a higher-priority source already has it
        target = DEST / re.sub(r"[^A-Za-z0-9._-]+", "-", name)
        if target.exists():
            skipped.append(f"{origin}/{name} (real directory {target} exists)")
            continue
        target.symlink_to(directory, target_is_directory=True)
        linked[name] = origin
    for name, origin in sorted(linked.items()):
        print(f"skill {name} <- {origin}")
    for line in skipped:
        print(f"skill skipped: {line}")
    print(f"{len(linked)} skills linked into {DEST}", file=sys.stderr)


if __name__ == "__main__":
    main()
