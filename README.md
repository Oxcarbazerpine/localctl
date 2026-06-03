# localctl

A daemon-less CLI to start / stop / inspect local apps — Node, Python, anything that runs from a command line.

## 设计要点

- **没有后台守护进程**。CLI 每次调用都是一次性进程，子进程用 detached 模式 spawn 后脱离父进程独立运行。
- **状态写在文件里**：`~/.localctl/<app>/pid.json` 记录 PID、启动时间、create_time（防 PID 复用误判）、实际端口。后续 `status` / `stop` 读这些文件，用 `psutil` 验活并杀整条进程树。
- **端口自动调节**：首选端口被占时按范围自动选号，把实际端口经 env / argv 占位符注入子进程。
- **日志写文件**：`stdout.log` / `stderr.log` 自动按大小滚动（默认 5MB → `.log.old`，单级）。

## 安装

```powershell
pip install -e .
```

装完后 `localctl.exe` 在 `<python-scripts-dir>`，把那个目录加进用户 PATH 就能任意位置调用：
```powershell
[Environment]::SetEnvironmentVariable(
    'PATH',
    [Environment]::GetEnvironmentVariable('PATH','User') + ';' + "$env:APPDATA\Python\Python313\Scripts",
    'User')
```

## 配置

唯一配置文件：`~/.localctl/apps.yaml`（可用 `-c <path>` 临时覆盖）。

```yaml
apps:
  - name: my-api                       # 必填，[A-Za-z0-9_.-]
    cwd:  E:\Projects\my-api           # 必填，工作目录
    cmd:  [node, server.js]            # 必填，argv 列表
    env:                               # 可选，注入子进程的环境变量
      NODE_ENV: development
    health:                            # 可选
      port: 3000                       # 监听端口
      auto: true                       # 被占时自动换号
      range: [3000, 3050]              # 候选范围；默认 [port, port+50]
      env: PORT                        # 把选中的端口写进哪个 env 变量
      placeholder: ${PORT}             # 或：在 cmd argv 里替换这个 token
      http: /healthz                   # 可选：HTTP 探活路径，比 TCP 探活更严格
```

启动配置错误会显示带路径的友好报错（如 `apps[2].health.range: lo > hi`）。

## 用法

### 列表与编号

```cmd
> localctl list
 1  envision-web     python -m envision.main web        (E:\Projects\EnVision)
 2  envision-worker  python -m envision.main worker     (E:\Projects\EnVision)
 3  dash-server      npm.cmd run dev                    (E:\Projects\PersonalDashboard\server)
 ...
```

所有命令的应用参数都接受 **编号 / 名称 / `all`**，可混搭：

```cmd
localctl start 1                  # 等同 start envision-web
localctl stop 1 3 dash-web        # 混搭
localctl status                   # 不带参数 = 看全部
localctl restart all
```

### 核心命令

| 命令 | 说明 |
|---|---|
| `localctl list` | 带编号列出所有应用 |
| `localctl start <id>... [--wait -t 30]` | 启动；`--wait` 阻塞到端口/HTTP 真就绪 |
| `localctl stop <id>...` | SIGTERM 整条进程链，10s 未退则 SIGKILL |
| `localctl restart <id>... [--wait]` | stop + start |
| `localctl status [<id>...]` | 表格输出；`--watch` 周期刷新；`--json` 给脚本 |
| `localctl logs <id> [-f] [--stderr] [--clear]` | 看输出、跟踪、清空 |
| `localctl reclaim <id\|name\|:port>` | 强制释放端口（杀占用进程） |
| `localctl edit` | 用 `$EDITOR` (默认 `notepad`) 打开 apps.yaml，关闭后自动校验 |
| `localctl help [command]` | 帮助；任何命令 `--help` 也行 |

### `reclaim` 的三种 TARGET 形式

```cmd
localctl reclaim 5             # 应用 #5 的 health.port
localctl reclaim envision-web  # 同样按应用名取端口
localctl reclaim :8000         # 显式端口号（冒号前缀）
```

冒号是为了消除"5 是编号还是端口"的歧义。

### 端口自动调节

`health.auto: true` 且首选端口被占时：
1. 在 `range` 内找第一个空闲端口
2. 通过 `health.env` / `health.placeholder` 注入到子进程
3. `status` NOTE 列显示 `reassigned from <preferred>`
4. `start` 输出：`port: 8000 -> 8001 [held by pid=27588 (python.exe)]`

