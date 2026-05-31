"""Tool/function-calling and multimodal (vision) passthrough.

These exercise the gateway's wire contract: tool params and image content must
survive the request schema and reach litellm, tool-call results must survive in
the response, and client-supplied credential overrides must never be forwarded.
litellm itself is stubbed, so no network and no provider keys are needed.
"""
from __future__ import annotations

import app.routing as routing
from app.engine import _litellm_kwargs
from app.schemas import ChatCompletionRequest

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]

IMAGE_MESSAGE = {
    "role": "user",
    "content": [
        {"type": "text", "text": "What is in this image?"},
        {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
    ],
}


# --- Unit: request schema + engine kwargs ---------------------------------
def test_tools_params_forwarded_to_litellm():
    req = ChatCompletionRequest(
        model="gpt-4o",
        messages=[{"role": "user", "content": "weather in Bern?"}],
        tools=TOOLS,
        tool_choice="auto",
    )
    kwargs = _litellm_kwargs(req, "gpt-4o", 30.0)
    assert kwargs["tools"] == TOOLS
    assert kwargs["tool_choice"] == "auto"


def test_credential_overrides_never_forwarded():
    # A malicious client tries to redirect the call to its own endpoint/key.
    req = ChatCompletionRequest(
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        api_base="https://attacker.example",
        api_key="sk-attacker",
        custom_llm_provider="openai",
    )
    kwargs = _litellm_kwargs(req, "gpt-4o", 30.0)
    for forbidden in ("api_base", "api_key", "custom_llm_provider", "base_url"):
        assert forbidden not in kwargs


def test_assistant_tool_calls_survive_schema():
    # Multi-turn: assistant emits a tool call, client echoes it back next turn.
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city": "Bern"}'},
    }
    req = ChatCompletionRequest(
        model="gpt-4o",
        messages=[
            {"role": "user", "content": "weather in Bern?"},
            {"role": "assistant", "content": None, "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call_1", "content": "12C, sunny"},
        ],
    )
    msgs = _litellm_kwargs(req, "gpt-4o", 30.0)["messages"]
    assert msgs[1]["tool_calls"] == [tool_call]
    assert msgs[2]["tool_call_id"] == "call_1"


def test_image_content_survives_schema():
    req = ChatCompletionRequest(model="gpt-4o", messages=[IMAGE_MESSAGE])
    content = _litellm_kwargs(req, "gpt-4o", 30.0)["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "https://example.com/cat.png"


# --- End-to-end through the route (litellm stubbed) -----------------------
async def test_chat_returns_tool_calls(client, api_key, monkeypatch):
    captured = {}

    async def fake_acomplete(request, model, timeout):
        captured["tools"] = _litellm_kwargs(request, model, timeout).get("tools")
        return {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "Bern"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "weather in Bern?"}],
            "tools": TOOLS,
            "tool_choice": "auto",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The gateway forwarded the tools...
    assert captured["tools"] == TOOLS
    # ...and passed the provider's tool_calls back to the client unchanged.
    msg = body["choices"][0]["message"]
    assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
    assert body["choices"][0]["finish_reason"] == "tool_calls"


async def test_chat_accepts_image_content(client, api_key, monkeypatch):
    seen = {}

    async def fake_acomplete(request, model, timeout):
        seen["content"] = _litellm_kwargs(request, model, timeout)["messages"][0][
            "content"
        ]
        return {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "a cat"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(routing, "acomplete", fake_acomplete)
    monkeypatch.setattr(routing, "cost_of", lambda m, p, c: 0.0)

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o", "messages": [IMAGE_MESSAGE]},
    )
    assert resp.status_code == 200, resp.text
    # The image part reached the provider intact.
    assert seen["content"][1]["image_url"]["url"] == "https://example.com/cat.png"
    assert resp.json()["choices"][0]["message"]["content"] == "a cat"
