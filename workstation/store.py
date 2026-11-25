"""Durable workshop sessions, each physical attempt and the shared tip inventory."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY, created TEXT NOT NULL, setup TEXT NOT NULL,
                    answer TEXT NOT NULL, guess TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, session INTEGER NOT NULL REFERENCES sessions(id),
                    phase TEXT NOT NULL, sample TEXT NOT NULL, well TEXT NOT NULL, command INTEGER NOT NULL,
                    target REAL, rho REAL, tip TEXT NOT NULL, started TEXT NOT NULL, status TEXT NOT NULL,
                    step TEXT NOT NULL, error TEXT DEFAULT '', pre REAL, post REAL, net REAL,
                    before_raw TEXT, after_raw TEXT, before_time TEXT, after_time TEXT,
                    station TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, attempt INTEGER NOT NULL, time TEXT NOT NULL, detail TEXT NOT NULL);
            """)
            if "redo_of" not in {r["name"] for r in db.execute("PRAGMA table_info(attempts)")}:
                db.execute("ALTER TABLE attempts ADD COLUMN redo_of INTEGER REFERENCES attempts(id)")
            db.execute("""UPDATE attempts SET status='uncertain',error='程序上次在动作中结束，须现场核对'
                          WHERE status IN ('running','dispensing')""")

    @contextmanager
    def db(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=8)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def setting(self, key, fallback):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def set(self, key, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, encode(value)))

    def load_profile(self, frame, initial):
        key = "profile_" + frame
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if row:
                return json.loads(row[0])
            profile = dict(points={k:list(v) for k,v in initial.items()},
                           sources=dict.fromkeys(initial,"旧工位"))
            old = db.execute("SELECT value FROM settings WHERE key=?",("points_"+frame,)).fetchone()
            receiver = json.loads(old[0]).get("receiver") if old else None
            if receiver is not None:
                profile["points"]["receiver"] = receiver
                profile["sources"]["receiver"] = "现场记录"
            db.execute("INSERT INTO settings VALUES (?,?)",(key,encode(profile)))
            return profile

    def create(self, setup, answer):
        with self.db() as db:
            sid = db.execute("INSERT INTO sessions(created,setup,answer) VALUES (?,?,?)",
                             (now(), encode(setup), encode(answer))).lastrowid
            db.execute("INSERT OR REPLACE INTO settings VALUES ('session',?)", (str(sid),))
        return sid

    def session(self, sid=None):
        sid = sid or self.setting("session", 0)
        with self.db() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if not row:
            return None
        data = dict(id=row["id"], created=row["created"], **json.loads(row["setup"]))
        if row["guess"]:
            data.update(guess=json.loads(row["guess"]), answer=json.loads(row["answer"]))
        return data

    def session_list(self):
        with self.db() as db:
            return [dict(id=r["id"], name=json.loads(r["setup"])["name"]) for r in
                    db.execute("SELECT id,setup FROM sessions ORDER BY id DESC")]

    def reveal(self, sid, guess):
        with self.db() as db:
            changed = db.execute("UPDATE sessions SET guess=? WHERE id=? AND guess IS NULL",
                                 (encode(guess), sid)).rowcount
            if not changed:
                raise ValueError("这一场的配对已经提交")

    def records(self, sid):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM attempts WHERE session=? ORDER BY id", (sid,))]

    def pending(self):
        with self.db() as db:
            row = db.execute("""SELECT * FROM attempts WHERE status IN
                           ('dispensed','waiting_mass','measured','uncertain') ORDER BY id LIMIT 1""").fetchone()
        return dict(row) if row else None

    def redo_target(self, sid, record_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM attempts WHERE session=? AND id=? AND phase='cal'",
                             (sid,record_id)).fetchone()
            if not row or row["status"] not in ("done","superseded","void"):
                raise ValueError("请选择本场可重做的标定记录")
            # Retrying a failed redo uses the same original measurement.
            if row["status"] == "void" and row["redo_of"]:
                row = db.execute("SELECT * FROM attempts WHERE id=?", (row["redo_of"],)).fetchone()
            while row["status"] == "superseded":
                row = db.execute("SELECT * FROM attempts WHERE redo_of=? AND status IN ('done','superseded')",
                                 (row["id"],)).fetchone()
            return dict(row)

    @staticmethod
    def _next_tip(db, tips):
        row = db.execute("SELECT value FROM settings WHERE key='tip_index'").fetchone()
        index = int(row[0]) if row else 0
        if index >= len(tips):
            raise ValueError("枪头盒已用完，请装新盒后登记起始孔")
        db.execute("INSERT OR REPLACE INTO settings VALUES ('tip_index',?)", (str(index+1),))
        return tips[index]

    def take_teaching_tip(self, tips):
        with self.db() as db:
            return self._next_tip(db,tips)

    def reserve(self, sid, phase, sample, well, command, target, rho, tips, station, redo_of=None):
        with self.db() as db:
            tip = self._next_tip(db,tips)
            aid = db.execute("""INSERT INTO attempts(session,phase,sample,well,command,target,rho,tip,
                    started,status,step,station,redo_of) VALUES (?,?,?,?,?,?,?,?,?,'running','预留枪头',?,?)""",
                    (sid,phase,sample,well,command,target,rho,tip,now(),encode(station),redo_of)).lastrowid
        return aid, tip

    def update(self, aid, **fields):
        allowed = {"status","step","error","pre","post","net","before_raw","after_raw","before_time","after_time"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("记录字段无效")
        with self.db() as db:
            db.execute("UPDATE attempts SET "+", ".join(k+"=?" for k in fields)+" WHERE id=?",
                       (*fields.values(), aid))
            if fields.get("status") == "done":
                replaced = db.execute("""UPDATE attempts SET status='superseded'
                    WHERE id=(SELECT redo_of FROM attempts WHERE id=?)""", (aid,))
                if replaced.rowcount:
                    db.execute("""INSERT INTO events(attempt,time,detail)
                        SELECT redo_of,?,? FROM attempts WHERE id=?""",(now(),f"由重做记录 #{aid} 替代",aid))
            if "step" in fields or fields.get("error"):
                db.execute("INSERT INTO events(attempt,time,detail) VALUES (?,?,?)",
                           (aid,now(),fields.get("error") or fields["step"]))

    def events(self, sid):
        with self.db() as db:
            return [dict(x) for x in db.execute("""SELECT e.attempt,e.time,e.detail FROM events e
                   JOIN attempts a ON a.id=e.attempt WHERE a.session=? ORDER BY e.id""",(sid,))]
