"""Tests for the configuration memory protocol, restore planning and jobs."""

import os
import threading
import time

import pytest

import cardio2e_modules.cardio2e_memory as mem
import cardio2e_modules.cardio2e_serial as cs

from _fakes import FakeSerial


class FakeCardio(FakeSerial):
    """Answers the E command family from an in-memory image, delivering the
    replies the way the SerialReader would (through the pending registry)."""

    def __init__(self, image=None):
        super().__init__()
        self.mem = bytearray(image if image is not None else bytes(mem.MEMORY_SIZE))
        self.read_ptr = None
        self.write_ptr = None
        self.drop_en_replies = set()   # indexes of @G EN replies to lose
        self.en_count = 0
        self.commands = []

    def _reply(self, line):
        cs._deliver_to_pending(line.split(), line)

    def write(self, data):
        super().write(data)
        cmd = data.decode().rstrip("\r")
        self.commands.append(cmd)
        if cmd == "@G EN":
            block = bytes(self.mem[self.read_ptr:self.read_ptr + 8])
            self.read_ptr += 8
            idx = self.en_count
            self.en_count += 1
            if idx not in self.drop_en_replies:
                self._reply("@I E%s%02X" % (block.hex().upper(), mem.checksum(block)))
        elif cmd.startswith("@G E"):
            raw = bytes.fromhex(cmd[4:])
            assert mem.checksum(raw[:3]) == raw[3]
            self.read_ptr = (raw[0] << 8) | raw[1]
            self._reply("@A E")
        elif cmd.startswith("@S ED"):
            raw = bytes.fromhex(cmd[5:])
            assert mem.checksum(raw[:2]) == raw[2]
            self.write_ptr = (raw[0] << 8) | raw[1]
            self._reply("@A E")
        elif cmd.startswith("@S EN"):
            raw = bytes.fromhex(cmd[5:])
            assert sum(raw) & 0xFF == 0
            self.mem[self.write_ptr:self.write_ptr + len(raw) - 1] = raw[:-1]
            self.write_ptr += len(raw) - 1
            self._reply("@A E")
        elif cmd.startswith("@S ER"):
            raise AssertionError("@S ER resets the memory to factory defaults")
        return len(data)


@pytest.fixture
def reader_on(monkeypatch):
    monkeypatch.setattr(mem, "REQUEST_TIMEOUT", 0.2)
    monkeypatch.setattr(cs, "BULK_QUIET_PERIOD", 0)
    monkeypatch.setattr(cs, "BULK_COMMAND_PAUSE", 0)
    monkeypatch.setattr(cs, "MEMORY_MODE_EXIT", 0)
    cs._reader_active.set()
    yield
    cs._reader_active.clear()


def _image():
    """A memory image with recognisable bytes everywhere."""
    return bytes((i * 7 + (i >> 8)) & 0xFF for i in range(mem.MEMORY_SIZE))


def _lamp(n):
    return mem.LAMPS_BASE + (n - 1) * mem.RECORD_SIZE


def _outlet(n):
    return mem.OUTLETS_BASE + (n - 1) * mem.RECORD_SIZE


class TestChecksum:
    def test_matches_pccardio_frames(self):
        # From a PCCardio capture: "@S ED856E0D" and "@S EN32101200111685"
        assert mem.checksum([0x85, 0x6E]) == 0x0D
        assert mem.checksum(bytes.fromhex("321012001116")) == 0x85

    def test_sum_with_checksum_is_zero(self):
        data = bytes(range(1, 9))
        assert (sum(data) + mem.checksum(data)) & 0xFF == 0


class TestReadMemory:
    def test_reads_across_chunks_from_unaligned_address(self, reader_on):
        conn = FakeCardio(_image())
        data = mem.read_memory(conn, 0x1003, 300)
        assert data == _image()[0x1003:0x1003 + 300]
        # The pointer is set at the start and again at the 256-byte boundary.
        assert sum(1 for c in conn.commands if c.startswith("@G E") and c != "@G EN") == 2

    def test_lost_reply_is_retried_from_the_right_address(self, reader_on):
        conn = FakeCardio(_image())
        conn.drop_en_replies = {3}
        assert mem.read_memory(conn, 0x2000, 64) == _image()[0x2000:0x2040]

    def test_progress_reports_bytes(self, reader_on):
        seen = []
        mem.read_memory(FakeCardio(_image()), 0, 20, lambda d, t: seen.append((d, t)))
        assert seen == [(8, 20), (16, 20), (20, 20)]

    def test_gives_up_after_retries(self, reader_on):
        conn = FakeCardio(_image())
        conn.drop_en_replies = set(range(100))
        with pytest.raises(mem.MemoryAccessError):
            mem.read_memory(conn, 0, 8)

    def test_rejects_range_outside_memory(self, reader_on):
        with pytest.raises(ValueError):
            mem.read_memory(FakeCardio(), mem.MEMORY_SIZE - 4, 8)

    def test_bad_checksum_raises(self, reader_on, monkeypatch):
        conn = FakeCardio(_image())
        monkeypatch.setattr(mem, "MAX_RETRIES", 0)
        original = conn._reply
        conn._reply = lambda line: original(line[:-2] + "00" if line.startswith("@I E") else line)
        with pytest.raises(mem.MemoryAccessError):
            mem.read_memory(conn, 0x10, 8)


