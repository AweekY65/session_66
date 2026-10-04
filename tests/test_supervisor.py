"""End-to-end tests for the local supervisor.

Uses small test programs (tests/programs/) that simulate normal exits,
crashes, signal-ignoring processes and heavy log output. Every wait is
bounded so the suite always terminates.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.config import ServiceConfig, load_config
from supervisor.state import proc_starttime
from supervisor.supervisor import Supervisor, read_status
from supervisor.service import ServiceStatus

PY = sys.executable
PROG = Path(__file__).resolve().parent / "programs"


def wait_for(cond, timeout=10.0, interval=0.02, msg="condition not met"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    raise AssertionError(msg)


def make_supervisor(tmp_path, *service_cfgs):
    from supervisor.config import SupervisorConfig

    cfg = SupervisorConfig(state_dir=tmp_path / "sv", services=list(service_cfgs))
    return Supervisor(cfg)


def svc_cfg(name, program, *args, **kw):
    kw.setdefault("backoff_initial", 0.1)
    kw.setdefault("backoff_max", 1.0)
    kw.setdefault("stop_timeout", 1.0)
    return ServiceConfig(
        name=name, command=[PY, str(PROG / program), *map(str, args)], **kw
    )


@pytest.fixture
def sup(tmp_path):
    """Provide a supervisor factory that guarantees cleanup."""
    supervisors = []

    def factory(*cfgs):
        s = make_supervisor(tmp_path, *cfgs)
        supervisors.append(s)
        return s

    yield factory
    for s in supervisors:
        s.stop_all()


# --------------------------------------------------------------------- config


def test_toml_config_loading(tmp_path):
    cfg_file = tmp_path / "sv.toml"
    cfg_file.write_text(
        """
[supervisor]
state_dir = "state"

[[service]]
name = "a"
command = "python3 worker.py"
restart = "always"
max_restarts = 3
backoff_initial = 0.2
log_max_bytes = 1024
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.state_dir == tmp_path / "state"
    (svc,) = cfg.services
    assert svc.command == ["python3", "worker.py"]
    assert svc.restart == "always"
    assert svc.max_restarts == 3
    assert svc.backoff_initial == 0.2
    assert svc.log_max_bytes == 1024


def test_invalid_restart_policy_rejected(tmp_path):
    with pytest.raises(ValueError):
        svc_cfg("bad", "normal_exit.py", restart="sometimes").validate()


# ------------------------------------------------------------- basic lifecycle


def test_normal_exit_records_state(sup):
    s = sup(svc_cfg("ok", "normal_exit.py", restart="never"))
    s.start_all()
    svc = s.services["ok"]
    wait_for(lambda: svc.status == ServiceStatus.EXITED, msg="never exited")
    snap = svc.snapshot()
    assert snap["exit_code"] == 0
    assert snap["started_at"] is not None
    assert snap["exited_at"] is not None
    assert snap["restart_count"] == 0
    # state file persisted
    data = json.loads(svc.state_path.read_text())
    assert data["status"] == "exited"
    assert data["exit_code"] == 0


def test_crash_with_never_policy_does_not_restart(sup):
    s = sup(svc_cfg("c", "crash.py", restart="never"))
    s.start_all()
    svc = s.services["c"]
    wait_for(lambda: svc.status == ServiceStatus.FAILED, msg="never failed")
    time.sleep(0.3)
    assert svc.snapshot()["restart_count"] == 0
    assert svc.snapshot()["exit_code"] == 1


# ------------------------------------------------------------ restart policy


def test_on_failure_restarts_up_to_max_then_failed(sup):
    s = sup(
        svc_cfg(
            "c",
            "crash.py",
            restart="on-failure",
            max_restarts=2,
            backoff_initial=0.05,
        )
    )
    s.start_all()
    svc = s.services["c"]
    wait_for(lambda: svc.status == ServiceStatus.FAILED, msg="did not reach FAILED")
    snap = svc.snapshot()
    assert snap["restart_count"] == 2
    assert snap["consecutive_restarts"] == 2
    assert snap["exit_code"] == 1
    assert [r["seq"] for r in snap["restarts"]] == [1, 2]


def test_on_failure_recovers_when_process_becomes_stable(sup, tmp_path):
    counter = tmp_path / "crashes.txt"
    s = sup(
        svc_cfg(
            "c",
            "crash.py",
            counter,
            2,  # crash twice, then stay alive
            restart="on-failure",
            max_restarts=5,
            backoff_initial=0.05,
        )
    )
    s.start_all()
    svc = s.services["c"]
    wait_for(
        lambda: svc.status == ServiceStatus.RUNNING
        and svc.snapshot()["restart_count"] == 2,
        msg="did not stabilize",
    )


