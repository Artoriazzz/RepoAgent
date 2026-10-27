import pytest

from repo_agent.worker.main import WorkerSettings


def test_worker_settings_load_concurrency_and_replay_delay(monkeypatch):
    monkeypatch.setenv("REPO_AGENT_RABBITMQ_URL", "amqp://localhost")
    monkeypatch.setenv("REPO_AGENT_BACKEND_URL", "http://localhost:8080")
    monkeypatch.setenv("REPO_AGENT_WORKER_TOKEN", "test-token")
    monkeypatch.setenv("REPO_AGENT_WORKER_CONCURRENCY", "3")
    monkeypatch.setenv("REPO_AGENT_REPLAY_STAGE_DELAY_SECONDS", "10")

    settings = WorkerSettings.from_environment()

    assert settings.worker_concurrency == 3
    assert settings.replay_stage_delay_seconds == 10
    assert settings.pipeline_enabled is True
    assert settings.pipeline_agent_concurrency == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REPO_AGENT_WORKER_CONCURRENCY", "not-a-number"),
        ("REPO_AGENT_REPLAY_STAGE_DELAY_SECONDS", "not-a-number"),
        ("REPO_AGENT_PIPELINE_AGENT_CONCURRENCY", "4"),
    ],
)
def test_worker_settings_reject_non_numeric_runtime_options(monkeypatch, name, value):
    monkeypatch.setenv("REPO_AGENT_RABBITMQ_URL", "amqp://localhost")
    monkeypatch.setenv("REPO_AGENT_BACKEND_URL", "http://localhost:8080")
    monkeypatch.setenv("REPO_AGENT_WORKER_TOKEN", "test-token")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        WorkerSettings.from_environment()
