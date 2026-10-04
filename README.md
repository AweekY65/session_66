# supervisor

一个纯本地进程 Supervisor：根据本地 JSON 配置启动、停止并监控多个子进程。
所有状态（配置、PID、重启记录、日志、运行元数据）只保存在**本地文件或内存**中，
不依赖 systemd、Docker、Kubernetes、数据库或任何外部服务。仅使用 Rust 标准库
（信号通过 `extern "C"` 直接调用 libc 符号），零第三方依赖。

## 构建与测试

```sh
cargo build          # 构建 supervisor 与 testprog
cargo test           # 运行全部单元测试与集成测试（所有测试都有超时，必然终止）
cargo run -- run --config supervisor.example.json   # 前台运行
cargo run -- status --config supervisor.example.json
cargo run -- stop --config supervisor.example.json
```

## 配置

配置文件为本地 JSON（见 `supervisor.example.json`）：

```json
{
  "state_dir": ".svstate",
  "log_dir": ".svstate/logs",
  "services": [
    {
      "name": "worker",
      "command": "/path/to/program",
      "args": ["--flag"],
      "restart": "on-failure",
      "max_restarts": 3,
      "backoff_initial_ms": 500,
      "backoff_max_ms": 30000,
      "stop_timeout_ms": 5000,
      "log_max_bytes": 10485760,
      "log_keep": 5
    }
  ]
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `restart` | `never` | `never` / `on-failure` / `always` |
| `max_restarts` | `3` | 最大**连续**重启次数，超过则放弃并置为 `failed` |
| `backoff_initial_ms` | `500` | 指数退避初始间隔 |
| `backoff_max_ms` | `30000` | 退避间隔上限 |
| `stop_timeout_ms` | `5000` | SIGTERM 后到 SIGKILL 的宽限时间 |
| `log_max_bytes` | `10485760` | 单个日志文件最大字节数（0 = 不轮转） |
| `log_keep` | `5` | 保留的轮转文件个数 |

## 状态机

每个服务由一个 monitor 线程驱动，状态持久化到 `state_dir/state.json`
（先写临时文件再原子 rename）：

```
                spawn
Stopped ───────────────► Starting ──► Running ──► (进程退出)
  ▲                          │                      │
  │ stop_all                 │ spawn 失败           ▼
  │                          ▼                记录退出码/退出时间
  │                        Failed                   │
  │                                             按策略判断
  │                          ┌──────────────────────┤
  │                          │                      │
  │                     不重启/达到上限           需要重启
  │                          │                      │
  │                     Exited/Failed               ▼
  │                                          Backoff（可中断睡眠
  │                                             initial*2^n 封顶 max）
  │                                               │ 回到 Starting
  └────────────── stop 请求（任意状态）◄──────────┘
```

- 退出码按 Unix 惯例记录：正常退出为退出码，被信号杀死为 `128 + 信号`。
- 进程稳定运行超过 `max(10 × backoff_initial, 1s)` 后，连续重启计数清零。
- 每次重启追加一条 `{at_ms, exit_code}` 到 `restart_history`（保留最近 100 条）。

## 信号处理

- **停止服务**：monitor 线程先向子进程发送 `SIGTERM`，在 `stop_timeout_ms`
  内轮询等待；超时后发送 `SIGKILL` 强制结束并 reap。
- **Supervisor 自身**：`run` 命令安装 `SIGTERM`/`SIGINT` 处理器，置位后
  对所有服务执行上述优雅停止流程再退出。`supervisor stop` 通过读取
  `state.json` 中的 `supervisor_pid` 向其发送 `SIGTERM`。
- **遗留状态识别**：状态文件同时记录 PID 和 `/proc/<pid>/stat` 的进程启动
  时间（starttime）。Supervisor 重启后逐项核对：PID 已不存在或 starttime
  不匹配（PID 被复用）→ 标记为 `exited`（stale）；进程仍存活且 starttime
  匹配 → 标记为 `orphaned`（不是本实例的子进程，无法 wait，不接管）。
  因此绝不会把已退出的旧 PID 误认为受管进程。

## 日志策略

- 子进程的 stdout/stderr 各由一个 pump 线程按行读取，加上
  `[UTC 时间戳] [stdout|stderr]` 前缀写入 `log_dir/<service>.log`。
- **按大小轮转**：写入前检查，若当前文件非空且写入后将超过
  `log_max_bytes`，则先轮转：`name.log.(k-1) → name.log.k`，
  `name.log → name.log.1`，再开新文件。轮转只发生在**记录边界**，
  已写入的完整记录不会被拆分或丢失；超出 `log_keep` 的最旧文件被删除。
- 日志写入不加应用层缓冲（直接 `write_all` 到底层文件），崩溃时已写
  记录不会滞留在内存缓冲区。

## 测试

```sh
cargo test
```

测试程序 `src/bin/testprog.rs` 模拟四类被管进程：

- `exit CODE [DELAY]`：正常退出 / 崩溃（非零退出码）
- `run`：常驻，收到 SIGTERM 后优雅退出 0
- `ignore-term`：忽略 SIGTERM（验证 SIGKILL 强制关闭路径）
- `spam LINES SIZE`：大量固定宽度日志（验证轮转不丢记录）

集成测试（`tests/integration.rs`）覆盖：三种重启策略、指数退避间隔递增、
优雅停止、超时强杀、日志轮转完整性、stale PID 识别、orphan 检测，以及
CLI 端到端（`run` + SIGTERM 干净退出）。所有测试均带显式超时，必然终止。
