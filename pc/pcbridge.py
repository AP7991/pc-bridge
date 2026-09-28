"""PC Bridge - PC app (one file). Shows a window with your PIN, status and any errors,
sets itself up (firewall, start with Windows, Wake-on-LAN) and serves the phone app.
Built into PCBridge.exe by the GitHub workflow.
"""
import ctypes, io, json, os, queue, secrets, socket, string, subprocess, sys, threading, time, traceback, uuid
import urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "PCBridge"
APP_DIR.mkdir(parents=True, exist_ok=True)
CFG_FILE, LOG_FILE = APP_DIR / "config.json", APP_DIR / "log.txt"
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
PORT, DISCOVERY_PORT = 47801, 47800
NO_WINDOW = 0x08000000
FROZEN = getattr(sys, "frozen", False)
EXE = sys.executable if FROZEN else f'"{sys.executable}" "{Path(__file__).resolve()}"'

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

# ---------------------------------------------------------------- config / log
def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text())
    except Exception:
        return {}


def save_cfg(c):
    CFG_FILE.write_text(json.dumps(c))


CFG = load_cfg()
if "pin" not in CFG:
    CFG["pin"] = "".join(secrets.choice(string.digits) for _ in range(8))
    save_cfg(CFG)
PIN = CFG["pin"]

EVENTS = queue.Queue()  # (kind, key, state, text) -> GUI


def log(msg):
    line = time.strftime("%H:%M:%S ") + msg
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    EVENTS.put(("log", None, None, line))


def check(key, state, text):
    """state: ok | bad | warn | busy"""
    EVENTS.put(("check", key, state, text))
    if state == "bad":
        log(f"PROBLEM - {text}")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW, **kw)


def ps(script):
    return run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------- PC info
def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic sent; picks the main network card
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith(("127.", "169.254.")):
                ips.append(ip)
    except Exception:
        pass
    return ips


def mac_addresses():
    out = set()
    try:
        for line in run(["getmac", "/fo", "csv", "/nh"]).stdout.splitlines():
            m = line.split(",")[0].strip('"')
            if len(m) == 17:
                out.add(m.replace("-", ":").upper())
    except Exception:
        pass
    n = uuid.getnode()
    out.add(":".join(f"{(n >> s) & 0xFF:02X}" for s in range(40, -1, -8)))
    return sorted(out)


MACS = []

# ---------------------------------------------------------------- PC control
import mss
import pyautogui
from PIL import Image

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0
try:
    import pygetwindow as gw
except Exception:
    gw = None


def jpeg(im, width, quality=60):
    if im.width > width:
        im = im.resize((width, im.height * width // im.width))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def grab(sct, region):
    shot = sct.grab(region)
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def primary():
    with mss.mss() as sct:
        return dict(sct.monitors[1])


def windows():
    if not gw:
        return []
    res = []
    for w in gw.getAllWindows():
        try:
            if (w.title.strip() and w.visible and w.width > 120 and w.height > 80
                    and w.title not in ("Program Manager", "PC Bridge")):
                res.append({"id": w._hWnd, "title": w.title, "min": w.isMinimized})
        except Exception:
            pass
    return res


def win_by_id(hwnd):
    return gw.Win32Window(int(hwnd))


def power(action):
    u32 = ctypes.windll.user32
    log(f"Phone asked to: {action}")
    if action == "lock":
        u32.LockWorkStation()
    elif action == "screenoff":
        u32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
    elif action == "sleep":
        ctypes.windll.powrprof.SetSuspendState(False, True, False)
    elif action == "shutdown":
        run(["shutdown", "/s", "/t", "0"])
    elif action == "restart":
        run(["shutdown", "/r", "/t", "0"])


def clipboard_get():
    return ps("Get-Clipboard -Raw").stdout


def clipboard_set(text):
    subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                   input=text, text=True, creationflags=NO_WINDOW)


class _KI(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _INP(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KI), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def type_text(text):
    for ch in text:
        if ch == "\n":
            pyautogui.press("enter")
            continue
        data = ch.encode("utf-16-le")
        for code in (int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)):
            for flags in (0x0004, 0x0006):  # KEYEVENTF_UNICODE, + KEYUP
                i = _INP(type=1)
                i.ki = _KI(0, code, flags, 0, 0)
                ctypes.windll.user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INP))


def drives():
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


# ---------------------------------------------------------------- discovery
SHOW_WINDOW = threading.Event()


