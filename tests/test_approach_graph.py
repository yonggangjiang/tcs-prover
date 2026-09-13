"""Browser graph derives navigation and dependency edges from real research files."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class ApproachGraphTests(unittest.TestCase):
    def run_browser(self, checks):
        source = Path(__file__).resolve().parents[1] / "ui" / "app.js"
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const functions = source.slice(source.indexOf('function appendFormattedText('), source.indexOf('function revealMemoryAnchor('))
  + source.slice(source.indexOf('function parseApproachGraph('), source.indexOf('function renderMemoryChoices('));
const element = tag => ({tagName: tag.toUpperCase(), children: [], attributes: {}, dataset: {}, style: {}, textContent: '', scrollTop: 0,
  append(...children) {this.children.push(...children);}, replaceChildren(...children) {this.children = children;},
  setAttribute(name, value) {this.attributes[name] = String(value);}, focus() {this.focused = true;}, scrollIntoView() {},
  querySelectorAll(tag) {return this.children.filter(child => child.tagName === tag.toUpperCase());},
});
const ui = Object.fromEntries(['approachGraphCanvas', 'approachGraphSummary', 'approachGraphWarning',
  'approachGraphInspector', 'memoryDocument', 'memoryFilename', 'memoryContent'].map(name => [name, element('div')]));
const selections = [];
const context = vm.createContext({assert, ui, selections,
  memoryFile: 'APPROACHES', approachIndex: {content: '', version: '', files: []}, approachGraphSignature: '',
  show: (element, visible) => {element.hidden = !visible;},
  document: {createElement: element, createElementNS: (_, tag) => element(tag),
    createDocumentFragment: () => element('fragment'), createTextNode: text => ({textContent: text})},
  selectMemoryDocument: name => selections.push(name),
});
vm.runInContext(functions + process.argv[2], context);
"""
        result = subprocess.run([shutil.which("node"), "-e", script, str(source), checks],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_lemma_anchor_markers_and_rules_render_without_accepting_html(self):
        self.run_browser(r"""
memoryFile = 'PROVED.md';
renderMemoryContent('# Proved lemmas\n\n<a id="l001"></a>\n## L001 — Base case\n\nLemma text.\n\n---\n\n<script>alert(1)</script>\n\n<a id="bad" onclick="alert(1)"></a>');
const descendants = element => [element, ...(element.children || []).flatMap(descendants)];
const all = descendants(ui.memoryContent);
assert.equal(all.filter(element => element.tagName === 'HR').length, 1);
assert.equal(all.some(element => element.textContent === '<a id="l001"></a>'), false);
assert.equal(all.some(element => element.textContent === '<script>alert(1)</script>'), true);
assert.equal(all.some(element => element.textContent === '<a id="bad" onclick="alert(1)"></a>'), true);
assert.equal(all.some(element => element.tagName === 'SCRIPT' || element.tagName === 'A'), false);
assert.equal(all.some(element => element.id === 'memory-l001-base-case'), true);
""")

    def test_parent_table_defines_edges_and_only_real_files_become_nodes(self):
        self.run_browser(r"""
const files = ['APPROACHES/index.md', 'APPROACHES/A001-root.md', 'APPROACHES/A002-independent.md',
  'APPROACHES/A003-child.md', 'APPROACHES/A004-unlisted.md', 'APPROACHES.md'];
const table = `# Attempted approaches
| ID and title | Parent IDs | Children | Status | Result or remaining question |
| --- | --- | --- | --- | --- |
| [A001 — Root lemma](A001-root.md) | none | A002, A003 | RESOLVED | A reusable base case. |
| [A002 — Independent](A002-independent.md) | none | A003 | PARKED | Need a bound. |
| [A003 — Combine both](A003-child.md) | [A001](A001-root.md), [A002](A002-independent.md) | none | ACTIVE | Can the cost be \\|S\\|? |
| [A099 — Future idea](A099-missing.md) | A003 | none | UNTRIED | Not investigated. |`;
const graph = parseApproachGraph(table, files);
assert.equal(graph.nodes.length, 4);
assert.equal(graph.edges.length, 2);
assert.equal(JSON.stringify(graph.edges), JSON.stringify([{from:'A001',to:'A003'}, {from:'A002',to:'A003'}]));
assert.equal(graph.nodes.find(node => node.id === 'A003').title, 'Combine both');
assert.equal(graph.nodes.find(node => node.id === 'A003').depth, 1);
assert.equal(graph.nodes.find(node => node.id === 'A002').depth, 0);
assert.equal(graph.nodes.find(node => node.id === 'A003').result, 'Can the cost be |S|?');
assert.match(graph.warnings.join(' '), /1 file is not linked/);
assert.equal(graph.nodes.some(node => node.id === 'A099'), false);
""")

    def test_cycles_and_missing_parents_are_visible_without_invented_nodes(self):
        self.run_browser(r"""
const table = `| Node | Parents | Status | Result |
| --- | --- | --- | --- |
| [A001](A001.md) | A002, A009 | PARKED | Investigate. |
| [A002](A002.md) | A001 | PARKED | Investigate. |`;
const graph = parseApproachGraph(table, ['APPROACHES/A001.md', 'APPROACHES/A002.md']);
assert.equal(graph.nodes.length, 2);
assert.equal(graph.edges.length, 2);
assert.match(graph.warnings.join(' '), /cycle/);
assert.match(graph.warnings.join(' '), /A009/);
assert.equal(graph.nodes.every(node => Number.isFinite(node.depth)), true);
assert.equal(memoryLinkTarget('javascript:alert(1)'), null);
assert.equal(memoryLinkTarget('../../PROVED.md', 'APPROACHES/A001.md'), null);
assert.equal(memoryLinkTarget('../audit_history/old-report.md', 'APPROACHES/A001.md').name, 'audit_history/old-report.md');
assert.equal(memoryLinkTarget('../PROVED.md#l001', 'APPROACHES/A001.md').anchor, 'l001');
""")

    def test_graph_buttons_open_files_and_reveal_hover_and_keyboard_details(self):
        self.run_browser(r"""
approachIndex = {files: ['APPROACHES/A001.md', 'APPROACHES/A002.md'], content: `| Node | Parents | Status | Result |
| --- | --- | --- | --- |
| [A001 — Root](A001.md) | none | RESOLVED | Proven base. |
| [A002 — <script>safe text</script>](A002.md) | A001 | ACTIVE | Remaining question. |`};
renderApproachGraph();
const buttons = ui.approachGraphCanvas.querySelectorAll('button');
assert.equal(buttons.length, 2);
assert.match(buttons[1].title, /Parents: A001/);
assert.equal(buttons[1].children[1].textContent, '<script>safe text</script>');
buttons[1].onmouseenter();
assert.equal(ui.approachGraphInspector.children[1].textContent, 'Remaining question.');
buttons[0].onfocus();
assert.equal(ui.approachGraphInspector.children[1].textContent, 'Proven base.');
buttons[1].onclick();
assert.equal(selections[0], 'APPROACHES/A002.md');
assert.equal(ui.memoryDocument.focused, true);
memoryFile = 'APPROACHES/A002.md'; renderApproachGraph();
assert.equal(buttons[1].attributes['aria-current'], 'true');
assert.equal(buttons[0].attributes['aria-current'], 'false');
""")


if __name__ == "__main__":
    unittest.main()
