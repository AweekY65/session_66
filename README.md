# leasesim — 完全本地的 Lease 分布式锁模拟器

纯 Go 实现，无任何外部依赖。锁状态、租约、fencing token 与操作日志只保存在
**内存或本地文件**中，不使用 Redis / etcd / ZooKeeper 或任何网络服务。
多客户端通过 goroutine 模拟（`lease.Client`），共享同一个本地 `lease.Server`。

## 目录结构

- `clock/` — 可注入时钟：`Real`（墙钟）与 `Manual`（测试用手动推进）
- `lease/` — 锁服务器（acquire / renew / release）、本地持久化存储、模拟客户端
- `fenced/` — 带 fencing token 校验的下游 KV 资源，用于识别过期持有者
- `cmd/leasesim/` — 可运行演示（真实时钟 + 本地文件持久化）

## Lease 状态机

每个资源同一时刻至多存在一条**有效**租约，状态转换如下：

```
            acquire 成功               release 成功
  (无租约) ──────────────► HELD(有效) ──────────────► (无租约)
     ▲                        │
     │                        │ now >= expires_at（惰性判定）
     │                        ▼
     └────── acquire 成功 ── EXPIRED(过期)
```

- **HELD**：仅持有者可 `renew`（延长 `expires_at = now + ttl`）或 `release`；
  其他客户端 `acquire` 失败。
- **EXPIRED**：过期不做主动回收，而是在每次操作时以注入时钟惰性判定；
  过期租约**不能续期、不能释放**，任何客户端可重新 `acquire`。
- 过期判定严格使用 `now < expires_at` 表示有效。

## Fencing token 原理

- 服务器维护一个**持久化的全局单调计数器** `last_token`，每次 `acquire`
  成功时 `+1` 并随租约一起下发，保证 token 严格递增（重启后也不会回退）。
- 下游资源（`fenced.KV`）记录已接受的最大 token，拒绝任何
  `token <= maxToken` 的写入。
- 因此：旧持有者暂停超过租约、锁被新持有者拿走（获得更大 token）后，
  旧持有者恢复时的 `renew`/`release` 会被锁服务器拒绝，其携带旧 token 的
  写操作也会被下游资源 fencing 掉——双重防护。

## 时间模型

- 所有时间判断都通过 `clock.Clock` 接口注入，服务器内部**没有任何定时器
  或 goroutine 后台过期任务**，过期完全靠操作时惰性检查。
- 测试使用 `clock.Manual`，通过 `Advance(d)` 推进时间，**不依赖真实 sleep**，
  因此“客户端暂停超过租约”等场景是确定性的。
- 演示程序 `cmd/leasesim` 使用 `clock.Real`。

## 持久化与重启恢复

`lease.FileStore` 将状态写入本地目录：

- `state.json` — `last_token` 与所有租约（原子写：临时文件 + rename）
- `oplog.jsonl` — 每次 acquire/renew/release 的操作日志（成功与失败均记录）

重启时 `NewServer` 重新加载状态：**加载时刻已过期的租约直接丢弃并记日志**，
不会错误复活；仍有效的租约继续生效；token 计数器从持久化值继续递增。

## 运行测试

```sh
go test ./...          # 全部测试
go test ./lease -v     # 查看各场景明细
```

覆盖场景：竞争获取（32 goroutine 仅 1 个获胜）、续约延长、租约超时后
不可续期、旧客户端暂停恢复后被 fencing、token 单调递增、释放后重获、
重启后有效租约保留 / 过期租约丢弃且 token 不回退。

## 运行演示

```sh
go run ./cmd/leasesim
```

演示 alice 获取锁后“暂停”超过 TTL，bob 接管租约，alice 恢复后的续期与
旧 token 写入均被拒绝，最后模拟进程重启并打印恢复出的状态。
