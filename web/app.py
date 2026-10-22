#!/usr/bin/env python3
"""sbm-web - sbm 的 Web 管理界面后端

只监听 127.0.0.1，由 Nginx 在 /admin/ 下做 HTTPS 和登录认证后反向代理过来。
业务逻辑全部复用 /usr/local/bin/sbm，和命令行共用同一把锁、同一份数据。
"""
import contextlib
import html
import importlib.machinery
import importlib.util
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import qrcode
import qrcode.image.svg

_loader = importlib.machinery.SourceFileLoader("sbm", "/usr/local/bin/sbm")
_spec = importlib.util.spec_from_loader("sbm", _loader)
sbm = importlib.util.module_from_spec(_spec)
_loader.exec_module(sbm)

HERE = os.path.dirname(os.path.abspath(__file__))
LISTEN = ("127.0.0.1", 8090)
NOTE_MAX = 64
_mutex = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


@contextlib.contextmanager
def mutating():
    with _mutex, sbm.locked():
        yield


def state():
    conf = sbm.load(sbm.CONF)
    users = sbm.load_users()
    creds = sbm.load_creds()
    traffic = sbm.traffic_summary(conf)
    return {
        "traffic": {
            "enabled": traffic["enabled"],
            "collected": traffic["collected"],
            "month_total": traffic["month_total"],
            "nodes_today": traffic["nodes_today"],
            "status": traffic["status"],
        },
        "pool": {
            "spares": len(creds["spares"]),
            "size": sbm.pool_size(conf),
            "retiring": [{"user": r["user"], "reason": r["reason"], "since": r["since"]} for r in creds["retiring"]],
            "dirty": creds["dirty"],
            "maintenance": conf.get("maintenance_time", "04:00"),
        },
        "sub_domain": conf["sub_domain"],
        "nodes": [
            {"name": n["name"], "host": n["host"], "server": n["server"], "port": n["port"],
             "managed": n.get("managed", False)}
            for n in conf["nodes"]
        ],
        "base_domain": sbm.base_domain(conf),
        # Token 只存在文件里，接口永远不返回
        "domains": [
            {"zone": d["zone"], "note": d.get("note", ""), "added": d.get("added", ""),
             "default": d["zone"] == sbm.base_domain(conf), "used_by": sbm.domain_usage(conf, d["zone"])}
            for d in sbm.domains(conf)
        ],
        "users": [
            {
                "name": u["name"],
                "note": u.get("note", ""),
                "enabled": u["enabled"],
                "pending_purge": sbm.pending_purge(creds, u),
                "created": u["created"],
                "links": sbm.sub_links(conf, u),
                "month": traffic["users"].get(u["id"], [0, 0]),
            }
            for u in users
        ],
    }


# 每行 key=value，某一项取不到也不会让后面的字段错位。
# 启动时间用开机时刻 + 单调时钟换算，避免解析各系统不同格式和时区的时间字符串；
# 不用 systemctl show --value：CentOS 7 的 systemd 219 不支持
STATUS_SCRIPT = r"""
echo "active=$(systemctl is-active sing-box)"
echo "version=$(sing-box version 2>/dev/null | awk 'NR==1{print $3}')"
echo "started_mono=$(systemctl show -p ActiveEnterTimestampMonotonic sing-box | cut -d= -f2)"
echo "btime=$(awk '/^btime/{print $2}' /proc/stat)"
echo "uptime=$(cut -d. -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
echo "cores=$(nproc)"
echo "mem=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print int((t-a)/1024), int(t/1024)}' /proc/meminfo)"
. /etc/os-release 2>/dev/null; echo "os=${PRETTY_NAME:-$(uname -s)}"
echo "arch=$(uname -m)"
echo "kernel=$(uname -r)"
"""


def _num(s, cast=float, default=None):
    try:
        return cast(s)
    except (TypeError, ValueError):
        return default


