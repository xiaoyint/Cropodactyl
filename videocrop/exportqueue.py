"""Background export queue: runs one EncodeSession at a time with QProcess.

Everything here runs in the GUI thread but never blocks it: QProcess is fully
asynchronous, and FFmpeg itself runs at reduced CPU priority (``nice``) so
the preview stays smooth.
"""
from __future__ import annotations

import itertools
import os
import uuid
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QProcess, Signal

from . import encoding, files
from .encoding import EncodePlan, EncodeSession, FFmpegCaps, Outcome, Step, StepResult

PENDING, RUNNING, DONE, FAILED, CANCELLED = "pending", "running", "done", "failed", "cancelled"
_STDERR_CAP = 64 * 1024


def pid_alive(pid: int) -> bool:
    """Does a process with this PID exist?  Never affects the process.

    (On Windows ``os.kill(pid, 0)`` would *terminate* the process, so the
    Win32 API is queried instead.)
    """
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return k32.GetLastError() == 5  # ERROR_ACCESS_DENIED: exists
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Job:
    id: int
    source: str
    plan: EncodePlan
    out_path: str  # reserved final path
    may_overwrite: bool
    state: str = PENDING
    progress: float = 0.0
    phase: str = ""
    speed: str = ""
    message: str = ""
    final_path: str | None = None
    size: int = 0
    method: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return os.path.basename(self.source)

    @property
    def active(self) -> bool:
        return self.state in (PENDING, RUNNING)


