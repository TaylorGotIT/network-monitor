"""网络质量监控：模块化实现。

包结构::

    netmon.config     配置默认值、校验、读写
    netmon.models     数据模型与目标状态机
    netmon.probes     ICMP / TCP / UDP 探测
    netmon.traceroute mtr / tracert / traceroute 解析与断点分析
    netmon.metrics    滚动窗口指标
    netmon.history    SQLite 历史数据（原始点 + 分钟聚合 + 断网事件）
    netmon.monitor    监控编排与 SSE 广播
    netmon.web        Tornado 路由与 API
"""

__version__ = "2.3.0"
