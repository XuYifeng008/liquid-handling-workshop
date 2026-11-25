"""Regression tests against the real COM9 captures, plus stream failure cases."""

import contextlib
import csv
import io
import json
import sys
import tempfile
import time
import unittest
from collections import deque
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rs232_scale import FrameDecoder, FrameIssue, Reading, ScaleReader, _StableWindow, main


ZERO = b"ST,GS,+  0.000 g\r\n"
POSITIVE = b"ST,GS,+  4.754 g\r\n"
NEGATIVE = b"ST,GS,-  4.753 g\r\n"
UNSTABLE = b"US,GS,+  4.753 g\r\n"
ROOT = Path(__file__).resolve().parent


class FakeSerial:
    """Chunks become available as new data; stale is pre-existing RX data."""

    def __init__(self, chunks=(), stale=b"", **settings):
        self.chunks = deque(chunks)
        self.stale = stale
        self.timeout = 0.1
        self.settings = settings
        self.reset_count = 0
        self.closed = False

    def open(self):
        self.closed = False

    def close(self):
        self.closed = True

    def reset_input_buffer(self):
        self.reset_count += 1
        self.stale = b""

    @property
    def in_waiting(self):
        return len(self.stale) if self.stale else (len(self.chunks[0]) if self.chunks else 0)

    def read(self, size):
        if self.stale:
            data, self.stale = self.stale[:size], self.stale[size:]
            return data
        if self.chunks:
            data = self.chunks.popleft()
            if len(data) > size:
                self.chunks.appendleft(data[size:])
            return data[:size]
        time.sleep(min(self.timeout, 0.002))
        return b""


