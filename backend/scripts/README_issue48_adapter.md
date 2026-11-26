# Issue #48 — Pilot r02 Adapter integration and safety guards

## Scope

Integrate the exact frozen r02 adapter and fact table into the HTTP evaluation pipeline (`4.0-pilot-r02-guarded`). This PR is limited to Pilot 40 evaluation tooling, prospective safety holds, and offline tests. It does not modify Agent/Single production logic, the frozen r02 data or scoring, the official 30-question dataset, or any Bad Case.

- First request uses original message and an independent session. Agent follow-up preserves session_id, question_id and message, and supplies only the requested fields from matched frozen input facts. Unknown/ambiguous/malformed or held fields cause an all-or-nothing `ADAPTER_UNMAPPED` stop; no partial POST. The original r02 alias/option matching stays intact.
- Joint review on 2026-11-25 confirmed holds for P015 (`item_name`, topic narrower than question), P023 (`item_name`, cigar vs cigarette), P026 (`item_name`, devices beyond pods/liquid), P034 (`traveler_status`, definition vs actual traveler identity), including aliases that resolve to those keys. None of these disputed fields was actually submitted in frozen r02; no change to r02 results. The existing diagnostic string `PENDING_FACT_REVIEW` is retained for compatibility: hold is approved, future corrected mapping still requires review.
- `manifest.json` is the original provenance manifest, not a manifest of this PR; its `make_facts.py` and `run_r02_original.py` hashes refer to the separately archived original delivery. `condition_adapter.py` and `clarification-facts.json` are byte-pinned; do not normalize or edit them. The script verifies their SHA-256 before loading. The live-run gate also pins the Pilot dataset byte SHA-256; do not run formal 30 questions here.
- Single mode never resumes clarification. Actual `model_name` is taken from the HTTP response; configured_model is separate. Missing fallback values remain null until the agreed HTTP fields are available. No score is inferred from HTTP success.

## Offline checks (repository root; Windows PowerShell)

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend/scripts -p test_eval_http_pipeline.py -v
.\.venv\Scripts\python.exe backend/scripts/eval_http_pipeline.py --check-only --questions eval/pilot43_questions_v1_40.jsonl
```

Observed locally on 2026-11-25: 15/15 offline tests pass, Pilot 40/40 fact matches; no HTTP in these checks. Tests cover aliases, exact options, domain blocking, four holds, duplicate fields, changed inputs, all-or-nothing blocking and mocked two-turn/Single flows.

Separate local HTTP smoke evidence on backend commit `56510d8cdd2783156bce76c24af8b21abab50770` with `gpt-6-sol`: Agent P001 two turns ANSWERED/SUCCEEDED, P013 one turn ANSWERED/SUCCEEDED, P037 INSUFFICIENT_EVIDENCE/SUCCEEDED; Single P001 HTTP 200 ANSWERED. Actual model field is preserved, including null for the refusal. The four holds were verified offline, not triggered in these live smoke cases. These observations are not the frozen NPU Gemma r02 metrics. No new Baseline run is authorized in this PR.

## File scope

`eval_http_pipeline.py`, `condition_adapter.py`, `clarification-facts.json`, `manifest.json`, `test_eval_http_pipeline.py`, and this README. Keep raw results, logs, credentials, V5 formal preflight and report spreadsheets outside this PR.
