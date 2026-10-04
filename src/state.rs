//! Persistent run-time state, stored as JSON in the local state dir.

use crate::json::Value;
use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ServiceStatus {
    Stopped,
    Starting,
    Running,
    Stopping,
    Backoff,
    Exited,
    Failed,
    /// Process is still alive but belongs to a previous supervisor
    /// instance; it is not managed by this one.
    Orphaned,
}

impl ServiceStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ServiceStatus::Stopped => "stopped",
            ServiceStatus::Starting => "starting",
            ServiceStatus::Running => "running",
            ServiceStatus::Stopping => "stopping",
            ServiceStatus::Backoff => "backoff",
            ServiceStatus::Exited => "exited",
            ServiceStatus::Failed => "failed",
            ServiceStatus::Orphaned => "orphaned",
        }
    }

    pub fn parse(s: &str) -> ServiceStatus {
        match s {
            "stopped" => ServiceStatus::Stopped,
            "starting" => ServiceStatus::Starting,
            "running" => ServiceStatus::Running,
            "stopping" => ServiceStatus::Stopping,
            "backoff" => ServiceStatus::Backoff,
            "exited" => ServiceStatus::Exited,
            "failed" => ServiceStatus::Failed,
            "orphaned" => ServiceStatus::Orphaned,
            _ => ServiceStatus::Stopped,
        }
    }

    pub fn is_active(&self) -> bool {
        matches!(
            self,
            ServiceStatus::Starting | ServiceStatus::Running | ServiceStatus::Stopping | ServiceStatus::Backoff
        )
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct RestartRecord {
    pub at_ms: u64,
    pub exit_code: i32,
}

#[derive(Clone, Debug)]
pub struct ServiceState {
    pub name: String,
    pub status: ServiceStatus,
    pub pid: Option<u32>,
    /// /proc start-time tick of `pid`, used to detect PID reuse.
    pub proc_start_time: Option<u64>,
    pub last_start_ms: Option<u64>,
    pub last_exit_code: Option<i32>,
    pub consecutive_restarts: u32,
    pub total_restarts: u64,
    pub exit_count: u64,
    pub restart_history: Vec<RestartRecord>,
    pub note: Option<String>,
}

const MAX_RESTART_HISTORY: usize = 100;

impl ServiceState {
    pub fn new(name: &str) -> Self {
        ServiceState {
            name: name.to_string(),
            status: ServiceStatus::Stopped,
            pid: None,
            proc_start_time: None,
            last_start_ms: None,
            last_exit_code: None,
            consecutive_restarts: 0,
            total_restarts: 0,
            exit_count: 0,
            restart_history: Vec::new(),
            note: None,
        }
    }

    pub fn push_restart_record(&mut self, rec: RestartRecord) {
        self.restart_history.push(rec);
        if self.restart_history.len() > MAX_RESTART_HISTORY {
            let drop = self.restart_history.len() - MAX_RESTART_HISTORY;
            self.restart_history.drain(0..drop);
        }
    }

    pub fn to_json(&self) -> Value {
        let history: Vec<Value> = self
            .restart_history
            .iter()
            .map(|r| {
                Value::Obj(vec![
                    ("at_ms".into(), Value::num_u64(r.at_ms)),
                    ("exit_code".into(), Value::num_i64(r.exit_code as i64)),
                ])
            })
            .collect();
        Value::Obj(vec![
            ("name".into(), Value::str(&self.name)),
            ("status".into(), Value::str(self.status.as_str())),
            ("pid".into(), Value::opt_u64(self.pid.map(|p| p as u64))),
            ("proc_start_time".into(), Value::opt_u64(self.proc_start_time)),
            ("last_start_ms".into(), Value::opt_u64(self.last_start_ms)),
            ("last_exit_code".into(), Value::opt_i32(self.last_exit_code)),
            ("consecutive_restarts".into(), Value::num_u64(self.consecutive_restarts as u64)),
            ("total_restarts".into(), Value::num_u64(self.total_restarts)),
            ("exit_count".into(), Value::num_u64(self.exit_count)),
            ("restart_history".into(), Value::Arr(history)),
            ("note".into(), Value::opt_str(&self.note)),
        ])
    }

    pub fn from_json(v: &Value) -> Option<ServiceState> {
        let name = v.get("name").and_then(Value::as_str)?.to_string();
        let mut st = ServiceState::new(&name);
        if let Some(s) = v.get("status").and_then(Value::as_str) {
            st.status = ServiceStatus::parse(s);
        }
        st.pid = v.get("pid").and_then(Value::as_u64).map(|p| p as u32);
        st.proc_start_time = v.get("proc_start_time").and_then(Value::as_u64);
        st.last_start_ms = v.get("last_start_ms").and_then(Value::as_u64);
        st.last_exit_code = v.get("last_exit_code").and_then(Value::as_i64).map(|c| c as i32);
        st.consecutive_restarts =
            v.get("consecutive_restarts").and_then(Value::as_u64).unwrap_or(0) as u32;
        st.total_restarts = v.get("total_restarts").and_then(Value::as_u64).unwrap_or(0);
        st.exit_count = v.get("exit_count").and_then(Value::as_u64).unwrap_or(0);
        if let Some(arr) = v.get("restart_history").and_then(Value::as_arr) {
            for item in arr {
                let at_ms = item.get("at_ms").and_then(Value::as_u64).unwrap_or(0);
                let exit_code = item.get("exit_code").and_then(Value::as_i64).unwrap_or(0) as i32;
                st.restart_history.push(RestartRecord { at_ms, exit_code });
            }
        }
        st.note = v.get("note").and_then(Value::as_str).map(str::to_string);
        Some(st)
    }
}

/// The on-disk state store: one JSON file, atomically replaced on save.
pub struct StateStore {
    path: PathBuf,
    pub supervisor_pid: u32,
    pub services: BTreeMap<String, ServiceState>,
}

impl StateStore {
    pub fn path_in(state_dir: &Path) -> PathBuf {
        state_dir.join("state.json")
    }

    pub fn load(state_dir: &Path) -> StateStore {
        let path = Self::path_in(state_dir);
        let mut store = StateStore {
            path,
            supervisor_pid: 0,
            services: BTreeMap::new(),
        };
        if let Ok(text) = std::fs::read_to_string(&store.path) {
            if let Ok(v) = Value::parse(&text) {
                store.supervisor_pid =
                    v.get("supervisor_pid").and_then(Value::as_u64).unwrap_or(0) as u32;
                if let Some(Value::Obj(services)) = v.get("services") {
                    for (name, sv) in services {
                        if let Some(st) = ServiceState::from_json(sv) {
                            store.services.insert(name.clone(), st);
                        }
                    }
                }
            }
        }
        store
    }

    pub fn save(&self) -> io::Result<()> {
        let services: Vec<(String, Value)> = self
            .services
            .iter()
            .map(|(k, v)| (k.clone(), v.to_json()))
            .collect();
        let root = Value::Obj(vec![
            ("supervisor_pid".into(), Value::num_u64(self.supervisor_pid as u64)),
            ("services".into(), Value::Obj(services)),
        ]);
        let tmp = self.path.with_extension("json.tmp");
        std::fs::write(&tmp, root.to_pretty())?;
        std::fs::rename(&tmp, &self.path)?;
        Ok(())
    }
}
