//! Service and supervisor configuration, loaded from a local JSON file.

use crate::json::Value;
use std::path::PathBuf;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RestartPolicy {
    Never,
    OnFailure,
    Always,
}

impl RestartPolicy {
    pub fn as_str(&self) -> &'static str {
        match self {
            RestartPolicy::Never => "never",
            RestartPolicy::OnFailure => "on-failure",
            RestartPolicy::Always => "always",
        }
    }

    pub fn parse(s: &str) -> Result<RestartPolicy, String> {
        match s {
            "never" => Ok(RestartPolicy::Never),
            "on-failure" | "on_failure" | "onfailure" => Ok(RestartPolicy::OnFailure),
            "always" => Ok(RestartPolicy::Always),
            other => Err(format!("unknown restart policy '{}'", other)),
        }
    }
}

#[derive(Clone, Debug)]
pub struct ServiceConfig {
    pub name: String,
    pub command: String,
    pub args: Vec<String>,
    pub restart: RestartPolicy,
    /// Maximum number of *consecutive* restarts before giving up.
    pub max_restarts: u32,
    pub backoff_initial_ms: u64,
    pub backoff_max_ms: u64,
    /// Grace period between SIGTERM and SIGKILL when stopping.
    pub stop_timeout_ms: u64,
    /// Rotate the service log when it would exceed this many bytes (0 = never).
    pub log_max_bytes: u64,
    /// Number of rotated log files to keep.
    pub log_keep: u32,
}

impl ServiceConfig {
    pub fn new(name: &str, command: &str) -> Self {
        ServiceConfig {
            name: name.to_string(),
            command: command.to_string(),
            args: Vec::new(),
            restart: RestartPolicy::Never,
            max_restarts: 3,
            backoff_initial_ms: 500,
            backoff_max_ms: 30_000,
            stop_timeout_ms: 5_000,
            log_max_bytes: 10 * 1024 * 1024,
            log_keep: 5,
        }
    }

    fn from_json(v: &Value) -> Result<Self, String> {
        let name = v
            .get("name")
            .and_then(Value::as_str)
            .ok_or("service missing 'name'")?
            .to_string();
        let command = v
            .get("command")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("service '{}' missing 'command'", name))?
            .to_string();
        let mut cfg = ServiceConfig::new(&name, &command);
        if let Some(args) = v.get("args").and_then(Value::as_arr) {
            cfg.args = args
                .iter()
                .map(|a| a.as_str().map(str::to_string).ok_or("args must be strings".to_string()))
                .collect::<Result<_, _>>()?;
        }
        if let Some(p) = v.get("restart").and_then(Value::as_str) {
            cfg.restart = RestartPolicy::parse(p)?;
        }
        if let Some(n) = v.get("max_restarts").and_then(Value::as_u64) {
            cfg.max_restarts = n as u32;
        }
        if let Some(n) = v.get("backoff_initial_ms").and_then(Value::as_u64) {
            cfg.backoff_initial_ms = n;
        }
        if let Some(n) = v.get("backoff_max_ms").and_then(Value::as_u64) {
            cfg.backoff_max_ms = n;
        }
        if let Some(n) = v.get("stop_timeout_ms").and_then(Value::as_u64) {
            cfg.stop_timeout_ms = n;
        }
        if let Some(n) = v.get("log_max_bytes").and_then(Value::as_u64) {
            cfg.log_max_bytes = n;
        }
        if let Some(n) = v.get("log_keep").and_then(Value::as_u64) {
            cfg.log_keep = n as u32;
        }
        Ok(cfg)
    }
}

#[derive(Clone, Debug)]
pub struct Config {
    pub state_dir: PathBuf,
    pub log_dir: PathBuf,
    pub services: Vec<ServiceConfig>,
}

impl Config {
    pub fn from_json_str(text: &str) -> Result<Config, String> {
        let v = Value::parse(text)?;
        let state_dir = v
            .get("state_dir")
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(".svstate"));
        let log_dir = v
            .get("log_dir")
            .and_then(Value::as_str)
            .map(PathBuf::from)
            .unwrap_or_else(|| state_dir.join("logs"));
        let services_v = v
            .get("services")
            .and_then(Value::as_arr)
            .ok_or("config missing 'services' array")?;
        let mut services = Vec::new();
        for s in services_v {
            services.push(ServiceConfig::from_json(s)?);
        }
        let mut names = std::collections::HashSet::new();
        for s in &services {
            if !names.insert(s.name.clone()) {
                return Err(format!("duplicate service name '{}'", s.name));
            }
        }
        Ok(Config { state_dir, log_dir, services })
    }

    pub fn load(path: &std::path::Path) -> Result<Config, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("cannot read {}: {}", path.display(), e))?;
        Config::from_json_str(&text)
    }
}
