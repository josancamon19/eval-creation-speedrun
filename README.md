# Generator

A Nim regression-task exporter and Harbor/Daytona evaluation pilot.

- [Dynamic adapter](src/generator/adapter.py)
- [Reusable Harbor task template](src/generator/task-template/)
- [Native mini-swe-agent job](src/generator/configs/job.yaml)

The [fail-to-pass filter](src/generator/filters/fail_to_pass.py) requires each scored
Nim program to fail against the unchanged compiler/library and pass with the PR implementation.
The adapter runs both states in Harbor before publishing a task.
The supported environment is **Linux amd64 with the C backend**. Docker stages
and compiler invocations pin that target; the verifier checks its actual host
and a compiler smoke test checks target symbols and integer width. Both calibration
reports must confirm this environment. Platform/backend overrides (including
`-d:mingw`) and source-level define/undef pragmas are rejected. Tests disabled on
Linux or without a C target are excluded. Platform-specific no-ops are removed by
the fail-to-pass gate; issue titles mentioning Windows/macOS are not blacklisted.
The manifest, task metadata and calibration evidence record the target environment.
Existing pilot datasets and results predate this stricter policy; regenerate them
with the current exporter to apply it.

```bash
uv sync --frozen
gh repo clone nim-lang/Nim repos/Nim
uv run generator export --limit 3
```

Export automatically clones `https://github.com/nim-lang/Nim.git` into `repos/Nim/`
when missing, or fetches upstream updates into the existing checkout without
replacing local edits. Accepted tasks are always published under `datasets/nim/`.
Export uses Daytona and `DAYTONA_API_KEY` from `.env`, with logs under
`.cache/calibration/`. Validation needs no model API.
Calibration uses one Harbor job per batch, with **16 concurrent trials** by default
(`--calibration-concurrency 16`). Baseline and patched runs are separate trials,
so a full batch contains eight PR pairs. Each trial has isolated sandboxes; both
states reuse the same environment snapshot. Rejected candidates are replaced in
later batches until the requested task count is reached or the scan is exhausted.
`--limit` counts validated tasks; increase `--scan-limit` (default 200) for a larger
PR search window. Existing tasks are never overwritten.

Export decisions are cached by default in `.cache/exports`: both static and
fail-to-pass rejections retain their reasons, and accepted tasks retain a complete
copy of the calibrated export. Repeating an export reuses matching decisions and
can restore accepted tasks into `datasets/nim/` after its removal without running Harbor.
An unchanged existing output is reused; a modified or stale output is never
overwritten. Cache keys include the repository path, PR metadata and merge commit,
filter/calibration code, task template, dependency lock, target and execution
backend. Changes to these inputs trigger fresh checks. Cached task contents and
file modes are verified before reuse. Infrastructure failures are not cached.
The cache location is fixed at `.cache/exports/`; use `--no-cache` to read/write none. The
summary reports accepted/rejected cache hits and newly calibrated PRs. The Python
`export_tasks` API enables the same behavior when passed `cache_dir=Path(...)`.
Exports created before this cache was added need one fresh evaluation.

Each prompt points to `/workspace/ISSUE_NUMBER.md`; export writes the linked issue
to `environment/ISSUE_NUMBER.md` and copies it to that runtime path. It removes
the `### Nim version` section and the `### Description` heading, preserving the
description, code examples and remaining sections.

After the agent finishes, Harbor transfers `compiler/` and `lib/` through its
native artifacts, excluding Git metadata, caches and object files. This follows
the source-directory pattern used by [TB4's vLLM task](https://github.com/harbor-framework/terminal-bench/blob/v4.0.0/tasks/vllm-deepseek-streaming/task.toml).
There is no custom collection hook or submission helper in the agent image.
`tests/Dockerfile` builds a dedicated verifier image with its own tests, bootstrap
compiler, source archive and dependencies. The verifier replaces only the two
source trees, then rebuilds as an unprivileged user with trusted configuration.
Source transfer is approximately 10 MB per trial; this deliberately favors
Harbor's built-in transfer over custom diff machinery. Each run writes
`/logs/verifier/ctrf.json` with individual submission, build, smoke and
regression-check results. Upstream Git history is absent from both images.

- The [static policy](src/generator/filters/integration.py) accepts supported C-backend
  compile-and-run programs using public APIs and recursively inspected helpers.
  It rejects compiler/private imports, source or filesystem inspection, generated
  code/representation checks, custom commands, unsupported configs and options.
  This is conservative: legitimate file-I/O, FFI and custom-harness tests are
  excluded. It is not a proof of arbitrary Nim program semantics.
- The verifier rebuilds the candidate compiler, then exposes only its binary,
  library, trusted config and protected fixtures. It compiles and executes each
  program separately, checking exit code and specified output. Generated C,
  caches and objects are removed before execution. Duplicate output lines must
  actually appear; Testament's ordered-subset matcher is not used.
- Every scored **file/configuration** must have a compile error, wrong exit code
  or wrong output without the fix, then pass with it. Baseline passes, timeouts,
  signals and reference failures are excluded. Memory/resource diagnostics and
  build/smoke/infrastructure errors cannot qualify as regression evidence.
  Older assertions may coexist with the regression inside a retained file; this
  gate proves the whole file/configuration fails, not every individual assertion.
- Tasks are staged outside the dataset and published atomically after validation.
  `tests/manifest.json` holds qualifying cases; `tests/calibration.json` records
  observations, exclusions, log paths and input hashes. Full Harbor logs remain
  in `.cache/calibration`. There is no CLI option to bypass calibration.

Local checks:

```bash
uv run checks/static.py datasets/nim/TASK
uv run checks/rubric.py datasets/nim/TASK
uv run checks/trial_analysis.py jobs/JOB
```

Task reports go to `.cache/checks/TASK/static.md` and `rubric.md`.
Static checks omit Terminal-Bench author/taxonomy/README requirements, package
naming, and the dedicated verifier Dockerfile rule. Instruction headings are
still checked.
The rubric omits difficulty, solution and verification explanations, category and
tags, task naming, README, expert-time estimates, the upstream TOML schema, and
reviewability. Artifact efficiency and CTRF reporting remain in the rubric.
Trial analysis runs Terminal-Bench's six trial criteria with Claude Code and
Sonnet 5.5 in Daytona, using `DAYTONA_API_KEY` and `CLAUDE_OAUTH_TOKEN` from `.env`.
It keeps only `.cache/checks/job-JOB_ID/TRIAL_ID.md`; IDs come from `result.json`,
falling back to directory names when absent. Analysis jobs use temporary directories
and Harbor's default timestamp names; Daytona sandboxes are deleted afterward.
Findings are advisory; execution or invalid-output errors exit with code 2.

AI assistance was used for repository research, implementation, tests, and
task-quality reviews.
