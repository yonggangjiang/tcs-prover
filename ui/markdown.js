/* TCS Prover: Markdown with LaTeX math, rendered for reading.

   A plain browser script (no modules, no build step). It needs, loaded earlier by <script> tags:
     /vendor/markdown-it/markdown-it.min.js   (window.markdownit)
     /vendor/katex/katex.min.js               (window.katex)
   and the stylesheets /vendor/katex/katex.min.css and /markdown.css.

   window.TCSMarkdown.render(text, element[, options]) replaces element's content with the rendered document
   (synchronously) and returns a summary { blocks, reused, formulas, rendered, errors, milliseconds }.
   options.math === false leaves the TeX as text. TCSMarkdown.clear(element) empties the element.

   The text is written by a language model, so it is untrusted, and the page's Content-Security-Policy
   (default-src 'self') forbids inline scripts and style attributes in markup:
   - markdown-it runs with html: false (raw HTML is shown as text) and linkify: false; only http, https and
     mailto links become links (target=_blank, rel=noopener noreferrer); images are never loaded (the alt
     text and the address are shown as text); table alignment is a class, never a style attribute.
   - Math (\( \), \[ \], $ $, $$ $$) becomes placeholder elements carrying the TeX in data-tex (and showing it as
     text); after the HTML is inserted, katex.render() fills each one through the DOM, so KaTeX's sizes and
     colours are set through the CSSOM, which the policy allows. KaTeX's renderToString output (style
     attributes) is never inserted as HTML.
   - KaTeX runs with trust: false, strict: "ignore", maxExpand: 1000, maxSize: 50, output: "html". It runs with
     throwOnError: true inside try/catch: KaTeX's own error span (throwOnError: false) colours itself with a
     style attribute, which the policy blocks, so a formula it cannot parse is shown here as its TeX source in
     a .md-math-error element instead, with the error as its title. No formula ever throws out of render().
   Rendering the same element again (the file changed) keeps the DOM of every top-level block whose HTML did
   not change, so only the changed blocks are parsed, typeset and laid out again; nothing is cached beyond
   the element's own children. Copying a selection that contains math copies the formulas as TeX. */
