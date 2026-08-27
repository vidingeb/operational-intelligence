"""Client-side table rendering and CSV export.

The chat pane builds messages from text nodes, so a Markdown table used to
arrive as literal pipes — 31 rows of "name | vmx-21 | 13312 | Tools running"
with no columns, which is what prompted this. The server was flattening tables
to compensate; that was a workaround for a missing renderer.

There is no Node runtime available here, so the JavaScript is checked two
ways: esprima parses it, and dukpy *executes* it against a DOM stub. Parsing
alone would only prove the file is syntactically valid, not that a table comes
out the other end. Both are optional test dependencies:

    pip install esprima dukpy

One trap, learned the hard way: the JS must be read from the *evaluated*
Python string, because a backslash-escaped regex in the Python source is not
what the browser receives.

Still unproven by these tests: how it looks. Borders, column widths and the
Download button are visual, and only a browser can confirm them.
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import web_ui  # noqa: E402

esprima = pytest.importorskip("esprima")


def _script() -> str:
    """The evaluated page's JavaScript, as the browser would receive it."""
    page = web_ui.HTML_PAGE
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert scripts, "no <script> block found in the page"
    return "\n".join(scripts)


def test_javascript_parses():
    """Catches a broken escape or stray brace that would kill the whole pane."""
    esprima.parseScript(_script())


def test_renderer_is_wired_into_addmessage():
    script = _script()
    assert "renderBody(div," in script, "assistant messages must go through renderBody"
    assert "div.appendChild(document.createTextNode('\\n' + text))" not in script, \
        "the old text-only path must be gone, or tables never reach the renderer"


def _table_code() -> str:
    """Just the table-rendering JavaScript, comments stripped.

    Scoped deliberately. The page has pre-existing innerHTML in the GPU
    telemetry strip, which builds from numbers off our own endpoint, and in
    static <option> literals. Those are out of scope for this change; the ban
    asserted here is on the code that handles model-authored text.
    """
    script = _script()
    start = script.index("const TABLE_ROW")
    end = script.index("// --- pending write confirmation")
    block = script[start:end]
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in block.split("\n"))


def test_no_innerhtml_in_the_table_renderer():
    """Cells come from a model; markup must never be interpreted."""
    code = _table_code()
    assert "innerHTML" not in code
    assert "outerHTML" not in code
    assert "insertAdjacentHTML" not in code


def test_dynamic_innerhtml_elsewhere_is_only_the_known_telemetry_strip():
    """Pins the exception so a new one cannot be added unnoticed."""
    page = web_ui.HTML_PAGE
    dynamic = [line.strip() for line in page.split("\n")
               if "innerHTML" in line and "//" not in line
               and not re.search(r"innerHTML = ('' *|'<option[^']*')\s*;", line)]
    assert dynamic == ["gpuStrip.innerHTML = html;"], \
        f"unexpected dynamic innerHTML: {dynamic}"


def test_cells_are_set_with_textcontent():
    script = _script()
    assert "th.textContent = cell" in script
    assert "td.textContent =" in script


def test_table_regexes_are_valid_and_match_real_rows():
    """The regexes are re-implemented here in Python to prove the *evaluated*
    patterns match the rows the model actually emits."""
    script = _script()
    row = re.search(r"const TABLE_ROW = /(.+?)/;", script).group(1)
    sep = re.search(r"const TABLE_SEP = /(.+?)/;", script).group(1)

    row_re = re.compile(row)
    sep_re = re.compile(sep)

    assert row_re.match("| MCP-LLM | vmx-22 | 12352 |")
    assert row_re.match("| Name | Hardware |")
    assert sep_re.match("|---|---|")
    assert sep_re.match("| :--- | ---: |")
    assert not sep_re.match("| MCP-LLM | vmx-22 |")
    assert not row_re.match("Just a sentence about vmx-22.")


def test_csv_escaping_handles_commas_quotes_and_newlines():
    """Guest OS strings contain commas — "Microsoft Windows Server 2022 (64-bit)"
    is fine, but "Red Hat Enterprise Linux 9, x64" would break a naive join."""
    script = _script()
    assert 'replace(/"/g, \'""\')' in script, "quotes must be doubled, per RFC 4180"
    assert "'\\uFEFF'" in script, "a BOM is needed for Excel to read UTF-8 names"
    assert "text/csv;charset=utf-8" in script


def test_csv_download_revokes_the_object_url():
    script = _script()
    assert "URL.createObjectURL" in script
    assert "URL.revokeObjectURL" in script, "leaking blob URLs on every export"


def test_header_and_separator_without_rows_is_not_a_table():
    script = _script()
    assert "if (rows.length) {" in script, \
        "an empty table should fall through to text rather than render a frame"


def test_table_styles_exist():
    page = web_ui.HTML_PAGE
    for selector in (".table-block table", ".table-block th, .table-block td",
                     ".csv-button"):
        assert selector in page, f"missing style for {selector}"


