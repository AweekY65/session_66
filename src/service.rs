//! Per-service runtime: spawn, log pumping, restart policy with
//! exponential backoff, and graceful shutdown.

use crate::config::{RestartPolicy, ServiceConfig};
use crate::logger::RotatingLogger;
use crate::state::{RestartRecord, ServiceState, ServiceStatus, StateStore};
use crate::sys;
use std::io::{BufRead, BufReader, Read};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};
#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;

const POLL_INTERVAL: Duration = Duration::from_millis(20);

pub struct Service {
    pub cfg: ServiceConfig,
    inner: Arc<ServiceInner>,
}

struct ServiceInner {
    cfg: ServiceConfig,
    state: Arc<Mutex<StateStore>>,
    logger: Arc<RotatingLogger>,
    shared: Mutex<Shared>,
    handle: Mutex<Option<JoinHandle<()>>>,
}

#[derive(Default)]
struct Shared {
    stop_requested: bool,
    child_pid: Option<u32>,
}

impl Service {
    pub fn new(
        cfg: ServiceConfig,
        state: Arc<Mutex<StateStore>>,
        logger: RotatingLogger,
    ) -> Service {
        let inner = Arc::new(ServiceInner {
            cfg: cfg.clone(),
            state,
            logger: Arc::new(logger),
            shared: Mutex::new(Shared::default()),
            handle: Mutex::new(None),
        });
        Service { cfg, inner }
    }

    /// Launch the monitor thread (idempotent).
    pub fn start(&self) {
        let mut handle = self.inner.handle.lock().unwrap();
        if handle.is_some() {
            return;
        }
        let inner = Arc::clone(&self.inner);
        *handle = Some(std::thread::spawn(move || monitor_loop(&inner)));
    }

    /// Ask the service to stop. The monitor thread performs the graceful
    /// shutdown sequence (SIGTERM, wait, SIGKILL).
    pub fn request_stop(&self) {
        self.inner.shared.lock().unwrap().stop_requested = true;
    }

    /// Block until the monitor thread finishes.
    pub fn join(&self) {
        let handle = self.inner.handle.lock().unwrap().take();
        if let Some(h) = handle {
            let _ = h.join();
        }
    }
}

fn is_stop_requested(inner: &ServiceInner) -> bool {
    inner.shared.lock().unwrap().stop_requested
}

fn update_state<F: FnOnce(&mut ServiceState)>(inner: &ServiceInner, f: F) {
    let mut store = match inner.state.lock() {
        Ok(g) => g,
        Err(e) => e.into_inner(),
    };
    let entry = store
        .services
        .entry(inner.cfg.name.clone())
        .or_insert_with(|| ServiceState::new(&inner.cfg.name));
    f(entry);
    let _ = store.save();
}

fn set_status(inner: &ServiceInner, status: ServiceStatus) {
    update_state(inner, |st| st.status = status);
}

