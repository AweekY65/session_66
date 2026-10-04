//! Integration tests for the supervisor. Every test is bounded by an
//! explicit timeout so the suite always terminates.

use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use supervisor::state::{ServiceState, ServiceStatus, StateStore};
use supervisor::{Config, RestartPolicy, ServiceConfig, Supervisor};

const TESTPROG: &str = env!("CARGO_BIN_EXE_testprog");
const SUPERVISOR: &str = env!("CARGO_BIN_EXE_supervisor");

static COUNTER: AtomicU64 = AtomicU64::new(0);

fn test_dir(name: &str) -> PathBuf {
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = std::env::temp_dir().join(format!("svtest-{}-{}-{}", std::process::id(), name, n));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn svc(name: &str, args: &[&str]) -> ServiceConfig {
    let mut cfg = ServiceConfig::new(name, TESTPROG);
    cfg.args = args.iter().map(|s| s.to_string()).collect();
    cfg
}

fn make_supervisor(name: &str, services: Vec<ServiceConfig>) -> (Supervisor, PathBuf) {
    let dir = test_dir(name);
    let config = Config {
        state_dir: dir.join("state"),
        log_dir: dir.join("logs"),
        services,
    };
    (Supervisor::new(config).unwrap(), dir)
}

fn get(sv: &Supervisor, name: &str) -> ServiceState {
    sv.status()
        .into_iter()
        .find(|s| s.name == name)
        .unwrap_or_else(|| panic!("service {} not found", name))
}

/// Poll `pred` until it returns true or the timeout elapses.
fn wait_until<F: FnMut() -> bool>(timeout: Duration, mut pred: F) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if pred() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(25));
    }
    false
}

#[test]
fn never_policy_does_not_restart() {
    let mut c = svc("never", &["exit", "1"]);
    c.restart = RestartPolicy::Never;
    let (sv, _dir) = make_supervisor("never", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(10), || {
        get(&sv, "never").status == ServiceStatus::Failed
    });
    assert!(ok, "service should reach failed state");
    let st = get(&sv, "never");
    assert_eq!(st.exit_count, 1);
    assert_eq!(st.total_restarts, 0);
    assert_eq!(st.last_exit_code, Some(1));
    assert!(st.last_start_ms.is_some());
    sv.stop_all();
}

#[test]
fn on_failure_restarts_up_to_max_then_gives_up() {
    let mut c = svc("crashy", &["exit", "1"]);
    c.restart = RestartPolicy::OnFailure;
    c.max_restarts = 2;
    c.backoff_initial_ms = 50;
    c.backoff_max_ms = 100;
    let (sv, _dir) = make_supervisor("onfail", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(10), || {
        get(&sv, "crashy").status == ServiceStatus::Failed
    });
    assert!(ok, "service should give up");
    let st = get(&sv, "crashy");
    assert_eq!(st.exit_count, 3, "1 initial run + 2 restarts");
    assert_eq!(st.total_restarts, 2);
    assert_eq!(st.restart_history.len(), 2);
    assert!(st.restart_history.iter().all(|r| r.exit_code == 1));
    sv.stop_all();
}

#[test]
fn on_failure_does_not_restart_on_success() {
    let mut c = svc("clean", &["exit", "0"]);
    c.restart = RestartPolicy::OnFailure;
    c.max_restarts = 3;
    c.backoff_initial_ms = 20;
    let (sv, _dir) = make_supervisor("onfail-ok", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(10), || {
        get(&sv, "clean").status == ServiceStatus::Exited
    });
    assert!(ok);
    let st = get(&sv, "clean");
    assert_eq!(st.exit_count, 1);
    assert_eq!(st.total_restarts, 0);
    assert_eq!(st.last_exit_code, Some(0));
    sv.stop_all();
}

#[test]
fn always_policy_restarts_even_on_success() {
    let mut c = svc("always", &["exit", "0"]);
    c.restart = RestartPolicy::Always;
    c.max_restarts = 2;
    c.backoff_initial_ms = 30;
    c.backoff_max_ms = 60;
    let (sv, _dir) = make_supervisor("always", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(10), || {
        let st = get(&sv, "always");
        st.status == ServiceStatus::Failed && st.exit_count == 3
    });
    assert!(ok, "always policy should restart successful exits too");
    sv.stop_all();
}

