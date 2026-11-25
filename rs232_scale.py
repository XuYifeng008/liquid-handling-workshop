"""COM9 scale reader for the captured 9600/8N1, ST/US, GS, gram protocol.

Receive only; use the instrument's buttons for tare. Python 3.10+.
Run ``python rs232_scale.py --help`` or import ScaleReader.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator


FRAME_SIZE = 18
FRAME_PATTERN = re.compile(rb"(ST|US),GS,([+-])( *[0-9]+\.[0-9]{3}) g\r\n")


def _positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")
    return value


@dataclass(frozen=True)
class Reading:
    """One complete received frame. GS is opaque, not a gross/net decision."""

    grams: Decimal
    status: str
    mode: str
    raw: bytes
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    monotonic_s: float = field(default_factory=time.monotonic, repr=False)

    @property
    def stable(self) -> bool:
        return self.status == "ST"

    @property
    def milligrams(self) -> Decimal:
        return self.grams * 1000


@dataclass(frozen=True)
class FrameIssue:
    reason: str
    raw: bytes


class FrameDecoder:
    """Decode partial/multiple frames; report every discarded byte.

    An arbitrary prefix (including the observed startup 00) may precede a
    valid 18-byte frame. It is reported rather than stripped inside a frame.
    Unknown status/unit/mode/precision is rejected, never guessed.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.discarded_bytes = 0

    def _issue(self, reason: str, data: bytes) -> FrameIssue:
        self.discarded_bytes += len(data)
        return FrameIssue(reason, data)

    def feed(self, data: bytes) -> Iterator[Reading | FrameIssue]:
        self.buffer.extend(data)
        while True:
            end = self.buffer.find(b"\r\n")
            if end < 0:
                break
            record = bytes(self.buffer[: end + 2])
            del self.buffer[: end + 2]
            candidate = record[-FRAME_SIZE:]
            match = FRAME_PATTERN.fullmatch(candidate) if len(candidate) == FRAME_SIZE else None
            if match is None:
                yield self._issue("unrecognized_frame", record)
                continue
            if len(record) > FRAME_SIZE:
                yield self._issue("discarded_prefix", record[:-FRAME_SIZE])
            sign, number = match.group(2), match.group(3).strip()
            yield Reading(
                grams=Decimal((sign + number).decode("ascii")),
                status=match.group(1).decode("ascii"),
                mode="GS",
                raw=candidate,
            )
        if len(self.buffer) > 4096:
            # Keep enough bytes for a partial known frame to finish next read.
            dropped = bytes(self.buffer[: -(FRAME_SIZE - 1)])
            del self.buffer[: -(FRAME_SIZE - 1)]
            yield self._issue("buffer_without_crlf", dropped)


class _StableWindow:
    def __init__(self, consecutive: int, tolerance_g: Decimal | str, max_gap: float):
        if isinstance(consecutive, bool) or not isinstance(consecutive, int) or consecutive < 1:
            raise ValueError("consecutive must be a positive integer")
        try:
            self.tolerance = Decimal(str(tolerance_g))
        except InvalidOperation as exc:
            raise ValueError("tolerance_g must be a finite nonnegative number") from exc
        if not self.tolerance.is_finite() or self.tolerance < 0:
            raise ValueError("tolerance_g must be a finite nonnegative number")
        self.max_gap = _positive(max_gap, "max_gap")
        self.window: deque[Reading] = deque(maxlen=consecutive)
        self.consecutive = consecutive
        self.last_discarded = 0

    def push(self, reading: Reading, discarded_bytes: int) -> bool:
        if (discarded_bytes != self.last_discarded or
                (self.window and reading.monotonic_s - self.window[-1].monotonic_s > self.max_gap)):
            self.window.clear()
        self.last_discarded = discarded_bytes
        if not reading.stable:
            self.window.clear()
            return False
        self.window.append(reading)
        values = [r.grams for r in self.window]
        return len(values) == self.consecutive and max(values) - min(values) <= self.tolerance


