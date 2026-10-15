import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import POINTER, cast
from pathlib import Path
from tkinter import filedialog

import customtkinter as ctk
import keyboard
import pystray
import serial
import serial.tools.list_ports
from PIL import Image, ImageDraw

APP_NAME = "StreamDeckESP32"
CONFIG_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
LOCK_PORT = 48731

if sys.stdout is None or sys.stderr is None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(CONFIG_DIR / "log.txt", "a", encoding="utf-8", buffering=1)
    except Exception:
        pass
BAUD = 115200
N_PAGES = 3
N_BUTTONS = 4
PORT_KEYS = ("CP210", "CH340", "CH910", "USB SERIAL", "USB-SERIAL")

TYPES = [
    ("Nenhum", "none"),
    ("Atalho de teclado", "hotkey"),
    ("Abrir programa ou arquivo", "program"),
    ("Abrir site", "url"),
    ("Tecla de mídia", "media"),
    ("Digitar texto", "text"),
]
TYPE_VALUE = dict(TYPES)
TYPE_LABEL = {v: k for k, v in TYPES}
MEDIA = [
    ("Play/Pause", "play/pause media"),
    ("Próxima faixa", "next track"),
    ("Faixa anterior", "previous track"),
    ("Mudo", "volume mute"),
    ("Parar", "stop media"),
]
MEDIA_VALUE = dict(MEDIA)
MEDIA_LABEL = {v: k for k, v in MEDIA}


# ---------------- Configuração ----------------
def default_config():
    pages = [
        {"buttons": [{"label": "", "type": "none", "value": ""} for _ in range(N_BUTTONS)]}
        for _ in range(N_PAGES)
    ]
    pages[0]["buttons"][0] = {"label": "Calculadora", "type": "program", "value": "calc.exe"}
    pages[0]["buttons"][1] = {"label": "Mutar Discord", "type": "hotkey", "value": "ctrl+shift+m"}
    return {"port": "auto", "volume": True, "pages": pages}


def load_config():
    cfg = default_config()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg["port"] = data.get("port", "auto")
        cfg["volume"] = data.get("volume", True)
        for p, page in enumerate(data.get("pages", [])[:N_PAGES]):
            for b, btn in enumerate(page.get("buttons", [])[:N_BUTTONS]):
                cfg["pages"][p]["buttons"][b] = {
                    "label": btn.get("label", ""),
                    "type": btn.get("type", "none"),
                    "value": btn.get("value", ""),
                }
    except Exception:
        pass
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------- Iniciar com o Windows ----------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_autostart():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except Exception:
        return False


def set_autostart(enable):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}" --minimized'
            else:
                exe = Path(sys.executable)
                pyw = exe.with_name("pythonw.exe")
                cmd = f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}" --minimized'
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------- Ações e volume ----------------
def run_action(btn):
    t = btn.get("type", "none")
    v = btn.get("value", "").strip()
    if t == "none" or not v:
        return
    try:
        if t in ("hotkey", "media"):
            keyboard.send(v)
        elif t == "program":
            try:
                os.startfile(v)
            except OSError:
                subprocess.Popen(v, shell=True)
        elif t == "url":
            webbrowser.open(v if "://" in v else "https://" + v)
        elif t == "text":
            keyboard.write(v)
    except Exception as e:
        print("Erro na ação:", e)


def get_volume_control():
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    speakers = AudioUtilities.GetSpeakers()
    if hasattr(speakers, "EndpointVolume"):
        return speakers.EndpointVolume
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


# ---------------- Leitura do ESP32 (thread) ----------------
class Worker(threading.Thread):
    def __init__(self, get_cfg, state):
        super().__init__(daemon=True)
        self.get_cfg = get_cfg
        self.state = state
        self.stop_flag = threading.Event()

    def find_port(self):
        port = self.get_cfg().get("port", "auto")
        if port != "auto":
            return port
        for p in serial.tools.list_ports.comports():
            desc = (p.description or "").upper()
            if any(k in desc for k in PORT_KEYS):
                return p.device
        return None

    def handle(self, line, volume):
        parts = line.split(",")
        cfg = self.get_cfg()
        try:
            if parts[0] == "P" and len(parts) == 2:
                value = int(parts[1])
                self.state["pot"] = value
                if cfg.get("volume", True) and volume is not None:
                    volume.SetMasterVolumeLevelScalar(value / 100, None)
            elif parts[0] == "B" and len(parts) == 3:
                page, btn = int(parts[1]), int(parts[2])
                data = cfg["pages"][page]["buttons"][btn]
                self.state["last"] = data.get("label") or f"Página {page + 1}, botão {btn + 1}"
                run_action(data)
        except (ValueError, IndexError, KeyError):
            pass
        except Exception as e:
            print("Erro:", e)

    def run(self):
        import comtypes
        comtypes.CoInitialize()
        volume = None
        try:
            volume = get_volume_control()
        except Exception as e:
            print("Sem controle de volume:", e)
        while not self.stop_flag.is_set():
            port = self.find_port()
            if not port:
                self.state["status"] = "Procurando o ESP32..."
                time.sleep(2)
                continue
            try:
                with serial.Serial(port, BAUD, timeout=1) as ser:
                    time.sleep(2)
                    self.state["status"] = f"Conectado em {port}"
                    while not self.stop_flag.is_set():
                        line = ser.readline().decode(errors="ignore").strip()
                        if line:
                            self.handle(line, volume)
            except (serial.SerialException, OSError):
                self.state["status"] = "Desconectado. Tentando de novo..."
                time.sleep(2)


