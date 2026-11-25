"""Windows loopback workshop server. Start with the adjacent CMD launcher."""
import argparse
import csv
import io
import json
import logging
import math
import secrets
import socket
import threading
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from workstation.control import Station
from workstation.science import summary, volume

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
APP = "laiyu-workshop-local-v1"


def report(store, sid):
    sess = store.session(sid)
    if not sess:
        raise ValueError("课堂不存在")
    rows = store.records(sid)
    analysis = summary(sess,rows)
    for r in rows:
        identity = sess.get("answer",{}).get(r["sample"],r["sample"])
        r["identity"] = identity
        r["rho"] = sess["densities"].get(identity)
        r["actual"] = volume(r["net"],r["rho"]) if r["net"] is not None and r["rho"] else None
        r["signed_error"] = r["absolute_error"] = r["percent_error"] = None
        if r["actual"] is not None and r["target"] is not None:
            r["signed_error"] = r["actual"] - r["target"]
            r["absolute_error"] = abs(r["signed_error"])
            r["percent_error"] = 100*r["signed_error"]/r["target"]
        r["station"] = json.loads(r["station"])
    return dict(session=sess, records=rows, analysis=analysis, events=store.events(sid))


def csv_report(doc):
    out = io.StringIO(newline="")
    w = csv.writer(out)
    cols = ["id","phase","sample","identity","well","command","target","rho","pre","post","net","actual",
            "signed_error","absolute_error","percent_error","tip","status","step","error","started",
            "before_time","after_time","before_raw","after_raw"]
    w.writerow(["记录编号","阶段","样品代号","已知或揭晓身份","试管位","移液器设定_μL","验证目标_μL","密度_g每mL",
                "前重_g","后重_g","净增质量_g","实测体积_μL","有符号误差_μL","绝对误差_μL","相对误差_百分比",
                "枪头孔位","状态","最后步骤","异常","开始时间_UTC","前重时间_UTC","后重时间_UTC","前重原始帧","后重原始帧",
                "拟合斜率","拟合截距_μL","R平方","学员配对","教师答案","重做原记录编号","润洗","预吸空气_μL"])
    for r in doc["records"]:
        fit = doc["analysis"]["curves"].get(r["identity"],{})
        sess = doc["session"]
        options = r["station"].get("liquid_options",{"prewet":True,"air":False})
        w.writerow([r[k] for k in cols]+[fit.get(k) for k in ("a","b","r2")]+
                   [sess.get("guess",{}).get(r["sample"]),sess.get("answer",{}).get(r["sample"]),
                    r["redo_of"],"开" if options["prewet"] else "关",50 if options["air"] else 0])
    return ("\ufeff"+out.getvalue()).encode("utf-8")


def make_server(port, station_factory):
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, code, body, mime="application/json; charset=utf-8", filename=None):
            if not isinstance(body,bytes):
                body = json.dumps(body,ensure_ascii=False,allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type",mime)
            self.send_header("Content-Length",str(len(body)))
            self.send_header("Cache-Control","no-store")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("X-Frame-Options","DENY")
            if filename:
                self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def local(self):
            port = self.server.server_port
            return self.headers.get("Host","") in (f"127.0.0.1:{port}",f"localhost:{port}")

        def do_GET(self):
            if not self.local():
                return self.send(403,{"error":"仅允许本机访问"})
            url = urlsplit(self.path)
            try:
                if url.path == "/api/health":
                    return self.send(200,{"app":APP})
                if url.path == "/api/state":
                    return self.send(200,self.server.station.state())
                if url.path in ("/export.csv","/export.json"):
                    sid = int(parse_qs(url.query)["session"][0])
                    if sid < 1:
                        raise ValueError("课堂编号无效")
                    doc = report(self.server.station.store,sid)
                    if url.path.endswith("csv"):
                        return self.send(200,csv_report(doc),"text/csv; charset=utf-8",f"workshop-{sid}.csv")
                    return self.send(200,doc,filename=f"workshop-{sid}.json")
                files = {"/":("index.html","text/html"),"/style.css":("style.css","text/css"),
                         "/app.js":("app.js","text/javascript")}
                if url.path in files:
                    name,mime = files[url.path]
                    content = (WEB/name).read_bytes()
                    if name == "index.html":
                        content = content.replace(b"__LOCAL_TOKEN__",token.encode())
                    return self.send(200,content,mime+"; charset=utf-8")
                return self.send(404,{"error":"页面不存在"})
            except (ValueError,KeyError,TypeError) as exc:
                self.send(400,{"error":str(exc)})

        def do_POST(self):
            port = self.server.server_port
            origins = (None,f"http://127.0.0.1:{port}",f"http://localhost:{port}")
            if (not self.local() or self.headers.get("Origin") not in origins or
                    self.headers.get("X-Workshop-Key") != token):
                return self.send(403,{"error":"请从本机工站页面操作"})
            try:
                length = int(self.headers.get("Content-Length",0))
                if not 2 <= length <= 16384:
                    raise ValueError("请求大小无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data,dict):
                    raise ValueError("操作参数无效")
                station = self.server.station
                if self.path == "/api/stop":
                    station.stop()
                    return self.send(200,{"ok":True})
                if self.path == "/api/action":
                    key = str(uuid.UUID(data["request"]))
                    job = station.start(data["op"],data,key)
                    return self.send(202,{"job":job})
                self.send(404,{"error":"未知操作"})
            except (KeyError,ValueError,TypeError,RuntimeError) as exc:
                self.send(409,{"error":str(exc)})

    class LocalServer(ThreadingHTTPServer):
        allow_reuse_address = False
        def server_bind(self):
            if hasattr(socket,"SO_EXCLUSIVEADDRUSE"):
                self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            super().server_bind()

    srv = LocalServer(("127.0.0.1",port),Handler)
    try:
        srv.station = station_factory()
    except BaseException:
        srv.server_close()
        raise
    return srv


def main():
    parser = argparse.ArgumentParser(description="移液实操工站")
    parser.add_argument("--no-browser",action="store_true")
    args=parser.parse_args()
    data=ROOT/"data"
    data.mkdir(exist_ok=True)
    logging.getLogger().setLevel(logging.INFO)
    logger=logging.FileHandler(data/"workshop.log",encoding="utf8")
    logger.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(logger)
    port=8765
    url=f"http://127.0.0.1:{port}"
    try:
        server=make_server(port,lambda: Station(data/"workshop.sqlite3"))
    except OSError:
        try:
            with urllib.request.urlopen(url+"/api/health",timeout=1) as r:
                existing=json.load(r).get("app")==APP
        except Exception:
            existing=False
        if not existing:
            raise
        if not args.no_browser:
            webbrowser.open(url)
        print("工站已经启动",url)
        return
    print("移液实操工站",url,flush=True)
    if not args.no_browser:
        threading.Timer(.4,lambda:webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server.station.busy:
            server.station.stop()
        server.server_close()
        server.station.rig.close()

if __name__ == "__main__":
    main()
