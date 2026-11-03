"""Full-inventory agent prompt + email disable aliasing (Task 2 Phase 2B).

1. Prefix stability: the system-prompt prose must NOT depend on the per-turn
   tool-RAG selection — identical (disabled, needs_admin, compact) inputs
   produce byte-identical prompts, so the provider-visible request prefix is
   stable across turns and upstream prompt caches (OpenCode Go) can hit.
   Measured motivation: ~21s prefill every turn because the tailored system
   message changed turn-to-turn.

2. Email disable aliasing: the Tools settings tab writes bare email tool
   names (``send_email``) into the global disabled list, while the request
   ships qualified schemas (``mcp__email__send_email``). The bare disable must
   remove the qualified schema/prose too — not leave it visible-but-blocked.
"""
from pathlib import Path

import src.agent_loop as al


# ── 1. Prefix stability ────────────────────────────────────────────────── #

def _base(disabled=(), needs_admin=True, relevant_tools=None):
    return al._build_base_prompt(
        set(disabled), None, needs_admin,
        relevant_tools=relevant_tools,
        suppress_skills=True,
        suppress_local_context=True,
    )[0]


def test_build_base_prompt_ignores_relevant_tools_selection():
    web = _base(relevant_tools={"web_search"})
    mail = _base(relevant_tools={"manage_calendar", "send_email", "mcp__email__read_email"})
    no_rag = _base(relevant_tools=None)

    assert web == mail == no_rag


def test_build_base_prompt_non_admin_matches_no_rag_fallback():
    rag = _base(needs_admin=False, relevant_tools={"manage_notes"})
    fallback = _base(needs_admin=False, relevant_tools=None)

    assert rag == fallback


def test_build_base_prompt_still_filters_disabled_tools():
    enabled = _base(needs_admin=False)
    disabled = _base(disabled={"web_search"}, needs_admin=False)

    assert "```web_search" in enabled
    assert "```web_search" not in disabled


def test_build_base_prompt_filters_disabled_for_admin_too():
    # The old admin fallback returned the precomputed AGENT_SYSTEM_PROMPT,
    # which ignored `disabled` — a disabled tool's section still shipped in
    # the prose. Fresh composition must filter it.
    disabled = _base(disabled={"todowrite"}, needs_admin=True)

    assert "```todowrite" not in disabled


def test_build_system_prompt_cache_key_ignores_relevant_tools(monkeypatch):
    monkeypatch.setattr(al, "set_active_model", lambda model: None)
    monkeypatch.setattr(al, "get_builtin_overrides", lambda: {})
    monkeypatch.setattr(al, "_cached_base_prompt", None)
    monkeypatch.setattr(al, "_cached_base_prompt_key", None)

    def build(relevant_tools):
        messages, _ = al._build_system_prompt(
            [{"role": "user", "content": "hi"}],
            model="gpt-oss-120b",
            active_document=None,
            mcp_mgr=None,
            relevant_tools=relevant_tools,
            suppress_skills=True,
        )
        return "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )

    first = build({"web_search"})
    second = build({"manage_calendar", "send_email"})

    assert first == second


def test_local_computer_rules_move_out_of_system_role(monkeypatch):
    # The rules assert a per-turn fact ("the user referred to this computer"),
    # so they keep their selection gate — but must ship as a late user-role
    # message, never merged into the byte-stable system prompt.
    monkeypatch.setattr(al, "set_active_model", lambda model: None)
    monkeypatch.setattr(al, "get_builtin_overrides", lambda: {})
    monkeypatch.setattr(al, "_cached_base_prompt", None)
    monkeypatch.setattr(al, "_cached_base_prompt_key", None)

    messages, _ = al._build_system_prompt(
        [{"role": "user", "content": "check this computer"}],
        model="gpt-oss-120b",
        active_document=None,
        mcp_mgr=None,
        relevant_tools={"bash", "read_file"},
        suppress_skills=True,
    )

    system_text = "\n".join(
        str(m.get("content") or "") for m in messages if m.get("role") == "system"
    )
    user_text = "\n".join(
        str(m.get("content") or "") for m in messages if m.get("role") == "user"
    )

    assert "local-machine mode" not in system_text
    assert "local-machine mode" in user_text


def test_email_format_block_is_availability_gated(monkeypatch):
    # Generic how-to-format guidance: present whenever email-capable tools
    # are available, gone when they are all disabled — never selection-gated.
    from src.tool_security import BUILTIN_EMAIL_TOOLS

    monkeypatch.setattr(al, "set_active_model", lambda model: None)
    monkeypatch.setattr(al, "get_builtin_overrides", lambda: {})
    monkeypatch.setattr(al, "_cached_base_prompt", None)
    monkeypatch.setattr(al, "_cached_base_prompt_key", None)

    def build(disabled):
        messages, _ = al._build_system_prompt(
            [{"role": "user", "content": "draft an email"}],
            model="gpt-oss-120b",
            active_document=None,
            mcp_mgr=None,
            disabled_tools=disabled,
            relevant_tools={"send_email"},
            suppress_skills=True,
        )
        return "\n".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )

    assert "EMAIL DOCUMENT FORMAT" in build(set())

    all_email_disabled = al._expand_disabled_email_aliases(
        set(BUILTIN_EMAIL_TOOLS) | {"resolve_contact", "ui_control"}
    )
    assert "EMAIL DOCUMENT FORMAT" not in build(all_email_disabled)


