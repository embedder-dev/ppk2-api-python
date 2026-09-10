# Embedder PPK2 streaming bridge (ENG-4348)

`ppk2Prelude.py` supplies three additional helpers for the Monitor Power tab.
The SDK remains `ppk2-api==0.9.2`; no upstream driver modification is required.

- `ppk2_stream(profile, on_batch)` owns one buffered acquisition until the callback
  returns `{"control": "stop"}`. Each callback receives a batch of statistics.
- `ppk2_live(profile)` connects that loop to Embedder's `power_live` bridge command.
- `ppk2_dut(profile)` sets source-mode DUT output without starting acquisition.
  Its successful output setting persists after the helper returns.

Profile fields: `stream_id`, `port` or `serial_number`, `mode` (`ampere_meter` by
 default or `source_meter`), `source_voltage_mv` (required in source mode, integer
800–5000), `dut_on` (default false), and `rate_hz` (1, 2, 5, 10, 20, 50 or 100;
default 2). With no device selector exactly one PPK2 must be attached.

Callbacks return `control` (`run`, `pause`, `stop`) and optionally `dut` (`on`,
`off`, source mode only). Stop takes precedence over any pending DUT command.
Pause drains and discards samples and partial statistics while retaining the
connection. Timestamps and accumulated charge exclude paused acquisition.

Batches contain `stream_id`, `device` (port), `rate_hz` and `samples`. Each sample
contains `t` in seconds, `current_a`, `min_current_a`, `max_current_a`, and cumulative
`charge_c`. `voltage_v`, `power_w`, and `energy_j` are null: source voltage settings
are not independent voltage measurements. The device's 100 kS/s raw samples are
aggregated into exact sample-count windows rather than wall-clock read intervals.

The stream never populates the legacy raw capture list or auto-publishes a raw
capture. SDK buffering is bounded, statistics retain constant state, and only the
current drain's windows are returned. Recordings are owned by the server.

Five seconds without samples raises an error. Exceptions, interrupts, bridge
failures and normal Stop all stop acquisition; source-mode cleanup also switches
DUT power off. The idle DUT helper intentionally removes its connection from the
legacy exit cleanup so that a successful output setting is preserved.

Run the hardware-independent tests with:

```sh
python3 -m unittest discover -s tests -v
```

The Embedder companion PR contains an opt-in hardware test covering its RPC,
Python process, bridge transport, real SDK, recording and stream controls.
