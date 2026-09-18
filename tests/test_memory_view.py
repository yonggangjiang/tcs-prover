"""The optional viewer reads only the selected author's notebooks and routes."""

import json
import os
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ui import server


class PageElements(HTMLParser):
    """Use the shipped markup for browser fixtures, including missing elements."""

    def __init__(self):
        super().__init__()
        self.elements = {}
        self.feed((server.UI / "index.html").read_text())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.elements[attrs["id"]] = attrs


class MemoryViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runs = Path(temporary.name)
        self.original = self.runs / "original"
        self.resumed = self.runs / "resumed"
        self.original.mkdir()
        self.resumed.mkdir()
        self.app = server.App(runs=self.runs)
        self.app.run_dir = self.resumed
        self.app.state["goalWorkspace"] = str(self.original)

    def test_resumed_job_reads_original_files_without_modifying_them(self):
        for name in server.RESEARCH_MEMORY_FILES:
            path = self.original / name
            content = "# Saved work\n\n∀x: exact argument <script>text only</script>\n"
            path.write_text(content, encoding="utf-8")
            (self.resumed / name).write_text("Wrong workspace")
            before = (path.read_bytes(), path.stat().st_mtime_ns)
            result = self.app.memory_file(name)
            self.assertEqual(result["content"], content)
            self.assertEqual(result["workspace"], "original")
            self.assertEqual(result["status"], "ready")
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_new_job_uses_its_own_workspace_and_missing_files_stay_missing(self):
        self.app.state["goalWorkspace"] = ""
        self.assertEqual(self.app.memory_file("APPROACHES.md")["status"], "missing")
        self.assertEqual(list(self.resumed.iterdir()), [])
        (self.resumed / "APPROACHES.md").write_text("New approach")
        self.assertEqual(self.app.memory_file("APPROACHES.md")["content"], "New approach")
        self.app.state["goalWorkspace"] = str(self.original)
        self.assertEqual(self.app.memory_file("APPROACHES.md")["status"], "missing")

    def test_unchanged_poll_does_not_read_or_transfer_contents(self):
        path = self.original / "PROVED.md"
        path.write_text("# Lemmas")
        first = self.app.memory_file(path.name)
        with patch.object(Path, "read_text", side_effect=AssertionError("Unchanged file was read")):
            second = self.app.memory_file(path.name, first["version"])
        self.assertTrue(second["unchanged"])
        self.assertNotIn("content", second)
        path.write_text("# Lemmas\n\n## L001\nA new proved result.")
        updated = self.app.memory_file(path.name, first["version"])
        self.assertNotEqual(first["version"], updated["version"])
        self.assertIn("L001", updated["content"])

    def test_arbitrary_paths_and_symlinks_are_not_read(self):
        for name in ("../secret.txt", "job-settings.json", "/etc/passwd", "", "prompts/author.txt"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.app.memory_file(name)
        secret = self.runs / "secret.txt"
        secret.write_text("Private contents")
        (self.original / "INITIAL_PROMPT.md").symlink_to(secret)
        result = self.app.memory_file("INITIAL_PROMPT.md")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("content", result)

    def test_temporary_unreadable_file_is_reported_without_hiding_other_files(self):
        (self.original / "PROVED.md").write_bytes(b"\xff")
        self.assertEqual(self.app.memory_file("PROVED.md")["status"], "unavailable")
        (self.original / "APPROACHES.md").write_text("Still readable")
        self.assertEqual(self.app.memory_file("APPROACHES.md")["content"], "Still readable")

    def test_approaches_defaults_to_index_and_lists_only_direct_route_files(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "INDEX.md").write_text("# Approach index")
        (folder / "A001-first-route.md").write_text("First argument")
        (folder / "A002.md").write_bytes(b"\xff")  # Listing must not read route contents.
        (folder / "notes.md").write_text("Not a route")
        (folder / "A003-nested.md").mkdir()
        (folder / "A004-link.md").symlink_to(self.original / "PROVED.md")
        (self.original / "APPROACHES.md").write_text("Legacy archive")
        result = self.app.memory_file("APPROACHES")
        self.assertEqual(result["name"], "APPROACHES/INDEX.md")
        self.assertEqual(result["content"], "# Approach index")
        self.assertEqual(result["files"], ["APPROACHES/INDEX.md", "APPROACHES/A001-first-route.md", "APPROACHES/A002.md", "APPROACHES.md"])
        self.assertEqual(self.app.memory_file(result["files"][1])["content"], "First argument")
        self.assertEqual(self.app.memory_file("APPROACHES.md")["content"], "Legacy archive")
        self.assertEqual(self.app.memory_file("APPROACHES.md")["files"], result["files"])

    def test_alias_falls_back_to_legacy_and_new_folder_takes_precedence(self):
        legacy = self.original / "APPROACHES.md"
        legacy.write_text("Old work")
        original_bytes = legacy.read_bytes()
        result = self.app.memory_file("APPROACHES")
        self.assertEqual(result["name"], "APPROACHES.md")
        self.assertEqual(result["content"], "Old work")
        self.assertEqual(result["files"], [])
        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "A001.md").write_text("New work")
        result = self.app.memory_file("APPROACHES", result["version"])
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["name"], "APPROACHES/INDEX.md")
        self.assertEqual(result["files"], ["APPROACHES/INDEX.md", "APPROACHES/A001.md", "APPROACHES.md"])
        self.assertEqual(legacy.read_bytes(), original_bytes)

    def test_unchanged_index_poll_refreshes_route_listing(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "INDEX.md").write_text("Index")
        first = self.app.memory_file("APPROACHES")
        (folder / "A001.md").write_text("First route")
        second = self.app.memory_file("APPROACHES", first["version"])
        self.assertTrue(second["unchanged"])
        self.assertNotIn("content", second)
        self.assertIn("APPROACHES/A001.md", second["files"])
        (folder / "A001.md").unlink()
        self.assertEqual(self.app.memory_file("APPROACHES", first["version"])["files"], ["APPROACHES/INDEX.md"])

    def test_graph_uses_node_files_in_resumed_workspace_without_an_index(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        first = "# A001 — First route\n\nParents: none\nChildren: A099\nStatus: ACTIVE.\n\n## Context and objective\nStart here.\n\nMore detail."
        second = "# A002 — Follow-up\n\n**Parents:** [A001](A001-first.md), A001\n**Status:** RESOLVED\n\n## Context and objective\nTry it.\n\n## Conclusions\nIt works."
        (folder / "A001-first.md").write_text(first)
        (folder / "A002.md").write_text(second)
        wrong = self.resumed / "APPROACHES"
        wrong.mkdir()
        (wrong / "A999.md").write_text("Wrong workspace")
        result = self.app.approach_graph()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["indexFile"], "")
        self.assertEqual(result["files"], ["APPROACHES/A001-first.md", "APPROACHES/A002.md"])
        self.assertEqual(result["nodes"], [
            {"id": "A001", "file": "APPROACHES/A001-first.md", "title": "First route", "parents": [],
             "status": "ACTIVE", "result": "Start here.", "hasParents": True, "readStatus": "ready"},
            {"id": "A002", "file": "APPROACHES/A002.md", "title": "Follow-up", "parents": ["A001"],
             "status": "RESOLVED", "result": "It works.", "hasParents": True, "readStatus": "ready"},
        ])
        self.assertFalse((folder / "index.md").exists())
        # Even an unreadable index cannot affect the graph.
        (folder / "index.md").write_bytes(b"\xff")
        with patch.object(server, "approach_metadata", side_effect=AssertionError("Unchanged node parsed")):
            unchanged = self.app.approach_graph()
        self.assertEqual(unchanged["nodes"], result["nodes"])
        self.assertEqual(unchanged["indexFile"], "APPROACHES/index.md")
        self.assertEqual((folder / "A001-first.md").read_text(), first)

    def test_graph_refreshes_changed_added_and_removed_nodes_without_index_edits(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        index = folder / "index.md"
        index.write_text("Stale index: A002 has no parents and is ACTIVE.")
        (folder / "A001.md").write_text("# Root\nParents: none\nStatus: ACTIVE")
        route = folder / "A002.md"
        route.write_text("# Route\nParents: none\nStatus: ACTIVE")
        self.app.approach_graph()
        before = (index.read_bytes(), index.stat().st_mtime_ns)
        route.write_text("# Revised route\nParents: A001\nStatus: BLOCKED\n\n## Obstacles\nMissing bound.")
        with patch.object(server, "approach_metadata", wraps=server.approach_metadata) as parse:
            refreshed = self.app.approach_graph()
        self.assertEqual(parse.call_count, 1)
        self.assertEqual(refreshed["nodes"][1]["parents"], ["A001"])
        self.assertEqual(refreshed["nodes"][1]["status"], "BLOCKED")
        self.assertEqual(refreshed["nodes"][1]["result"], "Missing bound.")
        route.unlink()
        (folder / "A003.md").write_text("# Next route\nParents: A001\nStatus: ACTIVE")
        refreshed = self.app.approach_graph()
        self.assertEqual([node["id"] for node in refreshed["nodes"]], ["A001", "A003"])
        self.assertNotIn("APPROACHES/A002.md", self.app._approach_cache)
        self.assertEqual((index.read_bytes(), index.stat().st_mtime_ns), before)

    def test_graph_reports_and_retries_unreadable_nodes_without_hiding_others(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        broken = folder / "A001-unfinished-route.md"
        broken.write_bytes(b"\xff")
        (folder / "A002.md").write_text("# Readable\nParents: A001\nStatus: ACTIVE")
        for _ in range(2):
            result = self.app.approach_graph()
            self.assertEqual([node["readStatus"] for node in result["nodes"]], ["unavailable", "ready"])
            self.assertEqual(result["nodes"][0]["title"], "unfinished route")
            self.assertEqual(self.app._approach_cache["APPROACHES/A001-unfinished-route.md"]["version"], "")
        broken.write_text("# Recovered\nParents: none\nStatus: RESOLVED")
        result = self.app.approach_graph()
        self.assertEqual(result["nodes"][0]["readStatus"], "ready")
        self.assertEqual(result["nodes"][0]["title"], "Recovered")

    def test_graph_ignores_symlinks_and_does_not_invent_missing_index_files(self):
        self.assertEqual(self.app.approach_graph()["files"], [])
        (self.original / "APPROACHES.md").write_text("Historical notebook")
        self.assertEqual(self.app.approach_graph()["files"], ["APPROACHES.md"])
        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "A001.md").write_text("# Root\nParents: none\nStatus: ACTIVE")
        (folder / "A002.md").symlink_to(self.original / "APPROACHES.md")
        (folder / "index.md").symlink_to(self.original / "APPROACHES.md")
        result = self.app.approach_graph()
        self.assertEqual(result["files"], ["APPROACHES/A001.md", "APPROACHES.md"])
        self.assertEqual(result["indexFile"], "")
        folder.rename(self.original / "previous")
        folder.symlink_to(self.original / "previous")
        result = self.app.approach_graph()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["nodes"], [])

    def test_portable_graph_listing_works_without_index_and_node_reads_skip_listing(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "A001.md").write_text("# Root\nParents: none\nStatus: ACTIVE")
        result = self.app._memory_file_portable(
            self.original.resolve(), "APPROACHES", "", {"status": "missing"}, True, listing_only=True,
        )
        self.assertEqual(result["files"], ["APPROACHES/A001.md"])
        self.assertEqual(result["status"], "ready")
        self.assertNotIn("content", result)
        with patch.object(Path, "iterdir", side_effect=AssertionError("Node read rescanned directory")):
            node = self.app._memory_file_portable(
                self.original.resolve(), "APPROACHES/A001.md", "", {"status": "missing"}, True, include_files=False,
            )
        self.assertIn("# Root", node["content"])

    def test_node_metadata_uses_header_fields_and_preserves_missing_metadata(self):
        result = server.approach_metadata("APPROACHES/A010-slug-title.md", """# A010 — Title
Parent: A002, [A003](A003-other.md)
Status: PARKED.

## Context and objective
Explain the task.

## Detailed work
Status: RESOLVED
Parents: A999

## Obstacles
The key bound is missing.

More technical detail.
""")
        self.assertEqual(result["parents"], ["A002", "A003"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["result"], "The key bound is missing.")
        missing = server.approach_metadata("APPROACHES/A011-new-idea.md", "Work in progress")
        self.assertEqual(missing["title"], "new idea")
        self.assertEqual(missing["status"], "UNSPECIFIED")
        self.assertFalse(missing["hasParents"])

    def test_approach_traversal_and_symlinked_folder_are_rejected(self):
        for name in ("APPROACHES/../PROVED.md", "APPROACHES/sub/A001.md", "APPROACHES/secret.md",
                     "APPROACHES/A001.md/extra", "APPROACHES//A001.md", "APPROACHES/A001.md\n"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.app.memory_file(name)
        secret = self.runs / "outside"
        secret.mkdir()
        (secret / "INDEX.md").write_text("Private index")
        (secret / "A001.md").write_text("Private route")
        (self.original / "APPROACHES").symlink_to(secret)
        for name in ("APPROACHES", "APPROACHES/INDEX.md", "APPROACHES/A001.md"):
            result = self.app.memory_file(name)
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("content", result)

    @unittest.skipIf(os.name == "nt", "Exercises POSIX directory descriptors")
    def test_concurrent_file_symlink_swap_does_not_escape_workspace(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        route = folder / "A001.md"
        route.write_text("Route")
        secret = self.runs / "secret.txt"
        secret.write_text("Private contents")
        real_open = os.open

        def swap_before_open(path, flags, *args, **kwargs):
            if path == "A001.md":
                route.unlink()
                route.symlink_to(secret)
            return real_open(path, flags, *args, **kwargs)

        with patch.object(os, "open", side_effect=swap_before_open):
            result = self.app.memory_file("APPROACHES/A001.md")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("content", result)

    def test_concurrent_folder_replacement_keeps_open_directory(self):
        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "INDEX.md").write_text("Original index")
        secret = self.runs / "outside"
        secret.mkdir()
        (secret / "INDEX.md").write_text("Private index")
        real_listdir = os.listdir

        def swap_folder(descriptor):
            folder.rename(self.original / "previous")
            folder.symlink_to(secret)
            return real_listdir(descriptor)

        with patch.object(os, "listdir", side_effect=swap_folder):
            self.assertEqual(self.app.memory_file("APPROACHES")["content"], "Original index")

    def test_portable_reader_supports_folder_legacy_and_rejects_links(self):
        directory = self.original.resolve()

        def read(name="APPROACHES", version=""):
            return self.app._memory_file_portable(
                directory, name, version, {"name": name, "status": "missing"}, True,
            )

        (directory / "APPROACHES.md").write_text("Legacy work")
        self.assertEqual(read()["content"], "Legacy work")
        folder = directory / "APPROACHES"
        folder.mkdir()
        (folder / "INDEX.md").write_text("Index")
        (folder / "A001.md").write_text("Route")
        result = read()
        self.assertEqual(result["files"], ["APPROACHES/INDEX.md", "APPROACHES/A001.md", "APPROACHES.md"])
        self.assertEqual(read("APPROACHES/A001.md")["content"], "Route")
        self.assertEqual(read("APPROACHES.md")["content"], "Legacy work")
        self.assertTrue(read(version=result["version"])["unchanged"])
        (folder / "A002.md").symlink_to(directory / "APPROACHES.md")
        self.assertEqual(read("APPROACHES/A002.md")["status"], "unavailable")
        with patch.object(Path, "is_junction", lambda path: path == folder, create=True):
            self.assertEqual(read()["status"], "unavailable")

    def test_http_endpoint_requires_token_and_exact_job(self):
        (self.original / "PROVED.md").write_text("Lemma from the selected job")
        http = server.Server((server.HOST, 0), runs=self.runs)
        http.jobs["resumed"] = self.app
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(http.shutdown)
        url = http.origin + "/memory?job=resumed&file=PROVED.md"
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 403)
        request = Request(url, headers={"X-TCS-Prover-Token": http.token})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["content"], "Lemma from the selected job")
        for path in ("/memory?job=missing&file=PROVED.md", "/memory?job=resumed&file=secret.txt"):
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(http.origin + path, headers={"X-TCS-Prover-Token": http.token}))
            self.assertEqual(error.exception.code, 400)

        folder = self.original / "APPROACHES"
        folder.mkdir()
        (folder / "INDEX.md").write_text("Selected index")
        (folder / "A001.md").write_text("Selected route")
        for name, content in (("APPROACHES", "Selected index"), ("APPROACHES/A001.md", "Selected route")):
            url = http.origin + "/memory?job=resumed&file=" + name
            with self.assertRaises(HTTPError) as error:
                urlopen(url)
            self.assertEqual(error.exception.code, 403)
            with urlopen(Request(url, headers={"X-TCS-Prover-Token": http.token})) as response:
                self.assertEqual(json.load(response)["content"], content)

        graph_url = http.origin + "/approach-graph?job=resumed"
        with self.assertRaises(HTTPError) as error:
            urlopen(graph_url)
        self.assertEqual(error.exception.code, 403)
        with urlopen(Request(graph_url, headers={"X-TCS-Prover-Token": http.token})) as response:
            graph = json.load(response)
            self.assertEqual([node["id"] for node in graph["nodes"]], ["A001"])
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(http.origin + "/approach-graph?job=missing", headers={"X-TCS-Prover-Token": http.token}))
        self.assertEqual(error.exception.code, 400)

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
    def test_browser_folder_selection_refresh_and_stale_responses(self):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const selected = source.slice(source.indexOf('function revealMemoryAnchor('), source.indexOf('function upsertTimeline('));
