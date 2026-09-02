"""Screenshots pasted into the chat.

The defect these start from: an attached image was discarded whenever any text
came with it, because the only image check ran in the "no text at all" branch.
Asked about a screenshot with a caption, the model answered having never been
told an image existed -- and its reply read as though it had looked and been
unable to see, which is a more convincing wrong answer than a blank one.

The second thing under test is the labelling. Measured on a network diagram,
qwen2.5vl:7b read every hostname correctly and three of eight IP addresses
wrongly, producing addresses indistinguishable in shape from the real ones. So
a transcription is a claim to be checked, never a reading, and the text that
says so has to travel with the content rather than live in documentation.
"""
import pytest
from fastapi.testclient import TestClient

import orchestrator as o

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="


@pytest.fixture
def client():
    return TestClient(o.app)


def user_turn(text, *parts):
    content = [{"type": "text", "text": text}] if text is not None else []
    return {"role": "user", "content": content + list(parts)}


def image_part(url=PNG):
    return {"type": "image_url", "image_url": {"url": url}}


@pytest.fixture
def captured(monkeypatch):
    """Capture the prompt the tool loop is actually given."""
    seen = {}

    async def fake_chat_with_tools(message, **kwargs):
        seen["message"] = message
        return {"answer": "ok", "tool_calls": [], "pending_actions": []}

    monkeypatch.setattr(o, "chat_with_tools", fake_chat_with_tools)
    return seen


# --- _extract_images ---------------------------------------------------------

def test_extract_images_takes_inline_data_urls():
    images = o._extract_images([{"type": "text", "text": "hi"}, image_part()])
    assert images == ["iVBORw0KGgoAAAANSUhEUg=="]


def test_extract_images_ignores_remote_urls():
    """Fetching one would have the orchestrator retrieve an arbitrary address."""
    assert o._extract_images([image_part("https://example.invalid/x.png")]) == []


def test_extract_images_handles_plain_string_url_form():
    assert o._extract_images([{"type": "image_url", "image_url": PNG}]) == [
        "iVBORw0KGgoAAAANSUhEUg=="
    ]


def test_extract_images_on_plain_string_content():
    assert o._extract_images("just text") == []


# --- the silent drop ---------------------------------------------------------

def test_image_with_caption_reaches_the_model(client, captured, monkeypatch):
    """The original bug: a caption made the image vanish without a trace."""
    async def fake_describe(images):
        assert images == ["iVBORw0KGgoAAAANSUhEUg=="]
        return "a diagram of five hosts"

    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    monkeypatch.setattr(o, "describe_images", fake_describe)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [user_turn("what is this?", image_part())]},
    )
    assert response.status_code == 200
    assert "what is this?" in captured["message"]
    assert "a diagram of five hosts" in captured["message"]


def test_transcription_is_labelled_unverified(client, captured, monkeypatch):
    async def fake_describe(images):
        return "esx01 10.0.0.101"

    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    monkeypatch.setattr(o, "describe_images", fake_describe)

    client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [user_turn("read this", image_part())]},
    )
    message = captured["message"]
    assert "not a measurement" in message
    assert "vl:test" in message


def test_vision_disabled_says_so_rather_than_dropping(client, captured, monkeypatch):
    monkeypatch.setattr(o, "VISION_MODEL", "")
    client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [user_turn("what is this?", image_part())]},
    )
    message = captured["message"]
    assert "cannot read it" in message
    assert "do not guess" in message


def test_vision_failure_does_not_fail_the_turn(client, captured, monkeypatch):
    """A dead vision model should cost the image, not the whole question."""
    async def boom(images):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    monkeypatch.setattr(o, "describe_images", boom)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [user_turn("check this", image_part())]},
    )
    assert response.status_code == 200
    assert "could not be read" in captured["message"]
    assert "RuntimeError" in captured["message"]
    assert "check this" in captured["message"]


def test_empty_transcription_is_reported(client, captured, monkeypatch):
    async def blank(images):
        return ""

    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    monkeypatch.setattr(o, "describe_images", blank)

    client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [user_turn("read this", image_part())]},
    )
    assert "returned nothing" in captured["message"]


def test_image_alone_without_caption_still_works(client, captured, monkeypatch):
    async def fake_describe(images):
        return "a login screen"

    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    monkeypatch.setattr(o, "describe_images", fake_describe)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [user_turn(None, image_part())]},
    )
    assert response.status_code == 200
    assert "a login screen" in captured["message"]


def test_remote_image_without_text_is_rejected_clearly(client, captured):
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "oi-all",
            "messages": [user_turn(None, image_part("https://example.invalid/x.png"))],
        },
    )
    assert response.status_code == 400
    assert "attach" in response.json()["detail"].lower()


def test_plain_text_turn_is_untouched(client, captured, monkeypatch):
    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    client.post(
        "/v1/chat/completions",
        json={"model": "oi-all", "messages": [{"role": "user", "content": "how many VMs?"}]},
    )
    assert captured["message"] == "how many VMs?"


# --- frame_screenshot --------------------------------------------------------

def test_frame_screenshot_counts_plural(monkeypatch):
    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    assert "2 screenshots" in o.frame_screenshot("x", 2)
    assert "a screenshot" in o.frame_screenshot("x", 1)


def test_frame_screenshot_keeps_the_description(monkeypatch):
    monkeypatch.setattr(o, "VISION_MODEL", "vl:test")
    assert o.frame_screenshot("esx01 10.0.0.101", 1).endswith("esx01 10.0.0.101")
