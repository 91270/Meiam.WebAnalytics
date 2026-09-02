#!/bin/bash
PATH=/bin:/sbin:/usr/bin:/usr/sbin:/usr/local/bin:/usr/local/sbin
export PATH

set -u

plugin_name="WebAnalytics"
plugin_path="/www/server/panel/plugin/${plugin_name}"
legacy_data_path="${plugin_path}/data"
persistent_root="/www/server/webanalytics"
data_path="${persistent_root}/data"
panel_python="/www/server/panel/pyenv/bin/python"
service_name="webanalytics.service"
service_file="/etc/systemd/system/${service_name}"

migrate_persistent_data() {
    mkdir -p "${data_path}"
    if [ ! -f "${data_path}/.external-data-v1" ] && [ -d "${legacy_data_path}" ] && [ ! -L "${legacy_data_path}" ]; then
        cp -a "${legacy_data_path}/." "${data_path}/"
    fi
    touch "${data_path}/.external-data-v1"
    chmod 700 "${persistent_root}" "${data_path}"
}

reset_statistics_database() {
    # 兼容从曾经“卸载后保留数据”的旧测试版重装：安装动作始终以日志为准重建。
    rm -f "${data_path}/stats.db" "${data_path}/stats.db-wal" \
        "${data_path}/stats.db-shm" "${data_path}/collector.lock"
}

detect_python() {
    if [ -x "${panel_python}" ]; then
        return 0
    fi
    echo "未找到宝塔面板 Python 运行环境: ${panel_python}"
    return 1
}

install_service() {
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "当前系统不支持 systemd，无法安装实时采集服务"
        return 1
    fi
    cp -f "${plugin_path}/webanalytics.service" "${service_file}"
    chmod 644 "${service_file}"
    systemctl daemon-reload
    systemctl enable "${service_name}"
    systemctl restart "${service_name}"
    for wait_index in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
        [ -S /tmp/webanalytics.sock ] && return 0
        sleep 1
    done
    echo "实时采集服务未能创建 /tmp/webanalytics.sock"
    systemctl status "${service_name}" --no-pager || true
    return 1
}

Install() {
    echo "正在安装网站访问分析..."
    detect_python || exit 1
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "${service_name}" >/dev/null 2>&1 || true
    fi
    migrate_persistent_data
    reset_statistics_database
    chmod 700 "${plugin_path}/scripts/collect.py" "${plugin_path}/scripts/init_db.py" \
        "${plugin_path}/scripts/socket_server.py" "${plugin_path}/scripts/configure_nginx.py" \
        "${plugin_path}/scripts/bootstrap_data.py"
    "${panel_python}" "${plugin_path}/scripts/init_db.py" || exit 1
    rm -f /etc/cron.d/webanalytics /etc/cron.d/webanalytics.tmp
    install_service || exit 1
    if ! "${panel_python}" "${plugin_path}/scripts/configure_nginx.py" enable; then
        systemctl disable --now "${service_name}" || true
        rm -f "${service_file}"
        systemctl daemon-reload
        exit 1
    fi
    echo "================================================"
    echo "安装完成：历史日志正在后台重建，Nginx/Apache 新请求将实时写入统计服务"
}

Update() {
    echo "正在升级网站访问分析..."
    detect_python || exit 1
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "${service_name}" >/dev/null 2>&1 || true
    fi
    migrate_persistent_data
    "${panel_python}" "${plugin_path}/scripts/init_db.py" || exit 1
    chmod 700 "${plugin_path}/scripts/bootstrap_data.py"
    rm -f /etc/cron.d/webanalytics /etc/cron.d/webanalytics.tmp
    install_service || exit 1
    "${panel_python}" "${plugin_path}/scripts/configure_nginx.py" enable || exit 1
    echo "升级完成：已迁移 HLL 去重并同步 Web 服务扩展配置"
}

Uninstall() {
    echo "正在卸载网站访问分析..."
    detect_python || exit 1
    "${panel_python}" "${plugin_path}/scripts/configure_nginx.py" disable || exit 1
    if command -v systemctl >/dev/null 2>&1; then
        systemctl disable --now "${service_name}" || true
        rm -f "${service_file}"
        systemctl daemon-reload
    fi
    rm -f /etc/cron.d/webanalytics /etc/cron.d/webanalytics.tmp /tmp/webanalytics.sock
    rm -rf "${plugin_path}"
    rm -rf "${persistent_root}"
    echo "卸载完成，统计数据库已删除；原始网站日志未被修改，重装时将据此重建"
}

case "${1:-}" in
    install) Install ;;
    update) Update ;;
    uninstall) Uninstall ;;
    *) echo "用法: install.sh {install|update|uninstall}"; exit 1 ;;
esac