const linkFormatter = source.slice(source.indexOf('function memoryLinkTarget('), source.indexOf('function renderMemoryContent('));
const pending = [], rendered = [], timers = [];
const markup = JSON.parse(process.argv[2]);
const element = () => ({ textContent: '', hidden: false, scrollTop: 0, attrs: {}, replaceChildren() {},
  setAttribute(key, value) {this.attrs[key] = value;}, querySelectorAll: () => [] });
const ui = Object.fromEntries(['memoryPanel', 'memoryApproachPicker', 'memoryApproach', 'memoryDocument',
  'memoryFilename', 'memoryUpdated', 'memoryContent', 'memoryMessage', 'approachGraph'].map(name => [name, markup[name] ? element() : null]));
ui.run = element();
ui.memoryPanel.open = true;
ui.memoryApproach.options = [];
ui.memoryApproach.replaceChildren = (...options) => { ui.memoryApproach.options = options; };
const tabs = Object.values(markup).filter(attrs => attrs['data-memory']).map(attrs => ({
  ...element(), dataset: {memory: attrs['data-memory']}, id: attrs.id,
}));
const approachTab = tabs.find(tab => tab.dataset.memory === 'APPROACHES');
const lemmaTab = tabs.find(tab => tab.dataset.memory === 'PROVED.md');
const context = vm.createContext({ ui, memoryTabs: tabs, currentJob: '', memoryJob: null,
  memoryFile: source.match(/let memoryFile = "([^"]+)"/)[1],
  state: {trace: []},
  memoryVersion: '', memoryRequest: 0, memoryTimer: null, memoryAnchor: '',
  approachGraphData: {nodes: [], files: [], indexFile: ''}, approachGraphSignature: '',
  document: { hidden: false, createElement: () => ({ children: [], append(child) { this.children.push(child); } }) },
  show: (element, visible) => { element.hidden = !visible; },
  managesResearchFiles: () => true,
  clearTimeout() {}, setTimeout: callback => { timers.push(callback); return timers.length; },
  jobPath: (path, values) => ({ path, ...values }),
  request: path => new Promise(resolve => pending.push({ path, resolve })),
  renderMemoryContent: content => rendered.push(content),
  appendFormattedText: (element, text) => element.children.push(text),
});
vm.runInContext(linkFormatter + selected + '\nfunction renderApproachGraph() {}', context);
const ready = (name, content, files, version = name) => ({ name, content, files, version,
  status: 'ready', modifiedAt: '2026-09-11T00:00:00Z' });
