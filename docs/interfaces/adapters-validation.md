# Issue 10: adapters and acceptance

Scope: embeddings and generation only. No chunking, search, prompt construction
or citation validation. contracts.py is unchanged.

## Runtime and setup

Windows Python 3.14.2 could resolve wheels, but PyTorch 2.14.1, 2.14.1+cpu and
2.9.1+cpu failed at import with WinError 1114 (c10.dll). This is a local runtime
failure, not proof that Python 3.14 is generally unsupported. Local RAG testing
uses Python 3.12 and CPU PyTorch 2.6.0. Direct integration with the team's
Python 3.14 FastAPI process still requires resolving that runtime difference.
No extra service boundary has been implemented.

From the repository root, create a Python 3.12 venv outside the repository:

```powershell
python -m venv D:\RAG-Dev\venvs\rag-py312
$python = 'D:\RAG-Dev\venvs\rag-py312\Scripts\python.exe'
& $python -m pip install -r backend/requirements-cpu.txt
& $python -m pip install -r backend/requirements.txt
cd backend
& $python -m unittest discover -s tests -v
& $python smoke_embedding.py
& $python smoke_generation.py
```

Use an explicit Python 3.12 executable if `python` points at another version.
Create backend/.env using .env.example as the template; the real file is ignored
by Git. Store model files outside the repository. SentencePiece 0.2.2 produced
a native access violation locally; the tested dependency uses 0.2.0.

## Embedding

`embed_texts(texts: list[str]) -> list[list[float]]` and
`embed_chunks(chunks: list[Chunk]) -> list[EmbeddingRecord]` are synchronous.
Chunk input follows the existing title plus text rule; whitespace-only body
text is rejected even when a title exists. Output order and chunk IDs are
preserved. Empty input returns []. Invalid input raises ValueError; model,
configuration and vector failures raise EmbeddingError with the cause retained.

Use EMBEDDING_MODEL=BAAI/bge-m3 or a downloaded model directory. Leave
EMBEDDING_BASE_URL blank for CPU inference. EMBEDDING_CACHE_DIR controls the
download location. A nonblank embedding service URL raises an explicit
unsupported-protocol error: NPU remote embedding is NOT implemented and the
adapter will not silently fall back to CPU. Do not count this as a remote
embedding connection-failure test.

Dense-only, float32 inference uses a configurable batch size (default 1), CPU
threads (default 1), max length (default 8192), and fixed MVP dimension 1024.
The CPU thread setting is process-wide through PyTorch.

## Generation

The user approved OpenAI-compatible HTTP as the temporary protocol on 2026-10-25.
This does not select the final model. Set GENERATION_MODEL and
GENERATION_BASE_URL to the actual served model ID and API root, including /v1
if the service requires it. The adapter appends /chat/completions. API keys are
optional for local services and must only be set locally. Never place keys in
an endpoint URL. Timeout defaults to 60 seconds, connect timeout at most 10.
Requests are synchronous, non-streaming and not automatically retried.

`generate_text(messages, tools=None) -> str` accepts a question string or a
list of chat message dictionaries and returns non-empty text. For possible
tool-only output, `generate(messages, tools=None) -> GenerationResult` retains
both text and the OpenAI-compatible function tool_calls. This adapter does not
execute tools. Empty or malformed responses and service/configuration failures
raise GenerationError. Error messages omit credentials and response bodies.

## Evidence and outstanding acceptance

Real CPU BGE-M3 inference passed with one Chinese sentence and one Korean
sentence: count=2, dimensions=[1024,1024], finite nonzero vectors, L2 norms
approximately [0.9999999782,0.9999999811]. Model revision:
5617a9f61b028005a4858fdac845db406aefb181. Weight SHA256:
b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38.

Real embedding configuration switch: first EMBEDDING_MODEL pointed to the
downloaded D:/RAG-Dev/models/huggingface/hub/models--BAAI--bge-m3/snapshots/
5617a9f61b028005a4858fdac845db406aefb181 directory. Then only .env was edited
to use EMBEDDING_MODEL=BAAI/bge-m3 and EMBEDDING_CACHE_DIR pointing at the same
hub cache. Both runs passed offline with identical vector samples. This verifies
two loading identifiers for the SAME model, not switching to a different model.

Unit tests stub embedding model outputs. Generation tests run a real loopback
HTTP server returning TEST FIXTURES, not LLM-generated text. They verify a
non-empty tools argument, tool-only responses, malformed/empty/503 responses,
and a real non-listening endpoint raising GenerationError.

Configuration switching test: only a temporary .env was edited, changing
GENERATION_MODEL model-a to model-b and the API path /api-a to /api-b; the
HTTP server observed each new model and address without code changes. These
are fixture model IDs, not two real LLMs. Real model switching was separately
verified against the school's gateway as recorded below.

Real generation smoke test passed on 2026-10-25 using the school's Baze API
root https://api.baze.co/v1/gateway and model ID gpt-6-sol. Running
backend/smoke_generation.py exited with status 0 and printed:

```
Non-empty generated text: PASS
Request with non-empty tools: PASS
```

These were two actual service requests using the locally configured school
key. This verifies non-empty text and acceptance of a non-empty tools argument;
it does not assert that the model chose to invoke a tool. No key or response
body was logged. backend/.env is confirmed ignored by Git. The model is only
a temporary development service, not the final NPU model selection.

## Real .env-only model switching (2026-10-25)

The school's authenticated model list returned both gpt-6-sol and gpt-6-luna.
The same question was sent through the existing generate_text() adapter:
"What is retrieval augmented generation? Reply in one short sentence."

| Phase | Only edited .env setting | Non-empty response | Characters |
| --- | --- | --- | --- |
| Before | GENERATION_MODEL=gpt-6-sol | PASS | 107 |
| Switched | GENERATION_MODEL=gpt-6-luna | PASS | 154 |
| Restored | GENERATION_MODEL=gpt-6-sol | PASS | 121 |

GENERATION_BASE_URL stayed https://api.baze.co/v1/gateway. File hashes of
config.py, embedding.py and generation.py were identical in all three phases.
A digest of the remaining .env settings confirmed no other configuration value
changed. The original model was restored. No code edits, alternate client,
environment model override, or fixture server were used for these three calls.
Credentials were not printed. Both models are temporary test choices only.

This satisfies the model-name branch of the "model name OR address" switch
acceptance criterion; a second real gateway address was not tested. The local
functional checks for issue 10 are complete. Final review and submission of
the latest template and validation documentation remain separate GitHub steps.
