"""Regression tests for the physical-attempt boundary and the classroom calculations."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workshop import csv_report, report
from workstation.control import Station
from workstation.geometry import LAYOUT, TIPS, TUBES, grid_point, validate_profile
from workstation.hardware import Rig, STEPS
from workstation.legacy.xyz_stepper_driver import MotorAxis, MotorStatus
from workstation.science import BLINDS, LIQUIDS, VOLUMES, correction, summary
from workstation.store import Store

POINTS = {k:[*xyz[:2],20] for k,xyz in LAYOUT["initial_points"].items()}
POINTS.update(safe=[0,0,1], receiver=[195,40,21])
DENSITIES = dict(zip(LIQUIDS,(1.0,1.128,1.165)))
WELLS = dict(zip((*LIQUIDS,*BLINDS),("A1","A2","A3","B1","B2","B3")))
ANSWER = dict(zip(BLINDS,(LIQUIDS[2],LIQUIDS[0],LIQUIDS[1])))
SETUP = dict(name="验收演练",wells=WELLS,densities=DENSITIES,repeats=1,answer=ANSWER)


class Bench:
    """Fixture only. The production server constructs Rig exclusively."""
    def __init__(self, points=POINTS):
        self.points = points
        self.initialized = True
        self.homed = True
        self.home_steps = {"x":0,"y":0,"z":0}
        self.home_method = dict.fromkeys("xyz","测试参考")
        self.cancel = threading.Event()
        self.attached = False
        self.pos = list(points["safe"])
        self.mass = Decimal("12.345")
        self.in_pipette = Decimal(0)
        self.air = 0
        self.liquid_events = []
        self.discharges = []
        self.fail_post = self.lose_ack = False

    def check(self):
        if self.cancel.is_set():
            raise ValueError("stopped")

    def initialize(self, progress):
        self.homed = self.initialized = True
        self.pos = [10,10,10]

    def read_defaults(self):
        return {}

    def position(self):
        return self.pos.copy()

    def height(self,z):
        self.pos[2] = z

    def travel(self, point, safe, hover=False):
        self.pos = list(point)
        if hover:
            self.height(safe)
            return
        if any(self.pos == grid_point(self.points,"tip",t) for t in TIPS):
            self.attached = True

    def tip(self):
        return self.attached

    def confirm_tip(self, expected):
        assert self.tip() == expected

    def eject(self):
        assert self.pos == self.points["waste"]
        self.attached = False

    def liquid(self,op,q):
        assert self.attached
        self.liquid_events.append((op,q,self.pos.copy()))
        if op == "P" and self.pos[2] == self.points["safe"][2]:
            assert self.air == 0 and self.in_pipette == 0
            self.air = q
        elif op == "D" and self.air and self.in_pipette == 0:
            assert self.air == q
            self.air = 0
        elif op == "P":
            name=next(n for n,w in WELLS.items() if grid_point(self.points,"tube",w)==self.pos)
            name=ANSWER.get(name,name)
            factor=(1.0,.88,1.055)[LIQUIDS.index(name)]
            self.in_pipette=Decimal(str(round(q*factor*DENSITIES[name]/1000,3)))
        elif self.pos == self.points["receiver"]:
            self.mass+=self.in_pipette
            self.in_pipette=Decimal(0)
            self.discharges.append(q)
            if self.lose_ack:
                raise TimeoutError("排液回执丢失")
        else:
            self.in_pipette=Decimal(0)

    def weigh(self):
        if self.fail_post and self.discharges:
            self.fail_post=False
            raise TimeoutError("等待稳定后重超时")
        return SimpleNamespace(grams=self.mass,raw=f"ST,GS,+{self.mass:7.3f} g\r\n".encode(),
                               received_at=datetime.now(timezone.utc))


class WorkshopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/"class.sqlite3"
        self.machine = Bench()
        self.app = Station(self.db,self.machine)
        self.app.connected = self.app.approved = True
        self.app.points = POINTS.copy()
        self.app.clearance = 1
        self.app.new_session(SETUP)
        self.sid = self.app.store.session()["id"]
        sleeper=patch("workstation.control.time")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def action(self,op,**payload):
        self.app.execute(op,dict(session=self.sid,**payload))

    def test_workshop(self):
        self.action("run",phase="cal",sample=LIQUIDS[0])
        self.action("run",phase="cal",sample="all")
        self.action("run",phase="cal",sample="all")
        self.assertEqual(len(self.machine.discharges),18)
        s=self.app.state()
        self.assertNotIn("answer",s["session"])
        self.assertNotIn("answer",report(self.app.store,self.sid)["session"])
        self.assertEqual(len(s["analysis"]["curves"]),3)
        for blind,q in zip(BLINDS,(500,300,800)):
            self.action("run",phase="blind",sample=blind,volume=q,repeats=2)
        self.assertDictEqual(self.app.state()["analysis"]["assignments"][0]["pair"],ANSWER)
        self.action("guess",pair=ANSWER)
        with self.assertRaises(ValueError):
            self.action("guess",pair=ANSWER)
        self.action("run",phase="verify")
        self.action("run",phase="verify")
        doc=report(self.app.store,self.sid)
        verification=[r for r in doc["records"] if r["phase"]=="verify"]
        self.assertEqual(len(verification),3)
        self.assertEqual(len(doc["records"]),27)
        self.assertEqual(len({r["tip"] for r in doc["records"]}),27)
        for r in verification:
            self.assertLess(r["absolute_error"],1.5)
            self.assertEqual(r["well"],WELLS[r["sample"]])
        self.assertIn("绝对误差",csv_report(doc).decode("utf-8-sig"))
        self.assertIn("before_raw",json.dumps(doc))
        self.assertTrue(all(r["pre"]>12 for r in doc["records"]))

    def test_liquid_switches_and_air_capacity(self):
        self.assertEqual(self.app.state()["liquid_options"],dict(prewet=False,air=False))
        source = grid_point(POINTS,"tube",WELLS[LIQUIDS[0]])
        receiver = POINTS["receiver"]
        for air in (False,True):
            for prewet in (False,True):
                self.action("liquid_option",key="air",enabled=air)
                self.action("liquid_option",key="prewet",enabled=prewet)
                self.machine.liquid_events.clear()
                sess = self.app.store.session()
                self.app.transfer(sess,"cal",LIQUIDS[0],950)
                expected = [("P",50,[*source[:2],1])] if air else []
                if prewet:
                    expected += [("P",950,source),("D",950,source)]
                expected += [("P",950,source),("D",950,receiver)]
                if air:
                    expected += [("D",50,receiver)]
                self.assertEqual(self.machine.liquid_events,expected)
                self.assertEqual(self.machine.air,0)
        restarted = Station(self.db,self.machine)
        self.assertEqual(restarted.liquid_options,dict(prewet=True,air=True))
        before = self.app.store.setting("tip_index",0)
        with patch.object(self.app,"plan",return_value=[("U1",951)]), self.assertRaisesRegex(ValueError,"950"):
            self.action("run",phase="blind")
        self.assertEqual(self.app.store.setting("tip_index",0),before)

    def test_redo_replaces_only_on_success_including_recovery(self):
        self.action("run",phase="cal",sample=LIQUIDS[0])
        record = self.app.store.records(self.sid)[3]
        with patch.object(self.app,"finish_weight",side_effect=TimeoutError), self.assertRaises(TimeoutError):
            self.action("redo",id=record["id"])
        pending = self.app.store.pending()
        self.assertEqual(self.app.store.records(self.sid)[3]["status"],"done")
        self.assertEqual(pending["redo_of"],record["id"])
        self.action("recover",id=pending["id"],confirmed=True)
        self.action("redo",id=record["id"])
        rows = self.app.store.records(self.sid)
        self.assertEqual(rows[3]["status"],"superseded")
        self.assertEqual(rows[-2]["status"],"superseded")
        self.assertEqual(rows[-1]["redo_of"],rows[-2]["id"])
        self.assertEqual((rows[-1]["command"],rows[-1]["well"]),(record["command"],record["well"]))
        self.assertEqual(len({r["tip"] for r in rows}),8)
        s = self.app.state()
        self.assertEqual(s["analysis"]["curves"][LIQUIDS[0]]["n"],6)
        self.assertEqual(self.app.plan(s["session"],dict(phase="cal",sample=LIQUIDS[0])),[])
        with self.assertRaisesRegex(ValueError,"本场"):
            self.app.store.redo_target(self.sid+1,record["id"])
        self.assertIn("重做原记录编号",csv_report(report(self.app.store,self.sid)).decode("utf-8-sig"))

    def test_mass_recovery_survives_restart_without_another_dose(self):
        self.machine.fail_post=True
        with self.assertRaises(TimeoutError):
            self.action("run",phase="cal",sample="all")
        waiting=self.app.store.pending()
        self.assertEqual(waiting["status"],"waiting_mass")
        restarted=Station(self.db,self.machine)
        restarted.points=POINTS
        restarted.connected=restarted.approved=True
        with self.assertRaises(ValueError):
            restarted.execute("run",dict(session=self.sid,phase="cal"))
        restarted.execute("recover",dict(id=waiting["id"],confirmed=True))
        self.assertEqual(self.machine.discharges,[30])
        self.assertEqual(restarted.store.records(self.sid)[0]["status"],"done")
        self.assertIsNone(restarted.store.pending())

    def test_uncertain_completion_is_not_repeated(self):
        self.machine.lose_ack=True
        with self.assertRaises(TimeoutError):
            self.action("run",phase="cal",sample="all")
        r=self.app.store.pending()
        self.assertEqual(r["status"],"uncertain")
        with self.assertRaises(ValueError):
            self.action("recover",id=r["id"],confirmed=True)
        self.assertEqual(self.machine.discharges,[30])
        self.action("void",id=r["id"],confirmed=True)
        self.assertEqual(self.app.store.setting("tip_index",0),1)

    def test_per_point_repeats_and_rack_geometry(self):
        self.app.new_session(dict(SETUP,repeats=2))
        sess=self.app.store.session()
        self.assertEqual(len(self.app.plan(sess,{"phase":"cal"})),36)
        validate_profile(POINTS)
        for kind,ws in (("tip",TIPS),("tube",TUBES)):
            self.assertEqual(len({tuple(grid_point(POINTS,kind,w)) for w in ws}),len(ws))
        with self.assertRaises(ValueError):
            validate_profile(dict(POINTS,tip_row=POINTS["tip_col"]))
        with self.assertRaises(ValueError):
            self.app.new_session(dict(SETUP,wells=dict.fromkeys(WELLS,"A1")))
        with self.assertRaises(ValueError):
            correction({"a":9,"b":5})

    def test_unconsumed_tip_on_exhaustion_and_missing_blinds(self):
        self.app.store.set("tip_index",96)
        with self.assertRaises(ValueError):
            self.action("run",phase="cal")
        self.assertEqual(self.app.store.records(self.sid),[])
        with self.assertRaises(ValueError):
            self.action("guess",pair=ANSWER)
        self.assertNotIn("answer",self.app.store.session())


class ProfileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.db = Path(folder.name)/"workshop.sqlite3"

    def test_first_class_needs_only_the_receiver(self):
        original = LAYOUT["initial_points"]
        receiver = [195.0,40.0,90.0]
        bench = Bench(dict(original,receiver=receiver))
        app = Station(self.db,bench)
        self.assertEqual(app.points,original)
        self.assertEqual(app.sources,dict.fromkeys(original,"旧工位"))
        for kind,wells,cols,x,y,pitch,z in (
            ("tip",TIPS,12,58.89,161.48,9,172),
            ("tube",TUBES,6,55.39,95.68,21.8,158),
        ):
            for index,well in enumerate(wells):
                row,col = divmod(index,cols)
                actual = grid_point(app.points,kind,well)
                for value,expected in zip(actual,(x+col*pitch,y+row*pitch,z)):
                    self.assertAlmostEqual(value,expected)
        self.assertEqual(app.points["waste"],[107.78,28.28,128.0])
        app.connected = True
        app.execute("initialize",{"confirmed":True})
        self.assertEqual(app.clearance,10)
        app.new_session(SETUP)
        sid = app.store.session()["id"]
        with self.assertRaisesRegex(ValueError,"天平"):
            app.execute("run",dict(session=sid,phase="cal"))
        self.assertEqual(app.store.records(sid),[])
        app.execute("teach_pick",{})
        self.assertTrue(app.tip)
        self.assertEqual(app.position,[58.89,161.48,10])
        with self.assertRaisesRegex(ValueError,"横移面"):
            app.execute("capture",{"key":"receiver"})
        self.assertNotIn("receiver",app.points)
        bench.pos = receiver
        app.execute("capture",{"key":"receiver"})
        self.assertTrue(app.approved)
        app.execute("teach_discard",{})
        self.assertFalse(app.tip)
        self.assertEqual(app.position,[107.78,28.28,10])
        self.assertEqual(app.store.setting("tip_index",0),1)
        bench.pos = [157.98,161.48,10]
        app.execute("capture",{"key":"tip_col"})
        restarted = Station(self.db,bench)
        self.assertFalse(restarted.approved)
        restarted.connected = True
        restarted.execute("initialize",{"confirmed":True})
        self.assertTrue(restarted.approved)
        self.assertEqual(restarted.points["receiver"],receiver)
        self.assertEqual(restarted.points["tip_col"],[157.98,161.48,10])
        self.assertEqual(restarted.sources["receiver"],"现场记录")
        self.assertEqual(restarted.sources["tip_col"],"现场记录")
        self.assertEqual(restarted.sources["waste"],"旧工位")

    def test_only_a_previously_homed_receiver_migrates(self):
        store = Store(self.db)
        store.set("points",dict(receiver=[11,22,33],safe=[0,0,4]))
        store.set("points_"+LAYOUT["frame"],dict(safe=[10,10,11],receiver=[90,100,110]))
        app = Station(self.db,Bench())
        self.assertEqual(app.points["receiver"],[90,100,110])
        self.assertEqual(app.points["safe"],LAYOUT["initial_points"]["safe"])
        self.assertEqual(app.sources["receiver"],"现场记录")


class OriginTests(unittest.TestCase):
    class Controller:
        def __init__(self, offset):
            self.limits={a:offset + 100*a.value for a in MotorAxis}
            self.steps={a:self.limits[a]+2000 for a in MotorAxis}
            self.status={a:MotorStatus.STANDBY for a in MotorAxis}
            self.order=[]
            self.seeks=[]
            self.moves=[]
            self.serial_conn=True
        def get_motor_status(self,a):
            return SimpleNamespace(steps=self.steps[a],status=self.status[a])
        def enable_motor(self,*args): return True
        def set_speed_mode(self,a,speed,acc):
            assert speed < 0
            self.order.append(a.name)
            self.seeks.append((a.name,speed,acc))
            self.status[a]=MotorStatus.REVERSE_LIMIT_STOP
            self.steps[a]=self.limits[a]
            return True
        def emergency_stop(self,a):
            self.status[a]=MotorStatus.STANDBY
            return True
        def move_to_position(self,a,target,rpm,acc,precision):
            self.moves.append((a.name,target,rpm,acc))
            self.steps[a]=target
            self.status[a]=MotorStatus.STANDBY
            return True

    def test_physical_home_rebases_point_targets_across_power_cycles(self):
        for offset in (-17001,97832):
            rig=Rig()
            rig.xyz=self.Controller(offset)
            rig.pipette=SimpleNamespace(tip=lambda:False,exchange=lambda *a,**k:None)
            with self.assertRaises(ValueError):
                rig.axis("x",50)
            rig.initialize(lambda msg:None)
            self.assertEqual(rig.xyz.order,["Z","X","Y"])
            self.assertEqual(rig.position(),[10.,10.,10.])
            rig.axis("x",58.89)
            self.assertEqual(rig.xyz.steps[MotorAxis.X],rig.xyz.limits[MotorAxis.X]+round(58.89*STEPS["x"]))

    def test_failed_home_never_authorizes_motion(self):
        rig=Rig()
        rig.xyz=self.Controller(-9876)
        def collide(a,speed,acc):
            rig.xyz.status[a]=MotorStatus.COLLISION_STOP
            return True
        rig.xyz.set_speed_mode=collide
        rig.pipette=SimpleNamespace(tip=lambda:False)
        with self.assertRaises(RuntimeError):
            rig.initialize(lambda msg:None)
        self.assertFalse(rig.homed)
        self.assertFalse(rig.initialized)

    def test_motor_commands_for_home_jog_and_parallel_travel(self):
        rig = Rig()
        ctl = rig.xyz = self.Controller(1000)
        rig.pipette = SimpleNamespace(tip=lambda:False,exchange=lambda *a,**k:None)
        with patch("workstation.hardware.time.sleep"):
            rig.initialize(lambda _:None)
        self.assertEqual(ctl.seeks,[("Z",-300,500),("X",-37,500),("Y",-37,500)])
        self.assertEqual([(a,rpm,acc) for a,_,rpm,acc in ctl.moves],
                         [("Z",300,500),("X",37,500),("Y",37,500)])
        ctl.moves.clear()
        with tempfile.TemporaryDirectory() as folder:
            app = Station(Path(folder)/"motion.sqlite3",rig)
            app.use_saved_points()
            self.assertIn(50,app.state()["jog_distances_mm"])
            app.calibrate("jog",dict(axis="x",delta=50))
            app.calibrate("jog",dict(axis="x",delta=-50))
            for name in "zxy":
                app.calibrate("jog",dict(axis=name,delta=1))
        writes_at_read = []
        read = ctl.get_motor_status
        def observed(axis):
            writes_at_read.append((axis.name,[move[0] for move in ctl.moves[-2:]]))
            return read(axis)
        with patch.object(ctl,"get_motor_status",side_effect=observed):
            rig.travel([25,45,70],safe=10)
        self.assertEqual(writes_at_read[1:3],[("X",["X","Y"]),("Y",["X","Y"])])
        expected = [("X",60,38),("X",10,38),("Z",11,300),("X",11,38),("Y",11,38),
                    ("Z",10,300),("X",25,38),("Y",45,38),("Z",70,300)]
        self.assertEqual(ctl.moves,[(axis,ctl.limits[MotorAxis[axis]]+round(mm*STEPS[axis.lower()]),rpm,500)
                                    for axis,mm,rpm in expected])

    def test_xy_second_command_failure_stops_both_axes_before_lowering(self):
        rig = Rig()
        ctl = rig.xyz = self.Controller(0)
        rig.home_steps = dict.fromkeys("xyz",0)
        rig.axes_enabled = True
        writes = []
        def command(axis,target,rpm,acc,precision):
            writes.append((axis.name,target))
            ctl.steps[axis] = target
            return axis != MotorAxis.Y
        with patch.object(ctl,"move_to_position",side_effect=command), patch.object(ctl,"emergency_stop",return_value=True) as stop:
            with self.assertRaisesRegex(RuntimeError,"Y 轴"):
                rig.travel([25,45,70],safe=10)
        self.assertEqual([axis for axis,_ in writes],["Z","X","Y"])
        self.assertEqual([call.args[0] for call in stop.call_args_list],list(MotorAxis))
        self.assertFalse(rig.homed)

    def test_home_times_out_after_thirty_seconds_of_continuous_motion(self):
        rig = Rig()
        ctl = rig.xyz = self.Controller(1000)
        rig.pipette = SimpleNamespace(tip=lambda:False)
        def seek(axis,speed,acc):
            ctl.status[axis] = MotorStatus.RUNNING
            return True
        def read(axis):
            if ctl.status[axis] == MotorStatus.RUNNING:
                ctl.steps[axis] -= round(STEPS[axis.name.lower()] * .2)
            return SimpleNamespace(steps=ctl.steps[axis],status=ctl.status[axis])
        ctl.set_speed_mode,ctl.get_motor_status = seek,read
        elapsed = 0.
        def advance(seconds):
            nonlocal elapsed
            elapsed += seconds
        with patch("workstation.hardware.time", SimpleNamespace(monotonic=lambda:elapsed,sleep=advance)):
            with self.assertRaises(TimeoutError):
                rig.initialize(lambda _:None)
        self.assertAlmostEqual(elapsed,30)
        self.assertFalse(rig.homed)
        self.assertFalse(ctl.moves)

    def test_position_plateau_homes_all_axes_without_a_switch_report(self):
        rig=Rig()
        ctl=self.Controller(51000)
        rig.xyz=ctl
        rig.pipette=SimpleNamespace(tip=lambda:False,exchange=lambda *a,**k:None)
        def seek(a,speed,acc):
            ctl.order.append(a.name)
            ctl.steps[a]=ctl.limits[a]
            ctl.status[a]=MotorStatus.STANDBY
            return True
        ctl.set_speed_mode=seek
        ticks=[0.]
        sleeps = []
        def advance(s):
            sleeps.append(s)
            ticks[0]+=s
        with patch("workstation.hardware.time",SimpleNamespace(monotonic=lambda:ticks[0],sleep=advance)):
            rig.initialize(lambda msg:None)
        self.assertTrue(rig.homed)
        self.assertEqual(ctl.order,["Z","X","Y"])
        self.assertEqual(set(rig.home_method.values()),{"寻零位置持续稳定"})
        self.assertTrue(all(s in (.2,.15) for s in sleeps))
        self.assertGreaterEqual(ticks[0],3*(1+.15)-1e-6)
        self.assertLess(ticks[0],3*(1.21+.15))
        self.assertEqual(rig.position(),[10.,10.,10.])