def one_status(n):
    r = sbm.ssh(n, STATUS_SCRIPT)
    kv = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    load = [_num(x) for x in kv.get("load", "").split()]
    mem = [_num(x, int) for x in kv.get("mem", "").split()]
    btime, mono = _num(kv.get("btime"), int), _num(kv.get("started_mono"), int)
    return {
        "name": n["name"],
        "reachable": r.returncode == 0,
        "active": kv.get("active") == "active",
        "version": kv.get("version", ""),
        # 服务没在运行时单调时间为 0
        "started_at": btime + mono // 1_000_000 if btime and mono else None,
        "uptime": _num(kv.get("uptime"), int),
        "load": load if len(load) == 3 else None,
        "cores": _num(kv.get("cores"), int),
        "mem_used": mem[0] if len(mem) == 2 else None,
        "mem_total": mem[1] if len(mem) == 2 else None,
        "os": kv.get("os", ""),
        "arch": kv.get("arch", ""),
        "kernel": kv.get("kernel", ""),
        "error": r.stderr.strip() if r.returncode != 0 else "",
    }


def node_status():
    nodes = sbm.load(sbm.CONF)["nodes"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one_status, nodes))


def run_op(op):
    """在锁内执行 sbm 的用户操作；只有操作要求时才推送节点（重启）。返回 (是否成功, 日志)。"""
    with mutating():
        users, creds = sbm.load_users(), sbm.load_creds()
        lines = []
        try:
            need_push, notice = op(users, creds)
            ok = sbm.commit(sbm.load(sbm.CONF), users, creds, need_push, log=lines.append)
        except sbm.SbmError as e:
            raise ApiError(400, str(e))
        if notice:
            lines.insert(0, notice)
        return ok, "\n".join(lines)


def check_note(note):
    note = (note or "").strip()
    if len(note) > NOTE_MAX:
        raise ApiError(400, f"备注最长 {NOTE_MAX} 个字符")
    return note


def add_user(body):
    name = (body.get("name") or "").strip()
    note = check_note(body.get("note"))
    return run_op(lambda users, creds: sbm.user_add(users, creds, name, note))


def user_action(name, action, body):
    # immediate：删除/停用默认等维护时清理，重置默认立即生效
    now = bool(body.get("immediate", action == "reset"))
    if action == "note":
        note = check_note(body.get("note"))

        def set_note(users, creds):
            # 备注只存在管理端，不需要推送节点
            sbm.get_user(users, name)["note"] = note
            return False, ""
        return run_op(set_note)
    ops = {
        "enable": lambda users, creds: sbm.user_enable(users, creds, name),
        "disable": lambda users, creds: sbm.user_disable(users, creds, name, now),
        "reset": lambda users, creds: sbm.user_reset(users, creds, name, now),
        "delete": lambda users, creds: sbm.user_delete(users, creds, name, now),
    }
    if action not in ops:
        raise ApiError(404, "未知操作")
    return run_op(ops[action])


def sync_all():
    with mutating():
        users, creds = sbm.load_users(), sbm.load_creds()
        sbm.save_users(users)
        lines = []
        try:
            ok = sbm.reconcile(sbm.load(sbm.CONF), users, creds, log=lines.append)
        except sbm.SbmError as e:
            raise ApiError(400, str(e))
        return ok, "\n".join(lines)


# ---------- 节点（部署耗时 1~2 分钟，放到后台任务里跑，界面轮询日志） ----------

_jobs = {}


class Job:
    def __init__(self, title):
        self.id = sbm.rand(12)
        self.title = title
        self.lines = []
        self.done = False
        self.ok = False

    def log(self, line):
        self.lines.append(f"{datetime.now():%H:%M:%S}  {line}")


def start_job(title, fn):
    job = Job(title)
    _jobs[job.id] = job

    def run():
        try:
            with mutating():
                fn(job.log)
            job.ok = True
        except sbm.SbmError as e:
            job.log(f"✗ {e}")
        except Exception as e:  # noqa: BLE001 - 写进任务日志给界面看
            job.log(f"✗ 内部错误 {type(e).__name__}: {e}")
        finally:
            job.done = True

    threading.Thread(target=run, daemon=True).start()
    return {"job": job.id}


def job_status(job_id, since):
    job = _jobs.get(job_id)
    if job is None:
        raise ApiError(404, "任务不存在（服务可能重启过）")
    return {"title": job.title, "lines": job.lines[since:], "next": len(job.lines),
            "done": job.done, "ok": job.ok}


def str_field(body, key, default=""):
    v = body.get(key, default)
    return str(v).strip() if v is not None else default


