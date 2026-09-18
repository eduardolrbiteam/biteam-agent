# BiTeam Agent

Agente que corre en la PC/Pi de cada mina: escanea los mineros ASIC de la red local,
controla el relé Modbus de extractores, y reporta todo a la API central de BiTeam ERP.

Repositorio público a propósito - el código no trae ninguna API key ni secreto
embebido (siempre se cargan por fuera, en `agent_setup.json` o variables de entorno).

- `src/` (raíz de este repo): código del agente para Windows (PyInstaller).
- `pi/`: herramientas para desplegarlo en Raspberry Pi (Nuitka + systemd).

## Publicar una version nueva

1. Compilar `Agente Start.exe` (`pyinstaller "Agente Start.spec" --noconfirm`).
2. Crear un tag (`vX.Y.Z`) y un GitHub Release con ese tag, subiendo el `.exe`
   compilado como asset.
3. Actualizar `AGENT_LATEST_VERSION` en el backend al nuevo tag.
4. Desde el ERP (pestaña Agentes), el botón "Actualizar agente" hace que cada agente
   se descargue esa version y se reinicie solo.
