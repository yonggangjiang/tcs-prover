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
  'approachGraphInspector', 'approachIndex', 'memoryDocument', 'memoryFilename', 'memoryContent'].map(name => [name, element('div')]));
const selections = [];
const context = vm.createContext({assert, ui, selections,
  memoryFile: 'APPROACHES', approachGraphData: {nodes: [], files: [], indexFile: ''}, approachGraphSignature: '',
  show: (element, visible) => {element.hidden = !visible;},
  document: {createElement: element, createElementNS: (_, tag) => element(tag),
    createDocumentFragment: () => element('fragment'), createTextNode: text => ({textContent: text})},
  selectMemoryDocument: name => selections.push(name),
});
vm.runInContext(functions + `
function node(id, parents = [], extra = {}) {
  return {id, file: 'APPROACHES/' + id + '.md', title: id, parents,
    status: 'ACTIVE', hasParents: true, readStatus: 'ready', result: 'Saved work.', ...extra};
}
` + process.argv[2], context);
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

    def test_node_records_define_edges_and_do_not_need_an_index(self):
        self.run_browser(r"""
const graph = parseApproachGraph([
  node('A001', [], {title: 'Root lemma', status: 'RESOLVED'}),
  node('A002', [], {title: 'Independent', status: 'BLOCKED'}),
  node('A003', ['A001', 'A002', 'A001'], {title: 'Combine both', result: 'Can the cost be |S|?'}),
  node('A004', ['A003']),
]);
assert.equal(graph.nodes.length, 4);
assert.equal(JSON.stringify(graph.edges), JSON.stringify([{from:'A001',to:'A003'}, {from:'A002',to:'A003'}, {from:'A003',to:'A004'}]));
assert.equal(graph.nodes.find(node => node.id === 'A003').title, 'Combine both');
assert.equal(graph.nodes.find(node => node.id === 'A003').depth, 1);
assert.equal(graph.nodes.find(node => node.id === 'A002').depth, 0);
assert.equal(graph.nodes.find(node => node.id === 'A003').result, 'Can the cost be |S|?');
assert.equal(graph.nodes.find(node => node.id === 'A004').depth, 2);
assert.equal(graph.warnings.length, 0);
assert.equal(JSON.stringify(graph.nodes.find(node => node.id === 'A003').children), '["A004"]');
""")

    def test_cycles_and_missing_parents_are_visible_without_invented_nodes(self):
        self.run_browser(r"""
const graph = parseApproachGraph([node('A001', ['A002', 'A009']), node('A002', ['A001'])]);
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
approachGraphData = {nodes: [node('A001', [], {title: 'Root', result: 'Proven base.'}),
  node('A002', ['A001'], {title: '<script>safe text</script>', result: 'Remaining question.'})], files: [], indexFile: ''};
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
assert.equal(ui.approachIndex.hidden, true);
approachGraphData.nodes[1].parents = [];
approachGraphData.nodes[1].status = 'BLOCKED';
renderApproachGraph();
const updated = ui.approachGraphCanvas.querySelectorAll('button')[1];
assert.match(updated.title, /Parents: none/);
assert.match(updated.className, /blocked/);
""")

    def test_incomplete_and_duplicate_nodes_warn_without_guessing_connections(self):
        self.run_browser(r"""
const graph = parseApproachGraph([
  node('A001'), node('A001', [], {file: 'APPROACHES/A001-copy.md'}),
  node('A002', ['A001']), node('A003', [], {hasParents: false, status: 'UNSPECIFIED'}),
  node('A004', [], {readStatus: 'unavailable', hasParents: false}),
]);
assert.equal(graph.nodes.length, 5);
assert.equal(graph.edges.length, 0);
assert.match(graph.warnings.join(' '), /ambiguous/);
assert.match(graph.warnings.join(' '), /A003: Parents field is missing/);
assert.match(graph.warnings.join(' '), /A004: file temporarily unavailable/);
assert.equal(graph.warnings.some(text => text.includes('cycle')), false);
""")


if __name__ == "__main__":
    unittest.main()
