import asyncio
import concurrent.futures
import json
import logging
import os
import subprocess
import sys
import time

from single_instance import ensure_single_instance
ensure_single_instance()

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "agent.log")

# pyasic reporta por WARNING cada IP del rango que no tiene minero (normal, no es un
# error). logging.disable() lo bloquea a nivel global sin importar que logger interno
# use la libreria, para que la consola solo muestre lo que imprime este agente.
logging.disable(logging.WARNING)

import requests
from pyasic import MinerNetwork, settings
from pyasic.device.algorithm.hashrate.sha256 import SHA256Unit

# Confirmado con el BiTeam Agent anterior (config.json: concurrency 90, timeout 1400ms,
# rango real .100-.175): escanear solo las IPs reales (760 en vez de 2540) con un pool
# de concurrencia plano es mucho mas rapido que escanear por rack, porque no se pierde
# tiempo esperando el timeout de miles de direcciones vacias.
settings.update("network_scan_semaphore", 150)
settings.update("network_ping_timeout", 3)
settings.update("factory_get_timeout", 3)

from pymodbus.client import ModbusTcpClient

from relay import WaveshareRelay
from ip_ranges import expand_range, expand_ranges
from config import (
    CLIENT_ID, API_BASE, AGENT_API_KEY, AGENT_NAME, AGENT_UID, rack_name_for_ip,
    INGEST_INTERVAL_SECONDS, COMMAND_POLL_INTERVAL_SECONDS, MODBUS_STATUS_INTERVAL_SECONDS,
)

# Client ID + API Key son de la EMPRESA (una sola, compartida por todas sus minas,
# estilo Foreman). X-Agent-Uid es el identificador estable de ESTA instalacion (no
# cambia nunca); X-Agent-Name es solo la etiqueta actual, renombrable desde el programa
# central sin que eso duplique la mina - ver get_current_agent() en la API.
# Se sube a mano en cada release (ver tag de git) - el ERP la compara contra
# AGENT_LATEST_VERSION para avisar si un agente quedo desactualizado.
AGENT_VERSION = "v1.2.1"

HEADERS = {
    "X-Client-Id": CLIENT_ID, "X-Api-Key": AGENT_API_KEY,
    "X-Agent-Name": AGENT_NAME, "X-Agent-Uid": AGENT_UID,
    "X-Agent-Version": AGENT_VERSION,
}


def log(msg):
    # Sin consola (--windowed) no hay stdout; se escribe a un archivo en vez de print().
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def normalize_hashrate(hr):
    """TH/s cuando es posible (SHA256: Whatsminer/Antminer). El Z15 (Equihash) no es
    convertible a TH/s; se deja el valor crudo en su unidad nativa (Sol/s)."""
    if hr is None:
        return None
    try:
        val = round(hr.into(SHA256Unit.TH).rate, 2)
    except Exception:
        val = round(hr.rate, 2)
    # Ningun ASIC actual pasa de ~1000 TH/s; si aparece un numero asi de grande es que
    # vino en H/s crudo y la conversion de unidad no aplico. Se corrige.
    if val is not None and val > 2000:
        val = round(val / 1e12, 2)
    return val


def extract_elapsed(summary):
    """pyasic ya usa el campo crudo 'Elapsed' (dialecto clasico cgminer) o 'elapsed'
    (dialecto nuevo Whatsminer V3) como su Uptime normalizado. El valor realmente
    distinto que se quiere mostrar en la columna 'Elapsed' es:
    - dialecto clasico (BTMinerRPCAPI): SUMMARY[0]['Uptime'] (tiempo de sistema, incluye
      lo que tardo en arrancar a minar despues de encender).
    - dialecto nuevo (BTMinerV3RPCAPI): msg.summary['bootup-time'] (mismo concepto, la
      clave cambia de nombre en el JSON nuevo de Whatsminer)."""
    if not summary:
        return None
    val = None
    if "SUMMARY" in summary:
        try:
            summary0 = summary["SUMMARY"][0]
            val = summary0.get("Uptime", summary0.get("Elapsed"))
        except (KeyError, IndexError, TypeError):
            val = None
    elif "msg" in summary:
        try:
            s = summary["msg"]["summary"]
            val = s.get("bootup-time", s.get("elapsed"))
        except (KeyError, TypeError):
            val = None
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def extract_psu_serial(psu, device_info):
    """El serial de la fuente de poder no viene en get_data()/summary() de pyasic para
    Whatsminer (solo lo expone para braiins_os) - hay que pedirlo aparte y la ubicacion
    cambia segun el dialecto: get_psu()['Msg']['serial_no'] en el clasico (ausente en
    modelos viejos como el M20), o get_device_info()['msg']['power']['sn'] en el V3."""
    if psu:
        try:
            val = psu["Msg"].get("serial_no")
            if val:
                return val
        except (KeyError, TypeError):
            pass
    if device_info:
        try:
            val = device_info["msg"]["power"].get("sn")
            if val:
                return val
        except (KeyError, TypeError):
            pass
    return None


def _to_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mhs_to_ths(v):
    # El dialecto clasico reporta "MHS av"/"HS RT" etc en una escala tal que /1e6 da TH/s
    # (confirmado contra datos reales: 58635159 "MHS av" = 58.6 TH/s, consistente con un
    # M30S+ de ese tamano).
    val = _to_float(v)
    return round(val / 1e6, 2) if val is not None else None


