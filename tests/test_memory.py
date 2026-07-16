import json

from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.memory import MemoryStore, MemoryType
from repo_agent.core.query_engine import QueryEngine
from repo_agent.core.tool import ToolRegistry
from repo_agent.tools.memory_tools import build_memory_tools


class FakeLLM:
    def __init__(self):
        self.messages_seen = None

    def invoke_with_tools(self, messages, tools):
        self.messages_seen = messages
        return LLMToolResponse(
            content=None,
            tool_calls=[ToolCall(id="call_1", name="finish", arguments=json.dumps({"answer": "完成"}))],
            raw_message={"tool_calls": [{"id": "call_1"}]},
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        )


def test_memory_store_saves_index_and_recalls(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    filename = store.save_memory(
        name="偏好中文简洁回复",
        description="用户喜欢中文且简洁的回复",
        type="user",
        content="用户希望默认中文回答，少写铺垫。",
    )

    assert filename.endswith(".md")
    assert "偏好中文简洁回复" in store.load_index()
    assert "Memory System" in store.build_prompt_section()
    recalled = store.recall("中文回复风格")
    assert recalled
    assert "默认中文回答" in recalled[0].content


def test_repair_recipe_memory_uses_shared_scope_and_type_filter(tmp_path):
    store = MemoryStore.for_scope("repo:owner/project", root=tmp_path)
    filename = store.save_memory(
        name="header rows roundtrip",
        description="header_rows table format must update writer reader roundtrip",
        type="repair_recipe",
        content="verification_contract: write, read, and round-trip header_rows.",
    )

    assert filename.startswith("repair_recipe_")
    assert "repair_recipe" in store.load_index()
    recalled = store.recall_repair_recipes("unexpected keyword header_rows reader")
    assert recalled
    assert recalled[0].header.type == MemoryType.REPAIR_RECIPE
    assert "round-trip" in recalled[0].content
    assert not store.search("header_rows", memory_type=MemoryType.FEEDBACK)

    updated = store.update_memory(
        filename,
        description="header_rows table format requires writer reader roundtrip verification",
        content="verification_contract: write, read, round-trip, and anti-pattern checks.",
    )

    assert updated == filename
    assert "anti-pattern checks" in store.read_memory(filename)
    assert "writer reader roundtrip" in store.load_index()
    assert store.similar_repair_recipes("header rows reader", "header_rows writer reader roundtrip")
    assert not store.similar_repair_recipes(
        "case insensitive parser tokens",
        "case-insensitive parser fixes must normalize regex classification and sentinel comparisons consistently",
    )


def test_memory_tools_save_and_read(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    registry = ToolRegistry()
    registry.register_many(build_memory_tools(store))

    save = registry.execute(
        "save_memory",
        {
            "name": "不要冗长总结",
            "description": "用户不喜欢结尾重复总结",
            "type": "feedback",
            "content": "Why: 用户希望节省时间。\nHow to apply: 最终回复只保留关键结果。",
        },
    )
    assert save.status == "success"

    filename = save.data["filename"]
    read = registry.execute("read_memory", {"filename": filename})
    assert "How to apply" in read.text


def test_memory_tools_save_repair_recipe_and_filter_search(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    registry = ToolRegistry()
    registry.register_many(build_memory_tools(store))

    save = registry.execute(
        "save_memory",
        {
            "name": "mutable buffer writeback",
            "description": "mutable buffer transformations must write back to original storage",
            "type": "repair_recipe",
            "content": "repair_strategy: assign transformed values back with target[:] = transformed.",
        },
    )
    assert save.status == "success"

    search = registry.execute("search_memories", {"query": "mutable buffer", "type": "repair_recipe"})
    assert search.status == "success"
    assert "mutable buffer writeback" in search.text

    duplicate = registry.execute(
        "save_memory",
        {
            "name": "mutable buffer storage update",
            "description": "mutable buffer transformations must write back to original array storage",
            "type": "repair_recipe",
            "content": "repair_strategy: use slice assignment to preserve original storage.",
        },
    )
    assert duplicate.status == "error"
    assert duplicate.error["code"] == "SIMILAR_REPAIR_RECIPE_EXISTS"
    assert "mutable buffer writeback" in duplicate.text

    filename = save.data["filename"]
    updated = registry.execute(
        "update_memory",
        {
            "filename": filename,
            "content": "repair_strategy: assign transformed values back with target[:] = transformed.\nanti_patterns: rebinding locals.",
        },
    )
    assert updated.status == "success"
    assert "rebinding locals" in registry.execute("read_memory", {"filename": filename}).text


def test_query_engine_injects_recalled_memory_and_saves_short_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    store.save_memory(
        name="pytest 验证偏好",
        description="用户希望涉及测试时说明 pytest 结果",
        type="feedback",
        content="Why: 用户看重验证闭环。\nHow to apply: 完成时写明 pytest 是否通过。",
    )
    registry = ToolRegistry()
    registry.register_function(lambda answer: answer, name="finish", description="Finish")
    llm = FakeLLM()
    engine = QueryEngine(llm=llm, tools=registry, output_dir=tmp_path / "out", cwd=tmp_path, memory_store=store)

    assert engine.chat("请修复并说明 pytest 验证") == "完成"

    assert any("system-reminder" in message.get("content", "") for message in llm.messages_seen)
    short_memory = json.loads((tmp_path / "out" / "short_memory.json").read_text(encoding="utf-8"))
    assert short_memory["task"]["user_goal"] == "请修复并说明 pytest 验证"
