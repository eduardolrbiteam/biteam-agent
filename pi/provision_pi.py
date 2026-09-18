"""Aprovisiona un Raspberry Pi nuevo con las credenciales de una mina, para que el
agente arranque ya activado (sin pedir Client ID/API Key por pantalla - aqui no hay
pantalla, el Pi corre headless). Se corre UNA sola vez por Pi, antes de instalar el
servicio systemd.

Uso:
    python3 provision_pi.py --client-id 1 --api-key TU_API_KEY --agent-name "Apodaca 2"

--agent-name es opcional - si se omite, el agente se pone un nombre provisional al
arrancar y se puede renombrar despues desde la pestana Agentes del ERP.
"""
import argparse
import json
import os
import stat


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--api-key", required=True)
    parser.add_argument("--agent-name", default=None)
    args = parser.parse_args()

    appdata_dir = os.path.join(os.path.expanduser("~"), "BiTeamAgent")
    os.makedirs(appdata_dir, exist_ok=True)
    setup_file = os.path.join(appdata_dir, "agent_setup.json")

    if os.path.exists(setup_file):
        print(f"Ya existe {setup_file} - este Pi ya estaba provisionado.")
        print("Borra el archivo primero si de verdad quieres re-provisionarlo (perderias el agent_uid actual).")
        return

    data = {"client_id": args.client_id, "api_key": args.api_key}
    if args.agent_name:
        data["agent_name"] = args.agent_name
    # agent_uid NO se genera aqui - el agente lo genera solo en su primer arranque
    # (a partir del hostname de este Pi), exactamente igual que en la PC de Windows.

    with open(setup_file, "w") as f:
        json.dump(data, f, indent=2)
    # Solo el dueno del archivo puede leerlo/escribirlo - la api_key no queda
    # legible para cualquier otro usuario del sistema.
    os.chmod(setup_file, stat.S_IRUSR | stat.S_IWUSR)

    print(f"Listo: {setup_file} escrito y protegido (permisos 600).")
    print("Ya puedes instalar y arrancar el servicio (ver biteam-agent.service).")


if __name__ == "__main__":
    main()
