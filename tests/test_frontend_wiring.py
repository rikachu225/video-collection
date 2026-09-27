"""Frontend wiring: the ids the scripts reach for exist, and every button is wired.

Why this exists: 3557c67. A cloud-sync conflict dropped the Refresh Icons
listener from app.js. The button was still in index.html, clicking it did
nothing, and the suite stayed green because no test looked at the frontend.

No browser and no node. A small scanner walks app.js and assistant.js the way a
JS tokenizer would (comments, strings, template literals, regex literals) and
records every id lookup - $("#id"), querySelector("#id"), getElementById("id") -
with whether it sits at the TOP LEVEL of the script (runs once when the script
loads, which is where this app binds its listeners) or inside a function body.
The body of an IIFE - assistant.js is wrapped in one - counts as top level.

(a) Every id looked up at top level exists in index.html. A missing one is
    null.addEventListener(...) at load time, which aborts the rest of the script.
(b) Every <button id> in index.html is looked up at top level. A button reached
    only from inside a function is not wired - exactly the 3557c67 shape, where
    refreshServiceIcons() still read the button but nothing bound its click.
"""
import re
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
