"""Frontend wiring: the ids the scripts reach for exist, and every button is wired.

Why this exists: 3557c67. A cloud-sync conflict dropped the Refresh Icons
listener from app.js. The button was still in index.html, clicking it did
nothing, and the suite stayed green because no test looked at the frontend.

The wiring checks need no browser and no node. A small scanner walks app.js and
assistant.js the way a JS tokenizer would (comments, strings, template literals,
regex literals) and records every id lookup - $("#id"), querySelector("#id"),
getElementById("id") - with whether it sits at the TOP LEVEL of the script (runs
once when the script loads, which is where this app binds its listeners) or inside
a function body. The body of an IIFE - assistant.js is wrapped in one - counts as
top level.

(a) Every id looked up at top level exists in index.html. A missing one is
    null.addEventListener(...) at load time, which aborts the rest of the script.
(b) Every <button id> in index.html is looked up at top level. A button reached
    only from inside a function is not wired - exactly the 3557c67 shape, where
    refreshServiceIcons() still read the button but nothing bound its click.

The behaviour checks at the end run real sections of app.js under node against a
small fake DOM (skipped when node is not on PATH).
"""
import importlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SCRIPTS = ("app.js", "assistant.js")

# Buttons that are legitimately NOT looked up by id at top level. Keep this short and
# say why - an entry here switches the check off for that button.
WIRED_ELSEWHERE = {
    # Click handling is bound to the whole .nav-btn class at top level; the id is only
    # used by applyBranding() to relabel it with the custom theater name.
    "nav-theater-btn",
    # Lives in the legacy single-player view (#view-player). Nothing activates that view
    # since the popup player replaced it, and this button has had no click handler since
    # the initial release; applyBranding() only retitles its tooltip. Dead markup, not a
    # lost listener - remove it together with the view rather than wiring it.
    "btn-add-theater",
}


# ── A deliberately small JS scanner ──────────────────────────────────────────
_REGEX_OK_AFTER_WORD = {
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
}
_BLOCK_PARENS = {"if", "for", "while", "switch", "catch", "with"}   # `(...) {` is a block
_PUNCT = ("=>", "?.", "...")
_ID_RE = re.compile(r"[A-Za-z_$][\w$]*")
_NUM_RE = re.compile(r"\d[\w.]*")