def test_prompt_asks_for_tables_and_forbids_pattern_claims():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))
    import orchestrator as o

    assert "Markdown table" in o.ENGINEER_RULES
    assert "follow the same pattern" in o.ENGINEER_RULES, \
        "the model claimed 30 unlisted VMs followed the same pattern"


# --- Executing the real JavaScript -------------------------------------------
#
# esprima proves the script parses; it does not prove the parser works. dukpy
# runs the actual table-rendering code against a minimal DOM stub, so these
# tests exercise what the browser will execute rather than a Python port of it.

dukpy = pytest.importorskip("dukpy")

DOM_STUB = """
function El(tag) {
    this.tag = tag; this.children = []; this.className = '';
    this.textContent = ''; this.value = ''; this.title = ''; this.href = '';
}
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.removeChild = function () {};
El.prototype.click = function () {};
El.prototype.addEventListener = function (name, fn) { this.onclick = fn; };
var document = {
    body: new El('body'),
    createElement: function (t) { return new El(t); },
    createTextNode: function (t) { var n = new El('#text'); n.textContent = t; return n; }
};
var capturedCsv = null;
var Blob = function (parts) { capturedCsv = parts.join(''); };
var URL = { createObjectURL: function () { return 'blob:x'; }, revokeObjectURL: function () {} };
"""


def _run(js_tail):
    """Run the real table code plus a snippet, returning its JSON result."""
    return dukpy.evaljs(DOM_STUB + _table_js() + "\n" + js_tail)


def _table_js():
    script = _script()
    start = script.index("const TABLE_ROW")
    end = script.index("function addMessage")
    return script[start:end]


def test_renderbody_separates_prose_from_a_table():
    result = _run("""
        var c = new El('div');
        renderBody(c, 'Here are the VMs:\\n| Name | HW |\\n|---|---|\\n| vc01 | vmx-10 |\\n| esx3 | vmx-21 |\\nThat is all.');
        JSON.stringify(c.children.map(function (k) { return k.tag; }));
    """)
    assert result == '["#text","div","#text"]', \
        "expected text, then a table block, then trailing text"


def test_rendered_table_has_the_right_cells():
    result = _run("""
        var c = new El('div');
        renderBody(c, '| Name | HW |\\n|---|---|\\n| vc01 | vmx-10 |\\n| esx3 | vmx-21 |');
        var table = c.children[0].children[1];
        var body = table.children[1];
        JSON.stringify(body.children.map(function (tr) {
            return tr.children.map(function (td) { return td.textContent; });
        }));
    """)
    assert result == '[["vc01","vmx-10"],["esx3","vmx-21"]]'


def test_row_count_is_reported():
    result = _run("""
        var c = new El('div');
        renderBody(c, '| A |\\n|---|\\n| 1 |\\n| 2 |\\n| 3 |');
        c.children[0].children[0].children[1].textContent;
    """)
    assert result == "3 rows"


def test_prose_containing_a_pipe_is_not_turned_into_a_table():
    result = _run("""
        var c = new El('div');
        renderBody(c, 'Use vcenter_vm_versions | it returns counts');
        JSON.stringify(c.children.map(function (k) { return k.tag; }));
    """)
    assert result == '["#text"]'


def test_ragged_row_is_padded_not_dropped():
    """A short row must not silently lose its columns."""
    result = _run("""
        var c = new El('div');
        renderBody(c, '| A | B | C |\\n|---|---|---|\\n| 1 | 2 |');
        var body = c.children[0].children[1].children[1];
        JSON.stringify(body.children[0].children.map(function (td) { return td.textContent; }));
    """)
    assert result == '["1","2",""]'


def test_csv_output_is_rfc4180():
    result = _run("""
        toCsv(['Name', 'Guest OS'],
              [['vm1', 'Red Hat Enterprise Linux 9, x64'],
               ['vm2', 'a "quoted" name']]);
    """)
    assert result == (
        'Name,Guest OS\r\n'
        'vm1,"Red Hat Enterprise Linux 9, x64"\r\n'
        'vm2,"a ""quoted"" name"'
    )


def test_csv_download_produces_a_bom_and_the_rows():
    result = _run("""
        downloadCsv(['Name'], [['vc01']], 1);
        capturedCsv;
    """)
    assert result.startswith("\ufeff"), "Excel needs a BOM to read UTF-8"
    assert "vc01" in result


def test_two_tables_in_one_answer_both_render():
    result = _run("""
        var c = new El('div');
        renderBody(c, '| A |\\n|---|\\n| 1 |\\n\\ntext\\n\\n| B |\\n|---|\\n| 2 |');
        JSON.stringify(c.children.map(function (k) { return k.tag; }));
    """)
    assert result == '["div","#text","div"]'
