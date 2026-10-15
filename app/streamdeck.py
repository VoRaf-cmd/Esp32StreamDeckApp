import subprocess
import time
import webbrowser
from ctypes import POINTER, cast

import pyautogui
import serial
import serial.tools.list_ports
from comtypes import CLSCTX_ALL
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

PORT = None  # deixe None para detectar sozinho, ou use "COM3"
BAUD = 115200


# ---------- Volume do Windows ----------
def get_volume_control():
    speakers = AudioUtilities.GetSpeakers()
    if hasattr(speakers, "EndpointVolume"):  # versões novas do pycaw
        return speakers.EndpointVolume
    interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


volume = get_volume_control()


def set_volume(percent):
    volume.SetMasterVolumeLevelScalar(percent / 100, None)


# ---------- Ações dos botões: (página, botão) -> função ----------
# Páginas e botões começam em 0: (0, 0) = página 1, botão 1
ACTIONS = {
    # Página 1: abrir programas
    (0, 0): lambda: subprocess.Popen("calc.exe"),
    (0, 1): lambda: subprocess.Popen("notepad.exe"),

    # Página 2: site e atalho de teclado
    (1, 0): lambda: webbrowser.open("https://www.youtube.com"),
    (1, 1): lambda: pyautogui.hotkey("ctrl", "shift", "m"),

    # Página 3: teclas de mídia
    (2, 0): lambda: pyautogui.press("playpause"),
    (2, 1): lambda: pyautogui.press("nexttrack"),
}


# ---------- Porta serial ----------
def find_port():
    if PORT:
        return PORT
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        desc = (p.description or "").upper()
        if any(k in desc for k in ("CP210", "CH340", "CH910", "USB SERIAL", "USB-SERIAL")):
            return p.device
    print("Portas encontradas:", [(p.device, p.description) for p in ports])
    raise SystemExit("Não achei o ESP32. Defina PORT = 'COMx' no topo do arquivo.")


def main():
    port = find_port()
    print(f"Conectando em {port}...")
    ser = serial.Serial(port, BAUD, timeout=1)
    time.sleep(2)  # o ESP32 reinicia ao abrir a porta
    print("Pronto. Ctrl+C para sair.")

    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        parts = line.split(",")

        if parts[0] == "P" and len(parts) == 2:
            set_volume(int(parts[1]))
        elif parts[0] == "B" and len(parts) == 3:
            key = (int(parts[1]), int(parts[2]))
            action = ACTIONS.get(key)
            print(f"Botão {key} -> {'ação' if action else 'sem ação'}")
            if action:
                action()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass