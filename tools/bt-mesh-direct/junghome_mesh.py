#!/usr/bin/env python3
"""Reference prototype: control JUNG HOME devices over Bluetooth Mesh, gateway-free.

This mirrors what the gateway's `middleware` does, but from your own host driving
a Silicon Labs EFR32 acting as a Bluetooth Mesh NCP over a serial port (the same
silicon + SDK the JUNG gateway uses, so the commands map 1:1).

It is a *reference* implementation grounded in the reverse-engineered protocol in
docs/bt-mesh-direct.md — it requires real hardware to run and assumes the EFR32
has already joined the JUNG mesh (NetKey/AppKey provisioned, the node's AppKey
bound to the relevant client models). Provisioning/key import is out of scope
here; do it once with Simplicity Studio's Bluetooth Mesh tooling or by importing
the CDB exported from the JUNG HOME app.

Hardware: flash an EFR32 dev kit with the Silabs "Bluetooth Mesh - NCP" example.
Then:  pip install pybgapi  (see requirements.txt)

Method names follow the Silabs BGAPI (`sl_bt.xapi` / `sl_btmesh.xapi`); exact
signatures may shift between SDK versions — adjust to your installed SDK.
"""

from __future__ import annotations

import argparse
import time

import bgapi  # pybgapi

# --- protocol constants (from middleware/dist/const, see docs/bt-mesh-direct.md) ---

# SIG client model ids
MODEL_GENERIC_ONOFF_CLIENT = 0x1001
MODEL_GENERIC_LEVEL_CLIENT = 0x1003
MODEL_LIGHT_LIGHTNESS_CLIENT = 0x1302

# MeshModelSetKind
KIND_ONOFF = 0
KIND_LEVEL = 2
KIND_LEVEL_MOVE = 4
KIND_LIGHTNESS_ACTUAL = 128

# Send tuning (config.json -> btmesh)
RETRANSMISSIONS = 3
INTERVAL_MS = 15
APPKEY_INDEX = 0

# Color temperature: JUNG drives Generic Level on the element after the main one,
# mapping Kelvin 2000..6000 onto int16 -0x8000..0x7FFF.
CT_KELVIN_MIN, CT_KELVIN_MAX = 2000, 6000
LEVEL_MIN, LEVEL_MAX = -0x8000, 0x7FFF


def convert_range(value, in_min, in_max, out_min, out_max):
    """Linear range conversion, matching the gateway's helper."""
    if in_max == in_min:
        return out_min
    frac = (value - in_min) / (in_max - in_min)
    return round(out_min + frac * (out_max - out_min))


def to_u16_le(value: int) -> bytes:
    return (value & 0xFFFF).to_bytes(2, "little")


