"""Configuration memory access for cardio2e (backup and restore).

The Cardio2e configuration lives in a 34176-byte memory that the Windows
PCCardio tool reads and writes over the same RS-232 link with an undocumented
``E`` command family (reverse engineered from PCCardio traffic):

* ``@G E<addr:4hex>08<cs>`` sets the read pointer (``@A E``), then every
  ``@G EN`` returns the next 8 bytes as ``@I E<8 bytes hex><cs>``.
* ``@S ED<addr:4hex><cs>`` sets the write pointer, then ``@S EN<bytes><cs>``
  writes up to 8 bytes there (``@A E`` each); the pointer auto-increments.
* ``<cs>`` is the two's complement of the byte sum, so that the bytes plus
  the checksum add up to 0 mod 256.

PCCardio starts its writes with ``@S ER <n>``, which resets the memory to the
factory defaults before only the non-default bytes are sent. It is never used
here: a restore writes only the bytes that differ from the controller, and
reads them back.

Normal commands (lights, relays, ...) interleaved with a read do not disturb
the read pointer, so a backup runs while the bridge keeps working. Memory
requests are bulk traffic (``bulk_request``): they never interleave with the
reply to another command and pause while the bus is busy, so they do not
garble light/relay commands. They are served by the SerialReader (steady
state only).
"""

import contextlib
import datetime
import logging
import os
import re
import threading
import time
import uuid

from .cardio2e_constants import CARDIO2E_TERMINATOR
from .cardio2e_serial import bulk_request, reader_active

_LOGGER = logging.getLogger(__name__)

MEMORY_SIZE = 0x8580
BLOCK_SIZE = 8          # bytes per @G EN reply / per @S EN write
READ_CHUNK = 256        # the read pointer is re-set at every chunk
REQUEST_TIMEOUT = 1.5
MAX_RETRIES = 3

# Range a restore may write: the configuration tables PCCardio writes
# (names, outlets, lamps, covers, zones, scenarios, ...).
RESTORE_RANGE = (0x5B30, 0x8574)

# Never restored: sensitive or runtime data inside RESTORE_RANGE.
RESTORE_EXCLUDED = (
    (0x7FC0, 0x7FE1, "monitoring station"),
    (0x80AB, 0x817E, "user/installer codes and emergency phones"),
    (0x818E, 0x818F, "runtime value"),
)

OUTLETS_BASE, OUTLETS_COUNT = 0x5B30, 40
LAMPS_BASE, LAMPS_COUNT = 0x5E78, 160
RECORD_SIZE = 21
NAME_SIZE = 9

LAMP_FLAGS = (
    (0x01, "blink on doorbell"),
    (0x02, "blink on phone"),
    (0x04, "sunrise/sunset reaction"),
    (0x08, "soft on/off"),
    (0x10, "blink on intrusion"),
)
OUTLET_FLAGS = (
    (0x01, "off on doorbell"),
    (0x02, "off on phone"),
    (0x04, "sunrise/sunset reaction"),
)

_PERIOD_HOUR_CODES = {0xE0: "sunset", 0xD0: "sunrise"}

# offset -> (field name, kind). Offsets 0..8 are the name (handled apart).
_COMMON_FIELDS = {
    9: ("X-10 house", "hex"),
    10: ("X-10 unit", "hex"),
    13: ("pulse zone", "zone"),
    16: ("active from (hour)", "hour"),
    17: ("active from (minute)", "bcd"),
    18: ("active to (hour)", "hour"),
    19: ("active to (minute)", "bcd"),
    20: ("reserved", "hex"),
}
OUTLET_FIELDS = {**_COMMON_FIELDS, **{
    11: ("options", "outlet_flags"),
    12: ("auto off (s)", "int"),
    14: ("device state", "onoff"),
    15: ("reaction duration (min)", "int"),
}}
LAMP_FIELDS = {**_COMMON_FIELDS, **{
    11: ("options", "lamp_flags"),
    12: ("deterrence random delay (min)", "int"),
    14: ("intensity (%)", "int"),
    15: ("duration (min)", "int"),
}}

BACKUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.bin$")