def test_always_policy_restarts_even_on_clean_exit(sup):
    s = sup(
        svc_cfg(
            "a",
            "normal_exit.py",
            restart="always",
            max_restarts=10,
            backoff_initial=0.05,
        )
    )
    s.start_all()
    svc = s.services["a"]
    wait_for(
        lambda: svc.snapshot()["restart_count"] >= 2, msg="always policy did not restart"
    )


# --------------------------------------------------------------------- backoff


def test_exponential_backoff_delays_grow(sup):
    s = sup(
        svc_cfg(
            "c",
            "crash.py",
            restart="on-failure",
            max_restarts=3,
            backoff_initial=0.2,
            backoff_factor=2.0,
            backoff_max=10.0,
        )
    )
    s.start_all()
    svc = s.services["c"]
    wait_for(lambda: svc.status == ServiceStatus.FAILED, msg="did not reach FAILED")
    restarts = svc.snapshot()["restarts"]
    assert len(restarts) == 3
    delays = [r["delay"] for r in restarts]
    assert delays == pytest.approx([0.2, 0.4, 0.8])
    # actual restart timestamps respect the backoff gaps (no busy loop)
    gaps = [b["time"] - a["time"] for a, b in zip(restarts, restarts[1:])]
    assert gaps[0] >= 0.15  # ~0.2 delay + crash runtime
    assert gaps[1] >= 0.35  # ~0.4 delay + crash runtime
    assert gaps[1] > gaps[0]  # backoff grows


def test_backoff_is_capped(sup):
    s = sup(
        svc_cfg(
            "c",
            "crash.py",
            restart="on-failure",
            max_restarts=3,
            backoff_initial=0.5,
            backoff_factor=10.0,
            backoff_max=0.6,
        )
    )
    s.start_all()
    svc = s.services["c"]
    wait_for(lambda: svc.status == ServiceStatus.FAILED, msg="did not reach FAILED")
    delays = [r["delay"] for r in svc.snapshot()["restarts"]]
    assert delays == pytest.approx([0.5, 0.6, 0.6])


# ------------------------------------------------------------------ shutdown


def test_graceful_shutdown_via_sigterm(sup):
    s = sup(svc_cfg("g", "stay_up.py", stop_timeout=2.0))
    s.start_all()
    svc = s.services["g"]
    wait_for(lambda: svc.status == ServiceStatus.RUNNING, msg="never ran")
    # wait until the child has installed its SIGTERM handler
    wait_for(
        lambda: svc.log_path.exists() and "ready" in svc.log_path.read_text(),
        msg="child not ready",
    )
    t0 = time.monotonic()
    svc.stop()
    elapsed = time.monotonic() - t0
    assert svc.status == ServiceStatus.STOPPED
    assert svc.snapshot()["exit_code"] == 0  # handled SIGTERM itself
    assert elapsed < 2.0  # no need to wait for the timeout


def test_force_kill_when_sigterm_ignored(sup):
    s = sup(svc_cfg("k", "ignore_term.py", stop_timeout=0.3))
    s.start_all()
    svc = s.services["k"]
    wait_for(lambda: svc.status == ServiceStatus.RUNNING, msg="never ran")
    wait_for(
        lambda: svc.log_path.exists() and "ready" in svc.log_path.read_text(),
        msg="child not ready",
    )
    t0 = time.monotonic()
    svc.stop()
    elapsed = time.monotonic() - t0
    assert svc.status == ServiceStatus.STOPPED
    assert svc.snapshot()["exit_code"] == -signal.SIGKILL
    assert elapsed >= 0.3  # graceful timeout was honored before SIGKILL


# ----------------------------------------------------------------------- logs


def _read_all_logs(log_path):
    files = [log_path]
    files += sorted(
        log_path.parent.glob(log_path.name + ".*"),
        key=lambda p: int(p.name.rsplit(".", 1)[1]),
    )
    out = b""
    for f in files:
        if f.exists():
            out += f.read_bytes()
    return out


def test_stdout_and_stderr_captured(sup):
    s = sup(svc_cfg("l", "log_spam.py", 10, 8, restart="never"))
    s.start_all()
    svc = s.services["l"]
    wait_for(lambda: svc.status == ServiceStatus.EXITED, msg="never exited")
    svc.close()
    content = svc.log_path.read_text()
    assert " stdout]" in content and "spam-00000" in content
    assert " stderr]" in content and "errspam-00000" in content