class EncodeQueue(QObject):
    changed = Signal()            # job added / state changed
    progress = Signal(object)     # Job (running job progress update)
    idle = Signal()               # queue became empty

    def __init__(self, caps: FFmpegCaps, out_dir: str, parent=None):
        super().__init__(parent)
        self.caps = caps
        self.out_dir = os.path.abspath(out_dir)
        self.jobs: list[Job] = []
        self._ids = itertools.count(1)
        self._reserved: dict[str, str] = {}  # source -> reserved output path
        self._created: set[str] = set()      # outputs written by this session
        self._proc: QProcess | None = None
        self._job: Job | None = None
        self._session: EncodeSession | None = None
        self._step: Step | None = None
        self._parser = encoding.ProgressParser()
        self._stdout = ""
        self._stderr = ""
        self._abort_for_size = False
        self._cancelling = False
        self._tmp_root = os.path.join(self.out_dir, files.TEMP_DIR_NAME)
        self._tmp_base = os.path.join(self._tmp_root, f"{os.getpid()}-{uuid.uuid4().hex[:8]}")
        self._clean_stale_temp()

    # ------------------------------------------------------------ helpers
    def _clean_stale_temp(self) -> None:
        """Remove temp dirs left behind by crashed earlier runs."""
        if not os.path.isdir(self._tmp_root):
            return
        for name in os.listdir(self._tmp_root):
            pid = name.split("-", 1)[0]
            alive = pid.isdigit() and int(pid) != os.getpid() and pid_alive(int(pid))
            if not alive:
                encoding.cleanup_dir(os.path.join(self._tmp_root, name))
        try:
            os.rmdir(self._tmp_root)
        except OSError:
            pass

    def counts(self) -> dict[str, int]:
        c = {PENDING: 0, RUNNING: 0, DONE: 0, FAILED: 0, CANCELLED: 0}
        for j in self.jobs:
            c[j.state] += 1
        return c

    def is_busy(self) -> bool:
        return any(j.active for j in self.jobs)

    def latest_job_for(self, source: str) -> Job | None:
        for j in reversed(self.jobs):
            if j.source == source:
                return j
        return None

    # ------------------------------------------------------------ public API
    def enqueue(self, plan: EncodePlan) -> Job:
        source = plan.source
        # Re-saving a video replaces any still-queued/running job for it.
        for j in self.jobs:
            if j.source == source and j.active:
                self.cancel(j.id, emit=False)
        if source in self._reserved:
            out_path = self._reserved[source]
        else:
            out_path = files.unique_output_path(self.out_dir, source,
                                                set(self._reserved.values()),
                                                ext=plan.output_ext)
            self._reserved[source] = out_path
        job = Job(next(self._ids), source, plan, out_path,
                  may_overwrite=out_path in self._created,
                  warnings=list(plan.warnings))
        self.jobs.append(job)
        self.changed.emit()
        self._start_next()
        return job

    def cancel(self, job_id: int, emit: bool = True) -> None:
        for j in self.jobs:
            if j.id != job_id:
                continue
            if j.state == PENDING:
                j.state = CANCELLED
                j.message = "cancelled"
            elif j.state == RUNNING and self._job is j:
                self._cancelling = True
                if self._proc is not None:
                    self._proc.kill()  # finished() handler finishes the job
        if emit:
            self.changed.emit()

    def cancel_pending(self) -> None:
        for j in self.jobs:
            if j.state == PENDING:
                j.state = CANCELLED
                j.message = "cancelled"
        self.changed.emit()

    def abort_all(self) -> None:
        """Synchronous shutdown: kill FFmpeg and delete temporary files."""
        for j in self.jobs:
            if j.state == PENDING:
                j.state = CANCELLED
                j.message = "cancelled (application closed)"
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            try:
                proc.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            proc.kill()
            proc.waitForFinished(5000)
        if self._job is not None:
            self._job.state = CANCELLED
            self._job.message = "cancelled (application closed)"
        if self._session is not None:
            encoding.cleanup_dir(self._session.tmp_dir)
        self._job = self._session = self._step = None
        self._remove_temp_base()

    def _remove_temp_base(self) -> None:
        encoding.cleanup_dir(self._tmp_base)
        try:
            os.rmdir(self._tmp_root)  # only succeeds if no other instance uses it
        except OSError:
            pass

    # ------------------------------------------------------------ job flow
    def _start_next(self) -> None:
        if self._job is not None:
            return
        job = next((j for j in self.jobs if j.state == PENDING), None)
        if job is None:
            self._remove_temp_base()
            self.idle.emit()
            return
        self._job = job
        job.state = RUNNING
        job.progress = 0.0
        tmp = os.path.join(self._tmp_base, f"job{job.id}")
        try:
            os.makedirs(tmp, exist_ok=True)
        except OSError as e:
            self._finish(Outcome(False, f"cannot create temporary folder: {e}"))
            return
        self._session = job.plan.make_session(self.caps, tmp, job.out_path,
                                              may_overwrite=job.may_overwrite)
        self.changed.emit()
        self._advance(self._session.first_step())

    def _advance(self, step: Step | Outcome) -> None:
        if isinstance(step, Outcome):
            self._finish(step)
            return
        self._step = step
        self._parser = encoding.ProgressParser()
        self._stdout = ""
        self._stderr = ""
        self._abort_for_size = False
        assert self._job is not None
        self._job.phase = step.label
        self._job.speed = ""
        self._job.progress = step.progress_lo
        self.progress.emit(self._job)

        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)
        args = list(step.args)
        if step.nice and self.caps.nice:
            program, args = self.caps.nice, ["-n", "10", *args]
        else:
            program, args = args[0], args[1:]
        self._proc = proc
        proc.start(program, args)

    def _on_stdout(self) -> None:
        proc = self._proc
        if proc is None or self._step is None or self._job is None:
            return
        text = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        if self._step.kind != "ffmpeg":  # probe / helper tools: keep their output
            self._stdout += text
            return
        if not self._parser.feed(text):
            return
        st, job = self._step, self._job
        dur = max(job.plan.info.duration, 0.001)
        frac = min(1.0, self._parser.out_time / dur)
        job.progress = st.progress_lo + (st.progress_hi - st.progress_lo) * frac
        job.speed = self._parser.speed
        self.progress.emit(job)
        if (st.watch_size and self._session is not None and not self._abort_for_size
                and self._session.should_abort(self._parser.total_size, self._parser.out_time)):
            self._abort_for_size = True
            proc.kill()

    def _on_stderr(self) -> None:
        if self._proc is None:
            return
        self._stderr += bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        if len(self._stderr) > _STDERR_CAP:
            self._stderr = self._stderr[-_STDERR_CAP:]

    def _on_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and self._proc is not None:
            # finished() is not emitted in this case
            self._stderr += f"\nfailed to start {self._proc.program()}"
            self._on_finished(-1, QProcess.ExitStatus.CrashExit)

    def _on_finished(self, exit_code: int, exit_status) -> None:
        proc = self._proc
        if proc is None:
            return
        self._on_stdout()
        self._on_stderr()
        self._proc = None
        proc.deleteLater()
        if self._cancelling:
            self._cancelling = False
            self._finish(Outcome(False, "cancelled"), cancelled=True)
            return
        crashed = exit_status != QProcess.ExitStatus.NormalExit
        result = StepResult(exit_code if not crashed else -1, self._stdout, self._stderr,
                            aborted=self._abort_for_size)
        assert self._session is not None
        try:
            nxt = self._session.next_step(result)
        except Exception as e:  # defensive: never wedge the queue
            nxt = Outcome(False, f"internal error: {e}")
        self._advance(nxt)

    def _finish(self, outcome: Outcome, cancelled: bool = False) -> None:
        job, session = self._job, self._session
        assert job is not None
        if cancelled:
            job.state = CANCELLED
            job.message = "cancelled"
        elif outcome.success:
            job.state = DONE
            job.progress = 1.0
            job.final_path = outcome.final_path
            job.size = outcome.size
            job.method = outcome.method
            job.message = f"{outcome.size / 1e6:.1f} MB, {outcome.method}"
            if outcome.final_path:
                self._created.add(outcome.final_path)
                self._reserved[job.source] = outcome.final_path
        else:
            job.state = FAILED
            job.message = outcome.message
        if session is not None:
            job.warnings += [n for n in session.notes if n not in job.warnings]
            encoding.cleanup_dir(session.tmp_dir)
        self._job = self._session = self._step = None
        self.changed.emit()
        self._start_next()
