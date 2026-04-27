# ppk2Prelude.py — PPK2 hardware helper bridge
# Copyright (c) 2026 Embedder Pty Ltd
# SPDX-License-Identifier: GPL-2.0-only
#
# This file is licensed under the GNU General Public License v2.0.
# It serves to improve the usability of the following package:
# ppk2-api Python package (https://github.com/IRNAS/ppk2-api-python).
# You may redistribute and/or modify it under the terms of the GPL v2.
# See https://www.gnu.org/licenses/old-licenses/gpl-2.0.html

import atexit
import csv
import json
import os
import sys
import tempfile
import time
import uuid

from ppk2_api.ppk2_api import PPK2_MP

_PPK2_SAMPLE_RATE_HZ = 100_000

_ppk2_connections = {}

_ppk2_capture_state = {
    "samples": None,
    "sample_rate": None,
    "started_at": None,
    "published": False,
}

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
    _ppk2_capture_state.update({
        "samples": [],
        "sample_rate": _PPK2_SAMPLE_RATE_HZ,
        "started_at": time.monotonic(),
        "published": False,
    })
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
                if _ppk2_capture_state["samples"] is not None:
                    _ppk2_capture_state["samples"].extend(samples)
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
    try:
        ppk2_publish_capture()
    finally:
        _ppk2_capture_state["samples"] = None
        _ppk2_capture_state["sample_rate"] = None
        _ppk2_capture_state["started_at"] = None
        _ppk2_capture_state["published"] = False
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
    else:
        ppk2.use_ampere_meter()
    _ppk2_capture_state.update({
        "samples": [],
        "sample_rate": _PPK2_SAMPLE_RATE_HZ,
        "started_at": time.monotonic(),
        "published": False,
    })
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
                    if _ppk2_capture_state["samples"] is not None:
                        _ppk2_capture_state["samples"].extend(samples)
            time.sleep(float(sample_interval_ms) / 1000.0)
    finally:
        ppk2.stop_measuring()
        if powered_on:
            ppk2.toggle_DUT_power("OFF")
        try:
            ppk2_publish_capture(name=f"PPK2 measure {duration_ms}ms")
        finally:
            _ppk2_capture_state["samples"] = None
            _ppk2_capture_state["sample_rate"] = None
            _ppk2_capture_state["started_at"] = None
            _ppk2_capture_state["published"] = False
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

def _ppk2_bridge_publish_capture(payload):
    try:
        _hardware_bridge_request({"cmd": "capture_published", "payload": payload})
    except NameError:
        return
    except Exception as exc:
        sys.stderr.write(f"[embedder] ppk2 capture_published failed: {exc}\n")
        sys.stderr.flush()

def _ppk2_write_analog_csv(samples, sample_rate):
    tmpdir = tempfile.mkdtemp(prefix="embedder_ppk2_")
    path = os.path.join(tmpdir, "analog.csv")
    dt = 1.0 / sample_rate if sample_rate else 0.0
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_sec", "current_ua"])
        for i, v in enumerate(samples):
            w.writerow([f"{i * dt:.9f}", f"{v:.6f}"])
    return tmpdir

def ppk2_publish_capture(name=None):
    state = _ppk2_capture_state
    if state["samples"] is None or state["published"]:
        return None
    samples = state["samples"]
    sample_rate = state["sample_rate"] or 0
    duration = (len(samples) / sample_rate) if sample_rate else 0.0
    tmpdir = _ppk2_write_analog_csv(samples, sample_rate) if samples else None
    payload = {
        "capture_id": uuid.uuid4().hex,
        "name": name or f"PPK2 {time.strftime('%H:%M:%S')}",
        "sample_rate": int(sample_rate),
        "duration_sec": float(duration),
        "sample_count": len(samples),
        "digital_channels": [],
        "analog_channels": [{"name": "current_ua", "index": 0}],
        "analyzers": [],
    }
    if tmpdir is not None:
        payload["analog_csv_dir"] = tmpdir
    _ppk2_bridge_publish_capture(payload)
    state["published"] = True
    return payload["capture_id"]

def _ppk2_cleanup():
    try:
        ppk2_publish_capture()
    except Exception:
        pass
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

atexit.register(_ppk2_cleanup)
