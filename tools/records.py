#!/usr/bin/env python3
"""Compact, capped views of a research run's records.

The author LLM reads its records through this tool instead of `cat`, because
every byte it reads is re-sent on every later model call until the next
compaction. Each command prints a bounded excerpt and says what it cut.

    records.py [--dir DIR] [--max-bytes N] overview
    records.py node A012 [--section objective|detailed|obstacles|conclusions|all] [--lines N]
    records.py lemma L034 [--full] [--lines N]
    records.py brief L034
    records.py grep PATTERN [--context N]
    records.py audits
    records.py validate
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_MAX_BYTES = 16000
NODE_ID = re.compile(r"^A\d{3}$")
LEMMA_ID = re.compile(r"^L\d{3}$")
LEMMA_HEADING = re.compile(r"^## (L\d{3})\b(.*)$")
NODE_HEADING = re.compile(r"^# (A\d{3})\b(.*)$")
SECTION_HEADING = re.compile(r"^## (.+?)\s*$")
PROOF_START = re.compile(r"^(\*\*Proof\.?\*\*|Proof\.|\*\*Proof\b|### )")
SECTION_ALIASES = {
    "objective": ("context and objective",),
    "detailed": ("detailed work",),
    "obstacles": ("obstacles",),
    "conclusions": ("conclusions",),
}


class Records:
    def __init__(self, directory, max_bytes=DEFAULT_MAX_BYTES):
        self.directory = Path(directory)
        self.max_bytes = max(1000, int(max_bytes))
        self.approaches = self.directory / "APPROACHES"
        self.proved = self.directory / "PROVED.md"
        self.audits = self.directory / "AUDITS"

    # ----- helpers -----------------------------------------------------
    def _read(self, path):
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _cap(self, text, limit=None):
        """Cut text to the byte cap and say so; never silently truncate."""
        limit = self.max_bytes if limit is None else limit
        data = text.encode("utf-8")
        if len(data) <= limit:
            return text
        cut = data[:limit].decode("utf-8", errors="ignore")
        cut = cut[: cut.rfind("\n")] if "\n" in cut else cut
        return f"{cut}\n[... cut at {limit} bytes of {len(data)}; narrow the request with --section, --lines or grep ...]\n"

    @staticmethod
    def _head(lines, count, label):
        if count and len(lines) > count:
            return lines[:count] + [f"[... {len(lines) - count} more lines in {label}; use --lines N or --lines 0 ...]"]
        return lines

    def node_files(self):
        if not self.approaches.is_dir():
            return {}
        files = {}
        for path in sorted(self.approaches.glob("A[0-9][0-9][0-9]*.md")):
            files.setdefault(path.name[:4], path)
        return files

    def index_text(self):
        return self._read(self.approaches / "index.md")

    # ----- overview ----------------------------------------------------
    def plan(self):
        lines = self.index_text().splitlines()
        out, inside = [], False
        for line in lines:
            if SECTION_HEADING.match(line):
                if inside:
                    break
                inside = line.strip().lower().startswith("## plan")
                if inside:
                    out.append(line)
                    continue
            if inside:
                out.append(line)
        return out

    def index_rows(self):
        rows = []
        for line in self.index_text().splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4 or not re.search(r"A\d{3}", cells[0]):
                continue
            if set(cells[0]) <= set("-: "):
                continue
            match = re.search(r"A\d{3}", cells[0])
            title = re.sub(r"\[|\]\([^)]*\)", "", cells[0])
            title = re.sub(r"^A\d{3}\s*[—-]\s*", "", title).strip()
            status = next((cell for cell in cells[1:] if cell.upper() in {"ACTIVE", "BLOCKED", "CLOSED", "RESOLVED", "PARKED"}), "")
            result = cells[-1] if cells[-1].upper() != status else ""
            rows.append((match.group(), status, title[:60], result[:110]))
        return rows

    def lemma_titles(self):
        titles, current, verified = [], None, set()
        for line in self._read(self.proved).splitlines():
            match = LEMMA_HEADING.match(line)
            if match:
                current = match.group(1)
                titles.append((current, f"{match.group(1)}{match.group(2)[:90]}"))
            elif current and line.lstrip().startswith("**Verified.**"):
                verified.add(current)
        return [(lemma_id, title, lemma_id in verified) for lemma_id, title in titles]

    def overview(self, full=False):
        parts = []
        plan = self.plan()
        parts.append("## PLAN" if not plan else plan[0])
        parts.extend(self._head(plan[1:], 40, "PLAN") if plan else ["(no PLAN section in APPROACHES/index.md yet)"])
        rows = self.index_rows()
        parts.append(f"\n## Approach nodes ({len(rows)} in index; {len(self.node_files())} files)")
        live = [row for row in rows if row[1].upper() in {"ACTIVE", "BLOCKED", ""}]
        settled = [row for row in rows if row not in live]
        for node, status, title, result in live:
            parts.append(f"{node} [{status or '?'}] {title} :: {result}")
        if settled:
            parts.append("Settled nodes (use `node ID` for details): " + "; ".join(
                f"{node} [{status}] {title[:40]}" for node, status, title, _ in settled))
        titles = self.lemma_titles()
        parts.append(f"\n## Proved lemmas ({len(titles)})")
        if len(titles) <= 40 or full:
            parts.extend(f"{title}{'' if verified else '  [unverified]'}" for _, title, verified in titles)
        else:
            parts.append("IDs: " + ", ".join(lemma_id for lemma_id, _, _ in titles)
                         + "  (titles: `overview --full`; statements: `lemma ID`; search: `grep`)")
        unverified = [lemma_id for lemma_id, _, verified in titles if not verified]
        if unverified:
            parts.append("Lemmas without a **Verified.** line: " + ", ".join(unverified))
        digest = self.directory / "audit-digest.md"
        reports = sorted(self.audits.glob("*.md")) if self.audits.is_dir() else []
        parts.append(f"\n## Audits: digest {'present' if digest.is_file() else 'absent'}; {len(reports)} report(s) in AUDITS/")
        sizes = []
        for path in (self.proved, self.approaches / "index.md"):
            if path.is_file():
                sizes.append(f"{path.name} {path.stat().st_size // 1024} KB")
        parts.append("Sizes: " + ", ".join(sizes))
        return self._cap("\n".join(parts) + "\n")

    # ----- node --------------------------------------------------------
    def node(self, node_id, section="objective", lines=80):
        node_id = node_id.upper()
        if not NODE_ID.match(node_id):
            return f"Use a node ID like A012 (got {node_id!r}).\n"
        path = self.node_files().get(node_id)
        if path is None:
            return f"No node file for {node_id} in APPROACHES/.\n"
        content = self._read(path).splitlines()
        header, sections, current = [], {}, None
        for line in content:
            match = SECTION_HEADING.match(line)
            if match:
                current = match.group(1).strip().lower()
                sections.setdefault(current, [])
                continue
            if current is None:
                header.append(line)
            else:
                sections[current].append(line)
        out = [f"# {path.name}"] + self._head([line for line in header if line.strip()], 14, "header")
        wanted = list(sections) if section == "all" else [
            name for name in sections if any(name.startswith(alias) for alias in SECTION_ALIASES.get(section, (section.lower(),)))]
        if not wanted:
            out.append(f"\n(no section matching {section!r}; sections: {', '.join(sections) or 'none'})")
        for name in wanted:
            body = sections[name]
            while body and not body[0].strip():
                body = body[1:]
            while body and not body[-1].strip():
                body = body[:-1]
            out.append(f"\n## {name}")
            out.extend(self._head(body, lines, f"section {name!r}"))
        return self._cap("\n".join(out) + "\n")

    # ----- lemma -------------------------------------------------------
    def lemma(self, lemma_id, full=False, lines=200):
        lemma_id = lemma_id.upper()
        if not LEMMA_ID.match(lemma_id):
            return f"Use a lemma ID like L034 (got {lemma_id!r}).\n"
        block, inside = [], False
        for line in self._read(self.proved).splitlines():
            match = LEMMA_HEADING.match(line)
            if match:
                if inside:
                    break
                inside = match.group(1) == lemma_id
            if inside:
                block.append(line)
        if not block:
            return f"No lemma {lemma_id} in PROVED.md.\n"
        if not full:
            statement = []
            for line in block:
                if statement and PROOF_START.match(line.strip()):
                    statement.append(f"[... proof omitted; use `lemma {lemma_id} --full` ...]")
                    break
                statement.append(line)
            block = statement
        return self._cap("\n".join(self._head(block, lines, lemma_id)) + "\n")

    # ----- brief -------------------------------------------------------
    def brief(self, lemma_id, lines=400):
        """A verification packet: the lemma in full plus the statements it cites."""
        lemma_id = lemma_id.upper()
        body = self.lemma(lemma_id, full=True, lines=lines)
        if body.startswith("No lemma") or body.startswith("Use a lemma"):
            return body
        cited = sorted({match for match in re.findall(r"\bL\d{3}\b", body) if match != lemma_id})
        parts = [f"# Verification packet for {lemma_id}", "",
                 "Judge only the lemma below against the statements it cites; read nothing else.", "", body]
        if cited:
            parts.append(f"\n# Statements of cited lemmas ({', '.join(cited)})\n")
            for other in cited:
                parts.append(self.lemma(other, full=False, lines=60))
        return self._cap("\n".join(parts), max(self.max_bytes, 32000))

    # ----- grep --------------------------------------------------------
    def grep(self, pattern, context=0, limit=60):
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"Invalid pattern: {exc}\n"
        files = []
        if self.approaches.is_dir():
            files.extend(sorted(self.approaches.glob("*.md")))
        if self.proved.is_file():
            files.append(self.proved)
        if self.audits.is_dir():
            files.extend(sorted(self.audits.glob("*.md")))
        out, count = [], 0
        for path in files:
            lines = self._read(path).splitlines()
            for number, line in enumerate(lines, 1):
                if regex.search(line):
                    count += 1
                    if count > limit:
                        out.append(f"[... more than {limit} matches; refine the pattern ...]")
                        return self._cap("\n".join(out) + "\n")
                    start, end = max(0, number - 1 - context), min(len(lines), number + context)
                    for index in range(start, end):
                        marker = ":" if index == number - 1 else "-"
                        out.append(f"{path.relative_to(self.directory)}:{index + 1}{marker} {lines[index][:220]}")
        if not out:
            out.append("no matches")
        return self._cap("\n".join(out) + "\n")

    # ----- validate ----------------------------------------------------
    def validate(self):
        """Deterministic record checks that replace ad-hoc index scripts."""
        problems = []
        files = self.node_files()
        rows = {node: (status, title) for node, status, title, _ in self.index_rows()}
        if not self.plan():
            problems.append("APPROACHES/index.md has no '## PLAN' section")
        for node in sorted(set(files) | set(rows)):
            if node not in rows:
                problems.append(f"{node}: node file without an index row")
            if node not in files:
                problems.append(f"{node}: index row without a node file")
        links = {}
        for node, path in files.items():
            text = self._read(path)
            header = text.split("\n## ", 1)[0]
            status = re.search(r"^Status:\s*([A-Z]+)", header, re.MULTILINE)
            if not status or status.group(1) not in {"ACTIVE", "BLOCKED", "CLOSED", "RESOLVED"}:
                problems.append(f"{node}: missing or invalid Status line")
            elif node in rows and rows[node][0] and rows[node][0].upper() != status.group(1):
                problems.append(f"{node}: status {status.group(1)} in the file but {rows[node][0]} in the index")
            if not re.search(r"^Abstract:", header, re.MULTILINE):
                problems.append(f"{node}: missing Abstract line")
            if not re.search(r"^Closes the MISSING step:", header, re.MULTILINE):
                problems.append(f"{node}: missing 'Closes the MISSING step' line")
            parents = re.search(r"^Parents:(.*)$", header, re.MULTILINE)
            children = re.search(r"^Children:(.*)$", header, re.MULTILINE)
            links[node] = (set(re.findall(r"A\d{3}", parents.group(1))) if parents else set(),
                           set(re.findall(r"A\d{3}", children.group(1))) if children else set())
            for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", text):
                if target.startswith(("http://", "https://")):
                    continue
                if not (path.parent / target).exists():
                    problems.append(f"{node}: broken link {target}")
        for node, (parents, children) in links.items():
            for parent in parents:
                if parent not in files:
                    problems.append(f"{node}: unknown parent {parent}")
                elif node not in links[parent][1]:
                    problems.append(f"{node}: lists parent {parent}, which does not list it as a child")
            for child in children:
                if child not in files:
                    problems.append(f"{node}: unknown child {child}")
                elif node not in links[child][0]:
                    problems.append(f"{node}: lists child {child}, which does not list it as a parent")
        if not problems:
            return f"OK: {len(files)} node files, {len(rows)} index rows, links reciprocal.\n"
        return "\n".join(problems[:80]) + (f"\n[... {len(problems) - 80} more ...]" if len(problems) > 80 else "") + "\n"

    # ----- audits ------------------------------------------------------
    def audits_view(self):
        digest = self.directory / "audit-digest.md"
        if digest.is_file():
            return self._cap(self._read(digest))
        if not self.audits.is_dir():
            return "No AUDITS/ directory.\n"
        out = ["No audit-digest.md; current reports:"]
        for path in sorted(self.audits.glob("*.md")):
            first = next((line for line in self._read(path).splitlines() if line.strip()), "")
            out.append(f"{path.name} ({path.stat().st_size // 1024} KB): {first[:120]}")
        return self._cap("\n".join(out) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compact views of research records.")
    parser.add_argument("--dir", default=".", help="run directory (default: current directory)")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="output cap per call")
    commands = parser.add_subparsers(dest="command", required=True)
    overview = commands.add_parser("overview", help="PLAN, live index rows, lemma IDs, audit status")
    overview.add_argument("--full", action="store_true", help="list every lemma title and settled node")
    brief = commands.add_parser("brief", help="verification packet: one lemma in full plus the statements it cites")
    brief.add_argument("id")
    node = commands.add_parser("node", help="one or more nodes, header plus a section")
    node.add_argument("id", nargs="+")
    node.add_argument("--section", default="objective", help="objective|detailed|obstacles|conclusions|all|<heading prefix>")
    node.add_argument("--lines", type=int, default=80, help="lines per section (0 = all)")
    lemma = commands.add_parser("lemma", help="one or more lemma statements, or full proofs with --full")
    lemma.add_argument("id", nargs="+")
    lemma.add_argument("--full", action="store_true")
    lemma.add_argument("--lines", type=int, default=200)
    grep = commands.add_parser("grep", help="regex search over nodes, lemmas, and audits")
    grep.add_argument("pattern")
    grep.add_argument("--context", type=int, default=0)
    commands.add_parser("audits", help="the audit digest, or the list of current reports")
    commands.add_parser("validate", help="check index rows, statuses, headers, links, and parent/child reciprocity")
    args = parser.parse_args(argv)
    records = Records(args.dir, args.max_bytes)
    if args.command == "overview":
        text = records.overview(args.full)
    elif args.command == "brief":
        text = records.brief(args.id)
    elif args.command == "node":
        text = "\n".join(records.node(node_id, args.section, args.lines) for node_id in args.id)
    elif args.command == "lemma":
        text = "\n".join(records.lemma(lemma_id, args.full, args.lines) for lemma_id in args.id)
    elif args.command == "grep":
        text = records.grep(args.pattern, args.context)
    elif args.command == "validate":
        text = records.validate()
    else:
        text = records.audits_view()
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