class MemoryAccessError(RuntimeError):
    """A memory request failed (no reply, NACK or bad checksum)."""


class JobCancelled(Exception):
    """A backup or compare was cancelled from the panel."""


def checksum(data):
    """Two's complement of the byte sum (bytes + checksum == 0 mod 256)."""
    return (-sum(data)) & 0xFF


def _is_e_reply(parts, prefix):
    return len(parts) >= 2 and parts[0] == prefix and parts[1].startswith("E")


def _request(serial_conn, command, prefix):
    """Send one E command and wait for its ``<prefix> E...`` reply.

    Returns the raw reply line. Raises MemoryAccessError on timeout or NACK.
    """
    def predicate(parts):
        return _is_e_reply(parts, prefix) or _is_e_reply(parts, "@N")

    line = bulk_request(serial_conn, command + CARDIO2E_TERMINATOR, predicate, REQUEST_TIMEOUT)
    if line is None:
        raise MemoryAccessError("no reply to %r" % command)
    if line.startswith("@N"):
        raise MemoryAccessError("%r rejected: %s" % (command, line))
    return line


def _set_read_pointer(serial_conn, addr):
    head = [addr >> 8, addr & 0xFF, BLOCK_SIZE]
    _request(serial_conn, "@G E%04X%02X%02X" % (addr, BLOCK_SIZE, checksum(head)), "@A")


def _read_block(serial_conn):
    line = _request(serial_conn, "@G EN", "@I")
    try:
        data = bytes.fromhex(line.split(None, 1)[1][1:])
    except (IndexError, ValueError):
        raise MemoryAccessError("malformed reply %r" % line)
    if len(data) != BLOCK_SIZE + 1 or sum(data) & 0xFF:
        raise MemoryAccessError("bad checksum in %r" % line)
    return data[:-1]


def read_memory(serial_conn, addr, length, progress=None):
    """Read ``length`` bytes from ``addr``. ``progress(done, total)`` is called
    after every block. A failed block is retried by re-setting the pointer
    (an ``@G EN`` is never resent blindly: a lost reply already advanced it).
    """
    if addr < 0 or length < 0 or addr + length > MEMORY_SIZE:
        raise ValueError("range %04X+%d outside memory" % (addr, length))
    out = bytearray()
    end = addr + length
    pos = addr
    pointer_ok = False
    retries = 0
    while pos < end:
        try:
            if not pointer_ok or (pos - addr) % READ_CHUNK == 0:
                _set_read_pointer(serial_conn, pos)
                pointer_ok = True
            block = _read_block(serial_conn)
        except MemoryAccessError as e:
            retries += 1
            if retries > MAX_RETRIES:
                raise
            _LOGGER.warning("Memory read at %04X failed (%s); retrying.", pos, e)
            pointer_ok = False
            continue
        retries = 0
        take = min(BLOCK_SIZE, end - pos)
        out += block[:take]
        pos += take
        if progress:
            progress(pos - addr, length)
    return bytes(out)


def write_memory(serial_conn, addr, data):
    """Write ``data`` at ``addr`` (each 8-byte chunk gets its own pointer, so
    a retried chunk always lands at the right address), then read it back."""
    if not _restorable(addr, len(data)):
        raise ValueError("refusing to write %04X+%d: outside restorable ranges" % (addr, len(data)))
    for off in range(0, len(data), BLOCK_SIZE):
        chunk = data[off:off + BLOCK_SIZE]
        a = addr + off
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                _request(serial_conn, "@S ED%04X%02X" % (a, checksum([a >> 8, a & 0xFF])), "@A")
                _request(serial_conn, "@S EN%s%02X" % (chunk.hex().upper(), checksum(chunk)), "@A")
                break
            except MemoryAccessError as e:
                if attempt == MAX_RETRIES:
                    raise
                _LOGGER.warning("Memory write at %04X failed (%s); retrying.", a, e)
    readback = read_memory(serial_conn, addr, len(data))
    if readback != bytes(data):
        raise MemoryAccessError("read-back mismatch at %04X" % addr)


# --- restore planning --------------------------------------------------------

