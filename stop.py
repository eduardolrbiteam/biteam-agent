import json
import os
import signal
import subprocess
import sys
import tkinter as tk

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
LOCK_FILE = os.path.join(BASE_DIR, "agent.lock")

# Misma ubicacion que config.py (%LOCALAPPDATA%, no junto al .exe - ver ese archivo para
# el porque). Ambos archivos deben apuntar al mismo lugar o "Stop" terminaria borrando un
# agent_setup.json viejo que ya nadie usa, dejando las credenciales activas sin querer.
_APPDATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BiTeamAgent")
SETUP_FILE = os.path.join(_APPDATA_DIR, "agent_setup.json")


def do_stop():
    if os.path.exists(LOCK_FILE):
        try:
            pid = int(open(LOCK_FILE).read().strip())
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                # En el Pi normalmente se detiene con "systemctl stop", no con este
                # script - esto queda solo por si alguna vez se invoca a mano.
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass

    # Desactiva el agente: la proxima vez que se corra "Agente Start" debe volver a pedir
    # Client ID + API Key, no reconectarse solo. PERO agent_uid y agent_name NO se tocan -
    # son la identidad estable de esta instalacion; borrarlos (como hacia antes este
    # archivo) hace que "Start" genere una identidad nueva y duplique la mina en la nube.
    if os.path.exists(SETUP_FILE):
        try:
            with open(SETUP_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data.pop("client_id", None)
        data.pop("api_key", None)
        try:
            with open(SETUP_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass


def main():
    root = tk.Tk()
    root.title("Mining Agent")
    root.resizable(False, False)

    tk.Label(root, text="¿Desea pausar el escaneo?", font=("Segoe UI", 12, "bold")).pack(padx=24, pady=(20, 16))

    def on_stop():
        do_stop()
        root.destroy()

    tk.Button(root, text="Stop", width=16, command=on_stop, bg="#e3564b", fg="white").pack(pady=(0, 20))
    root.eval("tk::PlaceWindow . center")
    root.mainloop()


if __name__ == "__main__":
    main()