# ---------------- Interface ----------------
class ButtonRow(ctk.CTkFrame):
    def __init__(self, master, index, data):
        super().__init__(master, corner_radius=10)
        self.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(self, text=f"Botão {index + 1}", width=64,
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=(12, 6), pady=12)
        self.label = ctk.CTkEntry(self, width=130, placeholder_text="Nome")
        self.label.grid(row=0, column=1, padx=6)
        self.type_menu = ctk.CTkOptionMenu(self, values=list(TYPE_VALUE), width=190, command=self.on_type)
        self.type_menu.grid(row=0, column=2, padx=6)
        self.value = ctk.CTkEntry(self)
        self.value.grid(row=0, column=3, padx=6, sticky="ew")
        self.btn_extra = ctk.CTkButton(self, width=110, command=self.on_extra)
        self.btn_extra.grid(row=0, column=4, padx=6)
        self.media_menu = ctk.CTkOptionMenu(self, values=list(MEDIA_VALUE), width=130, command=self.on_media)
        self.media_menu.grid(row=0, column=4, padx=6)
        ctk.CTkButton(self, text="Testar", width=70, fg_color="gray30", hover_color="gray40",
                      command=self.test).grid(row=0, column=5, padx=(6, 12))

        self.label.insert(0, data.get("label", ""))
        self.type_menu.set(TYPE_LABEL.get(data.get("type", "none"), "Nenhum"))
        self.set_value(data.get("value", ""))
        self.media_menu.set(MEDIA_LABEL.get(data.get("value", ""), "Play/Pause"))
        self.on_type()

    def set_value(self, text):
        self.value.delete(0, "end")
        self.value.insert(0, text)

    def current_type(self):
        return TYPE_VALUE[self.type_menu.get()]

    def on_type(self, _=None):
        t = self.current_type()
        hints = {
            "none": "",
            "hotkey": "ex.: ctrl+shift+m (use Gravar)",
            "program": "caminho do programa ou arquivo",
            "url": "ex.: youtube.com",
            "media": "",
            "text": "texto a digitar",
        }
        self.value.configure(placeholder_text=hints[t])
        self.btn_extra.grid_remove()
        self.media_menu.grid_remove()
        if t == "hotkey":
            self.btn_extra.configure(text="Gravar")
            self.btn_extra.grid()
        elif t == "program":
            self.btn_extra.configure(text="Escolher...")
            self.btn_extra.grid()
        elif t == "media":
            self.media_menu.grid()
            self.on_media()

    def on_media(self, _=None):
        self.set_value(MEDIA_VALUE[self.media_menu.get()])

    def on_extra(self):
        t = self.current_type()
        if t == "hotkey":
            self.record()
        elif t == "program":
            path = filedialog.askopenfilename(title="Escolha o programa ou arquivo")
            if path:
                self.set_value(path.replace("/", "\\"))

    def record(self):
        app = self.winfo_toplevel()
        self.btn_extra.configure(text="Pressione...", state="disabled")

        def work():
            try:
                hk = keyboard.read_hotkey(suppress=False)
            except Exception:
                hk = ""
            app.call_in_ui(lambda: self.finish_record(hk))

        threading.Thread(target=work, daemon=True).start()

    def finish_record(self, hk):
        self.btn_extra.configure(text="Gravar", state="normal")
        if hk:
            self.set_value(hk)

    def test(self):
        threading.Timer(2.0, run_action, [self.get()]).start()

    def get(self):
        return {
            "label": self.label.get().strip(),
            "type": self.current_type(),
            "value": self.value.get().strip(),
        }


