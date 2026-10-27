from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def main() -> None:
    parser = argparse.ArgumentParser(description="Submit SWE-bench instances to RepoAgent Backend")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--instance-id", action="append", default=[])
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", default="lite")
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--run-key", help="Optional suffix for intentionally rerunning an instance")
    parser.add_argument(
        "--issues-only",
        action="store_true",
        help="Import virtual repositories and Issues without creating repair tasks.",
    )
    args = parser.parse_args()

    if args.concurrency < 1 or args.concurrency > 16:
        parser.error("--concurrency must be between 1 and 16")

    rows = select_rows(load_rows(args.dataset), args.instance_id, args.start, args.limit)
    if not rows:
        parser.error("No SWE-bench instances selected")

    failures = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(
                submit_instance,
                args.backend_url,
                args.user_id,
                build_payload(row, args.split),
                args.timeout,
                args.run_key,
                args.issues_only,
            ): str(row["instance_id"])
            for row in rows
        }
        for future in as_completed(futures):
            instance_id = futures[future]
            try:
                response = future.result()
                print(json.dumps({"instance_id": instance_id, "status": "submitted", "response": response}))
            except Exception as error:
                failures += 1
                print(json.dumps({"instance_id": instance_id, "status": "failed", "error": str(error)}))

    if failures:
        raise SystemExit(1)


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else list(data["instances"])
    if path.suffix == ".parquet":
        try:
            import pandas as pd
        except ImportError as error:
            raise RuntimeError("Install RepoAgent with the benchmark extra to read Parquet") from error
        return [row.to_dict() for _, row in pd.read_parquet(path).iterrows()]
    raise ValueError(f"Unsupported dataset format: {path.suffix}")


def select_rows(
    rows: Iterable[dict[str, Any]],
    instance_ids: list[str],
    start: int,
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = list(rows)
    if instance_ids:
        wanted = set(instance_ids)
        selected = [row for row in selected if str(row.get("instance_id")) in wanted]
        missing = wanted - {str(row.get("instance_id")) for row in selected}
        if missing:
            raise ValueError(f"Instances not found: {', '.join(sorted(missing))}")
    else:
        selected = selected[start : None if limit is None else start + limit]
    return selected


def build_payload(row: dict[str, Any], split: str) -> dict[str, str]:
    required = ("instance_id", "repo", "base_commit", "problem_statement")
    missing = [key for key in required if not str(row.get(key) or "").strip()]
    if missing:
        raise ValueError(f"SWE-bench row is missing fields: {', '.join(missing)}")
    return {
        "instanceId": str(row["instance_id"]),
        "repository": str(row["repo"]),
        "baseCommit": str(row["base_commit"]),
        "problemStatement": str(row["problem_statement"]),
        "datasetSplit": split,
    }


def submit_instance(
    backend_url: str,
    user_id: str,
    payload: dict[str, str],
    timeout: int,
    run_key: str | None = None,
    issues_only: bool = False,
) -> dict[str, Any]:
    instance_id = payload["instanceId"]
    idempotency_key = f"swebench:{instance_id}"
    if run_key:
        idempotency_key += f":{run_key}"
        payload = {**payload, "runKey": run_key}
    endpoint = "/api/benchmark/issues" if issues_only else "/api/benchmark/repair-tasks"
    request = Request(
        backend_url.rstrip("/") + endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-User-Id": user_id,
            "Idempotency-Key": idempotency_key,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[-2_000:]
        raise RuntimeError(f"Backend returned {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Backend request failed: {error.reason}") from error


if __name__ == "__main__":
    main()
