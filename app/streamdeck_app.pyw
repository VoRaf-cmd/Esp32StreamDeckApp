import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import unicodedata
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
DEFAULT_PAGES = 3
MAX_PAGES = 10
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
        for _ in range(DEFAULT_PAGES)
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
        saved_pages = data.get("pages", [])[:MAX_PAGES]
        if saved_pages:
            cfg["pages"] = []
            for page in saved_pages:
                buttons = []
                for btn in page.get("buttons", [])[:N_BUTTONS]:
                    buttons.append({
                        "label": btn.get("label", ""),
                        "type": btn.get("type", "none"),
                        "value": btn.get("value", ""),
                    })
                buttons.extend({"label": "", "type": "none", "value": ""}
                               for _ in range(N_BUTTONS - len(buttons)))
                cfg["pages"].append({"buttons": buttons})
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
def send_hotkey(combo, hold=0.12):
    # segura as teclas um instante: alguns programas (OBS, Discord) perdem toques muito rápidos
    keyboard.press(combo)
    try:
        time.sleep(hold)
    finally:
        keyboard.release(combo)


def clean_label(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return text.replace("\n", " ").replace("\r", " ").strip()[:20]


def run_action(btn):
    t = btn.get("type", "none")
    v = btn.get("value", "").strip()
    if t == "none" or not v:
        return
    try:
        if t in ("hotkey", "media"):
            send_hotkey(v)
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
        self.resend = threading.Event()

    def send_labels(self, ser):
        cfg = self.get_cfg()
        ser.write(f"C,{len(cfg['pages'])}\n".encode("ascii"))
        time.sleep(0.03)
        for p, page in enumerate(cfg["pages"]):
            for b, btn in enumerate(page["buttons"]):
                text = clean_label(btn.get("label", ""))
                ser.write(f"L,{p},{b},{text}\n".encode("ascii"))
                time.sleep(0.03)

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
                with serial.Serial(port, BAUD, timeout=0.2) as ser:
                    time.sleep(2)
                    self.state["status"] = f"Conectado em {port}"
                    self.send_labels(ser)
                    while not self.stop_flag.is_set():
                        if self.resend.is_set():
                            self.resend.clear()
                            self.send_labels(ser)
                        line = ser.readline().decode(errors="ignore").strip()
                        if line:
                            self.handle(line, volume)
            except (serial.SerialException, OSError):
                self.state["status"] = "Desconectado. Tentando de novo..."
                time.sleep(2)


# ---------------- Interface ----------------
class ButtonRow(ctk.CTkFrame):
    def __init__(self, master, index, data):
        super().__init__(master, corner_radius=14, fg_color=("#F3F5FA", "#1B2232"),
                         border_width=1, border_color=("#E4E8F0", "#293247"))
        self.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(self, text=f"Botão {index + 1}", width=64,
                     text_color=("#4D5B78", "#A9B8D6"),
                     font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, padx=(14, 6), pady=13)
        self.label = ctk.CTkEntry(self, width=130, placeholder_text="Nome", corner_radius=9,
                                  border_color=("#DCE2ED", "#35415A"))
        self.label.grid(row=0, column=1, padx=6)
        self.type_menu = ctk.CTkOptionMenu(self, values=list(TYPE_VALUE), width=190, command=self.on_type,
                                           corner_radius=9, fg_color="#536DFE", button_color="#465CE0",
                                           button_hover_color="#354BC7")
        self.type_menu.grid(row=0, column=2, padx=6)
        self.value = ctk.CTkEntry(self, corner_radius=9, border_color=("#DCE2ED", "#35415A"))
        self.value.grid(row=0, column=3, padx=6, sticky="ew")
        self.btn_extra = ctk.CTkButton(self, width=110, command=self.on_extra, corner_radius=9,
                                       fg_color=("#E3E9FF", "#29365C"),
                                       hover_color=("#D3DCFF", "#354674"),
                                       text_color=("#3449B8", "#C4CEFF"))
        self.btn_extra.grid(row=0, column=4, padx=6)
        self.media_menu = ctk.CTkOptionMenu(self, values=list(MEDIA_VALUE), width=130, command=self.on_media,
                                            corner_radius=9)
        self.media_menu.grid(row=0, column=4, padx=6)
        ctk.CTkButton(self, text="Testar", width=70, corner_radius=9,
                      fg_color=("#E8EBF2", "#30394D"), hover_color=("#D8DDEA", "#3B4760"),
                      text_color=("#34405A", "#E0E7F5"),
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
        self.configure(fg_color=("#F6F7FB", "#111722"))
        self._fade_job = None
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
        top = ctk.CTkFrame(self, corner_radius=18, fg_color=("#FFFFFF", "#1A2231"),
                           border_width=1, border_color=("#E7EAF1", "#283349"))
        top.pack(fill="x", padx=22, pady=(20, 10))
        brand = ctk.CTkFrame(top, fg_color="transparent")
        brand.pack(side="left", padx=18, pady=13)
        ctk.CTkLabel(brand, text="◈", text_color="#7185FF",
                     font=ctk.CTkFont(size=27, weight="bold")).pack(side="left", padx=(0, 10))
        title_stack = ctk.CTkFrame(brand, fg_color="transparent")
        title_stack.pack(side="left")
        ctk.CTkLabel(title_stack, text="Stream Deck", anchor="w",
                     font=ctk.CTkFont(size=20, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(title_stack, text="ESP32  ·  PAINEL DE CONTROLE", anchor="w",
                     text_color=("#818BA0", "#8794AC"),
                     font=ctk.CTkFont(size=10, weight="bold")).pack(anchor="w", pady=(1, 0))
        metrics = ctk.CTkFrame(top, fg_color="transparent")
        metrics.pack(side="right", padx=18, pady=12)
        self.lbl_status = ctk.CTkLabel(metrics, text="", corner_radius=10,
                                       fg_color=("#EEF1F7", "#293247"),
                                       font=ctk.CTkFont(size=11, weight="bold"))
        self.lbl_status.pack(side="right", padx=(8, 0), ipadx=10, ipady=7)
        self.lbl_pot = ctk.CTkLabel(metrics, text="", text_color=("#66718A", "#A9B5CA"),
                                    font=ctk.CTkFont(size=12, weight="bold"))
        self.lbl_pot.pack(side="right", padx=12)
        self.lbl_last = ctk.CTkLabel(metrics, text="", text_color=("#3449B8", "#B8C4FF"),
                                     font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_last.pack(side="right", padx=12)

        page_controls = ctk.CTkFrame(self, fg_color="transparent")
        page_controls.pack(fill="x", padx=22, pady=(2, 0))
        self.page_count_label = ctk.CTkLabel(page_controls, text="")
        self.page_count_label.pack(side="left", padx=4)
        self.btn_add_page = ctk.CTkButton(page_controls, text="＋ Nova página", width=130,
                                          corner_radius=9, fg_color=("#E9EDFF", "#29365C"),
                                          hover_color=("#D9E1FF", "#354674"),
                                          text_color=("#3449B8", "#C4CEFF"), command=self.add_page)
        self.btn_add_page.pack(side="right", padx=(6, 0))
        self.btn_delete_page = ctk.CTkButton(page_controls, text="Excluir página", width=120,
                                             corner_radius=9, fg_color=("#FCE9E8", "#472B30"),
                                             hover_color=("#F7D9D7", "#60353B"),
                                             text_color=("#A44742", "#FFC0BB"), command=self.delete_page)
        self.btn_delete_page.pack(side="right", padx=6)

        self.tabs = ctk.CTkTabview(self)
        self.tabs.pack(fill="both", expand=True, padx=22, pady=8)
        self.tabs.configure(fg_color=("#FFFFFF", "#171F2D"),
                            segmented_button_fg_color=("#EEF1F7", "#222C3D"),
                            segmented_button_selected_color="#536DFE",
                            segmented_button_selected_hover_color="#455BE4",
                            segmented_button_unselected_color=("#EEF1F7", "#222C3D"),
                            segmented_button_unselected_hover_color=("#E2E7F0", "#2B374D"),
                            command=self.on_page_change)
        self.page_tab_names = []
        self._next_page_number = 1
        for p, page in enumerate(self.cfg["pages"]):
            self._create_page_ui(p, page)
        self._update_page_controls()

        bottom = ctk.CTkFrame(self, corner_radius=16, fg_color=("#FFFFFF", "#1A2231"),
                              border_width=1, border_color=("#E7EAF1", "#283349"))
        bottom.pack(fill="x", padx=22, pady=(8, 18))
        ctk.CTkLabel(bottom, text="DISPOSITIVO", text_color=("#7C879D", "#8491A9"),
                     font=ctk.CTkFont(size=10, weight="bold")).pack(side="left", padx=(17, 7), pady=14)
        self.port_menu = ctk.CTkOptionMenu(bottom, values=self.port_values(), width=140, corner_radius=9)
        self.port_menu.set("Automático" if self.cfg["port"] == "auto" else self.cfg["port"])
        self.port_menu.pack(side="left", padx=4)
        ctk.CTkButton(bottom, text="↻", width=34, corner_radius=9,
                      fg_color=("#E8EBF2", "#30394D"), hover_color=("#D8DDEA", "#3B4760"),
                      text_color=("#34405A", "#E0E7F5"),
                      command=self.refresh_ports).pack(side="left", padx=4)
        self.chk_volume = ctk.CTkCheckBox(bottom, text="Potenciômetro controla o volume")
        self.chk_volume.pack(side="left", padx=16)
        if self.cfg.get("volume", True):
            self.chk_volume.select()
        self.chk_auto = ctk.CTkCheckBox(bottom, text="Iniciar com o Windows", command=self.toggle_autostart)
        self.chk_auto.pack(side="left", padx=8)
        if is_autostart():
            self.chk_auto.select()
        self.btn_save = ctk.CTkButton(bottom, text="Salvar alterações", width=150, height=38,
                                      corner_radius=11, font=ctk.CTkFont(size=12, weight="bold"),
                                      fg_color="#536DFE", hover_color="#4359E0", command=self.save)
        self.btn_save.pack(side="right", padx=16)

    def port_values(self):
        return ["Automático"] + [p.device for p in serial.tools.list_ports.comports()]

    def refresh_ports(self):
        self.port_menu.configure(values=self.port_values())

    def _create_page_ui(self, index, page_data):
        name = f"Página {self._next_page_number}"
        self._next_page_number += 1
        tab = self.tabs.add(name)
        page_rows = []
        for b in range(N_BUTTONS):
            row = ButtonRow(tab, b, page_data["buttons"][b])
            row.pack(fill="x", padx=8, pady=7)
            page_rows.append(row)
        self.rows.append(page_rows)
        self.page_tab_names.append(name)

    def _update_page_controls(self):
        count = len(self.rows)
        self.page_count_label.configure(text=f"{count} de {MAX_PAGES} páginas")
        self.btn_add_page.configure(state="normal" if count < MAX_PAGES else "disabled")
        self.btn_delete_page.configure(state="normal" if count > 1 else "disabled")

    def add_page(self):
        if len(self.rows) >= MAX_PAGES:
            return
        page = {"buttons": [{"label": "", "type": "none", "value": ""}
                            for _ in range(N_BUTTONS)]}
        self._create_page_ui(len(self.rows), page)
        self.tabs.set(self.page_tab_names[-1])
        self._update_page_controls()

    def delete_page(self):
        if len(self.rows) <= 1:
            return
        from tkinter import messagebox
        selected_name = self.tabs.get()
        index = self.page_tab_names.index(selected_name)
        if not messagebox.askyesno("Excluir página", f"Excluir {selected_name} e suas ações?"):
            return
        self.tabs.delete(selected_name)
        self.rows.pop(index)
        self.page_tab_names.pop(index)
        self.tabs.set(self.page_tab_names[min(index, len(self.page_tab_names) - 1)])
        self._update_page_controls()

    def on_page_change(self):
        self.lbl_last.configure(text_color=("#536DFE", "#B8C4FF"))
        try:
            page_index = self.page_tab_names.index(self.tabs.get())
            rows = self.rows[page_index]
        except (ValueError, IndexError):
            return
        self._animate_rows(rows, 0)

    def _animate_rows(self, rows, step):
        """Briefly bring in the selected page rows with a soft accent fade."""
        dark = ctk.get_appearance_mode().lower() == "dark"
        background = ("#1B2232" if dark else "#F3F5FA")
        border = ("#293247" if dark else "#E4E8F0")
        accent = ("#354674" if dark else "#C9D2FF")
        def blend(start, end, amount):
            a = tuple(int(start[i:i + 2], 16) for i in (1, 3, 5))
            b = tuple(int(end[i:i + 2], 16) for i in (1, 3, 5))
            values = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
            return "#" + "".join(f"{value:02X}" for value in values)
        amount = min(1.0, step / 5)
        for row in rows:
            row.configure(fg_color=blend(accent, background, amount),
                          border_color=blend("#7185FF" if dark else "#7185FF", border, amount))
        if step < 5:
            self.after(28, self._animate_rows, rows, step + 1)

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
        self.worker.resend.set()
        self.btn_save.configure(text="✓  Salvo", fg_color="#22A884", hover_color="#1B9272")
        self.after(1600, lambda: self.btn_save.configure(
            text="Salvar alterações", fg_color="#536DFE", hover_color="#4359E0"))

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
        connected = s["status"].startswith("Conectado")
        status_color = "#DDF5EC" if connected else ("#FFF0D9" if "Procurando" in s["status"] else "#FDE6E5")
        status_dark = "#193C36" if connected else ("#44371F" if "Procurando" in s["status"] else "#472B30")
        self.lbl_status.configure(text=("●  " if connected else "○  ") + s["status"],
                                  fg_color=(status_color, status_dark),
                                  text_color=("#16785E" if connected else "#9B681B",
                                              "#8EE0C3" if connected else "#F1C77E"))
        self.lbl_last.configure(text="Último: " + str(s["last"]))
        self.lbl_pot.configure(text=f"◉  {s['pot']}%")
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
        self._fade_in()

    def _fade_in(self, step=0):
        """Fade the window in over a short interval when it opens from the tray."""
        try:
            self.attributes("-alpha", min(1.0, 0.25 + step * 0.15))
            if step < 5:
                self._fade_job = self.after(18, self._fade_in, step + 1)
        except Exception:
            pass

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
