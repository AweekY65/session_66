//! Test helper program used by the integration tests to simulate
//! normal exits, crashes, signal-ignoring processes and log spam.

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use supervisor::sys;

static GOT_TERM: AtomicBool = AtomicBool::new(false);

extern "C" fn on_term(_sig: i32) {
    GOT_TERM.store(true, Ordering::SeqCst);
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mode = args.first().map(String::as_str).unwrap_or("");
    match mode {
        // exit CODE [DELAY_MS]: print a couple of lines, then exit.
        "exit" => {
            let code: i32 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
            let delay: u64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(0);
            println!("testprog exiting with code {}", code);
            eprintln!("testprog stderr line");
            if delay > 0 {
                std::thread::sleep(Duration::from_millis(delay));
            }
            std::process::exit(code);
        }
        // run: heartbeat until SIGTERM, then exit 0 (graceful).
        "run" => {
            sys::set_handler(sys::SIGTERM, on_term);
            println!("testprog running");
            loop {
                if GOT_TERM.load(Ordering::SeqCst) {
                    println!("testprog got SIGTERM, exiting gracefully");
                    std::process::exit(0);
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        // ignore-term: ignore SIGTERM entirely; only SIGKILL can stop it.
        "ignore-term" => {
            sys::ignore_signal(sys::SIGTERM);
            println!("testprog ignoring SIGTERM");
            loop {
                std::thread::sleep(Duration::from_millis(50));
            }
        }
        // spam LINES SIZE: emit LINES fixed-width lines to stdout,
        // plus a stderr line every 7th.
        "spam" => {
            let lines: usize = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(100);
            let size: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(100);
            let stdout = std::io::stdout();
            let mut out = stdout.lock();
            for i in 0..lines {
                let prefix = format!("line-{:06}-", i);
                let pad = size.saturating_sub(prefix.len());
                let _ = writeln!(out, "{}{}", prefix, "x".repeat(pad));
                if i % 7 == 0 {
                    eprintln!("err-{:06}", i);
                }
            }
        }
        other => {
            eprintln!("unknown mode '{}'", other);
            std::process::exit(2);
        }
    }
}
