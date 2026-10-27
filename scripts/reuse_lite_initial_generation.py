"""复用因 Harness 错误而未评测完成的首轮补丁，继续原有评测和纠正流程。"""

import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch

from resume_lite_harness_errors_wsl import saved_generation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--previous-result", required=True, type=Path)
    parser.add_argument("batch_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root / "scripts"))
    sys.path.insert(0, str(args.source_root))
    import swebench_batch as batch

    old = json.loads(args.previous_result.read_text())["passes"][0]
    saved_patch = (Path(old["output_dir"]) / "model.patch").read_text()
    if not saved_patch.strip():
        raise SystemExit("Cannot resume an empty saved patch")
    argv = args.batch_args[1:] if args.batch_args[:1] == ["--"] else args.batch_args
    with patch.object(batch, "run_agent_pass", saved_generation(batch, old, saved_patch)):
        return batch.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