#[test]
fn backoff_delays_grow_and_prevent_tight_loops() {
    let mut c = svc("looper", &["exit", "1"]);
    c.restart = RestartPolicy::Always;
    c.max_restarts = 3;
    c.backoff_initial_ms = 100;
    c.backoff_max_ms = 400;
    let (sv, _dir) = make_supervisor("backoff", vec![c]);
    sv.start_all();

    let began = Instant::now();
    let ok = wait_until(Duration::from_secs(15), || {
        get(&sv, "looper").status == ServiceStatus::Failed
    });
    assert!(ok);
    let elapsed = began.elapsed();
    let st = get(&sv, "looper");
    assert_eq!(st.total_restarts, 3);

    // Backoff schedule: 100ms, 200ms, 400ms -> at least ~700ms total.
    assert!(
        elapsed >= Duration::from_millis(600),
        "restarts must be spaced by backoff, got {:?}",
        elapsed
    );
    // And the recorded restart timestamps must show growing gaps.
    let times: Vec<u64> = st.restart_history.iter().map(|r| r.at_ms).collect();
    assert_eq!(times.len(), 3);
    let gap1 = times[1] - times[0];
    let gap2 = times[2] - times[1];
    assert!(gap1 >= 80, "first gap too small: {}ms", gap1);
    assert!(gap2 >= gap1 + 80, "gaps must grow: {} -> {}", gap1, gap2);
    sv.stop_all();
}

#[test]
fn graceful_shutdown_uses_sigterm_first() {
    let mut c = svc("graceful", &["run"]);
    c.restart = RestartPolicy::Never;
    c.stop_timeout_ms = 3_000;
    let (sv, _dir) = make_supervisor("graceful", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(10), || {
        get(&sv, "graceful").status == ServiceStatus::Running
    });
    assert!(ok);
    let pid = get(&sv, "graceful").pid.expect("running service has a pid");

    sv.stop_all();
    let st = get(&sv, "graceful");
    assert_eq!(st.status, ServiceStatus::Stopped);
    assert_eq!(st.last_exit_code, Some(0), "testprog exits 0 on SIGTERM");
    assert!(!supervisor::sys::process_alive(pid), "child must be gone");
}

#[test]
fn force_kill_after_stop_timeout() {
    let mut c = svc("stubborn", &["ignore-term"]);
    c.restart = RestartPolicy::Never;
    c.stop_timeout_ms = 300;
    let (sv, _dir) = make_supervisor("forcekill", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(10), || {
        get(&sv, "stubborn").status == ServiceStatus::Running
    });
    assert!(ok);
    let pid = get(&sv, "stubborn").pid.unwrap();

    let began = Instant::now();
    sv.stop_all();
    let elapsed = began.elapsed();

    let st = get(&sv, "stubborn");
    assert_eq!(st.status, ServiceStatus::Stopped);
    assert_eq!(
        st.last_exit_code,
        Some(128 + 9),
        "process ignoring SIGTERM must be SIGKILLed"
    );
    assert!(
        elapsed >= Duration::from_millis(280) && elapsed < Duration::from_secs(5),
        "grace period should be honoured then escalated, took {:?}",
        elapsed
    );
    assert!(!supervisor::sys::process_alive(pid));
}

#[test]
fn log_rotation_preserves_complete_records() {
    const LINES: usize = 2_000;
    const SIZE: usize = 120;
    let mut c = svc("spammer", &["spam", &LINES.to_string(), &SIZE.to_string()]);
    c.restart = RestartPolicy::Never;
    c.log_max_bytes = 20_000;
    c.log_keep = 50;
    let (sv, dir) = make_supervisor("rotation", vec![c]);
    sv.start_all();

    let ok = wait_until(Duration::from_secs(30), || {
        get(&sv, "spammer").status == ServiceStatus::Exited
    });
    assert!(ok);
    sv.stop_all();

    let files = supervisor::logger::existing_log_files(&dir.join("logs"), "spammer");
    assert!(files.len() >= 2, "rotation should produce multiple files");

    let mut stdout_records = 0;
    let mut stderr_records = 0;
    for f in &files {
        let text = std::fs::read_to_string(f).unwrap();
        for line in text.lines() {
            if let Some(pos) = line.find("] [stdout] ") {
                let payload = &line[pos + "] [stdout] ".len()..];
                assert_eq!(
                    payload.len(),
                    SIZE,
                    "every record must survive rotation complete: {:?}",
                    payload
                );
                assert!(payload.starts_with("line-"));
                stdout_records += 1;
            } else if line.contains("] [stderr] ") {
                stderr_records += 1;
            } else {
                panic!("corrupt log line: {:?}", line);
            }
        }
    }
    assert_eq!(stdout_records, LINES, "no written record may be lost");
    assert_eq!(stderr_records, LINES / 7 + 1);
}

