# Optional structural Evidence Completion

Status (2026-11-03): implemented behind a disabled-by-default flag. The frozen
candidate Smoke result is 9 PASS / 1 FAIL, not a full acceptance or stability claim.
Q004 remains a Generation / over-answering / condition completeness risk.
See [Issue #13 final technical results](issue13-final-results.md).

## Boundary

`EVIDENCE_COMPLETION_ENABLED=false` preserves the existing Initial retrieval path.
Only the literal value `true` (case insensitive, whitespace ignored) enables completion.
Changing the flag does not change `RETRIEVAL_TOP_K`, the Prompt, the model, stored
chunks, vectors, schema, or HTTP/C5 contracts. No extra chunk lookup runs when disabled.

When enabled, `answer()` keeps `initial_hits` and `supplemental` separate, then
assigns Evidence IDs with Initial hits first. At most one original C2 Chunk can be
appended; it has no fabricated similarity score. File metadata/READY checks and
source citation mapping still run before generation. This is supplemental context,
not a replacement Top-5 or another vector retrieval.

## Supported subset

The Korean rule supports only `man N se` age limits with an unqualified person-import
provision. Its candidate must have the same normalized age bound, a directly
adjacent parenthesis containing both exclusion and forward-same markers, an earlier
document position, and a product overlap in the age-bearing unit. The small product
vocabulary is limited to alcohol, tobacco and perfume. Qualified identities, known
local-scope language and unsupported subject prefixes cause abstention. Different
documents, missing/non-READY sources, changed source snapshots, duplicate input
records and ambiguous definitions are not supplemented.

These checks are conservative syntax heuristics, not a complete legal identity,
scope or entailment resolver. An unseen qualification can still require additional
review; matching product names alone does not establish legal applicability.
Missing linkage returns no supplemental evidence. Read/validation failures log a
warning and retain Initial evidence; cancellation propagates normally.

The Chinese continuation rule requires all of the following, using the original
question supplied by `chat/runtime.py` without changing its text:

- A source beginning with an article heading that agrees with its `section_title`.
- An explicit enumeration introduction, consecutive parenthesized item numbers
  starting at one, and a trailing semicolon rather than a closed sentence.
- The same document and exact section title; the next chunk index and next physical
  page/page index must all agree. Same-page or multi-hop completion is not supported.
- A continuation starting directly with the next item number, internally consecutive
  numbering, and no new article heading.
- One continuation item must contain the same non-negated comparison operator as
  the question and at least two overlapping Chinese bigrams from its condition
  subject. No whole-question similarity, answer terms or question IDs are used.

This is conservative lexical matching, not general semantic inference. Short,
numeric, synonymous or unrecognized conditions abstain. Adjacent prose, unrelated
conditions, reset numbering, changed snapshots and ambiguous candidates abstain.
Korean definitions and Chinese continuations share one supplemental-source cap;
competing candidates from either rule return no supplemental evidence. No raw
chunks are merged or rewritten, including when a clause crosses physical pages.

## Data access and tests

`get_ready_chunk_sources()` selects original C2 columns only, joins READY documents,
and never loads embedding columns or writes data. Its input and output are internal;
no public contract is extended. The fixture contains all 111 original C2 records of
the Korean policy in the existing MVP database snapshot, without vectors or secrets.
The Chinese fixture preserves the recorded question, Initial evidence and three
source excerpts/metadata from the read-only diagnosis. Its unit-test C2 statistics
are placeholders, not a new chunking/token-count acceptance result.

Run from `backend`, in the project's virtual environment:

```text
python -m pytest -q -p no:cacheprovider tests/test_evidence_completion.py tests/test_evidence_completion_crosspage.py tests/test_evidence_completion_repo.py tests/test_retriever.py tests/test_orchestrator.py tests/test_citation.py tests/test_prompts.py tests/test_chat.py tests/test_chat_regressions.py tests/test_storage_repo.py
```

Repository tests use the shared isolated SQLite fixture. Do not opt tests into a
shared PostgreSQL database or authorize reset of existing MVP data.

The recorded final Smoke used the enabled flag, real models and the existing corpus.
It preserves the original first Q004 failure; later fixed-context A/B outputs do not
replace that result. Earlier paraphrase experiments are diagnostics, not proof that
all paraphrases pass with the final implementation. Page/UI acceptance, the data
owner's CSV verification and PR merge remain separate checks. Initial and Supplemental
records are kept separate in the local evidence archive. Unit tests and single-run
real passes do not establish repeated-run stability. Rollback first by setting the
flag to `false`; no data migration is needed.