# ── 2. Email disable aliasing ──────────────────────────────────────────── #

def test_expand_disabled_email_aliases_bare_to_qualified():
    out = al._expand_disabled_email_aliases({"send_email", "web_search"})

    assert "mcp__email__send_email" in out
    assert "web_search" in out
    assert "mcp__email__read_email" not in out


def test_expand_disabled_email_aliases_qualified_to_bare():
    out = al._expand_disabled_email_aliases({"mcp__email__read_email"})

    assert "read_email" in out
    assert "mcp__email__read_email" in out


def test_expand_disabled_email_aliases_empty_inputs():
    assert al._expand_disabled_email_aliases(None) == set()
    assert al._expand_disabled_email_aliases(set()) == set()


def test_disabled_qualified_email_name_drops_bare_section_from_prompt():
    # End-to-end through the loop's entry expansion: a qualified disable (or
    # an expanded bare one) must remove the tool's prose section too.
    disabled = al._expand_disabled_email_aliases({"mcp__email__send_email"})
    prompt = al._build_base_prompt(
        disabled, None, True,
        relevant_tools=None,
        suppress_skills=True,
        suppress_local_context=True,
    )[0]

    assert "```send_email" not in prompt


def test_stream_agent_loop_expands_email_aliases_at_entry():
    # The expansion must be applied where the loop normalizes disabled_tools,
    # so every downstream exact-match filter (schema surface, prose) sees the
    # qualified names. Source-level guard, mirroring the repo's existing
    # static-check pattern.
    source = (Path(__file__).resolve().parents[1] / "src" / "agent_loop.py").read_text()
    entry_norm = source.index("disabled_tools = set(disabled_tools or ())")
    expansion = source.index("_expand_disabled_email_aliases(disabled_tools)", entry_norm)

    assert expansion - entry_norm < 400


# ── 3. No per-turn tool recommendations ────────────────────────────────── #

class _FakeMcp:
    def __init__(self):
        self.seen_allowed_names = []

    def get_all_tools(self, disabled_map=None):
        return [
            {"server_id": "email", "server_name": "Built-in: Email",
             "name": "send_email", "qualified_name": "mcp__email__send_email",
             "description": "Send an email", "input_schema": {}, "is_disabled": False},
            {"server_id": "email", "server_name": "Built-in: Email",
             "name": "read_email", "qualified_name": "mcp__email__read_email",
             "description": "Read an email", "input_schema": {}, "is_disabled": False},
        ]

    def get_all_openai_schemas(self, disabled_map=None):
        return []

    def get_tool_descriptions_for_prompt(self, disabled_map=None, allowed_names=None):
        self.seen_allowed_names.append(allowed_names)
        return "MCP CATALOG mcp__email__send_email mcp__email__read_email"


def test_mcp_catalog_describes_all_enabled_mcp_tools(monkeypatch):
    # User decision 2026-11-03: no per-turn tool recommendations. The MCP
    # prose catalog must describe EVERY enabled MCP tool regardless of the
    # per-turn selection (and must not be gated on the selection containing
    # mcp__ names, as it used to be).
    monkeypatch.setattr(al, "set_active_model", lambda model: None)
    monkeypatch.setattr(al, "get_builtin_overrides", lambda: {})
    monkeypatch.setattr(al, "_cached_base_prompt", None)
    monkeypatch.setattr(al, "_cached_base_prompt_key", None)

    fake_mcp = _FakeMcp()
    messages, _ = al._build_system_prompt(
        [{"role": "user", "content": "hi"}],
        model="gpt-oss-120b",
        active_document=None,
        mcp_mgr=fake_mcp,
        relevant_tools={"web_search"},  # no mcp__ names, small selection
        suppress_skills=True,
    )

    catalog = [m for m in messages if "MCP CATALOG" in str(m.get("content") or "")]
    assert catalog, "catalog must be injected regardless of the selection"
    assert "mcp__email__send_email" in catalog[0]["content"]
    assert "mcp__email__read_email" in catalog[0]["content"]
    assert fake_mcp.seen_allowed_names == [None]


def test_full_surface_skips_similarity_retrieval(monkeypatch):
    import asyncio

    import src.agent_loop as agent_loop

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(
        agent_loop, "_configured_model_tool_surface",
        lambda *args, **kwargs: "full",
    )

    async def fake_stream(_candidates, _messages, **kwargs):
        yield 'data: {"delta": "complete"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream)

    calls = []

    def fail_get_tool_index():
        calls.append(1)
        raise AssertionError("similarity retrieval must be skipped on full surface")

    monkeypatch.setattr("src.tool_index.get_tool_index", fail_get_tool_index)

    async def run():
        chunks = []
        async for chunk in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "policy-model",
            [{"role": "user", "content": "Inspect the current state."}],
            max_rounds=1,
            owner="pewds",
            fallbacks=[],
            fallback_on_empty=False,
            _is_teacher_run=True,
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(run())
    assert any("complete" in chunk for chunk in chunks)
    assert calls == []