def discovery(sock):
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data.startswith(b"PCBRIDGE?"):
                sock.sendto(json.dumps({"name": socket.gethostname(), "port": PORT, "macs": MACS}).encode(), addr)
                log(f"Phone {addr[0]} found this PC")
            elif data == b"PCBRIDGE_SHOW" and addr[0] == "127.0.0.1":
                SHOW_WINDOW.set()
        except Exception:
            time.sleep(1)


# ---------------------------------------------------------------- web server
FAILS = {}
SEEN = set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def q(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def route(self):
        return urllib.parse.urlparse(self.path).path

    def authed(self):
        ip = self.client_address[0]
        if FAILS.get(ip, 0) >= 20:
            return False
        tok = self.q().get("t") or self.headers.get("X-Pin", "")
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pb":
                tok = tok or v
        if tok and secrets.compare_digest(tok, PIN):
            if ip not in SEEN:
                SEEN.add(ip)
                log(f"Phone {ip} connected")
            return True
        if tok:
            FAILS[ip] = FAILS.get(ip, 0) + 1
            log(f"Phone {ip} used a wrong PIN")
        return False

    def send(self, code=200, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json_body(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return {}

    def fail(self, e):
        log(f"Error on {self.route()}: {e}")
        try:
            self.send(500, {"error": str(e)})
        except Exception:
            pass

    def do_GET(self):
        if not self.authed():
            return self.send(401, {"error": "bad pin"})
        r, q = self.route(), self.q()
        try:
            if r == "/":
                page = (RES_DIR / "ui.html").read_text(encoding="utf-8").replace("__PIN__", PIN)
                self.send(200, page, "text/html; charset=utf-8", {"Set-Cookie": f"pb={PIN}; Path=/; Max-Age=31536000"})
            elif r == "/api/ping":
                self.send(200, {"name": socket.gethostname(), "macs": MACS})
            elif r == "/stream":
                self.stream()
            elif r == "/api/windows":
                self.send(200, windows())
            elif r == "/api/thumb":
                w = win_by_id(q["id"])
                if w.isMinimized:
                    return self.send(204)
                with mss.mss() as sct:
                    im = grab(sct, {"left": w.left, "top": w.top, "width": max(w.width, 1), "height": max(w.height, 1)})
                self.send(200, jpeg(im, 480, 55), "image/jpeg")
            elif r == "/api/ls":
                self.ls(q.get("path", ""))
            elif r == "/api/dl":
                self.download(q["path"])
            elif r == "/api/clip":
                self.send(200, {"text": clipboard_get()})
            else:
                self.send(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            self.fail(e)

    def stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=f")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        width = int(self.q().get("w", 1600))
        with mss.mss() as sct:
            mon = sct.monitors[1]
            while True:
                t = time.time()
                frame = jpeg(grab(sct, mon), width)
                self.wfile.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                                 str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
                time.sleep(max(0, 1 / 15 - (time.time() - t)))

    def ls(self, path):
        if not path:
            home = Path.home()
            items = [{"name": n, "path": str(home / n), "dir": True}
                     for n in ("Desktop", "Documents", "Downloads", "Pictures") if (home / n).exists()]
            items += [{"name": d, "path": d, "dir": True} for d in drives()]
            return self.send(200, {"path": "", "parent": None, "items": items})
        p = Path(path)
        items = []
        for e in sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                if e.name.startswith(("$", ".")) or e.name in ("desktop.ini", "Thumbs.db"):
                    continue
                items.append({"name": e.name, "path": str(e), "dir": e.is_dir(),
                              "size": 0 if e.is_dir() else e.stat().st_size})
            except Exception:
                pass
        self.send(200, {"path": str(p), "parent": "" if p.parent == p else str(p.parent), "items": items})

    def download(self, path):
        p = Path(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(p.stat().st_size))
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(p.name))
        self.end_headers()
        with open(p, "rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)
        log(f"Sent {p.name} to phone")

    def do_PUT(self):
        if not self.authed():
            return self.send(401, {"error": "bad pin"})
        if self.route() != "/api/up":
            return self.send(404)
        q = self.q()
        try:
            dest = Path(q.get("dir") or Path.home() / "Downloads") / Path(q["name"]).name
            n = int(self.headers.get("Content-Length", 0))
            with open(dest, "wb") as f:
                while n > 0:
                    chunk = self.rfile.read(min(n, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    n -= len(chunk)
            log(f"Received {dest.name} from phone")
            self.send(200, {"saved": str(dest)})
        except Exception as e:
            self.fail(e)

    def do_POST(self):
        if not self.authed():
            return self.send(401, {"error": "bad pin"})
        r, d = self.route(), self.json_body()
        try:
            if r == "/api/input":
                self.input(d)
            elif r == "/api/focus":
                w = win_by_id(d["id"])
                if w.isMinimized:
                    w.restore()
                try:
                    pyautogui.press("alt")
                    w.activate()
                except Exception:
                    pass
            elif r == "/api/close":
                win_by_id(d["id"]).close()
            elif r == "/api/clip":
                clipboard_set(d.get("text", ""))
            elif r == "/api/power":
                threading.Timer(1.0, power, [d.get("a")]).start()
            elif r == "/api/open":
                os.startfile(d["path"])
            else:
                return self.send(404)
            self.send(200, {"ok": True})
        except Exception as e:
            self.fail(e)

    def input(self, d):
        a = d.get("a")
        mon = primary()
        if "x" in d:
            x = mon["left"] + float(d["x"]) * mon["width"]
            y = mon["top"] + float(d["y"]) * mon["height"]
        if a == "move":
            pyautogui.moveTo(x, y)
        elif a == "click":
            pyautogui.click(x, y)
        elif a == "dbl":
            pyautogui.doubleClick(x, y)
        elif a == "rclick":
            pyautogui.rightClick(x, y)
        elif a == "down":
            pyautogui.mouseDown(x, y)
        elif a == "up":
            pyautogui.mouseUp(x, y)
        elif a == "scroll":
            pyautogui.scroll(int(d.get("dy", 0)))
        elif a == "type":
            type_text(d.get("text", ""))
        elif a == "key":
            keys = d.get("keys", [])
            pyautogui.hotkey(*keys) if len(keys) > 1 else pyautogui.press(keys[0])


# ---------------------------------------------------------------- startup + self-setup
def start_services():
    global MACS
    MACS = mac_addresses()

    # Web server (what the phone talks to)
    check("server", "busy", "Starting...")
    try:
        ThreadingHTTPServer.daemon_threads = True
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    except OSError as e:
        check("server", "bad", f"Port {PORT} is in use by another program ({e.strerror}). Restart the PC and open PC Bridge again.")
    except Exception as e:
        check("server", "bad", f"Could not start: {e}")

    # Phone discovery
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("", DISCOVERY_PORT))
        threading.Thread(target=discovery, args=(s,), daemon=True).start()
        check("discovery", "ok", "Phones on your Wi-Fi can find this PC")
    except Exception as e:
        check("discovery", "bad", f"Auto-find is off ({e}). Use 'Add by IP address' in the app.")

    run_checks(first=True)


def run_checks(first=False):
    threading.Thread(target=_checks, args=(first,), daemon=True).start()


def _checks(first):
    # Self-test: can we reach our own server?
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=3)
        check("server", "bad", "Something else answered on the port.")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            check("server", "ok", f"Running on port {PORT}")
        else:
            check("server", "bad", f"Server replied with error {e.code}")
    except Exception as e:
        check("server", "bad", f"Not reachable ({e})")

    # Network
    ips = lan_ips()
    if not ips:
        check("network", "bad", "No network found. Connect the PC to your Wi-Fi/router.")
    else:
        prof = ps("(Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory) -join ','").stdout.strip()
        if "Public" in prof and is_admin():
            ps("Get-NetConnectionProfile | Where-Object NetworkCategory -eq 'Public' | Set-NetConnectionProfile -NetworkCategory Private")
            prof = ps("(Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory) -join ','").stdout.strip()
        if "Public" in prof:
            check("network", "warn", f"IP {', '.join(ips)} - network is set to Public, which can block the phone")
        else:
            check("network", "ok", f"IP {', '.join(ips)}")
        EVENTS.put(("ip", None, None, ips[0]))

    if not is_admin():
        for k in ("firewall", "startup", "wake"):
            check(k, "warn", "Needs admin - close and re-open PC Bridge, click Yes")
        return

    # Firewall
    try:
        for proto, port in (("TCP", PORT), ("UDP", DISCOVERY_PORT)):
            name = f"PC Bridge {proto}"
            run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])
            r = run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={name}", "dir=in", "action=allow",
                     f"protocol={proto}", f"localport={port}", "profile=any"])
            if r.returncode != 0:
                raise RuntimeError((r.stdout + r.stderr).strip())
        if FROZEN:
            run(["netsh", "advfirewall", "firewall", "delete", "rule", "name=PC Bridge App"])
            run(["netsh", "advfirewall", "firewall", "add", "rule", "name=PC Bridge App", "dir=in", "action=allow",
                 f"program={sys.executable}", "profile=any"])
        check("firewall", "ok", "Allowed through Windows Firewall")
    except Exception as e:
        check("firewall", "bad", f"Could not open firewall: {e}")

    # Start with Windows
    try:
        tr = f'"{sys.executable}" --background' if FROZEN else f'{EXE} --background'
        r = run(["schtasks", "/create", "/tn", "PC Bridge", "/tr", tr, "/sc", "onlogon", "/rl", "highest", "/f"])
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr).strip())
        check("startup", "ok", "Starts automatically when you log in")
    except Exception as e:
        check("startup", "bad", f"Could not add to startup: {e}")

    # Wake-on-LAN (turn on from phone)
    try:
        if first or not CFG.get("wol_done"):
            ps("Get-NetAdapter -Physical | ForEach-Object { "
               "Set-NetAdapterPowerManagement -Name $_.Name -WakeOnMagicPacket Enabled -ErrorAction SilentlyContinue; "
               "Set-NetAdapterAdvancedProperty -Name $_.Name -DisplayName 'Wake on Magic Packet' -DisplayValue 'Enabled' -ErrorAction SilentlyContinue; "
               "powercfg /deviceenablewake $_.InterfaceDescription 2>$null }")
            run(["powercfg", "/h", "off"])  # no Fast Startup, so wake works after Shut down too
            CFG["wol_done"] = True
            save_cfg(CFG)
        wired = ps("(Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Up' -and $_.MediaType -eq '802.3' }).Count").stdout.strip()
        ok = ps("(Get-NetAdapterPowerManagement -ErrorAction SilentlyContinue | Where-Object WakeOnMagicPacket -eq 'Enabled').Count").stdout.strip()
        if ok in ("", "0"):
            check("wake", "warn", "Network card doesn't support wake-up. Phone can't turn this PC on.")
        elif wired in ("", "0"):
            check("wake", "warn", "On - but PC is on Wi-Fi. Wake from sleep may not work; an Ethernet cable is best.")
        else:
            check("wake", "ok", "On - phone can wake this PC (from off also needs Wake-on-LAN in BIOS)")
    except Exception as e:
        check("wake", "bad", f"Could not set up wake-up: {e}")