def test_log_rotation_preserves_complete_records(sup):
    count = 600
    s = sup(
        svc_cfg(
            "r",
            "log_spam.py",
            count,
            40,
            restart="never",
            log_max_bytes=2048,
            log_backups=100,
        )
    )
    s.start_all()
    svc = s.services["r"]
    wait_for(lambda: svc.status == ServiceStatus.EXITED, msg="never exited")
    svc.close()

    rotated = sorted(svc.log_path.parent.glob(svc.log_path.name + ".*"))
    assert rotated, "rotation never happened"

    raw = _read_all_logs(svc.log_path)
    lines = raw.decode().splitlines()
    # every record is complete and well-formed (no torn writes)
    pattern = re.compile(r"^\[[^\]]+ (stdout|stderr)\] (err)?spam-(\d{5}) x+$")
    seen = {"stdout": set(), "stderr": set()}
    for line in lines:
        m = pattern.match(line)
        assert m, f"torn or malformed record: {line!r}"
        seen[m.group(1)].add(int(m.group(3)))
    # no record lost during rotation
    assert seen["stdout"] == set(range(count))
    assert seen["stderr"] == set(range(count))


def test_log_rotation_caps_backup_count(sup):
    s = sup(
        svc_cfg(
            "r",
            "log_spam.py",
            3000,
            40,
            restart="never",
            log_max_bytes=1024,
            log_backups=2,
        )
    )
    s.start_all()
    svc = s.services["r"]
    wait_for(lambda: svc.status == ServiceStatus.EXITED, msg="never exited")
    svc.close()
    backups = list(svc.log_path.parent.glob(svc.log_path.name + ".*"))
    assert len(backups) == 2
    assert svc.log_path.stat().st_size <= 1024 + 128  # one record overshoot max


# ------------------------------------------------------------------- recovery


def _write_state(path, **kw):
    base = {
        "name": path.name.split(".")[0],
        "status": "running",
        "pid": None,
        "pgid": None,
        "proc_starttime": None,
        "started_at": time.time(),
        "exit_code": None,
        "consecutive_restarts": 0,
        "restarts": [],
    }
    base.update(kw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(base))


def test_recovery_ignores_dead_pid(sup):
    dead = subprocess.run([PY, "-c", "pass"]).returncode  # ensure python works
    proc = subprocess.Popen([PY, "-c", "pass"])
    proc.wait()
    dead_pid = proc.pid

    s = sup(svc_cfg("x", "stay_up.py"))
    state_path = s.state_dir / "x.state.json"
    _write_state(state_path, pid=dead_pid, proc_starttime=12345)
    result = s.recover()
    assert result["x"] == "stale"
    assert s.services["x"].status == ServiceStatus.STOPPED
    assert s.services["x"].snapshot()["pid"] is None


def test_recovery_ignores_recycled_pid(sup):
    # A live but unrelated process now owns the recorded PID.
    other = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
    try:
        s = sup(svc_cfg("x", "stay_up.py"))
        state_path = s.state_dir / "x.state.json"
        real_start = proc_starttime(other.pid)
        assert real_start is not None
        _write_state(
            state_path, pid=other.pid, proc_starttime=real_start + 1000
        )
        result = s.recover()
        assert result["x"] == "stale"
        assert s.services["x"].status == ServiceStatus.STOPPED
        # the unrelated process must not be touched
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_recovery_adopts_live_managed_process(sup):
    # Simulate a child left behind by a previous supervisor incarnation.
    proc = subprocess.Popen(
        [PY, str(PROG / "stay_up.py")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        s = sup(svc_cfg("x", "stay_up.py"))
        state_path = s.state_dir / "x.state.json"
        _write_state(
            state_path,
            pid=proc.pid,
            pgid=proc.pid,
            proc_starttime=proc_starttime(proc.pid),
        )
        result = s.recover()
        assert result["x"] == "adopted"
        svc = s.services["x"]
        assert svc.status == ServiceStatus.RUNNING
        assert svc.snapshot()["pid"] == proc.pid
        # the adopted process can still be gracefully stopped
        svc.stop()
        wait_for(lambda: proc.poll() is not None, msg="adopted proc not stopped")
        assert svc.status == ServiceStatus.STOPPED
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_read_status_from_disk(sup, tmp_path):
    s = sup(svc_cfg("ok", "normal_exit.py", restart="never"))
    s.start_all()
    wait_for(
        lambda: s.services["ok"].status == ServiceStatus.EXITED, msg="never exited"
    )
    status = read_status(s.state_dir)
    assert status["services"]["ok"]["status"] == "exited"
    assert status["supervisor"]["pid"] == os.getpid()
