#!/usr/bin/python3
# coding: utf-8
"""宝塔 Nginx/Apache 扩展配置接入，兼容清理 0.2.x 旧注入块。"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .settings import DATA_DIR, load_config
from .site_discovery import discover_sites


PANEL_VHOST = Path("/www/server/panel/vhost")
NGINX_DIR = PANEL_VHOST / "nginx"
APACHE_DIR = PANEL_VHOST / "apache"
NGINX_FORMAT = NGINX_DIR / "00-webanalytics-format.conf"
APACHE_FORMAT = APACHE_DIR / "00-webanalytics-format.conf"
NGINX_BIN = Path("/www/server/nginx/sbin/nginx")
APACHE_CTL = Path("/www/server/apache/bin/apachectl")
LEGACY_START = "# WebAnalytics-Config-Start"
LEGACY_END = "# WebAnalytics-Config-End"
INCLUDE_START = "# WebAnalytics-Extension-Start"
INCLUDE_END = "# WebAnalytics-Extension-End"
LEGACY_RE = re.compile(
    r"\n?[ \t]*# WebAnalytics-Config-Start\n.*?\n[ \t]*# WebAnalytics-Config-End\n?",
    re.S,
)
INCLUDE_RE = re.compile(
    r"\n?[ \t]*# WebAnalytics-Extension-Start\n.*?\n[ \t]*# WebAnalytics-Extension-End\n?",
    re.S,
)

NGINX_FORMAT_CONTENT = """# Managed by WebAnalytics. Offline local statistics only.
log_format webanalytics escape=json '{"time":"$time_iso8601","host":"$http_host","remote_addr":"$remote_addr","x_forwarded_for":"$http_x_forwarded_for","x_real_ip":"$http_x_real_ip","true_client_ip":"$http_true_client_ip","cf_connecting_ip":"$http_cf_connecting_ip","ali_cdn_real_ip":"$http_ali_cdn_real_ip","cdn_real_ip":"$http_cdn_real_ip","client_ip":"$http_client_ip","method":"$request_method","uri":"$request_uri","protocol":"$server_protocol","status":$status,"bytes":$body_bytes_sent,"referer":"$http_referer","ua":"$http_user_agent","cookie":"$http_cookie"}';
"""

APACHE_FORMAT_CONTENT = r'''# Managed by WebAnalytics. Offline local statistics only.
<IfModule !logio_module>
    LoadModule logio_module modules/mod_logio.so
</IfModule>
LogFormat '{"time":"%{%Y-%m-%dT%H:%M:%S%z}t","host":"%{Host}i","remote_addr":"%a","x_forwarded_for":"%{X-Forwarded-For}i","x_real_ip":"%{X-Real-IP}i","true_client_ip":"%{True-Client-IP}i","cf_connecting_ip":"%{CF-Connecting-IP}i","ali_cdn_real_ip":"%{Ali-CDN-Real-IP}i","cdn_real_ip":"%{CDN-Real-IP}i","client_ip":"%{Client-IP}i","method":"%m","uri":"%U%q","protocol":"%H","status":%>s,"bytes":%B,"referer":"%{Referer}i","ua":"%{User-Agent}i","cookie":"%{Cookie}i"}' webanalytics
'''


def _strip_blocks(content: str) -> str:
    return LEGACY_RE.sub("\n", INCLUDE_RE.sub("", content))


def _plugin_block(indent: str, panel_site_id: int) -> str:
    """仅用于测试/卸载兼容旧版本注入格式。"""
    return (
        "\n{0}{1}\n{0}access_log syslog:server=unix:/tmp/webanalytics.sock,"
        "nohostname,tag=wa_{2}_access webanalytics;\n{0}{3}"
    ).format(indent, LEGACY_START, panel_site_id, LEGACY_END)


def _inject(content: str, panel_site_id: int) -> str:
    """保留 0.2.x 测试兼容；0.3.x 正式安装不再调用。"""
    access_re = re.compile(r"^(?P<indent>[ \t]*)access_log\s+(?!syslog:).*?;[ \t]*$", re.M)
    clean = LEGACY_RE.sub("\n", content)
    updated, count = access_re.subn(
        lambda match: match.group(0) + _plugin_block(match.group("indent"), panel_site_id),
        clean,
    )
    if count == 0:
        raise RuntimeError("站点配置中未找到 access_log 指令")
    return updated


def _extension_path(vhost_dir: Path, site_name: str) -> Path:
    return vhost_dir / "extension" / Path(site_name).name / "webanalytics.conf"


def _include_line(vhost_dir: Path, site_name: str, web_server: str) -> str:
    wildcard = vhost_dir / "extension" / Path(site_name).name / "*.conf"
    if web_server == "apache":
        return 'IncludeOptional "{}"'.format(str(wildcard))
    return "include {};".format(str(wildcard))


def _has_extension_include(content: str, site_name: str) -> bool:
    safe = re.escape(Path(site_name).name)
    return bool(re.search(r"extension[/\\]{}[/\\]\*\.conf".format(safe), content))


def _ensure_include(content: str, include_line: str, site_name: str, web_server: str) -> str:
    if INCLUDE_START in content and include_line in content:
        return content
    clean = _strip_blocks(content)
    if _has_extension_include(clean, site_name):
        return clean
    block = "\n    {}\n    {}\n    {}\n".format(INCLUDE_START, include_line, INCLUDE_END)
    index = clean.lower().rfind("</virtualhost>") if web_server == "apache" else clean.rfind("}")
    if index < 0:
        raise RuntimeError("站点配置中未找到可插入扩展配置的位置")
    return clean[:index] + block + clean[index:]


def _site_extension(panel_site_id: int, web_server: str) -> str:
    if web_server == "apache":
        return (
            'CustomLog "| /usr/bin/logger -u /tmp/webanalytics.sock -d -S 8192 '
            '-t wa_{}_access" webanalytics\n'.format(panel_site_id)
        )
    return (
        "access_log syslog:server=unix:/tmp/webanalytics.sock,nohostname,"
        "tag=wa_{}_access webanalytics;\n".format(panel_site_id)
    )


def _run(command: List[str]) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
        return result.returncode == 0, result.stdout[-2000:]
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)


def _test_and_reload(web_server: str) -> None:
    if web_server == "apache":
        if not APACHE_CTL.is_file():
            raise RuntimeError("未找到 Apache: {}".format(APACHE_CTL))
        valid, output = _run([str(APACHE_CTL), "-t"])
        if not valid:
            raise RuntimeError("Apache 配置校验失败: {}".format(output))
        valid, output = _run([str(APACHE_CTL), "-k", "graceful"])
        if not valid:
            raise RuntimeError("Apache 重载失败: {}".format(output))
        return
    if not NGINX_BIN.is_file():
        raise RuntimeError("未找到 Nginx: {}".format(NGINX_BIN))
    valid, output = _run([str(NGINX_BIN), "-t"])
    if not valid:
        raise RuntimeError("Nginx 配置校验失败: {}".format(output))
    valid, output = _run([str(NGINX_BIN), "-s", "reload"])
    if not valid:
        raise RuntimeError("Nginx 重载失败: {}".format(output))


def _backup(path: Path, backup_dir: Path, originals: Dict[Path, Optional[bytes]]) -> None:
    if path in originals:
        return
    original = path.read_bytes() if path.exists() else None
    originals[path] = original
    if original is not None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(backup_dir / (path.parent.name + "-" + path.name)))


def _write_if_changed(
    path: Path,
    content: str,
    backup_dir: Path,
    originals: Dict[Path, Optional[bytes]],
) -> bool:
    current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else None
    if current == content:
        return False
    _backup(path, backup_dir, originals)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _remove_if_exists(
    path: Path, backup_dir: Path, originals: Dict[Path, Optional[bytes]]
) -> bool:
    if not path.exists():
        return False
    _backup(path, backup_dir, originals)
    path.unlink()
    return True


def _restore(originals: Dict[Path, Optional[bytes]]) -> None:
    for path, original in originals.items():
        if original is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)


@contextmanager
def _configuration_lock():
    """串行化安装脚本、常驻服务和面板修复触发的配置同步。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_DIR / "configure.lock").open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _configure_all_locked(enable: bool) -> Dict[str, object]:
    config = load_config()
    sites = [site for site in discover_sites(config) if site.panel_site_id > 0]
    backup_dir = DATA_DIR / "webserver-backups" / time.strftime("%Y%m%d-%H%M%S")
    originals: Dict[Path, Optional[bytes]] = {}
    changed_sites: List[str] = []
    changed_servers = set()
    expected_extensions = set()

    try:
        families = {site.web_server for site in sites}
        active_names = {
            "nginx": {Path(site.name).name for site in sites if site.web_server != "apache"},
            "apache": {Path(site.name).name for site in sites if site.web_server == "apache"},
        }
        if enable and "nginx" in families:
            if _write_if_changed(NGINX_FORMAT, NGINX_FORMAT_CONTENT, backup_dir, originals):
                changed_servers.add("nginx")
        if enable and "apache" in families:
            if _write_if_changed(APACHE_FORMAT, APACHE_FORMAT_CONTENT, backup_dir, originals):
                changed_servers.add("apache")

        for site in sites:
            web_server = "apache" if site.web_server == "apache" else "nginx"
            vhost_dir = APACHE_DIR if web_server == "apache" else NGINX_DIR
            vhost_path = vhost_dir / (Path(site.name).name + ".conf")
            extension_path = _extension_path(vhost_dir, site.name)
            expected_extensions.add(extension_path)
            if not vhost_path.is_file():
                continue
            current = vhost_path.read_text(encoding="utf-8", errors="replace")
            updated = (
                _ensure_include(
                    current,
                    _include_line(vhost_dir, site.name, web_server),
                    site.name,
                    web_server,
                )
                if enable
                else _strip_blocks(current)
            )
            changed = _write_if_changed(vhost_path, updated, backup_dir, originals)
            if enable:
                changed = _write_if_changed(
                    extension_path,
                    _site_extension(site.panel_site_id, web_server),
                    backup_dir,
                    originals,
                ) or changed
            else:
                changed = _remove_if_exists(extension_path, backup_dir, originals) or changed
            if changed:
                changed_sites.append(site.name)
                changed_servers.add(web_server)

        for web_server, vhost_dir in (("nginx", NGINX_DIR), ("apache", APACHE_DIR)):
            if vhost_dir.is_dir():
                for vhost_path in vhost_dir.glob("*.conf"):
                    if vhost_path in {NGINX_FORMAT, APACHE_FORMAT}:
                        continue
                    if enable and vhost_path.stem in active_names[web_server]:
                        continue
                    current = vhost_path.read_text(encoding="utf-8", errors="replace")
                    cleaned = _strip_blocks(current)
                    if _write_if_changed(vhost_path, cleaned, backup_dir, originals):
                        changed_servers.add(web_server)
            extension_root = vhost_dir / "extension"
            if extension_root.is_dir():
                for stale in extension_root.glob("*/webanalytics.conf"):
                    if (not enable or stale not in expected_extensions) and _remove_if_exists(
                        stale, backup_dir, originals
                    ):
                        changed_servers.add(web_server)
            if not enable:
                format_path = APACHE_FORMAT if web_server == "apache" else NGINX_FORMAT
                if _remove_if_exists(format_path, backup_dir, originals):
                    changed_servers.add(web_server)

        for web_server in sorted(changed_servers):
            _test_and_reload(web_server)
        return {
            "success": True,
            "changed_sites": sorted(set(changed_sites)),
            "changed_servers": sorted(changed_servers),
            "backup": str(backup_dir) if originals else "",
        }
    except Exception:
        _restore(originals)
        raise


def configure_all(enable: bool) -> Dict[str, object]:
    with _configuration_lock():
        return _configure_all_locked(enable)
