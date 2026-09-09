# RepoAgent: A Scoped Coordinator Agent for Repository Repair

Status: draft pending a full SWE-bench Lite rerun from one frozen source revision.

## Abstract

RepoAgent is an open-source repository repair agent implemented directly on the OpenAI-compatible Chat Completions tool-calling protocol. It does not depend on an agent SDK or a graph orchestration framework. The system uses a Coordinator to turn a public issue statement and repository evidence into a narrow edit contract, then delegates the source change to a constrained Implement Worker. Deterministic checks protect patch syntax and delivery without using SWE-bench harness feedback during inference.

On the current local SWE-bench Lite development run, RepoAgent resolved 118 of 300 instances (39.33%). This number is not yet intended as a leaderboard result because instances 0-199 and 200-299 were produced from different source revisions. A leaderboard submission must replace this draft result with a full run generated from one frozen revision and configuration.

## System Design

RepoAgent owns its agent loop and conversation state. A session-level `QueryEngine` tracks messages, token usage, traces, short-term memory, and termination. A turn-level `query_loop` sends the current tool schemas to the model, executes requested tools through a registry, records observations, and continues until the model finishes or the step budget is exhausted.

The SWE-bench profile uses a simplified Coordinator workflow:

1. Derive a Problem Contract from the public issue statement.
2. Use a small number of scoped repository search and read operations to identify the likely contract owner and edit location.
3. Construct an EditBrief containing required behavior, preserved behavior, allowed files, and allowed line ranges.
4. Dispatch one Implement Worker with write access restricted to those files and ranges.
5. Apply deterministic syntax, diff, policy, and delivery checks.
6. Optionally run one public behavior verification command when a trustworthy command is available.
7. Submit the first statically safe non-empty patch without using harness feedback for another repair attempt.

The Implement Worker cannot search the whole repository, inspect git history, run arbitrary commands, or edit outside the Coordinator's declared scope. This separation keeps localization and implementation responsibilities explicit and limits accidental patch expansion.

## Models

The evaluated configuration uses the official `deepseek-v4-flash` API identifier for every role. The provider currently maps this rolling identifier to DeepSeek-V4-Flash-0731.

- Thinking is enabled for the Coordinator, Reproducer, Implement/Corrective Implement, PatchRanker, PatchReview, and FailureReview roles.
- Thinking is disabled for lightweight repository reads, deterministic verification, test expansion, and memory curation.

The Coordinator and Implement Worker have independent message histories. Worker prompts are self-contained and include the problem contract, repository evidence, allowed edit scope, preserved behavior, and required output format.

## Tools and Isolation

Repository tools are imported through a registry that owns schema generation, argument validation, and dispatch. The SWE-bench profile exposes narrow file listing, search, ranged reads, constrained replacements, diff inspection, and focused command execution. Each task operates on a checkout of the instance base commit.

Inference traces are written as JSONL events containing model responses, tool calls, and tool results. The submission trajectory for each instance is copied directly from this inference-time trace; no post-hoc reasoning trace is generated.

## Benchmark Integrity

The official candidate configuration is designed around the following restrictions:

- One submitted prediction per task instance.
- No `hints_text` input.
- No `PASS_TO_PASS` or `FAIL_TO_PASS` metadata.
- No gold patch or hidden test patch.
- No SWE-bench harness result is returned to the Agent for a second repair.
- No web browser is available to the Agent.
- The repository checkout and public issue statement are the only task-specific evidence sources.
- Repair-memory evolution is disabled during official inference.
- Evaluation starts only after the prediction is finalized.

Batch summaries record `official_pass1=true`, `official_no_hints=true`, `diagnostic_mode=none`, and `leaderboard_eligible=true` for all 300 current development instances.

## Local SWE-bench Lite Result

The current development artifacts contain 300 predictions and 300 inference-time trajectories. The official `SWE-bench/experiments` result script recomputed the following local result from the preserved harness logs:

| Outcome | Instances |
|---|---:|
| Resolved | 118 |
| Unresolved with a non-empty patch | 155 |
| Empty prediction | 27 |
| Harness error after re-evaluation | 0 |

The resulting local resolved rate is 39.33%.

Resolved instances by repository:

| Repository | Resolved | Total |
|---|---:|---:|
| astropy/astropy | 3 | 6 |
| django/django | 60 | 114 |
| matplotlib/matplotlib | 4 | 23 |
| mwaskom/seaborn | 2 | 4 |
| pallets/flask | 0 | 3 |
| psf/requests | 0 | 6 |
| pydata/xarray | 1 | 5 |
| pylint-dev/pylint | 2 | 6 |
| pytest-dev/pytest | 4 | 17 |
| scikit-learn/scikit-learn | 11 | 23 |
| sphinx-doc/sphinx | 4 | 16 |
| sympy/sympy | 27 | 77 |

## Cost and Runtime

The three 100-instance inference batches recorded approximately 59.1 million total tokens, or 197 thousand tokens per task on average. Average token use increased from roughly 144 thousand for instances 0-99 to 257 thousand for instances 200-299. The later slice contains more Sphinx and SymPy tasks, but the increase also shows that Coordinator context and worker evidence remain important optimization targets.

## Limitations

The main correctness limitation is semantic precision after successful patch delivery. Compared with the earliest same-task experiment on Lite instances 10-99, the current system reduced empty patches from 25 to 2 and eliminated five harness errors, while resolved instances increased from 39 to 46. Many newly delivered patches still fail public regression behavior, so further gains should focus on problem-contract quality and producer/consumer semantic reasoning rather than adding more review loops.

The current 300-instance aggregate was produced in three batches. Instances 0-199 used revision `31f431e`; instances 200-299 used revision `cf5435a` plus working-tree changes. This prevents the aggregate from representing one reproducible frozen system. Before leaderboard submission, RepoAgent must freeze source, prompts, model routing, memory state, and batch flags, then regenerate all 300 predictions once.

## Reproduction

The source repository is available at <https://github.com/Artoriazzz/RepoAgent>. A final leaderboard release should include:

- The exact source commit and tag.
- Environment and model configuration without credentials.
- The command used to run all 300 Lite instances.
- The 300 prediction records and inference-time trajectories.
- The official SWE-bench harness artifacts.
- The output of `python -m analysis.get_results evaluation/lite/<submission>`.
