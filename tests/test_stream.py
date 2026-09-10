import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import Mock, patch


class FakeDevice:
    devices = [("/dev/ppk2", "ABC")]

    @staticmethod
    def list_devices():
        return FakeDevice.devices

    def __init__(self, port):
        self.port = port
        self.commands = []
        self.get_data = Mock(return_value=b"samples")
        self.get_samples = Mock(return_value=([1000.0] * 1000, []))

    def get_modifiers(self):
        pass

    def start_measuring(self):
        self.commands.append("start")

    def stop_measuring(self):
        self.commands.append("stop")

    def toggle_DUT_power(self, state):
        self.commands.append(state)

    def use_source_meter(self):
        self.commands.append("source")

    def use_ampere_meter(self):
        self.commands.append("ampere")

    def set_source_voltage(self, voltage):
        self.commands.append(voltage)


def load_bridge():
    sdk = types.ModuleType("ppk2_api.ppk2_api")
    sdk.PPK2_MP = FakeDevice
    ports = types.ModuleType("serial.tools.list_ports")
    ports.comports = lambda: [types.SimpleNamespace(device="/dev/ppk2", serial_number="ABC")]
    spec = importlib.util.spec_from_file_location(
        "test_ppk2_bridge", pathlib.Path(__file__).parents[1] / "ppk2Prelude.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"ppk2_api": types.ModuleType("ppk2_api"), "ppk2_api.ppk2_api": sdk, "serial.tools.list_ports": ports}):
        spec.loader.exec_module(module)
    return module


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.bridge = load_bridge()
        FakeDevice.devices = [("/dev/ppk2", "ABC")]
        self.device = self.bridge.ppk2_connect("/dev/ppk2")
        self.sleep = patch.object(self.bridge.time, "sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.bridge._ppk2_cleanup()
        self.bridge.atexit.unregister(self.bridge._ppk2_cleanup)

    def test_pypi_device_paths_and_fork_device_tuples(self):
        for devices in [["/dev/ppk2"], [("/dev/ppk2", "ABC")]]:
            FakeDevice.devices = devices
            self.assertEqual(self.bridge.ppk2_list_devices(), [{"port": "/dev/ppk2", "serial_number": "ABC"}])
            self.assertEqual(self.bridge._ppk2_stream_port({"serial_number": "ABC"}), "/dev/ppk2")

    def test_window_units_extrema_charge_and_bounded_state(self):
        stats = self.bridge._PPK2Statistics(100)
        self.assertEqual(stats.consume([1000.0] * 999), [])
        window = stats.consume([2000.0])[0]
        self.assertAlmostEqual(window["current_a"], 0.001001)
        self.assertAlmostEqual(window["min_current_a"], 0.001)
        self.assertAlmostEqual(window["max_current_a"], 0.002)
        self.assertAlmostEqual(window["charge_c"], 0.00001001)
        self.assertEqual(window["t"], 0)
        self.assertIsNone(window["voltage_v"])
        self.assertIsNone(window["power_w"])
        for _ in range(1000):
            stats.consume([1000.0] * 1000)
        self.assertEqual(stats.count, 0)
        self.assertFalse(any(isinstance(v, list) for v in vars(stats).values()))

    def test_pause_drains_without_recording_and_resume_keeps_connection(self):
        replies = iter(["run", "pause", "pause", "run", "run", "stop"])
        batches = []
        def on_batch(payload):
            batches.append(payload)
            return {"control": next(replies)}
        result = self.bridge.ppk2_stream({"input_voltage_mv": 3300, "rate_hz": 100}, on_batch)
        self.assertEqual([len(b["samples"]) for b in batches], [0, 1, 0, 0, 0, 1])
        self.assertEqual(result["windows"], 2)
        self.assertEqual(self.device.get_data.call_count, 5)
        self.assertEqual(self.device.commands, ["ampere", 3300, "start", "stop"])
        self.assertEqual(self.bridge._ppk2_connections, {})
        self.assertIsNone(self.bridge._ppk2_capture_state["samples"])

    def test_source_dut_commands_and_stop_power_off(self):
        replies = iter([{"dut": "on"}, {"control": "pause", "dut": "off"}, {"control": "stop", "dut": "on"}])
        self.bridge.ppk2_stream({"mode": "source_meter", "source_voltage_mv": 1800}, lambda _: next(replies))
        self.assertEqual(self.device.commands, ["OFF", "source", 1800, "OFF", "start", "ON", "OFF", "stop", "OFF"])

    def test_bridge_failure_stops_and_powers_off(self):
        with self.assertRaisesRegex(RuntimeError, "disconnected"):
            self.bridge.ppk2_stream({"mode": "source_meter", "source_voltage_mv": 3300}, Mock(side_effect=RuntimeError("disconnected")))
        self.assertEqual(self.device.commands[-2:], ["stop", "OFF"])
        self.assertEqual(self.bridge._ppk2_connections, {})

    def test_start_failure_still_powers_off(self):
        self.device.start_measuring = Mock(side_effect=RuntimeError("start failed"))
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            self.bridge.ppk2_stream({"mode": "source_meter", "source_voltage_mv": 3300, "dut_on": True}, Mock())
        self.assertEqual(self.device.commands[-2:], ["stop", "OFF"])

    def test_no_data_times_out(self):
        self.device.get_data.return_value = b""
        with patch.object(self.bridge.time, "monotonic", side_effect=[0.0, 6.0]):
            with self.assertRaisesRegex(RuntimeError, "stopped delivering"):
                self.bridge.ppk2_stream({"input_voltage_mv": 3300}, lambda _: {})
        self.assertEqual(self.device.commands[-1], "stop")

    def test_invalid_configuration_never_starts_hardware(self):
        for profile in [{"input_voltage_mv": None}, {"input_voltage_mv": 6000}, {"rate_hz": 10000}, {"mode": "source_meter"}, {"mode": "source_meter", "source_voltage_mv": 6000}, {"dut_on": True}, {"mode": "invalid"}]:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                self.bridge.ppk2_stream({"input_voltage_mv": 3300, **profile}, Mock())
        self.assertEqual(self.device.commands, [])

    def test_ambiguous_device_requires_selection(self):
        FakeDevice.devices.append(("/dev/other", "DEF"))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.bridge.ppk2_stream({"input_voltage_mv": 3300}, Mock())
        self.bridge.ppk2_stream({"serial_number": "ABC", "input_voltage_mv": 3300}, lambda _: {"control": "stop"})
        self.assertEqual(self.device.commands, ["ampere", 3300, "start", "stop"])

    def test_idle_dut_keeps_output_and_does_not_auto_publish(self):
        result = self.bridge.ppk2_dut({"mode": "source_meter", "source_voltage_mv": 3300, "dut_on": True})
        self.bridge._ppk2_cleanup()
        self.assertTrue(result["on"])
        self.assertEqual(self.device.commands, ["OFF", "source", 3300, "ON", "stop"])
        self.assertIsNone(self.bridge._ppk2_capture_state["samples"])

    def test_live_uses_shared_bridge_protocol(self):
        session = Mock()
        session.request.return_value = {"control": "stop"}
        with patch.object(self.bridge, "_HardwareBridgeSession", return_value=session, create=True):
            self.bridge.ppk2_live({"stream_id": "test-stream", "input_voltage_mv": 3300})
        self.assertEqual(session.request.call_args.args[0]["cmd"], "power_live")
        self.assertEqual(session.request.call_args.args[0]["payload"]["stream_id"], "test-stream")
        session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