def restore_ranges():
    """The (start, end) ranges a restore may write, in address order."""
    ranges = []
    start = RESTORE_RANGE[0]
    for ex_start, ex_end, _ in sorted(RESTORE_EXCLUDED):
        if start < ex_start:
            ranges.append((start, ex_start))
        start = max(start, ex_end)
    if start < RESTORE_RANGE[1]:
        ranges.append((start, RESTORE_RANGE[1]))
    return ranges


def _restorable(addr, length):
    return any(s <= addr and addr + length <= e for s, e in restore_ranges())


def diff_runs(current, wanted, max_gap=4):
    """Contiguous (addr, new_bytes) runs where ``wanted`` differs from
    ``current`` inside the restore ranges. Runs closer than ``max_gap`` bytes
    are merged (rewriting a few equal bytes is cheaper than a new pointer)."""
    runs = []
    for start, end in restore_ranges():
        run_start = last = None
        for a in range(start, end):
            if current[a] == wanted[a]:
                continue
            if run_start is not None and a - last <= max_gap:
                last = a
                continue
            if run_start is not None:
                runs.append((run_start, bytes(wanted[run_start:last + 1])))
            run_start = last = a
        if run_start is not None:
            runs.append((run_start, bytes(wanted[run_start:last + 1])))
    return runs


def _name(data, base):
    return data[base:base + NAME_SIZE].split(b"\0", 1)[0].decode("latin-1").strip()


def _format(kind, value):
    if kind == "int":
        return str(value)
    if kind == "onoff":
        return "ON" if value else "OFF"
    if kind == "zone":
        return "none" if value == 0 else "zone %d" % value
    if kind == "hour":
        return _PERIOD_HOUR_CODES.get(value, "%d h" % value if value < 24 else "0x%02X" % value)
    if kind == "bcd":
        return "%02X min" % value
    if kind in ("lamp_flags", "outlet_flags"):
        table = LAMP_FLAGS if kind == "lamp_flags" else OUTLET_FLAGS
        names = [n for bit, n in table if value & bit]
        rest = value & ~sum(bit for bit, _ in table)
        if rest:
            names.append("0x%02X" % rest)
        return ", ".join(names) or "none"
    return "0x%02X" % value


def describe_changes(current, wanted):
    """Human-readable list of differences inside the restore ranges, one
    entry per field: {section, record, field, current, wanted, addr}."""
    changes = []
    tables = (
        ("Outlets", OUTLETS_BASE, OUTLETS_COUNT, OUTLET_FIELDS),
        ("Lamps", LAMPS_BASE, LAMPS_COUNT, LAMP_FIELDS),
    )
    seen = set()
    for section, base, count, fields in tables:
        for i in range(count):
            rec = base + i * RECORD_SIZE
            label = "%d %s" % (i + 1, _name(current, rec) or _name(wanted, rec))
            if current[rec:rec + NAME_SIZE] != wanted[rec:rec + NAME_SIZE]:
                changes.append({"section": section, "record": label, "field": "name",
                                "current": _name(current, rec), "wanted": _name(wanted, rec),
                                "addr": "%04X" % rec})
            seen.update(range(rec, rec + NAME_SIZE))
            for off, (field, kind) in sorted(fields.items()):
                a = rec + off
                seen.add(a)
                if current[a] != wanted[a] and _restorable(a, 1):
                    changes.append({"section": section, "record": label, "field": field,
                                    "current": _format(kind, current[a]),
                                    "wanted": _format(kind, wanted[a]),
                                    "addr": "%04X" % a})
    other = [a for s, e in restore_ranges() for a in range(s, e)
             if a not in seen and current[a] != wanted[a]]
    groups = []
    for a in other:
        if groups and a == groups[-1][1]:
            groups[-1][1] = a + 1
        else:
            groups.append([a, a + 1])
    for start, end in groups:
        changes.append({"section": "Other", "record": "%04X" % start,
                        "field": "%d byte(s)" % (end - start),
                        "current": _hexascii(current[start:end]),
                        "wanted": _hexascii(wanted[start:end]),
                        "addr": "%04X" % start})
    return changes