class TestWriteMemory:
    def test_writes_in_chunks_with_own_pointer_and_reads_back(self, reader_on):
        conn = FakeCardio()
        data = bytes(range(1, 20))
        mem.write_memory(conn, 0x5B30, data)
        assert bytes(conn.mem[0x5B30:0x5B30 + 19]) == data
        assert [c for c in conn.commands if c.startswith("@S ED")] == [
            "@S ED5B30%02X" % mem.checksum([0x5B, 0x30]),
            "@S ED5B38%02X" % mem.checksum([0x5B, 0x38]),
            "@S ED5B40%02X" % mem.checksum([0x5B, 0x40]),
        ]
        assert not any(c.startswith("@S ER") for c in conn.commands)

    @pytest.mark.parametrize("addr", [0x0000, 0x4EB9, 0x80AB, 0x8100, 0x818E, 0x7FC0, 0x8574])
    def test_refuses_protected_addresses(self, reader_on, addr):
        conn = FakeCardio()
        with pytest.raises(ValueError):
            mem.write_memory(conn, addr, b"\x01")
        assert conn.commands == []

    def test_refuses_write_spanning_an_exclusion(self, reader_on):
        with pytest.raises(ValueError):
            mem.write_memory(FakeCardio(), 0x80A0, bytes(16))


class TestBulkYields:
    def test_waits_for_quiet_bus_after_other_command(self, reader_on, monkeypatch):
        monkeypatch.setattr(cs, "BULK_QUIET_PERIOD", 0.3)
        conn = FakeCardio(_image())
        cs.send_command(conn, "R", 9, "O")
        start = time.monotonic()
        mem.read_memory(conn, 0, 8)
        assert time.monotonic() - start >= 0.3
        assert conn.commands[0] == "@S R 9 O"

    def test_received_message_pauses_bulk(self, reader_on, monkeypatch):
        monkeypatch.setattr(cs, "BULK_QUIET_PERIOD", 0.3)
        reader = cs.SerialReader(FakeSerial(), on_message=lambda msg, parts: None)
        reader._buffer = "@I L 5 100\r"
        reader._process_buffer()
        start = time.monotonic()
        mem.read_memory(FakeCardio(_image()), 0, 8)
        assert time.monotonic() - start >= 0.3

    def test_other_command_is_not_written_inside_an_exchange(self, reader_on):
        conn = FakeCardio(_image())
        order = []
        threads = []
        original = conn._reply

        def slow_reply(line):
            # While the controller is "answering", another thread tries to
            # send a relay command: it must wait for the reply.
            t = threading.Thread(target=lambda: (cs.send_command(conn, "R", 9, "O"), order.append("relay")))
            t.start()
            threads.append(t)
            time.sleep(0.1)
            order.append("reply")
            original(line)

        conn._reply = slow_reply
        mem._set_read_pointer(conn, 0x100)
        threads[0].join(2)
        assert order == ["reply", "relay"]
        assert conn.commands == ["@G E010008%02X" % mem.checksum([1, 0, 8]), "@S R 9 O"]

    @pytest.mark.parametrize("command, pauses", [
        ("@S R 9 O\r", True),             # action: the controller must leave memory mode
        ("@G R 9\r", False),              # query
        ("@S D 20261012220000\r", False),  # periodic date
    ])
    def test_action_command_pauses_bulk_traffic(self, reader_on, monkeypatch, command, pauses):
        monkeypatch.setattr(cs, "BULK_COMMAND_PAUSE", 0.3)
        cs._write(FakeCardio(), command)
        start = time.monotonic()
        cs.wait_for_quiet_bus()
        assert (time.monotonic() - start >= 0.25) == pauses


