from __future__ import annotations

import argparse
import csv
import json
import math
import queue
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


COORDINATOR = "coordinator"
IMPLEMENT = "implement"
VALIDATION = "validation"
FAILURE_REVIEW = "failure_review"
CORRECTIVE_IMPLEMENT = "corrective_implement"
EXPERIENCE_MEMORY = "experience_memory"

# 固定耗时用于模拟各角色的相对复杂度，不受模型或网络波动影响。
DEFAULT_STAGE_DURATIONS = {
    COORDINATOR: 12.0,
    IMPLEMENT: 20.0,
    VALIDATION: 15.0,
    FAILURE_REVIEW: 14.0,
    CORRECTIVE_IMPLEMENT: 18.0,
    EXPERIENCE_MEMORY: 10.0,
}

SUCCESS_ROUTE = (COORDINATOR, IMPLEMENT, VALIDATION, EXPERIENCE_MEMORY)
RETRY_ROUTE = (
    COORDINATOR,
    IMPLEMENT,
    VALIDATION,
    FAILURE_REVIEW,
    CORRECTIVE_IMPLEMENT,
    VALIDATION,
    EXPERIENCE_MEMORY,
)


@dataclass(frozen=True)
class Experiment:
    key: str
    label: str
    topology: str
    end_to_end_workers: int = 0
    workers_per_stage: int = 0

    @property
    def total_workers(self) -> int:
        if self.topology == "end_to_end":
            return self.end_to_end_workers
        return self.workers_per_stage * 6


EXPERIMENTS = (
    Experiment("serial-1", "单 RepoAgent 串行", "end_to_end", end_to_end_workers=1),
    Experiment("serial-3", "3 个完整 RepoAgent 并行", "end_to_end", end_to_end_workers=3),
    Experiment("pipeline-1", "分阶段流水线（每角色 1 个 Worker）", "pipeline", workers_per_stage=1),
    Experiment("pipeline-3", "分阶段 Worker 池流水线", "pipeline", workers_per_stage=3),
)


@dataclass(frozen=True)
class TaskPlan:
    task_id: str
    stages: tuple[str, ...]


@dataclass(frozen=True)
class StageTiming:
    task_id: str
    stage: str
    occurrence: int
    worker_id: str
    enqueued_at: float
    started_at: float
    finished_at: float

    @property
    def queue_wait_seconds(self) -> float:
        return max(0.0, self.started_at - self.enqueued_at)

    @property
    def execution_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)


@dataclass(frozen=True)
class StageMetrics:
    stage: str
    executions: int
    queue_wait_average_seconds: float
    queue_wait_p95_seconds: float
    execution_average_seconds: float
    execution_p95_seconds: float


@dataclass(frozen=True)
class RunMetrics:
    experiment: str
    label: str
    repetition: int
    task_count: int
    retry_task_count: int
    total_workers: int
    makespan_seconds: float
    throughput_tasks_per_minute: float
    latency_p50_seconds: float
    latency_p95_seconds: float
    latency_p99_seconds: float
    queue_wait_average_seconds: float
    queue_wait_p95_seconds: float
    worker_utilization: float
    max_queue_depth: int
    completed_tasks: int
    stage_executions: int
    stage_metrics: tuple[StageMetrics, ...]


@dataclass(frozen=True)
class AggregateMetrics:
    experiment: str
    label: str
    task_count: int
    retry_task_count: int
    total_workers: int
    repetitions: int
    makespan_seconds: float
    throughput_tasks_per_minute: float
    latency_p50_seconds: float
    latency_p95_seconds: float
    latency_p99_seconds: float
    queue_wait_average_seconds: float
    queue_wait_p95_seconds: float
    worker_utilization: float
    max_queue_depth: float
    throughput_gain_vs_serial_1: float
    makespan_reduction_vs_serial_1: float