def _tokenize(src):
    """Tokens as (kind, value). kind: word, num, str, tmpl, regex, punct.

    A `${ ... }` substitution is emitted as a "(" ... ")" pair, so the depth pass
    treats the embedded expression like any other parenthesised code.
    """
    toks, i, n = [], 0, len(src)
    braces = []            # "{" for code braces, "tmpl" for an open ${ substitution

    def read_template(j):
        """Read template text from j; stop after the closing ` or at a ${."""
        start = j
        while j < n:
            ch = src[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                toks.append(("tmpl", src[start:j]))
                return j + 1
            if src.startswith("${", j):
                toks.append(("tmpl", src[start:j]))
                toks.append(("punct", "("))
                braces.append("tmpl")
                return j + 2
            j += 1
        raise ValueError("unterminated template literal")

    def regex_allowed():
        if not toks:
            return True
        kind, val = toks[-1]
        if kind == "word":
            return val in _REGEX_OK_AFTER_WORD
        if kind == "punct":
            return val not in (")", "]", "}")
        return False                     # after a number, string, template or regex

    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c in "'\"":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            toks.append(("str", src[i + 1:j]))
            i = j + 1
        elif c == "`":
            i = read_template(i + 1)
        elif c == "/" and regex_allowed():
            j, in_class = i + 1, False
            while True:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "\n":
                    raise ValueError(f"unterminated regex literal at offset {i}")
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1                   # flags
            toks.append(("regex", src[i:j]))
            i = j
        elif c == "{":
            braces.append("{")
            toks.append(("punct", "{"))
            i += 1
        elif c == "}":
            if braces and braces[-1] == "tmpl":
                braces.pop()
                toks.append(("punct", ")"))
                i = read_template(i + 1)
            else:
                if braces:
                    braces.pop()
                toks.append(("punct", "}"))
                i += 1
        elif _ID_RE.match(src, i):
            m = _ID_RE.match(src, i)
            toks.append(("word", m.group()))
            i = m.end()
        elif c.isdigit():
            m = _NUM_RE.match(src, i)
            toks.append(("num", m.group()))
            i = m.end()
        else:
            for p in _PUNCT:
                if src.startswith(p, i) and not (p == "?." and src[i + 2:i + 3].isdigit()):
                    toks.append(("punct", p))
                    i += len(p)
                    break
            else:
                toks.append(("punct", c))
                i += 1
    return toks


def scan_id_lookups(src):
    """Every id lookup in `src` as (id, at_top_level).

    Tracks a stack of open brackets, each marked as a function body or not:
      - `=> {` and `) {` (unless the paren belongs to if/for/while/switch/catch/with)
        open a function body. Any other `{` - an object literal, or an else/try/
        finally/do/catch block - does not.
      - `=> expr` (no brace) is a function body that ends at a `,` or `;` on its own
        level, or when an enclosing bracket closes.
      - A `function` expression that sits directly inside a grouping paren - the
        IIFE idiom, `(function () { ... })()` - runs at load, so its body is
        transparent and still counts as top level.
    """
    toks = _tokenize(src)
    stack = []          # entries: [kind, counts_as_function]; kind in ( [ { =>
    open_at = {}        # index of ")" -> index of its matching "("
    paren_open = []     # indices of currently open "("
    grouping = set()    # indices of "(" that are grouping parens, not calls
    lookups = []

    def fn_depth():
        return sum(1 for kind, is_fn in stack if is_fn)

    def close_arrows():
        while stack and stack[-1][0] == "=>":
            stack.pop()

    def close(opener, k):
        close_arrows()
        if not stack or stack[-1][0] != opener:
            # A mis-read regex or template derails everything after it - fail loudly
            # instead of quietly misclassifying the rest of the file.
            raise ValueError(f"unbalanced {opener!r} at token {k}: {toks[max(0, k - 5):k + 1]}")
        stack.pop()

    def prev(k, back=1):
        return toks[k - back] if k - back >= 0 else ("", "")

    for k, (kind, val) in enumerate(toks):
        if kind == "punct" and val in ("(", "["):
            if val == "(":
                pk, pv = prev(k)
                is_call = (pk == "word" and pv not in _REGEX_OK_AFTER_WORD
                           and pv not in _BLOCK_PARENS) or pv in (")", "]")
                if not is_call:
                    grouping.add(k)
                paren_open.append(k)
            stack.append([val, False])
        elif kind == "punct" and val in (")", "]"):
            close("(" if val == ")" else "[", k)
            if val == ")":
                open_at[k] = paren_open.pop()
        elif kind == "punct" and val == "{":
            pv = prev(k)[1]
            is_fn = False
            if pv == "=>":
                is_fn = True
            elif pv == ")" and (k - 1) in open_at:
                lp = open_at[k - 1]
                before = prev(lp)
                if before[1] not in _BLOCK_PARENS:
                    is_fn = True
                    # (function name?(...) { ... })  -> IIFE body, runs at load
                    j = lp - 1
                    if j >= 0 and toks[j][0] == "word" and toks[j][1] != "function":
                        j -= 1               # skip the function's name
                    if j >= 0 and toks[j] == ("word", "function"):
                        j -= 1
                        if j >= 0 and toks[j] == ("word", "async"):
                            j -= 1
                        if j >= 0 and j in grouping:
                            is_fn = False
            stack.append(["{", is_fn])
        elif kind == "punct" and val == "}":
            close("{", k)
        elif kind == "punct" and val in (",", ";"):
            close_arrows()
        elif kind == "punct" and val == "=>":
            if k + 1 < len(toks) and toks[k + 1] != ("punct", "{"):
                stack.append(["=>", True])
        elif kind == "word" and val in ("$", "querySelector", "getElementById"):
            # `( "literal" )` exactly: a template with a ${} substitution tokenizes as
            # tmpl ( ... ) tmpl, so it never matches - only constant ids are checkable.
            nxt = toks[k + 1:k + 4]
            if (len(nxt) == 3 and nxt[0] == ("punct", "(") and nxt[1][0] in ("str", "tmpl")
                    and nxt[2] == ("punct", ")")):
                arg = nxt[1][1]
                m = (re.fullmatch(r"[A-Za-z][\w-]*", arg) if val == "getElementById"
                     else re.fullmatch(r"#([A-Za-z][\w-]*)", arg))
                if m and (val == "$" or prev(k)[1] == "."):
                    lookups.append((m.group(1) if m.groups() else m.group(0), fn_depth() == 0))
    if stack:
        raise ValueError(f"scanner ended with {len(stack)} unclosed bracket(s): {stack[-3:]}")
    return lookups


# ── Fixtures ─────────────────────────────────────────────────────────────────
def _html():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _html_ids():
    # Skip anything inside <script>/<style> and comments; every other id="" counts.
    html = re.sub(r"<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>", "", _html(), flags=re.S)
    return set(re.findall(r"\bid\s*=\s*[\"']([^\"']+)[\"']", html))


def _html_button_ids():
    html = re.sub(r"<!--.*?-->", "", _html(), flags=re.S)
    return set(re.findall(r"<button\b[^>]*?\bid\s*=\s*[\"']([^\"']+)[\"']", html))


@pytest.fixture(scope="module")
def lookups():
    return {name: scan_id_lookups((STATIC / name).read_text(encoding="utf-8")) for name in SCRIPTS}


def _top_level_ids(lookups):
    return {i for refs in lookups.values() for i, top in refs if top}


# ── The scanner itself (so a derailed scan can't pass the real checks vacuously) ──
def test_scanner_classifies_top_level_vs_function_bodies():
    src = r"""
    const dom = { grid: $("#top-a") };                  // object literal: top level
    $("#top-b").addEventListener("click", () => $("#fn-a"));
    if (dom.grid) { $("#top-c"); } else { $("#top-d"); }
    try { $("#top-e"); } catch { }
    function later() { return $("#fn-b"); }
    const x = "it's /not/ code", y = s.replace(/'/g, "&#39;"), z = `a ${ $("#top-f") } b`;
    const tpl = `<b id="fn-c">${ [1].map((n) => `${ $("#fn-d") }`).join("") }</b>`;
    list.forEach((el) => { el.querySelector("#fn-e"); });
    document.getElementById("top-g"); el.getElementById("top-h");
    (function () {
      const orb = $("#top-i");                          // IIFE body runs at load
      function inner() { $("#fn-f"); }
      const api = { get(u) { return $("#fn-g"); } };
    })();
    """
    got = dict(scan_id_lookups(src))
    assert {k for k, top in got.items() if top} == {
        "top-a", "top-b", "top-c", "top-d", "top-e", "top-f", "top-g", "top-h", "top-i"}
    assert {k for k, top in got.items() if not top} == {"fn-a", "fn-b", "fn-d", "fn-e", "fn-f", "fn-g"}


def test_scanner_fails_loudly_instead_of_misreading():
    # A regex or template the tokenizer gets wrong shows up as unbalanced brackets.
    with pytest.raises(ValueError):
        scan_id_lookups("function f() { $(\"#a\"); ")
    with pytest.raises(ValueError):
        scan_id_lookups("const a = [1, 2); ")


def test_scanner_reads_the_real_scripts(lookups):
    # Balanced brackets are asserted inside scan_id_lookups; these anchor known facts so a
    # tokenizer slip (a regex or template read as code) shows up here, not as silence.
    app_top = {i for i, top in lookups["app.js"] if top}
    app_fn = {i for i, top in lookups["app.js"] if not top}
    assert "btn-settings" in app_top                        # top-level addEventListener
    assert "streaming-settings-list" in app_fn - app_top    # only read inside a render fn
    assert "ai-orb" in {i for i, top in lookups["assistant.js"] if top}   # IIFE body
    assert len(_top_level_ids(lookups)) > 40


# ── The wiring checks ────────────────────────────────────────────────────────
def test_every_top_level_id_lookup_exists_in_index_html(lookups):
    ids = _html_ids()
    missing = {
        name: sorted({i for i, top in refs if top and i not in ids})
        for name, refs in lookups.items()
    }
    assert not any(missing.values()), (
        f"ids looked up at script load but absent from index.html: {missing}. "
        "At load these are null.addEventListener(...) and abort the rest of the script.")


def test_every_button_in_index_html_is_wired(lookups):
    unwired = sorted(_html_button_ids() - _top_level_ids(lookups) - WIRED_ELSEWHERE)
    assert not unwired, (
        f"<button id> in index.html never looked up at the top level of {SCRIPTS}: {unwired}. "
        "Nothing binds their click (the 3557c67 regression). Wire them, or add them to "
        "WIRED_ELSEWHERE with the reason.")


def test_wired_elsewhere_allow_list_is_not_stale(lookups):
    buttons = _html_button_ids()
    assert WIRED_ELSEWHERE <= buttons, f"allow-listed ids no longer in index.html: {WIRED_ELSEWHERE - buttons}"
    still_needed = WIRED_ELSEWHERE - _top_level_ids(lookups)
    assert still_needed == WIRED_ELSEWHERE, (
        f"now wired at top level, drop from WIRED_ELSEWHERE: {WIRED_ELSEWHERE - still_needed}")


def test_every_ui_command_tool_has_a_handler_in_assistant_js():
    import ai_agent
    src = (STATIC / "assistant.js").read_text(encoding="utf-8")
    block = re.search(r"const UI = \{(.*?)\n  \};", src, flags=re.S)
    assert block, "assistant.js no longer defines `const UI = { ... };`"
    keys = set(re.findall(r"^\s{4}(\w+)\s*:", block.group(1), flags=re.M))
    missing = sorted(ai_agent.UI_COMMAND_TOOLS - keys)
    assert not missing, f"UI commands the model can emit but assistant.js can't run: {missing}"


# ── Behaviour: real app.js sections under node ───────────────────────────────
# Each test cuts whole sections out of app.js at its `// ── Title` headers, runs them
# under node against the fake DOM below, and asserts on what the code DID. The fake
# models only the browser behaviour a test depends on, and says so where it does.
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")


def _app_section(title):
    """One `// ── <title>` section of app.js, from its header up to the next one."""
    lines = (STATIC / "app.js").read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.startswith("// ── " + title)]
    assert len(starts) == 1, f"app.js section {title!r}: {len(starts)} headers found"
    end = next((i for i in range(starts[0] + 1, len(lines)) if lines[i].startswith("// ── ")), len(lines))
    return "".join(lines[starts[0]:end])


