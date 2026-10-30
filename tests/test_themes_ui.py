"""The appearance choices: four themes (Blue, Brown, Crimson, Ink), each in a light and a dark
mode, picked in the header's theme menu with a Light / Dark / System switch.

The browser checks cut ui/app.js into slices by marker text and run them in Node's vm with a
small fake DOM, as the other UI tests do: the bootstrap at the top of app.js (it sets
<html data-theme data-mode> before the first paint) and the theme menu near the end. The static
checks read ui/index.html and ui/styles.css.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "ui" / "app.js"
INDEX = ROOT / "ui" / "index.html"
STYLES = ROOT / "ui" / "styles.css"
THEMES = ("blue", "brown", "crimson", "ink")
MODES = ("light", "dark", "system")

BOOT = ["// Appearance, before the first paint", "// Keep the secret launch token in this tab"]
MENU = ["// Appearance: the dark theme", "// The workbench shell around the pages"]

RUNNER = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const {slices, setup, checks} = JSON.parse(fs.readFileSync(0, 'utf8'));
const code = slices.map(([from, to]) => {
  const start = source.indexOf(from), end = source.indexOf(to, start + 1);
  if (start < 0 || end < 0) throw new Error('Marker not found: ' + (start < 0 ? from : to));
  return source.slice(start, end);
}).join('\n');
let finished = false;
const context = {assert, console, URLSearchParams, finish: () => { finished = true; }};
process.on('exit', () => {
  if (!finished && !process.exitCode) { console.error('The checks did not finish.'); process.exitCode = 1; }
});
Promise.resolve(vm.runInNewContext(`${setup}\n${code}\n(async () => {\n${checks}\nfinish();\n})()`, context))
  .catch((error) => { console.error(error); process.exitCode = 1; });
"""

# A browser stand-in: <html> with its dataset, the theme-color metas, localStorage (which can be
# made to throw), location.search and a prefers-color-scheme query whose answer can change.
ENVIRONMENT = r"""
const root = {dataset: {}};
const metas = [{name: 'theme-color', content: '', setAttribute(key, value) { this[key] = value; }},
               {name: 'theme-color', content: '', setAttribute(key, value) { this[key] = value; }}];
const store = new Map(Object.entries(fixture.storage || {}));
const writes = [];
const localStorage = {
  getItem(key) { if (fixture.blocked) throw new Error('blocked'); return store.has(key) ? store.get(key) : null; },
  setItem(key, value) { if (fixture.blocked) throw new Error('blocked'); writes.push([key, String(value)]); store.set(key, String(value)); },
};
const location = {search: fixture.search || ''};
const query = {
  matches: Boolean(fixture.systemDark), listeners: [],
  addEventListener(type, listener) { if (type === 'change') this.listeners.push(listener); },
  set(dark) { this.matches = dark; for (const listener of this.listeners) listener({matches: dark}); },
};
const window = {matchMedia: (text) => { assert.equal(text, '(prefers-color-scheme: dark)'); return query; }};
const document = {
  documentElement: root,
  querySelectorAll(selector) { assert.equal(selector, 'meta[name="theme-color"]'); return metas; },
  getElementById: (id) => elements[id] || null,
};
const elements = {};
"""

# The theme menu as index.html writes it: the toggle, and two radio groups in the popover.
MENU_DOM = r"""
class Node {
  constructor(attrs = {}) {
    this.dataset = {...(attrs.dataset || {})}; this.attributes = {}; this.hidden = Boolean(attrs.hidden);
    this.tabIndex = 0; this.children = []; this.parent = null; this.title = ''; this.listeners = {};
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  focus() { document.activeElement = this; }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  contains(node) { for (let at = node; at; at = at.parent) if (at === this) return true; return false; }
  closest(selector) {
    for (let at = this; at; at = at.parent) {
      if (selector.split(',').some((part) => at.matches(part.trim()))) return at;
    }
    return null;
  }
  matches(selector) {
    if (selector === '.theme-menu') return this.isMenuWrapper === true;
    const match = /^\[data-([a-z-]+)\]$/.exec(selector);
    if (!match) return false;
    const key = match[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return this.dataset[key] !== undefined;
  }
  querySelectorAll(selector) {
    const all = [];
    const walk = (node) => { for (const child of node.children) { if (child.matches(selector)) all.push(child); walk(child); } };
    walk(this);
    return all;
  }
  addEventListener(type, listener) { this.listeners[type] = listener; }
}
const wrapper = new Node(); wrapper.isMenuWrapper = true;
const button = new Node();
const menu = new Node({hidden: true});
wrapper.append(button, menu);
const cards = ['blue', 'brown', 'crimson', 'ink'].map((value) => new Node({dataset: {themeChoice: value}}));
const modeButtons = ['light', 'dark', 'system'].map((value) => new Node({dataset: {modeChoice: value}}));
const cardGroup = new Node(), modeGroup = new Node();
cardGroup.append(...cards); modeGroup.append(...modeButtons); menu.append(cardGroup, modeGroup);
const outside = new Node();
elements.themeToggle = button; elements.themeMenu = menu;
const documentListeners = {};
document.addEventListener = (type, listener) => { documentListeners[type] = listener; };
document.activeElement = null;
const card = (value) => cards.find((item) => item.dataset.themeChoice === value);
const modeButton = (value) => modeButtons.find((item) => item.dataset.modeChoice === value);
const checked = (items) => items.filter((item) => item.getAttribute('aria-checked') === 'true').map((item) => item.dataset.themeChoice || item.dataset.modeChoice);
const key = (name) => { let prevented = false; menu.onkeydown({key: name, preventDefault: () => { prevented = true; }}); return prevented; };
"""


