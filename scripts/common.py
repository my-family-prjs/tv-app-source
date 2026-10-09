#!/usr/bin/env python3
"""公共工具模块。

为 fetch_cms.py / fetch_iptv.py / build_config.py 提供共同的小工具函数：
配置加载、HTTP 下载（带超时与重试）、日志打印。全部只依赖 Python 标准库。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

# 项目根目录（本文件位于 scripts/ 下，根目录是其上一级）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def log(message):
    """带时间戳的日志输出。"""
    print(time.strftime("[%H:%M:%S] ") + str(message), flush=True)


def load_config(path=None):
    """读取 config.json，返回字典。找不到文件时直接退出。"""
    config_path = path or DEFAULT_CONFIG_PATH
    if not os.path.isfile(config_path):
        log("错误：找不到配置文件 %s" % config_path)
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def project_path(relative):
    """把相对于项目根目录的路径转换为绝对路径。"""
    if os.path.isabs(relative):
        return relative
    return os.path.join(PROJECT_ROOT, relative)


GITHUB_HOSTS = ("raw.githubusercontent.com", "github.com", "gist.githubusercontent.com")


def with_github_proxy(url, proxy_prefix):
    """给 GitHub 相关域名加上代理前缀，用于国内直连访问。

    proxy_prefix 形如 https://gh-proxy.com/ 或自建 githubproxy 的地址
    （需以 / 结尾）。改写规则：https://raw.githubusercontent.com/a/b/c
    → https://gh-proxy.com/https://raw.githubusercontent.com/a/b/c。
    前缀为空、URL 不属于 GitHub 域名时原样返回。
    """
    prefix = (proxy_prefix or "").strip()
    if not prefix:
        return url
    from urllib.parse import urlsplit
    host = urlsplit(url).netloc.lower()
    if not any(host == h or host.endswith("." + h) for h in GITHUB_HOSTS):
        return url
    return prefix.rstrip("/") + "/" + url


def http_get(url, timeout=10, retries=2, retry_delay=1, user_agent=None,
             max_bytes=0, method="GET", proxy=None):
    """用 urllib 请求一个 URL。

    返回 (状态码, 响应体 bytes, 错误信息)。
    成功时错误信息为 None；失败时状态码与响应体可能为 None/空。
    max_bytes 大于 0 时读到该字节数即提前停止（用于直播源轻量抽测）。
    proxy 为代理地址（如 http://127.0.0.1:7890），用于访问国内直连不了的
    域名（如 raw.githubusercontent.com）；不填时强制直连（忽略系统代理环境变量）。
    """
    headers = {
        "User-Agent": user_agent or DEFAULT_UA,
        "Accept": "*/*",
    }
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        # 显式直连：不走系统代理环境变量，保证测活结果反映真实直连可达性
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = None
    for attempt in range(1, retries + 2):  # 首次 + retries 次重试
        try:
            request = urllib.request.Request(url, headers=headers, method=method)
            with opener.open(request, timeout=timeout) as response:
                status = response.status
                if max_bytes > 0:
                    body = response.read(max_bytes)
                else:
                    body = response.read()
                return status, body, None
        except urllib.error.HTTPError as exc:
            last_error = "HTTP %s" % exc.code
        except urllib.error.URLError as exc:
            last_error = "URLError: %s" % (exc.reason,)
        except (TimeoutError, OSError) as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
        except Exception as exc:  # 兜底，避免单个站点把整个任务打断
            last_error = "%s: %s" % (type(exc).__name__, exc)
        if attempt <= retries:
            time.sleep(retry_delay)
    return None, b"", last_error


def http_get_text(url, **kwargs):
    """http_get 的文本版，返回 (状态码, 文本, 错误信息)。"""
    status, body, error = http_get(url, **kwargs)
    if error is not None:
        return status, "", error
    return status, body.decode("utf-8", errors="replace"), None


def write_text(path, text):
    """写出文本文件，自动创建父目录。"""
    abs_path = project_path(path)
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    log("已写出文件：%s（%d 字节）" % (abs_path, len(text.encode("utf-8"))))


def write_json(path, data):
    """写出 UTF-8、带缩进的 JSON 文件（ensure_ascii=False，保证中文可读）。"""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
