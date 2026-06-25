import json

from repo_agent.core.llm import LLMToolResponse, ToolCall
from repo_agent.core.memory import MemoryStore
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
