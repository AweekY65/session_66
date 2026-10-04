# local-supervisor

一个纯本地进程 Supervisor：根据本地 TOML 配置启动、停止和守护多个子进程。
所有服务配置、PID 状态、重启记录、日志和运行元数据只保存在**本地文件或内存**中，
不依赖 systemd、Docker、Kubernetes、数据库或任何外部服务。仅使用 Python 标准库（>= 3.11）。

## 快速开始

```bash
python3 -m supervisor run examples/supervisor.toml     # 前台运行（Ctrl-C 优雅退出）
python3 -m supervisor status examples/supervisor.toml  # 查看磁盘上的状态
python3 -m supervisor stop examples/supervisor.toml    # 向运行中的 supervisor 发 SIGTERM
python3 -m pytest                                      # 运行全部自动化测试
```

## 配置

```toml
[supervisor]
state_dir = ".sv"          # 状态、元数据与日志的根目录（相对配置文件所在目录）

[[service]]
name = "worker"
command = ["python3", "worker.py"]
restart = "on-failure"     # never | on-failure | always
max_restarts = 5           # 最大连续重启次数，超过后进入 failed
backoff_initial = 0.5      # 首次重启等待秒数
backoff_factor = 2.0       # 指数退避因子
backoff_max = 30.0         # 退避上限（秒）
backoff_reset_after = 10.0 # 稳定运行超过该时长后重置连续重启计数
stop_signal = "TERM"       # 优雅停止首先发送的信号
stop_timeout = 5.0         # 超过该时长仍未退出则 SIGKILL
log_max_bytes = 1048576    # 单个日志文件最大字节数
log_backups = 3            # 保留的轮转文件个数
```

## 状态机

每个服务是一个独立状态机，状态持久化到 `<state_dir>/<name>.state.json`（原子写入：临时文件 + rename）：

```
STOPPED -> STARTING -> RUNNING --+--> EXITED   正常退出且策略不重启
                                 +--> FAILED   非零退出(never/on-failure) 或超过 max_restarts
                                 +--> BACKOFF -> STARTING   按策略重启，先指数退避
                                 +--> STOPPING -> STOPPED   优雅停止（超时后 SIGKILL）
```

每次状态迁移都会记录：`pid`、`pgid`、`started_at`、`exited_at`、`exit_code`、
`consecutive_restarts` 以及完整的 `restarts` 历史（序号、时间、退避时长、上次退出码）。

## 重启策略与指数退避

- `never`：退出后不重启；`on-failure`：仅退出码非零时重启；`always`：总是重启。
- 第 n 次连续重启的等待时间为 `min(backoff_initial * backoff_factor^(n-1), backoff_max)`，
  频繁崩溃的进程不会形成无间隔重启循环。
- 进程稳定运行超过 `backoff_reset_after` 秒后，连续重启计数清零，退避重新开始。

## 信号处理与优雅关闭

- 子进程使用 `start_new_session=True` 放入独立进程组，停止时向**整个进程组**发送
  `stop_signal`（默认 SIGTERM），等待 `stop_timeout` 秒；仍未退出则发送 SIGKILL 强制结束。
- supervisor 自身收到 SIGINT/SIGTERM 时，对所有服务并行执行上述优雅停止后再退出。

## 日志策略

- 每个服务的 stdout/stderr 由独立线程捕获，加上 `[时间戳 流名]` 前缀写入
  `<state_dir>/logs/<name>.log`。
- 按大小轮转：`name.log` → `name.log.1` → …→ `name.log.N`，超出 `log_backups` 的最旧文件被删除。
- 所有写入经单把锁串行化，且**只在完整记录（整行）边界处轮转**——
  已写入的完整记录在轮转过程中不会被截断或丢失（轮转测试逐行校验了记录完整性）。

## 重启恢复（遗留状态识别）

supervisor 重启后会读取磁盘状态并与现实核对：

- 状态文件中的 PID 已不存在 → 标记为 `stopped`，**不会**误认为旧 PID 仍是受管进程。
- PID 仍存在但 `/proc/<pid>/stat` 的 starttime 与记录不一致（PID 被回收复用）→
  同样判定为失效，且**不会**去动那个无关进程。
- PID 存活且 starttime 一致 → 判定为遗留的受管子进程，重新接管（adopt）：
  恢复 `running` 状态、继续监控其存活，并可对其执行优雅停止。

## 目录结构

```
<state_dir>/
  supervisor.json        # supervisor 元数据（自身 pid、版本、启动时间）
  <name>.state.json      # 每个服务的持久化状态
  logs/<name>.log[.N]    # 轮转日志
```

## 测试

```bash
python3 -m pytest -v
```

测试程序位于 `tests/programs/`：`normal_exit.py`（正常退出）、`crash.py`（崩溃 /
崩溃 N 次后稳定）、`stay_up.py`（响应 SIGTERM）、`ignore_term.py`（忽略 SIGTERM，
只能 SIGKILL）、`log_spam.py`（大量 stdout/stderr 日志）。覆盖：三种重启策略、
指数退避及上限、最大连续重启次数、优雅停止、超时强制杀死、stdout/stderr 捕获、
日志轮转完整性与备份上限、失效 PID / PID 复用识别、遗留进程接管。全部测试在终端内完成。
