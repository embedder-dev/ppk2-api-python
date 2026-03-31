# ppk2Prelude.py — PPK2 hardware helper bridge
# Copyright (c) 2026 Embedder Pty Ltd
# SPDX-License-Identifier: GPL-2.0-only
#
# This file is licensed under the GNU General Public License v2.0.
# It serves to improve the usability of the following package:
# ppk2-api Python package (https://github.com/IRNAS/ppk2-api-python).
# You may redistribute and/or modify it under the terms of the GPL v2.
# See https://www.gnu.org/licenses/old-licenses/gpl-2.0.html

import json
import time

from ppk2_api.ppk2_api import PPK2_MP

_ppk2_connections = {}

def ppk2_list_devices():
    raw = PPK2_MP.list_devices()
    if not raw:
        return []
    result = []
    for d in raw:
        port, sn, *_ = d
        result.append({"port": port, "serial_number": sn})
    return result

def ppk2_connect(port=None, use_buffered_reader=True):
    global _ppk2_connections
    if port is None:
        devices = ppk2_list_devices()
        if not devices:
            raise RuntimeError("No PPK2 device found")
        port = devices[0]["port"]
    if port in _ppk2_connections:
        return _ppk2_connections[port]
    if use_buffered_reader:
        ppk2 = PPK2_MP(port)
    else:
        from ppk2_api.ppk2_api import PPK2_API
        ppk2 = PPK2_API(port)
    ppk2.get_modifiers()
    _ppk2_connections[port] = ppk2
    return ppk2

def ppk2_use_source_meter(port=None):
    ppk2 = ppk2_connect(port)
    ppk2.use_source_meter()
    return {"success": True, "mode": "source_meter"}

def ppk2_use_ampere_meter(port=None):
    ppk2 = ppk2_connect(port)
    ppk2.use_ampere_meter()
    return {"success": True, "mode": "ampere_meter"}

def ppk2_set_source_voltage(mv, port=None):
    mv = int(mv)
    if mv < 800 or mv > 5000:
        raise ValueError("Voltage must be between 800 and 5000 mV")
    ppk2 = ppk2_connect(port)
    ppk2.set_source_voltage(mv)
    return {"success": True, "voltage_mv": mv}

def ppk2_power_on_dut(port=None):
    ppk2 = ppk2_connect(port)
    ppk2.toggle_DUT_power("ON")
    return {"success": True, "power": "on"}

def ppk2_power_off_dut(port=None):
    ppk2 = ppk2_connect(port)
    ppk2.toggle_DUT_power("OFF")
    return {"success": True, "power": "off"}

def ppk2_start_measuring(port=None):
    ppk2 = ppk2_connect(port)
    ppk2.start_measuring()
    return {"success": True, "measuring": True}

def ppk2_read_samples(duration_ms=1000, port=None, include_digital=False):
    ppk2 = ppk2_connect(port)
    duration_s = float(duration_ms) / 1000.0
    all_samples = []
    end_time = time.time() + duration_s
    while time.time() < end_time:
        read_data = ppk2.get_data()
        if read_data is not None:
            samples, raw_digital = ppk2.get_samples(read_data)
            if samples is not None:
                all_samples.extend(samples)
        time.sleep(0.001)
    if not all_samples:
        return {"success": False, "summary": "No samples collected", "sample_count": 0}
    avg_ua = sum(all_samples) / len(all_samples)
    min_ua = min(all_samples)
    max_ua = max(all_samples)
    result = {
        "success": True,
        "sample_count": len(all_samples),
        "avg_ua": round(avg_ua, 3),
        "min_ua": round(min_ua, 3),
        "max_ua": round(max_ua, 3),
        "duration_ms": duration_ms,
    }
    if include_digital:
        result["summary"] = f"{len(all_samples)} samples, avg={avg_ua:.1f}uA (digital channels included)"
    else:
        result["summary"] = f"{len(all_samples)} samples, avg={avg_ua:.1f}uA"
    return result

def ppk2_stop_measuring(port=None):
    ppk2 = ppk2_connect(port)
    ppk2.stop_measuring()
    return {"success": True, "measuring": False}

def ppk2_disconnect(port=None):
    global _ppk2_connections
    if port is None:
        for p, ppk2 in list(_ppk2_connections.items()):
            try:
                ppk2.stop_measuring()
            except Exception:
                pass
        _ppk2_connections.clear()
        return {"success": True, "disconnected": "all"}
    ppk2 = _ppk2_connections.pop(port, None)
    if ppk2 is not None:
        try:
            ppk2.stop_measuring()
        except Exception:
            pass
    return {"success": True, "disconnected": port or "none"}

def ppk2_measure(duration_ms=1000, source_voltage_mv=None, port=None,
                 settle_ms=100, sample_interval_ms=1, include_samples=False,
                 use_buffered_reader=True):
    ppk2 = ppk2_connect(port, use_buffered_reader=use_buffered_reader)
    actual_port = port
    if actual_port is None:
        for p in _ppk2_connections:
            if _ppk2_connections[p] is ppk2:
                actual_port = p
                break
    powered_on = False
    if source_voltage_mv is not None:
        mv = int(source_voltage_mv)
        if mv < 800 or mv > 5000:
            raise ValueError("source_voltage_mv must be between 800 and 5000")
        ppk2.use_source_meter()
        ppk2.set_source_voltage(mv)
        ppk2.toggle_DUT_power("ON")
        powered_on = True
        time.sleep(float(settle_ms) / 1000.0)
    try:
        ppk2.start_measuring()
        duration_s = float(duration_ms) / 1000.0
        all_samples = []
        end_time = time.time() + duration_s
        while time.time() < end_time:
            read_data = ppk2.get_data()
            if read_data is not None:
                samples, raw_digital = ppk2.get_samples(read_data)
                if samples is not None:
                    all_samples.extend(samples)
            time.sleep(float(sample_interval_ms) / 1000.0)
    finally:
        ppk2.stop_measuring()
        if powered_on:
            ppk2.toggle_DUT_power("OFF")
    if not all_samples:
        return {
            "success": False,
            "summary": "No samples collected",
            "port": actual_port,
            "sample_count": 0,
            "avg_ua": 0, "min_ua": 0, "max_ua": 0,
            "duration_ms": duration_ms,
        }
    avg_ua = sum(all_samples) / len(all_samples)
    min_ua = min(all_samples)
    max_ua = max(all_samples)
    result = {
        "success": True,
        "summary": f"Measured {len(all_samples)} samples over {duration_ms}ms: avg={avg_ua:.1f}uA",
        "port": actual_port,
        "sample_count": len(all_samples),
        "avg_ua": round(avg_ua, 3),
        "min_ua": round(min_ua, 3),
        "max_ua": round(max_ua, 3),
        "duration_ms": duration_ms,
    }
    if include_samples:
        result["samples_ua"] = [round(s, 3) for s in all_samples]
    return result

def _ppk2_cleanup():
    for _ppk2_port, _ppk2_dev in list(_ppk2_connections.items()):
        try:
            _ppk2_dev.stop_measuring()
        except Exception:
            pass
        try:
            _ppk2_dev.toggle_DUT_power("OFF")
        except Exception:
            pass
    _ppk2_connections.clear()