class _Recorder:
    def __init__(self, task_count: int) -> None:
        self._lock = threading.Lock()
        self.completed = threading.Event()
        self.remaining = task_count
        self.stage_timings: list[StageTiming] = []
        self.task_finished_at: dict[str, float] = {}
        self.max_queue_depth = 0

    def record_stage(self, timing: StageTiming) -> None:
        with self._lock:
            self.stage_timings.append(timing)

    def record_queue_depth(self, depth: int) -> None:
        with self._lock:
            self.max_queue_depth = max(self.max_queue_depth, depth)

    def finish_task(self, task_id: str, finished_at: float) -> None:
        with self._lock:
            if task_id in self.task_finished_at:
                raise RuntimeError(f"Task completed more than once: {task_id}")
            self.task_finished_at[task_id] = finished_at
            self.remaining -= 1
            if self.remaining == 0:
                self.completed.set()


@dataclass(frozen=True)
class _PipelineItem:
    plan: TaskPlan
    stage_index: int
    enqueued_at: float


def build_workload(task_count: int, retry_ratio: float, seed: int) -> tuple[TaskPlan, ...]:
    if task_count < 1:
        raise ValueError("task_count must be positive")
    if not 0 <= retry_ratio <= 1:
        raise ValueError("retry_ratio must be between 0 and 1")
    retry_count = round(task_count * retry_ratio)
    routes = [RETRY_ROUTE] * retry_count + [SUCCESS_ROUTE] * (task_count - retry_count)
    random.Random(seed).shuffle(routes)
    return tuple(TaskPlan(f"task-{index + 1:04d}", route) for index, route in enumerate(routes))


def run_experiment(
    experiment: Experiment,
    workload: tuple[TaskPlan, ...],
    stage_durations: dict[str, float],
    repetition: int = 1,
) -> RunMetrics:
    missing = {stage for task in workload for stage in task.stages} - stage_durations.keys()
    if missing:
        raise ValueError(f"Missing stage durations: {', '.join(sorted(missing))}")
    if any(value < 0 for value in stage_durations.values()):
        raise ValueError("Stage durations cannot be negative")

    recorder = _Recorder(len(workload))
    started_at = time.perf_counter()
    if experiment.topology == "end_to_end":
        _run_end_to_end(experiment, workload, stage_durations, recorder, started_at)
    elif experiment.topology == "pipeline":
        _run_pipeline(experiment, workload, stage_durations, recorder, started_at)
    else:
        raise ValueError(f"Unsupported topology: {experiment.topology}")
    finished_at = max(recorder.task_finished_at.values())
    return _metrics(experiment, workload, recorder, started_at, finished_at, repetition)


def _run_end_to_end(
    experiment: Experiment,
    workload: tuple[TaskPlan, ...],
    stage_durations: dict[str, float],
    recorder: _Recorder,
    submitted_at: float,
) -> None:
    recorder.record_queue_depth(len(workload))

    def execute(plan: TaskPlan) -> None:
        worker_id = threading.current_thread().name
        for index, stage in enumerate(plan.stages):
            started = time.perf_counter()
            time.sleep(stage_durations[stage])
            finished = time.perf_counter()
            recorder.record_stage(StageTiming(
                plan.task_id,
                stage,
                _occurrence(plan.stages, index),
                worker_id,
                submitted_at if index == 0 else started,
                started,
                finished,
            ))
        recorder.finish_task(plan.task_id, time.perf_counter())

    with ThreadPoolExecutor(
        max_workers=experiment.end_to_end_workers,
        thread_name_prefix="repoagent",
    ) as executor:
        futures = [executor.submit(execute, plan) for plan in workload]
        wait(futures)
        for future in futures:
            future.result()


