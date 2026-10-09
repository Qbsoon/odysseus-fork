"""OpenCode Go x-opencode-session header injection.

Go requires an x-opencode-session header per conversation (HTTP 400 without
it). OpenCode Zen is plain OpenAI-compatible and must not receive the header.
"""

import re

import pytest

from src.llm_core import _provider_headers


def test_go_gets_session_header():
    h = _provider_headers("opencode-go", session_id="sess-123")
    assert h["x-opencode-session"] == "sess-123"


@pytest.mark.parametrize(
    "provider",
    ["opencode-zen", "openai", "openrouter", "groq", "anthropic", "ollama"],
)
def test_no_session_header_for_zen_and_other_providers(provider):
    h = _provider_headers(provider, session_id="sess-123")
    assert "x-opencode-session" not in h


def test_session_less_go_call_gets_generated_fallback_id():
    value = _provider_headers("opencode-go")["x-opencode-session"]
    assert re.fullmatch(r"[0-9a-f]{32}", value)


def test_caller_supplied_header_wins():
    h = _provider_headers(
        "opencode-go",
        headers={"x-opencode-session": "explicit"},
        session_id="sess-123",
    )
    assert h["x-opencode-session"] == "explicit"