def parse_html():
    nodes = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag, attrs):
            nodes.append((tag, dict(attrs)))

    Collector().feed(INDEX.read_text(encoding="utf-8"))
    return nodes


def css_blocks():
    """Selector text -> {property: value} for every top-level and @media-nested rule."""
    css = re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)
    blocks = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(match.group(1).split())
        declarations = {}
        for item in match.group(2).split(";"):
            if ":" in item:
                name, value = item.split(":", 1)
                declarations[name.strip()] = " ".join(value.split())
        blocks.append((selector, declarations))
    return blocks


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser logic tests")
class AppearanceBrowserTests(unittest.TestCase):
    def run_app(self, slices, fixture, checks, extra_setup=""):
        setup = f"const fixture = {json.dumps(fixture)};\n" + ENVIRONMENT + extra_setup
        payload = json.dumps({"slices": slices, "setup": setup, "checks": checks})
        result = subprocess.run([shutil.which("node"), "-e", RUNNER, str(APP)], input=payload,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_markers_occur_once(self):
        source = APP.read_text(encoding="utf-8")
        for marker in BOOT + MENU:
            with self.subTest(marker=marker):
                self.assertEqual(source.count(marker), 1)
        # The bootstrap runs first: right after the strict-mode directive, before any other code.
        self.assertTrue(source.startswith('"use strict";\n\n' + BOOT[0]))

    def test_defaults_follow_the_system_with_the_blue_theme(self):
        for dark, mode, color in ((False, "light", "#f3f5f8"), (True, "dark", "#0c0e11")):
            with self.subTest(system_dark=dark):
                self.run_app([BOOT], {"systemDark": dark}, f"""
assert.equal(root.dataset.theme, 'blue');
assert.equal(root.dataset.mode, '{mode}');
assert.deepEqual(metas.map((meta) => meta.content), ['{color}', '{color}']);
assert.deepEqual(appearance.current(), {{theme: 'blue', mode: 'system'}});
assert.deepEqual(writes, []);  // nothing is saved until a choice is made
""")

    def test_saved_theme_and_mode_are_applied(self):
        canvas = {("blue", "light"): "#f3f5f8", ("blue", "dark"): "#0c0e11",
                  ("brown", "light"): "#f6f6f3", ("brown", "dark"): "#131312",
                  ("crimson", "light"): "#fcf8f7", ("crimson", "dark"): "#170d13",
                  ("ink", "light"): "#fafaf9", ("ink", "dark"): "#151515"}
        for (theme, mode), color in canvas.items():
            with self.subTest(theme=theme, mode=mode):
                # The saved mode wins over the system setting.
                self.run_app([BOOT], {"systemDark": mode == "light",
                                      "storage": {"tcs-prover-theme": theme, "tcs-prover-mode": mode}}, f"""
assert.equal(root.dataset.theme, '{theme}');
assert.equal(root.dataset.mode, '{mode}');
assert.equal(metas[0].content, '{color}');
query.set(!query.matches);  // an OS change does not move an explicit mode
assert.equal(root.dataset.mode, '{mode}');
""")

    def test_system_mode_follows_the_os_live(self):
        self.run_app([BOOT], {"systemDark": False, "storage": {"tcs-prover-theme": "crimson", "tcs-prover-mode": "system"}}, """
assert.equal(root.dataset.mode, 'light');
query.set(true);
assert.equal(root.dataset.mode, 'dark');
assert.equal(root.dataset.theme, 'crimson');
assert.equal(metas[1].content, '#170d13');
query.set(false);
assert.equal(root.dataset.mode, 'light');
assert.equal(metas[0].content, '#fcf8f7');
""")

    def test_earlier_light_or_dark_choices_become_the_mode(self):
        for saved, mode in (("light", "light"), ("dark", "dark"), ("system", "system")):
            with self.subTest(saved=saved):
                self.run_app([BOOT], {"systemDark": True, "storage": {"tcs-prover-theme": saved}}, f"""
assert.deepEqual(appearance.current(), {{theme: 'blue', mode: '{mode}'}});
assert.equal(root.dataset.mode, '{"dark" if mode != "light" else "light"}');
appearance.choose('theme', 'ink');
// The next save writes both parts in the new form.
assert.equal(store.get('tcs-prover-theme'), 'ink');
assert.equal(store.get('tcs-prover-mode'), '{mode}');
""")

    def test_unknown_values_and_blocked_storage_fall_back_to_the_defaults(self):
        self.run_app([BOOT], {"storage": {"tcs-prover-theme": "neon", "tcs-prover-mode": "dim"}}, """
assert.deepEqual(appearance.current(), {theme: 'blue', mode: 'system'});
assert.equal(root.dataset.mode, 'light');
""")
        self.run_app([BOOT], {"blocked": True, "systemDark": True}, """
assert.equal(root.dataset.theme, 'blue');
assert.equal(root.dataset.mode, 'dark');
appearance.choose('theme', 'brown');  // applies for this page even though nothing can be saved
assert.equal(root.dataset.theme, 'brown');
appearance.choose('mode', 'light');
assert.equal(root.dataset.mode, 'light');
assert.equal(metas[0].content, '#f6f6f3');
""")

    def test_a_choice_is_saved_and_repaints(self):
        self.run_app([BOOT], {"systemDark": False}, """
let painted = 0;
appearance.onChange(() => { painted += 1; });
appearance.choose('theme', 'crimson');
assert.equal(root.dataset.theme, 'crimson');
assert.deepEqual(writes.slice(-2), [['tcs-prover-theme', 'crimson'], ['tcs-prover-mode', 'system']]);
appearance.choose('mode', 'dark');
assert.equal(root.dataset.mode, 'dark');
assert.equal(store.get('tcs-prover-mode'), 'dark');
assert.equal(painted, 2);
// Unknown values are ignored.
appearance.choose('theme', 'neon');
appearance.choose('mode', 'sepia');
appearance.choose('palette', 'ink');
appearance.choose('palette', 'dark');
appearance.choose('theme', 'dark');
assert.deepEqual(appearance.current(), {theme: 'crimson', mode: 'dark'});
assert.equal(painted, 2);
""")

    def test_theme_query_previews_without_saving(self):
        cases = {"?theme=ink-dark": ("ink", "dark"), "?theme=dark": ("crimson", "dark"),
                 "?theme=light&new=1": ("crimson", "light"), "?job=x&theme=Brown": ("brown", "light"),
                 "?theme=blue_system": ("blue", "light"), "?theme=neon": ("crimson", "light"),
                 "?theme=": ("crimson", "light")}
        for search, (theme, mode) in cases.items():
            with self.subTest(search=search):
                self.run_app([BOOT], {"search": search, "systemDark": False,
                                      "storage": {"tcs-prover-theme": "crimson", "tcs-prover-mode": "light"}}, f"""
assert.equal(root.dataset.theme, '{theme}');
assert.equal(root.dataset.mode, '{mode}');
assert.deepEqual(writes, []);
""")
        # Choosing in the menu replaces the preview of that part only, and saves.
        self.run_app([BOOT], {"search": "?theme=ink-dark", "storage": {"tcs-prover-theme": "crimson"}}, """
appearance.choose('mode', 'light');
assert.deepEqual([root.dataset.theme, root.dataset.mode], ['ink', 'light']);
assert.equal(store.get('tcs-prover-theme'), 'crimson');
assert.equal(store.get('tcs-prover-mode'), 'light');
appearance.choose('theme', 'blue');
assert.deepEqual([root.dataset.theme, root.dataset.mode], ['blue', 'light']);
""")

    def test_menu_shows_both_choices_and_applies_clicks(self):
        self.run_app([BOOT, MENU], {"systemDark": True, "storage": {"tcs-prover-theme": "brown"}}, """
assert.deepEqual(checked(cards), ['brown']);
assert.deepEqual(checked(modeButtons), ['system']);
assert.deepEqual(cards.map((item) => item.tabIndex), [-1, 0, -1, -1]);
assert.equal(button.getAttribute('aria-label'), 'Appearance: Brown, System (Dark)');
assert.equal(button.dataset.appearanceMode, 'system');
// Opening moves focus to the chosen theme.
button.onclick();
assert.equal(menu.hidden, false);
assert.equal(button.getAttribute('aria-expanded'), 'true');
assert.equal(document.activeElement, card('brown'));
// A click on a card or a mode applies and saves it; the menu stays open for the other choice.
menu.onclick({target: card('crimson')});
assert.equal(root.dataset.theme, 'crimson');
assert.deepEqual(checked(cards), ['crimson']);
assert.equal(menu.hidden, false);
menu.onclick({target: modeButton('light')});
assert.equal(root.dataset.mode, 'light');
assert.deepEqual(checked(modeButtons), ['light']);
assert.equal(button.getAttribute('aria-label'), 'Appearance: Crimson, Light');
assert.equal(button.dataset.appearanceMode, 'light');
assert.equal(store.get('tcs-prover-theme'), 'crimson');
assert.equal(store.get('tcs-prover-mode'), 'light');
// A click elsewhere in the popover changes nothing.
menu.onclick({target: menu});
assert.deepEqual(appearance.current(), {theme: 'crimson', mode: 'light'});
""", MENU_DOM)

    def test_menu_keyboard_moves_the_choice_within_each_group(self):
        self.run_app([BOOT, MENU], {"systemDark": False}, """
button.onclick();
assert.equal(document.activeElement, card('blue'));
assert.equal(key('ArrowRight'), true);
assert.equal(root.dataset.theme, 'brown');
assert.equal(document.activeElement, card('brown'));
key('End');
assert.equal(root.dataset.theme, 'ink');
key('ArrowDown');  // wraps within the group
assert.equal(root.dataset.theme, 'blue');
key('ArrowLeft');
assert.equal(root.dataset.theme, 'ink');
key('Home');
assert.equal(root.dataset.theme, 'blue');
// The mode group moves on its own.
modeButton('system').focus();
key('ArrowLeft');
assert.deepEqual([root.dataset.theme, root.dataset.mode], ['blue', 'dark']);
assert.deepEqual(checked(modeButtons), ['dark']);
assert.deepEqual(modeButtons.map((item) => item.tabIndex), [-1, 0, -1]);
// Other keys are left to the browser (Enter and Space click the focused button).
assert.equal(key('Enter'), false);
// Escape closes and returns focus to the toggle.
assert.equal(key('Escape'), true);
assert.equal(menu.hidden, true);
assert.equal(button.getAttribute('aria-expanded'), 'false');
assert.equal(document.activeElement, button);
""", MENU_DOM)

    def test_menu_closes_on_an_outside_click_or_when_focus_leaves(self):
        self.run_app([BOOT, MENU], {}, """
button.onclick();
documentListeners.pointerdown({target: card('ink')});
assert.equal(menu.hidden, false);
documentListeners.pointerdown({target: outside});
assert.equal(menu.hidden, true);
button.onclick();
menu.listeners.focusout({relatedTarget: modeButton('dark')});
assert.equal(menu.hidden, false);
menu.listeners.focusout({relatedTarget: outside});
assert.equal(menu.hidden, true);
button.onclick();
button.onclick();  // the toggle closes it again
assert.equal(menu.hidden, true);
// Escape on the toggle itself (focus went back to it) closes an open menu too.
button.onclick();
let stopped = false;
button.onkeydown({key: 'Escape', preventDefault: () => { stopped = true; }});
assert.equal(menu.hidden, true);
assert.equal(stopped, true);
// The menu follows a change made elsewhere (an OS switch while System is chosen).
query.set(true);
assert.equal(button.getAttribute('aria-label'), 'Appearance: Blue, System (Dark)');
""", MENU_DOM)


class AppearanceMarkupTests(unittest.TestCase):
    def test_theme_menu_offers_four_themes_and_three_modes(self):
        nodes = parse_html()
        toggle = next(attrs for tag, attrs in nodes if attrs.get("id") == "themeToggle")
        self.assertEqual(toggle.get("aria-controls"), "themeMenu")
        self.assertEqual(toggle.get("aria-expanded"), "false")
        menu = next(attrs for tag, attrs in nodes if attrs.get("id") == "themeMenu")
        self.assertIn("hidden", menu)
        cards = [attrs for tag, attrs in nodes if "data-theme-choice" in attrs]
        modes = [attrs for tag, attrs in nodes if "data-mode-choice" in attrs]
        self.assertEqual([card["data-theme-choice"] for card in cards], list(THEMES))
        self.assertEqual([mode["data-mode-choice"] for mode in modes], list(MODES))
        for item in cards + modes:
            self.assertEqual((item.get("role"), item.get("type")), ("radio", "button"))
        groups = [attrs.get("aria-labelledby") for tag, attrs in nodes if attrs.get("role") == "radiogroup"]
        self.assertIn("themeCardsLabel", groups)
        self.assertIn("themeModeLabel", groups)
        # Each card's preview is drawn from that theme's own tokens.
        swatches = [attrs["data-swatch"] for tag, attrs in nodes if "data-swatch" in attrs]
        self.assertEqual(swatches, list(THEMES))
        # The browser chrome starts at the default theme's canvas for both system settings.
        metas = [attrs for tag, attrs in nodes if tag == "meta" and attrs.get("name") == "theme-color"]
        self.assertEqual({(meta["media"], meta["content"]) for meta in metas},
                         {("(prefers-color-scheme: light)", "#f3f5f8"), ("(prefers-color-scheme: dark)", "#0c0e11")})
        scheme = next(attrs for tag, attrs in nodes if tag == "meta" and attrs.get("name") == "color-scheme")
        self.assertEqual(scheme["content"], "light dark")
        # No inline style or script: the CSP (default-src 'self') would block them.
        self.assertFalse([attrs for tag, attrs in nodes if "style" in attrs])
        self.assertFalse([attrs for tag, attrs in nodes if tag == "script" and "src" not in attrs])


class AppearanceStylesheetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks = css_blocks()

    def tokens(self, selector):
        """The colour set whose selector list starts with `selector` (it declares color-scheme)."""
        found = [declarations for sel, declarations in self.blocks
                 if sel.split(",")[0].strip() == selector and "color-scheme" in declarations]
        self.assertEqual(len(found), 1, selector)
        return dict(found[0])

    def test_every_theme_and_mode_defines_the_same_colour_tokens(self):
        sets = {("blue", "light"): self.tokens(":root")}
        for theme in THEMES:
            if theme != "blue":
                sets[theme, "light"] = self.tokens(f':root[data-theme="{theme}"]')
            sets[theme, "dark"] = self.tokens(f':root[data-theme="{theme}"][data-mode="dark"]')
        # :root also carries the shared tokens; compare the colour names of the other sets.
        names = set(sets["brown", "light"])
        self.assertGreater(len(names), 60)
        for key, values in sets.items():
            with self.subTest(set=key):
                self.assertLessEqual(names, set(values))
                self.assertEqual(values["color-scheme"], key[1])
        # The bootstrap's chrome colours are the canvases of the stylesheet.
        source = APP.read_text(encoding="utf-8")
        boot = source[source.index(BOOT[0]):source.index(BOOT[1])]
        for (theme, mode), values in sets.items():
            with self.subTest(chrome=(theme, mode)):
                self.assertRegex(boot, rf'{theme}: \{{ light: "#[0-9a-f]{{6}}", dark: "#[0-9a-f]{{6}}" \}}')
                pair = re.search(rf'{theme}: \{{ light: "(#[0-9a-f]{{6}})", dark: "(#[0-9a-f]{{6}})" \}}', boot)
                self.assertEqual(pair.group(1 if mode == "light" else 2), values["--canvas"])

    def test_the_page_follows_the_system_before_app_js_runs(self):
        css = STYLES.read_text(encoding="utf-8")
        start = css.index("@media (prefers-color-scheme: dark)")
        fallback = css[start:css.index("\n}", start)]
        self.assertIn(":root:not([data-mode])", fallback)
        self.assertIn("color-scheme: dark", fallback)
        dark = self.tokens(':root[data-theme="blue"][data-mode="dark"]')
        for name in ("--canvas", "--surface", "--ink", "--accent"):
            self.assertIn(f"{name}: {dark[name]};", fallback)

    def rule(self, selector):
        found = [declarations for sel, declarations in self.blocks if sel == selector]
        self.assertTrue(found, selector)
        return found[-1]

    def test_live_work_is_teal_in_crimson_and_the_accent_elsewhere(self):
        # Crimson's accent is red, so its running state (the working step, the running count) takes
        # --running (teal); the other themes keep the accent, which is also their running colour.
        for theme in THEMES:
            with self.subTest(theme=theme):
                selector = ":root" if theme == "blue" else f':root[data-theme="{theme}"]'
                found = [d for sel, d in self.blocks if sel.split(",")[0].strip() == selector and "--live" in d]
                self.assertEqual(len(found), 1)
                source = "running" if theme == "crimson" else "accent"
                self.assertEqual(found[0]["--live"], f"var(--{source})")
                self.assertEqual(found[0]["--live-rgb"], f"var(--{source}-rgb)")
        for selector in (".workflow-node.active .node-dot", ".legend-dot.active"):
            with self.subTest(selector=selector):
                declarations = self.rule(selector)
                self.assertEqual(declarations["background"], "var(--live)")
                self.assertIn("var(--live-rgb)", declarations["box-shadow"])
        self.assertEqual(self.rule(".nav-badge-live")["color"], "var(--live-text)")

    def test_the_rail_badge_stays_filled_on_the_current_item(self):
        # The badge rule of a hovered or current item (.nav-item:is(:hover, [aria-current])
        # .nav-badge-live, three classes) must not repaint the rail's filled badge: the rail rule
        # carries four.
        current = self.rule(".nav-item:is(:hover, [aria-current]) .nav-badge-live")
        self.assertEqual(current["background"], "var(--surface)")
        rail = self.rule(':root[data-sidebar="rail"] .nav-item .nav-badge-live')
        self.assertEqual((rail["color"], rail["background"]), ("var(--on-live)", "var(--live)"))

    def test_chosen_segments_are_edged_at_three_to_one(self):
        for selector in ('.mode-choice[aria-checked="true"], .mode-choice[aria-checked="true"]:hover',
                         '.seg[aria-pressed="true"]', ".filter.active"):
            with self.subTest(selector=selector):
                self.assertEqual(self.rule(selector)["border-color"], "var(--field-line)")
        self.assertIn("var(--field-line)", self.rule(".segment input:checked + span")["box-shadow"])

    def test_conversation_and_related_works_use_theme_tokens_only(self):
        # The author conversation and the related-works lists are drawn from the theme tokens alone, so
        # each of the four themes styles them in light and dark (forced colours use system colours).
        new = (".author-conversation", ".conversation-", ".steer-buttons", ".limit-hint", ".related-work",
               ".related-note", ".batch-related")
        checked = 0
        for selector, declarations in self.blocks:
            if not any(name in selector for name in new):
                continue
            for name, value in declarations.items():
                if name in ("color", "background", "box-shadow", "outline") or name.startswith("border"):
                    checked += 1
                    with self.subTest(selector=selector, property=name):
                        self.assertNotRegex(value, r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?)\((?!var\()")
                        self.assertNotRegex(value, r"\b(?:white|black|red|green|blue|gray|grey|silver|orange)\b")
        self.assertGreater(checked, 15)

    def test_theme_cards_preview_each_theme_in_the_current_mode(self):
        selectors = [sel for sel, _ in self.blocks]
        for theme in THEMES:
            with self.subTest(theme=theme):
                self.assertTrue(any(f'.theme-swatch[data-swatch="{theme}"]' in sel and "[data-mode" not in sel
                                    for sel in selectors))
                self.assertTrue(any(f'[data-mode="dark"] .theme-swatch[data-swatch="{theme}"]' in sel
                                    for sel in selectors))

    def test_a_long_stage_wraps_inside_its_pill_on_a_phone(self):
        # At 320 px "Independent critic · paused · cycle 1 · round 2" is wider than the page.
        css = re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)
        phone = {}
        for match in re.finditer(r"@media \(max-width: 599px\) \{", css):
            depth, end = 1, match.end()
            while depth:
                depth += {"{": 1, "}": -1}.get(css[end], 0)
                end += 1
            for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css[match.end():end - 1]):
                phone[" ".join(rule.group(1).split())] = {
                    name.strip(): " ".join(value.split())
                    for name, value in (item.split(":", 1) for item in rule.group(2).split(";") if ":" in item)}
        pill = phone[".run-facts dd.status-pill"]
        self.assertEqual((pill["white-space"], pill["max-width"]), ("normal", "100%"))
        self.assertEqual(phone[".run-facts > .fact-stage"]["max-width"], "100%")
        # Wider pages keep it on one line.
        self.assertEqual(self.rule(".batch-progress span, .job-status, .status-pill")["white-space"], "nowrap")


if __name__ == "__main__":
    unittest.main()
