/* 网络监控前端逻辑：SSE 实时卡片 + 历史曲线/表格 + 断网事件 */
(function () {
  'use strict';

  var $ = function (id) { return document.getElementById(id); };

  /* 会话过期时统一跳转登录页 */
  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    return nativeFetch(input, init).then(function (response) {
      if (response.status === 401 && window.location.pathname !== '/login') {
        var next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.href = '/login?next=' + next;
      }
      return response;
    });
  };

  var state = {
    nodes: [],
    nodeSignature: '',
    selectedTarget: '',
    range: '1h',
    history: null,
    historySeq: 0,
    outages: [],
    expandedChecks: {},
    knownNodes: {},
    settingsFilled: false,
    lastSseAt: 0,
    chartLayout: null,
    hover: -1
  };

  var SETTING_KEYS = [
    'check_interval', 'window_size', 'fail_threshold',
    'mtr_recheck_interval', 'mtr_ping_count', 'timeout',
    'raw_retention_days', 'rollup_retention_days'
  ];

  /* ------------------------------------------------------------ 工具函数 */
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function pad2(n) { return n < 10 ? '0' + n : String(n); }

  function fmtTs(ts) {
    if (!ts) return '--';
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '--';
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' +
      pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  function fmtAxis(ts, withDate) {
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    var hm = pad2(d.getHours()) + ':' + pad2(d.getMinutes());
    return withDate ? (pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + hm) : hm;
  }

  function fmtDuration(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    if (seconds < 60) return seconds + ' 秒';
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + ' 分钟';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' 小时' + (minutes % 60 ? ' ' + (minutes % 60) + ' 分' : '');
    return Math.floor(hours / 24) + ' 天 ' + (hours % 24) + ' 小时';
  }

  function fmtBytes(bytes) {
    if (!bytes) return '0 B';
    var units = ['B', 'KB', 'MB', 'GB'];
    var i = 0;
    while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i += 1; }
    return (i === 0 ? bytes : bytes.toFixed(1)) + ' ' + units[i];
  }

  function numText(value, unit) {
    if (value === null || value === undefined) return '--';
    return value + (unit ? ' ' + unit : '');
  }

  var toastTimer = null;
  function toast(message, isError) {
    var el = $('toast');
    el.textContent = message;
    el.className = 'toast' + (isError ? ' error' : '');
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.hidden = true; }, 3200);
  }

  /* ------------------------------------------------------------ SSE 实时更新 */
  var sse = null;

  function setSsePill(text, cls) {
    var el = $('sseStatus');
    el.textContent = text;
    el.className = 'pill ' + cls;
  }

  function connectSse() {
    if (!window.EventSource) {
      setSsePill('⚠️ 浏览器不支持实时推送，使用轮询', 'pill-error');
      return;
    }
    sse = new EventSource('/api/events');
    sse.onopen = function () { setSsePill('● 实时推送已连接', 'pill-ok'); };
    sse.onmessage = function (event) {
      try {
        var data = JSON.parse(event.data);
        state.lastSseAt = Date.now();
        setSsePill('● 实时推送 · ' + (data.server_time || ''), 'pill-ok');
        applySnapshot(data);
      } catch (err) {
        console.error('解析推送数据失败', err);
      }
    };
    sse.onerror = function () {
      setSsePill('⚠️ 推送断开，正在自动重连…', 'pill-error');
    };
  }

  function applySnapshot(data) {
    state.nodes = data.nodes || [];
    var aliases = (data.config && data.config.known_nodes) || {};
    var aliasesChanged = JSON.stringify(aliases) !== JSON.stringify(state.knownNodes || {});
    state.knownNodes = aliases;
    renderCards();
    renderAliases(aliases);
    if (aliasesChanged && state.outages.length) {
      renderOutages();
    }
    syncTargetSelect();
    if (!state.settingsFilled && data.config && data.config.settings) {
      fillSettings(data.config.settings);
    }
  }

  /* 把文本里的已知 IP 替换成 “别名(IP)” */
  function withAliases(text) {
    if (!text) return '';
    Object.keys(state.knownNodes || {}).forEach(function (ip) {
      if (text.indexOf(ip) >= 0) {
        text = text.split(ip).join(state.knownNodes[ip] + '(' + ip + ')');
      }
    });
    return text;
  }

  /* 兜底轮询：SSE 异常时仍然能看到最新状态 */
  setInterval(function () {
    if (document.hidden) return;
    if (Date.now() - state.lastSseAt < 12000) return;
    fetch('/api/status').then(function (res) { return res.json(); }).then(applySnapshot).catch(function () {});
  }, 6000);

  /* ------------------------------------------------------------ 目标卡片 */
  var STATUS_META = {
    up: { cls: 'up', label: '在线' },
    degraded: { cls: 'degraded', label: '不稳定' },
    unknown: { cls: 'unknown', label: '无响应' },
    down: { cls: 'down', label: '断网' },
    pending: { cls: 'pending', label: '检测中' }
  };

  var CHECK_LABELS = {
    icmp: 'ICMP', tcp: 'TCP', tls: 'TLS', http: 'HTTP', https: 'HTTPS',
    udp: 'UDP', dns: 'DNS', ntp: 'NTP', dot: 'DoT', doh: 'DoH'
  };

  var CHECK_CHOICES = {
    icmp: [['icmp', 'ICMP (Ping)']],
    tcp: [
      ['tcp', 'TCP 握手'], ['tls', 'TLS + 证书'], ['https', 'HTTPS 请求'],
      ['http', 'HTTP 请求'], ['dot', 'DoT (DNS over TLS)'], ['doh', 'DoH (DNS over HTTPS)']
    ],
    udp: [['udp', 'UDP 通用（可能无响应）'], ['dns', 'DNS 查询'], ['ntp', 'NTP 查询']]
  };

  var CHECK_PORTS = {
    tls: '443', https: '443', http: '80', dot: '853', doh: '443', dns: '53', ntp: '123'
  };

  function checkLabel(check) {
    return CHECK_LABELS[check] || String(check || '').toUpperCase();
  }

  function renderCards() {
    var host = $('cards');
    if (!state.nodes.length) {
      host.innerHTML = '<div class="empty-state">暂无监控目标，请在下方添加。</div>';
      return;
    }
    var html = '';
    state.nodes.forEach(function (node) {
      var meta = STATUS_META[node.status] || STATUS_META.pending;
      var latency = node.latency;
      var loss = node.loss || 0;
      var lossCls = loss >= 100 ? 'bad' : (loss > 0 ? 'warn' : '');
      var footRight = node.consec_fails
        ? '连续失败 ' + node.consec_fails + ' 次'
        : '窗口样本 ' + (node.samples || 0) + ' 次';

      var detailHtml = '';
      if (node.status === 'down') {
        var faultText = withAliases(node.current_fault_node || '正在执行路由追踪…');
        var titleText = faultText + (node.last_error ? ' · ' + node.last_error : '');
        detailHtml = '<div class="card-detail"><div class="fault-line" title="' +
          esc(titleText) + '">🔍 ' + esc(faultText) + '</div></div>';
      } else if ((node.status === 'degraded' || node.status === 'unknown') && node.last_error) {
        detailHtml = '<div class="card-detail"><div class="error-line" title="' +
          esc(node.last_error) + '">⛔ ' + esc(node.last_error) + '</div></div>';
      } else if (node.status === 'pending') {
        detailHtml = '<div class="card-detail"><div class="hint-line">等待首次探测结果…</div></div>';
      }

      html += '<article class="card ' + meta.cls + '">' +
        '<button class="del-btn" data-del="' + esc(node.id) + '" title="删除目标">✖</button>' +
        '<div class="card-head"><span class="card-title">' + esc(node.name) + '</span>' +
        '<span class="badge ' + meta.cls + '">' + meta.label + '</span></div>' +
        '<div class="card-sub">' + esc(checkLabel(node.check || node.type)) + ' · ' +
        esc(node.host) + (node.port ? ':' + esc(node.port) : '') + '</div>' +
        '<div class="metrics">' +
        '<div><span class="k">延迟</span><span class="v">' + esc(numText(latency, 'ms')) + '</span></div>' +
        '<div><span class="k">平均</span><span class="v">' + esc(numText(node.avg, 'ms')) + '</span></div>' +
        '<div><span class="k">丢包</span><span class="v ' + lossCls + '">' + esc(loss) + '%</span></div>' +
        '<div><span class="k">抖动</span><span class="v">' + esc(numText(node.jitter, 'ms')) + '</span></div>' +
        '</div>' +
        detailHtml +
        '<div class="card-foot"><span>最近检测 ' + esc(node.last_check || '--') + '</span>' +
        '<span>' + esc(footRight) + ' · <button class="card-link" data-history="' + esc(node.id) + '">历史</button></span></div>' +
        '</article>';
    });
    host.innerHTML = html;
  }

  /* ------------------------------------------------------------ 目标下拉框 */
  function syncTargetSelect() {
    var select = $('histTarget');
    var signature = state.nodes.map(function (n) { return n.id; }).join(',');
    if (signature === state.nodeSignature && select.options.length) {
      return;
    }
    state.nodeSignature = signature;

    var previous = select.value || state.selectedTarget;
    if (!state.nodes.length) {
      select.innerHTML = '<option value="">（无目标）</option>';
      state.selectedTarget = '';
      clearHistory();
      return;
    }

    var html = '';
    state.nodes.forEach(function (node) {
      var label = node.name + ' · ' + String(node.type || '').toUpperCase() + ' ' + node.host +
        (node.port ? ':' + node.port : '');
      html += '<option value="' + esc(node.id) + '">' + esc(label) + '</option>';
    });
    select.innerHTML = html;

    var exists = state.nodes.some(function (node) { return node.id === previous; });
    var target = exists ? previous : state.nodes[0].id;
    select.value = target;
    if (target !== state.selectedTarget) {
      state.selectedTarget = target;
      loadHistory();
    }
  }

  /* ------------------------------------------------------------ 历史数据 */
  function currentHistoryParams(includePoints) {
    var params = new URLSearchParams();
    params.set('target_id', $('histTarget').value || state.selectedTarget);
    params.set('range', state.range);
    if (includePoints) params.set('points', '240');
    if (state.range === 'custom') {
      params.set('from', $('histFrom').value);
      params.set('to', $('histTo').value);
    }
    return params;
  }

  function loadHistory(silent) {
    var targetId = $('histTarget').value || state.selectedTarget;
    if (!targetId) {
      clearHistory();
      return;
    }
    if (state.range === 'custom' && (!$('histFrom').value || !$('histTo').value)) {
      toast('请选择自定义的起止时间', true);
      return;
    }

    var params = currentHistoryParams(true);
    var seq = ++state.historySeq;
    if (!silent) {
      $('histSummary').innerHTML = '<div class="summary-empty">正在加载历史数据…</div>';
    }

    fetch('/api/history?' + params.toString())
      .then(function (res) { return res.json().then(function (body) { return { ok: res.ok, body: body }; }); })
      .then(function (result) {
        if (seq !== state.historySeq) return;
        if (!result.ok) {
          $('histSummary').innerHTML = '<div class="summary-empty">加载失败：' + esc(result.body.error || '未知错误') + '</div>';
          return;
        }
        state.history = result.body;
        renderHistory(result.body);
        var exportParams = currentHistoryParams(false);
        $('histExport').href = '/api/history/export?' + exportParams.toString();
      })
      .catch(function (err) {
        if (seq !== state.historySeq) return;
        $('histSummary').innerHTML = '<div class="summary-empty">加载失败：' + esc(err.message) + '</div>';
      });
  }

  function clearHistory() {
    state.history = null;
    $('histSummary').innerHTML = '';
    $('histTableBody').innerHTML = '';
    $('chartEmpty').hidden = false;
    var canvas = $('histChart');
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    $('histMeta').textContent = '';
  }

  function bucketLabel(seconds) {
    if (seconds >= 86400) return Math.round(seconds / 86400) + ' 天/桶';
    if (seconds >= 3600) return Math.round(seconds / 3600) + ' 小时/桶';
    if (seconds >= 60) return Math.round(seconds / 60) + ' 分钟/桶';
    return seconds + ' 秒/桶';
  }

  function renderHistory(data) {
    var summary = data.summary || {};
    var rangeLabel = {
      '1h': '最近 1 小时', '6h': '最近 6 小时', '24h': '最近 24 小时',
      '7d': '最近 7 天', '30d': '最近 30 天', 'custom': '自定义区间'
    }[data.range] || data.range;

    var chips = [
      ['平均延迟', numText(summary.avg, 'ms'), ''],
      ['最大延迟', numText(summary.max, 'ms'), ''],
      ['丢包率', (summary.loss === undefined ? '--' : summary.loss + '%'), summary.loss > 0 ? 'warn' : ''],
      ['抖动', numText(summary.jitter, 'ms'), ''],
      ['在线率', (summary.uptime === null || summary.uptime === undefined ? '--' : summary.uptime + '%'), ''],
      ['样本数', summary.samples || 0, '']
    ];
    $('histSummary').innerHTML = chips.map(function (chip) {
      return '<span class="summary-chip">' + esc(chip[0]) + '<b class="' + chip[2] + '">' + esc(chip[1]) + '</b></span>';
    }).join('');

    var sourceText = data.source === 'rollup' ? '分钟聚合' : '原始探测点';
    $('histMeta').textContent = rangeLabel + ' · ' + bucketLabel(data.bucket) + ' · ' +
      sourceText + ' · ' + (data.series || []).length + ' 个数据点';

    renderHistoryTable(data.series || []);
    drawChart();
  }

  function renderHistoryTable(series) {
    var rows = series.slice().reverse().map(function (point) {
      return '<tr>' +
        '<td class="num">' + esc(fmtTs(point.t)) + '</td>' +
        '<td class="num">' + esc(numText(point.avg, 'ms')) + '</td>' +
        '<td class="num">' + esc(numText(point.min, 'ms')) + '</td>' +
        '<td class="num">' + esc(numText(point.max, 'ms')) + '</td>' +
        '<td class="num">' + esc(point.loss) + '%</td>' +
        '<td class="num">' + esc(numText(point.jitter, 'ms')) + '</td>' +
        '<td class="num">' + esc(point.samples) + '</td>' +
        '</tr>';
    }).join('');
    $('histTableBody').innerHTML = rows ||
      '<tr><td colspan="7" class="empty">暂无数据</td></tr>';
    applyTableLimit('histTableWrap', 5);
  }

  /* 表格最多显示 N 行，其余在表格区域内滚动 */
  function applyTableLimit(wrapperId, visibleRows) {
    var wrap = document.getElementById(wrapperId);
    if (!wrap) return;
    var table = wrap.querySelector('table');
    var tbody = wrap.querySelector('tbody');
    if (!table || !tbody) return;
    var rows = Array.prototype.filter.call(tbody.querySelectorAll('tr'), function (row) {
      return !row.querySelector('td.empty') && row.offsetHeight > 0;
    });
    if (!rows.length) {
      wrap.style.maxHeight = '';
      return;
    }
    var thead = table.querySelector('thead');
    var headerHeight = thead ? thead.getBoundingClientRect().height : 0;
    var count = Math.min(rows.length, visibleRows);
    var total = 0;
    for (var index = 0; index < count; index += 1) {
      total += rows[index].getBoundingClientRect().height;
    }
    wrap.style.maxHeight = Math.ceil(headerHeight + total + 1) + 'px';
  }

  /* ------------------------------------------------------------ 画图 */
  function niceMax(value) {
    if (!value || value <= 0) return 10;
    var exponent = Math.floor(Math.log(value) / Math.LN10);
    var fraction = value / Math.pow(10, exponent);
    var nice = fraction <= 1 ? 1 : (fraction <= 2 ? 2 : (fraction <= 5 ? 5 : 10));
    return nice * Math.pow(10, exponent);
  }

  function drawChart() {
    var canvas = $('histChart');
    var empty = $('chartEmpty');
    var data = state.history;

    if (!data || !data.series || !data.series.length) {
      empty.hidden = false;
      state.chartLayout = null;
      var emptyCtx = canvas.getContext('2d');
      emptyCtx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    empty.hidden = true;

    var dpr = window.devicePixelRatio || 1;
    var cssWidth = canvas.clientWidth || canvas.parentNode.clientWidth || 800;
    var cssHeight = canvas.clientHeight || 280;
    canvas.width = Math.round(cssWidth * dpr);
    canvas.height = Math.round(cssHeight * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssWidth, cssHeight);

    var pad = { l: 48, r: 46, t: 14, b: 26 };
    var plotW = Math.max(10, cssWidth - pad.l - pad.r);
    var plotH = Math.max(10, cssHeight - pad.t - pad.b);

    var series = data.series;
    var count = series.length;
    var latMax = niceMax(Math.max(
      data.summary && data.summary.max ? data.summary.max : 0,
      series.reduce(function (acc, p) { return Math.max(acc, p.max || 0, p.avg || 0); }, 0)
    ));
    var lossMax = 100;

    function xAt(index) {
      return count === 1 ? pad.l + plotW / 2 : pad.l + (index * plotW) / (count - 1);
    }
    function yLat(value) {
      return pad.t + plotH - (Math.min(value, latMax) / latMax) * plotH;
    }
    function yLoss(value) {
      return pad.t + plotH - (Math.min(value, lossMax) / lossMax) * plotH;
    }

    state.chartLayout = {
      pad: pad, plotW: plotW, plotH: plotH, count: count,
      xAt: xAt, yLat: yLat, yLoss: yLoss, latMax: latMax, cssWidth: cssWidth, cssHeight: cssHeight
    };

    // 网格 + 双轴刻度
    ctx.font = '11px -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif';
    ctx.textBaseline = 'middle';
    for (var i = 0; i <= 4; i += 1) {
      var y = pad.t + (plotH * i) / 4;
      ctx.strokeStyle = i === 4 ? '#334155' : 'rgba(51, 65, 85, 0.45)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.l, y);
      ctx.lineTo(pad.l + plotW, y);
      ctx.stroke();

      ctx.fillStyle = '#94a3b8';
      ctx.textAlign = 'right';
      ctx.fillText(String(Math.round((latMax * (4 - i)) / 4)), pad.l - 8, y);

      ctx.fillStyle = '#7c8ba1';
      ctx.textAlign = 'left';
      ctx.fillText(String(Math.round((lossMax * (4 - i)) / 4)) + '%', pad.l + plotW + 8, y);
    }

    // 丢包率柱状（右轴）
    var barWidth = Math.max(2, (plotW / Math.max(1, count)) * 0.55);
    series.forEach(function (point, index) {
      if (!point.loss) return;
      var x = xAt(index);
      var height = Math.max(1, (point.loss / lossMax) * plotH);
      ctx.fillStyle = point.loss >= 100 ? 'rgba(239, 68, 68, 0.45)' : 'rgba(248, 113, 113, 0.22)';
      ctx.fillRect(x - barWidth / 2, pad.t + plotH - height, barWidth, height);
    });

    // 延迟折线（左轴）
    ctx.strokeStyle = '#38bdf8';
    ctx.lineWidth = 2;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    var started = false;
    series.forEach(function (point, index) {
      if (point.avg === null || point.avg === undefined) {
        started = false;
        return;
      }
      var x = xAt(index);
      var y = yLat(point.avg);
      if (!started) { ctx.moveTo(x, y); started = true; } else { ctx.lineTo(x, y); }
    });
    ctx.stroke();

    // 数据点（点不多时才画，避免糊成一团）
    if (count <= 120) {
      ctx.fillStyle = '#0b1220';
      series.forEach(function (point, index) {
        if (point.avg === null || point.avg === undefined) return;
        var x = xAt(index);
        var y = yLat(point.avg);
        ctx.beginPath();
        ctx.arc(x, y, 2.4, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#38bdf8';
        ctx.lineWidth = 1.5;
        ctx.stroke();
      });
    }

    // X 轴时间刻度
    var span = data.end - data.start;
    var withDate = span > 86400 * 1.5;
    ctx.fillStyle = '#7c8ba1';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    var ticks = Math.min(6, count);
    for (var t = 0; t < ticks; t += 1) {
      var idx = count === 1 ? 0 : Math.round((t * (count - 1)) / (ticks - 1));
      var tx = xAt(idx);
      ctx.fillText(fmtAxis(series[idx].t, withDate), tx, pad.t + plotH + 7);
    }

    // 悬浮十字线
    if (state.hover >= 0 && state.hover < count) {
      var hx = xAt(state.hover);
      ctx.strokeStyle = 'rgba(148, 163, 184, 0.55)';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(hx, pad.t);
      ctx.lineTo(hx, pad.t + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      var hoverPoint = series[state.hover];
      if (hoverPoint.avg !== null && hoverPoint.avg !== undefined) {
        ctx.beginPath();
        ctx.arc(hx, yLat(hoverPoint.avg), 4, 0, Math.PI * 2);
        ctx.fillStyle = '#38bdf8';
        ctx.fill();
        ctx.strokeStyle = '#0b1220';
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }
  }

  function showTooltip(index) {
    var data = state.history;
    var layout = state.chartLayout;
    var tip = $('chartTip');
    if (!data || !layout || index < 0 || index >= layout.count) {
      tip.hidden = true;
      return;
    }
    var point = data.series[index];
    var x = layout.xAt(index);
    var y = point.avg === null || point.avg === undefined
      ? layout.pad.t + layout.plotH / 2
      : layout.yLat(point.avg);
    var left = Math.max(70, Math.min(layout.cssWidth - 70, x));
    var top = Math.max(52, y - 10);
    tip.innerHTML = '<div><b>' + esc(fmtTs(point.t)) + '</b></div>' +
      '<div>平均 ' + esc(numText(point.avg, 'ms')) + ' · 最大 ' + esc(numText(point.max, 'ms')) + '</div>' +
      '<div>丢包 ' + esc(point.loss) + '% · 抖动 ' + esc(numText(point.jitter, 'ms')) + ' · 样本 ' + esc(point.samples) + '</div>';
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
    tip.hidden = false;
  }

  /* ------------------------------------------------------------ 断网事件 */
  function loadOutages() {
    var hours = $('outageHours').value;
    fetch('/api/outages?hours=' + encodeURIComponent(hours) + '&limit=100')
      .then(function (res) { return res.json(); })
      .then(function (body) {
        state.outages = body.outages || [];
        renderOutages();
      })
      .catch(function () {});
  }

  function renderCheck(check) {
    var hops = (check.hops || []).map(function (hop) {
      var alias = hop.alias ? ' <span class="hop-alias">' + esc(hop.alias) + '</span>' : '';
      return '#' + esc(hop.hop) + ' ' + esc(hop.ip) + alias +
        ' (' + esc(hop.loss) + '% / ' + esc(hop.avg) + 'ms)';
    }).join(' → ');
    return '<div class="mtr-detail">' +
      '<div class="head">' + esc(check.time) + ' <span class="fault">' +
      esc(withAliases(check.fault_node || '')) + '</span></div>' +
      '<div class="hops">' + (hops || '未解析到跳点') + '</div>' +
      '</div>';
  }

  function renderOutages() {
    var outages = state.outages || [];
    var body = $('outageTableBody');
    var empty = $('outageEmpty');
    if (!outages.length) {
      body.innerHTML = '';
      empty.hidden = false;
      return;
    }
    empty.hidden = true;

    body.innerHTML = outages.map(function (outage) {
      var statusClass = outage.status === '故障中' ? 'down' : (outage.status === '已恢复' ? 'up' : 'pending');
      var allChecks = outage.checks || [];
      var expanded = !!state.expandedChecks[outage.id];
      var visibleChecks = expanded ? allChecks : allChecks.slice(-1);
      var checksHtml = visibleChecks.map(renderCheck).join('');

      var toggleHtml = '';
      if (allChecks.length > 1) {
        toggleHtml = '<button type="button" class="checks-toggle" data-checks="' + esc(outage.id) + '">' +
          (expanded
            ? '▾ 收起，只显示最近 1 次'
            : '▸ 下拉显示全部（共 ' + allChecks.length + ' 次）') +
          '</button>';
      }

      return '<tr>' +
        '<td><b>' + esc(outage.target_name) + '</b><br><span class="mono">' +
        esc(outage.protocol) + ' ' + esc(outage.host) + '</span></td>' +
        '<td class="num">起 ' + esc(fmtTs(outage.start_ts)) + '<br>止 ' +
        esc(outage.end_ts ? fmtTs(outage.end_ts) : '持续中') + '</td>' +
        '<td class="num">' + esc(fmtDuration(outage.duration_sec)) + '</td>' +
        '<td><span class="badge ' + statusClass + '">' + esc(outage.status) + '</span></td>' +
        '<td><div class="outage-checks">' + toggleHtml + '<div class="checks-list">' +
        (checksHtml || '<span class="mono">等待首次断点追踪…</span>') + '</div></div></td>' +
        '</tr>';
    }).join('');
  }

  /* ------------------------------------------------------------ 表单/交互 */
  function togglePortField() {
    var type = $('tType').value;
    var wrap = $('tPortWrap');
    wrap.hidden = type === 'icmp';
    $('tPort').required = type !== 'icmp';
  }

  function updateCheckOptions() {
    var type = $('tType').value;
    var select = $('tCheck');
    if (!select) return;
    var previous = select.value;
    var choices = CHECK_CHOICES[type] || CHECK_CHOICES.icmp;
    select.innerHTML = choices.map(function (item) {
      return '<option value="' + esc(item[0]) + '">' + esc(item[1]) + '</option>';
    }).join('');
    var values = choices.map(function (item) { return item[0]; });
    select.value = values.indexOf(previous) >= 0 ? previous : values[0];
    updatePortDefault();
  }

  function updatePortDefault() {
    var portInput = $('tPort');
    if (!portInput || portInput.value) return;
    var check = $('tCheck') ? $('tCheck').value : '';
    if (CHECK_PORTS[check]) portInput.value = CHECK_PORTS[check];
  }

  function fillSettings(settings) {
    SETTING_KEYS.forEach(function (key) {
      var input = $('set_' + key);
      if (!input) return;
      if (document.activeElement === input) return;
      input.value = settings[key];
    });
    state.settingsFilled = true;
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (body) { return { ok: res.ok, body: body }; });
    });
  }

  function bindEvents() {
    $('tType').addEventListener('change', function () {
      updateCheckOptions();
      togglePortField();
    });
    $('tCheck').addEventListener('change', updatePortDefault);
    togglePortField();
    updateCheckOptions();

    $('targetForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var payload = {
        name: $('tName').value,
        type: $('tType').value,
        check: $('tCheck').value,
        host: $('tHost').value,
        port: $('tPort').value
      };
      postJson('/api/target', payload).then(function (result) {
        if (!result.ok) return toast(result.body.error || '添加失败', true);
        $('tName').value = '';
        $('tHost').value = '';
        $('tPort').value = '';
        toast('已添加监控目标，首次探测约 1~3 秒后出结果');
        fetch('/api/status').then(function (r) { return r.json(); }).then(applySnapshot);
        setTimeout(loadOutages, 800);
      }).catch(function (err) { toast(err.message, true); });
    });

    $('aliasForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var ip = $('aIp').value.trim();
      var name = $('aName').value.trim();
      if (!ip || !name) return toast('请填写 IP 和名称', true);
      postJson('/api/alias', { ip: ip, name: name }).then(function (result) {
        if (!result.ok) return toast(result.body.error || '保存失败', true);
        $('aIp').value = '';
        $('aName').value = '';
        toast('别名已保存');
      }).catch(function (err) { toast(err.message, true); });
    });

    $('settingsForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var payload = {};
      SETTING_KEYS.forEach(function (key) {
        var input = $('set_' + key);
        if (input) payload[key] = input.value;
      });
      postJson('/api/settings', payload).then(function (result) {
        if (!result.ok) return toast(result.body.error || '保存失败', true);
        fillSettings(result.body.settings);
        $('saveMsg').textContent = '✅ 已保存并立即生效';
        setTimeout(function () { $('saveMsg').textContent = ''; }, 3000);
      }).catch(function (err) { toast(err.message, true); });
    });

    $('rangeSeg').addEventListener('click', function (event) {
      var button = event.target.closest('button[data-range]');
      if (!button) return;
      var range = button.getAttribute('data-range');
      Array.prototype.forEach.call($('rangeSeg').querySelectorAll('button'), function (item) {
        item.classList.toggle('active', item === button);
      });
      if (range === 'custom') {
        $('customRange').hidden = false;
        if (!$('histFrom').value) {
          var now = new Date();
          var from = new Date(now.getTime() - 3600 * 1000);
          $('histFrom').value = toLocalInput(from);
          $('histTo').value = toLocalInput(now);
        }
        return;
      }
      $('customRange').hidden = true;
      state.range = range;
      loadHistory();
    });

    $('histApply').addEventListener('click', function () {
      state.range = 'custom';
      loadHistory();
    });

    $('histTarget').addEventListener('change', function () {
      state.selectedTarget = $('histTarget').value;
      loadHistory();
    });

    $('histRefresh').addEventListener('click', function () { loadHistory(); });
    $('outageRefresh').addEventListener('click', loadOutages);
    $('outageHours').addEventListener('change', loadOutages);

    document.addEventListener('click', function (event) {
      var checksToggle = event.target.closest('[data-checks]');
      if (checksToggle) {
        var outageId = checksToggle.getAttribute('data-checks');
        state.expandedChecks[outageId] = !state.expandedChecks[outageId];
        renderOutages();
        return;
      }
      var historyBtn = event.target.closest('[data-history]');
      if (historyBtn) {
        var historyId = historyBtn.getAttribute('data-history');
        var select = $('histTarget');
        select.value = historyId;
        state.selectedTarget = historyId;
        loadHistory();
        $('historyPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
        return;
      }
      var delTarget = event.target.closest('[data-del]');
      if (delTarget) {
        var targetId = delTarget.getAttribute('data-del');
        if (confirm('确定删除该监控目标？历史数据仍会保留。')) {
          fetch('/api/target?id=' + encodeURIComponent(targetId), { method: 'DELETE' })
            .then(function () { fetch('/api/status').then(function (r) { return r.json(); }).then(applySnapshot); });
        }
        return;
      }
      var delAlias = event.target.closest('[data-alias]');
      if (delAlias) {
        var ip = delAlias.getAttribute('data-alias');
        fetch('/api/alias?ip=' + encodeURIComponent(ip), { method: 'DELETE' })
          .then(function () { fetch('/api/status').then(function (r) { return r.json(); }).then(applySnapshot); });
        return;
      }
      var resetBtn = event.target.closest('[data-reset]');
      if (resetBtn) {
        var resetUser = resetBtn.getAttribute('data-reset');
        var newPassword = prompt('为「' + resetUser + '」设置新密码（至少 8 位）');
        if (!newPassword) return;
        postJson('/api/users/password', { username: resetUser, password: newPassword })
          .then(function (result) {
            toast(result.ok ? '密码已重置' : (result.body.error || '重置失败'), !result.ok);
          })
          .catch(function (err) { toast(err.message, true); });
        return;
      }
      var delUser = event.target.closest('[data-user-del]');
      if (delUser) {
        var username = delUser.getAttribute('data-user-del');
        if (!confirm('确定删除用户「' + username + '」？')) return;
        fetch('/api/users?username=' + encodeURIComponent(username), { method: 'DELETE' })
          .then(function (res) { return res.json().then(function (body) { return { ok: res.ok, body: body }; }); })
          .then(function (result) {
            toast(result.ok ? '用户已删除' : (result.body.error || '删除失败'), !result.ok);
            if (result.ok) loadUsers();
          })
          .catch(function (err) { toast(err.message, true); });
        return;
      }
    });

    var canvas = $('histChart');
    canvas.addEventListener('mousemove', function (event) {
      var layout = state.chartLayout;
      if (!layout || !state.history) return;
      var rect = canvas.getBoundingClientRect();
      var x = event.clientX - rect.left;
      var index = layout.count === 1 ? 0 :
        Math.round(((x - layout.pad.l) / layout.plotW) * (layout.count - 1));
      index = Math.max(0, Math.min(layout.count - 1, index));
      if (index !== state.hover) {
        state.hover = index;
        drawChart();
      }
      showTooltip(index);
    });
    canvas.addEventListener('mouseleave', function () {
      state.hover = -1;
      $('chartTip').hidden = true;
      drawChart();
    });

    var resizeTimer = null;
    window.addEventListener('resize', function () {
      if (resizeTimer) clearTimeout(resizeTimer);
      resizeTimer = setTimeout(function () {
        drawChart();
        applyTableLimit('histTableWrap', 5);
      }, 150);
    });

    var histDetails = $('histDetails');
    if (histDetails) {
      histDetails.addEventListener('toggle', function () {
        if (histDetails.open) applyTableLimit('histTableWrap', 5);
      });
    }
  }

  function toLocalInput(date) {
    return date.getFullYear() + '-' + pad2(date.getMonth() + 1) + '-' + pad2(date.getDate()) +
      'T' + pad2(date.getHours()) + ':' + pad2(date.getMinutes());
  }

  function renderAliases(aliases) {
    var host = $('aliasList');
    var keys = Object.keys(aliases || {});
    if (!keys.length) {
      host.innerHTML = '<span class="empty">暂无别名</span>';
      return;
    }
    host.innerHTML = keys.map(function (ip) {
      return '<span class="chip"><b>' + esc(ip) + '</b>' +
        '<span class="alias">' + esc(aliases[ip]) + '</span>' +
        '<button class="x" data-alias="' + esc(ip) + '" title="删除别名">×</button></span>';
    }).join('');
  }

  /* ------------------------------------------------------------ 账号管理 */
  function logout() {
    fetch('/api/logout', { method: 'POST' })
      .catch(function () {})
      .then(function () { window.location.href = '/login'; });
  }

  function initAuth() {
    fetch('/api/auth/me')
      .then(function (res) { return res.json(); })
      .then(function (me) {
        if (!me || !me.ok) return;
        $('userChip').textContent = '👤 ' + me.username;
        $('accountName').textContent = me.username;
        $('accountRole').textContent = me.is_admin ? '管理员' : '普通用户';
        $('defaultPwdBanner').hidden = !me.must_change;
        if (me.is_admin) {
          $('adminSection').hidden = false;
          loadUsers();
        }
      })
      .catch(function () {});
  }

  function loadUsers() {
    fetch('/api/users')
      .then(function (res) { return res.json(); })
      .then(function (body) {
        if (!body || !body.ok) return;
        var users = body.users || [];
        $('userCount').textContent = '共 ' + users.length + ' 个账号';
        var rows = users.map(function (user) {
          return '<tr>' +
            '<td><b>' + esc(user.username) + '</b></td>' +
            '<td>' + (user.is_admin ? '<span class="tag-admin">管理员</span>' : '普通用户') + '</td>' +
            '<td class="mono">' + esc(user.created_time || '--') + '</td>' +
            '<td class="mono">' + esc(user.last_login || '--') + '</td>' +
            '<td><div class="user-actions">' +
            '<button type="button" class="btn-ghost" data-reset="' + esc(user.username) + '">重置密码</button>' +
            '<button type="button" class="btn-ghost btn-danger" data-user-del="' + esc(user.username) + '">删除</button>' +
            '</div></td>' +
            '</tr>';
        }).join('');
        $('userTableBody').innerHTML = rows || '<tr><td colspan="5" class="empty">暂无用户</td></tr>';
      })
      .catch(function () {});
  }

  function bindAccountEvents() {
    ['logoutBtn', 'logoutBtn2'].forEach(function (id) {
      var button = $(id);
      if (button) button.addEventListener('click', logout);
    });

    $('passwordForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var newPassword = $('newPassword').value;
      if (newPassword !== $('newPassword2').value) {
        return toast('两次输入的新密码不一致', true);
      }
      if (newPassword.length < 8) {
        return toast('新密码至少 8 位', true);
      }
      postJson('/api/auth/password', {
        old_password: $('oldPassword').value,
        new_password: newPassword
      }).then(function (result) {
        if (!result.ok) return toast(result.body.error || '修改失败', true);
        toast('密码已修改，请重新登录');
        setTimeout(function () { window.location.href = '/login'; }, 900);
      }).catch(function (err) { toast(err.message, true); });
    });

    $('userForm').addEventListener('submit', function (event) {
      event.preventDefault();
      var username = $('newUsername').value.trim();
      var password = $('newUserPassword').value;
      if (!username || !password) return toast('请填写用户名和初始密码', true);
      postJson('/api/users', {
        username: username,
        password: password,
        is_admin: $('newUserAdmin').checked
      }).then(function (result) {
        if (!result.ok) return toast(result.body.error || '添加失败', true);
        $('newUsername').value = '';
        $('newUserPassword').value = '';
        $('newUserAdmin').checked = false;
        toast('用户已创建');
        loadUsers();
      }).catch(function (err) { toast(err.message, true); });
    });
  }

  function bindDialogs() {
    $('accountBtn').addEventListener('click', function () {
      openDialog('accountDialog');
      if ($('adminSection') && !$('adminSection').hidden) loadUsers();
    });
    $('settingsBtn').addEventListener('click', function () {
      openDialog('settingsDialog');
    });
    Array.prototype.forEach.call(document.querySelectorAll('[data-close]'), function (button) {
      button.addEventListener('click', function () {
        var dialog = $(button.getAttribute('data-close'));
        if (dialog && dialog.close) dialog.close();
      });
    });
    ['accountDialog', 'settingsDialog'].forEach(function (id) {
      var dialog = $(id);
      if (!dialog) return;
      dialog.addEventListener('click', function (event) {
        if (event.target === dialog) dialog.close();
      });
    });
  }

  function openDialog(id) {
    var dialog = $(id);
    if (!dialog || dialog.open) return;
    if (typeof dialog.showModal === 'function') {
      dialog.showModal();
    } else {
      dialog.setAttribute('open', '');
    }
  }

  /* ------------------------------------------------------------ 历史库信息 */
  function refreshHealth() {
    fetch('/api/health').then(function (res) { return res.json(); }).then(function (body) {
      var storage = body.storage || {};
      $('storageInfo').textContent = '历史库 ' + fmtBytes(storage.size_bytes) +
        ' · ' + (storage.probes || 0) + ' 点 · ' + (storage.outages || 0) + ' 次故障';
    }).catch(function () {});
  }

  /* ------------------------------------------------------------ 启动 */
  function init() {
    bindEvents();
    bindAccountEvents();
    bindDialogs();
    initAuth();
    fetch('/api/status').then(function (res) { return res.json(); }).then(applySnapshot).catch(function () {});
    connectSse();
    loadOutages();
    refreshHealth();
    setInterval(function () { if (!document.hidden) refreshHealth(); }, 60000);
    setInterval(function () {
      if (!document.hidden && state.history && state.selectedTarget) loadHistory(true);
    }, 30000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
