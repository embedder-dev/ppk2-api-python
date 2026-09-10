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
import math
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

_PPK2_STREAM_RATES_HZ = (1, 2, 5, 10, 20, 50, 100)


def _ppk2_stream_port(profile):
    devices = ppk2_list_devices()
    port = profile.get("port")
    serial = profile.get("serial_number")
    matches = [d for d in devices if (not port or d["port"] == port)
               and (not serial or d["serial_number"] == serial)]
    if len(matches) != 1:
        raise ValueError("Select exactly one connected PPK2 by port or serial number")
    return matches[0]["port"]


def _ppk2_stream_config(profile):
    mode = profile.get("mode", "ampere_meter")
    if mode not in ("source_meter", "ampere_meter"):
        raise ValueError("Unknown PPK2 measurement mode")
    voltage = profile.get("source_voltage_mv")
    if mode == "source_meter":
        if isinstance(voltage, bool) or not isinstance(voltage, (int, float)) or not 800 <= voltage <= 5000 or int(voltage) != voltage:
            raise ValueError("Source mode requires an integer voltage between 800 and 5000 mV")
    if mode == "ampere_meter" and profile.get("dut_on"):
        raise ValueError("DUT power control requires source meter mode")
    return mode, voltage


class _PPK2Statistics:
    def __init__(self, rate_hz):
        self.window_size = _PPK2_SAMPLE_RATE_HZ // rate_hz
        self.rate_hz = rate_hz
        self.emitted = 0
        self.charge_c = 0.0
        self.discard()

    def discard(self):
        self.count = 0
        self.total = 0.0
        self.minimum = float("inf")
        self.maximum = float("-inf")

    def consume(self, samples):
        windows = []
        for current_ua in samples:
            current_a = float(current_ua) * 1e-6
            if not math.isfinite(current_a):
                raise ValueError("PPK2 returned a non-finite current sample")
            self.count += 1
            self.total += current_a
            self.minimum = min(self.minimum, current_a)
            self.maximum = max(self.maximum, current_a)
            if self.count == self.window_size:
                self.charge_c += self.total / _PPK2_SAMPLE_RATE_HZ
                windows.append({
                    "t": self.emitted / self.rate_hz,
                    "current_a": self.total / self.count,
                    "min_current_a": self.minimum,
                    "max_current_a": self.maximum,
                    "charge_c": self.charge_c,
                    "voltage_v": None,
                    "power_w": None,
                    "energy_j": None,
                })
                self.emitted += 1
                self.discard()
        return windows


def ppk2_stream(profile, on_batch):
    mode, voltage = _ppk2_stream_config(profile)
    rate_hz = profile.get("rate_hz", 2)
    if isinstance(rate_hz, bool) or rate_hz not in _PPK2_STREAM_RATES_HZ:
        raise ValueError("PPK2 statistics rate must be one of 1, 2, 5, 10, 20, 50, 100 Hz")
    port = _ppk2_stream_port(profile)
    device = ppk2_connect(port)
    stats = _PPK2Statistics(int(rate_hz))
    stream_id = str(profile.get("stream_id", ""))
    previous_control = "run"
    discard_next_batch = False
    last_data = time.monotonic()

    def push(samples):
        return on_batch({"stream_id": stream_id, "device": port,
                         "rate_hz": rate_hz, "samples": samples}) or {}

    try:
        if mode == "source_meter":
            device.toggle_DUT_power("OFF")
            device.use_source_meter()
            device.set_source_voltage(int(voltage))
            device.toggle_DUT_power("ON" if profile.get("dut_on") else "OFF")
        else:
            device.use_ampere_meter()
        device.start_measuring()
        response = push([])
        while True:
            control = response.get("control", "run")
            if control == "stop":
                break
            if control not in ("run", "pause"):
                raise ValueError("Unknown PPK2 stream control")
            if previous_control == "pause" and control == "run":
                discard_next_batch = True
            previous_control = control
            dut = response.get("dut")
            if dut is not None:
                if mode != "source_meter" or dut not in ("on", "off"):
                    raise ValueError("Invalid PPK2 DUT power command")
                device.toggle_DUT_power(dut.upper())
            raw = device.get_data()
            windows = []
            if raw:
                samples, _ = device.get_samples(raw)
                if samples:
                    last_data = time.monotonic()
                    if control == "run" and not discard_next_batch:
                        windows = stats.consume(samples)
                    discard_next_batch = False
            if control == "pause":
                stats.discard()
            if time.monotonic() - last_data > 5.0:
                raise RuntimeError("PPK2 stopped delivering samples; check the USB connection")
            response = push(windows)
            time.sleep(0.05)
    finally:
        try:
            device.stop_measuring()
        finally:
            try:
                if mode == "source_meter":
                    device.toggle_DUT_power("OFF")
            finally:
                _ppk2_connections.pop(port, None)
    return {"success": True, "summary": "PPK2 stream stopped",
            "windows": stats.emitted, "rate_hz": rate_hz}


def ppk2_live(profile):
    bridge = _HardwareBridgeSession(timeout=5.0)
    try:
        return ppk2_stream(profile, lambda payload: bridge.request(
            {"cmd": "power_live", "payload": payload}))
    finally:
        bridge.close()


def ppk2_dut(profile):
    mode, voltage = _ppk2_stream_config(profile)
    if mode != "source_meter":
        raise ValueError("DUT power control requires source meter mode")
    port = _ppk2_stream_port(profile)
    device = ppk2_connect(port)
    try:
        device.toggle_DUT_power("OFF")
        device.use_source_meter()
        device.set_source_voltage(int(voltage))
        device.toggle_DUT_power("ON" if profile.get("dut_on") else "OFF")
    except BaseException:
        device.toggle_DUT_power("OFF")
        raise
    finally:
        try:
            device.stop_measuring()
        finally:
            _ppk2_connections.pop(port, None)
    return {"success": True, "on": bool(profile.get("dut_on"))}
