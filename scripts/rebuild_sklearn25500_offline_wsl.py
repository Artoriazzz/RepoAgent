"""以本地基线 Git bundle 替代镜像构建中的网络 clone。"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import docker


def main():
    root = Path("/home/artoria/workspace/SWE-bench")
    work = root / "agent_work"
    image = "sweb.eval.x86_64.scikit-learn__scikit-learn-25500:latest"
    base = "4db04923a754b6a2defa1b172f55d492b85d165e"
    source = root / "logs/build_images/instances/sweb.eval.x86_64.scikit-learn__scikit-learn-25500__latest"
    context = Path(tempfile.mkdtemp(prefix="sklearn25500-offline-", dir=work))
    bare = context / "baseline.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True)
    subprocess.run(["git", "-C", str(bare), "fetch", str(work / "repo_cache/scikit-learn__scikit-learn.git"), base], check=True)
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/main", "FETCH_HEAD"], check=True)
    subprocess.run(["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "-C", str(bare), "bundle", "create", str(context / "repo.bundle"), "--all"], check=True)
    shutil.rmtree(bare)
    setup = (source / "setup_repo.sh").read_text()
    original = "git clone -o origin  --single-branch https://github.com/scikit-learn/scikit-learn /testbed"
    if setup.count(original) != 1:
        raise ValueError("Unexpected setup script; refusing to change installation steps")
    setup = setup.replace(original, "git clone --no-hardlinks -o origin --single-branch /root/repo.bundle /testbed")
    (context / "setup_repo.sh").write_text(setup)
    dockerfile = (source / "Dockerfile").read_text()
    dockerfile = dockerfile.replace("COPY ./setup_repo.sh /root/", "COPY ./setup_repo.sh /root/\nCOPY ./repo.bundle /root/")
    dockerfile = dockerfile.replace("RUN /bin/bash /root/setup_repo.sh", "RUN /bin/bash /root/setup_repo.sh && rm /root/repo.bundle")
    (context / "Dockerfile").write_text(dockerfile)
    print("Building from baseline-only local bundle", flush=True)
    client = docker.from_env()
    for event in client.api.build(path=str(context), tag=image, network_mode="host", rm=True, decode=True):
        if "stream" in event:
            print(event["stream"], end="", flush=True)
        if "error" in event:
            raise RuntimeError(event["error"])
    command = ["/opt/miniconda3/envs/testbed/bin/python", "-c",
               "import sklearn,numpy; from sklearn.isotonic import IsotonicRegression; print('imports_ok', sklearn.__version__)"]
    print(client.containers.run(image, command, remove=True).decode(), flush=True)
    (work / "sklearn25500-environment-ready.json").write_text(json.dumps(
        {"image": image, "baseline": base, "image_id": client.images.get(image).id, "imports_ok": True}, indent=2))
    print("Environment ready", flush=True)


if __name__ == "__main__":
    main()
