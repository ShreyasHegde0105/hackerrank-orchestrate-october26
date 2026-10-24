# Usage report: final full-dataset run

**Run:** `python code/main.py`, 2026-10-25, producing `output.csv` (518 rows, SHA-256 `ac85935e3dd2c0bf2a34814c49c964bce819160edbfcc0da30e9ea32902efb86`). Wall time was about 5 s on one machine.

## Model usage during the run

The final run made **no model calls**. Every decision is deterministic code:

| Step | How it is done |
|---|---|
| Deletion intent | Rules (`code/audit/intent.py`) |
| Identity resolution, evidence extraction, temporal reconstruction, policy | Code |
| PDFs | Read with `pdfplumber` |
| The 11 screenshots | Read from manual transcriptions stored in `code/llm/cache/screenshots/`, served from disk (`method: manual`) |

**Per model:**

| Provider | Model | Purpose | Calls | Input tokens | Output tokens | Total tokens | Est. cost |
|---|---|---|---|---|---|---|---|
| Anthropic | `claude-opus-5-5` (optional vision path, `--vision` in `tools/evidence_report.py`) | Screenshot transcription | **0** (not enabled) | 0 | 0 | 0 | $0.00 |
| — | `manual-transcription` (cache entries) | Screenshot transcription | 11 cache reads, 0 API calls | 0 | 0 | 0 | $0.00 |

**Overall totals:**

| Measure | Value |
|---|---|
| Model calls | 0 |
| Input / output / total tokens | 0 / 0 / 0 |
| Average tokens per request (518) | 0 |
| Estimated total cost | $0.00 |
| Estimated cost per request | $0.00 |

**How the counts were measured:** `llm.cache.UsageLedger`, which records every screenshot lookup with its method, together with the extraction report (`python code/tools/evidence_report.py`, section 10: "Vision API calls: 0; transcriptions served from the cache: 11 (methods: {'manual': 11})"). No other code path calls a model.

## Not part of the run

- **The manual screenshot transcriptions** were made during development by the coding assistant (Claude Code, `claude-opus-5-5`) viewing each PNG. That assistant also wrote the code.
  - This development-time usage is not part of the pipeline run.
  - Its token counts are not available to the project, so none are reported.
- **If the optional vision path were enabled** (`pip install anthropic` plus credentials), it would make at most 11 calls, once each, after which results are cached. The cost would be priced at Claude Opus 5.5 rates ($4 per million input tokens, $20 per million output tokens). It was not used for this submission.
