"""
Control del modulo Waveshare Modbus POE ETH Relay (30 canales, usando 26).
Vendored desde C:\\Users\\eduar\\WaveshareRelay\\relay.py para que el agente sea autocontenido.
Requiere: pip install pymodbus
"""

import time

from pymodbus.client import ModbusTcpClient

IP = "192.168.101.10"
PORT = 502
SLAVE_ID = 1
NUM_CANALES = 26  # de los 30 disponibles en la placa


class WaveshareRelay:
    def __init__(self, ip=IP, port=PORT, slave_id=SLAVE_ID):
        self.client = ModbusTcpClient(ip, port=port, timeout=3)
        self.slave_id = slave_id

    def connect(self):
        if not self.client.connect():
            raise ConnectionError(f"No se pudo conectar a {self.client.comm_params.host}:{self.client.comm_params.port}")
        return True

    def close(self):
        self.client.close()

    def is_connected(self):
        return self.client.connected

    def leer_canales(self, count=NUM_CANALES):
        """Devuelve una lista de bool, una por canal (0..count-1)."""
        result = self.client.read_coils(address=0, count=count, device_id=self.slave_id)
        if result.isError():
            raise IOError(f"Error leyendo canales: {result}")
        return result.bits[:count]

    def set_canal(self, canal, estado):
        """Enciende (True) o apaga (False) un canal individual (0-indexado)."""
        result = self.client.write_coil(address=canal, value=bool(estado), device_id=self.slave_id)
        if result.isError():
            raise IOError(f"Error escribiendo canal {canal}: {result}")

    def toggle_canal(self, canal):
        estado_actual = self.leer_canales(count=canal + 1)[canal]
        self.set_canal(canal, not estado_actual)

    def set_todos(self, estado, count=NUM_CANALES, stagger_seconds=0.4):
        """Enciende o apaga todos los canales usados. Al ENCENDER, uno por uno con una
        pequena pausa entre cada uno (evita el pico de corriente de arranque de tener
        26 extractores arrancando todos al mismo instante). Al apagar no hay ese riesgo,
        se hace de una sola vez."""
        if not estado:
            result = self.client.write_coils(address=0, values=[False] * count, device_id=self.slave_id)
            if result.isError():
                raise IOError(f"Error apagando todos los canales: {result}")
            return

        for canal in range(count):
            result = self.client.write_coil(address=canal, value=True, device_id=self.slave_id)
            if result.isError():
                raise IOError(f"Error encendiendo canal {canal}: {result}")
            if canal < count - 1:
                time.sleep(stagger_seconds)

    def direccion_dispositivo(self):
        result = self.client.read_holding_registers(address=0x4000, count=1, device_id=0)
        if result.isError():
            raise IOError(f"Error leyendo direccion: {result}")
        return result.registers[0]

    def version_firmware(self):
        result = self.client.read_holding_registers(address=0x8000, count=1, device_id=0)
        if result.isError():
            raise IOError(f"Error leyendo version: {result}")
        return result.registers[0]
