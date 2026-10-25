"""Real LLM smoke test; requires a configured OpenAI-compatible service."""

from app.adapters.generation import generate, generate_text


if __name__ == "__main__":
    answer = generate_text("What is retrieval augmented generation? Answer in one sentence.")
    assert answer.strip()
    print("Non-empty generated text: PASS")
    tools = [{"type": "function", "function": {"name": "lookup_policy", "description": "Look up customs policy", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}}]
    result = generate("Look up the customs declaration policy.", tools=tools)
    assert result.text.strip() or result.tool_calls
    print("Request with non-empty tools: PASS")
