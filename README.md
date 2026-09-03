# Meiam.WebAnalytics

面向宝塔 Linux 面板 v11.x 的本地网站访问分析插件。它直接分析 Nginx / Apache 访问日志，在宝塔面板内提供实时趋势、访客、蜘蛛、客户端、IP、URI、错误日志和统计报告。

项目默认完全离线运行：不插入网站前端代码，不依赖外部 SaaS，不向第三方发送域名、日志或访问 IP。

## 功能

- 概览：PV、UV、独立 IP、响应流量、请求数、实时流量、QPS，以及当前时段与上一时段趋势对比。
- 网站列表：全部宝塔站点的采集状态、Web 服务、核心指标、错误率和最后数据时间。
- 蜘蛛统计：识别百度、Google、Bing、搜狗、360、神马、Yandex、GPTBot、ClaudeBot 等搜索引擎和 AI 爬虫。
- 客户端统计：浏览器、操作系统和桌面端、移动端、平板、爬虫设备分布。
- IP 与 URI：请求量、流量、错误率、最后访问时间、离线 IP 归属地及页面排行。
- 错误与网站日志：40x/50x 筛选、URI/IP/User-Agent 搜索、分页和安全 CSV 导出。
- 统计报告与设置：时段报告、保留期、采集开关、队列容量、URI 排除规则和数据清理。

## 工作原理

```text
Nginx / Apache access log
          │
          ▼
 /tmp/webanalytics.sock
          │
          ▼
 systemd 实时采集服务
          │
          ├── 日志解析与可信代理 IP 识别
          ├── 蜘蛛、浏览器、系统和设备分类
          ├── 分钟指标与 HLL 去重
          └── 历史日志和轮转归档恢复
          │
          ▼
 SQLite WAL → 宝塔插件接口 → 面板报表
```

插件通过宝塔站点扩展配置接入日志，不覆盖网站原有 `access_log`。修改配置前创建备份，只有 `nginx -t` 或 `apachectl -t` 成功后才重载，失败自动回滚。

## 环境要求

- 宝塔 Linux 面板 v11.x
- Nginx 或 Apache
- systemd
- 宝塔面板自带 Python 3 环境
- Ubuntu 22.04/24.04、Debian 12 或 Rocky Linux 9

宝塔 v10.x 尚未列入正式兼容范围，需要单独回归测试。

## 安装

从 [Releases](https://github.com/91270/Meiam.WebAnalytics/releases) 下载最新的 `WebAnalytics-*.zip`。解压后确保目录为：

```text
/www/server/panel/plugin/WebAnalytics/
```

以 root 身份执行：

```bash
cd /www/server/panel/plugin/WebAnalytics
bash install.sh install
```

安装后在宝塔面板中打开“网站访问分析”。首次安装会在后台导入当前访问日志、普通轮转日志、`.gz` 文件和 tar 日志归档；大日志可在服务重启后继续恢复。

## 升级

用新版本覆盖插件目录后执行：

```bash
cd /www/server/panel/plugin/WebAnalytics
bash install.sh update
```

运行数据独立保存在 `/www/server/webanalytics/data`，正常覆盖升级不会清空统计。

## 卸载

```bash
cd /www/server/panel/plugin/WebAnalytics
bash install.sh uninstall
```

卸载会移除插件扩展配置、systemd 服务、Socket 和插件数据库，但不会删除或修改网站的 Nginx / Apache 原始日志。

## 数据与保留策略

| 数据 | 默认保留时间 | 用途 |
| --- | ---: | --- |
| 分钟核心指标 | 长期 | PV、UV、IP、流量和趋势 |
| 数据库访问明细 | 7 天 | 网站日志逐条查询与导出 |
| 错误访问明细 | 30 天 | 40x / 50x 日志分析 |
| IP、URI、客户端日聚合 | 90 天 | 排行与分布统计 |

这些设置只影响插件自己的 SQLite 数据库，不会清理 `/www/wwwlogs` 中的网站日志。

主要路径：

- 插件代码：`/www/server/panel/plugin/WebAnalytics`
- 持久数据：`/www/server/webanalytics/data`
- 数据库：`/www/server/webanalytics/data/stats.db`
- 配置：`/www/server/webanalytics/data/config.json`
- Unix Socket：`/tmp/webanalytics.sock`
- systemd 服务：`webanalytics.service`

## IP 归属地

归属地查询完全在服务器本地完成。插件自动查找常见位置的 GeoLite2 / GeoIP2 City MMDB，也可以把数据库放到：

```text
/www/server/webanalytics/data/GeoLite2-City.mmdb
```

运行环境需提供 `geoip2` 或 `maxminddb` Python 模块。没有本地数据库时，公网地址显示“未知”；本机、内网、保留和无效地址仍可识别。

## 统计口径

- 请求：成功解析并纳入统计的访问日志记录。
- PV：GET/HEAD 的 2xx/3xx 请求，排除静态资源和已识别爬虫。
- UV：按 `IP + User-Agent` 匿名指纹进行 HyperLogLog 近似去重。
- 独立 IP：按来源 IP 进行 HyperLogLog 近似去重。
- 流量：访问日志记录的响应字节数，不包含 TLS、请求体和网络层开销。
- 实时流量：所选时段中最近一个已采集分钟的响应字节数。
- QPS：最近一个已采集分钟的请求数除以 60。
- 错误：HTTP 状态码 400–599。

## 反向代理与 CDN

插件只在请求来自可信代理网段时采用转发头中的真实客户端 IP，支持 `X-Forwarded-For`、`X-Real-IP`、`True-Client-IP`、`CF-Connecting-IP` 和常见 CDN 客户端 IP 头。

## 服务检查

```bash
systemctl status webanalytics.service
journalctl -u webanalytics.service -n 100 --no-pager
ls -l /tmp/webanalytics.sock
```

界面会分别显示实时服务、Socket、站点 Web 服务配置、历史恢复和实际收包状态，并在可安全处理时提供修复入口。

## 安全与隐私

- 不开放额外 TCP/UDP 公网端口，不创建 cron 采集任务。
- 不加载外部 JS、CSS 或 CDN 资源。
- SQL 使用参数绑定，排序和分页参数使用白名单或范围限制。
- 日志字段展示前进行 HTML 转义，CSV 导出防止公式注入。
- 日志路径限制在允许的网站日志目录内。
- 清理插件数据需要显式确认，不操作网站原始日志。

## 开发与测试

在仓库根目录执行：

```bash
python -m unittest discover -s WebAnalytics/tests -v
```

自动化测试覆盖日志解析、增量采集、轮转与压缩归档、HLL、站点发现、反向代理、实时队列、数据库迁移、蜘蛛维度和 IP 归属地基础行为。

正式发布前仍应在宝塔 v11.x 真机验证安装、升级、卸载、systemd、Unix Socket、Nginx/Apache 配置回滚及持续写入场景。

## 版本记录

完整变更参见 [CHANGELOG.md](CHANGELOG.md)。

## 作者

- Developer: Meiam
- Website: [592.la](https://592.la)
- Repository: [91270/Meiam.WebAnalytics](https://github.com/91270/Meiam.WebAnalytics)
