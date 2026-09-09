"""Durable research history and rebuildable, human-readable research notebooks.

The SQLite database is authoritative. Events are append-only; small checkpoint
values may be replaced in the same transaction as an event. Markdown files are
derived views: a failed export never removes a committed research result, and
``rebuild_views`` repairs an interrupted export. No event or proof is truncated.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading


DATABASE_FILENAME = "research.sqlite3"
SCHEMA_VERSION = "1"
INDEX_LIMIT = 200
CACHE_PREFIXES = ("response:", "raw:", "retrieval:", "mechanism:", "proof:", "import:")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _tokens(value):
    return re.findall(r"\w+", value.casefold(), flags=re.UNICODE)


def _search_text(value):
    if isinstance(value, dict):
        return "\n".join(str(key) + "\n" + _search_text(item) for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(_search_text(item) for item in value)
    return str(value) if value is not None else ""


def _is_failure(kind, payload):
    """Index explicit unsuccessful outcomes, without treating timeouts as refutations."""
    failed = {"reject", "rejected", "duplicate", "blocked", "needs_author", "incomplete",
              "refuted", "failed", "failure", "interrupted", "needs_revision"}
    if kind.casefold() in failed or kind.casefold().endswith(("_rejected", "_failure")):
        return True
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"status", "outcome", "verdict", "decision", "approach_result"}:
                if isinstance(value, str) and value.casefold() in failed:
                    return True
            if any(part in kind.casefold() for part in ("review", "critic", "novelty")):
                if key in {"approved", "passed", "pass", "allow", "accepted"} and value is False:
                    return True
                if key in {"bugs", "blocked_routes", "unresolved_obligations"} and value:
                    return True
            if isinstance(value, (dict, list)) and _is_failure(kind, value):
                return True
    elif isinstance(payload, list):
        return any(_is_failure(kind, value) for value in payload)
    return False


def _heading(value):
    return str(value).replace("_", " ").replace("\n", " ")


def _render_value(value, depth=2):
    """Render every field, preserving full text (including long proofs and Unicode)."""
    if isinstance(value, dict):
        if not value:
            return "_None recorded._\n"
        parts = []
        for key, item in value.items():
            title = _heading(key)
            prefix = "#" * min(depth, 6)
            parts.append(f"{prefix} {title}\n\n{_render_value(item, depth + 1)}")
        return "\n".join(parts)
    if isinstance(value, list):
        if not value:
            return "_None recorded._\n"
        parts = []
        for index, item in enumerate(value, 1):
            if isinstance(item, (dict, list)):
                parts.append(f"{'#' * min(depth, 6)} Item {index}\n\n{_render_value(item, depth + 1)}")
            else:
                parts.append(f"{index}. {_render_value(item, depth).rstrip().replace(chr(10), chr(10) + '   ')}\n")
        return "\n".join(parts)
    if value is None:
        return "_Not provided._\n"
    if isinstance(value, bool):
        return ("Yes" if value else "No") + "\n"
    return str(value) + "\n"


def _atomic_text(path, content):
    """Replace one derived file atomically; write errors must reach the caller."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ResearchJournal:
    """One statement's permanent event archive, safe to reopen with new prompts."""

    def __init__(self, directory, statement):
        if not isinstance(statement, str):
            raise TypeError("Research statement must be a string")
        self.directory = Path(directory)
        self.path = self.directory / DATABASE_FILENAME
        self.views = self.directory / "research"
        self.statement = statement
        self._lock = threading.RLock()
        self._closed = False
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = False
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created = True
        except FileExistsError:
            pass
        self._db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        try:
            # Inspect before any schema writes: damaged or unrelated files are retained.
            tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not created and "metadata" not in tables:
                raise ValueError(f"Existing file is not a research archive: {self.path}")
            self._db.execute("PRAGMA busy_timeout = 30000")
            self._db.execute("PRAGMA synchronous = FULL")
            self._db.execute("PRAGMA foreign_keys = ON")
            if created:
                self._create_schema()
            metadata = dict(self._db.execute("SELECT key, value FROM metadata"))
            fingerprint = hashlib.sha256(statement.encode("utf-8")).hexdigest()
            if metadata.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("Unsupported research archive schema; original file was retained")
            if metadata.get("statement_sha256") != fingerprint or metadata.get("statement") != statement:
                raise ValueError("Research archive belongs to a different statement; original file was retained")
            integrity = self._db.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"Research archive integrity check failed: {integrity}")
            self._fts = metadata.get("fts5") == "1"
            self.path.chmod(0o600)
            self.views.mkdir(mode=0o700, exist_ok=True)
            (self.views / "records").mkdir(mode=0o700, exist_ok=True)
            # Reopening repairs a crash between the DB commit and readable export.
            self.rebuild_views(only_missing=True)
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    @contextmanager
    def _transaction(self):
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    def _create_schema(self):
        with self._transaction():
            self._db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute("""CREATE TABLE events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                search_text TEXT NOT NULL, is_failure INTEGER NOT NULL DEFAULT 0)""")
            self._db.execute("CREATE INDEX events_kind_sequence ON events(kind, sequence)")
            self._db.execute("CREATE INDEX events_failure_sequence ON events(is_failure, sequence)")
            self._db.execute("CREATE TABLE checkpoint (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._db.execute("""CREATE TRIGGER immutable_events_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'Research events are append-only'); END""")
            self._db.execute("""CREATE TRIGGER immutable_events_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'Research events are append-only'); END""")
            fts = True
            try:
                self._db.execute("CREATE VIRTUAL TABLE event_search USING fts5(event_id UNINDEXED, content)")
            except sqlite3.OperationalError as exc:
                if "no such module: fts5" not in str(exc).lower():
                    raise
                fts = False
            self._db.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", [
                ("schema_version", SCHEMA_VERSION),
                ("statement", self.statement),
                ("statement_sha256", hashlib.sha256(self.statement.encode("utf-8")).hexdigest()),
                ("fts5", "1" if fts else "0"),
            ])

    @staticmethod
    def _record(row):
        return {"id": f"e{row['sequence']:06d}", "kind": row["kind"],
                "payload": json.loads(row["payload"]), "createdAt": row["created_at"]}

    @staticmethod
    def _limit(limit):
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a nonnegative integer")
        return limit

    def commit(self, kind, payload, state_updates=None):
        """Durably append an event and checkpoint, then update readable views.

        A Markdown export error is raised after the database commit. The result
        remains in ``events`` and reopening/rebuilding repairs the readable view.
        """
        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("Event kind must be a nonempty string")
        if state_updates is not None and not isinstance(state_updates, dict):
            raise TypeError("state_updates must be a dictionary")
        encoded = _json(payload)
        updates = []
        for key, value in (state_updates or {}).items():
            if not isinstance(key, str) or not key:
                raise ValueError("Checkpoint keys must be nonempty strings")
            updates.append((key, _json(value)))
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        search_text = kind + "\n" + _search_text(payload)
        with self._lock:
            with self._transaction():
                cursor = self._db.execute(
                    "INSERT INTO events(kind, payload, created_at, search_text, is_failure) VALUES (?, ?, ?, ?, ?)",
                    (kind, encoded, created_at, search_text, int(_is_failure(kind, payload))))
                sequence = cursor.lastrowid
                event_id = f"e{sequence:06d}"
                if self._fts:
                    self._db.execute("INSERT INTO event_search(event_id, content) VALUES (?, ?)",
                                     (event_id, search_text))
                self._db.executemany(
                    "INSERT INTO checkpoint(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value", updates)
            record = {"id": event_id, "kind": kind, "payload": json.loads(encoded), "createdAt": created_at}
            self._write_record(record)
            self._write_indexes()
            return event_id

    def get_state(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM checkpoint WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def get(self, event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r"e\d+", event_id):
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM events WHERE sequence = ?", (int(event_id[1:]),)).fetchone()
            return self._record(row) if row else None

    def recent(self, kind=None, limit=12):
        limit = self._limit(limit)
        sql = "SELECT * FROM events"
        parameters = []
        if kind is not None:
            sql += " WHERE kind = ?"
            parameters.append(kind)
        sql += " ORDER BY sequence DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            return [self._record(row) for row in reversed(self._db.execute(sql, parameters).fetchall())]

    def events(self, kind=None):
        """Iterate the complete retained history, in event order, without clipping."""
        # Fetch in pages so no transaction/lock is held while caller processes rows.
        with self._lock:
            maximum = self._db.execute("SELECT COALESCE(MAX(sequence), 0) FROM events").fetchone()[0]
        after = 0
        while after < maximum:
            sql = "SELECT * FROM events WHERE sequence > ? AND sequence <= ?"
            parameters = [after, maximum]
            if kind is not None:
                sql += " AND kind = ?"
                parameters.append(kind)
            sql += " ORDER BY sequence LIMIT 100"
            with self._lock:
                rows = self._db.execute(sql, parameters).fetchall()
            if not rows:
                break
            for row in rows:
                yield self._record(row)
            after = rows[-1]["sequence"]

    def search(self, query, limit=12, kinds=None):
        """Search all retained content; user text is never interpreted as SQL/FTS syntax."""
        limit = self._limit(limit)
        if not isinstance(query, str):
            raise TypeError("Search query must be a string")
        if kinds is not None and (not isinstance(kinds, (list, tuple, set)) or
                                  any(not isinstance(kind, str) for kind in kinds)):
            raise TypeError("kinds must be a collection of event kind strings")
        if kinds is not None and not kinds:
            return []
        tokens = list(dict.fromkeys(_tokens(query)))
        if not tokens or not limit:
            return []
        with self._lock:
            if self._fts:
                expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
                kind_filter = " AND events.kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds is not None else ""
                parameters = [expression] + (list(kinds) if kinds is not None else []) + [limit]
                rows = self._db.execute(
                    "SELECT events.* FROM event_search JOIN events "
                    "ON events.sequence = CAST(SUBSTR(event_search.event_id, 2) AS INTEGER) "
                    "WHERE event_search MATCH ?" + kind_filter +
                    " ORDER BY bm25(event_search), events.sequence DESC LIMIT ?", parameters).fetchall()
                return [self._record(row) for row in rows]
            # FTS5 is optional in Python's SQLite build. This safe fallback scans
            # the permanent archive, rather than searching only recent records.
            matches = []
            kind_filter = " WHERE kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds is not None else ""
            for row in self._db.execute("SELECT * FROM events" + kind_filter, list(kinds) if kinds is not None else []):
                words = set(_tokens(row["search_text"]))
                score = sum(token in words for token in tokens)
                if score:
                    matches.append((score, row["sequence"], self._record(row)))
            matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return [item[2] for item in matches[:limit]]

    def _write_record(self, record):
        title = f"# {record['id']} — {_heading(record['kind'])}\n\n"
        details = f"Recorded: {record['createdAt']}\n\n[Research index](../INDEX.md)\n\n"
        _atomic_text(self.views / "records" / (record["id"] + ".md"),
                     title + details + _render_value(record["payload"]))

    def _write_indexes(self):
        count = self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        kinds = self._db.execute("SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY kind").fetchall()
        records = self._db.execute("SELECT sequence, kind, created_at FROM events ORDER BY sequence DESC LIMIT ?",
                                   (INDEX_LIMIT,)).fetchall()
        links = [f"- [e{item['sequence']:06d} — {_heading(item['kind'])}](records/e{item['sequence']:06d}.md) · {item['created_at']}"
                 for item in records]
        index = (f"# Research history\n\n{count} permanent records. Every complete record is in `records/`. "
                 f"This index lists the most recent {INDEX_LIMIT}; older files remain available and searchable "
                 "in `../research.sqlite3`.\n\n"
                 "[Statement](STATEMENT.md) · [Current checkpoint](STATE.md) · [Failed or unfinished work](FAILED.md) · [Reviewed results and candidates](PROVED.md)\n\n"
                 "## Records by kind\n\n" + "\n".join(f"- {_heading(row[0])}: {row[1]}" for row in kinds)
                 + "\n\n## Recent records\n\n" + ("\n".join(links) or "No records yet.") + "\n")
        failed_count = self._db.execute("SELECT COUNT(*) FROM events WHERE is_failure = 1").fetchone()[0]
        failed = self._db.execute("SELECT sequence, kind FROM events WHERE is_failure = 1 ORDER BY sequence DESC LIMIT ?",
                                  (INDEX_LIMIT,)).fetchall()
        failure_links = [f"- [e{row['sequence']:06d} — {_heading(row['kind'])}](records/e{row['sequence']:06d}.md)"
                         for row in failed]
        failures = ("# Failed, rejected, or unfinished work\n\n"
                    "Read the evidence and reopening conditions in each record. An interruption or missing "
                    "argument does not establish that an approach is false.\n\n"
                    f"{failed_count} records; the most recent {INDEX_LIMIT} appear here. Complete history is retained.\n\n"
                    + ("\n".join(failure_links) or "No unsuccessful outcomes recorded yet.") + "\n")
        cache_where = " OR ".join("key LIKE ?" for _ in CACHE_PREFIXES)
        cache_patterns = [prefix + "%" for prefix in CACHE_PREFIXES]
        checkpoint = {row[0]: json.loads(row[1]) for row in self._db.execute(
            "SELECT key, value FROM checkpoint WHERE NOT (" + cache_where + ") ORDER BY key", cache_patterns)}
        cache_counts = {prefix: self._db.execute("SELECT COUNT(*) FROM checkpoint WHERE key LIKE ?",
                                               (prefix + "%",)).fetchone()[0] for prefix in CACHE_PREFIXES}
        candidate_id = checkpoint.get("currentAttemptId")
        candidate_record = self.get(candidate_id)
        if candidate_record is None and candidate_id:
            # Proof identities are stable content IDs; event IDs name files.
            matching = self.search(candidate_id, limit=1, kinds=['candidate', 'research_review'])
            candidate_record = matching[0] if matching else None
        candidate_event_id = candidate_record['id'] if candidate_record else None
        # STATE is a workbench, not a second copy of every response and proof.
        # Event cards and the DB retain the complete unabridged values.
        def summarize(value, key=""):
            if isinstance(value, dict):
                return {name: summarize(item, name) for name, item in value.items()}
            if isinstance(value, list):
                return [summarize(item, key) for item in value]
            if isinstance(value, str) and (len(value) > 6000 or key in {"solution", "candidate", "proof", "raw"}):
                reference = (f" See [current candidate {candidate_id}](records/{candidate_event_id}.md)."
                             if candidate_event_id and key in {"solution", "candidate", "proof"} else "")
                return f"Full text ({len(value)} characters) is retained in the SQLite checkpoint and its research records.{reference}"
            return value
        visible_checkpoint = summarize(checkpoint)
        results = self._db.execute(
            "SELECT * FROM events WHERE kind IN ('research_review', 'candidate', 'critic', 'candidate_status') "
            "ORDER BY sequence DESC LIMIT ?", (INDEX_LIMIT,)).fetchall()
        result_links = []
        for row in results:
            record = self._record(row)
            payload = record["payload"]
            status = "reviewed record"
            if isinstance(payload, dict):
                report = payload.get("review", payload.get("report", payload))
                if isinstance(report, dict):
                    status = str(report.get("verdict", report.get("status", status)))
            result_links.append(f"- [{record['id']} — {_heading(record['kind'])}](records/{record['id']}.md) · {status}")
        proved = ("# Reviewed results and candidate arguments\n\n"
                  "This is an index of model-reviewed research, reusable results, candidates, and their recorded "
                  "review status. The filename PROVED.md does not mean that every entry is proved: candidates "
                  "may still have gaps, and model review is not formal proof verification. Read each record's "
                  "assumptions, evidence, reusable results, and remaining obligations.\n\n"
                  f"The most recent {INDEX_LIMIT} records appear here; all earlier records are retained.\n\n"
                  + ("\n".join(result_links) or "No reviewed results or candidates yet.") + "\n")
        _atomic_text(self.views / "INDEX.md", index)
        _atomic_text(self.views / "FAILED.md", failures)
        _atomic_text(self.views / "PROVED.md", proved)
        _atomic_text(self.views / "STATE.md", "# Current research checkpoint\n\n"
                     "Large proof bodies and internal caches remain in the database; this notebook shows the "
                     "working state and links to the permanent records.\n\n" + _render_value(visible_checkpoint)
                     + "\n## Internal checkpoint counts\n\n" + _render_value(cache_counts, 3))
        _atomic_text(self.views / "STATEMENT.md", "# Original statement\n\n" + self.statement + "\n")

    def rebuild_views(self, only_missing=False):
        """Regenerate readable notebooks from SQLite; never alter the event history."""
        with self._lock:
            self.views.mkdir(parents=True, exist_ok=True, mode=0o700)
            (self.views / "records").mkdir(exist_ok=True, mode=0o700)
            for record in self.events():
                if not only_missing or not (self.views / "records" / (record["id"] + ".md")).is_file():
                    self._write_record(record)
            self._write_indexes()

    def backup_to(self, destination_directory):
        """Create a consistent SQLite backup and rebuild all of its readable views.

        Refuse to replace an existing archive: continuation must use a new run
        directory, and an accidental retry must not destroy newer research.
        """
        destination = Path(destination_directory)
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = destination / DATABASE_FILENAME
        if self.path.resolve() == target.resolve():
            self.rebuild_views()
            return target
        with self._lock:
            return _backup_connection(self._db, self.statement, destination)

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def _backup_connection(database, statement, destination_directory):
    destination = Path(destination_directory)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = destination / DATABASE_FILENAME
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing research archive: {target}")
    fd, temporary = tempfile.mkstemp(prefix=".research-backup-", dir=str(destination))
    os.close(fd)
    try:
        backup = sqlite3.connect(temporary)
        try:
            database.backup(backup)
            backup.commit()
        finally:
            backup.close()
        with open(temporary, "rb") as copied:
            os.fsync(copied.fileno())
        # Hard linking avoids racing with another process that created target.
        os.link(temporary, target)
        with ResearchJournal(destination, statement) as restored:
            restored.rebuild_views()
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def copy_research_archive(source_directory, destination_directory):
    """Copy a prior run's archive. Missing archive is a compatible legacy run."""
    source = Path(source_directory) / DATABASE_FILENAME
    if not source.exists():
        return False
    # Read-only discovery cannot create or repair a corrupted input database.
    database = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        metadata = dict(database.execute("SELECT key, value FROM metadata"))
        statement = metadata.get("statement")
        if statement is None:
            raise ValueError(f"Research archive has no statement: {source}")
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported research archive schema: {source}")
        if hashlib.sha256(statement.encode("utf-8")).hexdigest() != metadata.get("statement_sha256"):
            raise ValueError(f"Research archive statement identity is inconsistent: {source}")
        integrity = database.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"Research archive integrity check failed: {integrity}")
        _backup_connection(database, statement, destination_directory)
    finally:
        database.close()
    return True
