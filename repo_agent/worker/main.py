from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from repo_agent.core.llm import DEFAULT_MODEL
from repo_agent.worker.backend_client import BackendClient, BackendTransportError
from repo_agent.worker.executor import RepoAgentExecutor
from repo_agent.worker.models import RepairTaskMessage
from repo_agent.worker.service import RepairWorkerService
from repo_agent.worker.workspace import WorktreeManager

LOGGER = logging.getLogger(__name__)
QUEUE = "repoagent.repair.task.created"


@dataclass(frozen=True)
class WorkerSettings:
    rabbitmq_url: str
    backend_url: str
    worker_token: str
    mirror_root: Path
    worktree_root: Path
    output_root: Path
    model: str
    max_steps: int
    timeout_seconds: int
    executable: str

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
        return cls(
            rabbitmq_url=str(required["REPO_AGENT_RABBITMQ_URL"]),
            backend_url=str(required["REPO_AGENT_BACKEND_URL"]),
            worker_token=str(required["REPO_AGENT_WORKER_TOKEN"]),
            mirror_root=Path(os.getenv("REPO_AGENT_MIRROR_ROOT", root / "mirrors")),
            worktree_root=Path(os.getenv("REPO_AGENT_WORKTREE_ROOT", root / "worktrees")),
            output_root=Path(os.getenv("REPO_AGENT_OUTPUT_ROOT", root / "runs")),
            model=os.getenv("REPO_AGENT_MODEL", DEFAULT_MODEL),
            max_steps=int(os.getenv("REPO_AGENT_MAX_STEPS", "16")),
            timeout_seconds=int(os.getenv("REPO_AGENT_TIMEOUT_SECONDS", "3600")),
            executable=os.getenv("REPO_AGENT_EXECUTABLE", "repo-agent"),
        )


def main() -> None:
    logging.basicConfig(level=os.getenv("REPO_AGENT_WORKER_LOG_LEVEL", "INFO"))
    settings = WorkerSettings.from_environment()
    service = RepairWorkerService(
        backend=BackendClient(settings.backend_url, settings.worker_token),
        workspace=WorktreeManager(settings.mirror_root, settings.worktree_root),
        executor=RepoAgentExecutor(
            settings.executable,
            settings.model,
            settings.max_steps,
            settings.timeout_seconds,
        ),
        output_root=settings.output_root,
    )
    _consume(settings.rabbitmq_url, service)


def _consume(rabbitmq_url: str, service: RepairWorkerService) -> None:
    try:
        import pika
    except ImportError as error:
        raise RuntimeError("Install RepoAgent with the worker extra before starting repo-agent-worker") from error

    connection = pika.BlockingConnection(pika.URLParameters(rabbitmq_url))
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE, durable=True)
    channel.basic_qos(prefetch_count=1)

    def consume(channel, method, _properties, body: bytes) -> None:
        try:
            message = RepairTaskMessage.from_payload(json.loads(body.decode("utf-8")))
            service.process(message)
            channel.basic_ack(method.delivery_tag)
        except BackendTransportError as error:
            LOGGER.warning("Backend unavailable; requeueing task: %s", error)
            time.sleep(5)
            channel.basic_nack(method.delivery_tag, requeue=True)
        except Exception:
            LOGGER.exception("Discarding malformed repair task message")
            channel.basic_ack(method.delivery_tag)

    channel.basic_consume(queue=QUEUE, on_message_callback=consume, auto_ack=False)
    LOGGER.info("RepoAgent Worker is consuming %s", QUEUE)
    channel.start_consuming()


if __name__ == "__main__":
    main()