如果不想换号、想直接赶走占用者：`localctl reclaim :<port>` 或 `localctl reclaim <id>`。

### HTTP 探活

`health.http: /` 启用后，`status` 会对实际端口发 GET，2xx/3xx 视为就绪：

```
NAME            STATE    PID    PORT
envision-web    running  44544  8000 HTTP    ← 真正能响应
envision-web    running  44544  8000 HTTPx   ← 端口听了但 HTTP 失败
```

`start --wait` 也会优先用 HTTP 探活作为"就绪"判据。没配 `http` 时退回 TCP-connect 探活，显示 `OK` / `DOWN`。

## 状态目录

```
~/.localctl/
├── apps.yaml                 # 唯一配置
└── <app>/
    ├── pid.json              # {pid, started_at, cmd, port, create_time}
    ├── stdout.log
    ├── stderr.log
    ├── stdout.log.old        # 滚动后
    └── stderr.log.old
```

环境变量覆盖：
- `LOCALCTL_CONFIG=<path>` — 用别的配置文件
- `LOCALCTL_STATE=<dir>` — 用别的状态目录
- `LOCALCTL_LOG_MAX_BYTES=<n>` — 调整日志滚动阈值（默认 5MB）

## 登录自启动（Windows）

`autostart.bat` 配合 Windows Task Scheduler 实现开机/登录自启：

```powershell
# 注册任务（在 LocalStarter 目录下）
$action = New-ScheduledTaskAction -Execute "$PWD\autostart.bat" -WorkingDirectory "$PWD"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName localctl-autostart -Action $action -Trigger $trigger -Principal $principal -Settings $settings
```

失败信号有两层：
- `autostart.log` —— 总是写，包含每次 start 的输出
- `autostart-failures.log` —— 仅失败时追加一行时间戳，无需任何权限
- Windows Application 事件日志（来源 `localctl-autostart`）—— 需要先用 admin 运行一次 `register-eventsource.ps1` 注册事件源；之后无需 admin

## 实现要点 / 已踩过的坑

- **PID 复用**：状态里同时存 `create_time`。`_verify_proc` 比对 OS 报告的 create_time 与记录值，差异 > 1s 视为已死（即便 PID 还在，只是被回收给别的进程了）。
- **Stop 的 race**：先 snapshot 子进程 → SIGTERM 全部 → `wait_procs` → **再 snapshot 一次**（SIGTERM 触发期间可能 fork 新孙子）→ SIGKILL 残留。
- **Windows `SO_REUSEADDR` 假阳性**：纯 `bind()` 测试在 Windows 上对带 `SO_REUSEADDR` 的占用者会成功（误判端口空闲）。`_port_free` 先查 `psutil.net_connections` 拿 LISTEN 状态，bind 时加 `SO_EXCLUSIVEADDRUSE`。
- **Vite/Node 默认绑 IPv6**：Windows 上 `localhost` 优先解析 `::1`，Vite 只监听 v6。`_port_listening` 同时试 v4 和 v6；`_port_owner` 用 `kind="inet"` 涵盖 v4+v6。
- **GBK 中文乱码**：cli.py 模块加载时把 `sys.stdout/stderr` reconfigure 成 UTF-8，否则中文 Windows console（GBK）会乱码。Click 帮助里要保留换行得用 `\b` 标记。
- **Node 项目首启慢**：tsx watch / Vite 编译 5-15s。`start` 立刻返回时显示 DOWN 是正常的，用 `--wait` 阻塞到真就绪。
- **`npm.cmd` 而非 `npm`**：Windows 上 npm 是 `.cmd` 脚本，Python `subprocess.Popen` 直接拿 `npm` 找不到，配置里必须写 `npm.cmd`。

## 故意没做的事

- **崩溃自动重启**：会破坏"无 daemon"前提。需要的话，注册一个每 5 分钟跑 `localctl start all` 的计划任务即可（`start` 对已运行应用是 no-op）。
- **应用依赖排序**：当前 `start all` 按 YAML 顺序串起，没拓扑排序。需要时再加。
- **Shell tab 补全**：去掉了。`localctl list` 给的编号已经足够短，比补全应用名更快。
