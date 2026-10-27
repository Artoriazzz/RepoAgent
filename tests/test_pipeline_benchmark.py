from repo_agent.benchmark.pipeline_benchmark import (
    DEFAULT_STAGE_DURATIONS,
    EXPERIMENTS,
    _stage_durations,
    aggregate,
    build_workload,
    run_experiment,
    write_reports,
)


def test_benchmark_compares_four_fair_topologies(tmp_path):
    workload = build_workload(18, retry_ratio=0.5, seed=7)
    durations = {
        "coordinator": 0.003,
        "implement": 0.003,
        "validation": 0.003,
        "failure_review": 0.003,
        "corrective_implement": 0.003,
        "experience_memory": 0.003,
    }
    runs = tuple(run_experiment(experiment, workload, durations) for experiment in EXPERIMENTS)
    summary = aggregate(runs)

    assert [item.experiment for item in summary] == [
        "serial-1", "serial-3", "pipeline-1", "pipeline-3"
    ]
    assert all(run.completed_tasks == 18 for run in runs)
    assert all(run.stage_executions == sum(len(task.stages) for task in workload) for run in runs)
    assert summary[1].throughput_gain_vs_serial_1 > 1
    assert summary[3].throughput_gain_vs_serial_1 > summary[1].throughput_gain_vs_serial_1

    settings = {
        "taskCount": 18,
        "retryRatio": 0.5,
        "repetitions": 1,
        "warmups": 0,
        "seed": 7,
        "stageDurationsSeconds": durations,
    }
    write_reports(tmp_path, runs, summary, settings)
    assert tmp_path.joinpath("pipeline-benchmark.json").is_file()
    assert tmp_path.joinpath("pipeline-stage-benchmark.csv").is_file()
    assert {stage.stage for stage in runs[-1].stage_metrics} == set(durations)
    assert "3 个完整 RepoAgent 并行" in tmp_path.joinpath(
        "pipeline-benchmark.md"
    ).read_text(encoding="utf-8")


def test_workload_has_deterministic_retry_mix():
    first = build_workload(20, retry_ratio=0.5, seed=42)
    second = build_workload(20, retry_ratio=0.5, seed=42)

    assert first == second
    assert sum(len(task.stages) == 7 for task in first) == 10


def test_official_benchmark_uses_fixed_role_specific_delays():
    assert _stage_durations(None, []) == DEFAULT_STAGE_DURATIONS
    assert DEFAULT_STAGE_DURATIONS == {
        "coordinator": 12.0,
        "implement": 20.0,
        "validation": 15.0,
        "failure_review": 14.0,
        "corrective_implement": 18.0,
        "experience_memory": 10.0,
    }
    assert set(_stage_durations(0.01, []).values()) == {0.01}