def deploy_job(body):
    name, host, sub = str_field(body, "name"), str_field(body, "host"), str_field(body, "sub").lower()
    ssh_port, port = str_field(body, "ssh_port", "22"), str_field(body, "port", "443")
    zone = str_field(body, "zone") or None
    if not (ssh_port.isdigit() and port.isdigit()):
        raise ApiError(400, "端口必须是数字")
    return start_job(f"部署节点 {name}", lambda log: sbm.deploy_node(
        sbm.load(sbm.CONF), sbm.load_users(), name, host, ssh_port, sub, port, zone, log=log))


# ---------- 域名与 Cloudflare Token ----------

def domain_op(fn):
    """在锁内执行域名操作；fn(conf) 返回要显示的结果行。"""
    with mutating():
        try:
            lines = fn(sbm.load(sbm.CONF)) or []
        except sbm.SbmError as e:
            raise ApiError(400, str(e))
    return True, "\n".join(lines)


def user_traffic(name):
    users = sbm.load_users()
    try:
        u = sbm.get_user(users, name)
    except sbm.SbmError as e:
        raise ApiError(404, str(e))
    if not os.path.exists(sbm.TRAFFIC_DB):
        raise ApiError(404, "还没有流量数据")
    return dict(sbm.traffic_detail(u["id"]), name=u["name"])


def domain_check(zone):
    try:
        return {"ok": True, "log": "\n".join(sbm.domain_check(sbm.load(sbm.CONF), zone))}
    except sbm.SbmError as e:
        return {"ok": False, "log": str(e)}


def attach_job(body):
    name, host = str_field(body, "name"), str_field(body, "host")
    ssh_port = str_field(body, "ssh_port", "22")
    if not ssh_port.isdigit():
        raise ApiError(400, "端口必须是数字")
    tag = str_field(body, "inbound_tag") or "anytls-in"
    path = str_field(body, "config_path") or "/etc/sing-box/config.json"
    server = str_field(body, "server") or None
    return start_job(f"接入节点 {name}", lambda log: sbm.attach_node(
        sbm.load(sbm.CONF), sbm.load_users(), name, host, ssh_port, tag, path, server, log=log))


def versions(refresh):
    try:
        return sbm.releases(refresh=refresh)
    except sbm.SbmError as e:
        raise ApiError(502, str(e))


def upgrade_job(body):
    names = body.get("names") or []
    if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
        raise ApiError(400, "请选择要升级的节点")
    version = str_field(body, "version") or None
    if version and not sbm.VERSION_RE.match(version):
        raise ApiError(400, "版本号格式不对")
    label = "、".join(names) if len(names) <= 2 else f"{len(names)} 个节点"
    return start_job(f"升级 sing-box · {label}", lambda log: sbm.upgrade_nodes(
        sbm.load(sbm.CONF), names, version, log=log))


def remove_node(name):
    lines = []
    with mutating():
        try:
            sbm.remove_node(sbm.load(sbm.CONF), sbm.load_users(), name, log=lines.append)
        except sbm.SbmError as e:
            raise ApiError(400, str(e))
    return {"ok": True, "log": "\n".join(lines), "state": state()}


def render_manual():
    """管理手册里的 {{SUB_DOMAIN}} 等占位符按当前配置填入，仓库里不需要保存实例的域名和 IP。"""
    conf = sbm.load(sbm.CONF)
    values = {
        "SUB_DOMAIN": conf["sub_domain"],
        "SUB_PORT": str(conf["sub_port"]),
        "BASE_DOMAIN": sbm.base_domain(conf),
        "MANAGER_IP": conf.get("manager_ip", "订阅服务器IP"),
    }
    with open(os.path.join(HERE, "manual.html"), encoding="utf-8") as f:
        page = f.read()
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", html.escape(v))
    return page


def qr_svg(text):
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    return img.to_string(encoding="unicode")