def _ghs_to_ths(v):
    val = _to_float(v)
    return round(val / 1e3, 2) if val is not None else None


def extract_raw_details(summary, psu, device_info, edevs, pools_raw, setting):
    """Campos crudos adicionales (fuera de lo que get_data() de pyasic normaliza) para
    el detalle completo tipo WhatsMinerTool. Cada dialecto guarda esto en una forma
    distinta - se detecta por la forma del dict de summary ('SUMMARY' clasico cgminer
    vs 'msg' el JSON nuevo V3 de Whatsminer) y se arma un dict plano unificado."""
    d = {}
    is_v3 = isinstance(summary, dict) and "msg" in summary

    if is_v3:
        s = ((summary or {}).get("msg") or {}).get("summary", {}) or {}
        info_msg = (device_info or {}).get("msg", {}) or {}
        m = info_msg.get("miner", {}) or {}
        net = info_msg.get("network", {}) or {}
        sysinfo = info_msg.get("system", {}) or {}
        pw = info_msg.get("power", {}) or {}
        stg = (setting or {}).get("msg", {}) or {}
        pool_list = ((pools_raw or {}).get("msg", {}) or {}).get("pools", [])
        board_list = ((edevs or {}).get("msg", {}) or {}).get("edevs", [])

        d.update({
            "hostname": net.get("hostname"), "net_proto": net.get("proto"),
            "net_netmask": net.get("netmask"), "net_dns": net.get("dns"), "net_gateway": net.get("gateway"),
            "hash_board_variant": m.get("hash-board"),
            "chipdata0": m.get("chipdata0"), "chipdata1": m.get("chipdata1"), "chipdata2": m.get("chipdata2"),
            "board_num": _to_int(m.get("board-num")),
            "power_vendor": pw.get("vendor"), "power_type": pw.get("model") or pw.get("type"),
            "power_hwversion": pw.get("hwversion"), "power_swversion": pw.get("swversion"),
            "power_vin": _to_float(pw.get("vin")), "power_iin": _to_float(pw.get("iin")),
            "power_vout": _to_float(pw.get("vout")), "power_pin": _to_float(pw.get("pin")),
            "power_fanspeed": _to_int(pw.get("fanspeed")), "power_temp0": _to_float(pw.get("temp0")),
            "system_api": sysinfo.get("api"), "system_fwversion": sysinfo.get("fwversion"),
            "system_control_board_version": sysinfo.get("control-board-version"),
            "system_platform": sysinfo.get("platform"),
            "hash_realtime_ths": _to_float(s.get("hash-realtime")), "hash_average_ths": _to_float(s.get("hash-average")),
            "hash_1min_ths": _to_float(s.get("hash-1min")), "hash_15min_ths": _to_float(s.get("hash-15min")),
            "factory_hash_ths": _to_float(s.get("factory-hash")),
            "power_realtime_w": _to_float(s.get("power-realtime")), "power_5min_w": _to_float(s.get("power-5min")),
            "power_rate_pct": _to_float(s.get("power-rate")),
            "board_temperature": s.get("board-temperature"),
            "chip_temp_min": _to_float(s.get("chip-temp-min")), "chip_temp_avg": _to_float(s.get("chip-temp-avg")),
            "chip_temp_max": _to_float(s.get("chip-temp-max")),
            "freq_avg_mhz": _to_float(s.get("freq-avg")), "power_limit_raw_w": _to_float(s.get("power-limit")),
            "up_freq_finished": bool(s.get("up-freq-finish")) if s.get("up-freq-finish") is not None else None,
            "power_mode": stg.get("power-mode"), "fast_boot": stg.get("fast-boot"),
            "fast_hash": stg.get("fast-hash"), "heat_mode": stg.get("heat-mode"),
            "target_freq": _to_float(stg.get("target-freq")), "power_percent": _to_float(stg.get("power-percent")),
        })
        pools_detail = [
            {"url": p.get("url"), "user": p.get("account"), "status": p.get("status"),
             "active": bool(p.get("stratum-active")), "reject_rate": p.get("reject-rate")}
            for p in pool_list
        ] or None
        boards_extra = {
            b.get("slot"): {
                "hash_average_ths": b.get("hash-average"), "factory_hash_ths": b.get("factory-hash"),
                "freq_mhz": b.get("freq"), "effective_chips": b.get("effective-chips"),
                "chip_temp_min": b.get("chip-temp-min"), "chip_temp_avg": b.get("chip-temp-avg"),
                "chip_temp_max": b.get("chip-temp-max"),
            }
            for b in board_list
        }
    else:
        s = ((summary or {}).get("SUMMARY") or [{}])[0] if summary else {}
        pw = (psu or {}).get("Msg", {}) or {}
        pool_list = (pools_raw or {}).get("POOLS", [])
        board_list = (edevs or {}).get("DEVS", [])

        d.update({
            "hostname": None, "net_proto": None, "net_netmask": None, "net_dns": None, "net_gateway": None,
            "hash_board_variant": None, "chipdata0": None, "chipdata1": None, "chipdata2": None,
            "board_num": len(board_list) or None,
            "power_vendor": pw.get("vendor"), "power_type": pw.get("model") or pw.get("name"),
            "power_hwversion": pw.get("hw_version"), "power_swversion": pw.get("sw_version"),
            # El dialecto clasico manda vin/iin como enteros sin escalar (ej. "25150" =
            # 251.50V, "12109" = 12.109A) - confirmado comparando contra el mismo campo
            # ya escalado del dialecto V3 en modelos equivalentes (~250V / ~14A).
            "power_vin": (lambda v: v / 100 if v is not None else None)(_to_float(pw.get("vin"))),
            "power_iin": (lambda v: v / 1000 if v is not None else None)(_to_float(pw.get("iin"))),
            "power_vout": None, "power_pin": _to_float(pw.get("pin")),
            "power_fanspeed": _to_int(pw.get("fan_speed")), "power_temp0": _to_float(pw.get("temp0")),
            "system_api": None, "system_fwversion": s.get("Firmware Version"),
            "system_control_board_version": s.get("CB Version"), "system_platform": s.get("CB Platform"),
            "hash_realtime_ths": _mhs_to_ths(s.get("HS RT")), "hash_average_ths": _mhs_to_ths(s.get("MHS av")),
            "hash_1min_ths": _mhs_to_ths(s.get("MHS 1m")), "hash_15min_ths": _mhs_to_ths(s.get("MHS 15m")),
            "factory_hash_ths": _ghs_to_ths(s.get("Factory GHS")),
            "power_realtime_w": _to_float(s.get("Power_RT", s.get("Power"))), "power_5min_w": None,
            "power_rate_pct": _to_float(s.get("Power Rate")),
            "board_temperature": [b.get("Temperature") for b in board_list] or None,
            "chip_temp_min": _to_float(s.get("Chip Temp Min")), "chip_temp_avg": _to_float(s.get("Chip Temp Avg")),
            "chip_temp_max": _to_float(s.get("Chip Temp Max")),
            "freq_avg_mhz": _to_float(s.get("freq_avg")), "power_limit_raw_w": _to_float(s.get("Power Limit")),
            "up_freq_finished": bool(s.get("Upfreq Complete")) if s.get("Upfreq Complete") is not None else None,
            "power_mode": s.get("Power Mode"), "fast_boot": s.get("Btminer Fast Boot"),
            "fast_hash": None, "heat_mode": None,
            "target_freq": _to_float(s.get("Target Freq")), "power_percent": None,
        })
        pools_detail = [
            {"url": p.get("URL"), "user": p.get("User"), "status": p.get("Status"),
             "active": bool(p.get("Stratum Active")), "reject_rate": p.get("Pool Rejected%")}
            for p in pool_list
        ] or None
        boards_extra = {
            b.get("Slot"): {
                "hash_average_ths": _mhs_to_ths(b.get("MHS av")), "factory_hash_ths": _ghs_to_ths(b.get("Factory GHS")),
                "freq_mhz": b.get("Chip Frequency"), "effective_chips": b.get("Effective Chips"),
                "chip_temp_min": b.get("Chip Temp Min"), "chip_temp_avg": b.get("Chip Temp Avg"),
                "chip_temp_max": b.get("Chip Temp Max"),
            }
            for b in board_list
        }

    d["pools_detail"] = pools_detail
    d["boards_extra"] = boards_extra
    return d


