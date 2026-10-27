"""先统一修复并预检所选题目环境，通过后再串行运行模式一。"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

INSTANCES = ["sympy__sympy-19254", "sympy__sympy-18835", "sympy__sympy-11400",
             "sphinx-doc__sphinx-7975", "sphinx-doc__sphinx-7686"]
EXTRA_INSTANCES = ["sphinx-doc__sphinx-7738", "sympy__sympy-12236", "sympy__sympy-12171"]


def clean_cached_test_image(row, swebench, client, log, *, proxy):
    """修复已缓存的测试依赖或版本标签，保留生产代码与原镜像。"""
    import shutil
    from docker.errors import ImageNotFound

    legacy_sphinx = row["repo"] == "sphinx-doc/sphinx" and str(row.get("version")) == "3.1"
    if row["repo"] not in {"matplotlib/matplotlib", "pytest-dev/pytest"} and not legacy_sphinx:
        return {}
    image = f"sweb.eval.x86_64.{row['instance_id']}:latest"
    original = client.images.get(image)
    label = "repoagent.test-environment"
    revision = "sphinx31-dependencies-plain-20261024" if legacy_sphinx else "dependencies-and-version-tags-20261023"
    if original.labels.get(label) == revision and (not legacy_sphinx or
            original.labels.get("repoagent.base-commit") == row["base_commit"]):
        return {"clean_test_environment": True, "image_id": original.id}
    context = Path(tempfile.mkdtemp(prefix=row["instance_id"] + "-test-clean-", dir=swebench / "agent_work"))
    python = "/opt/miniconda3/envs/testbed/bin/python"
    dockerfile = f"FROM {original.id}\n"
    dockerfile += "ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple PIP_DEFAULT_TIMEOUT=45 PIP_RETRIES=3\n"
    if row["repo"] == "matplotlib/matplotlib":
        shutil.copyfile(Path(__file__).with_name("repair_swebench_test_environment.py"), context / "repair.py")
        dockerfile += ("COPY repair.py /opt/repoagent/repair.py\n"
                       f"RUN {python} /opt/repoagent/repair.py --constraints /opt/repoagent/test-constraints.txt\n"
                       "ENV PIP_CONSTRAINT=/opt/repoagent/test-constraints.txt "
                       "PIP_BUILD_CONSTRAINT=/opt/repoagent/test-constraints.txt\n")
        check = "import warnings,matplotlib,numpy,pandas; warnings.simplefilter('error',DeprecationWarning); print(matplotlib.__version__,numpy.__version__,pandas.__version__)"
    elif legacy_sphinx:
        # 历史官方安装会提交依赖配置改动，核对后记录原始题目基线。
        verify = ("import subprocess; "
                  "subprocess.run(['git','diff','--exit-code','HEAD'],check=True); "
                  f"subprocess.run(['git','merge-base','--is-ancestor','{row['base_commit']}','HEAD'],check=True); "
                  f"changed=subprocess.check_output(['git','diff','--name-only','{row['base_commit']}'],text=True).splitlines(); "
                  "assert set(changed) <= {'setup.py','tox.ini'}, changed; print(changed)")
        log.write(client.containers.run(image, [python, "-c", verify], working_dir="/testbed", remove=True).decode())
        dockerfile += f'LABEL repoagent.base-commit="{row["base_commit"]}"\n'
        # 使用支持 Python 3.9 的旧版测试栈；约束同样用于 Harness 的重新安装。
        pins = ["pytest==6.2.5", "pytest-cov==3.0.0", "docutils==0.16", "setuptools==68.2.2", "roman==3.3"]
        (context / "constraints.txt").write_text("\n".join(pins) + "\n")
        dockerfile += ("COPY constraints.txt /opt/repoagent/sphinx-test-constraints.txt\n"
                       "ENV PIP_CONSTRAINT=/opt/repoagent/sphinx-test-constraints.txt "
                       "PIP_BUILD_CONSTRAINT=/opt/repoagent/sphinx-test-constraints.txt\n"
                       'ENV PYTEST_ADDOPTS="--assert=plain --showlocals"\n'
                       f"RUN {python} -m pip install " + " ".join(pins) + "\n")
        check = ("import pytest,docutils,sphinx; "
                 "assert pytest.__version__ == '6.2.5'; assert docutils.__version__ == '0.16'; "
                 "print(pytest.__version__,docutils.__version__,sphinx.__version__)")
    else:
        mirror = swebench / "agent_work/repo_cache" / (row["repo"].replace("/", "__") + ".git")
        tag = subprocess.check_output(["git", "-C", str(mirror), "describe", "--tags", "--abbrev=0",
                                       row["base_commit"]], text=True).strip()
        subprocess.run(["git", "-C", str(mirror), "bundle", "create", str(context / "version.bundle"), tag],
                       check=True, stdout=log, stderr=subprocess.STDOUT)
        dockerfile += ("COPY version.bundle /opt/repoagent/version.bundle\n"
                       "RUN cd /testbed && git fetch /opt/repoagent/version.bundle 'refs/tags/*:refs/tags/*' "
                       f"&& {python} -m pip install -e .\n")
        check = "import re,pytest; print(pytest.__version__); assert int(re.match(r'([0-9]+)', pytest.__version__).group(1)) >= 2"
    dockerfile += f'LABEL {label}="{revision}"\n'
    (context / "Dockerfile").write_text(dockerfile)
    backup = image.rsplit(":", 1)[0] + (":before-sphinx31-clean-20261024" if legacy_sphinx else ":before-test-clean-20261023")
    try:
        client.images.get(backup)
    except ImageNotFound:
        original.tag(backup)
    for event in client.api.build(path=str(context), tag=image, network_mode="host", rm=True, decode=True,
                                  buildargs={"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
                                             "NO_PROXY": "localhost,127.0.0.1,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn",
                                             "no_proxy": "localhost,127.0.0.1,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn"}):
        log.write(event.get("stream", ""))
        log.flush()
        if event.get("error"):
            raise RuntimeError(event["error"])
    data = client.containers.run(image, [python, "-c", check], remove=True)
    log.write(data.decode())
    return {"clean_test_environment": True, "image_id": client.images.get(image).id,
            "original_image_id": original.id, "backup_image": backup}


def prepare_image(row, swebench, client, log, *, imports=None, proxy=None, wheelhouse=None,
                  apt_mirror=None, build_assets=None, canonical=False, clean_tests=False):
    import shutil

    instance = row["instance_id"]
    image = f"sweb.eval.x86_64.{instance}:latest"
    source = swebench / "logs/build_images/instances" / image.replace(":", "__")
    context = Path(tempfile.mkdtemp(prefix=instance + "-offline-", dir=swebench / "agent_work"))
    bare = context / "baseline.git"
    mirror = swebench / "agent_work/repo_cache" / (row["repo"].replace("/", "__") + ".git")
    for command in (["git", "init", "--bare", str(bare)],
                    ["git", "-C", str(bare), "fetch", str(mirror), row["base_commit"]],
                    ["git", "-C", str(bare), "update-ref", "refs/heads/main", "FETCH_HEAD"],
                    ["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"],
                    ["git", "-C", str(bare), "bundle", "create", str(context / "repo.bundle"), "--all"]):
        subprocess.run(command, check=True, stdout=log, stderr=subprocess.STDOUT)
    shutil.rmtree(bare)
    generated_scripts = canonical or not all((source / name).is_file()
                                              for name in ("setup_repo.sh", "Dockerfile"))
    if generated_scripts:
        from swebench.harness.test_spec.test_spec import make_test_spec
        from swebench.harness.docker_build import build_env_images
        from docker.errors import ImageNotFound

        spec = make_test_spec(row)
        try:
            client.images.get(spec.env_image_key)
        except ImageNotFound:
            _, failed = build_env_images(client, [spec], False, 1)
            if failed:
                raise RuntimeError(f"Environment image build failed: {spec.env_image_key}")
        setup, dockerfile = spec.install_repo_script, spec.instance_dockerfile
        log.write("Using generated official Harness build scripts\n")
    else:
        setup = (source / "setup_repo.sh").read_text()
        dockerfile = (source / "Dockerfile").read_text()
    if f"git reset --hard {row['base_commit']}" not in setup:
        raise ValueError("Saved build script baseline does not match dataset")
    setup, count = re.subn(r"git clone[^\n]*?(?:https://github\.com/|http://(?:127\.0\.0\.1|172\.17\.0\.1):\d+/)[^\s]+ /testbed",
                          "git clone --no-hardlinks -o origin --single-branch /root/repo.bundle /testbed", setup)
    if count != 1:
        raise ValueError("Unexpected clone command in saved harness context")
    if apt_mirror:
        # 仅替换系统软件包下载地址，仍使用 Ubuntu 签名和原有安装步骤。
        mirror_setup = (
            "sed -i -E 's#https?://(archive|security).ubuntu.com/ubuntu#"
            + apt_mirror.rstrip("/")
            + "#g' /etc/apt/sources.list\n"
            "printf '%s\\n' 'Acquire::Retries \"5\";' "
            "'Acquire::http::Timeout \"30\";' 'Acquire::https::Timeout \"30\";' "
            "> /etc/apt/apt.conf.d/80-swebench-retries\n"
        )
        setup = setup.replace("apt-get -y update", mirror_setup + "apt-get -y update")
    if build_assets is not None and row["repo"] == "matplotlib/matplotlib":
        # 预先下载官方源码包，避免构建进程重复请求不稳定的上游站点。
        shutil.copytree(build_assets, context / "build-assets")
        unpack = ("mkdir -p /testbed/build\n"
                  "for archive in /opt/repoagent-build-assets/freetype-*.tar.gz; do "
                  "tar -xzf \"$archive\" -C /testbed/build; done\n")
        setup = setup.replace("python -m pip install", unpack + "python -m pip install")
        setup = setup.replace('wget -O "$QHULL_TAR" "$QHULL_URL"',
                              'cp /opt/repoagent-build-assets/qhull-2020-src-8.0.2.tgz "$QHULL_TAR"')
    (context / "setup_repo.sh").write_text(setup)
    dockerfile = dockerfile.replace("COPY ./setup_repo.sh /root/", "COPY ./setup_repo.sh /root/\nCOPY ./repo.bundle /root/")
    dockerfile = dockerfile.replace("RUN /bin/bash /root/setup_repo.sh", "RUN /bin/bash /root/setup_repo.sh && rm /root/repo.bundle")
    if build_assets is not None and row["repo"] == "matplotlib/matplotlib":
        dockerfile = dockerfile.replace("COPY ./setup_repo.sh /root/",
                                       "COPY ./build-assets /opt/repoagent-build-assets\nCOPY ./setup_repo.sh /root/")
    # 缓存的历史构建脚本也需要镜像源，隔离构建的依赖会继承这些配置。
    dockerfile = dockerfile.replace("COPY ./setup_repo.sh /root/",
                                   "ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple PIP_DEFAULT_TIMEOUT=45 PIP_RETRIES=5\nCOPY ./setup_repo.sh /root/")
    if canonical:
        if row["repo"] == "pylint-dev/pylint":
            # setuptools 62 不支持 PEP 660；pip 24 保留该项目需要的 legacy editable 安装。
            dockerfile = dockerfile.replace("RUN /bin/bash /root/setup_repo.sh",
                                           'RUN /opt/miniconda3/envs/testbed/bin/python -m pip install "pip==24.0"\nRUN /bin/bash /root/setup_repo.sh')
    if wheelhouse is not None:
        # 保留 pip 的隔离构建，只把构建依赖改为本地 wheel，评测阶段也使用同一缓存。
        shutil.copytree(wheelhouse, context / "wheelhouse")
        dockerfile = dockerfile.replace(
            "COPY ./setup_repo.sh /root/",
            "COPY ./wheelhouse /opt/repoagent-wheelhouse\n"
            "ENV PIP_NO_INDEX=1 PIP_FIND_LINKS=/opt/repoagent-wheelhouse PIP_RETRIES=1 PIP_DEFAULT_TIMEOUT=15\n"
            "COPY ./setup_repo.sh /root/",
        )
    sphinx = row["repo"] == "sphinx-doc/sphinx"
    if sphinx:
        # 独立补齐缺失依赖，不改项目源码、测试或警告规则。
        subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "roman==3.3",
                        "--index-url", "https://pypi.tuna.tsinghua.edu.cn/simple", "--timeout", "45", "--retries", "3",
                        "--dest", str(context)], check=True, stdout=log, stderr=subprocess.STDOUT)
        wheel = next(context.glob("roman-*.whl"))
        dockerfile += (f"\nCOPY ./{wheel.name} /root/{wheel.name}\n"
                       f"RUN /opt/miniconda3/envs/testbed/bin/python -m pip install --no-deps /root/{wheel.name}\n")
    if clean_tests and row["repo"] == "matplotlib/matplotlib":
        shutil.copyfile(Path(__file__).with_name("repair_swebench_test_environment.py"),
                        context / "repair_test_environment.py")
        dockerfile += (
            "\nCOPY ./repair_test_environment.py /opt/repoagent/repair_test_environment.py\n"
            "RUN /opt/miniconda3/envs/testbed/bin/python /opt/repoagent/repair_test_environment.py "
            "--constraints /opt/repoagent/test-constraints.txt\n"
            "ENV PIP_CONSTRAINT=/opt/repoagent/test-constraints.txt "
            "PIP_BUILD_CONSTRAINT=/opt/repoagent/test-constraints.txt\n"
        )
    dockerfile += f'\nLABEL repoagent.base-commit="{row["base_commit"]}"\n'
    (context / "Dockerfile").write_text(dockerfile)
    buildargs = ({name: proxy for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")}
                 if proxy is not None else {})
    if proxy:
        bypass = os.environ.get("NO_PROXY", "localhost,127.0.0.1,pypi.tuna.tsinghua.edu.cn,mirrors.ustc.edu.cn")
        buildargs.update(NO_PROXY=bypass, no_proxy=bypass)
    if proxy == "":
        buildargs.update(NO_PROXY="*", no_proxy="*")
    for event in client.api.build(path=str(context), tag=image, network_mode="host", rm=True,
                                  decode=True, buildargs=buildargs):
        log.write(event.get("stream", ""))
        log.flush()
        if event.get("error"):
            raise RuntimeError(event["error"])
    imports = imports or ("import sphinx,docutils,roman,pytest; import sphinx.testing.fixtures; "
               "import sphinx.ext.autosummary.generate; import sphinx.environment.adapters.indexentries"
               if sphinx else "import sympy,mpmath; import sympy.printing; import sympy.utilities.runtests")
    output = client.containers.run(image, ["/opt/miniconda3/envs/testbed/bin/python", "-c",
                                           imports + "; print(\"imports_ok\")"], remove=True)
    log.write(output.decode())
    if sphinx and instance in {"sphinx-doc__sphinx-7686", "sphinx-doc__sphinx-7738"}:
        path = {
            "sphinx-doc__sphinx-7686": "tests/test_ext_autosummary.py",
            "sphinx-doc__sphinx-7738": "tests/test_ext_napoleon_docstring.py",
        }[instance]
        output = client.containers.run(image,
            ["/opt/miniconda3/envs/testbed/bin/python", "-m", "pytest", "--collect-only", "-q", path],
            working_dir="/testbed", remove=True)
        log.write(output.decode())
    return {"status": "ready", "image": image, "image_id": client.images.get(image).id,
            "baseline": row["base_commit"], "roman": "3.3" if sphinx else None,
            "generated_build_scripts": generated_scripts}


def main():
    import docker
    import pandas as pd

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-name", default="expbuild-failed5-preflight-glm-20261014")
    parser.add_argument("--instance-id", action="append", choices=INSTANCES + EXTRA_INSTANCES)
    args = parser.parse_args()
    instances = args.instance_id or INSTANCES
    count = len(instances)
    project = Path(__file__).resolve().parents[1]
    swebench = Path("/home/artoria/workspace/SWE-bench")
    work = swebench / "agent_work"
    output = work / "runs" / args.run_name
    output.mkdir(parents=True, exist_ok=False)
    dataset = swebench / "test-00000-of-00001.parquet"
    frame = pd.read_parquet(dataset)
    rows = {row["instance_id"]: row.to_dict() for _, row in frame.iterrows() if row["instance_id"] in instances}
    (output / "instances.json").write_text(json.dumps(instances, indent=2))
    manifest = {}

    def progress(message):
        (output / "progress.txt").write_text(message + "\n")
        print(message, flush=True)

    client = docker.from_env()
    for index, instance in enumerate(instances, 1):
        progress(f"preflight {index}/{count} {instance}")
        try:
            with (output / (instance + "-environment.log")).open("w") as log:
                manifest[instance] = prepare_image(rows[instance], swebench, client, log)
        except Exception as exc:
            manifest[instance] = {"status": "error", "error": str(exc)}
        (output / "environment.json").write_text(json.dumps(manifest, indent=2))
        progress(f"preflight {index}/{count} {instance} {manifest[instance]['status']}")
    if any(value["status"] != "ready" for value in manifest.values()):
        progress("blocked: environment preflight failed; no agent experiments started")
        return 1
    env = os.environ.copy()
    env.update(NO_PROXY="open.bigmodel.cn", no_proxy="open.bigmodel.cn",
               REPO_AGENT_LLM_TIMEOUT_SECONDS="300", REPO_AGENT_LLM_MAX_RETRIES="1",
               SWEBENCH_RAW_PROXY="http://127.0.0.1:7897", SWEBENCH_RAW_URL_PREFIX="https://ghproxy.net")
    results = []
    for index, instance in enumerate(instances, 1):
        progress(f"running {index}/{count} {instance}")
        case = output / instance
        command = [str(project / ".venv/bin/python"), str(project / "scripts/swebench_batch.py"),
            "--dataset", str(dataset), "--repo-agent-bin", str(project / ".venv/bin/repo-agent"),
            "--model", "glm-5.3-flash", "--implement-model", "glm-5.3-flash", "--reasoning-effort", "low",
            "--coordinator-reasoning-effort", "high", "--implement-reasoning-effort", "low",
            "--work-dir", str(work), "--output-dir", str(case), "--env-file", str(project / ".env"),
            "--proxy", "http://127.0.0.1:7897", "--offline-cache", "--instance-id", instance,
            "--max-steps", "16", "--repair-mode", "experience-build", "--timeout", "3600", "--evaluate",
            "--swebench-dir", str(swebench), "--harness-python", str(swebench / ".venv/bin/python"),
            "--run-id", args.run_name + "-" + str(index), "--harness-timeout", "900", "--cache-level", "env",
            "--namespace", "none", "--docker-network", "host", "--candidate-mode", "off",
            "--memory-provenance", "swebench-test-diagnostic", "--no-prebuild-instance-image"]
        with (output / (instance + ".log")).open("w") as log:
            case_env = {**env, "REPO_AGENT_LLM_LOG_DIR": str(case / "llm-requests")}
            completed = subprocess.run(command, cwd=project, env=case_env, stdout=log, stderr=subprocess.STDOUT)
        summary = case / "batch_summary.json"
        result = json.loads(summary.read_text())["instances"][0] if summary.exists() else {
            "instance_id": instance, "status": "runner_failed", "resolved": False}
        result["process_exit_code"] = completed.returncode
        results.append(result)
        (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        progress(f"finished {index}/{count} {instance} status={result.get('status')} resolved={result.get('resolved')}")
    progress(f"finished {count}/{count} resolved={sum(bool(r.get('resolved')) for r in results)}/{count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
