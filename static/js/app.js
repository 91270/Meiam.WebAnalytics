(function () {
    'use strict';

    var state = {
        siteId: 0,
        period: 'today',
        metric: 'pv',
        data: null,
        loading: false
    };

    var metrics = [
        { key: 'pv', label: '浏览量 (PV)', tip: '排除静态资源后的 GET/HEAD 请求' },
        { key: 'uv', label: '访客量 (UV)', tip: '每日 IP + User-Agent 哈希估算' },
        { key: 'ip', label: 'IP 数', tip: '所选时间范围内的独立 IP' },
        { key: 'body_bytes', label: '流量', tip: '响应日志中的字节数之和', bytes: true },
        { key: 'requests', label: '请求', tip: '纳入统计的访问日志总数' },
        { key: 'realtime_bytes', label: '实时流量', tip: '最近完整采集分钟的响应流量', bytes: true },
        { key: 'qps', label: '每秒请求', tip: '最近完整采集分钟请求数 / 60', decimal: true }
    ];

    function requestPlugin(method, args, callback) {
        $.ajax({
            type: 'POST',
            url: '/plugin?action=a&s=' + encodeURIComponent(method) + '&name=WebAnalytics',
            data: args || {},
            timeout: 60000,
            success: callback,
            error: function (xhr) {
                var detail = '';
                if (xhr.responseJSON) {
                    detail = xhr.responseJSON.message || xhr.responseJSON.msg || '';
                }
                if (!detail && xhr.responseText && xhr.responseText.length < 500) {
                    detail = String(xhr.responseText).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
                }
                callback({
                    success: false,
                    message: '请求失败（HTTP ' + xhr.status + '）' + (detail ? '：' + detail : '')
                });
            }
        });
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    }

    function formatNumber(value) {
        var number = Number(value || 0);
        return isFinite(number) ? number.toLocaleString('zh-CN') : '0';
    }

    function formatBytes(value) {
        var bytes = Math.max(0, Number(value || 0));
        var units = ['B', 'KB', 'MB', 'GB', 'TB'];
        var index = 0;
        while (bytes >= 1024 && index < units.length - 1) {
            bytes /= 1024;
            index += 1;
        }
        return (index === 0 ? Math.round(bytes) : bytes.toFixed(bytes >= 100 ? 0 : 2)) + ' ' + units[index];
    }

    function metricValue(source, metric) {
        if (!source) return 0;
        return Number(source[metric.key] || 0);
    }

    function formatMetric(value, metric) {
        if (metric.bytes) return formatBytes(value);
        if (metric.decimal) return Number(value || 0).toFixed(2);
        return formatNumber(value);
    }

    function showNotice(message, allowRepair) {
        var notice = $('#wa-notice');
        if (!message) return notice.attr('hidden', true).text('');
        var body = '<span>' + escapeHtml(message) + '</span>';
        if (allowRepair) body += '<button type="button" id="wa-repair">修复采集配置</button>';
        notice.removeAttr('hidden').html(body);
    }

    function setLoading(loading) {
        state.loading = loading;
        $('#wa-loading').prop('hidden', !loading);
        $('#wa-dashboard').prop('hidden', loading);
        $('#wa-refresh').prop('disabled', loading);
    }

    function renderSites(sites, selected) {
        var html = '';
        sites.forEach(function (site) {
            html += '<option value="' + Number(site.id) + '"' + (Number(site.id) === Number(selected) ? ' selected' : '') + '>' + escapeHtml(site.name) + '</option>';
        });
        $('#wa-site').html(html).prop('disabled', !sites.length);
    }

    function renderMetrics(data) {
        var current = data.overview || {};
        var previous = data.previous || {};
        var html = '';
        metrics.forEach(function (metric) {
            var currentValue = metricValue(current, metric);
            var previousValue = metricValue(previous, metric);
            var comparison = previousValue > 0 ? ((currentValue - previousValue) / previousValue * 100) : null;
            var comparisonText = comparison == null ? '暂无对比' : (comparison >= 0 ? '↑ ' : '↓ ') + Math.abs(comparison).toFixed(1) + '%';
            var comparisonClass = comparison == null ? '' : (comparison >= 0 ? ' class="is-up"' : ' class="is-down"');
            html += '<button class="wa-metric' + (state.metric === metric.key ? ' is-active' : '') + '" data-metric="' + metric.key + '" title="' + escapeHtml(metric.tip) + '">'
                + '<div class="wa-metric-label">' + escapeHtml(metric.label) + '<em>?</em></div>'
                + '<div class="wa-metric-value">' + formatMetric(currentValue, metric) + '</div>'
                + '<div class="wa-metric-compare">上一时段 <b' + comparisonClass + '>' + comparisonText + '</b></div></button>';
        });
        $('#wa-metrics').html(html);
    }

    function seriesValue(row, metric, bucket) {
        if (!row) return 0;
        if (metric.key === 'realtime_bytes') return Number(row.body_bytes || 0);
        if (metric.key === 'qps') return Number(row.requests || 0) / Math.max(1, bucket);
        return Number(row[metric.key] || 0);
    }

    function chartPoints(values, width, height, padding, maxValue) {
        var usableWidth = width - padding.left - padding.right;
        var usableHeight = height - padding.top - padding.bottom;
        return values.map(function (value, index) {
            var x = padding.left + (values.length === 1 ? usableWidth / 2 : index * usableWidth / (values.length - 1));
            var y = padding.top + usableHeight - (value / maxValue * usableHeight);
            return { x: x, y: y, value: value };
        });
    }

    function smoothPathFor(points) {
        if (!points.length) return '';
        if (points.length === 1) return 'M' + points[0].x.toFixed(1) + ',' + points[0].y.toFixed(1);
        var path = 'M' + points[0].x.toFixed(1) + ',' + points[0].y.toFixed(1);
        for (var index = 1; index < points.length; index++) {
            var previous = points[index - 1];
            var current = points[index];
            var middle = (previous.x + current.x) / 2;
            path += ' C' + middle.toFixed(1) + ',' + previous.y.toFixed(1)
                + ' ' + middle.toFixed(1) + ',' + current.y.toFixed(1)
                + ' ' + current.x.toFixed(1) + ',' + current.y.toFixed(1);
        }
        return path;
    }

    function niceMaximum(value, metric) {
        if (!(value > 0)) {
            if (metric.bytes) return 1024;
            if (metric.decimal) return 1;
            return 100;
        }
        var exponent = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
        var scaled = value / exponent;
        var nice = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
        return nice * exponent;
    }

    function renderChart() {
        if (!state.data) return;
        var metric = metrics.filter(function (item) { return item.key === state.metric; })[0] || metrics[0];
        var trend = state.data.trend || [];
        var previousTrend = state.data.previous_trend || [];
        var bucket = Number((state.data.range || {}).bucket || 3600);
        $('#wa-chart-title').text(metric.label.replace(/\s*\(.+\)/, '') + '趋势');

        if (!trend.length && !previousTrend.length) {
            $('#wa-chart').html('<div class="wa-empty-chart">暂无统计数据，请访问网站后稍候刷新</div>');
            return;
        }

        var count = Math.max(trend.length, previousTrend.length, 1);
        var currentValues = [];
        var previousValues = [];
        var labels = [];
        for (var i = 0; i < count; i++) {
            currentValues.push(seriesValue(trend[i], metric, bucket));
            previousValues.push(seriesValue(previousTrend[i], metric, bucket));
            var source = trend[i] || previousTrend[i];
            var date = source ? new Date(Number(source.timestamp) * 1000) : null;
            labels.push(date ? (bucket >= 86400 ? (date.getMonth() + 1) + '/' + date.getDate() : String(date.getHours()).padStart(2, '0') + ':00') : '');
        }
        if (count === 1) {
            currentValues.push(currentValues[0]);
            previousValues.push(previousValues[0]);
            labels.push(labels[0]);
            count = 2;
        }
        var observedMax = Math.max.apply(Math, currentValues.concat(previousValues).concat([0]));
        var maxValue = niceMaximum(observedMax, metric);
        var width = 900, height = 300, padding = { left: 54, right: 18, top: 18, bottom: 34 };
        var currentPoints = chartPoints(currentValues, width, height, padding, maxValue);
        var previousPoints = chartPoints(previousValues, width, height, padding, maxValue);
        var currentPath = smoothPathFor(currentPoints);
        var previousPath = smoothPathFor(previousPoints);
        var baseline = height - padding.bottom;
        var grid = '', yLabels = '';
        for (var gridIndex = 0; gridIndex <= 4; gridIndex++) {
            var y = padding.top + (height - padding.top - padding.bottom) * gridIndex / 4;
            var labelValue = maxValue * (4 - gridIndex) / 4;
            grid += '<line class="wa-grid-line" x1="' + padding.left + '" y1="' + y + '" x2="' + (width - padding.right) + '" y2="' + y + '"/>';
            var axisLabel = metric.bytes ? formatBytes(labelValue) : (metric.decimal ? labelValue.toFixed(2) : formatNumber(Math.round(labelValue)));
            yLabels += '<text x="43" y="' + (y + 4) + '" text-anchor="end" fill="#8f9aaa" font-size="11">' + escapeHtml(axisLabel) + '</text>';
        }
        var xLabels = '';
        var labelStep = Math.max(1, Math.ceil(count / 9));
        labels.forEach(function (label, index) {
            if (index % labelStep !== 0 && index !== count - 1) return;
            var x = padding.left + (count === 1 ? 0 : index * (width - padding.left - padding.right) / (count - 1));
            xLabels += '<text x="' + x + '" y="289" text-anchor="middle" fill="#8f9aaa" font-size="11">' + escapeHtml(label) + '</text>';
        });
        var areaPath = currentPath + ' L' + currentPoints[currentPoints.length - 1].x.toFixed(1) + ',' + baseline
            + ' L' + currentPoints[0].x.toFixed(1) + ',' + baseline + ' Z';
        var points = '';
        if (currentPoints.length <= 48) {
            currentPoints.forEach(function (point, index) {
                if (!(point.value > 0)) return;
                var title = (labels[index] ? labels[index] + ' · ' : '') + formatMetric(point.value, metric);
                points += '<circle class="wa-point" cx="' + point.x.toFixed(1) + '" cy="' + point.y.toFixed(1) + '" r="3.2"><title>' + escapeHtml(title) + '</title></circle>';
            });
        }
        var svg = '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
            + '<defs><linearGradient id="wa-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#438cf8" stop-opacity=".23"/><stop offset=".72" stop-color="#70a9fa" stop-opacity=".07"/><stop offset="1" stop-color="#8ab8f8" stop-opacity="0"/></linearGradient><clipPath id="wa-plot-clip"><rect x="' + padding.left + '" y="' + padding.top + '" width="' + (width - padding.left - padding.right) + '" height="' + (height - padding.top - padding.bottom) + '" rx="2"/></clipPath></defs>'
            + grid + yLabels + xLabels
            + '<line class="wa-axis-base" x1="' + padding.left + '" y1="' + baseline + '" x2="' + (width - padding.right) + '" y2="' + baseline + '"/>'
            + '<g clip-path="url(#wa-plot-clip)"><path class="wa-line-previous" d="' + previousPath + '"/><path d="' + areaPath + '" fill="url(#wa-area)" stroke="none"/><path class="wa-line-current" d="' + currentPath + '"/>' + points + '</g>'
            + '</svg>';
        $('#wa-chart').html(svg);
    }

    function renderChartTabs() {
        var html = '';
        metrics.forEach(function (metric) {
            html += '<button data-metric="' + metric.key + '" class="' + (metric.key === state.metric ? 'is-active' : '') + '">' + escapeHtml(metric.label.replace(/\s*\(.+\)/, '')) + '</button>';
        });
        $('#wa-chart-tabs').html(html);
    }

    function renderHealth(health, diagnostics) {
        var badge = $('#wa-health');
        var run = health && health.realtime_service;
        badge.removeClass('is-good is-warn');
        if (!diagnostics || !diagnostics.service_ready || !run || !run.running) return badge.addClass('is-warn').html('<i></i>实时服务未运行');
        if (!diagnostics.socket_ready) return badge.addClass('is-warn').html('<i></i>Socket 未就绪');
        if (run.phase === 'backfill') return badge.addClass('is-warn').html('<i></i>首次数据补录中');
        if (!diagnostics.webserver_configured && !diagnostics.nginx_configured) return badge.addClass('is-warn').html('<i></i>站点尚未接入');
        var queue = diagnostics.queue || {};
        if (Number(queue.write_errors || 0) > 0 || Number(queue.dropped || 0) > 0) return badge.addClass('is-warn').html('<i></i>采集队列异常');
        var time = new Date(Number(run.updated_at || 0) * 1000);
        if ((Date.now() - time.getTime()) > 30000) return badge.addClass('is-warn').html('<i></i>实时服务无响应');
        if (!diagnostics.received_for_site) return badge.addClass('is-warn').html('<i></i>等待网站请求');
        badge.addClass('is-good').html('<i></i>实时接收中');
    }

    function renderDiagnostics(data) {
        var diagnostics = data.diagnostics || {};
        var requests = Number((data.overview || {}).requests || 0);
        var queue = diagnostics.queue || {};
        var sync = diagnostics.config_sync || {};
        if (!diagnostics.service_ready || !diagnostics.socket_ready) {
            var serviceError = ((data.health || {}).realtime_service || {}).error || '';
            showNotice('实时采集服务未正常运行' + (serviceError ? '：' + serviceError : '，当前不会产生新统计数据。'), true);
        } else if (((data.health || {}).realtime_service || {}).phase === 'backfill') {
            showNotice('正在从当前网站已有访问日志补录首次统计，完成后页面会显示数据。');
        } else if (!diagnostics.webserver_configured && !diagnostics.nginx_configured) {
            showNotice('当前网站尚未写入 ' + (diagnostics.web_server === 'apache' ? 'Apache' : 'Nginx') + ' 实时日志扩展配置。', true);
        } else if (Number(queue.write_errors || 0) > 0) {
            showNotice('实时队列写入统计数据库失败 ' + Number(queue.write_errors) + ' 次，请修复采集服务。', true);
        } else if (Number(queue.dropped || 0) > 0) {
            showNotice('访问高峰期间采集队列已丢弃 ' + Number(queue.dropped) + ' 条日志，请增大 queue_size 后重启服务。');
        } else if (sync.success === false) {
            showNotice('站点配置自动同步失败：' + (sync.message || '未知原因'), true);
        } else if (!diagnostics.received_for_site && requests === 0) {
            showNotice('实时服务和 Nginx 配置正常，但尚未收到当前网站请求。请访问该网站后刷新；若仍为 0，可修复采集配置。', true);
        }
    }

    function render(data) {
        state.data = data;
        state.siteId = Number(data.selected_site_id || 0);
        renderSites(data.sites || [], state.siteId);
        renderMetrics(data);
        renderChartTabs();
        renderChart();
        renderHealth(data.health || {}, data.diagnostics || {});
        renderDiagnostics(data);
        $('#wa-generated-at').text('数据生成于 ' + new Date(Number(data.generated_at || Date.now() / 1000) * 1000).toLocaleString('zh-CN'));
        $('#wa-dashboard').prop('hidden', false);
    }

    function loadDashboard() {
        setLoading(true);
        showNotice('');
        requestPlugin('get_bootstrap', { site_id: state.siteId, period: state.period }, function (response) {
            setLoading(false);
            if (!response || !response.success) {
                showNotice(response && response.message ? response.message : '无法读取插件数据');
                return;
            }
            var data = response.data || {};
            if (!data.sites || !data.sites.length) {
                renderSites([], 0);
                showNotice(response.message || '未发现网站日志。请确认宝塔网站已开启访问日志。');
                $('#wa-dashboard').prop('hidden', true);
                return;
            }
            render(data);
        });
    }

    $('#wa-site').on('change', function () {
        state.siteId = Number(this.value || 0);
        loadDashboard();
    });

    $('.wa-period').on('click', 'button', function () {
        state.period = $(this).data('period');
        $(this).addClass('is-active').siblings().removeClass('is-active');
        loadDashboard();
    });

    $('#wa-metrics, #wa-chart-tabs').on('click', '[data-metric]', function () {
        state.metric = $(this).data('metric');
        renderMetrics(state.data);
        renderChartTabs();
        renderChart();
    });

    $('#wa-refresh').on('click', function () {
        loadDashboard();
    });

    $('#wa-notice').on('click', '#wa-repair', function () {
        var button = $(this);
        button.prop('disabled', true).text('修复中...');
        requestPlugin('repair_realtime', {}, function (response) {
            if (!response || !response.success) {
                button.prop('disabled', false).text('重试修复');
                showNotice(response && response.message ? response.message : '修复失败', true);
                return;
            }
            showNotice('采集配置已修复，请访问网站后刷新。');
            window.setTimeout(loadDashboard, 1500);
        });
    });

    function fitPluginLayer() {
        var layerBox = $('.layui-layer-page');
        if (!layerBox.length) return;
        var viewportWidth = $(window).width();
        var viewportHeight = $(window).height();
        var modalWidth = Math.min(1120, Math.max(760, viewportWidth - 40));
        var modalHeight = Math.min(760, Math.max(560, viewportHeight - 40));
        var titleHeight = layerBox.find('.layui-layer-title').outerHeight() || 42;
        layerBox.css({
            width: modalWidth + 'px',
            height: modalHeight + 'px',
            left: Math.max(20, Math.round((viewportWidth - modalWidth) / 2)) + 'px',
            top: Math.max(20, Math.round((viewportHeight - modalHeight) / 2)) + 'px',
            margin: 0,
            maxWidth: 'none'
        });
        layerBox.find('.layui-layer-content').css({
            height: Math.max(500, modalHeight - titleHeight) + 'px',
            overflow: 'hidden'
        });
        $('#wa-app').css({ width: '100%', height: Math.max(500, modalHeight - titleHeight) + 'px', minHeight: 0 });
    }

    fitPluginLayer();
    $(window).off('resize.webanalytics').on('resize.webanalytics', fitPluginLayer);
    loadDashboard();
})();