def build_miner_dict(rack_name, data, elapsed_seconds=None, power_serial_number=None, details=None):
    details = details or {}
    # data.pools (metrica en vivo) casi siempre viene vacia en este firmware; el pool y
    # el worker reales estan en la configuracion (data.config.pools). pools_detail (del
    # RPC crudo, ver extract_raw_details) casi siempre SI trae los 3 pools con su estado
    # real (alive/activo), asi que se prefiere ese cuando esta disponible.
    pool_url, worker = None, None
    if data.config and data.config.pools and data.config.pools.groups:
        cfg_pools = data.config.pools.groups[0].pools
        if cfg_pools:
            pool_url = str(cfg_pools[0].url) if cfg_pools[0].url else None
            worker = cfg_pools[0].user

    all_pools = details.get("pools_detail")
    if not all_pools and data.pools:
        all_pools = [
            {"url": str(p.url) if p.url else None, "user": p.user, "active": p.active,
             "accepted": p.accepted, "rejected": p.rejected}
            for p in data.pools
        ]

    errors = [str(e) for e in (data.errors or [])]
    error_flags = None
    if errors or data.fault_light:
        error_flags = {"fault_light": bool(data.fault_light), "errors": errors}

    hashrate_val = normalize_hashrate(data.hashrate)
    expected_hashrate_val = normalize_hashrate(data.expected_hashrate)

    model_name = None
    if data.device_info and data.device_info.model:
        model_name = str(data.device_info.model)

    fan_speeds = [f.speed for f in data.fans if f.speed is not None] or None

    # boards_extra (del RPC crudo: freq/chips efectivos/hash por board) se fusiona por
    # slot con lo que ya normaliza pyasic (temp/chips/voltaje/serial), para no perder
    # ninguno de los dos y tener el detalle completo de cada hashboard en un solo lugar.
    boards_extra = details.get("boards_extra") or {}
    hashboards = [
        {
            "slot": hb.slot,
            "chip_temp": hb.chip_temp,
            "chips": hb.chips,
            "voltage": hb.voltage,
            "hashrate_ths": normalize_hashrate(hb.hashrate),
            "serial_number": hb.serial_number,
            **(boards_extra.get(hb.slot) or {}),
        }
        for hb in (data.hashboards or [])
    ] or None

    # PCB SN0/1/2: el serial individual de cada hashboard, expuesto como columnas planas
    # ademas de venir dentro de la lista "hashboards" (que trae todo el detalle por slot).
    pcb_serials = {hb.slot: hb.serial_number for hb in (data.hashboards or [])}

    out = {
        "ip": data.ip,
        "rack": rack_name,
        "model": model_name,
        "worker": worker,
        "uptime_seconds": data.uptime,
        "elapsed_seconds": elapsed_seconds,
        "hashrate_ths": hashrate_val,
        "avg_temp_c": data.temperature_avg,
        "fan_speeds": fan_speeds,
        "pool_url": pool_url,
        "error_flags": error_flags,
        "online": bool(data.is_mining),
        "mac": data.mac,
        "serial_number": data.serial_number,
        "power_serial_number": power_serial_number or data.psu_serial_number,
        "pcb_sn0": pcb_serials.get(0),
        "pcb_sn1": pcb_serials.get(1),
        "pcb_sn2": pcb_serials.get(2),
        "firmware": str(data.device_info.firmware) if data.device_info and data.device_info.firmware else None,
        "api_ver": data.api_ver,
        "wattage": data.wattage,
        "wattage_limit": data.raw_wattage_limit,
        "efficiency": data.efficiency,
        "env_temp_c": data.env_temp,
        "expected_hashrate_ths": expected_hashrate_val,
        "percent_expected_hashrate": data.percent_expected_hashrate,
        "percent_expected_wattage": data.percent_expected_wattage,
        "hashboards": hashboards,
        "pools": all_pools or None,
    }
    # Los 44 campos crudos adicionales (network, identidad de fuente de poder, resumen
    # detallado, config) van tal cual vinieron de extract_raw_details - ya excluye
    # pools_detail/boards_extra (ya consumidos arriba) para no mandarlos dos veces.
    for key, val in details.items():
        if key not in ("pools_detail", "boards_extra"):
            out[key] = val

    # board_num/board_temperature normalmente salen de la llamada cruda "edevs" (se quito
    # por inestabilidad a escala completa - ver comentario en scan_all_miners), pero para
    # el dialecto clasico ya tenemos lo mismo gratis en data.hashboards (de get_data(),
    # sin llamada extra), asi que se rellenan de ahi si vinieron vacios.
    if out.get("board_num") is None and data.hashboards:
        out["board_num"] = len(data.hashboards)
    if out.get("board_temperature") is None and data.hashboards:
        temps = [hb.chip_temp for hb in data.hashboards if hb.chip_temp is not None]
        if temps:
            out["board_temperature"] = temps

    return out


