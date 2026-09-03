<div align="center">
  <img src="icon.png" width="112" height="112" alt="Meiam.WebAnalytics">
  <h1>Meiam.WebAnalytics</h1>
  <p>运行在宝塔 Linux 面板中的本地网站访问分析插件</p>
  <p>
    <a href="https://github.com/91270/Meiam.WebAnalytics/releases"><img src="https://img.shields.io/github/v/release/91270/Meiam.WebAnalytics?style=flat-square" alt="Release"></a>
    <a href="https://github.com/91270/Meiam.WebAnalytics/actions"><img src="https://img.shields.io/badge/tests-42%20passed-20a53a?style=flat-square" alt="Tests"></a>
    <img src="https://img.shields.io/badge/BaoTa-v11.x-20a53a?style=flat-square" alt="BaoTa v11.x">
    <img src="https://img.shields.io/badge/Python-3.x-3776ab?style=flat-square" alt="Python 3">
  </p>
</div>

WebAnalytics 直接分析服务器上的 Nginx / Apache 访问日志，在宝塔面板内提供实时趋势、访客、蜘蛛、客户端、IP、URI、错误日志和统计报告。

无需修改网站代码，不注入前端脚本，不依赖外部 SaaS。域名、日志和访问 IP 默认只保存在你的服务器上。

## 为什么选择 WebAnalytics

- **真正本地化**：采集、存储、查询和 IP 归属地均在本机完成。
- **秒级采集**：Nginx / Apache 通过 Unix Socket 将访问日志实时送入常驻服务。
- **历史可恢复**：支持当前日志、轮转日志、`.gz` 和 tar 归档，重启后可继续导入。
- **低资源占用**：SQLite WAL、批量事务、有界队列与 HyperLogLog 去重。
- **面向运维**：采集健康状态、配置自愈、错误分析和多站点集中对比。
- **安全接入**：不覆盖原访问日志；配置检查失败时自动回滚。

## 功能一览

| 模块 | 能力 |
| --- | --- |
| 概览 | PV、UV、独立 IP、流量、请求、实时流量、QPS、同期趋势 |
| 网站列表 | 多站点采集状态、Web 服务、核心指标、错误率、最后数据时间 |
| 蜘蛛统计 | 百度、Google、Bing、搜狗、360、神马及常见 AI 爬虫 |
| 客户端统计 | 浏览器、操作系统、桌面端、移动端、平板和爬虫设备 |
| IP 统计 | 请求、流量、错误率、最后访问时间和离线归属地 |
| URI 统计 | 页面与接口访问排行、流量和错误率 |
| 错误日志 | 40x / 50x 筛选、搜索、分页和 CSV 导出 |
| 网站日志 | URI、IP、User-Agent 搜索、分页和 CSV 导出 |
| 统计报告 | 今日、昨日、近 7 天、近 30 天摘要和访问排行 |
| 全局设置 | 采集开关、数据保留期、队列容量、排除规则和数据清理 |

## 架构

```text
Nginx / Apache access log
          │
          ▼
 /tmp/webanalytics.sock
          │
          ▼
 systemd 实时采集服务
          │
          ├── 日志解析 / 真实客户端 IP
          ├── 蜘蛛 / 浏览器 / 系统 / 设备分类
          ├── 分钟指标 / 日维度聚合 / HLL 去重
          └── 日志轮转与历史归档恢复
          │
          ▼
      SQLite WAL
          │
          ▼
 宝塔插件接口与本地报表
```

## 快速开始

### 环境

- 宝塔 Linux 面板 v11.x
- Nginx 或 Apache
- systemd
- 宝塔面板自带 Python 3
- Ubuntu 22.04/24.04、Debian 12 或 Rocky Linux 9

### 安装

推荐直接通过宝塔面板导入：