class ScaleReader:
    """Receive-only instrument connection; use as a context manager.

    Logging is optional. log_prefix creates .csv, .bin and .events.jsonl
    files using exclusive creation, so previous experiment data is preserved.
    All valid ST and US frames are logged, even when the CLI filters output.
    A connection supports one active reading operation at a time.
    """

    def __init__(self, port: str = "COM9", log_prefix: str | Path | None = None):
        self.port_name = port
        self.log_prefix = Path(log_prefix) if log_prefix else None
        self._serial = None
        self._stack: ExitStack | None = None
        self._decoder = FrameDecoder()
        self._csv = self._raw = self._events = None
        self._writer = None

    def __enter__(self) -> ScaleReader:
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def open(self) -> ScaleReader:
        if self._serial is not None:
            raise RuntimeError("Connection is already open")
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("Install dependency: python -m pip install pyserial==3.5") from exc
        stack = ExitStack()
        try:
            # Defer opening until DTR/RTS are explicitly disabled.
            connection = serial.Serial(
                port=None, baudrate=9600, bytesize=8, parity="N", stopbits=1,
                timeout=0.1, xonxoff=False, rtscts=False, dsrdtr=False,
            )
            stack.callback(connection.close)
            connection.dtr = False
            connection.rts = False
            connection.port = self.port_name
            connection.open()
            if self.log_prefix:
                self.log_prefix.parent.mkdir(parents=True, exist_ok=True)
                prefix = str(self.log_prefix)
                self._csv = stack.enter_context(open(prefix + ".csv", "x", newline="", encoding="utf-8-sig"))
                self._raw = stack.enter_context(open(prefix + ".bin", "xb"))
                self._events = stack.enter_context(open(prefix + ".events.jsonl", "x", encoding="utf-8"))
                self._writer = csv.writer(self._csv)
                self._writer.writerow(["received_at_utc", "status", "mode", "grams", "milligrams", "raw_hex"])
                self._csv.flush()
            self._decoder = FrameDecoder()
            self._serial = connection
            self._stack = stack
        except BaseException:
            stack.close()
            self._csv = self._raw = self._events = self._writer = None
            raise
        return self

    def close(self) -> None:
        try:
            if self._stack:
                self._stack.close()
        finally:
            self._stack = self._serial = None
            self._csv = self._raw = self._events = self._writer = None

    def _event(self, kind: str, data: bytes = b"", **extra) -> None:
        if self._events:
            self._events.write(json.dumps({
                "utc": datetime.now(timezone.utc).isoformat(),
                "event": kind, "hex": data.hex(" "), **extra,
            }) + "\n")
            self._events.flush()

    def readings(self, *, idle_timeout: float = 3.0,
                 duration: float | None = None, fresh: bool = True) -> Iterator[Reading]:
        """Yield new readings; raise TimeoutError if no valid frame arrives.

        fresh=True discards host-side old buffered input before starting;
        this sends no command. duration limits this iteration in seconds.
        Log files contain all bytes actually read, after any buffer discard.
        """
        _positive(idle_timeout, "idle_timeout")
        if duration is not None:
            _positive(duration, "duration")
        if self._serial is None:
            raise RuntimeError("Use 'with ScaleReader(...) as scale:' first")
        if fresh:
            self._serial.reset_input_buffer()
            self._decoder = FrameDecoder()
            self._event("fresh_start", note="Host receive buffer discarded; no device command sent")
        started = last_valid = time.monotonic()
        deadline = started + duration if duration is not None else math.inf
        while True:
            now = time.monotonic()
            if now >= deadline:
                return
            remaining_idle = idle_timeout - (now - last_valid)
            if remaining_idle <= 0:
                raise TimeoutError(f"No valid weight frame for {idle_timeout:g} seconds on {self.port_name}")
            self._serial.timeout = min(0.1, remaining_idle, deadline - now)
            data = self._serial.read(min(max(self._serial.in_waiting, 1), 4096))
            if not data:
                continue
            if self._raw:
                self._raw.write(data)
                self._raw.flush()
            self._event("rx", data)
            for event in self._decoder.feed(data):
                if isinstance(event, FrameIssue):
                    self._event(event.reason, event.raw)
                    print(f"Discarded {len(event.raw)} bytes ({event.reason}): {event.raw.hex(' ')}", file=sys.stderr)
                    continue
                last_valid = event.monotonic_s
                if self._writer:
                    self._writer.writerow([
                        event.received_at.isoformat(), event.status, event.mode,
                        str(event.grams), str(event.milligrams), event.raw.hex(" "),
                    ])
                    self._csv.flush()
                yield event

    def wait_stable(self, *, timeout: float = 15.0, consecutive: int = 5,
                    tolerance_g: Decimal | str = "0.002", max_gap: float = 0.5) -> Reading:
        """Return the latest sample in a stable window, not an average.

        The window resets on US, discarded bytes or a gap > max_gap seconds.
        Always starts with fresh input. Thresholds are application settings,
        not manufacturer guarantees of stability or weighing accuracy.
        """
        _positive(timeout, "timeout")
        window = _StableWindow(consecutive, tolerance_g, max_gap)
        stream = self.readings(duration=timeout, idle_timeout=min(3.0, timeout), fresh=True)
        try:
            for reading in stream:
                if window.push(reading, self._decoder.discarded_bytes):
                    return reading
        finally:
            stream.close()
        raise TimeoutError(f"No stable window within {timeout:g} seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="已验证的称重串口读取：9600/8N1，ST/US，GS，g；只接收数据。")
    parser.add_argument("--port", default="COM9", help="串口，默认 COM9")
    parser.add_argument("--stable", action="store_true", help="等待一组稳定读数，输出最新一帧并退出")
    parser.add_argument("--stable-only", action="store_true", help="连续显示时只显示 ST，日志仍保存全部读数")
    parser.add_argument("--seconds", type=float, help="连续读取时长；默认持续至 Ctrl+C")
    parser.add_argument("--count", type=int, help="显示指定数量的读数后退出")
    parser.add_argument("--timeout", type=float, default=15, help="--stable 的总等待时间，默认 15 秒")
    parser.add_argument("--idle-timeout", type=float, default=3, help="连续读取无合法帧超时，默认 3 秒")
    parser.add_argument("--consecutive", type=int, default=5, help="稳定窗口帧数，默认 5")
    parser.add_argument("--tolerance-g", default="0.002", help="窗口允许的最大极差，单位 g，默认 0.002")
    parser.add_argument("--max-gap", type=float, default=0.5, help="稳定窗口内帧的最大接收间隔，默认 0.5 秒")
    parser.add_argument("--log-prefix", type=Path, help="保存 PREFIX.csv、PREFIX.bin 和 PREFIX.events.jsonl；不覆盖已有文件")
    args = parser.parse_args(argv)
    try:
        _positive(args.timeout, "timeout")
        _positive(args.idle_timeout, "idle_timeout")
        _StableWindow(args.consecutive, args.tolerance_g, args.max_gap)
        if args.seconds is not None:
            _positive(args.seconds, "seconds")
        if args.count is not None and args.count < 1:
            raise ValueError("count must be greater than zero")
        if args.stable and (args.seconds is not None or args.count is not None or args.stable_only):
            raise ValueError("--stable cannot be combined with --seconds, --count or --stable-only")
    except ValueError as exc:
        parser.error(str(exc))

    def show(reading: Reading) -> None:
        print(f"{reading.received_at.isoformat()}  {reading.status} {reading.mode}  "
              f"{reading.grams:+.3f} g  ({reading.milligrams:+f} mg)", flush=True)

    try:
        with ScaleReader(args.port, args.log_prefix) as scale:
            if args.stable:
                show(scale.wait_stable(timeout=args.timeout, consecutive=args.consecutive,
                                       tolerance_g=args.tolerance_g, max_gap=args.max_gap))
            else:
                count = 0
                stream = scale.readings(duration=args.seconds, idle_timeout=args.idle_timeout)
                try:
                    for reading in stream:
                        if args.stable_only and not reading.stable:
                            continue
                        show(reading)
                        count += 1
                        if args.count is not None and count >= args.count:
                            break
                finally:
                    stream.close()
                if count == 0:
                    raise TimeoutError("No readings matched the requested filter during this run")
    except KeyboardInterrupt:
        print("Stopped; serial port and logs closed.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
