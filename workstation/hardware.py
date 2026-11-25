"""Serial device adapters for the LaiYu workshop (no Uni-Lab runtime)."""
import logging
import threading
import time
from contextlib import ExitStack

import serial
from rs232_scale import ScaleReader
from .geometry import LAYOUT, bounds
from .legacy.xyz_stepper_driver import MotorAxis, MotorStatus, XYZStepperController

log = logging.getLogger(__name__)
STEPS = {"x": 204.8, "y": 204.8, "z": 3276.8}
AXIS_SETTINGS = LAYOUT["motion"]
DEFAULTS = ("a30000", "b200", "c200", "s2000", "$4", "!20", "%350", "C1000", "j1.0", "m0", "k200")
MOTION = "a30000b200c200s2000"
ERRORS = {1:"前一个动作未完成", 2:"未初始化", 3:"过载", 4:"无效指令", 5:"液位检测故障",
          13:"空吸", 14:"堵针", 16:"泡沫", 17:"超量程"}


def motor_rpm(name, speed_mm_s, *, homing=False):
    """Use the nearest whole rpm for travel and preserve the homing feed."""
    rpm = speed_mm_s * STEPS[name] * 60 / XYZStepperController.STEPS_PER_REVOLUTION
    return min(int(rpm) if homing else round(rpm), AXIS_SETTINGS["max_rpm"][name])


class SOPA:
    def __init__(self, port):
        self.serial = serial.Serial(None, 115200, timeout=.08, write_timeout=.7)
        self.serial.dtr = self.serial.rts = False
        self.serial.port = port
        self.serial.open()
        time.sleep(.25)

    def exchange(self, command, expected=None, timeout=8):
        """Consume complete, addressed, checksummed 13-byte frames. Never resend an action."""
        body = ("/4" + command + "E").encode("ascii")
        s = self.serial
        s.reset_input_buffer()
        s.write(body + bytes([sum(body) & 255]))
        s.flush()
        end = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < end:
            buf.extend(s.read(s.in_waiting or 1))
            while len(buf) >= 13:
                if buf[:2] != b"/\x04" or buf[11] != 69 or sum(buf[:12]) & 255 != buf[12]:
                    del buf[0]
                    continue
                frame = bytes(buf[:13])
                del buf[:13]
                log.debug("SOPA %s RX %s", command, frame.hex(" "))
                if frame[2] == 6:
                    status = frame[3]
                    if status == 10:  # receipt ACK only
                        continue
                    if status:
                        raise RuntimeError(f"SOPA {command} 返回 {status:02X} {ERRORS.get(status, '设备异常')}")
                    if expected is None:
                        return
                elif frame[2] == expected:
                    return frame[3:11].rstrip(b"\0").decode("ascii")
        raise TimeoutError(f"SOPA {command} 等待{'数据' if expected else '完成回执'}超时")

    def query(self, command, tag):
        for attempt in range(3):
            try:
                return self.exchange(command, ord(tag), timeout=1.3)
            except (TimeoutError, RuntimeError):
                if attempt == 2:
                    raise
                time.sleep(.15)

    def tip(self):
        value = self.query("Q28", "T")
        if value not in ("0", "1"):
            raise RuntimeError(f"枪头检测返回未知值 {value!r}")
        return value == "1"

    def liquid(self, op, amount):
        if op not in ("P", "D") or not 10 <= amount <= 1000 or int(amount) != amount:
            raise ValueError("移液指令需要 10–1000 μL 整数体积")
        self.exchange(f"{MOTION}{op}{amount}", timeout=12 + amount/100)


