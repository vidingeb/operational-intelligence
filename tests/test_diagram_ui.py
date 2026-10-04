"""Execute the actual custom UI renderer; an image token must become an img."""

import json
import re

import pytest
from fastapi.testclient import TestClient

esprima = pytest.importorskip("esprima")
dukpy = pytest.importorskip("dukpy")

import web_ui
from test_table_rendering import DOM_STUB, _script


PUBLIC = "https://assistant.example.ts.net/diagrams"
IMAGE = "0123456789abcdef.png"

DOM_IMAGES = """
El.prototype.appendChild = function (child) {
    child.parentNode = this; this.children.push(child); return child;
};
El.prototype.addEventListener = function (name, callback) {
    this.events = this.events || {};
    this.events[name] = this.events[name] || [];
    this.events[name].push(callback);
};
El.prototype.fire = function (name) {
    (this.events && this.events[name] || []).forEach(function (fn) { fn(); });
};
El.prototype.querySelectorAll = function (selector) {
    var found = [];
    this.children.forEach(function (child) {
        if ((selector === 'img.diagram-image' && child.tag === 'img' &&
             child.className === 'diagram-image') ||
            (selector === '.diagram-status' && child.className === 'diagram-status')) {
            found.push(child);
        }
        found = found.concat(child.querySelectorAll(selector));
    });
    return found;
};
El.prototype.querySelector = function (selector) {
    return this.querySelectorAll(selector)[0] || null;
};
"""


def run(tail, include_messages=False):
    script = _script().replace("{{DIAGRAM_PUBLIC_BASE}}", PUBLIC)
    start = script.index("const TABLE_ROW")
    end = script.index("// --- pending write confirmation") if include_messages else script.index("function addMessage")
    return dukpy.evaljs(DOM_STUB + DOM_IMAGES + script[start:end] + "\n" + tail)


@pytest.mark.parametrize("url", [f"{PUBLIC}/{IMAGE}", f"/diagrams/{IMAGE}"])
def test_generated_diagram_becomes_responsive_image_with_open_download_controls(url):
    result = json.loads(run("""
        var c = new El('div');
        renderBody(c, %s);
        var figure = c.children[0];
        var image = figure.children[0];
        var controls = figure.children[2].children;
        JSON.stringify([figure.tag, image.tag, image.src, image.alt,
                        controls[0].href, controls[0].rel, controls[1].download]);
    """ % json.dumps(f"![Measured infrastructure sketch]({url})")))
    assert result == ["figure", "img", url, "Measured infrastructure sketch",
                      url, "noopener noreferrer", IMAGE]
    assert ".diagram-image { display: block; max-width: 100%; height: auto;" in web_ui.HTML_PAGE


@pytest.mark.parametrize("url", [
    "javascript:alert", "data:image/svg+xml;base64,PHN2Zz4=", "//evil.example/diagrams/" + IMAGE,
    "https://evil.example/diagrams/" + IMAGE, PUBLIC + "/../secrets",
    PUBLIC + "/" + IMAGE + "?export=1", PUBLIC + "/" + IMAGE + "#fragment",
    PUBLIC + "/%2e%2e/" + IMAGE, PUBLIC + "/../../" + IMAGE,
    PUBLIC.replace("https://", "https://user@") + "/" + IMAGE,
    PUBLIC.replace("https://", "http://") + "/" + IMAGE,
    PUBLIC.replace("example.ts.net", "example.ts.net.evil.example") + "/" + IMAGE,
])
def test_unsafe_image_urls_are_blocked_without_creating_fetchable_elements(url):
    result = json.loads(run("""
        var c = new El('div');
        renderBody(c, %s);
        JSON.stringify([c.querySelectorAll('img.diagram-image').length, textOf(c)]);
    """ % json.dumps(f"![diagram]({url})")))
    assert result[0] == 0
    assert "blocked" in result[1]


def test_failed_image_load_is_visible_not_a_blank_success():
    result = json.loads(run("""
        var block = buildDiagram('diagram', '/diagrams/%s');
        block.children[0].fire('error');
        JSON.stringify([block.children[0].hidden, block.children[1].hidden,
                        block.children[1].textContent]);
    """ % IMAGE))
    assert result[:2] == [True, False]
    assert "failed to load" in result[2]


def test_image_load_clears_loading_status():
    assert run("""
        var block = buildDiagram('diagram', '/diagrams/%s');
        block.children[0].fire('load');
        block.children[1].hidden;
    """ % IMAGE) is True