class App(ctk.CTk):
    def __init__(self, start_hidden, lock_socket):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("Stream Deck ESP32")
        self.geometry("1020x640")
        self.minsize(960, 560)
        self.cfg = load_config()
        self.shared = {"status": "Iniciando...", "last": "-", "pot": 0}
        self.ui_queue = queue.Queue()
        self.rows = []
        self.build_ui()
        self.protocol("WM_DELETE_WINDOW", self.hide)

        self.worker = Worker(lambda: self.cfg, self.shared)
        self.worker.start()

        menu = pystray.Menu(
            pystray.MenuItem("Abrir", lambda: self.call_in_ui(self.show), default=True),
            pystray.MenuItem("Sair", lambda: self.call_in_ui(self.quit_all)),
        )
        self.tray = pystray.Icon(APP_NAME, make_icon(), "Stream Deck ESP32", menu)
        self.tray.run_detached()

        threading.Thread(target=self.listen_lock, args=(lock_socket,), daemon=True).start()
        self.after(100, self.poll)
        if start_hidden:
            self.withdraw()

    def build_ui(self):
        top = ctk.CTkFrame(self, corner_radius=12)
        top.pack(fill="x", padx=16, pady=(16, 8))
        self.lbl_status = ctk.CTkLabel(top, text="")
        self.lbl_status.pack(side="left", padx=16, pady=12)
        self.lbl_pot = ctk.CTkLabel(top, text="")
        self.lbl_pot.pack(side="right", padx=16)
        self.lbl_last = ctk.CTkLabel(top, text="")
        self.lbl_last.pack(side="right", padx=16)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=16, pady=8)
        for p in range(N_PAGES):
            tab = self.tabs.add(f"Página {p + 1}")
            page_rows = []
            for b in range(N_BUTTONS):
                row = ButtonRow(tab, b, self.cfg["pages"][p]["buttons"][b])
                row.pack(fill="x", padx=4, pady=5)
                page_rows.append(row)
            self.rows.append(page_rows)

        bottom = ctk.CTkFrame(self, corner_radius=12)
        bottom.pack(fill="x", padx=16, pady=(8, 16))
        ctk.CTkLabel(bottom, text="Porta:").pack(side="left", padx=(16, 6), pady=12)
        self.port_menu = ctk.CTkOptionMenu(bottom, values=self.port_values(), width=140)
        self.port_menu.set("Automático" if self.cfg["port"] == "auto" else self.cfg["port"])
        self.port_menu.pack(side="left", padx=4)
        ctk.CTkButton(bottom, text="↻", width=34, fg_color="gray30", hover_color="gray40",
                      command=self.refresh_ports).pack(side="left", padx=4)
        self.chk_volume = ctk.CTkCheckBox(bottom, text="Potenciômetro controla o volume")
        self.chk_volume.pack(side="left", padx=16)
        if self.cfg.get("volume", True):
            self.chk_volume.select()
        self.chk_auto = ctk.CTkCheckBox(bottom, text="Iniciar com o Windows", command=self.toggle_autostart)
        self.chk_auto.pack(side="left", padx=8)
        if is_autostart():
            self.chk_auto.select()
        self.btn_save = ctk.CTkButton(bottom, text="Salvar", width=110, command=self.save)
        self.btn_save.pack(side="right", padx=16)

    def port_values(self):
        return ["Automático"] + [p.device for p in serial.tools.list_ports.comports()]

    def refresh_ports(self):
        self.port_menu.configure(values=self.port_values())

    def toggle_autostart(self):
        try:
            set_autostart(bool(self.chk_auto.get()))
        except Exception as e:
            print("Erro no autostart:", e)

    def save(self):
        port = self.port_menu.get()
        self.cfg = {
            "port": "auto" if port == "Automático" else port,
            "volume": bool(self.chk_volume.get()),
            "pages": [{"buttons": [r.get() for r in rows]} for rows in self.rows],
        }
        save_config(self.cfg)
        self.btn_save.configure(text="Salvo!")
        self.after(1500, lambda: self.btn_save.configure(text="Salvar"))

    def call_in_ui(self, fn):
        self.ui_queue.put(fn)

    def poll(self):
        while True:
            try:
                self.ui_queue.get_nowait()()
            except queue.Empty:
                break
            except Exception as e:
                print("Erro na UI:", e)
        s = self.shared
        self.lbl_status.configure(text="Status: " + s["status"])
        self.lbl_last.configure(text="Último botão: " + str(s["last"]))
        self.lbl_pot.configure(text=f"Volume: {s['pot']}%")
        self.after(100, self.poll)

    def listen_lock(self, sock):
        while True:
            try:
                conn, _ = sock.accept()
                conn.close()
                self.call_in_ui(self.show)
            except OSError:
                break

    def hide(self):
        self.withdraw()

    def show(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def quit_all(self):
        self.worker.stop_flag.set()
        try:
            self.tray.stop()
        except Exception:
            pass
        self.destroy()
        os._exit(0)


def make_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 4, 60, 60), radius=10, fill=(83, 74, 183, 255))
    for r in range(2):
        for c in range(2):
            x, y = 14 + c * 20, 14 + r * 20
            d.rounded_rectangle((x, y, x + 16, y + 16), radius=3, fill=(255, 255, 255, 255))
    return img


def single_instance():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        try:
            c = socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=1)
            c.sendall(b"show")
            c.close()
        except OSError:
            pass
        sys.exit(0)


if __name__ == "__main__":
    lock = single_instance()
    try:
        App("--minimized" in sys.argv, lock).mainloop()
    finally:
        os._exit(0)
