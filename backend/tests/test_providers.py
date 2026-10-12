from ithaca.mcp_client import _qualified
from ithaca.providers.anthropic_provider import AnthropicProvider
from ithaca.providers.openai_compat import OpenAICompatProvider


def test_anthropic_wire_merges_tool_results_and_replays_raw_blocks():
    raw_blocks = [
        {"type": "thinking", "thinking": "plan", "signature": "sig"},
        {"type": "tool_use", "id": "t1", "name": "list_files", "input": {}},
        {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "a"}},
    ]
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "list_files", "arguments": {}},
                                                          {"id": "t2", "name": "read_file", "arguments": {"path": "a"}}],
         "raw": {"anthropic": {"model": "claude-opus-5-5", "content": raw_blocks}}},
        {"role": "tool", "tool_call_id": "t1", "content": "a"},
        {"role": "tool", "tool_call_id": "t2", "content": "boom", "is_error": True},
    ]
    system, wire = AnthropicProvider.to_wire(messages, "claude-opus-5-5")
    assert system == "sys"
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[1]["content"] == raw_blocks  # replayed verbatim, thinking included
    results = wire[2]["content"]
    assert [b["tool_use_id"] for b in results] == ["t1", "t2"] and results[1]["is_error"]

    # A different model gets rebuilt blocks without the other model's thinking.
    _, wire2 = AnthropicProvider.to_wire(messages, "claude-sonnet-5-5")
    assert all(b["type"] != "thinking" for b in wire2[1]["content"])


def test_openai_wire_serializes_tool_calls():
    wire = OpenAICompatProvider.to_wire([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "name": "x", "arguments": {"a": 1}}]},
        {"role": "tool", "tool_call_id": "c", "content": "ok"},
    ])
    assert wire[0]["tool_calls"][0]["function"] == {"name": "x", "arguments": '{"a": 1}'}
    assert wire[1] == {"role": "tool", "tool_call_id": "c", "content": "ok"}


def test_mcp_names_fit_provider_limits():
    name = _qualified("a-very-long-server-name-for-testing-purposes", "an_extremely_long_tool_name_" * 4)
    assert len(name) <= 64 and name.startswith("mcp__")


def test_string_arguments_are_coerced_to_schema_types():
    from ithaca.tools.registry import coerce_args, validate_args

    schema = {"type": "object", "properties": {"ref": {"type": "integer"}, "submit": {"type": "boolean"},
                                               "text": {"type": "string"}, "ratio": {"type": "number"},
                                               "tags": {"type": "array"}}, "required": ["ref"]}
    args = coerce_args(schema, {"ref": "4", "submit": "True", "text": "42", "ratio": "0.5", "tags": '["a"]'})
    assert args == {"ref": 4, "submit": True, "text": "42", "ratio": 0.5, "tags": ["a"]}
    assert validate_args(schema, args) is None
    bad = coerce_args(schema, {"ref": "four"})
    assert bad == {"ref": "four"} and validate_args(schema, bad)
