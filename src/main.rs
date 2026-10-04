//! `supervisor` CLI: run / status / stop.

use std::path::PathBuf;
use std::process::exit;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use supervisor::state::StateStore;
use supervisor::{sys, Config, Supervisor};

static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_sig: i32) {
    STOP.store(true, Ordering::SeqCst);
}

fn usage() -> ! {
    eprintln!(
        "usage:\n  supervisor run    --config FILE   start services in the foreground\n  supervisor status --config FILE   print service states\n  supervisor stop   --config FILE   ask a running supervisor to stop"
    );
    exit(2);
}

fn parse_args() -> (String, PathBuf) {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let cmd = args.first().cloned().unwrap_or_else(|| usage());
    let mut config = PathBuf::from("supervisor.json");
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--config" if i + 1 < args.len() => {
                config = PathBuf::from(&args[i + 1]);
                i += 2;
            }
            _ => usage(),
        }
    }
    (cmd, config)
}

fn main() {
    let (cmd, config_path) = parse_args();
    match cmd.as_str() {
        "run" => {
            let config = Config::load(&config_path).unwrap_or_else(|e| {
                eprintln!("config error: {}", e);
                exit(1);
            });
            let sv = Supervisor::new(config).unwrap_or_else(|e| {
                eprintln!("supervisor init failed: {}", e);
                exit(1);
            });
            sys::set_handler(sys::SIGTERM, on_signal);
            sys::set_handler(sys::SIGINT, on_signal);
            sv.start_all();
            while !STOP.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(50));
            }
            sv.stop_all();
        }
        "status" => {
            let config = Config::load(&config_path).unwrap_or_else(|e| {
                eprintln!("config error: {}", e);
                exit(1);
            });
            let store = StateStore::load(&config.state_dir);
            println!("supervisor pid: {}", store.supervisor_pid);
            for st in store.services.values() {
                println!(
                    "{:<20} {:<10} pid={:<8} exits={:<3} restarts={:<3} last_exit={:?} note={}",
                    st.name,
                    st.status.as_str(),
                    st.pid.map(|p| p.to_string()).unwrap_or_else(|| "-".into()),
                    st.exit_count,
                    st.total_restarts,
                    st.last_exit_code,
                    st.note.clone().unwrap_or_default(),
                );
            }
        }
        "stop" => {
            let config = Config::load(&config_path).unwrap_or_else(|e| {
                eprintln!("config error: {}", e);
                exit(1);
            });
            let store = StateStore::load(&config.state_dir);
            if store.supervisor_pid == 0 {
                eprintln!("no supervisor pid recorded in state");
                exit(1);
            }
            match sys::send_signal(store.supervisor_pid, sys::SIGTERM) {
                Ok(()) => println!("sent SIGTERM to supervisor pid {}", store.supervisor_pid),
                Err(e) => {
                    eprintln!("failed to signal supervisor: {}", e);
                    exit(1);
                }
            }
        }
        _ => usage(),
    }
}
