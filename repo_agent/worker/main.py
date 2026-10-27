from __future__ import annotations

import json
import logging
import multiprocessing
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from repo_agent.core.llm import DEFAULT_MODEL
from repo_agent.core.llm import ChatLLM
from repo_agent.worker.backend_client import BackendClient, BackendTransportError
from repo_agent.worker.executor import RepoAgentExecutor
from repo_agent.worker.evaluator import HistoricalReplayEvaluator, SweBenchHarnessEvaluator
from repo_agent.worker.models import RepairPipelineStage, RepairTaskMessage
from repo_agent.worker.memory_curator import SemanticMemoryCurator
from repo_agent.worker.replay_executor import ReplayRepoAgentExecutor
from repo_agent.worker.service import RepairWorkerService
from repo_agent.worker.workspace import WorktreeManager

LOGGER = logging.getLogger(__name__)
LEGACY_QUEUE = "repoagent.repair.task.created"


@dataclass(frozen=True)
class WorkerSettings:
    rabbitmq_url: str
    backend_url: str
    worker_token: str
    mirror_root: Path
    swebench_mirror_root: Path
    worktree_root: Path
    output_root: Path
    model: str
    max_steps: int
    timeout_seconds: int
    executable: str
    swebench_dataset: Path | None
    swebench_dir: Path | None
    harness_python: str
    harness_timeout_seconds: int
    worker_id: str
    lease_seconds: int
    heartbeat_interval_seconds: int
    memory_mode: str
    memory_recall_limit: int
    execution_mode: str
    replay_predictions: Path | None
    replay_reports_root: Path | None
    worker_concurrency: int
    pipeline_enabled: bool
    pipeline_agent_concurrency: int
    replay_stage_delay_seconds: float

    @classmethod
    def from_environment(cls) -> "WorkerSettings":
        required = {
            "REPO_AGENT_RABBITMQ_URL": os.getenv("REPO_AGENT_RABBITMQ_URL"),
            "REPO_AGENT_BACKEND_URL": os.getenv("REPO_AGENT_BACKEND_URL"),
            "REPO_AGENT_WORKER_TOKEN": os.getenv("REPO_AGENT_WORKER_TOKEN"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        root = Path(os.getenv("REPO_AGENT_WORK_ROOT", "/var/lib/repo-agent"))
        settings = cls(
            rabbitmq_url=str(required["REPO_AGENT_RABBITMQ_URL"]),
            backend_url=str(required["REPO_AGENT_BACKEND_URL"]),
            worker_token=str(required["REPO_AGENT_WORKER_TOKEN"]),
            mirror_root=Path(os.getenv("REPO_AGENT_MIRROR_ROOT", root / "mirrors")),
            swebench_mirror_root=Path(
                os.getenv("REPO_AGENT_SWEBENCH_MIRROR_ROOT", root / "swebench-mirrors")
            ),
            worktree_root=Path(os.getenv("REPO_AGENT_WORKTREE_ROOT", root / "worktrees")),
            output_root=Path(os.getenv("REPO_AGENT_OUTPUT_ROOT", root / "runs")),
            model=os.getenv("REPO_AGENT_MODEL", DEFAULT_MODEL),
            max_steps=int(os.getenv("REPO_AGENT_MAX_STEPS", "16")),
            timeout_seconds=int(os.getenv("REPO_AGENT_TIMEOUT_SECONDS", "3600")),
            executable=os.getenv("REPO_AGENT_EXECUTABLE", "repo-agent"),
            swebench_dataset=(
                Path(value) if (value := os.getenv("REPO_AGENT_SWEBENCH_DATASET")) else None
            ),
            swebench_dir=(
                Path(value) if (value := os.getenv("REPO_AGENT_SWEBENCH_DIR")) else None
            ),
            harness_python=os.getenv("REPO_AGENT_HARNESS_PYTHON", "python3"),
            harness_timeout_seconds=int(os.getenv("REPO_AGENT_HARNESS_TIMEOUT_SECONDS", "900")),
            worker_id=os.getenv("REPO_AGENT_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}"),
            lease_seconds=int(os.getenv("REPO_AGENT_LEASE_SECONDS", "90")),
            heartbeat_interval_seconds=int(
                os.getenv("REPO_AGENT_HEARTBEAT_INTERVAL_SECONDS", "15")
            ),
            # learn 不在修复前召回记忆，只在独立评测成功后沉淀经验。
            memory_mode=os.getenv("REPO_AGENT_MEMORY_MODE", "learn").strip().lower(),
            memory_recall_limit=int(os.getenv("REPO_AGENT_MEMORY_RECALL_LIMIT", "4")),
            execution_mode=os.getenv("REPO_AGENT_EXECUTION_MODE", "live").strip().lower(),
            replay_predictions=(
                Path(value) if (value := os.getenv("REPO_AGENT_REPLAY_PREDICTIONS")) else None
            ),
            replay_reports_root=(
                Path(value) if (value := os.getenv("REPO_AGENT_REPLAY_REPORTS_ROOT")) else None
            ),
            worker_concurrency=int(os.getenv("REPO_AGENT_WORKER_CONCURRENCY", "1")),
            pipeline_enabled=_environment_flag("REPO_AGENT_PIPELINE_ENABLED", default=True),
            pipeline_agent_concurrency=int(
                os.getenv("REPO_AGENT_PIPELINE_AGENT_CONCURRENCY", "3")
            ),
            replay_stage_delay_seconds=float(
                os.getenv("REPO_AGENT_REPLAY_STAGE_DELAY_SECONDS", "10")
            ),
        )
        if not 1 <= settings.pipeline_agent_concurrency <= 3:
            raise ValueError("REPO_AGENT_PIPELINE_AGENT_CONCURRENCY must be between 1 and 3")
        return settings


def main() -> None:
    logging.basicConfig(level=os.getenv("REPO_AGENT_WORKER_LOG_LEVEL", "INFO"))
    settings = WorkerSettings.from_environment()
    if settings.execution_mode not in {"live", "replay"}:
        raise ValueError("REPO_AGENT_EXECUTION_MODE must be live or replay")
    if settings.worker_concurrency < 1:
        raise ValueError("REPO_AGENT_WORKER_CONCURRENCY must be at least 1")
    if settings.replay_stage_delay_seconds < 0:
        raise ValueError("REPO_AGENT_REPLAY_STAGE_DELAY_SECONDS cannot be negative")
    if settings.pipeline_enabled:
        _run_pipeline_worker_processes(settings)
        return
    if settings.worker_concurrency == 1:
        _run_worker(settings, settings.worker_id)
        return
    _run_worker_processes(settings)


def _run_worker(
    settings: WorkerSettings,
    worker_id: str,
    pipeline_stage: RepairPipelineStage | None = None,
) -> None:
    logging.basicConfig(level=os.getenv("REPO_AGENT_WORKER_LOG_LEVEL", "INFO"))
    reported_model = settings.model if settings.execution_mode == "live" else "historical-replay"
    evaluator = None
    if settings.execution_mode == "live" and settings.swebench_dataset and settings.swebench_dir:
        evaluator = SweBenchHarnessEvaluator(
            settings.swebench_dataset,
            settings.swebench_dir,
            reported_model,
            python=settings.harness_python,
            timeout_seconds=settings.harness_timeout_seconds,
            docker_network=os.getenv("REPO_AGENT_HARNESS_DOCKER_NETWORK"),
            proxy=os.getenv("REPO_AGENT_HARNESS_PROXY"),
        )
    backend = BackendClient(settings.backend_url, settings.worker_token)
    if settings.execution_mode == "replay":
        if settings.replay_predictions is None:
            raise ValueError("REPO_AGENT_REPLAY_PREDICTIONS is required in replay mode")
        executor = ReplayRepoAgentExecutor(settings.replay_predictions)
        reports_root = (
            settings.replay_reports_root
            or settings.replay_predictions.parent / "logs"
        )
        evaluator = HistoricalReplayEvaluator(reports_root)
    else:
        executor = RepoAgentExecutor(
            settings.executable,
            settings.model,
            settings.max_steps,
            settings.timeout_seconds,
        )
    memory_curator = None
    if settings.execution_mode == "live" and settings.memory_mode in {"learn", "evolve"}:
        memory_curator = SemanticMemoryCurator(
            backend,
            ChatLLM(model=settings.model, reasoning_effort="high", enable_thinking=True),
        )
    service = RepairWorkerService(
        backend=backend,
        workspace=WorktreeManager(
            settings.mirror_root,
            settings.worktree_root,
            settings.swebench_mirror_root,
        ),
        executor=executor,
        output_root=settings.output_root,
        evaluator=evaluator,
        worker_id=worker_id,
        lease_seconds=settings.lease_seconds,
        model_name=reported_model,
        memory_mode=settings.memory_mode,
        memory_recall_limit=settings.memory_recall_limit,
        memory_curator=memory_curator,
        stage_delay_seconds=(
            settings.replay_stage_delay_seconds if settings.execution_mode == "replay" else 0
        ),
        worker_role=pipeline_stage.worker_role if pipeline_stage is not None else "repair",
    )
    _consume_forever(
        settings.rabbitmq_url,
        service,
        settings.heartbeat_interval_seconds,
        pipeline_stage,
    )


def _run_worker_processes(settings: WorkerSettings) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_run_worker,
            args=(settings, f"{settings.worker_id}-{index + 1}", None),
            name=f"repo-agent-worker-{index + 1}",
        )
        for index in range(settings.worker_concurrency)
    ]

    def stop_workers(_signum=None, _frame=None) -> None:
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, stop_workers)
    try:
        for process in processes:
            process.start()
        LOGGER.info("Started %s independent repair workers", len(processes))
        while True:
            for process in processes:
                process.join(timeout=1)
                if process.exitcode is not None:
                    raise RuntimeError(
                        f"{process.name} stopped unexpectedly with exit code {process.exitcode}"
                    )
    except KeyboardInterrupt:
        LOGGER.info("Stopping repair workers")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _run_pipeline_worker_processes(settings: WorkerSettings) -> None:
    """每个外层 Agent 角色独占一条队列，容量固定为不超过三个运行时实例。"""
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_run_worker,
            args=(
                settings,
                f"{settings.worker_id}-{stage.worker_role}-{index + 1}",
                stage,
            ),
            name=f"repo-agent-{stage.worker_role}-{index + 1}",
        )
        for stage in RepairPipelineStage
        for index in range(settings.pipeline_agent_concurrency)
    ]

    def stop_workers(_signum=None, _frame=None) -> None:
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, stop_workers)
    try:
        for process in processes:
            process.start()
        LOGGER.info(
            "Started %s pipeline Agent workers across %s roles (capacity=%s per role)",
            len(processes),
            len(RepairPipelineStage),
            settings.pipeline_agent_concurrency,
        )
        while True:
            for process in processes:
                process.join(timeout=1)
                if process.exitcode is not None:
                    raise RuntimeError(
                        f"{process.name} stopped unexpectedly with exit code {process.exitcode}"
                    )
    except KeyboardInterrupt:
        LOGGER.info("Stopping pipeline Agent workers")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=5)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _consume_forever(
    rabbitmq_url: str,
    service: RepairWorkerService,
    heartbeat_interval_seconds: int,
    pipeline_stage: RepairPipelineStage | None = None,
) -> None:
    stop = threading.Event()
    service.register_presence()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(service, stop, heartbeat_interval_seconds),
        name="repo-agent-worker-heartbeat",
        daemon=True,
    )
    heartbeat.start()
    delay = 1
    try:
        while True:
            try:
                _consume(rabbitmq_url, service, pipeline_stage)
                delay = 1
            except KeyboardInterrupt:
                return
            except Exception as error:
                LOGGER.warning("RabbitMQ connection lost; reconnecting in %ss: %s", delay, error)
                if stop.wait(delay):
                    return
                delay = min(delay * 2, 30)
    finally:
        stop.set()
        heartbeat.join(timeout=2)
        service.unregister_presence()