class TestMemoryModeHold:
    """The controller keeps its timers stopped for ~11 s after the last E
    exchange: an action command is held until then (a relay pulse would last
    that long and the outer gate ignores it). Queries are never held."""

    @pytest.mark.parametrize("command, held", [
        ("@S R 9 O\r", True),
        ("@G R 9\r", False),
        ("@S D 20261012220000\r", False),
    ])
    def test_hold_after_bulk_exchange(self, reader_on, monkeypatch, command, held):
        monkeypatch.setattr(cs, "MEMORY_MODE_EXIT", 0.3)
        monkeypatch.setattr(cs, "_last_bulk_exchange", time.monotonic())
        start = time.monotonic()
        cs._write(FakeCardio(), command)
        assert (time.monotonic() - start >= 0.25) == held

    def test_no_hold_without_memory_traffic(self, reader_on, monkeypatch):
        monkeypatch.setattr(cs, "MEMORY_MODE_EXIT", 0.3)
        monkeypatch.setattr(cs, "_last_bulk_exchange", 0.0)
        start = time.monotonic()
        cs._write(FakeCardio(), "@S R 9 O\r")
        assert time.monotonic() - start < 0.2


class TestRestorePlanning:
    def test_ranges_skip_exclusions(self):
        ranges = mem.restore_ranges()
        assert ranges[0][0] == 0x5B30 and ranges[-1][1] == 0x8574
        for start, end, _ in mem.RESTORE_EXCLUDED:
            assert not any(s < end and start < e for s, e in ranges)

    def test_diff_runs_merges_close_bytes_and_ignores_protected(self):
        current = bytearray(mem.MEMORY_SIZE)
        wanted = bytearray(current)
        wanted[0x5B3C] = 2
        wanted[0x5B3E] = 3            # 2 bytes away: merged into one run
        wanted[0x5C00] = 9            # far: own run
        wanted[0x80B0] = 1            # codes: never restored
        wanted[0x100] = 1             # outside the configuration area
        assert mem.diff_runs(current, wanted) == [
            (0x5B3C, bytes([2, 0, 3])),
            (0x5C00, bytes([9])),
        ]

    def test_describe_lamp_and_outlet_fields(self):
        current = bytearray(mem.MEMORY_SIZE)
        lamp = _lamp(22)
        current[lamp:lamp + 21] = bytes.fromhex("4c616d702332322000" "1010000a00640500000000" "00")
        wanted = bytearray(current)
        # Lamp 22 as PCCardio wrote it: blink on doorbell, zone 2,
        # intensity 37, duration 29, active 1:23 -> 4:56.
        wanted[lamp + 11:lamp + 20] = bytes.fromhex("010a02251d01230456")
        outlet = _outlet(9)
        current[outlet:outlet + 9] = b"PortaoEx\0"
        wanted[outlet:outlet + 9] = b"PortaoEx\0"
        current[outlet + 12] = 5
        wanted[outlet + 12] = 2
        changes = {(c["record"], c["field"]): (c["current"], c["wanted"])
                   for c in mem.describe_changes(current, wanted)}
        assert changes[("22 Lamp#22", "options")] == ("none", "blink on doorbell")
        assert changes[("22 Lamp#22", "pulse zone")] == ("none", "zone 2")
        assert changes[("22 Lamp#22", "intensity (%)")] == ("100", "37")
        assert changes[("22 Lamp#22", "active from (minute)")] == ("00 min", "23 min")
        assert changes[("9 PortaoEx", "auto off (s)")] == ("5", "2")
        assert len(changes) == 9

    def test_describe_sun_period_flags_and_other(self):
        current = bytearray(mem.MEMORY_SIZE)
        wanted = bytearray(current)
        lamp = _lamp(23)
        wanted[lamp + 11] = 0x04 | 0x10
        wanted[lamp + 16] = 0xE0
        wanted[lamp + 18] = 0xD0
        wanted[0x7EC0:0x7EC4] = b"Sala"
        changes = mem.describe_changes(current, wanted)
        by_field = {c["field"]: c["wanted"] for c in changes if c["section"] == "Lamps"}
        assert by_field["options"] == "sunrise/sunset reaction, blink on intrusion"
        assert by_field["active from (hour)"] == "sunset"
        assert by_field["active to (hour)"] == "sunrise"
        other = [c for c in changes if c["section"] == "Other"]
        assert len(other) == 1 and other[0]["record"] == "7EC0" and "|Sala|" in other[0]["wanted"]