class JungMesh:
    """Thin wrapper over a Silabs EFR32 BT-Mesh NCP."""

    def __init__(self, port: str):
        self.lib = bgapi.BGLib(
            bgapi.SerialConnector(port), "sl_bt.xapi", "sl_btmesh.xapi"
        )
        self._tid = 0

    def open(self):
        self.lib.open()
        # bring up the stack / confirm we talk to the NCP
        self.lib.bt.system.get_version()

    def close(self):
        self.lib.close()

    def _next_tid(self) -> int:
        self._tid = (self._tid + 1) & 0xFF
        return self._tid

    def _generic_client_set(self, address, model_id, kind, value_bytes, transition_ms=0):
        """sl_btmesh_cmd_generic_client_set, retransmitted like the gateway."""
        tid = self._next_tid()
        for i in range(RETRANSMISSIONS):
            delay_ms = INTERVAL_MS * (RETRANSMISSIONS - 1 - i)
            self.lib.btmesh.generic_client.set(
                address,          # server_address
                0,                # elem_index
                model_id,         # model_id
                APPKEY_INDEX,     # appkey_index
                tid,              # tid
                transition_ms,    # transition_ms
                delay_ms,         # delay_ms
                1,                # flags
                kind,             # type (MeshModelSetKind)
                value_bytes,      # parameters
            )
            if i < RETRANSMISSIONS - 1:
                time.sleep(INTERVAL_MS / 1000)

    # --- standard SIG model control ---

    def set_onoff(self, address: int, on: bool):
        self._generic_client_set(
            address, MODEL_GENERIC_ONOFF_CLIENT, KIND_ONOFF, bytes([1 if on else 0])
        )

    def set_brightness(self, address: int, percent: int):
        """percent 0..100 -> Light Lightness Actual uint16."""
        value = convert_range(percent, 0, 100, 0, 0xFFFF)
        self._generic_client_set(
            address, MODEL_LIGHT_LIGHTNESS_CLIENT, KIND_LIGHTNESS_ACTUAL, to_u16_le(value)
        )

    def set_color_temp(self, address: int, kelvin: int):
        """Generic Level on the CTL temperature element (address + 1)."""
        level = convert_range(kelvin, CT_KELVIN_MIN, CT_KELVIN_MAX, LEVEL_MIN, LEVEL_MAX)
        self._generic_client_set(
            address + 1, MODEL_GENERIC_LEVEL_CLIENT, KIND_LEVEL, to_u16_le(level)
        )

    def blinds_move(self, address: int, direction: str):
        """direction: 'up' | 'down' | 'stop' (Generic Level Move)."""
        value = {"down": 0x7FFF, "up": 0x8000, "stop": 0x0000}[direction]
        self._generic_client_set(
            address, MODEL_GENERIC_LEVEL_CLIENT, KIND_LEVEL_MOVE, to_u16_le(value),
            transition_ms=0xFFFE,
        )

    def recall_scene(self, scene_number: int, address: int = 0xFFFF):
        tid = self._next_tid()
        for i in range(RETRANSMISSIONS):
            delay_ms = INTERVAL_MS * (RETRANSMISSIONS - 1 - i)
            self.lib.btmesh.scene_client.recall(
                address, 0, scene_number, APPKEY_INDEX, 1, tid, 0, delay_ms
            )
            if i < RETRANSMISSIONS - 1:
                time.sleep(INTERVAL_MS / 1000)

    # --- vendor model (status LED / buttons), company 0x0527 ---

    def set_status_led(self, address: int, model_client: int, prop_id: int, on: bool):
        """JUNG vendor 'Property' model via the host->NCP passthrough.

        Mirrors btmesh_set_datapoint_service._sendKeyStatus (commandId 2 =
        STATUS_LBC_PROP_SEND_ID). See docs/bt-mesh-direct.md.
        """
        self.lib.bt.user.message_to_target(
            bytes([
                2,                       # commandId STATUS_LBC_PROP_SEND_ID
                0,                       # elementId (lo) ...
            ])
            # NOTE: exact field packing for user_message_to_target depends on the
            # vendor app on the NCP; see the documented field list. Left as a
            # starting point for the buttons/LED path.
        )

    # --- receiving state / events ---

    def listen(self):
        """Print status and button events as they arrive."""
        for evt in self.lib.gen_events(max_time=None):
            name = evt._str  # e.g. 'btmesh_evt_generic_client_server_status'
            if "generic_client_server_status" in name:
                print(f"state  addr=0x{evt.server_address:04x} model=0x{evt.model_id:04x} "
                      f"params={bytes(evt.parameters).hex()}")
            elif "sensor_client_status" in name:
                print(f"sensor addr=0x{evt.server_address:04x} data={bytes(evt.sensor_data).hex()}")
            elif "scene_client_status" in name:
                print(f"scene  addr=0x{evt.server_address:04x} current={evt.current_scene}")
            elif "user_message_to_host" in name:
                print(f"vendor data={bytes(evt.message).hex()}")


def main():
    ap = argparse.ArgumentParser(description="JUNG HOME BT-Mesh direct control (prototype)")
    ap.add_argument("--port", default="/dev/ttyACM0", help="EFR32 NCP serial port")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("onoff"); p.add_argument("address", type=lambda x: int(x, 0)); p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("brightness"); p.add_argument("address", type=lambda x: int(x, 0)); p.add_argument("percent", type=int)
    p = sub.add_parser("ct"); p.add_argument("address", type=lambda x: int(x, 0)); p.add_argument("kelvin", type=int)
    p = sub.add_parser("scene"); p.add_argument("number", type=int)
    sub.add_parser("listen")
    args = ap.parse_args()

    mesh = JungMesh(args.port)
    mesh.open()
    try:
        if args.cmd == "onoff":
            mesh.set_onoff(args.address, args.state == "on")
        elif args.cmd == "brightness":
            mesh.set_brightness(args.address, args.percent)
        elif args.cmd == "ct":
            mesh.set_color_temp(args.address, args.kelvin)
        elif args.cmd == "scene":
            mesh.recall_scene(args.number)
        elif args.cmd == "listen":
            mesh.listen()
    finally:
        mesh.close()


if __name__ == "__main__":
    main()
