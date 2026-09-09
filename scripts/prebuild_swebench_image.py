from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prebuild one SWE-bench instance image.")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--namespace", default="none")
    parser.add_argument("--result-file", required=True, type=Path)
    return parser


def load_instance(dataset: Path, instance_id: str) -> dict[str, Any]:
    import pandas as pd

    frame = pd.read_parquet(dataset)
    selected = frame[frame["instance_id"] == instance_id]
    if selected.empty:
        raise ValueError(f"Instance not found in dataset: {instance_id}")
    return selected.iloc[0].to_dict()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    result: dict[str, Any] = {
        "instance_id": args.instance_id,
        "status": "error",
        "image": "",
        "already_present": False,
    }
    try:
        import docker
        from swebench.harness.docker_build import build_instance_images
        from swebench.harness.test_spec.test_spec import make_test_spec

        instance = load_instance(args.dataset, args.instance_id)
        namespace = None if args.namespace.strip().lower() in {"", "none"} else args.namespace
        test_spec = make_test_spec(
            instance,
            namespace=namespace,
            instance_image_tag="latest",
            env_image_tag="latest",
        )
        client = docker.from_env()
        try:
            client.images.get(test_spec.instance_image_key)
            already_present = True
        except docker.errors.ImageNotFound:
            already_present = False
            build_instance_images(
                client,
                [instance],
                force_rebuild=False,
                max_workers=1,
                namespace=namespace,
                tag="latest",
                env_image_tag="latest",
            )
        image = client.images.get(test_spec.instance_image_key)
        result.update(
            {
                "status": "ready",
                "image": test_spec.instance_image_key,
                "already_present": already_present,
                "image_id": image.short_id,
                "size_bytes": int(image.attrs.get("Size") or 0),
            }
        )
    except Exception as exc:  # noqa: BLE001 - serialize build failures for the batch runner.
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["seconds"] = round(time.time() - started, 3)
    args.result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
