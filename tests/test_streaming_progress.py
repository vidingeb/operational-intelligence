"""Live OpenAI SSE progress without contaminating assistant answer content."""
import asyncio
import json

import orchestrator as o


def _event(line):
    assert line.startswith("data: ")
    return json.loads(line.removeprefix("data: "))


def test_first_progress_frame_arrives_while_work_is_running():
    async def scenario():
        state = {"working": False}

        async def complete(progress):
            state["working"] = True
            await progress("selecting_tools", "Selecting and analyzing tools", {})
            await asyncio.sleep(0.02)
            state["working"] = False
            return o._openai_response("assistant-all", "final answer", {})

        stream = o._progress_sse_stream("assistant-all", complete)
        first = await anext(stream)
        assert state["working"] is True
        event = _event(first.strip())
        assert event["x_copilot_status"]["stage"] == "selecting_tools"
        assert event["choices"][0]["delta"]["reasoning_content"]
        await stream.aclose()

    asyncio.run(scenario())


def test_progress_precedes_content_and_never_becomes_answer_text():
    async def scenario():
        async def complete(progress):
            await progress("selecting_tools", "Selecting and analyzing tools", {})
            await progress(
                "executing_tool",
                "Executing networks_flow_inventory",
                {"tool": "networks_flow_inventory"},
            )
            await progress(
                "final_analysis",
                "Analyzing tool results and preparing the final answer",
                {},
            )
            return o._openai_response("assistant-all", "final answer", {})

        return [
            line.strip()
            async for line in o._progress_sse_stream("assistant-all", complete)
        ]

    lines = asyncio.run(scenario())
    assert lines[-1] == "data: [DONE]"
    events = [_event(line) for line in lines[:-1]]
    status_events = [event for event in events if "x_copilot_status" in event]
    content_events = [
        event for event in events
        if event.get("choices")
        and "content" in event["choices"][0].get("delta", {})
    ]

    assert [event["x_copilot_status"]["stage"] for event in status_events] == [
        "selecting_tools",
        "executing_tool",
        "final_analysis",
    ]
    assert [event["choices"][0]["delta"]["content"] for event in content_events] == [
        "final answer"
    ]
    assert all(
        "content" not in event["choices"][0]["delta"]
        for event in status_events
    )
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_legacy_completed_response_stream_remains_openai_compatible():
    payload = o._openai_response("assistant-all", "answer", {})
    lines = [line.strip() for line in o._sse_stream(payload)]
    events = [_event(line) for line in lines[:-1]]

    assert events[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert events[1]["choices"][0]["delta"] == {"content": "answer"}
    assert events[2]["choices"][0]["finish_reason"] == "stop"
    assert lines[-1] == "data: [DONE]"
