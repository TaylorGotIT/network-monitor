# 网络质量与断点监控

<img src="static/favicon.svg" width="84" alt="项目图标" />

实时网络质量监控：ICMP / TCP / UDP 探测、按时间查询历史延迟与丢包、断网时自动执行
MTR / tracert 定位断点。后端 Tornado + SQLite，前端原生 JS（无外部依赖，离线可用）。

## 目录结构

```
monitor/
├─ app.py                    # 启动入口（python app.py）
├─ start.bat / stop.bat      # Windows 启动 / 停止
├─ config.json               # 监控目标与全局参数
├─ requirements.txt
├─ static/                   # 前端
│  ├─ index.html
│  ├─ app.css
│  ├─ app.js                 # 卡片、历史曲线、断网事件、账号管理
│  ├─ login.html / login.js  # 登录页
├─ netmon/                   # 后端，按职责拆分
│  ├─ config.py              # 默认值、校验、原子读写
│  ├─ auth.py                # 用户、密码哈希与会话
│  ├─ models.py              # Target / ProbeResult / Settings / 状态机
│  ├─ probes.py              # ICMP / TCP / UDP 探测与 ping 输出解析
│  ├─ traceroute.py          # mtr / tracert / traceroute 解析与断点分析
│  ├─ metrics.py             # 滚动窗口（延迟/丢包/抖动）
│  ├─ history.py             # SQLite：原始点 + 分钟聚合 + 断网事件
│  ├─ monitor.py             # 探测定时、状态机、事件、SSE 广播
│  ├─ web.py                 # HTTP 路由 / JSON API / SSE
│  └─ timeutil.py
├─ tests/                    # 单元测试 + 端到端冒烟测试
└─ data/
   ├─ monitor.db             # 历史数据库（自动创建）
   ├─ auth.db                # 用户与会话（自动创建）
   └─ cookie_secret          # 会话签名密钥（自动生成）
```

## 快速开始

Windows：

```bat
:: 直接启动（前台运行，Ctrl+C 停止）
.\venv\Scripts\python.exe app.py
:: 或双击 start.bat；停止用 stop.bat（按 8888 端口找进程）
```

Linux / macOS：

```bash
python3 app.py
```

浏览器打开 <http://127.0.0.1:8888>，会先跳转到登录页。首次启动会：

1. 规范化并补全 `config.json`（新增 `raw_retention_days` / `rollup_retention_days`）；
2. 创建 `data/monitor.db` 与账号库 `data/auth.db`；
3. 生成会话签名密钥 `data/cookie_secret`；
4. 若 `--legacy-logs` 指向的旧版日志存在，则一次性导入其中的断网事件。

### 登录与账号

首次启动自动创建默认管理员：

| 账号 | 密码 |
| --- | --- |
| `admin` | `admin@123` |

登录后请在页面底部「👤 账号管理」里修改密码。管理员还可以：

- 添加普通用户 / 管理员；
- 重置其他用户的密码；
- 删除用户（不能删除自己，且至少保留一个管理员）。

修改或重置密码后，该用户已有的登录会话立即失效，需要重新登录；登录态默认保持 7 天。
所有页面与 API 都需要登录，只有 `/login` 和 `/static/*` 公开。

### 命令行参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `0.0.0.0` | 监听地址；只允许本机访问可设为 `127.0.0.1` |
| `--port` | `8888` | 监听端口 |
| `--config` | `./config.json` | 配置文件 |
| `--db` | `./data/monitor.db` | 历史数据库 |
| `--static` | `./static` | 前端目录 |
| `--legacy-logs` | 可选 | 旧版事件日志路径，文件存在时一次性导入 |
| `--debug` | 关 | Tornado 调试模式 |
| `-v` | 关 | 输出调试日志 |

## 下载安装包

发布页：<https://github.com/TaylorGotIT/network-monitor/releases>

### Windows

1. 下载 `NetworkMonitor-<version>-win64.exe`；
2. 放到有写权限的目录（配置 `config.json` 和 `data/` 会生成在 exe 同目录），双击运行；
3. 浏览器打开 <http://127.0.0.1:8888>，初始账号 `admin / admin@123`。

### Ubuntu / Debian

```bash
sudo apt install ./network-monitor_<version>_all.deb
sudo systemctl status network-monitor
```

- 服务监听 `0.0.0.0:8888`，开机自启；
- 配置与数据库：`/var/lib/network-monitor/`；
- 程序目录：`/opt/network-monitor/`；
- 卸载：`sudo apt remove network-monitor`；连数据一起删：`sudo apt purge network-monitor`。

## 功能

- **登录与账号**：默认管理员 `admin / admin@123`，密码 PBKDF2 加密存储；管理员可添加用户、
  重置密码、删除用户，修改密码后旧会话立即失效。
- **多种探测方式**：ICMP；TCP 握手 / TLS 证书 / HTTP(S) 请求；UDP 通用 / DNS / NTP。
- **实时卡片**：延迟、平均、丢包、抖动；状态区分「检测中 / 在线 / 不稳定 / 无响应 / 断网」，
  失败时直接显示失败原因（如「ICMP: 来自 192.168.1.1 的回复: 无法访问目标主机」）。
