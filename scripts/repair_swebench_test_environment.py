"""在评测镜像内锁定历史项目需要的测试依赖，不修改仓库文件。"""

import argparse
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys


def matplotlib_constraints(python_version, numpy_version):
    version = tuple(int(part) for part in numpy_version.split(".")[:2])
    if python_version >= (3, 9) and version >= (1, 24):
        pandas_version = "2.2.3"
    elif version >= (1, 21):
        pandas_version = "1.5.3"
    else:
        pandas_version = "1.3.5"
    return [f"numpy=={numpy_version}", f"pandas=={pandas_version}", "setuptools-scm==7.1.0"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--constraints", required=True, type=Path)
    args = parser.parse_args()
    pins = matplotlib_constraints(sys.version_info[:2], metadata.version("numpy"))
    args.constraints.parent.mkdir(parents=True, exist_ok=True)
    args.constraints.write_text("\n".join(pins) + "\n")
    subprocess.run([sys.executable, "-m", "pip", "install", "--constraint", str(args.constraints),
                    pins[1], pins[2]], check=True)
    # 新工具的 entry point 会覆盖旧 scheme，即使 setuptools_scm 本身已降级。
    subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "vcs-versioning"], check=True)
    print(json.dumps({"test_environment_constraints": pins}), flush=True)


if __name__ == "__main__":
    main()
