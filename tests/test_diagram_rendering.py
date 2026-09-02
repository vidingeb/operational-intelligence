"""Server-side diagram rendering.

Why this exists at all: asked for this estate's topology, FLUX.1-dev spent 54 s
on the GPU and produced boxes labelled "EXN03", "ESXt01" and "Magrmnt VSAN 01".
mermaid-cli typeset the same labels exactly, on the CPU, in 1.9 s. An image
model paints things that resemble letters; a renderer sets strings. So the
picture is produced by the renderer, and the tests below are about the wiring
around it rather than the drawing.

Two rules the wiring has to keep:

  - the Mermaid source stays in the answer. The chat client renders it inline
    anyway, so a broken renderer costs the download and not the diagram, and a
    wrong diagram can still be read and corrected. A picture cannot.
  - a render that fails, times out or cannot start returns the answer intact.
    A diagram is decoration on an answer that was already correct.
"""
import asyncio
import os

import pytest

import orchestrator as o

DIAGRAM = "```mermaid\ngraph TD\n    a --> b\n```"


@pytest.fixture
def rendered(monkeypatch, tmp_path):
    """Pretend the renderer works, and record what it was asked to draw."""
    seen = []

    async def fake_render(source):
        seen.append(source)
        return "0123456789abcdef.png"

    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "local/mermaid-cli:test")
    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    monkeypatch.setattr(o, "_render_one", fake_render)
    return seen


def run(coro):
    return asyncio.run(coro)


# --- render_diagrams ---------------------------------------------------------

def test_png_is_added_and_source_is_kept(rendered):
    out = run(o.render_diagrams(f"Here it is.\n\n{DIAGRAM}\n"))
    assert "```mermaid" in out
    assert "graph TD" in out
    assert "![Diagram](/diagrams/0123456789abcdef.png)" in out


def test_only_the_diagram_body_is_rendered(rendered):
    run(o.render_diagrams(DIAGRAM))
    assert rendered == ["graph TD\n    a --> b"]


def test_disabled_renderer_leaves_the_answer_alone(monkeypatch):
    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "")
    out = run(o.render_diagrams(DIAGRAM))
    assert out == DIAGRAM
    assert "![" not in out


def test_failed_render_leaves_the_answer_alone(monkeypatch, tmp_path):
    async def fails(source):
        return None

    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "local/mermaid-cli:test")
    monkeypatch.setattr(o, "_render_one", fails)
    out = run(o.render_diagrams(f"text\n\n{DIAGRAM}"))
    assert "```mermaid" in out
    assert "![" not in out


def test_other_languages_are_not_rendered(rendered):
    out = run(o.render_diagrams("```python\nprint('graph TD')\n```"))
    assert "![" not in out
    assert rendered == []


def test_two_diagrams_each_get_a_picture(rendered):
    out = run(o.render_diagrams(f"{DIAGRAM}\n\ntext\n\n```mermaid\ngraph LR\n    c --> d\n```"))
    assert out.count("![Diagram](") == 2
    assert len(rendered) == 2


def test_empty_diagram_is_skipped(rendered):
    out = run(o.render_diagrams("```mermaid\n```"))
    assert "![" not in out
    assert rendered == []


def test_prose_without_diagrams_is_unchanged(rendered):
    text = "There are 122 virtual machines."
    assert run(o.render_diagrams(text)) == text


# --- _render_one -------------------------------------------------------------

def test_render_names_by_content_hash(monkeypatch, tmp_path):
    """The same diagram twice must not render twice."""
    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "local/mermaid-cli:test")

    import hashlib
    digest = hashlib.sha256(b"graph TD").hexdigest()[:16]
    open(os.path.join(tmp_path, f"{digest}.png"), "wb").close()

    started = []

    async def should_not_run(*args, **kwargs):
        started.append(args)
        raise AssertionError("re-rendered a diagram that was already drawn")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", should_not_run)
    assert run(o._render_one("graph TD")) == f"{digest}.png"
    assert started == []


def test_render_survives_a_missing_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "local/mermaid-cli:test")

    async def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_docker)
    assert run(o._render_one("graph TD")) is None


def test_render_cleans_up_its_source_file(monkeypatch, tmp_path):
    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    monkeypatch.setattr(o, "DIAGRAM_IMAGE", "local/mermaid-cli:test")

    async def no_docker(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_docker)
    run(o._render_one("graph TD"))
    assert [f for f in os.listdir(tmp_path) if f.endswith(".mmd")] == []


# --- the serving route -------------------------------------------------------

def test_diagram_name_pattern_rejects_traversal():
    assert not o._DIAGRAM_NAME.match("../../etc/passwd")
    assert not o._DIAGRAM_NAME.match("0123456789abcdef.png/../x")
    assert not o._DIAGRAM_NAME.match("ZZZZ.png")
    assert o._DIAGRAM_NAME.match("0123456789abcdef.png")


def test_diagram_requires_proxy_identity(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    monkeypatch.setattr(o, "DIAGRAM_AUTH", "tailscale")
    open(os.path.join(tmp_path, "0123456789abcdef.png"), "wb").close()

    client = TestClient(o.app)
    assert client.get("/diagrams/0123456789abcdef.png").status_code == 403
    ok = client.get(
        "/diagrams/0123456789abcdef.png",
        headers={"Tailscale-User-Login": "someone@example.com"},
    )
    assert ok.status_code == 200


def test_unknown_diagram_is_404_not_500(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(o, "DIAGRAM_DIR", str(tmp_path))
    monkeypatch.setattr(o, "DIAGRAM_AUTH", "none")
    client = TestClient(o.app)
    assert client.get("/diagrams/0000000000000000.png").status_code == 404
