# WebAnalytics

宝塔 Linux 面板 v11.x 网站访问分析插件。

- Developer: Meiam
- Install source: [592.la](https://592.la)

## 当前功能

- 与官方 `site_total` 一致，优先通过宝塔 `public.M('sites')` 发现网站，SQLite 仅作降级；不会把面板、代理或其他 `.log` 文件混入列表。
- 历史恢复优先读取站点 Nginx / Apache 虚拟主机中配置的真实访问日志路径，并限制在网站日志目录内。
- 以宝塔站点 ID 作为稳定身份；日志路径变化时自动合并旧记录，历史统计不会因路径修正而隐藏。
- 通过独立 Unix Socket 实时接收 Nginx/Apache syslog 日志。
- 使用宝塔站点扩展配置目录接入，不覆盖原有访问日志。
- 识别可信反向代理和常见 CDN 真实客户端 IP。
- 文件增量解析保留为历史回溯和故障恢复能力。
- 使用 SQLite WAL 保存分钟指标，使用 HyperLogLog 低内存估算 UV/IP。
- 全新安装先初始化空数据库，再完整导入站点当前日志、未压缩轮转日志及 `.gz` 轮转日志。
- 完整历史导入按文件游标续跑，服务或服务器重启后不会从头重复累计。
- 有界队列隔离收包与刷盘，并监控积压、丢弃和写入异常。
- 面板内查看 PV、UV、IP、流量、请求、实时流量、QPS 和趋势。
- 网站列表集中对比全部站点的采集状态、Web 服务、核心指标、错误率和最后数据时间，支持搜索、排序及跳转概览。
- 概览页区分服务、Socket、站点 Nginx 接入和实际收包状态，并提供一键修复采集配置。
- systemd 守护实时采集进程；不创建 cron，不开放公网端口，不加载外部资源。

## 安装行为

- 插件目录：`/www/server/panel/plugin/WebAnalytics`。
- 持久数据目录：`/www/server/webanalytics/data`，与插件代码分离，覆盖升级不会清空统计。
- 安装后通过 `/tmp/webanalytics.sock` 实时接收新访问。
- 安装脚本会备份站点配置、写入扩展配置，执行 `nginx -t`/`apachectl -t` 成功后才重载；失败自动回滚。
- 常驻服务自动同步宝塔新增、删除和切换 Web 服务的网站，无需计划任务。
- 服务启动时立即同步全部站点配置；后续新增站点最长约 15 秒自动接入，无需逐站点击修复。
- 卸载时删除 `/www/server/webanalytics` 统计数据库；原始网站日志不受影响，重新安装时依据仍存在的日志重新生成历史统计。

## 开发验证

在插件根目录的上一级执行：

```bash
python -m unittest discover -s WebAnalytics/tests -v
```

本地测试只验证采集与统计核心；正式发布前仍需在宝塔 v11.x 正式版真机完成安装、接口、systemd、Unix Socket、Nginx 配置回滚、升级和卸载验收。
