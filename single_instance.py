import os
import sys

MUTEX_NAME = "Global\\MiningAgent_SingleInstanceMutex"
ERROR_ALREADY_EXISTS = 183
BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
LOCK_FILE = os.path.join(BASE_DIR, "agent.lock")

_lock_file_handle = None  # se mantiene abierto mientras el proceso vive (Linux) - si
# se cierra, el flock se libera solo, asi que no se puede usar 'with' aqui.


def _write_lock_file():
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))


def _show_already_running_message():
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo(
        "Mining Agent",
        "El agente ya esta activo.\nUsa 'Agente Stop' para detenerlo.",
    )
    root.destroy()


def _ensure_single_instance_windows():
    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        _show_already_running_message()
        sys.exit(0)
    _write_lock_file()


def _ensure_single_instance_posix():
    """En el Pi no hay sesion grafica - nada de popups, solo se loguea a stdout (systemd
    ya captura eso en el journal) y se termina limpio si otra instancia ya tiene el
    archivo bloqueado."""
    import fcntl

    global _lock_file_handle
    _lock_file_handle = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("El agente ya esta activo - terminando esta instancia.")
        sys.exit(0)
    _lock_file_handle.write(str(os.getpid()))
    _lock_file_handle.flush()


def ensure_single_instance():
    """Si ya hay un agente corriendo, avisa y termina este proceso. Si no, registra
    este proceso (PID en agent.lock) como el activo, para que 'Agente Stop' lo detenga."""
    if sys.platform == "win32":
        _ensure_single_instance_windows()
    else:
        _ensure_single_instance_posix()