def _heartbeat_loop(
    service: RepairWorkerService,
    stop: threading.Event,
    interval_seconds: int,
) -> None:
    service.send_heartbeat()
    while not stop.wait(max(1, interval_seconds)):
        service.send_heartbeat()


def _consume(
    rabbitmq_url: str,
    service: RepairWorkerService,
    pipeline_stage: RepairPipelineStage | None = None,
) -> None:
    try:
        import pika
    except ImportError as error:
        raise RuntimeError("Install RepoAgent with the worker extra before starting repo-agent-worker") from error

    parameters = pika.URLParameters(rabbitmq_url)
    parameters.heartbeat = 60
    parameters.blocked_connection_timeout = 300
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    queue_name = pipeline_stage.queue_name if pipeline_stage is not None else LEGACY_QUEUE
    channel.queue_declare(queue=queue_name, durable=True)
    channel.basic_qos(prefetch_count=1)

    def consume(channel, method, _properties, body: bytes) -> None:
        try:
            message = RepairTaskMessage.from_payload(json.loads(body.decode("utf-8")))
            if pipeline_stage is None:
                service.process(message)
            elif message.stage != pipeline_stage:
                raise ValueError(
                    f"Pipeline message stage {message.stage.value} was delivered to {pipeline_stage.value}"
                )
            else:
                service.process_stage(message)
            channel.basic_ack(method.delivery_tag)
        except BackendTransportError as error:
            LOGGER.warning("Backend unavailable; requeueing task: %s", error)
            time.sleep(5)
            channel.basic_nack(method.delivery_tag, requeue=True)
        except Exception:
            LOGGER.exception("Discarding malformed repair task message")
            channel.basic_ack(method.delivery_tag)

    channel.basic_consume(queue=queue_name, on_message_callback=consume, auto_ack=False)
    LOGGER.info("RepoAgent Worker is consuming %s", queue_name)
    channel.start_consuming()


def _environment_flag(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


if __name__ == "__main__":
    main()
