# Agente en Raspberry Pi 5

Flujo completo para poner un Pi nuevo a correr el agente, protegido (binario nativo
compilado con Nuitka, no Python plano) y con aprovisionamiento automatico (sin pedir
Client ID/API Key por pantalla - se precargan antes del primer arranque).

## 1. Preparar la tarjeta SD

Flashear **Raspberry Pi OS Lite** (64-bit, sin escritorio - el agente corre headless,
como servicio). Con Raspberry Pi Imager se puede preconfigurar hostname, usuario
`pi`, WiFi/Ethernet y SSH desde ahi mismo, antes de encender el Pi por primera vez.

## 2. Copiar este proyecto al Pi

```bash
scp -r MiningAgent pi@<ip-del-pi>:~/MiningAgent
ssh pi@<ip-del-pi>
```

## 3. Compilar el binario protegido

```bash
sudo apt update && sudo apt install -y python3-venv python3-dev build-essential
cd ~/MiningAgent/pi
chmod +x build_pi.sh
./build_pi.sh
```

Esto deja el binario en `/home/pi/biteam-agent/biteam-agent` - ya no hace falta
Python instalado para correrlo, ni queda el codigo fuente legible ahi.

## 4. Provisionar las credenciales de esta mina

```bash
cd ~/MiningAgent/pi
python3 provision_pi.py --client-id TU_CLIENT_ID --api-key TU_API_KEY --agent-name "Nombre de la mina"
```

(`--agent-name` es opcional - si se omite, se puede renombrar despues desde la
pestana Agentes del ERP.)

## 5. Instalar el servicio (arranca solo, sin login, se reinicia si truena)

```bash
sudo cp ~/MiningAgent/pi/biteam-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now biteam-agent
```

## 6. Verificar

```bash
sudo systemctl status biteam-agent
tail -f /home/pi/biteam-agent/agent.log
```

Y confirmar que la mina ya aparece sola en la pestana **Agentes** del ERP.

## Para clonar el Pi a otras minas despues

Una vez armado y probado un Pi, se puede clonar la tarjeta SD entera (con el binario
ya compilado) para las siguientes minas - solo hay que **borrar
`~/BiTeamAgent/agent_setup.json` y `~/biteam-agent/agent.lock`** antes de clonar (o en
el primer arranque del clon), y volver a correr `provision_pi.py` con las credenciales
de la mina nueva - si no, dos Pis compartirian la misma identidad (`agent_uid`) y se
pisarian en el sistema central.