fn monitor_loop(inner: &ServiceInner) {
    let cfg = &inner.cfg;
    let mut consecutive: u32 = 0;
    // A run longer than this resets the consecutive-restart counter.
    let stable_after = Duration::from_millis((cfg.backoff_initial_ms * 10).max(1_000));

    loop {
        if is_stop_requested(inner) {
            set_status(inner, ServiceStatus::Stopped);
            return;
        }
        set_status(inner, ServiceStatus::Starting);

        let mut child = match spawn_child(cfg) {
            Ok(c) => c,
            Err(e) => {
                update_state(inner, |st| {
                    st.status = ServiceStatus::Failed;
                    st.note = Some(format!("spawn failed: {}", e));
                });
                return;
            }
        };

        let pid = child.id();
        let start_ms = sys::now_ms();
        let proc_start = sys::proc_start_time(pid);
        inner.shared.lock().unwrap().child_pid = Some(pid);
        update_state(inner, |st| {
            st.status = ServiceStatus::Running;
            st.pid = Some(pid);
            st.proc_start_time = proc_start;
            st.last_start_ms = Some(start_ms);
            st.note = None;
        });

        let pumps = start_pumps(inner, &mut child);
        let began = Instant::now();

        let exit = wait_or_stop(inner, &mut child);
        for p in pumps {
            let _ = p.join();
        }
        inner.shared.lock().unwrap().child_pid = None;

        let exit_code = exit_code_of(&exit);
        let runtime = began.elapsed();
        update_state(inner, |st| {
            st.exit_count += 1;
            st.last_exit_code = Some(exit_code);
            st.pid = None;
            st.proc_start_time = None;
        });

        if is_stop_requested(inner) {
            set_status(inner, ServiceStatus::Stopped);
            return;
        }
        if runtime >= stable_after {
            consecutive = 0;
        }

        let should_restart = match cfg.restart {
            RestartPolicy::Never => false,
            RestartPolicy::OnFailure => exit_code != 0,
            RestartPolicy::Always => true,
        };
        if !should_restart {
            set_status(
                inner,
                if exit_code == 0 { ServiceStatus::Exited } else { ServiceStatus::Failed },
            );
            return;
        }
        if consecutive >= cfg.max_restarts {
            update_state(inner, |st| {
                st.status = ServiceStatus::Failed;
                st.note = Some(format!(
                    "gave up after {} consecutive restarts",
                    consecutive
                ));
            });
            return;
        }

        // Exponential backoff: initial * 2^consecutive, capped at max.
        let shift = consecutive.min(20);
        let delay_ms = cfg
            .backoff_initial_ms
            .saturating_mul(1u64 << shift)
            .min(cfg.backoff_max_ms);
        consecutive += 1;
        update_state(inner, |st| {
            st.status = ServiceStatus::Backoff;
            st.consecutive_restarts = consecutive;
            st.total_restarts += 1;
            st.push_restart_record(RestartRecord { at_ms: sys::now_ms(), exit_code });
        });

        // Interruptible backoff sleep.
        let deadline = Instant::now() + Duration::from_millis(delay_ms);
        while Instant::now() < deadline {
            if is_stop_requested(inner) {
                break;
            }
            std::thread::sleep(POLL_INTERVAL);
        }
    }
}

fn spawn_child(cfg: &ServiceConfig) -> std::io::Result<Child> {
    Command::new(&cfg.command)
        .args(&cfg.args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

fn start_pumps(inner: &ServiceInner, child: &mut Child) -> Vec<JoinHandle<()>> {
    let mut pumps = Vec::new();
    if let Some(out) = child.stdout.take() {
        pumps.push(pump(out, "stdout", Arc::clone(&inner.logger)));
    }
    if let Some(err) = child.stderr.take() {
        pumps.push(pump(err, "stderr", Arc::clone(&inner.logger)));
    }
    pumps
}

fn pump<R: Read + Send + 'static>(
    reader: R,
    stream: &'static str,
    logger: Arc<RotatingLogger>,
) -> JoinHandle<()> {
    std::thread::spawn(move || {
        let reader = BufReader::new(reader);
        for line in reader.lines() {
            match line {
                Ok(l) => logger.log(stream, &l),
                Err(_) => break,
            }
        }
    })
}

/// Wait for the child; if a stop is requested, run the graceful shutdown
/// sequence: SIGTERM, wait up to stop_timeout, then SIGKILL.
fn wait_or_stop(inner: &ServiceInner, child: &mut Child) -> ExitStatus {
    loop {
        if let Ok(Some(status)) = child.try_wait() {
            return status;
        }
        if is_stop_requested(inner) {
            set_status(inner, ServiceStatus::Stopping);
            let pid = child.id();
            let _ = sys::send_signal(pid, sys::SIGTERM);
            let deadline =
                Instant::now() + Duration::from_millis(inner.cfg.stop_timeout_ms);
            loop {
                if let Ok(Some(status)) = child.try_wait() {
                    return status;
                }
                if Instant::now() >= deadline {
                    let _ = sys::send_signal(pid, sys::SIGKILL);
                    return child
                        .wait()
                        .unwrap_or_else(|_| ExitStatus::from_raw(0));
                }
                std::thread::sleep(POLL_INTERVAL);
            }
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

fn exit_code_of(status: &ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        if let Some(sig) = status.signal() {
            return 128 + sig;
        }
    }
    -1
}
