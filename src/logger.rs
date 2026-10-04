//! Size-rotating log writer. Rotation happens *between* records so a
//! complete record is never split across files and no written record is
//! lost: the active file is renamed as a whole and a fresh file opened.

use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub struct RotatingLogger {
    base: PathBuf,
    max_bytes: u64,
    keep: u32,
    inner: Mutex<Inner>,
}

struct Inner {
    file: File,
    size: u64,
}

impl RotatingLogger {
    pub fn new(log_dir: &Path, service: &str, max_bytes: u64, keep: u32) -> io::Result<Self> {
        std::fs::create_dir_all(log_dir)?;
        let base = log_dir.join(format!("{}.log", sanitize(service)));
        let file = OpenOptions::new().create(true).append(true).open(&base)?;
        let size = file.metadata().map(|m| m.len()).unwrap_or(0);
        Ok(RotatingLogger {
            base,
            max_bytes,
            keep: keep.max(1),
            inner: Mutex::new(Inner { file, size }),
        })
    }

    /// Append one complete record (a single line with timestamp/stream).
    pub fn log(&self, stream: &str, msg: &str) {
        let record = format!("[{}] [{}] {}\n", format_ts(crate::sys::now_ms()), stream, msg);
        let mut inner = match self.inner.lock() {
            Ok(g) => g,
            Err(e) => e.into_inner(),
        };
        if self.max_bytes > 0
            && inner.size > 0
            && inner.size + record.len() as u64 > self.max_bytes
        {
            let _ = self.rotate(&mut inner);
        }
        if inner.file.write_all(record.as_bytes()).is_ok() {
            inner.size += record.len() as u64;
        }
    }

    /// Shift name.log.(k-1) -> name.log.k, name.log -> name.log.1, then
    /// open a fresh name.log. The current file is flushed first so every
    /// record written so far is preserved in the rotated file.
    fn rotate(&self, inner: &mut Inner) -> io::Result<()> {
        inner.file.flush()?;
        let oldest = self.rotated_path(self.keep);
        let _ = std::fs::remove_file(&oldest);
        for i in (1..self.keep).rev() {
            let from = self.rotated_path(i);
            if from.exists() {
                let _ = std::fs::rename(&from, self.rotated_path(i + 1));
            }
        }
        let _ = std::fs::rename(&self.base, self.rotated_path(1));
        inner.file = OpenOptions::new().create(true).append(true).open(&self.base)?;
        inner.size = 0;
        Ok(())
    }

    fn rotated_path(&self, index: u32) -> PathBuf {
        let mut p = self.base.clone().into_os_string();
        p.push(format!(".{}", index));
        PathBuf::from(p)
    }
}

/// List existing log files for a base path (active first, then rotations
/// in ascending age). Used by `status` tooling and tests.
pub fn existing_log_files(log_dir: &Path, service: &str) -> Vec<PathBuf> {
    let base = log_dir.join(format!("{}.log", sanitize(service)));
    let mut out = Vec::new();
    if base.exists() {
        out.push(base.clone());
    }
    for i in 1..=1000u32 {
        let mut p = base.clone().into_os_string();
        p.push(format!(".{}", i));
        let p = PathBuf::from(p);
        if p.exists() {
            out.push(p);
        } else {
            break;
        }
    }
    out
}

fn sanitize(name: &str) -> String {
    name.chars()
        .map(|c| if c.is_ascii_alphanumeric() || c == '-' || c == '_' { c } else { '_' })
        .collect()
}

/// Format epoch milliseconds as an ISO-8601 UTC timestamp.
fn format_ts(ms: u64) -> String {
    let secs = (ms / 1000) as i64;
    let milli = ms % 1000;
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400);
    let (y, mo, d) = civil_from_days(days);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        y,
        mo,
        d,
        tod / 3600,
        (tod / 60) % 60,
        tod % 60,
        milli
    )
}

/// Howard Hinnant's civil-from-days algorithm.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn timestamp_format() {
        assert_eq!(format_ts(0), "1970-01-01T00:00:00.000Z");
        assert_eq!(format_ts(1_759_500_000_000), "2025-10-03T14:00:00.000Z");
    }

    #[test]
    fn rotates_and_preserves_records() {
        let dir = std::env::temp_dir().join(format!("svlog-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let logger = RotatingLogger::new(&dir, "svc", 200, 3).unwrap();
        for i in 0..50 {
            logger.log("stdout", &format!("record-{:03}", i));
        }
        let files = existing_log_files(&dir, "svc");
        assert!(files.len() > 1, "expected rotation to happen");
        assert!(files.len() <= 4, "keep=3 plus active file");
        let mut lines = 0;
        for f in &files {
            let text = std::fs::read_to_string(f).unwrap();
            for line in text.lines() {
                assert!(line.contains("record-"), "record must be complete: {}", line);
                lines += 1;
            }
        }
        // keep=3 may drop the oldest chunks, but every surviving record is whole.
        assert!(lines > 0);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