# ---------------------------------------------------------------- window
def gui(background):
    import tkinter as tk

    BG, CARD, FG, MUTED = "#0f1115", "#171a21", "#e8e8ea", "#8a8f9c"
    COL = {"ok": "#3ecf8e", "bad": "#ff6b6b", "warn": "#f5b74e", "busy": "#6ea8ff"}
    ICON = {"ok": "✔", "bad": "✖", "warn": "!", "busy": "…"}

    root = tk.Tk()
    root.title("PC Bridge")
    root.configure(bg=BG)
    root.geometry("520x640")
    root.minsize(460, 560)
    F = "Segoe UI"

    tk.Label(root, text="PC Bridge", font=(F, 20, "bold"), bg=BG, fg=FG).pack(anchor="w", padx=24, pady=(20, 0))
    head = tk.Label(root, text="Starting...", font=(F, 11), bg=BG, fg=COL["busy"])
    head.pack(anchor="w", padx=24)

    pin_card = tk.Frame(root, bg=CARD)
    pin_card.pack(fill="x", padx=24, pady=16)
    tk.Label(pin_card, text="PIN for the phone app", font=(F, 10), bg=CARD, fg=MUTED).pack(anchor="w", padx=16, pady=(12, 0))
    row = tk.Frame(pin_card, bg=CARD)
    row.pack(fill="x", padx=16, pady=(0, 4))
    tk.Label(row, text=f"{PIN[:4]} {PIN[4:]}", font=(F, 30, "bold"), bg=CARD, fg=FG).pack(side="left")

    def copy_pin():
        root.clipboard_clear(); root.clipboard_append(PIN); copy_btn.config(text="Copied")
        root.after(1500, lambda: copy_btn.config(text="Copy"))
    copy_btn = tk.Button(row, text="Copy", command=copy_pin, font=(F, 10), bg="#222733", fg=FG, bd=0,
                         activebackground="#2f3542", activeforeground=FG, padx=12, pady=4)
    copy_btn.pack(side="right")
    ip_lbl = tk.Label(pin_card, text=f"{socket.gethostname()}", font=(F, 10), bg=CARD, fg=MUTED)
    ip_lbl.pack(anchor="w", padx=16, pady=(0, 12))

    checks = tk.Frame(root, bg=BG)
    checks.pack(fill="x", padx=24)
    rows = {}
    for key, title in (("server", "Phone connection"), ("discovery", "Auto-find on Wi-Fi"), ("network", "Network"),
                       ("firewall", "Firewall"), ("startup", "Start with Windows"), ("wake", "Turn on from phone")):
        f = tk.Frame(checks, bg=BG)
        f.pack(fill="x", pady=3)
        ic = tk.Label(f, text=ICON["busy"], width=2, font=(F, 12, "bold"), bg=BG, fg=COL["busy"])
        ic.pack(side="left", anchor="n")
        box = tk.Frame(f, bg=BG)
        box.pack(side="left", fill="x", expand=True)
        tk.Label(box, text=title, font=(F, 11), bg=BG, fg=FG).pack(anchor="w")
        d = tk.Label(box, text="Checking...", font=(F, 9), bg=BG, fg=MUTED, wraplength=420, justify="left")
        d.pack(anchor="w")
        rows[key] = (ic, d)
    states = {}

    tk.Label(root, text="Activity", font=(F, 10), bg=BG, fg=MUTED).pack(anchor="w", padx=24, pady=(14, 2))
    logbox = tk.Text(root, height=6, bg=CARD, fg=MUTED, bd=0, font=("Consolas", 9), wrap="word", padx=10, pady=8)
    logbox.pack(fill="both", expand=True, padx=24)
    logbox.config(state="disabled")

    btns = tk.Frame(root, bg=BG)
    btns.pack(fill="x", padx=24, pady=14)

    def button(text, cmd, side="left"):
        tk.Button(btns, text=text, command=cmd, font=(F, 10), bg="#222733", fg=FG, bd=0, padx=14, pady=6,
                  activebackground="#2f3542", activeforeground=FG).pack(side=side, padx=(0, 8))
    button("Check again", lambda: run_checks())
    button("Open log", lambda: os.startfile(LOG_FILE) if LOG_FILE.exists() else None)
    button("Quit", lambda: (root.destroy(), os._exit(0)), side="right")

    def refresh_head():
        if any(s == "bad" for s in states.values()):
            head.config(text="Something needs fixing - see the red items below", fg=COL["bad"])
        elif states.get("server") == "ok":
            head.config(text="Running - open PC Bridge on your phone and tap Find my PC", fg=COL["ok"])

    def pump():
        try:
            while True:
                kind, key, state, text = EVENTS.get_nowait()
                if kind == "check" and key in rows:
                    ic, d = rows[key]
                    ic.config(text=ICON[state], fg=COL[state])
                    d.config(text=text, fg=COL["bad"] if state == "bad" else MUTED)
                    states[key] = state
                    refresh_head()
                elif kind == "ip":
                    ip_lbl.config(text=f"{socket.gethostname()}  ·  {text}   (use this for 'Add by IP address')")
                elif kind == "log":
                    logbox.config(state="normal"); logbox.insert("end", text + "\n"); logbox.see("end")
                    logbox.config(state="disabled")
        except queue.Empty:
            pass
        if SHOW_WINDOW.is_set():
            SHOW_WINDOW.clear()
            root.deiconify(); root.lift(); root.focus_force()
        root.after(200, pump)

    # Closing the window keeps PC Bridge running (minimised); "Quit" really stops it.
    root.protocol("WM_DELETE_WINDOW", root.iconify)
    if background:
        root.iconify()
    root.after(100, pump)
    root.mainloop()


def already_running():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=1.5)
    except urllib.error.HTTPError as e:
        return e.code == 401
    except Exception:
        return False
    return False


def main():
    if already_running():  # bring the existing window to the front instead
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"PCBRIDGE_SHOW", ("127.0.0.1", DISCOVERY_PORT))
        return
    log("PC Bridge started")
    threading.Thread(target=start_services, daemon=True).start()
    gui(background="--background" in sys.argv)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        try:
            LOG_FILE.open("a").write(err)
        except Exception:
            pass
        ctypes.windll.user32.MessageBoxW(0, err[-1500:], "PC Bridge - error", 0x10)