#[test]
fn stale_pid_from_previous_run_is_not_resurrected() {
    let dir = test_dir("stale");
    let state_dir = dir.join("state");
    std::fs::create_dir_all(&state_dir).unwrap();

    // Create a genuinely dead PID: spawn a process and reap it.
    let mut child = Command::new("true").spawn().unwrap();
    let dead_pid = child.id();
    child.wait().unwrap();

    let mut store = StateStore::load(&state_dir);
    let mut ghost = ServiceState::new("ghost");
    ghost.status = ServiceStatus::Running;
    ghost.pid = Some(dead_pid);
    ghost.proc_start_time = Some(1); // deliberately wrong start time
    store.services.insert("ghost".into(), ghost);
    store.supervisor_pid = dead_pid;
    store.save().unwrap();

    let config = Config {
        state_dir: state_dir.clone(),
        log_dir: dir.join("logs"),
        services: vec![svc("ghost", &["exit", "0"])],
    };
    let sv = Supervisor::new(config).unwrap();
    let st = get(&sv, "ghost");
    assert_eq!(
        st.status,
        ServiceStatus::Exited,
        "dead leftover pid must be marked exited, not running"
    );
    assert_eq!(st.pid, None);
    assert!(st.note.unwrap().contains("stale"));
    sv.stop_all();
}

#[test]
fn live_orphan_is_detected_not_adopted() {
    // A still-running process recorded by a "previous" supervisor must be
    // marked orphaned, not running.
    let dir = test_dir("orphan");
    let state_dir = dir.join("state");
    std::fs::create_dir_all(&state_dir).unwrap();

    let mut child = Command::new(TESTPROG)
        .args(["ignore-term"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let pid = child.id();
    let start = supervisor::sys::proc_start_time(pid).unwrap();

    let mut store = StateStore::load(&state_dir);
    let mut st = ServiceState::new("orphan");
    st.status = ServiceStatus::Running;
    st.pid = Some(pid);
    st.proc_start_time = Some(start);
    store.services.insert("orphan".into(), st);
    store.save().unwrap();

    let config = Config {
        state_dir,
        log_dir: dir.join("logs"),
        services: vec![svc("orphan", &["exit", "0"])],
    };
    let sv = Supervisor::new(config).unwrap();
    let st = get(&sv, "orphan");
    assert_eq!(st.status, ServiceStatus::Orphaned);
    sv.stop_all();

    let _ = supervisor::sys::send_signal(pid, supervisor::sys::SIGKILL);
    let _ = child.wait();
}

#[test]
fn cli_run_stops_cleanly_on_sigterm() {
    let dir = test_dir("cli");
    let config_path = dir.join("supervisor.json");
    let config = format!(
        r#"{{
  "state_dir": "{0}",
  "log_dir": "{1}",
  "services": [
    {{"name": "worker", "command": "{2}", "args": ["run"],
      "restart": "never", "stop_timeout_ms": 2000}}
  ]
}}"#,
        dir.join("state").display(),
        dir.join("logs").display(),
        TESTPROG
    );
    std::fs::write(&config_path, config).unwrap();

    let mut sup = Command::new(SUPERVISOR)
        .args(["run", "--config", config_path.to_str().unwrap()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();

    let state_dir = dir.join("state");
    let ok = wait_until(Duration::from_secs(10), || {
        let store = StateStore::load(&state_dir);
        store
            .services
            .get("worker")
            .map(|s| s.status == ServiceStatus::Running)
            .unwrap_or(false)
    });
    assert!(ok, "worker should be running");

    supervisor::sys::send_signal(sup.id(), supervisor::sys::SIGTERM).unwrap();
    let exited = wait_until(Duration::from_secs(10), || matches!(sup.try_wait(), Ok(Some(_))));
    assert!(exited, "supervisor must exit after SIGTERM");
    let status = sup.wait().unwrap();
    assert!(status.success());

    let store = StateStore::load(&state_dir);
    let worker = store.services.get("worker").unwrap();
    assert_eq!(worker.status, ServiceStatus::Stopped);
    assert_eq!(worker.last_exit_code, Some(0));
}