def _run_pipeline(
    experiment: Experiment,
    workload: tuple[TaskPlan, ...],
    stage_durations: dict[str, float],
    recorder: _Recorder,
    submitted_at: float,
) -> None:
    stages = tuple(stage_durations)
    queues = {stage: queue.Queue() for stage in stages}
    stop = object()
    threads: list[threading.Thread] = []

    def enqueue(item: _PipelineItem) -> None:
        stage = item.plan.stages[item.stage_index]
        target = queues[stage]
        target.put(item)
        recorder.record_queue_depth(target.qsize())

    def consume(stage: str, worker_index: int) -> None:
        worker_id = f"{stage}-{worker_index}"
        source = queues[stage]
        while True:
            item = source.get()
            try:
                if item is stop:
                    return
                started = time.perf_counter()
                time.sleep(stage_durations[stage])
                finished = time.perf_counter()
                recorder.record_stage(StageTiming(
                    item.plan.task_id,
                    stage,
                    _occurrence(item.plan.stages, item.stage_index),
                    worker_id,
                    item.enqueued_at,
                    started,
                    finished,
                ))
                next_index = item.stage_index + 1
                if next_index == len(item.plan.stages):
                    recorder.finish_task(item.plan.task_id, finished)
                else:
                    enqueue(_PipelineItem(item.plan, next_index, finished))
            finally:
                source.task_done()

    for stage in stages:
        for worker_index in range(1, experiment.workers_per_stage + 1):
            thread = threading.Thread(
                target=consume,
                args=(stage, worker_index),
                name=f"{stage}-{worker_index}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)

    for plan in workload:
        enqueue(_PipelineItem(plan, 0, submitted_at))
    recorder.completed.wait()
    for source in queues.values():
        for _ in range(experiment.workers_per_stage):
            source.put(stop)
    for thread in threads:
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError(f"Pipeline worker did not stop: {thread.name}")


def _metrics(
    experiment: Experiment,
    workload: tuple[TaskPlan, ...],
    recorder: _Recorder,
    started_at: float,
    finished_at: float,
    repetition: int,
) -> RunMetrics:
    makespan = max(1e-9, finished_at - started_at)
    latencies = [finished - started_at for finished in recorder.task_finished_at.values()]
    waits = [timing.queue_wait_seconds for timing in recorder.stage_timings]
    busy = sum(timing.execution_seconds for timing in recorder.stage_timings)
    expected_stages = sum(len(plan.stages) for plan in workload)
    if len(recorder.task_finished_at) != len(workload) or len(recorder.stage_timings) != expected_stages:
        raise RuntimeError("Benchmark lost or duplicated workflow stages")
    return RunMetrics(
        experiment=experiment.key,
        label=experiment.label,
        repetition=repetition,
        task_count=len(workload),
        retry_task_count=sum(plan.stages == RETRY_ROUTE for plan in workload),
        total_workers=experiment.total_workers,
        makespan_seconds=makespan,
        throughput_tasks_per_minute=len(workload) / makespan * 60,
        latency_p50_seconds=_percentile(latencies, 50),
        latency_p95_seconds=_percentile(latencies, 95),
        latency_p99_seconds=_percentile(latencies, 99),
        queue_wait_average_seconds=statistics.fmean(waits) if waits else 0,
        queue_wait_p95_seconds=_percentile(waits, 95),
        worker_utilization=min(1.0, busy / (makespan * experiment.total_workers)),
        max_queue_depth=recorder.max_queue_depth,
        completed_tasks=len(recorder.task_finished_at),
        stage_executions=len(recorder.stage_timings),
        stage_metrics=_stage_metrics(recorder.stage_timings),
    )


def aggregate(runs: Iterable[RunMetrics]) -> tuple[AggregateMetrics, ...]:
    groups: dict[str, list[RunMetrics]] = {}
    for run in runs:
        groups.setdefault(run.experiment, []).append(run)
    baseline_runs = groups.get("serial-1")
    if not baseline_runs:
        raise ValueError("serial-1 baseline is required")
    baseline_throughput = statistics.median(run.throughput_tasks_per_minute for run in baseline_runs)
    baseline_makespan = statistics.median(run.makespan_seconds for run in baseline_runs)
    results: list[AggregateMetrics] = []
    for experiment in EXPERIMENTS:
        values = groups.get(experiment.key)
        if not values:
            continue
        median: Callable[[str], float] = lambda field: statistics.median(
            float(getattr(value, field)) for value in values
        )
        throughput = median("throughput_tasks_per_minute")
        makespan = median("makespan_seconds")
        first = values[0]
        results.append(AggregateMetrics(
            experiment=experiment.key,
            label=experiment.label,
            task_count=first.task_count,
            retry_task_count=first.retry_task_count,
            total_workers=first.total_workers,
            repetitions=len(values),
            makespan_seconds=makespan,
            throughput_tasks_per_minute=throughput,
            latency_p50_seconds=median("latency_p50_seconds"),
            latency_p95_seconds=median("latency_p95_seconds"),
            latency_p99_seconds=median("latency_p99_seconds"),
            queue_wait_average_seconds=median("queue_wait_average_seconds"),
            queue_wait_p95_seconds=median("queue_wait_p95_seconds"),
            worker_utilization=median("worker_utilization"),
            max_queue_depth=median("max_queue_depth"),
            throughput_gain_vs_serial_1=throughput / baseline_throughput,
            makespan_reduction_vs_serial_1=(baseline_makespan - makespan) / baseline_makespan,
        ))
    return tuple(results)


def write_reports(
    output_dir: Path,
    runs: tuple[RunMetrics, ...],
    aggregates: tuple[AggregateMetrics, ...],
    settings: dict[str, object],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.joinpath("pipeline-benchmark.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "settings": settings,
                "runs": [asdict(run) for run in runs],
                "summary": [asdict(item) for item in aggregates],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    with output_dir.joinpath("pipeline-benchmark.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(aggregates[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(item) for item in aggregates)
    with output_dir.joinpath("pipeline-stage-benchmark.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        fieldnames = ["experiment", "repetition", *StageMetrics.__dataclass_fields__]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for run in runs:
            for stage in run.stage_metrics:
                writer.writerow({
                    "experiment": run.experiment,
                    "repetition": run.repetition,
                    **asdict(stage),
                })
    output_dir.joinpath("pipeline-benchmark.md").write_text(
        _markdown_report(aggregates, settings), encoding="utf-8"
    )


def _markdown_report(
    aggregates: tuple[AggregateMetrics, ...], settings: dict[str, object]
) -> str:
    lines = [
        "# RepoAgent 异步流水线并发基准",
        "",
        f"任务数：{settings['taskCount']}；失败后纠正比例：{settings['retryRatio']:.0%}；"
        f"重复次数：{settings['repetitions']}。",
        "",
        "| 实验组 | Worker 总数 | 总耗时(s) | 吞吐(任务/min) | P95延迟(s) | P95排队(s) | 利用率 | 吞吐提升 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in aggregates:
        lines.append(
            f"| {item.label} | {item.total_workers} | {item.makespan_seconds:.3f} | "
            f"{item.throughput_tasks_per_minute:.2f} | {item.latency_p95_seconds:.3f} | "
            f"{item.queue_wait_p95_seconds:.3f} | {item.worker_utilization:.1%} | "
            f"{item.throughput_gain_vs_serial_1:.2f}x |"
        )
    lines.extend([
        "",
        "> 该基准使用固定阶段耗时，用于隔离调度拓扑带来的收益；LLM、Docker、网络和 SWE-bench harness 性能需另做端到端压测。",
        "",
    ])
    return "\n".join(lines)


def _occurrence(stages: tuple[str, ...], index: int) -> int:
    return sum(1 for stage in stages[: index + 1] if stage == stages[index])


def _stage_metrics(timings: Iterable[StageTiming]) -> tuple[StageMetrics, ...]:
    grouped: dict[str, list[StageTiming]] = {}
    for timing in timings:
        grouped.setdefault(timing.stage, []).append(timing)
    return tuple(
        StageMetrics(
            stage=stage,
            executions=len(values),
            queue_wait_average_seconds=statistics.fmean(
                value.queue_wait_seconds for value in values
            ),
            queue_wait_p95_seconds=_percentile(
                (value.queue_wait_seconds for value in values), 95
            ),
            execution_average_seconds=statistics.fmean(
                value.execution_seconds for value in values
            ),
            execution_p95_seconds=_percentile(
                (value.execution_seconds for value in values), 95
            ),
        )
        for stage, values in grouped.items()
    )


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0
    position = (len(ordered) - 1) * percentile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _stage_durations(default_seconds: float | None, overrides: list[str]) -> dict[str, float]:
    durations = dict(DEFAULT_STAGE_DURATIONS)
    if default_seconds is not None:
        if default_seconds < 0:
            raise ValueError("stage delay cannot be negative")
        durations = {stage: default_seconds for stage in durations}
    for override in overrides:
        name, separator, raw_value = override.partition("=")
        if not separator or name not in durations:
            raise ValueError(f"Invalid stage duration override: {override}")
        durations[name] = float(raw_value)
    return durations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare end-to-end RepoAgent workers with role-based asynchronous pipelines"
    )
    parser.add_argument("--tasks", type=int, default=20)
    parser.add_argument("--retry-ratio", type=float, default=0.5)
    parser.add_argument(
        "--stage-delay",
        type=float,
        help="Use one common delay for a quick smoke test; omitted means role-specific 10-20s delays",
    )
    parser.add_argument(
        "--stage-duration",
        action="append",
        default=[],
        metavar="STAGE=SECONDS",
        help="Override one stage, for example implement=2.5",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--warmups", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results/pipeline"))
    args = parser.parse_args()
    if args.repetitions < 1 or args.warmups < 0:
        parser.error("--repetitions must be positive and --warmups cannot be negative")
    try:
        workload = build_workload(args.tasks, args.retry_ratio, args.seed)
        durations = _stage_durations(args.stage_delay, args.stage_duration)
    except ValueError as error:
        parser.error(str(error))

    print(
        f"Starting benchmark: tasks={args.tasks}, retry_ratio={args.retry_ratio:.0%}, "
        f"warmups={args.warmups}, repetitions={args.repetitions}",
        flush=True,
    )
    for warmup in range(1, args.warmups + 1):
        for group, experiment in enumerate(EXPERIMENTS, start=1):
            print(
                f"[warmup {warmup}/{args.warmups}] [group {group}/{len(EXPERIMENTS)}] "
                f"START {experiment.key} {experiment.label}",
                flush=True,
            )
            result = run_experiment(experiment, workload, durations, repetition=0)
            print(
                f"[warmup {warmup}/{args.warmups}] [group {group}/{len(EXPERIMENTS)}] "
                f"DONE  {experiment.key} makespan={result.makespan_seconds:.3f}s",
                flush=True,
            )
    measured_runs: list[RunMetrics] = []
    for repetition in range(1, args.repetitions + 1):
        for group, experiment in enumerate(EXPERIMENTS, start=1):
            print(
                f"[run {repetition}/{args.repetitions}] [group {group}/{len(EXPERIMENTS)}] "
                f"START {experiment.key} {experiment.label}",
                flush=True,
            )
            result = run_experiment(experiment, workload, durations, repetition=repetition)
            measured_runs.append(result)
            print(
                f"[run {repetition}/{args.repetitions}] [group {group}/{len(EXPERIMENTS)}] "
                f"DONE  {experiment.key} makespan={result.makespan_seconds:.3f}s "
                f"throughput={result.throughput_tasks_per_minute:.2f}/min",
                flush=True,
            )
    runs = tuple(measured_runs)
    aggregates = aggregate(runs)
    settings = {
        "taskCount": args.tasks,
        "retryRatio": args.retry_ratio,
        "repetitions": args.repetitions,
        "warmups": args.warmups,
        "seed": args.seed,
        "stageDurationsSeconds": durations,
    }
    write_reports(args.output_dir, runs, aggregates, settings)
    print(_markdown_report(aggregates, settings))
    print(f"\nReports written to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
