import logging
import math
import re
import threading
import time
from decimal import Decimal

from .geometry import LAYOUT, POINTS, TIPS, TUBES, grid_point, validate_profile, validate_rack
from .hardware import DEFAULTS, Rig
from .science import BLINDS, LIQUIDS, VOLUMES, correction, summary
from .store import Store

log = logging.getLogger(__name__)
DEFAULT_PORTS = dict(xyz="COM13", pipette="COM11", scale="COM12")
JOG_DISTANCES_MM = (.1, .5, 1, 5, 10, 50)
AIR_VOLUME = 50
DEFAULT_LIQUID_OPTIONS = {"prewet":False, "air":False}
NAMES = LIQUIDS + BLINDS
PHASES = {"cal":"六点标定", "blind":"盲样测量", "verify":"200 μL 复测"}
OPERATIONS = {"connect","disconnect","initialize","read","jog","lift","capture","hover","accept_station",
              "confirm_height","teach_pick","teach_discard","teach_eject","tips","liquid_option","new_session","open_session","run","redo","guess","recover","void"}


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{label}须为 {low}–{high} 的整数")
    return value


def pairing(values):
    if not isinstance(values, dict) or set(values) != set(BLINDS) or set(values.values()) != set(LIQUIDS):
        raise ValueError("U1、U2、U3 必须与三种液体一一对应")
    return values