- **历史曲线**：1 小时 / 6 小时 / 24 小时 / 7 天 / 30 天 / 自定义起止时间；
  左轴平均延迟折线，右轴丢包率柱状；分桶明细表；一键导出 CSV（带 BOM，Excel 直接打开）。
- **断网定位**：连续失败达到阈值后自动执行 mtr / tracert / traceroute，
  按周期复检并记录每次的断点与完整跳表。
- **节点别名**：给路由跳点起名，断点跳表里直接显示「电信骨干网出口」。
- **参数热更新**：检测间隔、窗口大小、失败阈值、MTR 周期、超时、数据保留天数即时生效。

## 探测方式（check）

每个目标除了 `type` / `port`，还可以指定 `check`：

| check | 适用协议 | 行为 |
| --- | --- | --- |
| `icmp` | icmp | ping 并解析真实回显 RTT |
| `tcp` | tcp | TCP 建连（SYN → SYN/ACK） |
| `tls` | tcp | TLS 握手 + 证书链/主机名/有效期校验 |
| `https` | tcp | TLS + HTTP HEAD，要求 2xx/3xx |
| `http` | tcp | 明文 HTTP HEAD，要求 2xx/3xx |
| `udp` | udp | 发送通用报文等回包；无回包记为「无响应」，不算断网 |
| `dns` | udp | 标准 DNS 查询，校验 txid / rcode / 应答数 |
| `ntp` | udp | 标准 48 字节 NTP 请求，校验 mode / stratum |
| `dot` | tcp | DNS over TLS（RFC 7858，默认 853），TLS + 长度前缀 |
| `doh` | tcp | DNS over HTTPS（RFC 8484，默认 443 `/dns-query`），TLS + POST |

可选字段：

| 字段 | 说明 |
| --- | --- |
| `sni` | TLS/HTTPS/DoT/DoH 的 SNI，默认使用 `host` |
| `path` | HTTP/HTTPS 请求路径；DoH 默认 `/dns-query` |
| `payload` | 通用 UDP 发送内容（十六进制，如 `00ff`） |
| `expect` | 通用 UDP 期望回包内容（十六进制子串） |
| `query` | DNS/DoT/DoH 查询的域名，默认 `www.baidu.com` |
| `retries` | 探测重试次数（1-10，默认 2） |

示例：

```json
{"type": "tcp", "host": "www.baidu.com", "port": 443, "check": "tls"}
{"type": "udp", "host": "ntp.aliyun.com", "port": 123, "check": "ntp"}
{"type": "tcp", "host": "dns.google", "port": 853, "check": "dot", "query": "example.com"}
{"type": "tcp", "host": "dns.alidns.com", "port": 443, "check": "doh", "path": "/dns-query"}
{"type": "udp", "host": "192.168.1.1", "port": 5000, "check": "udp", "payload": "00ff", "expect": "a1b2", "retries": 3}
```

不填 `check` 时按协议默认：`icmp→icmp`、`tcp→tcp`、`udp→udp`。
TCP/UDP 端口失败但路由到达主机时，断点会写成「主机可达，端口 X 不通（原因）」，
不再误报「已到达」。

## 默认参数与调优建议

默认值偏"稳"，避免网络轻微抖动就刷屏或误报断网：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `check_interval` | 10 秒 | 每个目标每 10 秒探测一次（5 个目标约 0.5 次/秒） |
| `window_size` | 30 次 | 卡片上的滚动统计窗口，30 × 10s ≈ 最近 5 分钟 |
| `fail_threshold` | 5 次 | 连续失败 5 次（约 50 秒）才判定断网，可跨过瞬时丢包 |
| `timeout` | 3 秒 | 单次探测超时，给移动网络/跨运营商链路留余量 |
| `mtr_recheck_interval` | 300 秒 | 断网期间每 5 分钟复检一次断点，首次断网立即追踪 |
| `mtr_ping_count` | 5 | mtr 每跳发包数 |

调优参考：

- 想更灵敏（快速发现闪断）：`check_interval=5`、`fail_threshold=3`
- 想更安静（只关心长时间断网）：`check_interval=30`、`fail_threshold=6`
- 弱网/移动网络误报多：把 `timeout` 提到 5 秒，或增大 `fail_threshold`

所有参数都可以在页面「⚙️ 全局监控参数」里修改，保存后立即生效并写回 `config.json`，无需重启。

## 本次修复的两个问题

### 1. 无法 ping 通的 IP 也显示「正常」

旧版 `check_icmp()` 有两个叠加的缺陷：

- **解码错误**：中文 Windows 的 `ping.exe` 输出是控制台代码页（cp936/GBK），旧版按 UTF-8
  解码后「时间=16ms」变成乱码，正则永远匹配不到；代码随后执行
  `return float(match.group(1)) if match else round(elapsed, 2)`，把**进程启动耗时**
  当成延迟返回（Windows 下恰好是 15.6ms 的整数倍），于是任何退出码为 0 的目标都「在线」。