def _run_node(tmp_path, *parts):
    """Run the parts as one script. The script prints one JSON value on its last line."""
    script = tmp_path / "harness.js"
    script.write_text("\n".join(parts), encoding="utf-8")
    proc = subprocess.run([NODE, str(script)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


_FAKE_DOM = r"""
const timers = [];                                  // run on demand, never by the clock
const setTimeout = (fn) => timers.push(fn);
const clearTimeout = () => {};
const flushTimers = () => { while (timers.length) timers.shift()(); };
const requestAnimationFrame = (fn) => fn();
const winListeners = {};
const window = {
  innerWidth: 1600, innerHeight: 900,
  addEventListener(type, fn) { (winListeners[type] = winListeners[type] || []).push(fn); },
};
const fireWindow = (type) => (winListeners[type] || []).forEach((fn) => fn({ type }));
class FakeClassList {
  constructor(...names) { this.set = new Set(names); }
  add(...n) { n.forEach((x) => this.set.add(x)); }
  remove(...n) { n.forEach((x) => this.set.delete(x)); }
  contains(n) { return this.set.has(n); }
  toggle(n, on) { if (on === undefined) on = !this.set.has(n); if (on) this.set.add(n); else this.set.delete(n); return on; }
}
class FakeEl {
  constructor(tag = "div", ...classes) {
    this.tagName = tag.toUpperCase(); this.classList = new FakeClassList(...classes);
    this.children = []; this.dataset = {}; this.parentElement = null; this.listeners = {};
    this.style = { setProperty(k, v) { this[k] = v; } };
  }
  get className() { return [...this.classList.set].join(" "); }
  set className(v) { this.classList = new FakeClassList(...String(v).split(/\s+/).filter(Boolean)); }
  set innerHTML(v) { if (v === "") this.children = []; this.html = v; }
  get innerHTML() { return this.html || ""; }
  get firstElementChild() { return this.children[0] || null; }
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  append(...cs) { cs.forEach((c) => this.appendChild(c)); }
  remove() { const p = this.parentElement; if (p) p.children.splice(p.children.indexOf(this), 1); this.parentElement = null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  matches(sel) {
    return sel.split(",").some((s) => s.trim().split(".").filter(Boolean).every((c) => this.classList.contains(c)));
  }
}
const document = { createElement: (tag) => new FakeEl(tag) };
const rect = (left, top, width, height) => ({ left, top, width, height, right: left + width, bottom: top + height });
const hit = (a, b) => a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top;
"""


# ── Bento: a window resize re-clamps column spans ──
_BENTO_DOM = r"""
// The CSS Grid behaviour the clamp depends on: the resolved grid-template-columns lists
// EVERY column - the template's own (`explicit`, set by the test as the window "resizes")
// plus the implicit ones a span wider than the template creates. A grid in a hidden view
// has no layout box and reports the computed value, which holds no px tracks.
const spanOf = (el) => parseInt(String(el.style.gridColumnEnd || "span 1").replace("span ", ""), 10) || 1;
class FakeGrid extends FakeEl {
  constructor(view, explicit) { super("div"); this.view = view; this.explicit = explicit; }
}
function getComputedStyle(el) {
  if (!(el instanceof FakeGrid)) return {};
  const n = Math.max(el.explicit, ...el.children.map(spanOf));
  return { columnGap: "16px", rowGap: "16px",
           gridTemplateColumns: el.view.classList.contains("active")
             ? Array(n).fill("100px").join(" ") : "repeat(12, 1fr)" };
}
class FakeTile extends FakeEl {
  constructor(cls, path) {
    super("div", cls);
    this.dataset.path = path;
    this.append(new FakeEl("div"), Object.assign(new FakeEl("div"), { offsetHeight: 40 }));
    this.children[0].style.aspectRatio = "1.7778";
  }
  get clientWidth() { return spanOf(this) * 116 - 16; }
}
const views = { browse: new FakeEl("section", "view", "active"), theater: new FakeEl("section", "view") };
const browseGrid = new FakeGrid(views.browse, 5), theaterGrid = new FakeGrid(views.theater, 12);
const grids = [browseGrid, theaterGrid];
const main = { scrollTop: 0 };
const dom = { videoGrid: browseGrid, theaterGrid, breadcrumb: new FakeEl("div") };
function $(sel) {
  const view = /^#view-(\w+)$/.exec(sel);
  if (view) return views[view[1]] || null;
  return { "#main": main, "#browse-media-controls": new FakeEl("div") }[sel] || null;
}
function $$(sel) {
  if (/video-card|theater-cell/.test(sel)) return grids.flatMap((g) => g.children);
  if (/video-grid|theater-grid/.test(sel)) return grids;
  if (sel === ".view") return Object.values(views);
  return [];
}
let bentoResize = null;
const loadTheater = () => {}, loadPlaylists = () => {}, loadStreaming = () => {}, showFolderGrid = () => {};
const state = {
  currentView: "browse", currentFolder: "F",
  currentFolderLayouts: { "F/wide.mp4": { tileCols: 3 } },
  theaterClips: [{ path: "T/a.mp4" }, { path: "T/b.mp4" }, { path: "T/c.mp4", bentoCols: 12 }],
};
"""

_BENTO_SCENARIO = r"""
const spans = (g) => g.children.map(spanOf);
const tracks = (g) => getComputedStyle(g).gridTemplateColumns.split(" ").length;
function renderInto(grid, tile, cols, view, fallback) {   // what renderVideoGrid / renderTheater do
  grid.appendChild(tile);
  bentoSpan(tile, bentoTileWidth(grid, applyBentoCols(tile, cols, view, fallback)));
}
const resize = (grid, explicit) => { grid.explicit = explicit; fireWindow("resize"); flushTimers(); };
const out = {};

["F/a.mp4", "F/b.mp4", "F/wide.mp4", "F/c.mp4", "F/d.mp4"].forEach((p) =>
  renderInto(browseGrid, new FakeTile("video-card", p), state.currentFolderLayouts[p]?.tileCols, "browse"));
out.browseWide = spans(browseGrid);
resize(browseGrid, 2);
out.browseNarrow = spans(browseGrid);
out.browseNarrowTracks = tracks(browseGrid);
resize(browseGrid, 5);
out.browseRewide = spans(browseGrid);

switchView("theater");
const dflt = theaterDefaultCols(state.theaterClips.length);
state.theaterClips.forEach((c) => renderInto(theaterGrid, new FakeTile("theater-cell", c.path), c.bentoCols, "theater", dflt));
out.theaterWide = spans(theaterGrid);
resize(theaterGrid, 1);                      // the phone rule: .theater-grid { grid-template-columns: 1fr }
out.theaterPhone = spans(theaterGrid);
out.theaterPhoneTracks = tracks(theaterGrid);
resize(theaterGrid, 12);
out.theaterRewide = spans(theaterGrid);

resize(browseGrid, 2);                       // the window narrows while Browse is hidden
out.browseWhileHidden = spans(browseGrid);
switchView("browse");                        // back to the open folder: no re-render
out.browseShownAgain = spans(browseGrid);
out.browseShownAgainTracks = tracks(browseGrid);
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def bento_run(tmp_path_factory):
    if NODE is None:
        pytest.skip("node is not on PATH")
    return _run_node(tmp_path_factory.mktemp("bento"), _FAKE_DOM, _BENTO_DOM, _app_section("Bento grid"),
                     _app_section("Navigation"), _BENTO_SCENARIO)


def test_window_resize_reclamps_bento_spans_against_the_template_not_implicit_columns(bento_run):
    # A 3-wide card in a 2-column grid makes CSS Grid add a third, implicit column, and the
    # resolved track list includes it - so clamping against that list kept the card 3 wide
    # (and packed its neighbours into the implicit column) however often it re-ran.
    got = bento_run
    assert got["browseWide"] == [1, 1, 3, 1, 1]
    assert got["browseNarrow"] == [1, 1, 2, 1, 1], "narrowing the window must re-clamp the wide card"
    assert got["browseNarrowTracks"] == 2, "a span still wider than the grid is creating implicit columns"
    assert got["browseRewide"] == [1, 1, 3, 1, 1], "widening must give the stored size back"
    assert got["theaterWide"] == [6, 6, 12]
    assert got["theaterPhone"] == [1, 1, 1]
    assert got["theaterPhoneTracks"] == 1
    assert got["theaterRewide"] == [6, 6, 12]


def test_a_bento_grid_hidden_during_the_resize_is_reclamped_when_shown(bento_run):
    got = bento_run
    # Hidden: no tracks to clamp against, so it is left alone rather than un-clamped...
    assert got["browseWhileHidden"] == [1, 1, 3, 1, 1]
    # ...and fixed the moment Browse is shown again, which re-renders nothing.
    assert got["browseShownAgain"] == [1, 1, 2, 1, 1]
    assert got["browseShownAgainTracks"] == 2


def test_main_reserves_its_scrollbar_gutter():
    # The observer re-clamps on a width change and the re-clamp changes the grid's height. If
    # that height could add or remove #main's scrollbar, it would change the width again: in
    # Chromium a re-clamp re-triggered itself twice that way before settling.
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "styles.css").read_text(encoding="utf-8"), flags=re.S)
    rules = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
    gutter = [v for sel, body in rules if sel == "#main"
              for v in re.findall(r"scrollbar-gutter\s*:\s*([\w -]+?)\s*(?:;|$)", body)]
    assert gutter == ["stable"], gutter


# ── Toasts stay clear of the assistant orb and panel ──
_TOAST_DOM = r"""
// Geometry from styles.css / assistant.css: the stack's home is right: 20px; bottom: 20px
// (an inline right/bottom moves it); each toast is TOAST_W x 50 with 8px between them;
// the orb is 56px square, by default at right: 24px; bottom: 24px.
const TOAST_W = 232;
const box = new FakeEl("div");
box.getBoundingClientRect = function () {
  const n = this.children.length, w = n ? TOAST_W : 0, h = n ? n * 50 + (n - 1) * 8 : 0;
  const right = window.innerWidth - (this.style.right ? parseFloat(this.style.right) : 20);
  const bottom = window.innerHeight - (this.style.bottom ? parseFloat(this.style.bottom) : 20);
  return rect(right - w, bottom - h, w, h);
};
const orb = new FakeEl("button"), panel = new FakeEl("div", "hidden");
orb.getBoundingClientRect = () => orb.classList.contains("hidden") ? rect(0, 0, 0, 0) : rect(orb.x, orb.y, 56, 56);
panel.getBoundingClientRect = () => panel.classList.contains("hidden") ? rect(0, 0, 0, 0) : panel.box;
const dom = { toastContainer: box };
const $ = (sel) => ({ "#ai-orb": orb, "#ai-panel": panel })[sel] || null;
const $$ = () => [];
// assistant.js moves, shows and hides the orb and panel through their style and class
// attributes. mutate() changes one and notifies the observers watching it, as a browser would.
const observers = [];
class MutationObserver {
  constructor(cb) { this.cb = cb; this.watch = []; observers.push(this); }
  observe(target, opts) { this.watch.push([target, opts]); }
}
function mutate(el, attr, change) {
  change();
  for (const o of observers) {
    if (o.watch.some(([t, opts]) => t === el && opts.attributes
        && (!opts.attributeFilter || opts.attributeFilter.includes(attr)))) {
      o.cb([{ type: "attributes", target: el, attributeName: attr }], o);
    }
  }
}
function reset(w, h) {
  box.children = [];
  window.innerWidth = w; window.innerHeight = h;
  orb.x = w - 80; orb.y = h - 80;
  orb.classList.remove("hidden"); panel.classList.add("hidden");
}
"""

_TOAST_SCENARIO = r"""
const link = { label: "Open Netflix", href: "https://example.invalid/" };
const report = () => {
  const b = box.getBoundingClientRect();
  return { overOrb: hit(b, orb.getBoundingClientRect()), overPanel: hit(b, panel.getBoundingClientRect()),
           onScreen: b.left >= 0 && b.top >= 0 && b.right <= window.innerWidth && b.bottom <= window.innerHeight,
           moved: Boolean(box.style.right || box.style.bottom) };
};
const out = {};

reset(1600, 900);
toast("Netflix is ready", "info", link);
out.defaultOrb = report();
const at = box.getBoundingClientRect();      // the user drags the orb onto the toast
mutate(orb, "style", () => { orb.x = at.left + 20; orb.y = at.top - 3; });
out.orbDraggedOntoToast = report();

reset(1600, 900);
mutate(panel, "class", () => { panel.classList.remove("hidden"); panel.box = rect(1196, 408, 380, 400); });
toast("One", "info"); toast("Two", "success"); toast("Netflix is ready", "info", link);
out.stackWithChatOpen = report();

reset(320, 800);                              // no room to slide left of the orb
toast("Netflix is ready", "info", link);
out.narrowWindow = report();

reset(1600, 900);
orb.classList.add("hidden");                  // assistant switched off
toast("Saved", "success");
out.orbHidden = report();
console.log(JSON.stringify(out));
"""


@needs_node
def test_toasts_never_cover_the_assistant_orb_wherever_it_is(tmp_path):
    # The toast stack and the orb shared the bottom-right corner. A link toast takes
    # clicks for 10s, so clicking the orb there launched the service instead.
    got = _run_node(tmp_path, _FAKE_DOM, _TOAST_DOM, _app_section("Toast Notifications"), _TOAST_SCENARIO)
    assert not got["defaultOrb"]["overOrb"], "a toast covers the orb in its default corner"
    assert not got["orbDraggedOntoToast"]["overOrb"], "a toast on screen must move when the orb is dragged onto it"
    assert not got["stackWithChatOpen"]["overOrb"]
    assert not got["stackWithChatOpen"]["overPanel"], "the stack covers the open chat panel"
    assert not got["narrowWindow"]["overOrb"], "no room to the left must not mean covering the orb"
    assert not got["orbHidden"]["moved"], "with no orb the stack stays in its CSS corner"
    assert all(r["onScreen"] for r in got.values()), got


def test_only_a_shown_action_toast_takes_clicks():
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "styles.css").read_text(encoding="utf-8"), flags=re.S)
    rules = [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]
    container = [re.findall(r"pointer-events\s*:\s*([\w-]+)", body) for sel, body in rules if sel == "#toast-container"]
    assert {v for vals in container for v in vals} == {"none"}, (
        "#toast-container must stay click-through, or its whole box eats clicks")
    takes_clicks = [part.strip() for sel, body in rules if re.search(r"pointer-events\s*:\s*auto", body)
                    for part in sel.split(",") if ".toast" in part]
    assert takes_clicks, "no toast takes clicks - a link toast could not be followed"
    # Fading in or out, a toast is (nearly) invisible and must not swallow clicks meant for
    # whatever is under it.
    assert all(".show" in part for part in takes_clicks), takes_clicks


# ── Streaming icon URLs ──
_STREAMING_DOM = r"""
const state = { streamingServices: [
  { id: "netflix", name: "Netflix", url: "https://www.netflix.com/", accent: "#e50914", enabled: true } ] };
const dom = { streamingGrid: new FakeEl("div"), streamingEmpty: new FakeEl("div") };
const $ = () => null, $$ = () => [];
"""

_STREAMING_SCENARIO = r"""
const logoSrc = () => dom.streamingGrid.children[0].children.find((c) => c.className === "streaming-logo").src;
renderStreaming();
const normal = logoSrc();
iconBust = "1700000000000";                   // what Settings > Refresh Icons sets
renderStreaming();
console.log(JSON.stringify({ normal, refreshed: logoSrc() }));
"""

# Every URL v2.8.2 and earlier requested icons at, each cached for 7 days (max-age=604800).
_PRE_283_ICON_URL = re.compile(r"/api/service-icon/netflix(\?t=\d+)?")


@needs_node
def test_tile_icon_urls_never_reuse_a_pre_upgrade_cache_entry(tmp_path, monkeypatch):
    got = _run_node(tmp_path, _FAKE_DOM, _STREAMING_DOM,
                    _app_section("Streaming (deep-link launcher tiles)"), _STREAMING_SCENARIO)
    for url in got.values():
        assert not _PRE_283_ICON_URL.fullmatch(url), (
            f"{url} is a URL v2.8.2 cached for a week; the browser serves that without asking")
    assert got["refreshed"] != got["normal"], "Refresh Icons must change the URL within the page"

    # ...and the server answers them (the route ignores the query).
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("VIDCOL_DATA_DIR", str(data))
    import server
    importlib.reload(server)
    icon = b"\x89PNG\r\n\x1a\n" + b"i" * 32
    (server.SERVICE_ICONS_DIR / "netflix.png").write_bytes(icon)   # a user drop-in: no fetch
    client = server.app.test_client()
    for url in got.values():
        res = client.get(url)
        assert res.status_code == 200 and res.data == icon, url