(function () {
  "use strict";

  var VERSION = "1";
  var KATEX_OPTIONS = {
    displayMode: false,
    output: "html",
    throwOnError: true, // caught below; see the header
    trust: false,
    strict: "ignore",
    maxExpand: 1000,
    maxSize: 50
  };
  var CLOSE = { "\\(": "\\)", "\\[": "\\]", "$": "$", "$$": "$$" };
  var LINK_RE = /^(?:https?:|mailto:)/i;

  var md = null;
  var svgPatched = false;

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return ch === "&" ? "&amp;" : ch === "<" ? "&lt;" : ch === ">" ? "&gt;" : ch === '"' ? "&quot;" : "&#39;";
    });
  }

  function isSpace(code) {
    return code === 0x20 || code === 0x09 || code === 0x0A || code === 0x0D || code === 0x0C;
  }

  function now() {
    return window.performance && performance.now ? performance.now() : Date.now();
  }

  /* Math syntax ------------------------------------------------------------------------------------------ */

  // The index in src[from, max) where `close` ends the formula, or -1. A backslash always takes the next
  // character with it, so \$ never closes and \\) is a line break followed by ")". A single $ closes only
  // when not preceded by white space and not followed by a digit (the pandoc rule), so "costs $5 and $10"
  // stays prose. Inline formulas stop at a backtick: a code span wins over math.
  function findClose(src, from, max, close, single, inline) {
    var c0 = close.charCodeAt(0);
    var c1 = close.length > 1 ? close.charCodeAt(1) : -1;
    for (var i = from; i < max; i++) {
      var ch = src.charCodeAt(i);
      if (ch === 0x60 && inline) return -1;
      if (ch === 0x5C) {
        if (c0 === 0x5C && i + 1 < max && src.charCodeAt(i + 1) === c1) return i;
        i++;
        continue;
      }
      if (ch !== c0) continue;
      if (c1 >= 0) {
        if (i + 1 < max && src.charCodeAt(i + 1) === c1) return i;
        continue;
      }
      if (single) {
        if (i === from || isSpace(src.charCodeAt(i - 1))) continue;
        var after = src.charCodeAt(i + 1);
        if (after >= 0x30 && after <= 0x39) continue;
      }
      return i;
    }
    return -1;
  }

  // Inline rule, before markdown-it's "escape": \( \) and $ $ inline, \[ \] and $$ $$ in display style
  // inside a paragraph. \$ and every other escape go on to markdown-it.
  function mathInline(state, silent) {
    var src = state.src;
    var pos = state.pos;
    var max = state.posMax;
    var ch = src.charCodeAt(pos);
    var open;
    var display;
    if (ch === 0x5C) {
      var next = src.charCodeAt(pos + 1);
      if (next === 0x28) { open = "\\("; display = false; }
      else if (next === 0x5B) { open = "\\["; display = true; }
      else return false;
    } else if (ch === 0x24) {
      if (src.charCodeAt(pos + 1) === 0x24) { open = "$$"; display = true; }
      else {
        if (pos + 1 >= max || isSpace(src.charCodeAt(pos + 1))) return false;
        open = "$"; display = false;
      }
    } else {
      return false;
    }
    var close = CLOSE[open];
    var start = pos + open.length;
    // A search that reached the end without a closer stays failed for every later opener of the same kind in
    // this range, so text with many lone dollars stays linear.
    var misses = state.tcsMathMisses || (state.tcsMathMisses = {});
    var missKey = open + ":" + max;
    if (misses[missKey] !== undefined && start >= misses[missKey]) return false;
    var end = findClose(src, start, max, close, open === "$", true);
    if (end < 0) {
      var tick = src.indexOf("`", start);
      if (tick < 0 || tick >= max) misses[missKey] = start;
      return false;
    }
    var tex = src.slice(start, end);
    if (!tex.trim()) return false;
    if (!silent) {
      var token = state.push(display ? "math_inline_display" : "math_inline", "math", 0);
      token.content = tex;
      token.markup = open;
    }
    state.pos = end + close.length;
    return true;
  }

  // Block rule: a line that starts with $$ or \[ opens display math up to the line where $$ or \] ends it
  // with nothing after it. It may interrupt a paragraph, so lines such as "= x" or "- y" inside the formula
  // never become Markdown. A blank line before the closer means no block (an unclosed $$ never swallows the
  // rest of the document); the inline rule then sees the text.
  function mathBlock(state, startLine, endLine, silent) {
    if (state.sCount[startLine] - state.blkIndent >= 4) return false;
    var src = state.src;
    var pos = state.bMarks[startLine] + state.tShift[startLine];
    var open;
    if (src.charCodeAt(pos) === 0x24 && src.charCodeAt(pos + 1) === 0x24) open = "$$";
    else if (src.charCodeAt(pos) === 0x5C && src.charCodeAt(pos + 1) === 0x5B) open = "\\[";
    else return false;
    var close = CLOSE[open];
    var line = startLine;
    var from = pos + 2;
    var parts = [];
    var end;
    for (;;) {
      var lineEnd = state.eMarks[line];
      end = findClose(src, from, lineEnd, close, false, false);
      if (end >= 0) {
        parts.push(src.slice(from, end));
        break;
      }
      parts.push(src.slice(from, lineEnd));
      line += 1;
      if (line >= endLine || state.isEmpty(line) || state.sCount[line] < state.blkIndent) return false;
      from = state.bMarks[line] + state.tShift[line];
    }
    if (src.slice(end + close.length, state.eMarks[line]).trim()) return false;
    var tex = parts.join("\n");
    if (!tex.trim()) return false;
    if (silent) return true;
    var token = state.push("math_block", "math", 0);
    token.block = true;
    token.content = tex;
    token.markup = open;
    token.map = [startLine, line + 1];
    state.line = line + 1;
    return true;
  }

  function placeholder(tag, tex, open, display) {
    var classes = "md-math md-math-pending" + (display ? " md-math-display" : "");
    return "<" + tag + ' class="' + classes + '" data-tex="' + escapeHtml(tex) + '">' +
      escapeHtml(open + tex + CLOSE[open]) + "</" + tag + ">";
  }

  /* markdown-it ------------------------------------------------------------------------------------------ */

  function createMarkdown() {
    var instance = window.markdownit({ html: false, linkify: false, typographer: false, breaks: false });
    instance.validateLink = function (url) { return LINK_RE.test(String(url).trim()); };
    instance.inline.ruler.before("escape", "tcs_math_inline", mathInline);
    instance.block.ruler.before("fence", "tcs_math_block", mathBlock,
      { alt: ["paragraph", "reference", "blockquote", "list"] });
    // A link whose address is not http, https or mailto (markdown-it keeps "[x]()" as a link with an empty
    // address) is shown as its text only.
    instance.core.ruler.push("tcs_safe_links", function (state) {
      state.tokens.forEach(function (block) {
        if (block.type !== "inline" || !block.children) return;
        var hiding = false;
        block.children.forEach(function (token) {
          if (token.type === "link_open") {
            hiding = !LINK_RE.test(String(token.attrGet("href") || "").trim());
            token.hidden = hiding;
          } else if (token.type === "link_close") {
            token.hidden = hiding;
            hiding = false;
          }
        });
      });
    });
    var rules = instance.renderer.rules;
    rules.math_inline = function (tokens, idx) {
      return placeholder("span", tokens[idx].content, tokens[idx].markup, false);
    };
    rules.math_inline_display = function (tokens, idx) {
      return placeholder("span", tokens[idx].content, tokens[idx].markup, true);
    };
    rules.math_block = function (tokens, idx) {
      return placeholder("div", tokens[idx].content, tokens[idx].markup, true) + "\n";
    };
    var defaultFence = rules.fence;
    rules.fence = function (tokens, idx, options, env, self) {
      var info = (tokens[idx].info || "").trim().split(/\s+/)[0].toLowerCase();
      if (info === "math") return placeholder("div", tokens[idx].content.replace(/\n$/, ""), "\\[", true) + "\n";
      return defaultFence(tokens, idx, options, env, self);
    };
    rules.link_open = function (tokens, idx, options, env, self) {
      var token = tokens[idx];
      if (token.hidden) return "";
      token.attrSet("target", "_blank");
      token.attrSet("rel", "noopener noreferrer");
      return self.renderToken(tokens, idx, options);
    };
    // Images are never loaded: the alt text, then the address as text.
    rules.image = function (tokens, idx, options, env, self) {
      var token = tokens[idx];
      var alt = self.renderInline(token.children || [], options, env);
      return '<span class="md-image"><span class="md-image-alt">' + (alt || "image") + "</span> " +
        '<span class="md-image-src">' + escapeHtml(token.attrGet("src") || "") + "</span></span>";
    };
    // Table alignment as a class: markdown-it writes style="text-align:...", which the policy blocks.
    function aligned(tokens, idx, options, env, self) {
      var token = tokens[idx];
      var style = token.attrGet("style");
      if (style) {
        var match = /text-align:\s*(left|center|right)/.exec(style);
        token.attrs = (token.attrs || []).filter(function (pair) { return pair[0] !== "style"; });
        if (match) token.attrJoin("class", "md-align-" + match[1]);
      }
      return self.renderToken(tokens, idx, options);
    }
    rules.th_open = aligned;
    rules.td_open = aligned;
    return instance;
  }

  /* KaTeX ------------------------------------------------------------------------------------------------ */

  // KaTeX sets one style attribute through setAttribute: the width of the SVG pieces of tall delimiters and of
  // \oiint, \oiiint. The policy blocks that (and reports it); set the same declarations through the CSSOM.
  function patchSvgNode(katex) {
    if (svgPatched) return;
    svgPatched = true;
    var proto = katex.__domTree && katex.__domTree.SvgNode && katex.__domTree.SvgNode.prototype;
    if (!proto || typeof proto.toNode !== "function") return;
    var original = proto.toNode;
    proto.toNode = function () {
      var attributes = this.attributes;
      if (!attributes || !Object.prototype.hasOwnProperty.call(attributes, "style")) return original.call(this);
      var style = attributes.style;
      delete attributes.style;
      var node;
      try {
        node = original.call(this);
      } finally {
        attributes.style = style;
      }
      node.style.cssText = style;
      return node;
    };
  }

  // Fill every pending placeholder in `element`. A formula that occurs several times is rendered once and
  // cloned (the memo lives for one call only).
  function typeset(element, katex) {
    var nodes = element.querySelectorAll(".md-math-pending");
    var memo = new Map();
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var tex = node.getAttribute("data-tex") || "";
      var display = node.classList.contains("md-math-display");
      var key = (display ? "D" : "I") + tex;
      var entry = memo.get(key);
      var rendered = null;
      if (entry) {
        if (entry.node) rendered = entry.node.cloneNode(true);
      } else {
        entry = { node: null, error: "" };
        var holder = document.createElement("span");
        try {
          KATEX_OPTIONS.displayMode = display;
          katex.render(tex, holder, KATEX_OPTIONS);
          entry.node = rendered = holder.firstChild;
        } catch (error) {
          entry.error = String(error && error.message || error).slice(0, 500);
        }
        memo.set(key, entry);
      }
      node.classList.remove("md-math-pending");
      if (rendered) {
        node.textContent = "";
        node.appendChild(rendered);
        node.setAttribute("role", "math");
        node.setAttribute("aria-label", tex);
      } else {
        node.classList.add("md-math-error");
        node.setAttribute("title", entry.error || "This formula could not be rendered.");
      }
    }
    return nodes.length;
  }

  /* Blocks ----------------------------------------------------------------------------------------------- */

  // element -> { math, blocks: [{ key, nodes }] }: the element's children, block by block, in order.
  var states = new WeakMap();
  var SEPARATOR = "<!--tcs-md-block-->";

  // The HTML of each top-level block (before math is filled in); it is also the block's identity.
  function blockHtml(tokens, env) {
    var out = [];
    var start = 0;
    var depth = 0;
    for (var i = 0; i < tokens.length; i++) {
      depth += tokens[i].nesting;
      if (depth <= 0) {
        out.push(md.renderer.render(tokens.slice(start, i + 1), md.options, env));
        start = i + 1;
        depth = 0;
      }
    }
    if (start < tokens.length) out.push(md.renderer.render(tokens.slice(start), md.options, env));
    return out;
  }

  // The element's state from the last render, if its children are still exactly the nodes put there.
  function currentState(element, math) {
    var state = states.get(element);
    if (!state || state.math !== math) return null;
    var child = element.firstChild;
    for (var b = 0; b < state.blocks.length; b++) {
      var nodes = state.blocks[b].nodes;
      for (var n = 0; n < nodes.length; n++) {
        if (nodes[n] !== child) return null;
        child = child.nextSibling;
      }
    }
    return child === null ? state : null;
  }

  /* Public API ------------------------------------------------------------------------------------------- */

  function render(text, element, options) {
    if (!element || element.nodeType !== 1) throw new TypeError("TCSMarkdown.render needs an element.");
    options = options || {};
    var started = now();
    var source = text == null ? "" : String(text);
    element.classList.add("md-doc");
    if (!window.markdownit) {
      // markdown-it did not load: show the text as it is.
      var pre = document.createElement("pre");
      pre.className = "md-plain";
      pre.textContent = source;
      states.delete(element);
      element.replaceChildren(pre);
      return { blocks: 1, reused: 0, formulas: 0, rendered: 0, errors: 0, milliseconds: Math.round(now() - started) };
    }
    if (!md) md = createMarkdown();
    var katex = options.math === false ? null : window.katex || null;
    var math = !!katex;
    var env = {};
    var keys = blockHtml(md.parse(source, env), env);

    // Match the new blocks to the old ones in order: a block keeps its old nodes when its HTML is the same
    // as that of an old block further down than the last one kept.
    var previous = currentState(element, math);
    if (!previous) element.replaceChildren();
    var oldBlocks = previous ? previous.blocks : [];
    var pool = new Map();
    oldBlocks.forEach(function (block, index) {
      var list = pool.get(block.key);
      if (list) list.push(index); else pool.set(block.key, [index]);
    });
    var blocks = [];
    var kept = new Array(oldBlocks.length);
    var last = -1;
    var fresh = [];
    keys.forEach(function (key) {
      var list = pool.get(key);
      while (list && list.length && list[0] <= last) list.shift();
      if (list && list.length) {
        last = list.shift();
        kept[last] = true;
        blocks.push(oldBlocks[last]);
      } else {
        var block = { key: key, nodes: null };
        blocks.push(block);
        fresh.push(block);
      }
    });
    oldBlocks.forEach(function (block, index) {
      if (!kept[index]) block.nodes.forEach(function (node) { node.remove(); });
    });

    // Parse the new blocks' HTML once, split it at the separators, and put each block in its place.
    if (fresh.length) {
      var template = document.createElement("template");
      template.innerHTML = fresh.map(function (block) { return block.key; }).join(SEPARATOR);
      var index = 0;
      fresh[0].nodes = [];
      Array.prototype.slice.call(template.content.childNodes).forEach(function (node) {
        if (node.nodeType === 8 && node.data === "tcs-md-block") {
          index += 1;
          fresh[index].nodes = [];
          node.remove();
        } else {
          fresh[index].nodes.push(node);
        }
      });
      var cursor = element.firstChild;
      blocks.forEach(function (block) {
        if (block.nodes.length && block.nodes[0].parentNode === element) {
          cursor = block.nodes[block.nodes.length - 1].nextSibling;
        } else {
          block.nodes.forEach(function (node) { element.insertBefore(node, cursor); });
        }
      });
    }
    states.set(element, { math: math, blocks: blocks });

    var rendered = 0;
    if (katex) {
      patchSvgNode(katex);
      rendered = typeset(element, katex);
    }
    return {
      blocks: blocks.length,
      reused: blocks.length - fresh.length,
      formulas: element.getElementsByClassName("md-math").length,
      rendered: rendered,
      errors: element.getElementsByClassName("md-math-error").length,
      milliseconds: Math.round(now() - started)
    };
  }

  function clear(element) {
    if (!element || element.nodeType !== 1) return;
    states.delete(element);
    element.replaceChildren();
  }

  /* Copy: math in the selection is copied as its TeX ----------------------------------------------------- */

  function mathAncestor(node) {
    var element = node && (node.nodeType === 1 ? node : node.parentElement);
    return element && element.closest ? element.closest(".md-doc .md-math") : null;
  }

  function onCopy(event) {
    var selection = window.getSelection ? window.getSelection() : null;
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed || !event.clipboardData) return;
    var range = selection.getRangeAt(0).cloneRange();
    var startMath = mathAncestor(range.startContainer);
    var endMath = mathAncestor(range.endContainer);
    if (startMath) range.setStartBefore(startMath);
    if (endMath) range.setEndAfter(endMath);
    var fragment = range.cloneContents();
    var maths = fragment.querySelectorAll(".md-math");
    if (!maths.length) return;
    for (var i = 0; i < maths.length; i++) {
      var tex = maths[i].getAttribute("data-tex") || "";
      var display = maths[i].classList.contains("md-math-display");
      maths[i].replaceWith(document.createTextNode(display ? "\\[" + tex + "\\]" : "\\(" + tex + "\\)"));
    }
    var holder = document.createElement("div");
    holder.appendChild(fragment);
    event.clipboardData.setData("text/plain", holder.textContent);
    event.clipboardData.setData("text/html", holder.innerHTML);
    event.preventDefault();
  }

  document.addEventListener("copy", onCopy);

  window.TCSMarkdown = {
    version: VERSION,
    render: render,
    clear: clear
  };
})();