class DecoderTests(unittest.TestCase):
    def test_every_captured_frame_against_saved_csv_with_different_chunking(self):
        with (ROOT / "rs232-verification/frames.csv").open(encoding="utf-8-sig", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(len(rows), 1486)
        captures = sorted((ROOT / "rs232-verification/captures").glob("*/*.bin"))
        self.assertEqual(len(captures), 4)
        for size in (1, 7, 64, 4096):
            total = negative = unstable = discarded = 0
            for path in captures:
                decoder = FrameDecoder()
                data = path.read_bytes()
                decoded = []
                for offset in range(0, len(data), size):
                    for event in decoder.feed(data[offset:offset + size]):
                        if isinstance(event, Reading):
                            decoded.append(event)
                expected = [r for r in rows if r["file"] == path.parent.name]
                self.assertEqual(len(decoded), len(expected), (path, size))
                for actual, saved in zip(decoded, expected):
                    self.assertEqual(actual.raw, saved["ascii"].encode("ascii") + b"\r\n")
                    self.assertEqual(actual.grams, Decimal(saved["value"]))
                    self.assertEqual(actual.status, saved["status"])
                    self.assertEqual(actual.mode, saved["mode"])
                self.assertEqual(decoder.buffer, b"")
                total += len(decoded)
                negative += sum(r.grams.is_signed() for r in decoded)
                unstable += sum(not r.stable for r in decoded)
                discarded += decoder.discarded_bytes
            self.assertEqual((total, negative, unstable, discarded), (1486, 212, 131, 1))

    def test_split_crlf_prefix_and_multiple_records(self):
        decoder = FrameDecoder()
        self.assertEqual(list(decoder.feed(b"\x00" + POSITIVE[:-1])), [])
        events = list(decoder.feed(b"\n" + NEGATIVE + ZERO))
        self.assertIsInstance(events[0], FrameIssue)
        self.assertEqual(events[0].raw, b"\x00")
        readings = [r for r in events if isinstance(r, Reading)]
        self.assertEqual([r.grams for r in readings], [Decimal("4.754"), Decimal("-4.753"), Decimal("0.000")])
        self.assertEqual(readings[1].milligrams, Decimal("-4753"))

    def test_unknown_and_corrupt_records_are_not_weights(self):
        records = [
            POSITIVE.replace(b"GS", b"NT"), POSITIVE.replace(b"ST", b"OL"),
            POSITIVE.replace(b" g", b"kg"), POSITIVE.replace(b"4.754", b"4\x00754"),
            b"ST,GS,+ 4.7540 g\r\n",
        ]
        decoder = FrameDecoder()
        events = list(decoder.feed(b"".join(records) + ZERO))
        self.assertEqual(len([e for e in events if isinstance(e, FrameIssue)]), len(records))
        self.assertEqual([e.raw for e in events if isinstance(e, Reading)], [ZERO])
        self.assertEqual(decoder.discarded_bytes, sum(map(len, records)))

    def test_unterminated_garbage_is_bounded_and_next_frame_recovers(self):
        decoder = FrameDecoder()
        issues = list(decoder.feed(b"X" * 10000))
        self.assertTrue(all(isinstance(e, FrameIssue) for e in issues))
        self.assertLessEqual(len(decoder.buffer), 17)
        events = list(decoder.feed(NEGATIVE))
        self.assertEqual(events[-1].raw, NEGATIVE)
        self.assertEqual(decoder.discarded_bytes, 10000)


class StableWindowTests(unittest.TestCase):
    def sample(self, value, status="ST", at=0):
        return Reading(Decimal(value), status, "GS", ZERO, monotonic_s=at)

    def test_uses_whole_window_range_not_just_adjacent_difference(self):
        window = _StableWindow(3, "0.002", 1)
        # Each adjacent difference is <= .002, but the entire window drifts .004.
        self.assertFalse(window.push(self.sample("1.000", at=0.0), 0))
        self.assertFalse(window.push(self.sample("1.002", at=0.1), 0))
        self.assertFalse(window.push(self.sample("1.004", at=0.2), 0))
        self.assertTrue(window.push(self.sample("1.003", at=0.3), 0))

    def test_unstable_invalid_data_and_long_gap_reset_confirmation(self):
        window = _StableWindow(2, "0", 0.5)
        self.assertFalse(window.push(self.sample("0", at=0.0), 0))
        self.assertFalse(window.push(self.sample("0", "US", 0.1), 0))
        self.assertFalse(window.push(self.sample("0", at=0.2), 0))
        self.assertFalse(window.push(self.sample("0", at=0.3), 1))
        self.assertTrue(window.push(self.sample("0", at=0.4), 1))
        self.assertFalse(window.push(self.sample("0", at=1.1), 1))
        self.assertTrue(window.push(self.sample("0", at=1.2), 1))

    def test_bad_settings_are_rejected(self):
        for count, tolerance, gap in [(0, "0", 1), (True, "0", 1), (1, "NaN", 1),
                                      (1, "-1", 1), (1, "x", 1), (1, "0", float("inf"))]:
            with self.assertRaises(ValueError):
                _StableWindow(count, tolerance, gap)


class ReaderTests(unittest.TestCase):
    @contextlib.contextmanager
    def connect(self, fake, prefix=None):
        def factory(**settings):
            fake.settings.update(settings)
            return fake
        with patch.dict(sys.modules, {"serial": SimpleNamespace(Serial=factory)}):
            with ScaleReader(log_prefix=prefix) as scale:
                yield scale

    def test_no_tx_settings_fresh_data_and_log_round_trip(self):
        fake = FakeSerial([b"\x00" + POSITIVE[:5], POSITIVE[5:] + UNSTABLE + NEGATIVE], stale=ZERO)
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "run"
            with self.connect(fake, prefix) as scale, contextlib.redirect_stderr(io.StringIO()):
                stream = scale.readings(duration=0.1)
                result = [next(stream), next(stream), next(stream)]
                stream.close()
            self.assertEqual(fake.reset_count, 1)
            self.assertEqual((fake.port, fake.dtr, fake.rts, fake.closed), ("COM9", False, False, True))
            self.assertEqual({k: fake.settings[k] for k in ("baudrate", "bytesize", "parity", "stopbits", "xonxoff", "rtscts", "dsrdtr")},
                             dict(baudrate=9600, bytesize=8, parity="N", stopbits=1, xonxoff=False, rtscts=False, dsrdtr=False))
            self.assertEqual([r.raw for r in result], [POSITIVE, UNSTABLE, NEGATIVE])
            self.assertEqual(Path(str(prefix) + ".bin").read_bytes(), b"\x00" + POSITIVE + UNSTABLE + NEGATIVE)
            with Path(str(prefix) + ".csv").open(encoding="utf-8-sig", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual([r["grams"] for r in rows], ["4.754", "4.753", "-4.753"])
            events = [json.loads(line) for line in Path(str(prefix) + ".events.jsonl").read_text().splitlines()]
            reconstructed = b"".join(bytes.fromhex(e["hex"]) for e in events if e["event"] == "rx")
            self.assertEqual(reconstructed, Path(str(prefix) + ".bin").read_bytes())
            self.assertIn("discarded_prefix", [e["event"] for e in events])

    def test_wait_stable_does_not_use_old_buffer_or_count_across_invalid_frames(self):
        fake = FakeSerial([ZERO * 2, b"bad\r\n", POSITIVE * 2, UNSTABLE, NEGATIVE * 3], stale=ZERO * 10)
        with self.connect(fake) as scale, contextlib.redirect_stderr(io.StringIO()):
            result = scale.wait_stable(timeout=0.2, consecutive=3, tolerance_g="0")
        self.assertEqual(result.raw, NEGATIVE)
        self.assertTrue(fake.closed)

    def test_silence_and_invalid_bytes_do_not_return_a_stale_weight(self):
        fake = FakeSerial([b"bad\r\n", POSITIVE.replace(b"GS", b"NT")])
        with self.connect(fake) as scale, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(TimeoutError, "No valid weight"):
                list(scale.readings(idle_timeout=0.01))
        self.assertTrue(fake.closed)

    def test_stable_wait_has_total_deadline_even_with_valid_us_frames(self):
        class RepeatingUS(FakeSerial):
            @property
            def in_waiting(self):
                return len(UNSTABLE)

            def read(self, _size):
                time.sleep(0.001)
                return UNSTABLE

        with self.connect(RepeatingUS()) as scale:
            with self.assertRaisesRegex(TimeoutError, "No stable window"):
                scale.wait_stable(timeout=0.02)

    def test_existing_log_is_not_overwritten_and_port_closes_on_failure(self):
        fake = FakeSerial()
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "run"
            path = Path(str(prefix) + ".csv")
            path.write_text("existing measurement")
            with self.assertRaises(FileExistsError):
                with self.connect(fake, prefix):
                    self.fail("Should reject an existing log")
            self.assertEqual(path.read_text(), "existing measurement")
            self.assertTrue(fake.closed)

    def test_cli_count_output_and_invalid_options(self):
        fake = FakeSerial([UNSTABLE, POSITIVE, NEGATIVE])
        factory = lambda **kwargs: fake
        output = io.StringIO()
        with patch.dict(sys.modules, {"serial": SimpleNamespace(Serial=factory)}), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--stable-only", "--count", "2"]), 0)
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        self.assertIn("-4.753 g", output.getvalue())
        self.assertTrue(fake.closed)
        with contextlib.redirect_stderr(io.StringIO()):
            for options in (["--count", "0"], ["--seconds", "nan"], ["--stable", "--seconds", "1"]):
                with self.assertRaises(SystemExit) as result:
                    main(options)
                self.assertEqual(result.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
