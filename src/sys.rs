//! Thin wrappers over libc symbols (linked by default on Linux) so the
//! crate stays dependency-free.

use std::io;
use std::time::{SystemTime, UNIX_EPOCH};

pub const SIGINT: i32 = 2;
pub const SIGKILL: i32 = 9;
pub const SIGTERM: i32 = 15;
pub const SIG_IGN: usize = 1;

extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
    fn signal(signum: i32, handler: usize) -> usize;
}

/// Send a signal to a process. Returns Ok(true) when the signal was
/// delivered (or, for sig 0, the process exists).
pub fn send_signal(pid: u32, sig: i32) -> io::Result<()> {
    let rc = unsafe { kill(pid as i32, sig) };
    if rc == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

/// True if the process exists (and is signal-able by us).
pub fn process_alive(pid: u32) -> bool {
    let rc = unsafe { kill(pid as i32, 0) };
    if rc == 0 {
        return true;
    }
    // EPERM means the process exists but belongs to another user.
    io::Error::last_os_error().raw_os_error() == Some(1)
}

/// Install a C signal handler.
pub fn set_handler(signum: i32, handler: extern "C" fn(i32)) {
    unsafe {
        signal(signum, handler as usize);
    }
}

/// Ignore a signal entirely.
pub fn ignore_signal(signum: i32) {
    unsafe {
        signal(signum, SIG_IGN);
    }
}

/// Current time as milliseconds since the Unix epoch.
pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

/// Read the process start time (field 22 of /proc/<pid>/stat, in clock
/// ticks since boot). Used to detect PID reuse: a recycled PID will have
/// a different start time than the one we recorded.
pub fn proc_start_time(pid: u32) -> Option<u64> {
    let data = std::fs::read_to_string(format!("/proc/{}/stat", pid)).ok()?;
    // comm (field 2) may contain spaces/parens; skip past the last ')'.
    let close = data.rfind(')')?;
    let rest = &data[close + 1..];
    let fields: Vec<&str> = rest.split_whitespace().collect();
    // fields[0] is field 3 (state); starttime is field 22 -> index 19.
    fields.get(19)?.parse().ok()
}