class Rig:
    def __init__(self):
        self.stack = None
        self.xyz = self.pipette = self.scale = None
        self.cancel = threading.Event()
        self.command_lock = threading.RLock()
        self.initialized = False
        self.axes_enabled = False
        self.home_steps = {}
        self.home_method = {}

    @property
    def homed(self):
        return len(self.home_steps) == 3

    def connect(self, ports):
        self.close()
        stack = ExitStack()
        try:
            self.xyz = XYZStepperController(ports["xyz"], timeout=.55)
            if not self.xyz.connect():
                raise RuntimeError(f"无法打开三轴 {ports['xyz']}，请检查串口占用")
            stack.callback(self.xyz.disconnect)
            self.position()
            self.pipette = SOPA(ports["pipette"])
            stack.callback(self.pipette.serial.close)
            tip = self.pipette.tip()
            self.scale = stack.enter_context(ScaleReader(ports["scale"]))
            reading = self.scale.wait_stable(timeout=12)
        except BaseException:
            stack.close()
            self.xyz = self.pipette = self.scale = None
            raise
        self.stack = stack
        self.cancel.clear()
        return tip, reading

    def close(self):
        if self.stack:
            self.stack.close()
        self.stack = self.xyz = self.pipette = self.scale = None
        self.initialized = False
        self.axes_enabled = False
        self.home_steps = {}

    def check(self):
        if self.cancel.is_set():
            raise RuntimeError("运行已停止，请核对现场后重新连接设备")

    def initialize(self, progress):
        self.initialized = False
        self.home_steps = {}
        self.home_method = {}
        if self.pipette.tip():
            raise ValueError("回零前请先卸下枪头，确认 Z 向上及 X、Y 的回零路径畅通")
        for axis in MotorAxis:
            with self.command_lock:
                self.check()
                if not self.xyz.enable_motor(axis, True):
                    raise RuntimeError(f"{axis.name} 轴使能失败")
        self.axes_enabled = True
        found = {}
        try:
            for name in ("z", "x", "y"):
                progress(f"{name.upper()} 轴正在向负限位回零")
                found[name] = self._home_axis(name, progress)
        except Exception:
            self.stop()
            raise
        self.home_steps = found
        progress("三轴回零已完成，正在加载移液器参数")
        for cmd in ("H", *DEFAULTS):
            self.check()
            self.pipette.exchange(cmd, timeout=15)
        self.initialized = True

    def _home_axis(self, name, progress):
        """Seek in negative velocity mode; a limit signal or sustained position plateau ends the search."""
        axis = MotorAxis[name.upper()]
        scale = STEPS[name]
        span = LAYOUT["limits"][name][1]
        rpm = motor_rpm(name, AXIS_SETTINGS["homing_speed_mm_s"], homing=True)
        first = self.xyz.get_motor_status(axis)
        if first.status == MotorStatus.COLLISION_STOP:
            raise RuntimeError(f"{axis.name} 轴碰撞停，请检查后再回零")
        found = first.steps if first.status == MotorStatus.REVERSE_LIMIT_STOP else None
        method = "负光电限位"
        if found is None:
            with self.command_lock:
                self.check()
                if not self.xyz.set_speed_mode(axis, -rpm, AXIS_SETTINGS["acceleration_rpm_s"]):
                    raise RuntimeError(f"{axis.name} 轴未确认回零速度指令")
            last_steps, last_move = first.steps, time.monotonic()
            deadline = last_move + AXIS_SETTINGS["homing_timeout_s"]
            while time.monotonic() < deadline:
                self.check()
                state = self.xyz.get_motor_status(axis)
                observed = time.monotonic()
                if observed >= deadline:
                    break
                if state.status == MotorStatus.REVERSE_LIMIT_STOP:
                    found = state.steps
                    break
                if state.status in (MotorStatus.COLLISION_STOP, MotorStatus.FORWARD_LIMIT_STOP):
                    raise RuntimeError(f"{axis.name} 轴回零遇到 {state.status.name}，请复核路径")
                travelled = (first.steps-state.steps)/scale
                if travelled < -.6 or travelled > span+1:
                    raise RuntimeError(f"{axis.name} 轴回零方向或行程异常")
                if abs(state.steps-last_steps)/scale > .08:
                    last_steps, last_move = state.steps, observed
                elif observed-last_move >= AXIS_SETTINGS["homing_stable_time_s"]:
                    found = state.steps
                    method = "寻零位置持续稳定"
                    break
                time.sleep(min(AXIS_SETTINGS["homing_check_interval_s"], deadline-observed))
        if found is None:
            raise TimeoutError(f"{axis.name} 轴寻找负限位超时，未建立原点")
        with self.command_lock:
            self.check()
            if not self.xyz.emergency_stop(axis):
                raise RuntimeError(f"{axis.name} 轴回零停止应答缺失")
        time.sleep(.15)
        clearance = AXIS_SETTINGS["homing_clearance_mm"]
        progress(f"{axis.name} 轴{method}已确认，退离 {clearance:g} mm")
        self._move_targets({name:(found + round(clearance*scale),rpm)},release=True)
        self.home_method[name] = method
        log.info("Homed %s: %s at %d encoder steps",axis.name,method,found)
        return found

    def read_defaults(self):
        return {cmd:self.pipette.query(cmd,tag) for cmd,tag in
                (("Q01","a"),("Q02","b"),("Q03","c"),("Q06","s"),("Q29","j"))}

    def position(self):
        result = []
        for axis in MotorAxis:
            state = self.xyz.get_motor_status(axis)
            allowed = (MotorStatus.STANDBY,) if self.homed else (
                MotorStatus.STANDBY, MotorStatus.REVERSE_LIMIT_STOP, MotorStatus.FORWARD_LIMIT_STOP)
            if state.status not in allowed:
                raise RuntimeError(f"{axis.name} 轴状态 {state.status.name}，请核对现场")
            name = axis.name.lower()
            result.append(round((state.steps-self.home_steps.get(name,0))/STEPS[name], 4))
        return result

    def move_axes(self, positions):
        self.check()
        if not self.axes_enabled or not self.homed:
            raise ValueError("请先完成本次三轴回零")
        targets = {}
        for name,value in positions.items():
            target = self.home_steps[name] + round(bounds(name, value) * STEPS[name])
            factor = AXIS_SETTINGS["z_speed_factor" if name == "z" else "xy_speed_factor"]
            targets[name] = (target,motor_rpm(name, AXIS_SETTINGS["default_speed_mm_s"] * factor))
        try:
            self._move_targets(targets)
        except Exception:
            self.stop()
            raise

    def axis(self, name, value):
        self.move_axes({name:value})

    def _move_targets(self, targets, release=False):
        with self.command_lock:
            for name,(target,rpm) in targets.items():
                self.check()
                if not self.xyz.move_to_position(MotorAxis[name.upper()], target, rpm, AXIS_SETTINGS["acceleration_rpm_s"], precision=12):
                    raise RuntimeError(f"{name.upper()} 轴未确认目标指令")
        start = time.monotonic()
        end = start + 55
        while time.monotonic() < end:
            arrived = True
            for name,(target,_) in targets.items():
                self.check()
                state = self.xyz.get_motor_status(MotorAxis[name.upper()])
                if state.status == MotorStatus.STANDBY and abs(state.steps-target) <= max(14, STEPS[name]*.12):
                    continue
                arrived = False
                departing = release and state.status == MotorStatus.REVERSE_LIMIT_STOP and time.monotonic()-start < 2
                if state.status not in (MotorStatus.STANDBY, MotorStatus.RUNNING) and not departing:
                    raise RuntimeError(f"{name.upper()} 轴触发 {state.status.name}")
            if arrived:
                return
            time.sleep(.05)
        raise TimeoutError(f"{'/'.join(targets).upper()} 轴未确认到位")

    def stop(self):
        self.cancel.set()
        errors = []
        with self.command_lock:
            if self.xyz and self.xyz.serial_conn:
                for axis in MotorAxis:
                    try:
                        if not self.xyz.emergency_stop(axis):
                            errors.append(axis.name)
                    except Exception as exc:
                        errors.append(f"{axis.name} {exc}")
        self.initialized = False
        self.axes_enabled = False
        self.home_steps = {}
        if errors:
            raise RuntimeError("轴停止回执缺失 " + " / ".join(errors))

    def height(self, z):
        self.axis("z", z)

    def travel(self, target, safe, hover=False):
        self.height(safe)
        self.move_axes({"x":target[0],"y":target[1]})
        if not hover:
            self.height(target[2])

    def tip(self):
        return self.pipette.tip()

    def confirm_tip(self, expected):
        for _ in range(3):
            self.check()
            if self.tip() is expected:
                return
            time.sleep(.2)
        raise RuntimeError("取头后未检测到枪头" if expected else "退头后仍检测到枪头")

    def eject(self):
        self.check()
        self.pipette.exchange("R")
        self.confirm_tip(False)

    def liquid(self, op, amount):
        self.check()
        self.pipette.liquid(op, amount)

    def weigh(self):
        self.check()
        reading = self.scale.wait_stable()
        self.check()
        return reading
