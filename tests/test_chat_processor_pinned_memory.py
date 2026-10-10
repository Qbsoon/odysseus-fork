from types import SimpleNamespace

from src.chat_processor import ChatProcessor


class _Memory:
    def __init__(self, rows):
        self.rows = rows
        self.incremented = []

    def load(self, owner=None):
        return list(self.rows)

    def increment_uses(self, ids):
        self.incremented.extend(ids)


class _Docs:
    rag_manager = None


def _context_text(preface):
    return "\n".join(m.get("content", "") for m in preface)


def _processor(rows):
    return ChatProcessor(memory_manager=_Memory(rows), personal_docs_manager=_Docs())


def test_every_pinned_memory_is_injected_regardless_of_relevance():
    rows = [
        {
            "id": "identity",
            "text": "User's name is Felix.",
            "category": "identity",
            "pinned": True,
            "timestamp": 3,
        },
        {
            "id": "party",
            "text": "User is planning a birthday party with sack races.",
            "category": "fact",
            "pinned": True,
            "timestamp": 2,
        },
        {
            "id": "efficiency",
            "text": "User wants efficient, concise answers with no human-like filler.",
            "category": "preference",
            "pinned": True,
            "timestamp": 1,
        },
    ]

    preface, _, _ = _processor(rows).build_context_preface(
        message="Explain how Python decorators work",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=True,
    )

    text = _context_text(preface)
    assert "User's name is Felix." in text
    assert "birthday party with sack races" in text
    assert "efficient, concise answers" in text


def test_pinned_memories_are_injected_for_memory_management_requests():
    rows = [
        {
            "id": "efficiency",
            "text": "User wants efficient, concise answers.",
            "category": "preference",
            "pinned": True,
            "timestamp": 1,
        },
        {
            "id": "coffee",
            "text": "User likes dark roast coffee.",
            "category": "preference",
            "pinned": False,
            "timestamp": 2,
        },
    ]

    processor = _processor(rows)
    preface, _, _ = processor.build_context_preface(
        message="list my saved memories",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=True,
    )

    text = _context_text(preface)
    assert "efficient, concise answers" in text
    # Recalled (unpinned) memories are still skipped for management commands.
    assert "dark roast coffee" not in text
    assert [m["type"] for m in processor._last_used_memories] == ["pinned"]


def test_unpinned_memories_still_go_through_relevance_retrieval():
    rows = [
        {
            "id": "coffee",
            "text": "User likes dark roast coffee.",
            "category": "preference",
            "pinned": False,
            "timestamp": 1,
        },
        {
            "id": "party",
            "text": "User is planning a birthday party with sack races.",
            "category": "fact",
            "pinned": False,
            "timestamp": 2,
        },
    ]

    preface, _, _ = _processor(rows).build_context_preface(
        message="likes coffee roast",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=True,
    )

    text = _context_text(preface)
    assert "User likes dark roast coffee." in text
    assert "birthday party with sack races" not in text


def test_unpinned_recall_keeps_its_own_budget_beside_many_pinned():
    rows = [
        {
            "id": f"pinned-{idx}",
            "text": f"User pinned fact {idx}.",
            "category": "fact",
            "pinned": True,
            "timestamp": idx,
        }
        for idx in range(8)
    ]
    rows.extend([
        {
            "id": f"coffee-{idx}",
            "text": f"User likes coffee roast {idx}.",
            "category": "preference",
            "pinned": False,
            "timestamp": idx,
        }
        for idx in range(6)
    ])

    processor = _processor(rows)
    processor.build_context_preface(
        message="likes coffee roast",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=True,
    )

    types = [m["type"] for m in processor._last_used_memories]
    assert types.count("pinned") == 8
    # Full recall budget (MEMORY_CONTEXT_LIMIT) despite 8 pinned memories.
    assert types.count("recalled") == 5


def test_pinned_injection_is_bounded_by_sanity_limit():
    rows = [
        {
            "id": f"pinned-{idx}",
            "text": f"User pinned fact {idx}.",
            "category": "fact",
            "pinned": True,
            "timestamp": idx,
        }
        for idx in range(60)
    ]

    processor = _processor(rows)
    processor.build_context_preface(
        message="Who is the user?",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=True,
    )

    assert len(processor._last_used_memories) == ChatProcessor.PINNED_MEMORY_LIMIT


def test_explicit_memory_management_skips_recall_injection_only():
    rows = [{
        "id": "coffee",
        "text": "User likes dark roast coffee.",
        "category": "preference",
        "pinned": False,
        "timestamp": 1,
    }]

    processor = _processor(rows)
    preface, _, _ = processor.build_context_preface(
        message="list my saved memories",
        session=SimpleNamespace(),
        use_rag=False,
        use_memory=True,
    )

    assert "dark roast coffee" not in _context_text(preface)
    assert processor._last_used_memories == []


def test_memory_management_detection_does_not_disable_normal_recall():
    assert ChatProcessor._is_memory_management_request("find my saved memories")
    assert ChatProcessor._is_memory_management_request("what do you remember about my nationality?")
    assert not ChatProcessor._is_memory_management_request("Explain how memory allocation works")