- **退出码不可靠**：`ping` 收到「无法访问目标主机 / TTL 传输中过期」等 ICMP 错误时，
  **退出码仍是 0**，统计行还写着 `已接收 = 1, 丢失 = 0`。只看退出码或丢包率都会误判。

修复（`netmon/probes.py`）：

- 按控制台代码页解码（UTF-8 → Windows OEM/ANSI 代码页 → 系统首选编码），并按关键词命中数选择；
- 先识别 `无法访问目标主机 / 请求超时 / 传输失败 / Destination host unreachable / TTL expired`
  等失败标记，再解析「时间= / time=」回显；
- **只有解析到真实回显 RTT 才算成功**，否则一律失败，绝不伪造延迟；
- 初始状态改为 `pending`（检测中），连续失败达到阈值前显示「不稳定」，
  并把失败原因展示在卡片上；
- 另外修正了 ping 超时后子进程残留的问题。

### 2. 无法按时间查看历史延迟、丢包

旧版只保留内存里 20 个点，页面刷新/进程重启即丢失，也没有任何查询接口。
现在每次探测都写入 SQLite（`netmon/history.py`）：

- `probes`：原始探测点，默认保留 7 天；
- `rollup_1m`：分钟级聚合（次数/成功数/延迟和/极值/抖动差分），默认保留 180 天；
- 查询时 6 小时内直接用原始点，更长区间自动走聚合表，再归并成约 240 个时间桶，
  因此 30 天查询也很快；
- `outages` / `outage_checks` 持久化断网事件和每次 MTR 复检结果；
- 服务重启会接管未闭环的断网事件；目标被删除时对应事件标记为「已移除」。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/login` | 登录页 |
| POST | `/api/login` | 登录 `{username,password}` |
| POST | `/api/logout` | 退出登录 |
| GET | `/api/auth/me` | 当前账号信息 |
| POST | `/api/auth/password` | 修改自己的密码 `{old_password,new_password}` |
| GET | `/api/users` | 用户列表（管理员） |
| POST | `/api/users` | 新建用户（管理员）`{username,password,is_admin}` |
| DELETE | `/api/users?username=` | 删除用户（管理员） |
| POST | `/api/users/password` | 重置他人密码（管理员）`{username,password}` |
| GET | `/` | 监控页面 |
| GET | `/api/status` | 当前快照（节点、配置） |
| GET | `/api/events` | SSE 实时推送 |
| GET | `/api/history?target_id=&range=1h\|6h\|24h\|7d\|30d\|custom&from=&to=&points=` | 历史曲线与汇总 |
| GET | `/api/history/export?...` | 历史数据 CSV |
| GET | `/api/outages?hours=24&target_id=&limit=` | 断网事件（含跳表） |
| GET | `/api/health` | 健康状态与库统计 |
| POST | `/api/target` | 添加目标 `{name,host,type,port,check}` |
| DELETE | `/api/target?id=` | 删除目标 |
| POST | `/api/settings` | 更新参数（服务端会做范围校验） |
| POST/DELETE | `/api/alias` | 节点别名 |

除 `/login`、`/api/login` 与 `/static/*` 外，所有接口都需要登录 Cookie。

## 许可

本项目由 **Taylor.Gao** 以 [CC0 1.0 公有领域](https://creativecommons.org/publicdomain/zero/1.0/deed.zh)
发布：可自由使用、修改、分发，甚至用于商业用途，无需署名。详见 [LICENSE](LICENSE)。

## 测试

```bat
:: 单元测试（85 个用例：鉴权、ping 解析、探测方式、路由分析、历史库、监控状态机、HTTP）
.\venv\Scripts\python.exe -m unittest discover -s tests -t .

:: 端到端冒烟测试：真实启动服务、真实探测、重启后验证历史仍在
.\venv\Scripts\python.exe tests\smoke_server.py
```

## 常见问题

### ICMP 全部失败，错误是「传输失败 / 一般故障」

说明运行 `app.py` 的 Python 进程处于 Windows **低完整性级别（Low IL）**，无权创建 ICMP
套接字。项目目录若被沙箱/安全软件打上 Low 完整性标签就会这样（TCP/UDP 不受影响）。
检查：

```bat
icacls .
:: 若输出含 Mandatory Label\Low Mandatory Level 即异常，正常应为 Medium
```

恢复为默认的 Medium 后重启服务：

```bat
icacls . /setintegritylevel (OI)(CI)Medium /T
```

## 升级说明

旧版单文件实现（`network_monitor.py`、`network_monitor_v2.py`、`network_monitor_sse.py`）
已删除，入口统一为 `app.py`（或 `start.bat`）。如果还有旧版 `monitor_data.json` 备份，
放到项目根目录或用 `--legacy-logs` 指定，启动时会自动导入其中的断网事件；导入完成后
该文件即可删除。