class Handler(BaseHTTPRequestHandler):
    server_version = "sbm-web"
    sys_version = ""

    def log_message(self, fmt, *args):
        print(f"{self.headers.get('X-Real-IP', self.client_address[0])} {fmt % args}", flush=True)

    def send(self, status, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 管理员的网络经过刚重启的节点时，浏览器可能已经断开；操作本身已完成
            print("client disconnected before response was sent", flush=True)

    def send_json(self, status, obj):
        self.send(status, json.dumps(obj, ensure_ascii=False))

    def handle_api(self, fn):
        try:
            self.send_json(200, fn())
        except ApiError as e:
            self.send_json(e.status, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001 - 返回给界面显示，而不是让连接直接断掉
            self.send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def handle_page(self, fn):
        try:
            self.send(200, fn(), "text/html; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self.send(500, f"页面生成失败: {type(e).__name__}: {e}", "text/plain; charset=utf-8")

    def read_body(self):
        # 自定义请求头 + JSON：浏览器跨站提交表单无法同时满足这两点，用来防 CSRF
        if self.headers.get("X-Requested-With") != "sbm-web":
            raise ApiError(403, "缺少请求头")
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            raise ApiError(413, "请求过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "请求不是合法的 JSON")

    def mutation(self, fn):
        def run():
            ok, log = fn(self.read_body())
            return {"ok": ok, "log": log, "state": state()}
        self.handle_api(run)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif url.path == "/manual":
            self.handle_page(render_manual)
        elif url.path == "/api/state":
            self.handle_api(state)
        elif url.path == "/api/status":
            self.handle_api(node_status)
        elif t := re.fullmatch(r"/api/users/([^/]+)/traffic", url.path):
            self.handle_api(lambda: user_traffic(unquote(t.group(1))))
        elif m := re.fullmatch(r"/api/domains/([a-z0-9.-]+)/check", url.path):
            self.handle_api(lambda: domain_check(m.group(1)))
        elif url.path == "/api/versions":
            refresh = parse_qs(url.query).get("refresh", ["0"])[0] == "1"
            self.handle_api(lambda: versions(refresh))
        elif url.path == "/api/pubkey":
            self.handle_api(lambda: {"command": sbm.authorize_command(sbm.load(sbm.CONF))})
        elif m := re.fullmatch(r"/api/jobs/([A-Za-z0-9]{12})", url.path):
            since = parse_qs(url.query).get("since", ["0"])[0]
            self.handle_api(lambda: job_status(m.group(1), int(since) if since.isdigit() else 0))
        elif url.path == "/api/qr":
            text = parse_qs(url.query).get("text", [""])[0]
            if not text or len(text) > 1024:
                self.send_json(400, {"ok": False, "error": "text 参数无效"})
            else:
                self.send(200, qr_svg(text), "image/svg+xml")
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/users/([^/]+)/(enable|disable|reset|note)", path)
        if path == "/api/users":
            self.mutation(add_user)
        elif path == "/api/sync":
            self.mutation(lambda _body: sync_all())
        elif path == "/api/nodes/deploy":
            self.handle_api(lambda: deploy_job(self.read_body()))
        elif path == "/api/nodes/attach":
            self.handle_api(lambda: attach_job(self.read_body()))
        elif path == "/api/nodes/upgrade":
            self.handle_api(lambda: upgrade_job(self.read_body()))
        elif path == "/api/domains":
            self.mutation(lambda body: domain_op(lambda conf: sbm.domain_add(
                conf, str_field(body, "zone"), str_field(body, "token"), str_field(body, "note"))))
        elif d := re.fullmatch(r"/api/domains/([a-z0-9.-]+)/(token|default)", path):
            zone, action = d.group(1), d.group(2)
            if action == "token":
                self.mutation(lambda body: domain_op(lambda conf: sbm.domain_set_token(conf, zone, str_field(body, "token"))))
            else:
                self.mutation(lambda body: domain_op(lambda conf: sbm.domain_set_default(conf, zone)))
        elif m:
            self.mutation(lambda body: user_action(unquote(m.group(1)), m.group(2), body))
        else:
            self.send_json(404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        path = urlparse(self.path).path
        m = re.fullmatch(r"/api/users/([^/]+)", path)
        n = re.fullmatch(r"/api/nodes/([^/]+)", path)
        d = re.fullmatch(r"/api/domains/([a-z0-9.-]+)", path)
        if d:
            self.mutation(lambda body: domain_op(lambda conf: sbm.domain_del(conf, d.group(1))))
        elif m:
            self.mutation(lambda body: user_action(unquote(m.group(1)), "delete", body))
        elif n:
            def run():
                self.read_body()  # 只为校验防 CSRF 请求头
                return remove_node(unquote(n.group(1)))
            self.handle_api(run)
        else:
            self.send_json(404, {"ok": False, "error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    print(f"sbm-web listening on {LISTEN[0]}:{LISTEN[1]}", flush=True)
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()
