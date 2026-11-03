"""ODYSSEUS_DISABLE_EMAIL_MCP — kill the built-in email MCP server only.

The env gate must skip registering the email built-in server (its subprocess
never spawns, so no email tools exist anywhere in the app) while the other
built-ins (memory / rag / image_gen / builtin_browser) still register.
Module loaded in isolation like the other builtin_mcp tests — no real
servers or subprocesses are involved.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def _load_builtin_mcp(monkeypatch, env=None):
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)

    core = types.ModuleType("core")
    core.__path__ = []
    platform_compat = types.ModuleType("core.platform_compat")
    platform_compat.IS_WINDOWS = False
    platform_compat.which_tool = lambda name: None
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)

    spec = importlib.util.spec_from_file_location(
        "builtin_mcp_under_test",
        ROOT / "src" / "builtin_mcp.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeManager:
    def __init__(self):
        self.connected = []

    async def connect_server(self, server_id, name, transport, command, args, env):
        self.connected.append(server_id)
        return True

    async def hold_owned_connection(self, server_id):
        pass


async def _register(module, monkeypatch):
    """Run register_builtin_servers to completion with a fake manager."""
    manager = _FakeManager()
    spawned = []

    def fake_spawn(coro):
        spawned.append(coro)
        return coro

    monkeypatch.setattr(module, "_spawn_bg", fake_spawn)
    await module.register_builtin_servers(manager)
    for coro in spawned:
        await coro
    return manager


async def test_email_mcp_env_skips_email_server_only(monkeypatch):
    module = _load_builtin_mcp(
        monkeypatch, {"ODYSSEUS_DISABLE_EMAIL_MCP": "1"}
    )
    assert module.EMAIL_MCP_DISABLED is True

    manager = await _register(module, monkeypatch)

    assert "email" not in manager.connected
    # The other built-ins still register.
    assert "memory" in manager.connected
    assert "rag" in manager.connected
    assert "image_gen" in manager.connected


async def test_email_mcp_registers_by_default(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_DISABLE_EMAIL_MCP", raising=False)
    module = _load_builtin_mcp(monkeypatch)
    assert module.EMAIL_MCP_DISABLED is False

    manager = await _register(module, monkeypatch)

    assert "email" in manager.connected


async def test_email_mcp_env_does_not_affect_other_mcp_gates(monkeypatch):
    # The narrow gate must not trip the global MCP_DISABLED path.
    module = _load_builtin_mcp(
        monkeypatch,
        {"ODYSSEUS_DISABLE_EMAIL_MCP": "1"},
    )
    assert module.MCP_DISABLED is False
