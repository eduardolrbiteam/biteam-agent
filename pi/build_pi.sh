#!/usr/bin/env bash
# Compila el agente a un binario nativo ARM64 con Nuitka.
#
# IMPORTANTE: corre esto EN el propio Raspberry Pi, no en la PC de Windows - Nuitka
# compila para la arquitectura de la maquina donde se ejecuta, no hace cross-compile
# de Windows a ARM64 Linux.
#
# Prerequisitos (una sola vez, en el Pi):
#   sudo apt update && sudo apt install -y python3-venv python3-dev build-essential
set -e

cd "$(dirname "$0")/../src"

python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install nuitka

mkdir -p /home/pi/biteam-agent

python3 -m nuitka \
  --onefile \
  --follow-imports \
  --remove-output \
  --output-dir=/home/pi/biteam-agent \
  --output-filename=biteam-agent \
  main.py

deactivate

echo ""
echo "Listo: /home/pi/biteam-agent/biteam-agent"
echo "Siguiente paso: python3 provision_pi.py --client-id ... --api-key ..."
echo "Despues: instalar biteam-agent.service (ver README.md)"