def test_mermaid_is_collapsed_text_not_executed_or_large_default_block():
    text = 'Before\n```mermaid\nflowchart LR\n a["<script>"]\n' \
           f"![not an image]({PUBLIC}/{IMAGE})\n```\n\n![diagram]({PUBLIC}/{IMAGE})"
    result = json.loads(run("""
        var c = new El('div');
        renderBody(c, %s);
        var details = c.children[1];
        JSON.stringify([c.children.map(function (n) { return n.tag; }),
                        !!details.open, textOf(details),
                        c.querySelectorAll('img.diagram-image').length]);
    """ % json.dumps(text)))
    assert result[0] == ["p", "details", "figure"]
    assert result[1] is False
    assert '<script>' in result[2]
    assert result[3] == 1


def test_escaped_labels_display_exact_text_without_creating_html():
    result = json.loads(run("""
        var c = new El('div');
        renderBody(c, '| VM |\\n|---|\\n| &lt;script&gt;&#124;&#96;&#128008; |');
        JSON.stringify([textOf(c), flatten(c)]);
    """))
    assert "<script>|`🐈" in result[0]
    assert all(not item.startswith("script") for item in result[1])


def test_live_and_stored_assistant_messages_use_the_same_renderer():
    # Stored history calls addMessage without a model; it used to skip rendering.
    result = json.loads(run("""
        var chatContainer = new El('div');
        var lastQuestion = 'diagram';
        function buildUsageBar() { return null; }
        function buildConfirmBox() { return new El('div'); }
        var live = addMessage(%s, 'assistant', 'gpt-oss:120b', {});
        var stored = addMessage(%s, 'assistant');
        JSON.stringify([live.querySelectorAll('img.diagram-image').length,
                        stored.querySelectorAll('img.diagram-image').length]);
    """ % (json.dumps(f"![diagram]({PUBLIC}/{IMAGE})"),
           json.dumps(f"![diagram]({PUBLIC}/{IMAGE})")), include_messages=True))
    assert result == [1, 1]


def test_pdf_waits_for_image_before_printing():
    result = json.loads(run("""
        var root = buildDiagram('diagram', '/diagrams/%s');
        var timers = [];
        var didPrint = false;
        printWhenImagesReady(root, function () { didPrint = true; },
                             function (fn) { timers.push(fn); });
        var before = didPrint;
        root.children[0].fire('load');
        timers[0]();
        JSON.stringify([before, didPrint, root.children[1].hidden]);
    """ % IMAGE))
    assert result == [False, True, True]


def test_pdf_timeout_prints_an_explicit_image_error():
    result = json.loads(run("""
        var root = buildDiagram('diagram', '/diagrams/%s');
        var timers = [];
        var count = 0;
        printWhenImagesReady(root, function () { count++; },
                             function (fn) { timers.push(fn); });
        timers[0]();
        root.children[0].fire('error');
        JSON.stringify([count, root.children[0].hidden,
                        root.children[1].hidden, root.children[1].textContent]);
    """ % IMAGE))
    assert result[:3] == [1, True, False]
    # The UI handler can replace the PDF handler's text, but neither is silent.
    assert "failed to load" in result[3]


def test_print_css_contains_images_but_hides_source_and_controls():
    css = run("PRINT_CSS;")
    assert ".diagram-image { max-width: 100%; height: auto; }" in css
    assert ".diagram-source" in css and ".diagram-tools" in css
    assert "Diagram unavailable in PDF" in web_ui.HTML_PAGE


@pytest.mark.parametrize("configured", [PUBLIC, '</script><script>alert("unsafe")</script>'])
def test_page_receives_trusted_origin_from_config_with_script_safe_escaping(monkeypatch, configured):
    class Response:
        def json(self):
            return {"diagram_public_url_base": configured}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            assert url.endswith("/config")
            return Response()

    monkeypatch.setattr(web_ui.httpx, "AsyncClient", Client)
    client = TestClient(web_ui.app, client=("127.0.0.1", 5000))
    page = client.get("/", headers={"Tailscale-User-Login": "user@example.com"}).text
    encoded = json.dumps(configured)[1:-1].replace("<", "\\u003c").replace(">", "\\u003e")
    assert f'const DIAGRAM_PUBLIC_BASE = "{encoded}";' in page
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    assert len(scripts) == 1
    esprima.parseScript(scripts[0])
