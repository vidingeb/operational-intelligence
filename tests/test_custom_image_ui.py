"""Image composer and transport for the custom on-prem UI."""

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "orchestrator"))

import web_ui  # noqa: E402


def client():
    return TestClient(
        web_ui.app,
        client=("127.0.0.1", 51000),
        headers={"Tailscale-User-Login": "vidingeb@github"},
    )


class Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "answer": "ok",
            "model": "test",
            "conversation_id": "conv-1",
            "history_turns": 0,
        }


def test_custom_ui_has_upload_paste_preview_remove_and_limits():
    page = web_ui.HTML_PAGE
    for marker in (
        'id="image-input"',
        'id="attachment-preview"',
        "document.addEventListener('paste'",
        "remove.addEventListener('click'",
        "const MAX_IMAGES = 4",
        "const MAX_IMAGE_BYTES = 8 * 1024 * 1024",
        "const MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024",
        "selectedImages = []",
        "Analyzing image before VMware checks",
    ):
        assert marker in page


def test_custom_ui_accepts_only_supported_image_types():
    page = web_ui.HTML_PAGE
    assert 'accept="image/png,image/jpeg,image/webp,image/gif"' in page
    assert "IMAGE_TYPES.has(file.type)" in page
    assert "Image not attached:" in page


def test_custom_ui_proxy_forwards_inline_images(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            seen["url"] = url
            seen["json"] = json
            return Response()

    monkeypatch.setattr(web_ui.httpx, "AsyncClient", FakeClient)
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="
    response = client().post(
        "/api/chat",
        json={
            "message": "",
            "model": "test",
            "scope": "all",
            "conversation_id": None,
            "images": [image],
        },
    )
    assert response.status_code == 200
    assert seen["url"].endswith("/chat")
    assert seen["json"]["images"] == [image]
    assert seen["json"]["message"] == ""


def test_custom_ui_keeps_images_after_failed_submission():
    script = web_ui.HTML_PAGE
    reset = script.index("selectedImages = [];", script.index("if (response.ok)"))
    error = script.index("} else {", reset)
    assert reset < error
