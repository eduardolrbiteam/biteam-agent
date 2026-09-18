import json
import os
import platform
import sys
import time
import uuid

# Se guarda en %LOCALAPPDATA% (carpeta local fija, nunca sincronizada por OneDrive) en
# vez de junto al .exe. Antes vivia junto al .exe, y si ese .exe quedaba dentro de una
# carpeta de OneDrive, un reinicio de la PC podia arrancar el agente antes de que OneDrive
# terminara de re-sincronizar el archivo - la lectura fallaba, se interpretaba como "no
# hay nada guardado" y se generaba una identidad nueva, duplicando la mina en el programa
# central (ya paso una vez).
_APPDATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "BiTeamAgent")
os.makedirs(_APPDATA_DIR, exist_ok=True)
SETUP_FILE = os.path.join(_APPDATA_DIR, "agent_setup.json")

# Ubicacion vieja (junto al .exe) - si ya habia una instalacion con la identidad guardada
# ahi, se migra una sola vez a la carpeta nueva para no perderla.
_LEGACY_SETUP_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "agent_setup.json")

# El servidor es fijo: la nube de BiTeam ERP. No hay servidor local ni el usuario lo
# configura - el agente solo pide Client ID, API Key y nombre de la mina.
API_BASE = "https://biteam-api-2ysez.ondigitalocean.app"


def _read_json_with_retries(path, attempts=6, delay=0.5):
    """Si el archivo existe pero por un instante no se puede leer (ej. bloqueado por el
    antivirus justo al arrancar con la PC), reintenta antes de rendirse - un solo intento
    fallido no debe tratarse como "no hay nada guardado", porque eso generaria una
    identidad nueva que le gana (sobre-escribe) a la real."""
    last_error = None
    for _ in range(attempts):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            last_error = e
            time.sleep(delay)
    raise RuntimeError(f"No se pudo leer {path} tras {attempts} intentos: {last_error}")


def _load_setup_file():
    if os.path.exists(SETUP_FILE):
        return _read_json_with_retries(SETUP_FILE)
    if os.path.exists(_LEGACY_SETUP_FILE):
        data = _read_json_with_retries(_LEGACY_SETUP_FILE)
        if data:
            _save_setup_file(data)  # migracion unica a la ubicacion nueva
        return data
    return {}


def _save_setup_file(data):
    with open(SETUP_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_setup():
    """Carga CLIENT_ID, AGENT_API_KEY (los de la EMPRESA - una sola api key para todas
    sus minas, estilo Foreman), AGENT_NAME (el nombre de ESTA mina) y AGENT_UID (id
    estable de esta instalacion, para poder renombrar la mina sin que se duplique en el
    programa central). Si no hay nada guardado ni variables de entorno, abre una ventana
    emergente para activarlo."""
    try:
        saved = _load_setup_file()
    except Exception as e:
        # No seguir: es preferible que el agente no arranque esta vez (y quede visible en
        # el log) a que arranque con una identidad nueva y duplique la mina en la nube.
        log_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "agent.log")
        try:
            with open(log_path, "a") as f:
                f.write(f"No se pudo leer {SETUP_FILE} - el agente NO va a arrancar para no duplicar la mina: {e}\n")
        except Exception:
            pass
        raise

    client_id = os.environ.get("AGENT_CLIENT_ID") or saved.get("client_id")
    api_key = os.environ.get("AGENT_API_KEY") or saved.get("api_key")
    agent_name = os.environ.get("AGENT_NAME") or saved.get("agent_name")
    agent_uid = saved.get("agent_uid")

    if not api_key:
        from setup_gui import run_setup_dialog
        activation = run_setup_dialog()
        if activation is None:
            sys.exit(0)
        client_id, api_key = activation

    if not agent_uid:
        # Se genera una sola vez (en base al nombre de esta PC, para que sea
        # reconocible) y queda fijo de por vida de esta instalacion en este archivo,
        # aunque despues cambien el nombre de la mina o hasta el de la PC. El sufijo
        # evita choques si dos PCs llegaran a compartir el mismo nombre de host.
        hostname = platform.node() or "pc"
        agent_uid = f"{hostname}-{uuid.uuid4().hex[:6]}"

    if not agent_name:
        # Nombre provisional hasta que lo renombren desde el programa central (pestana
        # Agentes) - no se le pide al usuario aqui.
        agent_name = f"Mina nueva {agent_uid[:8]}"

    _save_setup_file({
        "client_id": client_id, "api_key": api_key,
        "agent_name": agent_name, "agent_uid": agent_uid,
    })

    return client_id, api_key, agent_name, agent_uid


CLIENT_ID, AGENT_API_KEY, AGENT_NAME, AGENT_UID = load_setup()

INGEST_INTERVAL_SECONDS = int(os.environ.get("INGEST_INTERVAL_SECONDS", "60"))
COMMAND_POLL_INTERVAL_SECONDS = int(os.environ.get("COMMAND_POLL_INTERVAL_SECONDS", "5"))
MODBUS_STATUS_INTERVAL_SECONDS = int(os.environ.get("MODBUS_STATUS_INTERVAL_SECONDS", "30"))


def rack_name_for_ip(ip):
    third_octet = int(str(ip).split(".")[2])
    return f"R{third_octet}"
