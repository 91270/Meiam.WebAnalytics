(function () {
    'use strict';

    var state = {
        siteId: 0,
        period: 'today',
        metric: 'pv',
        page: 'overview',
        data: null,
        sitesData: null,
        spidersData: null,
        moduleData: {},
        pageNumber: { errors: 1, requests: 1 },
        siteQuery: '',
        siteSort: { key: 'requests', direction: 'desc' },
        loading: false,
        dashboardRequest: 0,
        siteOptionsSignature: ''
    };

    var metrics = [
        { key: 'pv', label: '浏览量 (PV)', tip: '页面浏览请求次数。仅统计 GET/HEAD、2xx/3xx 响应，并排除静态资源与爬虫。' },
        { key: 'uv', label: '访客量 (UV)', tip: '所选时段内的独立访客估算值，以 IP + User-Agent 生成匿名指纹并使用 HLL 去重。' },
        { key: 'ip', label: 'IP 数', tip: '所选时段内访问网站的独立来源 IP 估算值，使用 HLL 低内存去重。' },
        { key: 'body_bytes', label: '流量', tip: '所选时段内全部已采集请求的响应字节数之和，以访问日志记录值为准。', bytes: true },
        { key: 'requests', label: '请求', tip: '所选时段内成功解析并纳入统计的访问日志请求总数。' },
        { key: 'realtime_bytes', label: '实时流量', tip: '当前时段中最近一个已采集分钟的响应字节数，用于观察短时流量。', bytes: true },
        { key: 'qps', label: '每秒请求', tip: '当前时段中最近一个已采集分钟的请求数除以 60，表示该分钟的平均每秒请求数。', decimal: true }
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

    function setLoading(loading, message) {
        state.loading = loading;
        if (message) $('#wa-loading-label').text(message);
        $('#wa-loading').prop('hidden', !loading);
        $('#wa-dashboard').prop('hidden', loading);
        $('#wa-refresh').prop('disabled', loading);
    }

    function renderSiteOptions(sites, selected) {
        var signature = sites.map(function (site) {
            return Number(site.id) + ':' + String(site.name || '') + ':' + Number(site.panel_site_id || 0);
        }).join('|');
        var menu = $('#wa-site-menu');
        if (signature !== state.siteOptionsSignature) {
            var html = '';
            sites.forEach(function (site) {
                var siteId = Number(site.id);
                html += '<button type="button" class="wa-site-option" role="option" data-site-id="' + siteId + '" aria-selected="false">'
                    + '<span class="wa-site-option-icon">W</span><span class="wa-site-option-text"><strong>' + escapeHtml(site.name) + '</strong>'
                    + '<small>站点 ID ' + Number(site.panel_site_id || siteId) + '</small></span><span class="wa-site-option-check"></span></button>';
            });
            menu.html(html);
            state.siteOptionsSignature = signature;
        }
        var selectedId = Number(selected || 0);
        var selectedSite = sites.filter(function (site) { return Number(site.id) === selectedId; })[0];
        $('#wa-site-current').text(selectedSite ? selectedSite.name : '请选择网站');
        $('#wa-site-trigger').prop('disabled', !sites.length);
        menu.find('.wa-site-option').removeClass('is-selected').attr('aria-selected', 'false').find('.wa-site-option-check').text('');
        menu.find('[data-site-id="' + selectedId + '"]').addClass('is-selected').attr('aria-selected', 'true').find('.wa-site-option-check').text('✓');
    }

    function closeSitePicker() {
        $('#wa-site-picker').removeClass('is-open');
        $('#wa-site-trigger').attr('aria-expanded', 'false');
        $('#wa-site-menu').prop('hidden', true);
    }

    function showPage(page) {
        var allowed = { overview:1, sites:1, spiders:1, clients:1, ip:1, uri:1, errors:1, requests:1, reports:1, settings:1 };
        state.page = allowed[page] ? page : 'overview';
        $('.wa-nav [data-page]').removeClass('is-active').filter('[data-page="' + state.page + '"]').addClass('is-active');
        $('.wa-page').prop('hidden', true).filter('[data-page="' + state.page + '"]').prop('hidden', false);
        $('.wa-site-filter').prop('hidden', state.page === 'sites' || state.page === 'settings');
    }

    function renderMetrics(data) {
        var current = data.overview || {};
        var previous = data.previous || {};
        var html = '';
        metrics.forEach(function (metric, index) {
            var currentValue = metricValue(current, metric);
            var previousValue = metricValue(previous, metric);
            var comparison = previousValue > 0 ? ((currentValue - previousValue) / previousValue * 100) : null;
            var comparisonText = comparison == null ? '暂无对比' : (comparison >= 0 ? '↑ ' : '↓ ') + Math.abs(comparison).toFixed(1) + '%';
            var comparisonClass = comparison == null ? '' : (comparison >= 0 ? ' class="is-up"' : ' class="is-down"');
            var helpPosition = index === 0 ? ' is-left' : (index === metrics.length - 1 ? ' is-right' : '');
            html += '<button class="wa-metric' + (state.metric === metric.key ? ' is-active' : '') + '" data-metric="' + metric.key + '" aria-label="' + escapeHtml(metric.label + '：' + metric.tip) + '">'
                + '<div class="wa-metric-label"><span class="wa-metric-name">' + escapeHtml(metric.label) + '</span>'
                + '<span class="wa-metric-help' + helpPosition + '" data-tip="' + escapeHtml(metric.tip) + '" aria-hidden="true">?</span></div>'
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

    function chartTimeLabel(timestamp, bucket, detailed) {
        var date = new Date(Number(timestamp || 0) * 1000);
        if (!isFinite(date.getTime())) return '';
        var monthDay = (date.getMonth() + 1) + '/' + date.getDate();
        var time = String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
        if (detailed) return bucket >= 86400 ? monthDay : monthDay + ' ' + time;
        return bucket >= 86400 ? monthDay : time;
    }

    function renderChartSummary(values, previousValues, labels, metric) {
        var sum = values.reduce(function (total, value) { return total + value; }, 0);
        var previousSum = previousValues.reduce(function (total, value) { return total + value; }, 0);
        var peak = Math.max.apply(Math, values.concat([0]));
        var peakIndex = values.indexOf(peak);
        var average = values.length ? sum / values.length : 0;
        var comparison = previousSum > 0 ? (sum - previousSum) / previousSum * 100 : null;
        var comparisonText = comparison == null ? '暂无同期基准' : (comparison >= 0 ? '↑ ' : '↓ ') + Math.abs(comparison).toFixed(1) + '%';
        var comparisonClass = comparison == null ? '' : (comparison >= 0 ? ' is-up' : ' is-down');
        $('#wa-chart-summary').html(
            '<div class="wa-chart-stat"><span>当前总量</span><strong>' + formatMetric(sum, metric) + '</strong></div>'
            + '<div class="wa-chart-stat"><span>每桶平均</span><strong>' + formatMetric(average, metric) + '</strong></div>'
            + '<div class="wa-chart-stat"><span>峰值 · ' + escapeHtml(labels[peakIndex] || '—') + '</span><strong>' + formatMetric(peak, metric) + '</strong></div>'
            + '<div class="wa-chart-stat"><span>较上一时段</span><strong class="' + comparisonClass.trim() + '">' + comparisonText + '</strong></div>'
        );
        return peakIndex;
    }

    function bindChartInteraction(points, previousPoints, timestamps, bucket, metric, padding, width, height) {
        var chart = $('#wa-chart');
        var svg = chart.find('svg');
        if (!svg.length || !points.length) return;
        var overlay = svg.find('.wa-chart-interaction');
        var tooltip = $('<div class="wa-chart-tooltip" hidden></div>').appendTo(chart);
        overlay.on('mousemove', function (event) {
            var rect = this.getBoundingClientRect();
            var localX = (event.clientX - rect.left) / Math.max(1, rect.width) * (width - padding.left - padding.right) + padding.left;
            var spacing = points.length > 1 ? (width - padding.left - padding.right) / (points.length - 1) : 1;
            var index = Math.max(0, Math.min(points.length - 1, Math.round((localX - padding.left) / spacing)));
            var current = points[index], previous = previousPoints[index] || { value: 0, y: height - padding.bottom };
            var delta = previous.value > 0 ? (current.value - previous.value) / previous.value * 100 : null;
            var deltaText = delta == null ? '环比：暂无基准' : '环比：' + (delta >= 0 ? '↑ ' : '↓ ') + Math.abs(delta).toFixed(1) + '%';
            svg.find('.wa-chart-hover').attr('visibility', 'visible');
            svg.find('.wa-chart-crosshair').attr({ x1: current.x, x2: current.x });
            svg.find('.wa-hover-current').attr({ cx: current.x, cy: current.y });
            svg.find('.wa-hover-previous').attr({ cx: current.x, cy: previous.y });
            var chartRect = chart[0].getBoundingClientRect();
            var renderedX = current.x / width * chartRect.width;
            var renderedY = current.y / height * chartRect.height;
            tooltip.toggleClass('is-right', renderedX > chartRect.width * .7).css({ left: renderedX, top: renderedY }).removeAttr('hidden').html(
                '<b>' + escapeHtml(chartTimeLabel(timestamps[index], bucket, true)) + '</b>'
                + '<div class="wa-chart-tooltip-row"><span><i></i>当前时段</span><strong>' + formatMetric(current.value, metric) + '</strong></div>'
                + '<div class="wa-chart-tooltip-row is-previous"><span><i></i>上一时段</span><strong>' + formatMetric(previous.value, metric) + '</strong></div>'
                + '<div class="wa-chart-tooltip-delta">' + deltaText + '</div>'
            );
        }).on('mouseleave', function () {
            svg.find('.wa-chart-hover').attr('visibility', 'hidden');
            tooltip.attr('hidden', true);
        });
    }

    function renderChart() {
        if (!state.data) return;
        var metric = metrics.filter(function (item) { return item.key === state.metric; })[0] || metrics[0];
        var trend = state.data.trend || [];
        var previousTrend = state.data.previous_trend || [];
        var bucket = Number((state.data.range || {}).bucket || 3600);
        $('#wa-chart-title').text(metric.label.replace(/\s*\(.+\)/, '') + '趋势');

        if (!trend.length && !previousTrend.length) {
            $('#wa-chart-summary').empty();
            $('#wa-chart').html('<div class="wa-empty-chart">所选时间范围暂无可展示的访问数据</div>');
            return;
        }

        var count = Math.max(trend.length, previousTrend.length, 1);
        var currentValues = [];
        var previousValues = [];
        var labels = [];
        var timestamps = [];
        for (var i = 0; i < count; i++) {
            currentValues.push(seriesValue(trend[i], metric, bucket));
            previousValues.push(seriesValue(previousTrend[i], metric, bucket));
            var source = trend[i] || previousTrend[i];
            var timestamp = source ? Number(source.timestamp) : 0;
            timestamps.push(timestamp);
            labels.push(chartTimeLabel(timestamp, bucket, false));
        }
        if (count === 1) {
            currentValues.push(currentValues[0]);
            previousValues.push(previousValues[0]);
            labels.push(labels[0]);
            timestamps.push(timestamps[0]);
            count = 2;
        }
        var peakIndex = renderChartSummary(currentValues, previousValues, labels, metric);
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
        var peakPoint = currentPoints[peakIndex];
        var peakMarkup = peakPoint && peakPoint.value > 0
            ? '<circle class="wa-peak-point" cx="' + peakPoint.x.toFixed(1) + '" cy="' + peakPoint.y.toFixed(1) + '" r="4"/><text class="wa-peak-label" x="' + peakPoint.x.toFixed(1) + '" y="' + Math.max(12, peakPoint.y - 9).toFixed(1) + '" text-anchor="middle">峰值</text>'
            : '';
        var svg = '<svg viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="xMidYMid meet" aria-hidden="true">'
            + '<defs><linearGradient id="wa-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#438cf8" stop-opacity=".23"/><stop offset=".72" stop-color="#70a9fa" stop-opacity=".07"/><stop offset="1" stop-color="#8ab8f8" stop-opacity="0"/></linearGradient><clipPath id="wa-plot-clip"><rect x="' + padding.left + '" y="' + padding.top + '" width="' + (width - padding.left - padding.right) + '" height="' + (height - padding.top - padding.bottom) + '" rx="2"/></clipPath></defs>'
            + grid + yLabels + xLabels
            + '<line class="wa-axis-base" x1="' + padding.left + '" y1="' + baseline + '" x2="' + (width - padding.right) + '" y2="' + baseline + '"/>'
            + '<g clip-path="url(#wa-plot-clip)"><path class="wa-line-previous" d="' + previousPath + '"/><path d="' + areaPath + '" fill="url(#wa-area)" stroke="none"/><path class="wa-line-current" d="' + currentPath + '"/>' + points + '</g>' + peakMarkup
            + '<g class="wa-chart-hover" visibility="hidden"><line class="wa-chart-crosshair" y1="' + padding.top + '" y2="' + baseline + '"/><circle class="wa-chart-hover-point wa-hover-current" r="4" stroke="#3f82f7"/><circle class="wa-chart-hover-point wa-hover-previous" r="3.5" stroke="#a9ccef"/></g>'
            + '<rect class="wa-chart-hit wa-chart-interaction" x="' + padding.left + '" y="' + padding.top + '" width="' + (width - padding.left - padding.right) + '" height="' + (height - padding.top - padding.bottom) + '"/>'
            + '</svg>';
        $('#wa-chart').html(svg);
        bindChartInteraction(currentPoints, previousPoints, timestamps, bucket, metric, padding, width, height);
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
        if (diagnostics.backfill_for_site) return badge.addClass('is-warn').html('<i></i>正在恢复历史数据');
        if (!diagnostics.webserver_configured && !diagnostics.nginx_configured) return badge.addClass('is-warn').html('<i></i>站点尚未接入');
        var queue = diagnostics.queue || {};
        if (Number(queue.write_errors || 0) > 0 || Number(queue.dropped || 0) > 0) return badge.addClass('is-warn').html('<i></i>采集队列异常');
        var time = new Date(Number(run.updated_at || 0) * 1000);
        if ((Date.now() - time.getTime()) > 30000) return badge.addClass('is-warn').html('<i></i>实时服务无响应');
        if (!diagnostics.received_for_site) return badge.addClass('is-warn').html('<i></i>暂无实时请求');
        badge.addClass('is-good').html('<i></i>实时接收中');
    }

    function renderDiagnostics(data) {
        var diagnostics = data.diagnostics || {};
        var requests = Number((data.overview || {}).requests || 0);
        var queue = diagnostics.queue || {};
        var sync = diagnostics.config_sync || {};
        var history = diagnostics.history_import || {};
        if (!diagnostics.service_ready || !diagnostics.socket_ready) {
            var serviceError = ((data.health || {}).realtime_service || {}).error || '';
            showNotice('实时采集服务未正常运行' + (serviceError ? '：' + serviceError : '，当前不会产生新统计数据。'), true);
        } else if (diagnostics.backfill_for_site) {
            showNotice('');
        } else if (!diagnostics.webserver_configured && !diagnostics.nginx_configured) {
            showNotice('当前网站尚未写入 ' + (diagnostics.web_server === 'apache' ? 'Apache' : 'Nginx') + ' 实时日志扩展配置。', true);
        } else if (Number(queue.write_errors || 0) > 0) {
            showNotice('实时队列写入统计数据库失败 ' + Number(queue.write_errors) + ' 次，请修复采集服务。', true);
        } else if (Number(queue.dropped || 0) > 0) {
            showNotice('访问高峰期间采集队列已丢弃 ' + Number(queue.dropped) + ' 条日志，请增大 queue_size 后重启服务。');
        } else if (sync.success === false) {
            showNotice('站点配置自动同步失败：' + (sync.message || '未知原因'), true);
        } else if (!diagnostics.received_for_site && requests === 0) {
            if (history.error) {
                showNotice('历史日志初始化失败：' + history.error, true);
            } else if (Number(history.lines || 0) > 0 && Number(history.events || 0) === 0) {
                showNotice('历史初始化已读取 ' + formatNumber(history.lines) + ' 行日志，但没有一行符合支持的 Nginx/Apache 访问日志格式。日志路径：' + (history.log_path || '未知'));
            } else if (Number(history.events || 0) > 0) {
                showNotice('已从历史日志导入 ' + formatNumber(history.events) + ' 条请求，但当前选择的时间范围内没有数据；可切换“近 7 天”或“近 30 天”查看。');
            } else if (history.complete) {
                showNotice('历史日志初始化已完成，但当前日志及轮转日志中没有可解析的完整访问记录。日志路径：' + (history.log_path || '未知'));
            } else {
                showNotice('历史日志初始化没有生成数据，请检查站点访问日志是否存在及其格式。');
            }
        }
    }

    function render(data) {
        state.data = data;
        state.siteId = Number(data.selected_site_id || 0);
        renderSiteOptions(data.sites || [], state.siteId);
        renderMetrics(data);
        renderChartTabs();
        renderChart();
        renderHealth(data.health || {}, data.diagnostics || {});
        renderDiagnostics(data);
        $('#wa-generated-at').text('数据生成于 ' + new Date(Number(data.generated_at || Date.now() / 1000) * 1000).toLocaleString('zh-CN'));
        $('#wa-dashboard').prop('hidden', false);
        showPage('overview');
    }

    function siteSortValue(site, key) {
        if (key === 'name') return String(site.name || '').toLocaleLowerCase('zh-CN');
        if (key === 'error_rate') return Number(site.error_rate || 0);
        return Number((site.metrics || {})[key] || 0);
    }

    function formatLastSeen(timestamp) {
        var value = Number(timestamp || 0);
        if (!value) return '暂无数据';
        var date = new Date(value * 1000);
        var now = new Date();
        if (date.toDateString() === now.toDateString()) {
            return String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
        }
        return (date.getMonth() + 1) + '-' + String(date.getDate()).padStart(2, '0') + ' '
            + String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
    }

    function renderSitesTable() {
        var data = state.sitesData || {};
        var query = String(state.siteQuery || '').trim().toLocaleLowerCase('zh-CN');
        var sites = (data.sites || []).filter(function (site) {
            return !query || String(site.name || '').toLocaleLowerCase('zh-CN').indexOf(query) !== -1;
        });
        var sort = state.siteSort;
        sites.sort(function (left, right) {
            var leftValue = siteSortValue(left, sort.key);
            var rightValue = siteSortValue(right, sort.key);
            var result;
            if (typeof leftValue === 'string') result = leftValue.localeCompare(rightValue, 'zh-CN');
            else result = leftValue === rightValue ? 0 : (leftValue < rightValue ? -1 : 1);
            if (!result) result = String(left.name || '').localeCompare(String(right.name || ''), 'zh-CN');
            return sort.direction === 'asc' ? result : -result;
        });

        var allowedStatuses = { collecting: true, waiting: true, unconfigured: true, error: true };
        var html = '';
        sites.forEach(function (site) {
            var metrics = site.metrics || {};
            var status = site.status || {};
            var statusKey = allowedStatuses[status.key] ? status.key : 'waiting';
            var errorRate = Number(site.error_rate || 0);
            var server = String(site.web_server || 'nginx').toLowerCase() === 'apache' ? 'Apache' : 'Nginx';
            html += '<tr>'
                + '<td><div class="wa-site-name" title="' + escapeHtml(site.name) + '">' + escapeHtml(site.name) + '</div><div class="wa-site-id">站点 ID ' + Number(site.panel_site_id || 0) + '</div></td>'
                + '<td><span class="wa-status is-' + statusKey + '">' + escapeHtml(status.label || '等待访问') + '</span></td>'
                + '<td><span class="wa-server">' + server + '</span></td>'
                + '<td class="is-number">' + formatNumber(metrics.pv) + '</td>'
                + '<td class="is-number">' + formatNumber(metrics.uv) + '</td>'
                + '<td class="is-number">' + formatNumber(metrics.ip) + '</td>'
                + '<td class="is-number">' + formatNumber(metrics.requests) + '</td>'
                + '<td class="is-number">' + formatBytes(metrics.body_bytes) + '</td>'
                + '<td class="is-number wa-error-rate' + (errorRate > 0 ? ' has-errors' : '') + '">' + errorRate.toFixed(2) + '%</td>'
                + '<td class="wa-last-seen">' + formatLastSeen(metrics.last_seen) + '</td>'
                + '<td class="is-action"><button class="wa-view-site" data-open-site="' + Number(site.id) + '">查看概览</button></td>'
                + '</tr>';
        });
        $('#wa-sites-body').html(html);
        $('#wa-sites-empty').prop('hidden', sites.length > 0);
        $('#wa-site-count').text(sites.length + (sites.length === (data.sites || []).length ? ' 个网站' : ' / ' + (data.sites || []).length + ' 个网站'));
        $('.wa-table th button[data-sort]').removeClass('is-sorted').removeAttr('data-direction')
            .filter('[data-sort="' + sort.key + '"]').addClass('is-sorted').attr('data-direction', sort.direction === 'asc' ? '↑' : '↓');
        $('#wa-sites-generated-at').text('数据生成于 ' + new Date(Number(data.generated_at || Date.now() / 1000) * 1000).toLocaleString('zh-CN'));
    }

    function renderSitesPage(data) {
        state.sitesData = data || {};
        renderSitesTable();
        $('#wa-dashboard').prop('hidden', false);
        showPage('sites');
    }

    function renderSpiderTrend(rows) {
        var values = (rows || []).map(function (row) { return Number(row.requests || 0); });
        if (!values.length || Math.max.apply(Math, values) <= 0) {
            return $('#wa-spider-trend').html('<div class="wa-empty-chart">暂无蜘蛛趋势数据</div>');
        }
        var width = 520, height = 140, padding = { left: 35, right: 8, top: 10, bottom: 22 };
        var max = niceMaximum(Math.max.apply(Math, values), { bytes: false, decimal: false });
        var points = chartPoints(values, width, height, padding, max);
        var path = smoothPathFor(points);
        var baseline = height - padding.bottom;
        var area = path + ' L' + points[points.length - 1].x + ',' + baseline + ' L' + points[0].x + ',' + baseline + ' Z';
        var labels = '', dots = '';
        var step = Math.max(1, Math.ceil(points.length / 6));
        points.forEach(function (point, index) {
            if (index % step === 0 || index === points.length - 1) {
                labels += '<text x="' + point.x + '" y="137" text-anchor="middle" fill="#929dac" font-size="9">' + escapeHtml(chartTimeLabel(rows[index].timestamp, Number((state.spidersData.range || {}).bucket || 3600), false)) + '</text>';
            }
            if (point.value > 0) dots += '<circle cx="' + point.x + '" cy="' + point.y + '" r="2.5" fill="#fff" stroke="#7b61d1" stroke-width="1.5"><title>' + formatNumber(point.value) + ' 次</title></circle>';
        });
        $('#wa-spider-trend').html('<svg viewBox="0 0 520 140" aria-hidden="true"><line x1="35" y1="' + baseline + '" x2="512" y2="' + baseline + '" stroke="#e5eaf0"/><path d="' + area + '" fill="rgba(123,97,209,.10)"/><path d="' + path + '" fill="none" stroke="#7b61d1" stroke-width="2"/>' + dots + labels + '</svg>');
    }

    function renderSpidersPage(data) {
        state.spidersData = data || {};
        state.siteId = Number(data.selected_site_id || 0);
        renderSiteOptions(data.sites || [], state.siteId);
        var summary = data.summary || {}, ranking = data.ranking || [], total = Number(summary.requests || 0);
        $('#wa-spider-kpis').html(
            '<div class="wa-spider-kpi"><span>蜘蛛请求</span><strong>' + formatNumber(total) + '</strong></div>'
            + '<div class="wa-spider-kpi"><span>识别类型</span><strong>' + formatNumber(summary.types) + '</strong></div>'
            + '<div class="wa-spider-kpi"><span>蜘蛛流量</span><strong>' + formatBytes(summary.body_bytes) + '</strong></div>'
            + '<div class="wa-spider-kpi"><span>错误率</span><strong>' + Number(summary.error_rate || 0).toFixed(2) + '%</strong></div>'
        );
        var colors = ['#7b61d1','#3f82f7','#20a53a','#f0a13a','#e05d68','#55b7b3','#98a2b1'];
        var stops = [], legend = '', cursor = 0;
        ranking.slice(0, 6).forEach(function (row, index) {
            var share = total ? Number(row.requests || 0) * 100 / total : 0;
            stops.push(colors[index] + ' ' + cursor.toFixed(2) + '% ' + (cursor + share).toFixed(2) + '%');
            cursor += share;
            legend += '<div><span><i style="background:' + colors[index] + '"></i>' + escapeHtml(row.spider) + '</span><b>' + share.toFixed(1) + '%</b></div>';
        });
        if (cursor < 100) stops.push('#dce4ec ' + cursor.toFixed(2) + '% 100%');
        $('#wa-spider-donut').css('background', stops.length ? 'conic-gradient(' + stops.join(',') + ')' : '').html('<b>' + formatNumber(total) + '</b><span>蜘蛛请求</span>');
        $('#wa-spider-legend').html(legend || '<div><span>暂无分类数据</span></div>');
        var body = '';
        ranking.slice(0, 6).forEach(function (row) {
            var requests = Number(row.requests || 0), errors = Number(row.errors || 0);
            body += '<tr><td class="wa-spider-name">' + escapeHtml(row.spider) + '</td><td class="is-number">' + formatNumber(requests) + '</td><td class="is-number">' + (total ? requests * 100 / total : 0).toFixed(2) + '%</td><td class="is-number">' + formatBytes(row.body_bytes) + '</td><td class="is-number">' + (requests ? errors * 100 / requests : 0).toFixed(2) + '%</td><td>' + formatLastSeen(row.last_seen) + '</td></tr>';
        });
        $('#wa-spider-body').html(body);
        $('#wa-spider-empty').prop('hidden', ranking.length > 0);
        renderSpiderTrend(data.trend || []);
        $('#wa-spiders-generated-at').text('数据生成于 ' + new Date(Number(data.generated_at || Date.now() / 1000) * 1000).toLocaleString('zh-CN'));
        $('#wa-dashboard').prop('hidden', false);
        showPage('spiders');
    }

    function renderClients(data) {
        var titles = { browser: '浏览器', system: '操作系统', device: '设备类型' }, html = '';
        Object.keys(titles).forEach(function (key) {
            var rows = (data.dimensions || {})[key] || [];
            var total = rows.reduce(function (sum, row) { return sum + Number(row.requests || 0); }, 0);
            html += '<section class="wa-dimension-card"><h3>' + titles[key] + '</h3>';
            rows.slice(0, 10).forEach(function (row) {
                var share = total ? Number(row.requests || 0) * 100 / total : 0;
                html += '<div class="wa-dimension-row"><span>' + escapeHtml(row.name) + '</span><span class="wa-dimension-bar"><i style="width:' + share.toFixed(2) + '%"></i></span><b>' + share.toFixed(1) + '%</b></div>';
            });
            html += rows.length ? '</section>' : '<div class="wa-empty-chart">暂无数据</div></section>';
        });
        $('#wa-client-dimensions').html(html);
    }

    function renderRank(data, kind) {
        var html = '';
        if (kind === 'ip') {
            var geo = data.geoip || {};
            $('#wa-geoip-status').text(geo.available ? '本地 IP 库已启用' : '未检测到本地 IP 库');
        }
        (data.items || []).slice(0, 12).forEach(function (row) {
            var requests = Number(row.requests || 0), errors = Number(row.errors || 0);
            html += '<tr><td title="' + escapeHtml(row.name) + '">' + escapeHtml(row.name) + '</td>'
                + (kind === 'ip' ? '<td title="' + escapeHtml(row.location || '未知') + '">' + escapeHtml(row.location || '未知') + '</td>' : '')
                + '<td>' + formatNumber(requests) + '</td><td>' + formatBytes(row.body_bytes) + '</td><td>' + (requests ? errors * 100 / requests : 0).toFixed(2) + '%</td><td>' + formatLastSeen(row.last_seen) + '</td></tr>';
        });
        $('#wa-' + kind + '-body').html(html || '<tr><td colspan="' + (kind === 'ip' ? 6 : 5) + '">当前时段暂无明细数据</td></tr>');
    }

    function renderRequestPage(data, kind) {
        var html = '';
        (data.items || []).forEach(function (row) {
            var date = new Date(Number(row.timestamp || 0) * 1000).toLocaleString('zh-CN');
            var tail = kind === 'errors' ? formatBytes(row.body_bytes) : escapeHtml((row.browser || 'Other') + ' / ' + (row.device || 'Other'));
            html += '<tr><td>' + escapeHtml(date) + '</td><td><span class="wa-status-code' + (Number(row.status) >= 400 ? ' is-error' : '') + '">' + Number(row.status || 0) + '</span></td><td>' + escapeHtml(row.method) + '</td><td title="' + escapeHtml(row.uri) + '">' + escapeHtml(row.uri) + '</td><td>' + escapeHtml(row.remote_addr) + '</td><td>' + tail + '</td></tr>';
        });
        $('#wa-' + kind + '-body').html(html || '<tr><td colspan="6">当前筛选条件下暂无访问明细</td></tr>');
        var pages = Math.max(1, Math.ceil(Number(data.total || 0) / Number(data.page_size || 12)));
        var current = Number(data.page || 1), start = Math.max(1, Math.min(current - 2, pages - 4)), end = Math.min(pages, start + 4), buttons = '';
        for (var page = start; page <= end; page++) {
            buttons += '<button data-page-target="' + page + '" class="' + (page === current ? 'is-current' : '') + '">' + page + '</button>';
        }
        $('#wa-' + kind + '-pager').html('<span>共 ' + formatNumber(data.total) + ' 条 · ' + current + ' / ' + pages + ' 页</span>'
            + '<button data-page-target="1" ' + (current <= 1 ? 'disabled' : '') + ' aria-label="首页">«</button>'
            + '<button data-page-step="-1" ' + (current <= 1 ? 'disabled' : '') + ' aria-label="上一页">‹</button>' + buttons
            + '<button data-page-step="1" ' + (current >= pages ? 'disabled' : '') + ' aria-label="下一页">›</button>'
            + '<button data-page-target="' + pages + '" ' + (current >= pages ? 'disabled' : '') + ' aria-label="末页">»</button>');
    }

    function renderReports(data) {
        var value = data.overview || {};
        $('#wa-report-kpis').html('<div><span>PV</span><strong>' + formatNumber(value.pv) + '</strong></div><div><span>UV</span><strong>' + formatNumber(value.uv) + '</strong></div><div><span>IP</span><strong>' + formatNumber(value.ip) + '</strong></div><div><span>请求</span><strong>' + formatNumber(value.requests) + '</strong></div><div><span>流量</span><strong>' + formatBytes(value.body_bytes) + '</strong></div>');
        function list(rows) { var html = '<div class="wa-report-list">'; (rows || []).slice(0, 10).forEach(function (row) { html += '<div><span title="' + escapeHtml(row.name) + '">' + escapeHtml(row.name) + '</span><b>' + formatNumber(row.requests) + '</b></div>'; }); return html + '</div>'; }
        $('#wa-report-uri').html(list(data.top_uri));
        $('#wa-report-ip').html(list(data.top_ip));
    }

    function renderSettings(data) {
        var form = $('#wa-settings-form')[0];
        form.elements.enabled.checked = !!data.enabled;
        form.elements.raw_retention_days.value = Number(data.raw_retention_days || 7);
        form.elements.error_retention_days.value = Number(data.error_retention_days || 30);
        form.elements.analytics_retention_days.value = Number(data.analytics_retention_days || 90);
        form.elements.queue_size.value = Number(data.queue_size || 20000);
        form.elements.excluded_paths.value = (data.excluded_paths || []).join('\n');
    }

    function renderModule(page, data) {
        state.moduleData[page] = data;
        if (data.sites) {
            state.siteId = Number(data.selected_site_id || 0);
            renderSiteOptions(data.sites, state.siteId);
        }
        if (page === 'clients') renderClients(data);
        else if (page === 'ip' || page === 'uri') renderRank(data, page);
        else if (page === 'errors' || page === 'requests') renderRequestPage(data, page);
        else if (page === 'reports') renderReports(data);
        else if (page === 'settings') renderSettings(data);
        $('#wa-dashboard').prop('hidden', false);
        showPage(page);
    }

    function loadModule(page) {
        var methods = { clients:'get_clients', ip:'get_ip_rank', uri:'get_uri_rank', errors:'get_errors', requests:'get_requests', reports:'get_reports', settings:'get_settings' };
        var args = { site_id: state.siteId, period: state.period };
        if (page === 'errors') { args.page = state.pageNumber.errors; args.page_size = 12; args.query = $('#wa-error-query').val() || ''; args.status_group = $('#wa-error-group').val() || ''; }
        if (page === 'requests') { args.page = state.pageNumber.requests; args.page_size = 12; args.query = $('#wa-request-query').val() || ''; }
        setLoading(true, '正在读取' + $('.wa-nav [data-page="' + page + '"]').text().trim() + '...');
        requestPlugin(methods[page], args, function (response) {
            setLoading(false);
            if (!response || !response.success) return showNotice(response && response.message ? response.message : '读取数据失败');
            renderModule(page, response.data || {});
        });
    }

    function loadCurrentPage() {
        if (state.page === 'overview') loadDashboard();
        else if (state.page === 'sites') loadSites();
        else if (state.page === 'spiders') loadSpiders();
        else loadModule(state.page);
    }

    function loadDashboard() {
        var requestId = ++state.dashboardRequest;
        setLoading(true, '正在读取网站统计...');
        showNotice('');
        requestPlugin('get_bootstrap', { site_id: state.siteId, period: state.period }, function (response) {
            if (requestId !== state.dashboardRequest) return;
            setLoading(false);
            if (!response || !response.success) {
                showNotice(response && response.message ? response.message : '无法读取插件数据');
                return;
            }
            var data = response.data || {};
            if (!data.sites || !data.sites.length) {
                renderSiteOptions([], 0);
                showNotice(response.message || '未发现网站日志。请确认宝塔网站已开启访问日志。');
                $('#wa-dashboard').prop('hidden', true);
                return;
            }
            render(data);
        });
    }

    function loadSites() {
        state.dashboardRequest += 1;
        setLoading(true, '正在汇总全部网站...');
        showNotice('');
        requestPlugin('get_sites', { period: state.period }, function (response) {
            setLoading(false);
            if (!response || !response.success) {
                showNotice(response && response.message ? response.message : '无法读取网站列表');
                showPage('sites');
                return;
            }
            renderSitesPage(response.data || {});
        });
    }

    function loadSpiders() {
        state.dashboardRequest += 1;
        setLoading(true, '正在读取蜘蛛统计...');
        showNotice('');
        requestPlugin('get_spiders', { site_id: state.siteId, period: state.period }, function (response) {
            setLoading(false);
            if (!response || !response.success) {
                showNotice(response && response.message ? response.message : '无法读取蜘蛛统计');
                showPage('spiders');
                return;
            }
            renderSpidersPage(response.data || {});
        });
    }

    $('#wa-site-trigger').on('click', function () {
        if ($(this).prop('disabled')) return;
        var picker = $('#wa-site-picker');
        var open = !picker.hasClass('is-open');
        closeSitePicker();
        if (open) {
            picker.addClass('is-open');
            $(this).attr('aria-expanded', 'true');
            $('#wa-site-menu').prop('hidden', false).find('.is-selected').trigger('focus');
        }
    });

    $('#wa-site-menu').on('click', '.wa-site-option', function () {
        state.siteId = Number($(this).data('site-id') || 0);
        closeSitePicker();
        loadCurrentPage();
    });

    $(document).on('mousedown.webanalytics-site-picker', function (event) {
        if (!$(event.target).closest('#wa-site-picker').length) closeSitePicker();
    });

    $('#wa-site-picker').on('keydown', function (event) {
        var options = $('#wa-site-menu .wa-site-option');
        if (event.key === 'Escape') { closeSitePicker(); $('#wa-site-trigger').trigger('focus'); return; }
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
        event.preventDefault();
        if ($('#wa-site-menu').prop('hidden')) $('#wa-site-trigger').trigger('click');
        var index = options.index(document.activeElement);
        index = event.key === 'ArrowDown' ? Math.min(options.length - 1, index + 1) : Math.max(0, index < 0 ? options.length - 1 : index - 1);
        options.eq(index).trigger('focus');
    });

    $('.wa-nav').on('click', 'button[data-page]', function () {
        var page = $(this).data('page');
        if (page === state.page && ((page === 'overview' && state.data) || (page === 'sites' && state.sitesData) || (page === 'spiders' && state.spidersData) || state.moduleData[page])) return;
        showPage(page);
        loadCurrentPage();
    });

    $('.wa-period').on('click', 'button', function () {
        state.period = $(this).data('period');
        $(this).addClass('is-active').siblings().removeClass('is-active');
        loadCurrentPage();
    });

    $('#wa-metrics, #wa-chart-tabs').on('click', '[data-metric]', function () {
        state.metric = $(this).data('metric');
        renderMetrics(state.data);
        renderChartTabs();
        renderChart();
    });

    $('#wa-refresh').on('click', function () {
        loadCurrentPage();
    });

    $('#wa-error-group').on('change', function () { state.pageNumber.errors = 1; loadModule('errors'); });
    $('#wa-error-query').on('change', function () { state.pageNumber.errors = 1; loadModule('errors'); });
    $('#wa-request-query').on('change', function () { state.pageNumber.requests = 1; loadModule('requests'); });
    $('.wa-pager').on('click', 'button', function () {
        if ($(this).prop('disabled') || $(this).hasClass('is-current')) return;
        var target = $(this).data('page-target');
        state.pageNumber[state.page] = target ? Number(target) : Math.max(1, Number(state.pageNumber[state.page] || 1) + Number($(this).data('page-step') || 0));
        loadModule(state.page);
    });

    $('#wa-settings-form').on('submit', function (event) {
        event.preventDefault();
        var form = this;
        requestPlugin('save_settings', {
            enabled: form.elements.enabled.checked,
            raw_retention_days: form.elements.raw_retention_days.value,
            error_retention_days: form.elements.error_retention_days.value,
            analytics_retention_days: form.elements.analytics_retention_days.value,
            queue_size: form.elements.queue_size.value,
            excluded_paths: form.elements.excluded_paths.value
        }, function (response) { showNotice(response && response.message ? response.message : '设置保存完成'); });
    });

    $('#wa-clear-data').on('click', function () {
        if (!window.confirm('确定清理当前网站的全部插件统计数据？原始网站日志不会删除。')) return;
        requestPlugin('clear_data', { site_id: state.siteId, confirm: 'CLEAR' }, function (response) {
            showNotice(response && response.message ? response.message : '清理完成');
        });
    });

    $('[data-export]').on('click', function () {
        var kind = String($(this).data('export'));
        var data = kind === 'report' ? state.moduleData.reports : state.moduleData[kind];
        if (!data) return;
        var rows = kind === 'report' ? (data.top_uri || []) : (data.items || []);
        var columns = kind === 'report' ? ['name','requests','body_bytes','errors'] : Object.keys(rows[0] || {});
        var safe = function (value) { var text = String(value == null ? '' : value); if (/^[=+\-@]/.test(text)) text = "'" + text; return '"' + text.replace(/"/g, '""') + '"'; };
        var csv = '\ufeff' + columns.map(safe).join(',') + '\r\n' + rows.map(function (row) { return columns.map(function (key) { return safe(row[key]); }).join(','); }).join('\r\n');
        var link = document.createElement('a'); link.href = URL.createObjectURL(new Blob([csv], { type:'text/csv;charset=utf-8' })); link.download = 'webanalytics-' + kind + '.csv'; link.click(); URL.revokeObjectURL(link.href);
    });

    $('#wa-site-search').on('input', function () {
        state.siteQuery = this.value || '';
        renderSitesTable();
    });

    $('.wa-table').on('click', 'th button[data-sort]', function () {
        var key = String($(this).data('sort') || 'requests');
        if (state.siteSort.key === key) {
            state.siteSort.direction = state.siteSort.direction === 'asc' ? 'desc' : 'asc';
        } else {
            state.siteSort.key = key;
            state.siteSort.direction = key === 'name' ? 'asc' : 'desc';
        }
        renderSitesTable();
    });

    $('#wa-sites-body').on('click', '[data-open-site]', function () {
        state.siteId = Number($(this).data('open-site') || 0);
        showPage('overview');
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
            showNotice('采集配置已修复，可点击右上角“刷新”查看最新状态。');
            window.setTimeout(loadDashboard, 1500);
        });
    });

    function fitPluginLayer() {
        var layerBox = $('.layui-layer-page');
        if (!layerBox.length) return;
        var viewportWidth = $(window).width();
        var viewportHeight = $(window).height();
        var modalWidth = Math.min(1380, Math.max(760, viewportWidth - 32));
        var modalHeight = Math.min(860, Math.max(560, viewportHeight - 32));
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