def _wait(service, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        job = service.status()["job"]
        if job and job["state"] != "running":
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class TestMemoryService:
    def test_backup_compare_apply(self, reader_on, tmp_path):
        conn = FakeCardio(_image())
        service = mem.MemoryService(str(tmp_path))
        service.set_serial_conn(conn)

        service.start_backup()
        job = _wait(service, 60)
        assert job["state"] == "done", job
        [backup] = service.list_backups()
        with open(service.backup_path(backup["name"]), "rb") as f:
            assert f.read() == _image()

        # The controller changes; restoring the backup puts it back.
        conn.mem[_outlet(9) + 12] ^= 0xFF
        conn.mem[0x80B0] ^= 0xFF          # a code: must stay as it is now
        service.start_compare(backup["name"])
        assert _wait(service, 60)["state"] == "done"
        plan = service.status()["plan"]
        assert plan["bytes"] == 1
        assert [c["field"] for c in plan["changes"]] == ["auto off (s)"]

        service.start_apply(plan["id"])
        job = _wait(service)
        assert job["state"] == "done", job
        assert conn.mem[_outlet(9) + 12] == _image()[_outlet(9) + 12]
        assert conn.mem[0x80B0] == _image()[0x80B0] ^ 0xFF
        assert service.status()["plan"] is None
        assert any(b["name"].startswith("before-restore-") for b in service.list_backups())

    def test_apply_needs_current_plan(self, reader_on, tmp_path):
        service = mem.MemoryService(str(tmp_path))
        service.set_serial_conn(FakeCardio())
        with pytest.raises(ValueError):
            service.start_apply("nope")

    def test_jobs_need_connection(self, tmp_path):
        service = mem.MemoryService(str(tmp_path))
        service.set_serial_conn(FakeCardio())
        with pytest.raises(RuntimeError):
            service.start_backup()

    def test_one_job_at_a_time(self, reader_on, tmp_path, monkeypatch):
        service = mem.MemoryService(str(tmp_path))
        service.set_serial_conn(FakeCardio())
        monkeypatch.setattr(service, "_run_backup", lambda job: time.sleep(0.3))
        service.start_backup()
        with pytest.raises(RuntimeError):
            service.start_backup()
        _wait(service)

    def test_cancel_backup_saves_nothing(self, reader_on, tmp_path):
        service = mem.MemoryService(str(tmp_path))
        service.set_serial_conn(FakeCardio(_image()))
        service.start_backup()
        service.cancel()
        job = _wait(service, 60)
        assert job["state"] == "cancelled", job
        assert job["done"] < mem.MEMORY_SIZE
        assert service.list_backups() == []
        service.start_backup()           # the next job is not cancelled
        assert _wait(service, 60)["state"] == "done"

    def test_restore_cannot_be_cancelled(self, reader_on, tmp_path, monkeypatch):
        service = mem.MemoryService(str(tmp_path))
        service.set_serial_conn(FakeCardio())
        with pytest.raises(RuntimeError):
            service.cancel()             # nothing running
        monkeypatch.setattr(mem.MemoryService, "_run_apply", lambda self, job, plan: time.sleep(0.3))
        service._plan = {"id": "p", "source": "x.bin", "runs": [(0x5B30, b"x")], "changes": [], "bytes": 1}
        service.start_apply("p")
        with pytest.raises(RuntimeError):
            service.cancel()
        assert _wait(service)["state"] == "done"

    def test_delete_backup(self, tmp_path):
        (tmp_path / "cardio2e-1.bin").write_bytes(b"x")
        service = mem.MemoryService(str(tmp_path))
        service.delete_backup("cardio2e-1.bin")
        assert service.list_backups() == []
        for bad in ("cardio2e-1.bin", "../x.bin"):
            with pytest.raises(ValueError):
                service.delete_backup(bad)

    def test_upload_checks_size_and_sanitises_name(self, tmp_path):
        service = mem.MemoryService(str(tmp_path))
        with pytest.raises(ValueError):
            service.save_upload("x.bin", b"short")
        name = service.save_upload("../../etc/my casa.bin", bytes(mem.MEMORY_SIZE))
        assert name.startswith("uploaded-") and name.endswith("-my_casa.bin")
        assert os.path.isfile(os.path.join(str(tmp_path), name))

    @pytest.mark.parametrize("name", ["../x.bin", "a/b.bin", ".hidden.bin", "x.txt", ""])
    def test_backup_path_rejects_bad_names(self, tmp_path, name):
        assert mem.MemoryService(str(tmp_path)).backup_path(name) is None

    def test_compare_rejects_non_image(self, tmp_path):
        service = mem.MemoryService(str(tmp_path))
        (tmp_path / "small.bin").write_bytes(b"123")
        with pytest.raises(ValueError):
            service.start_compare("small.bin")


class TestBulkSessionWindow:
    def test_session_opens_with_a_bulk_request_and_expires(self, reader_on, monkeypatch):
        monkeypatch.setattr(cs, "_last_bulk", 0.0)
        assert not cs.bulk_session_active()
        conn = FakeCardio()
        mem.read_memory(conn, 0x5B30, 8)
        assert cs.bulk_session_active()
        assert not cs.bulk_session_active(time.monotonic() + cs.BULK_SESSION_WINDOW + 0.1)