let graphFiles = ['APPROACHES/INDEX.md', 'APPROACHES/A001.md', 'APPROACHES/A002.md'];
const settleGraph = async (files = graphFiles) => {
  if (pending[0]?.path.path !== '/approach-graph') return;
  graphFiles = files;
  pending.shift().resolve({files, nodes: files.filter(name => /\/A\d/.test(name)).map(file => ({file})),
    indexFile: files.find(name => /\/(index|INDEX)\.md$/.test(name)) || ''});
  await new Promise(setImmediate);
};
const settle = async value => {
  await settleGraph(value.files || graphFiles);
  if (!pending.length) return;
  pending.shift().resolve(value); await new Promise(setImmediate);
};
(async () => {
  assert.equal(context.memoryFile, 'APPROACHES');
  context.syncMemoryPanel();
  assert.equal(ui.memoryPanel.hidden, true);
  assert.equal(pending.length, 0);
  context.currentJob = 'resumed';
  context.syncMemoryPanel();
  assert.equal(ui.memoryPanel.hidden, false);
  assert.equal(ui.memoryPanel.open, false);
  assert.equal(approachTab.attrs['aria-selected'], 'true');
  assert.equal(approachTab.tabIndex, 0);
  assert.equal(ui.memoryDocument.attrs['aria-labelledby'], approachTab.id);
  ui.memoryPanel.open = true;
  context.loadMemory();
  assert.equal(pending[0].path.path, '/approach-graph');
  const files = ['APPROACHES/INDEX.md', 'APPROACHES/A001.md', 'APPROACHES/A002.md'];
  await settleGraph(files);
  assert.equal(ui.memoryApproachPicker.hidden, false);
  assert.equal(ui.memoryApproach.value, 'APPROACHES');
  assert.equal(rendered.length, 0);
  assert.match(ui.memoryMessage.textContent, /Select an approach/);
  context.selectMemoryDocument(files[0]);
  await settle(ready(files[0], 'Index', files));
  assert.equal(rendered.at(-1), 'Index');
  context.selectMemoryDocument('APPROACHES.md');
  await settle(ready('APPROACHES.md', 'Historical work', [...files, 'APPROACHES.md']));
  assert.equal(ui.memoryApproachPicker.hidden, false);
  assert.equal(ui.memoryApproach.value, 'APPROACHES.md');
  assert.equal(ui.memoryApproach.options.at(-1).textContent, 'Historical notebook');
  const links = { children: [], append(child) { this.children.push(child); } };
  context.appendMemoryText(links, '[Route](A001.md) [Index](INDEX.md) [Archive](../APPROACHES.md) '
    + '[External](https://example.com) [Escape](../../secret.md) [Unknown](A999.md) [Initial prompt](../INITIAL_PROMPT.md)');
  const buttons = links.children.filter(child => child.className === 'memory-link');
  assert.equal(buttons.length, 4);
  assert.equal(buttons[0].children[0], 'Route');
  assert.equal(buttons[2].children[0], 'Archive');
  assert.ok(links.children.includes('[External](https://example.com)'));
  assert.ok(links.children.includes('[Escape](../../secret.md)'));
  assert.ok(links.children.includes('[Initial prompt](../INITIAL_PROMPT.md)'));
  buttons[0].onclick();
  await settleGraph();
  assert.equal(pending[0].path.file, files[1]);
  await settle(ready(files[1], 'Linked route', [...files, 'APPROACHES.md']));
  buttons[1].onclick();
  await settleGraph();
  assert.equal(pending[0].path.file, files[0]);
  await settle(ready(files[0], 'Linked index', [...files, 'APPROACHES.md']));
  context.selectMemoryDocument(files[1]);
  await settleGraph();
  assert.equal(pending[0].path.version, '');
  const slow = pending.shift();
  context.selectMemoryDocument(files[2]);
  await settle(ready(files[2], 'Route two', files));
  slow.resolve(ready(files[1], 'Stale route one', files));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(rendered.at(-1), 'Route two');
  assert.equal(ui.memoryFilename.textContent, files[2]);
  context.loadMemory();
  await settle({ ...ready(files[2], undefined, [...files, 'APPROACHES/A003.md']), unchanged: true });
  assert.equal(rendered.at(-1), 'Route two');
  assert.equal(ui.memoryApproach.options.length, 5);
  context.loadMemory();
  await settleGraph();
  const oldRoute = pending.shift();
  context.selectMemoryFile(lemmaTab);
  await settle(ready('PROVED.md', 'Lemma', undefined));
  oldRoute.resolve(ready(files[2], 'Old route', files));
  await Promise.resolve(); await Promise.resolve();
  assert.equal(ui.memoryApproachPicker.hidden, true);
  assert.equal(rendered.at(-1), 'Lemma');
  const plain = { children: [], append(child) { this.children.push(child); } };
  context.appendMemoryText(plain, '[Route](A001.md)');
  assert.equal(plain.children.join(''), '[Route](A001.md)');
  const linked = { children: [], append(child) { this.children.push(child); } };
  context.appendMemoryText(linked, '[Route](APPROACHES/A001.md)');
  linked.children.find(child => child.className === 'memory-link').onclick();
  await settleGraph();
  assert.equal(pending[0].path.file, files[1]);
  await settle(ready(files[1], 'Route one', files));
  const lemmaLink = { children: [], append(child) { this.children.push(child); } };
  context.appendMemoryText(lemmaLink, '[L095](../PROVED.md#l095)');
  lemmaLink.children.find(child => child.className === 'memory-link').onclick();
  assert.equal(pending[0].path.file, 'PROVED.md');
  const lemma = { dataset: { heading: 'L095 — Exact terminal SCCs' }, open: false, tagName: 'DETAILS', closest() {return this;},
    scrollIntoView() { this.scrolled = true; } };
  ui.memoryContent.querySelectorAll = () => [lemma];
  await settle(ready('PROVED.md', 'Lemma proof', undefined));
  assert.equal(lemma.open, true);
  assert.equal(lemma.scrolled, true);
  assert.equal(lemmaTab.tabIndex, 0);
  assert.equal(approachTab.tabIndex, -1);
  context.selectMemoryDocument('APPROACHES.md');
  await settle(ready('APPROACHES.md', 'Legacy notebook', []));
  assert.equal(ui.memoryApproachPicker.hidden, true);
  assert.equal(ui.memoryFilename.textContent, 'APPROACHES.md');
  context.loadMemory();
  await settleGraph();
  ui.memoryPanel.open = false;
  await settle(ready(files[0], 'Hidden response', files));
  assert.equal(rendered.at(-1), 'Legacy notebook');
  const count = pending.length;
  context.loadMemory();
  assert.equal(pending.length, count);

  ui.memoryPanel.open = true;
  context.selectMemoryDocument('APPROACHES');
  const oldGraph = pending.shift();
  context.selectMemoryDocument(files[1]);
  await settle(ready(files[1], 'Latest route', files.slice(1)));
  const latestGraph = context.approachGraphData;
  oldGraph.resolve({files: [], nodes: [], indexFile: ''});
  await new Promise(setImmediate);
  assert.equal(context.approachGraphData, latestGraph);
  assert.equal(rendered.at(-1), 'Latest route');
  assert.equal(pending.length, 0);

  context.loadMemory();
  const previousJobGraph = pending.shift();
  context.currentJob = 'another-job';
  context.syncMemoryPanel();
  ui.memoryPanel.open = true;
  context.loadMemory();
  await settleGraph(['APPROACHES/A010.md']);
  const anotherJobGraph = context.approachGraphData;
  previousJobGraph.resolve({files, nodes: [{file: files[1]}], indexFile: files[0]});
  await new Promise(setImmediate);
  assert.equal(context.approachGraphData, anotherJobGraph);
  assert.equal(ui.memoryApproach.options.length, 2);
  assert.equal(pending.length, 0);

  context.loadMemory();
  ui.memoryPanel.open = false;
  await settleGraph(files);
  assert.equal(context.approachGraphData, anotherJobGraph);
  assert.equal(pending.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(server.UI / "app.js"), json.dumps(PageElements().elements)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ui_bindings_and_memory_labels_match_remaining_html(self):
        elements = PageElements().elements
        source = (server.UI / "app.js").read_text()
        bindings = set(re.findall(r'\$\("([^"]+)"\)', source))
        self.assertEqual(bindings - elements.keys(), set())
        tabs = [attrs for attrs in elements.values() if "data-memory" in attrs]
        selected = [tab for tab in tabs if tab.get("aria-selected") == "true"]
        self.assertEqual([tab["data-memory"] for tab in selected], ["APPROACHES"])
        self.assertNotEqual(selected[0].get("tabindex"), "-1")
        self.assertEqual(elements["memoryDocument"]["aria-labelledby"], selected[0]["id"])
        self.assertNotIn("INITIAL_PROMPT.md", [tab["data-memory"] for tab in tabs])


if __name__ == "__main__":
    unittest.main()