def fetch_scan_ranges():
    try:
        resp = requests.get(f"{API_BASE}/agent/config", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json().get("ranges", [])
    except Exception as e:
        log(f"Error consultando rango de escaneo: {e}")
        return []


async def scan_all_miners():
    ranges = fetch_scan_ranges()
    if not ranges:
        log("Sin rango de IP configurado. Usa 'Buscar Mineros' en el modulo Agentes del programa central.")
        return []

    try:
        ips = expand_ranges(ranges)
    except ValueError as e:
        log(f"Rango de escaneo invalido: {e}")
        return []

    try:
        network = MinerNetwork.from_list(ips)
        miners = await network.scan()
    except Exception as e:
        log(f"Error escaneando: {e}")
        return []

    # Antes se pedia get_data() a cada minero uno por uno (secuencial), lo que hacia
    # que un rack de ~70 equipos tardara 70-90s solo por la latencia acumulada de red.
    # Se piden todos los datos en paralelo. Ademas se piden llamadas RPC crudas que
    # get_data() no expone (Elapsed real y Serial de la fuente de poder) - la llamada
    # correcta cambia segun el dialecto que hable el firmware del minero:
    #   - Whatsminer clasico / Antminer (BTMinerRPCAPI / cgminer): .summary() + .get_psu()
    #   - Whatsminer V3 (BTMinerV3RPCAPI, JSON nuevo): .get_miner_status_summary() +
    #     .get_device_info() (el serial de la fuente esta en msg.power.sn, no en get_psu)
    # Cada llamada tiene su propio try/except para que un fallo ahi no tire los datos
    # completos del minero (get_data() ya trajo lo importante).
    # Ademas de summary/psu/device_info se piden edevs (detalle por hashboard: freq,
    # solo para el dialecto V3 tambien get_miner_setting (power_mode/fast_boot/etc, que
    # en el dialecto clasico ya vienen dentro de summary()). Todo en paralelo por minero.
    # (edevs/pools crudos -detalle por hashboard y estado real de cada pool- se probaron
    # tambien pero causaban cuelgues del ciclo completo a escala de 662 mineros dentro
    # del .exe compilado -no en pruebas directas con python-, asi que se quitaron; la
    # gran mayoria de los 44 campos nuevos ya vienen en summary/device_info sin ellos.)
    # Estas llamadas extra NO pasan por el semaforo de escaneo de pyasic (ese solo
    # limita la fase de descubrimiento) - se acota aparte a 100 mineros en vuelo a la vez,
    # y cada llamada individual tiene su propio timeout para que una sola conexion
    # trabada no cuelgue el resto.
    raw_calls_semaphore = asyncio.Semaphore(100)

    async def safe_call(rpc, name):
        fn = getattr(rpc, name, None)
        if fn is None:
            return None
        try:
            return await asyncio.wait_for(fn(), timeout=5)
        except Exception:
            return None

    async def fetch_one(miner):
        data = await miner.get_data()
        rpc = miner.rpc
        async with raw_calls_semaphore:
            if hasattr(rpc, "summary"):
                summary, psu = await asyncio.gather(
                    safe_call(rpc, "summary"), safe_call(rpc, "get_psu"),
                )
                device_info, setting = None, None
            else:
                summary, device_info, setting = await asyncio.gather(
                    safe_call(rpc, "get_miner_status_summary"), safe_call(rpc, "get_device_info"),
                    safe_call(rpc, "get_miner_setting"),
                )
                psu = None
        return data, summary, psu, device_info, None, None, setting

    results = await asyncio.gather(
        *(fetch_one(miner) for miner in miners), return_exceptions=True
    )

    miners_out = []
    for miner, result in zip(miners, results):
        if isinstance(result, Exception):
            log(f"Error leyendo datos de {getattr(miner, 'ip', '?')}: {result}")
            continue
        data, summary, psu, device_info, edevs_raw, pools_raw, setting = result
        details = extract_raw_details(summary, psu, device_info, edevs_raw, pools_raw, setting)
        miners_out.append(build_miner_dict(
            rack_name_for_ip(data.ip), data,
            extract_elapsed(summary), extract_psu_serial(psu, device_info),
            details,
        ))

    return miners_out


def _probe_modbus_port(ip, port=502, timeout=0.6):
    """Solo confirma que algo responde Modbus TCP en ese puerto (conexion rapida, sin
    leer nada todavia) - se corre en paralelo sobre todo el rango."""
    try:
        client = ModbusTcpClient(ip, port=port, timeout=timeout)
        if client.connect():
            client.close()
            return ip
    except Exception:
        pass
    return None


# Tamanos de placa Waveshare Modbus POE ETH Relay que existen en el mercado (de 8 a 36
# canales) - se prueba de mayor a menor, ya que el modulo responde con error si pides
# leer mas canales de los que realmente tiene.
_CANALES_POSIBLES = [36, 32, 30, 24, 16, 8]


def _detect_num_channels(rel):
    for count in _CANALES_POSIBLES:
        try:
            result = rel.client.read_coils(address=0, count=count, device_id=rel.slave_id)
            if not result.isError():
                return count
        except Exception:
            continue
    return None


def _identify_device(ip, port=502):
    """Ya sabiendo que algo responde en ese puerto, intenta leer los registros propios
    del modulo Waveshare (direccion + version de firmware) para confirmar que SI es un
    rele de ese tipo (y no otro dispositivo Modbus cualquiera), y cuantos canales tiene
    de verdad (probando cuantos responde sin error, ya que va de 8 a 36 segun el modelo)."""
    entry = {"ip": ip, "port": port, "slave_id": 1, "device_type": None, "num_channels": None, "serial_number": None, "label": f"Dispositivo Modbus TCP sin identificar ({ip})"}
    rel = WaveshareRelay(ip=ip, port=port, slave_id=1)
    try:
        rel.connect()
        direccion = rel.direccion_dispositivo()
        firmware = rel.version_firmware()
        num_channels = _detect_num_channels(rel)
        entry["device_type"] = "relay"
        entry["num_channels"] = num_channels
        entry["serial_number"] = str(direccion)
        canales_txt = f"{num_channels} canales detectados" if num_channels else "canales sin detectar"
        entry["label"] = f"Relé Waveshare ({canales_txt}) — dirección {direccion}, firmware v{firmware}"
    except Exception:
        pass
    finally:
        rel.close()
    return entry


def discover_modbus_devices(range_str):
    ips = expand_range(range_str)
    with concurrent.futures.ThreadPoolExecutor(max_workers=60) as ex:
        found_ips = [ip for ip in ex.map(_probe_modbus_port, ips) if ip]
        devices = list(ex.map(_identify_device, found_ips)) if found_ips else []
    return devices


def fetch_relay_config():
    """Ya no viene fijo en el codigo: el agente pregunta a la API que dispositivos Modbus
    tiene registrados esta mina y usa el primero de tipo 'relay' (extractores), con el
    numero de canales que se confirmo al registrarlo (varia de 8 a 36 segun el modelo)."""
    try:
        resp = requests.get(f"{API_BASE}/agent/modbus-devices", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        devices = resp.json()
    except Exception as e:
        log(f"Error consultando dispositivos Modbus: {e}")
        return None

    for d in devices:
        if d["device_type"] == "relay":
            num_channels = (d.get("config") or {}).get("num_channels") or 0
            return d["ip"], d["port"], d["slave_id"], num_channels
    return None


RELAY_STATE = {"obj": None, "ip": None, "port": None, "slave_id": None, "num_channels": 0}


def refresh_relay():
    """Se llama una vez por ciclo de ingest. Reconecta si cambio la config o se cayo,
    reutiliza la conexion si nada cambio. Devuelve None si no hay rele registrado."""
    cfg = fetch_relay_config()
    if cfg is None:
        if RELAY_STATE["obj"] is not None:
            log("El rele de extractores ya no esta registrado para esta mina.")
        RELAY_STATE.update({"obj": None, "ip": None, "port": None, "slave_id": None, "num_channels": 0})
        return None

    ip, port, slave_id, num_channels = cfg
    if (ip, port, slave_id) != (RELAY_STATE["ip"], RELAY_STATE["port"], RELAY_STATE["slave_id"]):
        new_relay = WaveshareRelay(ip=ip, port=port, slave_id=slave_id)
        try:
            new_relay.connect()
        except Exception as e:
            log(f"No se pudo conectar al rele en {ip}:{port}: {e}")
            return None
        RELAY_STATE.update({"obj": new_relay, "ip": ip, "port": port, "slave_id": slave_id, "num_channels": num_channels})
        log(f"Conectado al modulo Waveshare ({ip}:{port}, {num_channels} canales)")
    else:
        ensure_connected(RELAY_STATE["obj"])
        RELAY_STATE["num_channels"] = num_channels

    return RELAY_STATE["obj"]


def read_extractor_channels(relay):
    if relay is None:
        return None
    ensure_connected(relay)
    try:
        return relay.leer_canales(count=RELAY_STATE["num_channels"])
    except Exception as e:
        # Mismo caso que en command_loop: is_connected() puede no haber detectado que
        # el rele ya cerro la conexion. Se reintenta una vez forzando reconexion antes
        # de darse por vencido, para no dejar de reportar el estado real ciclo tras
        # ciclo hasta que algo mas fuerce una reconexion.
        log(f"Error leyendo extractores ({e}), forzando reconexion...")
        try:
            relay.close()
            relay.connect()
            return relay.leer_canales(count=RELAY_STATE["num_channels"])
        except Exception as e2:
            log(f"Error leyendo extractores tras reconectar: {e2}")
            return None


def post_ingest(miners, extractor_channels=None):
    payload = {"miners": miners, "extractor_channels": extractor_channels}
    try:
        resp = requests.post(f"{API_BASE}/agent/ingest", json=payload, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log(f"Error subiendo datos a la API: {e}")


async def ingest_loop():
    while True:
        start = time.monotonic()
        miners = await scan_all_miners()
        relay = refresh_relay()

        by_rack = {}
        for m in miners:
            by_rack.setdefault(m["rack"], []).append(m)
        for rack_name in sorted(by_rack):
            log(f"  {rack_name}: {len(by_rack[rack_name])} mineros")

        post_ingest(miners, extractor_channels=read_extractor_channels(relay))
        log(f"Ciclo completo: {len(miners)} mineros en {time.monotonic() - start:.1f}s")

        elapsed = time.monotonic() - start
        await asyncio.sleep(max(0, INGEST_INTERVAL_SECONDS - elapsed))


def _probe_registered_device(d):
    """Igual que la deteccion en discover_modbus_devices, pero mas ligero (solo confirma
    conexion, no vuelve a detectar canales) - se corre en cada ciclo del escaneo de
    dispositivos Modbus, separado del escaneo de mineros para no mezclarlos."""
    connected = False
    if d["device_type"] == "relay":
        rel = WaveshareRelay(ip=d["ip"], port=d["port"], slave_id=d["slave_id"])
        try:
            rel.connect()
            result = rel.client.read_coils(address=0, count=1, device_id=rel.slave_id)
            connected = not result.isError()
        except Exception:
            connected = False
        finally:
            rel.close()
    else:
        # Los demas tipos (PDU, sensores...) todavia no tienen lectura funcional propia
        # ("proximamente" en el programa) - por ahora solo se confirma que el puerto
        # Modbus responde.
        connected = _probe_modbus_port(d["ip"], d["port"]) is not None
    return {"id": d["id"], "connected": connected}


def _probe_registered_devices(devices):
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as ex:
        return list(ex.map(_probe_registered_device, devices))


async def modbus_status_loop():
    """Escanea la conexion real de TODOS los dispositivos Modbus registrados en esta
    mina (rele de extractores, PDU, sensores, etc.) - igual que se escanean los
    mineros, pero para el equipo de infraestructura. Ahorita solo hay uno (el rele),
    pero esto ya soporta cualquier cantidad."""
    while True:
        try:
            resp = requests.get(f"{API_BASE}/agent/modbus-devices", headers=HEADERS, timeout=10)
            resp.raise_for_status()
            devices = resp.json()
        except Exception as e:
            log(f"Error consultando dispositivos Modbus para estatus: {e}")
            devices = []

        if devices:
            try:
                results = await asyncio.to_thread(_probe_registered_devices, devices)
                requests.post(
                    f"{API_BASE}/agent/modbus-devices/status",
                    json={"devices": results},
                    headers=HEADERS, timeout=10,
                )
            except Exception as e:
                log(f"Error escaneando/reportando dispositivos Modbus: {e}")

        await asyncio.sleep(MODBUS_STATUS_INTERVAL_SECONDS)


def _download_update(repo, tag, dest_path):
    """Descarga el .exe publicado como asset del release {tag} en {repo} (repo publico
    de GitHub, sin necesitar token). Verifica que el tamano descargado coincida con el
    anunciado por el servidor y que sea de un tamano razonable (un exe real pesa varios
    MB) antes de darlo por bueno - mejor fallar aqui que dejar un .exe corrupto listo
    para reemplazar al que si funciona."""
    url = f"https://github.com/{repo}/releases/download/{tag}/AgenteStart.exe"
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()
    expected_size = int(resp.headers.get("Content-Length") or 0)
    written = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            f.write(chunk)
            written += len(chunk)
    if expected_size and written != expected_size:
        raise IOError(f"Descarga incompleta: {written}/{expected_size} bytes")
    if written < 1_000_000:
        raise IOError(f"Archivo descargado demasiado chico ({written} bytes) - no parece un .exe real")
    return written


def _apply_update_and_relaunch(new_exe_path):
    """Un .exe no se puede reemplazar a si mismo mientras esta corriendo en Windows -
    se lanza un .bat aparte (proceso independiente) que espera a que este proceso
    termine, hace el reemplazo, relanza el agente, y se autoborra."""
    current_exe = os.path.abspath(sys.argv[0])
    exe_dir = os.path.dirname(current_exe)
    bat_path = os.path.join(exe_dir, "_apply_update.bat")
    bat_contents = (
        "@echo off\r\n"
        "timeout /t 2 /nobreak > nul\r\n"
        f'move /y "{new_exe_path}" "{current_exe}"\r\n'
        # Un .exe recien escrito/reemplazado a veces tarda unos segundos en "asentarse"
        # con el antivirus/Defender antes de poder arrancar limpio (se vio en pruebas
        # reales: arrancaba con una ventana de Error, pero el mismo .exe abria bien
        # segundos despues sin tocarle nada) - este margen extra evita ese problema.
        "timeout /t 5 /nobreak > nul\r\n"
        f'start "" "{current_exe}"\r\n'
        'del "%~f0"\r\n'
    )
    with open(bat_path, "w") as f:
        f.write(bat_contents)
    subprocess.Popen(["cmd", "/c", bat_path], creationflags=subprocess.CREATE_NEW_CONSOLE)


async def command_loop():
    while True:
        relay = RELAY_STATE["obj"]
        try:
            resp = requests.get(f"{API_BASE}/agent/commands/pending", headers=HEADERS, timeout=10)
            resp.raise_for_status()
            commands = resp.json()
        except Exception as e:
            log(f"Error consultando comandos pendientes: {e}")
            commands = []

        for cmd in commands:
            status, result = "done", None
            if cmd["action"] == "discover_modbus":
                # No requiere que ya haya un rele registrado (es justo para encontrar
                # uno nuevo) - se corre en un thread aparte para no congelar el resto
                # del agente (escaneo de mineros, etc.) mientras dura la busqueda.
                try:
                    devices = await asyncio.to_thread(discover_modbus_devices, cmd["params"]["range"])
                    result = json.dumps(devices)
                    log(f"Comando {cmd['id']} (discover_modbus): {len(devices)} dispositivo(s) encontrado(s)")
                except Exception as e:
                    status, result = "failed", str(e)
                    log(f"Error ejecutando comando {cmd['id']}: {e}")
                try:
                    requests.post(
                        f"{API_BASE}/agent/commands/{cmd['id']}/ack",
                        json={"status": status, "result": result},
                        headers=HEADERS, timeout=10,
                    )
                except Exception as e:
                    log(f"Error confirmando comando {cmd['id']}: {e}")
                continue

            if cmd["action"] == "update_agent":
                # No depende del rele ni de nada mas - se puede pedir en cualquier
                # momento. Si algo falla en la descarga/verificacion, el agente actual
                # sigue corriendo sin tocarse (nunca se llega a reemplazar el .exe).
                # Un solo punto de salida "normal" (manda el ack y continue) para
                # cualquier caso que no sea el exito, que sale por su cuenta con
                # os._exit despues de reiniciar.
                ready_to_relaunch = None  # se llena con tmp_path solo si la descarga salio bien

                if sys.platform != "win32" or not getattr(sys, "frozen", False):
                    status, result = "failed", "Auto-actualizacion solo soportada en el .exe compilado de Windows"
                    log(f"Comando {cmd['id']} (update_agent): {result}")
                else:
                    repo = cmd["params"]["repo"]
                    tag = cmd["params"]["tag"]
                    tmp_path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), f"_update_{tag}.exe")
                    try:
                        size = await asyncio.to_thread(_download_update, repo, tag, tmp_path)
                        log(f"Comando {cmd['id']} (update_agent {tag}): descarga OK ({size} bytes)")
                        result = f"Actualizando a {tag} y reiniciando..."
                        ready_to_relaunch = tmp_path
                    except Exception as e:
                        status, result = "failed", str(e)
                        log(f"Error descargando actualizacion {tag} (comando {cmd['id']}): {e}")
                        try:
                            if os.path.exists(tmp_path):
                                os.remove(tmp_path)
                        except OSError:
                            pass

                try:
                    requests.post(
                        f"{API_BASE}/agent/commands/{cmd['id']}/ack",
                        json={"status": status, "result": result},
                        headers=HEADERS, timeout=10,
                    )
                except Exception as e:
                    log(f"Error confirmando comando {cmd['id']}: {e}")

                if ready_to_relaunch:
                    # Se manda el ack ANTES de reiniciar - una vez que el proceso se
                    # cierra ya no puede confirmar nada.
                    log(f"Aplicando actualizacion {tag} y reiniciando...")
                    _apply_update_and_relaunch(ready_to_relaunch)
                    os._exit(0)
                continue

            if cmd["action"] == "reboot_miner":
                # No depende del rele de extractores - se conecta directo a ese minero
                # puntual (mismo mecanismo pyasic que usa el escaneo normal) y le pide
                # reiniciar. Lo emite el monitoreo de IA del backend (ver
                # api/app/services/ai_monitor.py) tras varios ciclos de anomalia.
                ip = cmd["params"]["ip"]
                try:
                    network = MinerNetwork.from_list([ip])
                    miners = await network.scan()
                    if not miners:
                        status, result = "failed", f"No se pudo conectar al minero en {ip}"
                    else:
                        await miners[0].reboot()
                        result = f"Reinicio enviado a {ip}"
                    log(f"Comando {cmd['id']} (reboot_miner {ip}): {status}")
                except Exception as e:
                    status, result = "failed", str(e)
                    log(f"Error ejecutando comando {cmd['id']} (reboot_miner {ip}): {e}")
                try:
                    requests.post(
                        f"{API_BASE}/agent/commands/{cmd['id']}/ack",
                        json={"status": status, "result": result},
                        headers=HEADERS, timeout=10,
                    )
                except Exception as e:
                    log(f"Error confirmando comando {cmd['id']}: {e}")
                continue

            if relay is not None:
                ensure_connected(relay)
            try:
                if relay is None:
                    status, result = "failed", "No hay un rele de extractores registrado para esta mina"
                elif cmd["action"] in ("extractor_set_channel", "extractor_set_all"):
                    try:
                        _run_relay_command(relay, cmd["action"], cmd["params"])
                    except Exception as first_err:
                        # pymodbus no siempre detecta que el rele cerro la conexion de su
                        # lado (ej. WinError 10054) hasta que se intenta escribir -
                        # is_connected() puede seguir diciendo "conectado" con un socket
                        # ya muerto. Se fuerza una reconexion real y se reintenta una vez
                        # antes de rendirse, en vez de dejar el rele en un estado roto
                        # hasta el siguiente ciclo de ingest.
                        log(f"Comando {cmd['id']}: fallo inicial ({first_err}), forzando reconexion al rele...")
                        relay.close()
                        relay.connect()
                        _run_relay_command(relay, cmd["action"], cmd["params"])
                else:
                    status, result = "failed", f"accion desconocida: {cmd['action']}"
                log(f"Comando {cmd['id']} ({cmd['action']}) ejecutado: {status}")
            except Exception as e:
                status, result = "failed", str(e)
                log(f"Error ejecutando comando {cmd['id']}: {e}")

            try:
                requests.post(
                    f"{API_BASE}/agent/commands/{cmd['id']}/ack",
                    json={"status": status, "result": result},
                    headers=HEADERS, timeout=10,
                )
            except Exception as e:
                log(f"Error confirmando comando {cmd['id']}: {e}")

        await asyncio.sleep(COMMAND_POLL_INTERVAL_SECONDS)


def ensure_connected(relay):
    if not relay.is_connected():
        try:
            relay.connect()
            log("Reconectado al modulo Waveshare")
        except Exception as e:
            log(f"Fallo al reconectar al modulo Waveshare: {e}")


def _run_relay_command(relay, action, params):
    if action == "extractor_set_channel":
        relay.set_canal(params["channel"], params["state"])
    elif action == "extractor_set_all":
        relay.set_todos(params["state"], count=params.get("count") or RELAY_STATE["num_channels"])
    else:
        raise ValueError(f"accion desconocida: {action}")


def _report_location():
    """Geolocalizacion aproximada por IP publica (no GPS - una mini PC no trae eso). Se
    manda una sola vez al arrancar, con fallo silencioso si no hay internet todavia o el
    servicio de geolocalizacion no responde - no debe impedir que el agente arranque."""
    try:
        geo = requests.get("http://ip-api.com/json/", timeout=10).json()
        if geo.get("status") != "success":
            log(f"No se pudo obtener la ubicacion por IP: {geo.get('message')}")
            return
        payload = {
            "latitude": geo["lat"], "longitude": geo["lon"],
            "city": f"{geo.get('city', '')}, {geo.get('regionName', '')}, {geo.get('country', '')}".strip(", "),
        }
        requests.post(f"{API_BASE}/agent/location", json=payload, headers=HEADERS, timeout=10)
        log(f"Ubicacion reportada (aproximada por IP): {payload['city']}")
    except Exception as e:
        log(f"Error reportando ubicacion: {e}")


async def main():
    log(f"Agente iniciado. Nombre={AGENT_NAME} Client ID={CLIENT_ID} API_BASE={API_BASE}")
    await asyncio.to_thread(_report_location)
    # El rele ya no se conecta al arrancar: se resuelve dinamicamente desde el registro
    # de dispositivos Modbus de la mina (puede no haber ninguno todavia, o cambiar).
    await asyncio.gather(ingest_loop(), command_loop(), modbus_status_loop())


if __name__ == "__main__":
    asyncio.run(main())
