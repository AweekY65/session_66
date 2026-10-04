//! The supervisor: owns all services, persists state, and reconciles
//! leftover state from a previous run.

use crate::config::Config;
use crate::logger::RotatingLogger;
use crate::service::Service;
use crate::state::{ServiceState, ServiceStatus, StateStore};
use crate::sys;
use std::io;
use std::sync::{Arc, Mutex};

pub struct Supervisor {
    pub config: Config,
    state: Arc<Mutex<StateStore>>,
    services: Vec<Service>,
}

impl Supervisor {
    /// Create a supervisor: prepare directories, load any previous state,
    /// and reconcile it so stale PIDs from a crashed/killed previous
    /// instance are never mistaken for live managed processes.
    pub fn new(config: Config) -> io::Result<Supervisor> {
        std::fs::create_dir_all(&config.state_dir)?;
        std::fs::create_dir_all(&config.log_dir)?;

        let mut store = StateStore::load(&config.state_dir);
        store.supervisor_pid = std::process::id();
        reconcile(&mut store);
        for cfg in &config.services {
            store
                .services
                .entry(cfg.name.clone())
                .or_insert_with(|| ServiceState::new(&cfg.name));
        }
        store.save()?;

        let state = Arc::new(Mutex::new(store));
        let mut services = Vec::new();
        for cfg in &config.services {
            let logger = RotatingLogger::new(
                &config.log_dir,
                &cfg.name,
                cfg.log_max_bytes,
                cfg.log_keep,
            )?;
            services.push(Service::new(cfg.clone(), Arc::clone(&state), logger));
        }
        Ok(Supervisor { config, state, services })
    }

    pub fn start_all(&self) {
        for s in &self.services {
            s.start();
        }
    }

    /// Gracefully stop every service and wait for all monitor threads.
    pub fn stop_all(&self) {
        for s in &self.services {
            s.request_stop();
        }
        for s in &self.services {
            s.join();
        }
    }

    pub fn status(&self) -> Vec<ServiceState> {
        let store = self.state.lock().unwrap();
        store.services.values().cloned().collect()
    }

    pub fn state_store(&self) -> Arc<Mutex<StateStore>> {
        Arc::clone(&self.state)
    }
}

/// Mark leftover "active" entries from a previous supervisor run:
/// - PID dead, or PID reused by an unrelated process -> `exited` (stale)
/// - PID alive and matches the recorded start time -> `orphaned`
///   (still running, but not a child of this supervisor, so unmanaged)
fn reconcile(store: &mut StateStore) {
    for st in store.services.values_mut() {
        if !st.status.is_active() {
            continue;
        }
        let still_ours = match (st.pid, st.proc_start_time) {
            (Some(pid), Some(start)) => {
                sys::process_alive(pid) && sys::proc_start_time(pid) == Some(start)
            }
            _ => false,
        };
        match (st.pid, still_ours) {
            (Some(pid), true) => {
                st.status = ServiceStatus::Orphaned;
                st.note = Some(format!(
                    "pid {} still alive but belongs to a previous supervisor instance",
                    pid
                ));
            }
            _ => {
                st.status = ServiceStatus::Exited;
                st.note = Some("stale state: process no longer exists".to_string());
                st.pid = None;
                st.proc_start_time = None;
            }
        }
    }
}
