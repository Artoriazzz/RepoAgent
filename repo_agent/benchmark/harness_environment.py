"""构建代理与被测程序的代理设置分别管理。"""

from collections.abc import Mapping


PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def runtime_container_environment(configured: Mapping[str, str], host: Mapping[str, str]) -> dict[str, str]:
    # 空值覆盖镜像内遗留设置；测试专用代理必须明确指定。
    environment = dict.fromkeys(PROXY_KEYS, "")
    proxy = host.get("SWEBENCH_TEST_PROXY", "")
    if proxy:
        environment.update({key: proxy for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
        environment["NO_PROXY"] = environment["no_proxy"] = host.get("SWEBENCH_TEST_NO_PROXY", "")
    # pip 的下载代理不改变 Requests 对 HTTP/HTTPS 的路由。
    download_proxy = host.get("HTTPS_PROXY") or host.get("https_proxy")
    if download_proxy:
        environment["PIP_PROXY"] = download_proxy
    environment.update(configured)
    return environment