def _hexascii(data):
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
    return "%s  |%s|" % (data.hex(" "), text)


# --- jobs (backup / compare / apply) -----------------------------------------

class MemoryService(object):
    """Runs one memory job at a time in a background thread and keeps the
    backups directory. Thread-safe; the web panel polls ``status()``."""

    def __init__(self, backup_dir):
        self.backup_dir = backup_dir
        self._serial_conn = None
        self._lock = threading.Lock()
        self._job = None        # dict, see _start()
        self._plan = None       # pending restore: {id, source, runs, changes}
        self._cancel = threading.Event()
        # Held for a whole job and for every settings operation: an E read
        # in the middle of another one moves the controller's read pointer
        # (the other read would get valid blocks from the wrong address).
        self._op_lock = threading.Lock()
        self._restore_listeners = []

    def set_serial_conn(self, serial_conn):
        with self._lock:
            self._serial_conn = serial_conn

    @contextlib.contextmanager
    def exclusive(self, timeout):
        """Memory access outside the jobs (settings entities): waits up to
        ``timeout`` seconds for a running job; yields the serial connection."""
        if not self._op_lock.acquire(timeout=timeout):
            raise RuntimeError("the configuration memory is busy (backup or restore running)")
        try:
            if not reader_active() or self._serial_conn is None:
                raise RuntimeError("cardio2e is not connected")
            yield self._serial_conn
        finally:
            self._op_lock.release()

    def add_restore_listener(self, callback):
        """``callback()`` runs after every successful restore."""
        self._restore_listeners.append(callback)

    def cancel(self):
        """Stop the running backup or compare after the current block. A
        restore cannot be cancelled: stopping it halfway would leave the
        configuration partly written."""
        with self._lock:
            job = self._job
            if not job or job["state"] != "running":
                raise RuntimeError("no job is running")
            if not job["cancellable"]:
                raise RuntimeError("a restore cannot be cancelled")
            self._cancel.set()
        return dict(job)

    # -- status / files

    def status(self):
        with self._lock:
            job = dict(self._job) if self._job else None
            plan = None
            if self._plan:
                plan = {k: self._plan[k] for k in ("id", "source", "changes", "bytes")}
        return {"connected": reader_active(), "job": job, "plan": plan,
                "backups": self.list_backups()}

    def list_backups(self):
        try:
            names = os.listdir(self.backup_dir)
        except FileNotFoundError:
            return []
        out = []
        for name in sorted(names, reverse=True):
            path = os.path.join(self.backup_dir, name)
            if BACKUP_NAME_RE.match(name) and os.path.isfile(path):
                st = os.stat(path)
                out.append({"name": name, "size": st.st_size,
                            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")})
        return out

    def backup_path(self, name):
        """Path of an existing backup, or None for an invalid/unknown name."""
        if not BACKUP_NAME_RE.match(name or ""):
            return None
        path = os.path.join(self.backup_dir, name)
        return path if os.path.isfile(path) else None

    def delete_backup(self, name):
        """Delete a stored backup (the controller is not touched)."""
        path = self.backup_path(name)
        if path is None:
            raise ValueError("no such backup: %s" % name)
        os.remove(path)
        _LOGGER.info("Memory backup deleted: %s", name)

    def save_upload(self, filename, data):
        """Store an uploaded memory image; returns its backup name."""
        if len(data) != MEMORY_SIZE:
            raise ValueError("a memory image has %d bytes, got %d" % (MEMORY_SIZE, len(data)))
        stem = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.splitext(os.path.basename(filename or "upload"))[0])[:40]
        name = "uploaded-%s-%s.bin" % (_stamp(), stem or "upload")
        self._write_file(name, data)
        return name

    def _write_file(self, name, data):
        os.makedirs(self.backup_dir, exist_ok=True)
        tmp = os.path.join(self.backup_dir, "." + name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(self.backup_dir, name))

    # -- jobs

    def start_backup(self):
        return self._start("backup", MEMORY_SIZE, self._run_backup)

    def start_compare(self, name):
        path = self.backup_path(name)
        if path is None:
            raise ValueError("unknown backup %r" % name)
        with open(path, "rb") as f:
            wanted = f.read()
        if len(wanted) != MEMORY_SIZE:
            raise ValueError("%s is not a memory image (%d bytes)" % (name, len(wanted)))
        total = sum(e - s for s, e in restore_ranges())
        return self._start("compare", total, lambda job: self._run_compare(job, name, wanted))

    def start_apply(self, plan_id):
        with self._lock:
            plan = self._plan
        if not plan or plan["id"] != plan_id:
            raise ValueError("no such restore plan (compare again)")
        if not plan["runs"]:
            raise ValueError("nothing to restore")
        total = sum(len(d) for _, d in plan["runs"])
        return self._start("apply", total, lambda job: self._run_apply(job, plan))

    def _start(self, kind, total, target):
        with self._lock:
            if self._job and self._job["state"] == "running":
                raise RuntimeError("another job is running (%s)" % self._job["kind"])
            if not reader_active() or self._serial_conn is None:
                raise RuntimeError("cardio2e is not connected")
            if kind != "apply":
                self._plan = None
            self._cancel.clear()
            self._job = {"kind": kind, "state": "running", "done": 0, "total": total,
                         "message": "", "started": _now_iso(), "finished": None,
                         "cancellable": kind != "apply"}
            job = self._job
        threading.Thread(target=self._run, args=(job, target), daemon=True,
                         name="cardio2e-memory-%s" % kind).start()
        return dict(job)

    def _run(self, job, target):
        try:
            with self._op_lock:
                job["message"] = target(job) or ""
            job["state"] = "done"
            if job["kind"] == "apply":
                for callback in self._restore_listeners:
                    callback()
        except JobCancelled:
            _LOGGER.info("Memory %s cancelled.", job["kind"])
            job["state"] = "cancelled"
            job["message"] = "cancelled; nothing was written"
        except Exception as e:
            _LOGGER.error("Memory %s failed: %s", job["kind"], e)
            job["state"] = "error"
            job["message"] = str(e)
        job["finished"] = _now_iso()

    def _progress(self, job, offset=0):
        def update(done, _total):
            job["done"] = offset + done
            if job["cancellable"] and self._cancel.is_set():
                raise JobCancelled()
        return update

    def _run_backup(self, job):
        _LOGGER.info("Memory backup started (%d bytes).", MEMORY_SIZE)
        data = read_memory(self._serial_conn, 0, MEMORY_SIZE, self._progress(job))
        name = "cardio2e-%s.bin" % _stamp()
        self._write_file(name, data)
        _LOGGER.info("Memory backup saved: %s", name)
        return "saved %s" % name

    def _read_restore_ranges(self, job):
        """Current controller bytes in the restore ranges (zeros elsewhere)."""
        current = bytearray(MEMORY_SIZE)
        done = 0
        for start, end in restore_ranges():
            current[start:end] = read_memory(self._serial_conn, start, end - start,
                                             self._progress(job, done))
            done += end - start
        return bytes(current)

    def _run_compare(self, job, name, wanted):
        current = self._read_restore_ranges(job)
        runs = diff_runs(current, wanted)
        changes = describe_changes(current, wanted)
        plan = {"id": uuid.uuid4().hex, "source": name, "runs": runs, "changes": changes,
                "bytes": sum(len(d) for _, d in runs), "current": current}
        with self._lock:
            self._plan = plan
        return "%d change(s) in %d byte(s)" % (len(changes), plan["bytes"]) if runs else "no differences"

    def _run_apply(self, job, plan):
        # Keep what is being overwritten, so the restore itself can be undone
        # (only the restore ranges are meaningful in this file).
        undo = "before-restore-%s.bin" % _stamp()
        self._write_file(undo, plan["current"])
        done = 0
        for addr, data in plan["runs"]:
            _LOGGER.info("Restoring %d byte(s) at %04X.", len(data), addr)
            write_memory(self._serial_conn, addr, data)
            done += len(data)
            job["done"] = done
        with self._lock:
            if self._plan is plan:
                self._plan = None
        return "restored %d byte(s) from %s (undo file: %s)" % (done, plan["source"], undo)


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")