class Station:
    def __init__(self, database, rig=None):
        self.store, self.rig = Store(database), rig or Rig()
        self.guard = threading.RLock()
        self.busy = False
        self.stop_in_progress = False
        self.job = 0
        self.step = "等待操作"
        self.error = ""
        self.progress = None
        self.connected = self.approved = False
        self.position = self.weight = self.tip = self.clearance = None
        self.readback = {}
        # Earlier, unreferenced encoder points cannot be replayed against a new home.
        self.points_key = "profile_" + LAYOUT["frame"]
        profile = self.store.load_profile(LAYOUT["frame"], LAYOUT["initial_points"])
        self.points, self.sources = profile["points"], profile["sources"]
        self.ports = self.store.setting("ports", DEFAULT_PORTS)
        self.liquid_options = self.store.setting("liquid_options",DEFAULT_LIQUID_OPTIONS)
        self.requests = {}

    def state(self):
        with self.guard:
            state = dict(busy=self.busy or self.stop_in_progress, job=self.job, step=self.step, error=self.error,
                         progress=self.progress, connected=self.connected, initialized=self.rig.initialized,
                         homed=self.rig.homed, approved=self.approved, clearance=self.clearance, position=self.position,
                         weight=self.weight, tip=self.tip, points=dict(self.points), sources=dict(self.sources), ports=self.ports,
                         readback=self.readback, jog_distances_mm=JOG_DISTANCES_MM, liquid_options=dict(self.liquid_options),
                         home_method=dict(self.rig.home_method) if self.rig.homed else {})
        sess = self.store.session()
        rows = self.store.records(sess["id"]) if sess else []
        pending = self.store.pending()
        for row in rows:
            row.pop("station", None)
        if pending:
            pending.pop("station", None)
        state.update(session=sess, records=rows, analysis=summary(sess,rows) if sess else {},
                     pending=pending, sessions=self.store.session_list(), tips=self.store.setting("tip_index",0))
        return state

    def start(self, name, data, request_id):
        if name not in OPERATIONS:
            raise ValueError("未知操作")
        with self.guard:
            if request_id in self.requests:
                return self.requests[request_id]
            if self.busy or self.stop_in_progress:
                raise ValueError("上一项操作正在进行")
            self.busy = True
            self.job += 1
            job = self.job
            self.step, self.error, self.progress = "正在执行", "", None
            self.requests[request_id] = job
            if len(self.requests) > 256:
                self.requests.pop(next(iter(self.requests)))
        threading.Thread(target=self._worker, args=(name,data), daemon=True).start()
        return job

    def _worker(self, name, d):
        try:
            self.execute(name,d)
        except Exception as exc:
            log.warning("%s failed: %s", name, exc, exc_info=True)
            with self.guard:
                self.error = str(exc) or type(exc).__name__
                self.step = "已暂停"
        finally:
            with self.guard:
                self.busy = False

    def stop(self):
        with self.guard:
            if self.stop_in_progress:
                return
            self.stop_in_progress = True
            self.approved = False
            self.step = "正在停止"
            self.rig.cancel.set()
        try:
            self.rig.stop()
            self.step = "已请求停止三轴，请核对现场"
        finally:
            with self.guard:
                self.stop_in_progress = False

    def require_link(self):
        if not self.connected:
            raise ValueError("请先连接并检查三台设备")
        self.rig.check()

    def homing_progress(self, message):
        self.step = message
        log.info(message)

    def current(self, data):
        sess = self.store.session()
        if not sess or data.get("session") != sess["id"]:
            raise ValueError("课堂已切换，请刷新后选择当前课堂")
        return sess

    def execute(self, op, d):
        if op == "liquid_option":
            key,value = d.get("key"),d.get("enabled")
            if key not in DEFAULT_LIQUID_OPTIONS or type(value) is not bool:
                raise ValueError("移液动作设置无效")
            options = {**self.liquid_options,key:value}
            self.store.set("liquid_options",options)
            self.liquid_options = options
            self.step = "移液动作设置已保存"
            return
        if op in ("connect","disconnect"):
            self.connected = self.approved = False
            self.position = self.tip = self.weight = self.clearance = None
            self.readback = {}
            self.rig.close()
            if op == "disconnect":
                self.step = "设备已断开"
                return
            ports = {k:str(d.get("ports",self.ports).get(k,"")).strip().upper() for k in DEFAULT_PORTS}
            if len(set(ports.values())) != 3 or not all(re.fullmatch(r"COM[1-9]\d{0,2}",v) for v in ports.values()):
                raise ValueError("请指定三个不同的 COM 串口")
            self.step = "正在查询三轴、枪头传感器和稳定重量"
            tip, reading = self.rig.connect(ports)
            try:
                self.position = self.rig.position()
            except Exception:
                self.rig.close()
                raise
            self.ports, self.tip, self.weight = ports, tip, float(reading.grams)
            self.store.set("ports",ports)
            self.connected, self.step = True, "三台设备通信正常"
            return
        if op in ("new_session","open_session","tips"):
            if self.store.pending():
                raise ValueError("请先处理上一笔未结束的测量")
            if op == "new_session":
                self.new_session(d)
            elif op == "open_session":
                sid = integer(d.get("id"),1,2147483647,"课堂编号")
                if not self.store.session(sid):
                    raise ValueError("课堂不存在")
                self.store.set("session",sid)
                self.step = "已打开课堂"
            else:
                index = TIPS.index(d.get("next",""))
                self.store.set("tip_index",index)
                self.step = f"下一个新枪头为 {TIPS[index]}"
            return
        if op == "guess":
            sess = self.current(d)
            if self.store.pending():
                raise ValueError("请先处理未结束的测量")
            stats = summary(sess,self.store.records(sess["id"]))
            if len(stats["scores"]) != 3:
                raise ValueError("请先为三个盲样各完成至少一笔测量")
            self.store.reveal(sess["id"],pairing(d.get("pair")))
            self.step = "配对已提交，可以揭晓答案"
            return
        if op == "void":
            pending = self.store.pending()
            if not pending or d.get("id") != pending["id"] or not d.get("confirmed"):
                raise ValueError("请先核对现场和待作废的记录")
            self.store.update(pending["id"],status="void",step="经现场核对作废")
            self.step = "本笔已作废，原始记录已保留"
            return
        self.require_link()
        if op == "initialize":
            if not d.get("confirmed"):
                raise ValueError("请确认空枪且回零路径畅通")
            self.approved = False
            self.clearance = None
            self.position = None
            self.readback = {}
            self.rig.initialize(self.homing_progress)
            try:
                self.position = self.rig.position()
                self.readback = self.rig.read_defaults()
                self.tip = self.rig.tip()
            except Exception:
                self.rig.initialized = False
                raise
            self.use_saved_points()
            self.step = ("三轴已回零，已载入完整工位" if self.approved else
                         "三轴已回零，旧工位点已载入，仅待标定天平接收瓶")
        elif op == "read":
            self.position = self.rig.position()
            self.tip = self.rig.tip()
            self.weight = float(self.rig.weigh().grams)
            self.step = "已取得新鲜稳定读数"
        elif op in ("jog","lift","capture","hover","accept_station","confirm_height","teach_pick","teach_discard","teach_eject"):
            self.calibrate(op,d)
        elif op == "recover":
            row = self.store.pending()
            if not row or row["id"] != d.get("id") or not d.get("confirmed"):
                raise ValueError("请核对并选择待处理记录")
            if row["status"] in ("dispensed","waiting_mass"):
                self.finish_weight(row["id"],row["pre"])
            elif row["status"] != "measured":
                raise ValueError("动作结果不确定，请现场核对并作废该笔")
            self.finish_tip(row["id"])
        elif op == "run":
            self.run(self.current(d),d)
        elif op == "redo":
            self.run(self.current(d),{"phase":"cal"},redo_id=integer(d.get("id"),1,2147483647,"记录编号"))

    def new_session(self,d):
        name = str(d.get("name","")).strip()
        if not 1 <= len(name) <= 60:
            raise ValueError("请输入 1–60 字的课堂名称")
        wells, densities = d.get("wells",{}), d.get("densities",{})
        if set(wells) != set(NAMES) or len(set(wells.values())) != 6 or not set(wells.values()) <= set(TUBES):
            raise ValueError("请为六支样品选择六个不同的有效试管位")
        if set(densities) != set(LIQUIDS):
            raise ValueError("请填写三种已知液的密度")
        rho = {k:float(v) for k,v in densities.items()}
        if not all(math.isfinite(v) and v>0 for v in rho.values()):
            raise ValueError("密度必须为正数，单位 g/mL")
        setup = dict(name=name,wells=wells,densities=rho,repeats=integer(d.get("repeats"),1,999,"重复次数"))
        self.store.create(setup,pairing(d.get("answer")))
        self.step = "课堂已建立，答案已封存"

    def use_saved_points(self):
        self.approved = False
        self.clearance = self.points["safe"][2]
        if "receiver" in self.points:
            validate_profile(self.points)
            self.approved = self.rig.homed and self.rig.initialized

    def calibrate(self,op,d):
        if op == "teach_eject":
            if not d.get("confirmed"):
                raise ValueError("请确认枪头在废头容器开口内")
            self.rig.eject()
            self.tip = False
            self.step = "已确认退头"
            return
        if not self.rig.homed:
            raise ValueError("请先完成本次三轴回零，再标定或移动")
        if op in ("teach_pick","teach_discard"):
            if self.store.pending() or not self.rig.initialized:
                raise ValueError("请先完成初始化并处理待结束的实验记录")
            safe = self.points["safe"][2]
            if op == "teach_pick":
                if self.rig.tip():
                    raise ValueError("已有枪头，可直接标定天平接收瓶")
                validate_rack(self.points,"tip")
                tip = self.store.take_teaching_tip(TIPS)
                self.step = "取标定枪头 "+tip
                self.rig.travel(grid_point(self.points,"tip",tip),safe)
                self.rig.confirm_tip(True)
            else:
                if not self.rig.tip():
                    raise ValueError("当前为空枪")
                self.step = "移至 C03 退标定枪头"
                self.rig.travel(self.points["waste"],safe)
                self.rig.eject()
            self.rig.height(safe)
            self.position, self.tip = self.rig.position(), self.rig.tip()
            self.step = "标定枪头已装好，可定位接收瓶" if op=="teach_pick" else "标定枪头已退出"
            return
        pos = self.rig.position()
        self.position = pos
        if op == "lift":
            if self.clearance is None:
                raise ValueError("请先记录或核对横移高度")
            if pos[2] > self.clearance:
                self.rig.height(self.clearance)
            self.position = self.rig.position()
            self.step = "已到达横移高度"
        elif op == "jog":
            axis, distance = d.get("axis"), d.get("delta")
            if axis not in ("x","y","z") or type(distance) not in (int,float) or abs(distance) not in JOG_DISTANCES_MM:
                raise ValueError("点动距离无效")
            self.rig.axis(axis,pos["xyz".index(axis)]+distance)
            self.position = self.rig.position()
            self.step = "点动已到位"
        elif op == "capture":
            key = d.get("key")
            if key not in POINTS:
                raise ValueError("点位名称无效")
            if self.store.pending():
                raise ValueError("请先处理未结束的测量再改标定")
            if key in ("tip_a","tube_a","receiver","waste") and pos[2] <= self.points["safe"][2]:
                raise ValueError("当前仍在横移面或其上方，请点动 Z 到实际操作高度后再记录")
            if key == "safe" and any(self.points[k][2] <= pos[2] for k in
                                     ("tip_a","tube_a","receiver","waste") if k in self.points):
                raise ValueError("横移面须高于所有操作点，请先抬高 Z")
            if key == "tip_a":
                self.tip = self.rig.tip()
                if not self.tip:
                    raise ValueError("A01 须用空枪小步压入，待传感器确认已装头后记录最终 Z")
                if self.store.setting("tip_index",0) == 0:
                    self.store.set("tip_index",1)
            if key in ("tube_a","receiver","waste") and not self.rig.tip():
                raise ValueError("请装上标定枪头后记录此操作高度")
            self.points = {**self.points,key:pos}
            self.sources = {**self.sources,key:"现场记录"}
            self.store.set(self.points_key,dict(points=self.points,sources=self.sources))
            self.use_saved_points()
            self.step = ("天平接收瓶排液位已保存，完整工位已启用" if key == "receiver" and self.approved
                         else "已保存实测点位")
        elif op == "confirm_height":
            safe = self.points.get("safe")
            if not safe or pos[2] > safe[2] + .2:
                raise ValueError("请用 Z− 抬到已存横移面或更高，并现场核对")
            self.clearance = safe[2]
            self.step = "已核对本次横移高度"
        elif op == "hover":
            if self.clearance is None:
                raise ValueError("请先记录或核对横移高度")
            key = d.get("key")
            if key in ("tip_last","tube_last"):
                kind = key.split("_")[0]
                target = grid_point(self.points,kind,(TIPS if kind=="tip" else TUBES)[-1])
            else:
                target = self.points.get(key)
            if target is None:
                raise ValueError("此点须先由现场点动定位")
            self.rig.travel(target,self.clearance,hover=True)
            self.position = self.rig.position()
            self.step = "已在横移高度到达点位上方"
        else:
            validate_profile(self.points)
            if self.clearance is None or not d.get("confirmed"):
                raise ValueError("请先核对安全横移面与工位实际点位")
            self.approved = True
            self.step = "当前工位已核对，可开始实操"

    def plan(self, sess, d):
        phase = d.get("phase")
        rows = self.store.records(sess["id"])
        good = [r for r in rows if r["status"]=="done"]
        stats = summary(sess,rows)
        if phase == "cal":
            which = d.get("sample","all")
            if which not in (*LIQUIDS,"all"):
                raise ValueError("请选择已知液")
            plan = []
            for sample in LIQUIDS if which=="all" else (which,):
                for value in VOLUMES:
                    count = sum(r["phase"]=="cal" and r["sample"]==sample and r["command"]==value for r in good)
                    plan.extend([(sample,value)]*max(0,sess["repeats"]-count))
            return plan
        if len(stats["curves"]) != 3:
            raise ValueError("请先完成三种已知液的六点标定并取得有效拟合")
        if phase == "blind":
            if sess.get("guess"):
                raise ValueError("本场盲样已揭晓")
            sample = d.get("sample")
            if sample not in BLINDS:
                raise ValueError("请选择盲样")
            value = integer(d.get("volume"),10,1000,"盲测体积")
            return [(sample,value)]*integer(d.get("repeats"),1,999,"重复次数")
        if phase == "verify" and sess.get("guess"):
            return [(k,correction(stats["curves"][k])) for k in LIQUIDS
                    if not any(r["phase"]=="verify" and r["sample"]==k for r in good)]
        raise ValueError("请先提交盲样配对并揭晓")

    def run(self,sess,d,redo_id=None):
        if not self.rig.homed or not self.rig.initialized:
            raise ValueError("请先完成三轴回零并初始化")
        if not self.approved:
            validate_profile(self.points)
            raise ValueError("请重新初始化以载入保存的工位")
        if self.store.pending():
            raise ValueError("请先处理未结束的测量")
        if self.rig.tip():
            raise ValueError("移液器上已有枪头，请在废头位退头")
        redo_of = None
        phase = d["phase"]
        if redo_id is not None:
            original = self.store.redo_target(sess["id"],redo_id)
            redo_of = original["id"]
            plan = [(original["sample"],original["command"])]
        else:
            plan = self.plan(sess,d)
        if not plan:
            self.step = "本阶段计划已完成"
            return
        if self.liquid_options["air"] and any(q + AIR_VOLUME > 1000 for _,q in plan):
            raise ValueError("吸入 50 μL 空气时，液体设定量最多为 950 μL")
        for i,(sample,command) in enumerate(plan,1):
            self.rig.check()
            self.progress = dict(index=i,total=len(plan),sample=sample,command=command)
            self.transfer(sess,phase,sample,command,redo_of)
        self.step = f"已完成标定记录 #{redo_of} 的重做" if redo_of else PHASES[phase]+"已完成"

    def mark(self,aid,step,**fields):
        self.step = step
        self.store.update(aid,step=step,**fields)

    def transfer(self,sess,phase,sample,command,redo_of=None):
        if self.rig.tip():
            raise ValueError("传感器检测到已有枪头，已暂停")
        well = sess["wells"][sample]
        rho = sess["densities"].get(sample)
        station = dict(frame=LAYOUT["frame"],home_steps=self.rig.home_steps,home_method=self.rig.home_method,points=self.points,sources=self.sources,
                       ports=self.ports,defaults=DEFAULTS,readback=self.readback,liquid_options=dict(self.liquid_options))
        aid, tip = self.store.reserve(sess["id"],phase,sample,well,command,200 if phase=="verify" else None,
                                      rho,TIPS,station,redo_of)
        safe = self.points["safe"][2]
        try:
            self.mark(aid,"取新枪头 "+tip)
            self.rig.travel(grid_point(self.points,"tip",tip),safe)
            self.rig.confirm_tip(True)
            self.tip = True
            self.rig.height(safe)
            self.mark(aid,"读取加液前稳定质量")
            pre = self.rig.weigh()
            self.weight = float(pre.grams)
            self.mark(aid,"进入源管 "+well,pre=self.weight,before_time=pre.received_at.isoformat(),
                      before_raw=pre.raw.hex(" "))
            source = grid_point(self.points,"tube",well)
            air = self.liquid_options["air"]
            self.rig.travel(source,safe,hover=air)
            if air:
                self.draw_air_and_lower(aid,source[2])
            if self.liquid_options["prewet"]:
                self.mark(aid,"润洗枪头 · 吸入后排回源管")
                self.rig.liquid("P",command)
                time.sleep(.2)
                self.rig.liquid("D",command)
                time.sleep(.2)
            self.mark(aid,"从 "+well+" 正式吸液")
            self.rig.liquid("P",command)
            self.mark(aid,"移至天平接收瓶")
            self.rig.travel(self.points["receiver"],safe)
            self.mark(aid,"向接收瓶排液",status="dispensing")
            self.rig.liquid("D",command)
            if air:
                self.mark(aid,"在接收瓶排出 50 μL 空气")
                self.rig.liquid("D",AIR_VOLUME)
            self.mark(aid,"已排液，正在退出接收瓶",status="dispensed")
            self.rig.height(safe)
            self.finish_weight(aid,float(pre.grams))
            self.finish_tip(aid)
        except Exception as exc:
            row = next(r for r in self.store.records(sess["id"]) if r["id"]==aid)
            status = row["status"]
            if status not in ("dispensed","waiting_mass","measured"):
                status = "uncertain"
            self.store.update(aid,status=status,error=str(exc))
            raise

    def draw_air_and_lower(self,aid,z):
        self.mark(aid,"在源管上方吸入 50 μL 空气")
        self.rig.liquid("P",AIR_VOLUME)
        self.rig.height(z)

    def finish_weight(self,aid,pre):
        self.mark(aid,"已排液，等候稳定后重",status="waiting_mass")
        reading = self.rig.weigh()
        net = reading.grams - Decimal(str(pre))
        self.weight = float(reading.grams)
        if net <= 0:
            raise ValueError("后重没有增加，请核对容器和读数，可补读或作废")
        self.mark(aid,"已保存后重，待退头",status="measured",post=self.weight,net=float(net),
                  after_time=reading.received_at.isoformat(),after_raw=reading.raw.hex(" "),error="")

    def finish_tip(self,aid):
        if self.rig.tip():
            if not self.approved or not self.rig.initialized:
                raise ValueError("重量已保存。请在废头位退头，再点击完成本笔")
            self.step = "在 C03 废头容器退头"
            self.rig.travel(self.points["waste"],self.points["safe"][2])
            self.rig.eject()
            self.rig.height(self.points["safe"][2])
        self.tip = False
        self.position = self.rig.position()
        self.mark(aid,"本笔完成",status="done",error="")