1. 从 [Releases](https://github.com/91270/Meiam.WebAnalytics/releases) 下载最新的 `WebAnalytics-*.zip`，不要修改压缩包内部目录结构。
2. 打开宝塔面板的 **软件商店**。
3. 进入 **第三方应用**，点击顶部的 **导入插件**。
4. 选择下载的 ZIP 安装包并确认导入。
5. 安装完成后，在第三方应用列表中找到“网站访问分析”，点击 **设置** 打开插件。

宝塔导入后，插件代码安装在：

```text
/www/server/panel/plugin/WebAnalytics
```

通常不需要手动解压或运行安装脚本。仅在面板导入失败、需要调试安装流程时，才使用命令行方式：

```bash
unzip WebAnalytics-*.zip -d /www/server/panel/plugin/
cd /www/server/panel/plugin/WebAnalytics
bash install.sh install
```

安装后在宝塔面板中打开“网站访问分析”。首次启动会在后台恢复已有访问日志，不会阻塞实时请求采集。

### 升级

优先下载新版 ZIP，然后在宝塔 **软件商店 → 第三方应用 → 导入插件** 中重新导入。升级过程会运行数据库迁移，统计数据位于独立持久目录，不会因覆盖插件代码而丢失。

需要手动升级时：

```bash
cd /www/server/panel/plugin/WebAnalytics
bash install.sh update
```

统计数据位于 `/www/server/webanalytics/data`，覆盖插件代码不会清空数据。

### 卸载

```bash
cd /www/server/panel/plugin/WebAnalytics
bash install.sh uninstall
```

卸载会清理插件配置、服务、Socket 和统计数据库，但不会删除网站原始日志。

## 数据与隐私

| 数据类型 | 默认保留 | 说明 |
| --- | ---: | --- |
| 核心分钟指标 | 长期 | PV、UV、IP、流量和趋势 |
| 数据库访问明细 | 7 天 | 逐条网站日志查询与导出 |
| 错误访问明细 | 30 天 | 40x / 50x 分析 |
| IP、URI、客户端聚合 | 90 天 | 排行与分布统计 |

保留策略只作用于插件自己的 SQLite 数据库，不会清理 `/www/wwwlogs` 中的文件。

主要运行路径：

```text
/www/server/panel/plugin/WebAnalytics    插件代码
/www/server/webanalytics/data           持久数据
/www/server/webanalytics/data/stats.db  SQLite 数据库
/www/server/webanalytics/data/config.json  插件配置
/etc/systemd/system/webanalytics.service   systemd 服务
/tmp/webanalytics.sock                  实时采集 Socket
```

## IP 归属地

插件支持 GeoLite2 / GeoIP2 City MMDB 离线解析。推荐将数据库放到：

```text
/www/server/webanalytics/data/GeoLite2-City.mmdb
```

运行环境需安装 `geoip2` 或 `maxminddb`。没有本地数据库时公网 IP 显示“未知”，插件不会退回公网查询接口；本机、内网、保留和无效地址仍可识别。

## 统计口径

- **PV**：GET/HEAD 的 2xx/3xx 请求，排除静态资源和已识别爬虫。
- **UV**：按 `IP + User-Agent` 匿名指纹进行 HyperLogLog 近似去重。
- **独立 IP**：按来源 IP 进行 HyperLogLog 近似去重。
- **流量**：访问日志中的响应字节数，不含 TLS、请求体和网络层开销。
- **实时流量**：最近一个已采集分钟的响应字节数。
- **QPS**：最近一个已采集分钟的请求数除以 60。
- **错误**：HTTP 400–599 状态码。

## 运维排查

```bash
systemctl status webanalytics.service
journalctl -u webanalytics.service -n 100 --no-pager
ls -l /tmp/webanalytics.sock
```

界面会区分服务、Socket、站点配置、历史恢复和实际收包状态。WebAnalytics 只在来源属于可信代理网段时采用 `X-Forwarded-For`、`X-Real-IP`、`CF-Connecting-IP` 等真实 IP 请求头。

## 开发

```bash
git clone https://github.com/91270/Meiam.WebAnalytics.git
cd Meiam.WebAnalytics
python -m unittest discover -s tests -v
```

项目仅使用插件随附的本地静态资源。自动化测试覆盖日志解析、轮转、压缩归档、HLL、站点发现、反向代理、实时队列、数据库迁移、蜘蛛维度和 IP 归属地。

## 参与贡献

欢迎通过 [Issues](https://github.com/91270/Meiam.WebAnalytics/issues) 报告问题或提出建议，也欢迎提交 Pull Request。

提交代码前请：

1. 保持 Python 3 和宝塔面板运行环境兼容。
2. 不增加运行时公网依赖或遥测。
3. 为数据口径、数据库迁移和日志解析变化补充测试。
4. 执行完整测试并确认安装、升级和卸载路径不受影响。

## 路线图

- 更多宝塔 v11.x 发行版真机回归
- 宝塔 v10.x 兼容性评估
- 更丰富的报告快照与导出格式
- 可选的本地 IP 数据库管理工具
- 高流量站点的长期聚合和容量保护优化

完整版本记录参见 [CHANGELOG.md](CHANGELOG.md)。

## 作者

- Developer: Meiam
- Website: [592.la](https://592.la)
