"""Collect a read-only, provenance-preserving snapshot for internal Lite runs."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unicodedata

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_agent.core.frontmatter import parse_frontmatter


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--scan-root", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lite_ids = set(pq.read_table(args.dataset, columns=["instance_id"])["instance_id"].to_pylist())
    if len(lite_ids) != 300:
        raise SystemExit("Expected the full 300-instance Lite test set")
    skip = {".git", ".venv", "__pycache__", "repo_cache", "repos", "work", "source", "subagents", "node_modules"}
    groups = defaultdict(list)
    excluded = []
    counts = Counter()
    seen = set()
    for root in args.scan_root:
        for directory, children, filenames in os.walk(root.resolve()):
            children[:] = sorted(set(children) - skip)
            for filename in sorted(filenames):
                if not filename.startswith("global_experience_") or not filename.endswith(".md"):
                    continue
                path = Path(directory) / filename
                if path in seen or path.is_symlink():
                    continue
                seen.add(path)
                raw = path.read_bytes()
                parsed = parse_frontmatter(raw.decode("utf-8", errors="replace"))
                provenance = parsed.meta.get("provenance", "")
                evidence = f"{path} {provenance}"
                instance_ids = sorted(lite_ids.intersection(re.findall(r"[\w.-]+__[\w.-]+-\d+", evidence)))
                lite_source = bool(instance_ids or re.search(r"\blite\b", evidence, re.IGNORECASE))
                reason = None
                if parsed.meta.get("type") != "global_experience" or not parsed.body.strip():
                    reason = "invalid_global_entry"
                elif any(part.startswith("INVALID-") for part in path.parts):
                    reason = "invalidated_run"
                elif not lite_source:
                    reason = "no_traceable_lite_source"
                item = {"path": str(path), "sha256": sha(raw), "provenance": provenance, "lite_instance_ids": instance_ids}
                if reason:
                    counts[reason] += 1
                    excluded.append({**item, "reason": reason})
                    continue
                body_key = sha(" ".join(unicodedata.normalize("NFC", parsed.body).split()).encode())
                groups[body_key].append(item)
    if not groups:
        raise SystemExit("No traceable Lite global experience found")
    args.output.mkdir(parents=True, exist_ok=False)
    memory_dir = args.output / "global" / "memory"
    memory_dir.mkdir(parents=True)
    entries = []
    for key, sources in sorted(groups.items()):
        representative = Path(sources[0]["path"])
        stem = representative.stem[:160]
        destination = memory_dir / f"{stem}_{key[:16]}.md"
        shutil.copy2(representative, destination)
        destination.chmod(0o444)
        entries.append({"filename": destination.name, "sha256": sha(destination.read_bytes()),
                        "normalized_body_sha256": key, "sources": sources})
    memory_dir.chmod(0o555)
    manifest = {"test_set_adapted": True, "leaderboard_eligible": False,
                "dataset": str(args.dataset), "dataset_sha256": sha(args.dataset.read_bytes()),
                "scan_roots": [str(root) for root in args.scan_root],
                "unique_entries": len(entries), "included_copies": sum(map(len, groups.values())),
                "excluded_counts": dict(counts), "entries": entries, "excluded": excluded}
    manifest_path = args.output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in manifest.items() if key not in {"entries", "excluded"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
