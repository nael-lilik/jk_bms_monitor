#!/usr/bin/env python3

import asyncio
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List

from bleak import BleakClient


# ============================================================
# CONFIGURATION
# ============================================================

BMS_MAC = "28:D4:1E:8D:71:2C"

BLE_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"

# JK-BMS private enterprise OID
SNMP_ROOT = (1, 3, 6, 1, 4, 1, 55555, 1)

BLE_RECONNECT_DELAY = 5
DATA_TIMEOUT = 120


# ============================================================
# DATA
# ============================================================

@dataclass
class BMSData:
    vendor: str = "JK"
    hardware: str = "23H"
    software: str = "23.02"
    serial: str = ""
    device_name: str = "JK-BD6A24S6P"

    cell_count: int = 15

    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0
    soc: int = 0

    temp: float = 0.0
    mos_temp: float = 0.0

    cycles: int = 0
    capacity: float = 0.0
    remaining_capacity: float = 0.0

    cells: List[float] = field(default_factory=lambda: [0.0] * 32)

    min_cell: float = 0.0
    max_cell: float = 0.0
    delta_cell: float = 0.0

    balance_current: float = 0.0
    balancing: bool = False

    charge_enabled: bool = False
    discharge_enabled: bool = False

    connected: bool = False
    last_update: float = 0.0


DATA = BMSData()
DATA_LOCK = threading.Lock()

STOP_EVENT = threading.Event()


# ============================================================
# HELPERS
# ============================================================

def u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little", signed=False)


def i16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 2], "little", signed=True)


def u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=False)


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + 4], "little", signed=True)


def checksum_ok(frame: bytes) -> bool:
    if len(frame) < 2:
        return False

    return (sum(frame[:-1]) & 0xFF) == frame[-1]


# ============================================================
# JK-BMS RUNTIME PARSER
# ============================================================

def parse_runtime(frame: bytes):
    """
    JK02_32S runtime frame.

    Known offsets:

      Cells:
        byte 6 onward
        2 bytes / cell
        0.001 V

      Enabled cell mask:
        70-73

      MOS temperature:
        144-145
        signed * 0.1 C

      Battery voltage:
        150-153
        uint32 * 0.001 V

      Current:
        158-161
        signed int32 * 0.001 A

      Temp 1:
        162-163
        signed * 0.1 C

      Temp 2:
        164-165

      Balance current:
        170-171
        signed * 0.001 A

      Balance action:
        172

      SOC:
        173

      Remaining capacity:
        174-177
        uint32 * 0.001 Ah

      Nominal capacity:
        178-181
        uint32 * 0.001 Ah

      Cycles:
        182-185
        uint32
    """

    if len(frame) < 190:
        return False

    # Runtime packet type
    if frame[0:4] != bytes.fromhex("55 AA EB 90"):
        return False

    # Cells
    cells = []

    for i in range(32):
        off = 6 + (i * 2)

        if off + 2 > len(frame):
            break

        cells.append(u16(frame, off) * 0.001)

    with DATA_LOCK:

        DATA.cell_count = 0

        # Determine enabled cells from mask.
        enabled_count = 0

        if len(frame) >= 74:
            mask = int.from_bytes(frame[70:74], "little")

            for i, value in enumerate(cells):
                if mask & (1 << i):
                    enabled_count += 1

        # Fallback for this 15S BMS.
        if enabled_count == 0:
            enabled_count = 15

        DATA.cell_count = min(enabled_count, 32)

        for i in range(32):
            DATA.cells[i] = 0.0

        for i in range(min(len(cells), 32)):
            DATA.cells[i] = cells[i]

        active_cells = [
            v for v in DATA.cells[:DATA.cell_count]
            if v > 0
        ]

        if active_cells:
            DATA.min_cell = min(active_cells)
            DATA.max_cell = max(active_cells)
            DATA.delta_cell = DATA.max_cell - DATA.min_cell

        # MOS temperature
        DATA.mos_temp = i16(frame, 144) * 0.1

        # Battery voltage
        DATA.voltage = u32(frame, 150) * 0.001

        # Current
        DATA.current = i32(frame, 158) * 0.001

        # Power
        DATA.power = DATA.voltage * DATA.current

        # Temperature
        DATA.temp = i16(frame, 162) * 0.1

        # Balance current
        DATA.balance_current = i16(frame, 170) * 0.001

        # Balance action
        DATA.balancing = frame[172] != 0

        # SOC
        DATA.soc = frame[173]

        # Remaining capacity
        DATA.remaining_capacity = u32(frame, 174) * 0.001

        # Nominal capacity
        DATA.capacity = u32(frame, 178) * 0.001

        # Cycles
        DATA.cycles = u32(frame, 182)

        DATA.last_update = time.time()


    return True


# ============================================================
# BLE PROTOCOL
# ============================================================

def build_request(command: int) -> bytes:
    """
    JK newer BLE protocol:

        AA 55 90 EB ...

    Commands:

        0x96 = settings
        0x97 = device information
    """

    packet = bytearray(20)

    packet[0:4] = bytes.fromhex("AA 55 90 EB")
    packet[4] = command

    # Remaining bytes are zero.

    packet[-1] = sum(packet[:-1]) & 0xFF

    return bytes(packet)


def parse_packet(buffer: bytearray):
    """
    Extract complete JK BLE frames from notification stream.

    Returns:
        list[bytes], remaining buffer
    """

    frames = []

    while True:

        if len(buffer) < 4:
            break

        # Search frame header.
        pos = buffer.find(bytes.fromhex("55 AA EB 90"))

        if pos < 0:
            # Keep last 3 bytes in case header is fragmented.
            if len(buffer) > 3:
                del buffer[:-3]
            break

        if pos > 0:
            del buffer[:pos]

        if len(buffer) < 300:
            break

        frame = bytes(buffer[:300])

        del buffer[:300]

        if checksum_ok(frame):
            frames.append(frame)

    return frames


async def request_bms(client: BleakClient, command: int):

    packet = build_request(command)

    try:
        await client.write_gatt_char(
            BLE_CHAR,
            packet,
            response=False,
        )
    except Exception:
        try:
            await client.write_gatt_char(
                BLE_CHAR,
                packet,
                response=True,
            )
        except Exception as e:
            print(
                f"[BLE] write command 0x{command:02X} failed: {e}",
                file=sys.stderr,
            )


# ============================================================
# BLE WORKER
# ============================================================

async def ble_worker():

    while not STOP_EVENT.is_set():

        buffer = bytearray()

        def notification_handler(sender, data: bytearray):

            nonlocal buffer

            # Ignore AT\r\n
            if bytes(data) == b"AT\r\n":
                return

            buffer.extend(data)

            frames = parse_packet(buffer)

            for frame in frames:
                parse_runtime(frame)

        try:

            print(
                f"[BLE] connecting to {BMS_MAC}",
                file=sys.stderr,
            )

            async with BleakClient(
                BMS_MAC,
                timeout=15,
            ) as client:

                print(
                    "[BLE] connected",
                    file=sys.stderr,
                )

                with DATA_LOCK:
                    DATA.connected = True

                await client.start_notify(
                    BLE_CHAR,
                    notification_handler,
                )

                # Request settings and device info.
                await request_bms(client, 0x96)

                await asyncio.sleep(1)

                await request_bms(client, 0x97)

                # Keep connection alive.
                while (
                    not STOP_EVENT.is_set()
                    and client.is_connected
                ):
                    await asyncio.sleep(5)

                    # Ask again periodically.
                    await request_bms(client, 0x96)

                    await asyncio.sleep(1)

                    await request_bms(client, 0x97)

        except Exception as e:

            print(
                f"[BLE] error: {e}",
                file=sys.stderr,
            )

        finally:

            with DATA_LOCK:
                DATA.connected = False

            print(
                "[BLE] disconnected",
                file=sys.stderr,
            )

        if not STOP_EVENT.is_set():
            await asyncio.sleep(BLE_RECONNECT_DELAY)


def ble_thread_main():

    try:
        asyncio.run(ble_worker())

    except Exception as e:
        print(
            f"[BLE] worker stopped: {e}",
            file=sys.stderr,
        )


# ============================================================
# SNMP OID TABLE
# ============================================================

def oid(*parts):
    return ".".join(str(x) for x in parts)


# Scalar OIDs
OID_MAP = {}


def register_oid(suffix, value_type, getter):
    """
    suffix:
        tuple relative to SNMP_ROOT

    value_type:
        integer
        gauge
        counter
        string

    getter:
        callable returning value
    """

    full = SNMP_ROOT + tuple(suffix)

    OID_MAP[full] = {
        "type": value_type,
        "getter": getter,
    }


# ============================================================
# BMS OIDS
# ============================================================

register_oid(
    (1,),
    "gauge",
    lambda d: round(d.voltage * 1000),
)

register_oid(
    (2,),
    "integer",
    lambda d: round(d.current * 1000),
)

register_oid(
    (3,),
    "integer",
    lambda d: round(d.power),
)

register_oid(
    (4,),
    "gauge",
    lambda d: d.soc,
)

register_oid(
    (5,),
    "integer",
    lambda d: round(d.temp * 10),
)

register_oid(
    (6,),
    "integer",
    lambda d: round(d.mos_temp * 10),
)

register_oid(
    (7,),
    "counter",
    lambda d: d.cycles,
)

register_oid(
    (8,),
    "gauge",
    lambda d: round(d.capacity * 1000),
)

register_oid(
    (9,),
    "integer",
    lambda d: d.cell_count,
)

register_oid(
    (10,),
    "gauge",
    lambda d: round(d.min_cell * 1000),
)

register_oid(
    (11,),
    "gauge",
    lambda d: round(d.max_cell * 1000),
)

register_oid(
    (12,),
    "gauge",
    lambda d: round(d.delta_cell * 1000),
)

register_oid(
    (13,),
    "integer",
    lambda d: round(d.balance_current * 1000),
)

register_oid(
    (14,),
    "integer",
    lambda d: 1 if d.balancing else 0,
)

register_oid(
    (15,),
    "integer",
    lambda d: 1 if d.charge_enabled else 0,
)

register_oid(
    (16,),
    "integer",
    lambda d: 1 if d.discharge_enabled else 0,
)

register_oid(
    (17,),
    "gauge",
    lambda d: round(d.remaining_capacity * 1000),
)

register_oid(
    (18,),
    "integer",
    lambda d: 1 if (
        d.connected
        and (time.time() - d.last_update) < DATA_TIMEOUT
    ) else 0,
)


# ============================================================
# DEVICE INFORMATION
# ============================================================

register_oid(
    (100,),
    "string",
    lambda d: d.vendor,
)

register_oid(
    (101,),
    "string",
    lambda d: d.hardware,
)

register_oid(
    (102,),
    "string",
    lambda d: d.software,
)

register_oid(
    (103,),
    "string",
    lambda d: d.serial,
)

register_oid(
    (104,),
    "string",
    lambda d: d.device_name,
)


# ============================================================
# CELL VOLTAGES
# ============================================================

for cell_index in range(1, 33):

    register_oid(
        (20, cell_index),
        "gauge",
        lambda d, i=cell_index - 1:
            round(d.cells[i] * 1000),
    )


# Sorted OIDs for GETNEXT
SORTED_OIDS = sorted(OID_MAP.keys())


# ============================================================
# SNMP VALUE
# ============================================================

def get_value(entry):

    with DATA_LOCK:
        value = entry["getter"](DATA)

    return value


def snmp_output(oid_tuple, entry):

    value_type = entry["type"]
    value = get_value(entry)

    return (
        f"{value_type}\n"
        f"{value}\n"
    )


# ============================================================
# PASS_PERSIST PROTOCOL
# ============================================================

def find_exact(request_oid):

    return OID_MAP.get(tuple(request_oid))


def find_next(request_oid):

    request_oid = tuple(request_oid)

    for candidate in SORTED_OIDS:

        if candidate > request_oid:
            return candidate, OID_MAP[candidate]

    return None


def parse_oid(text):

    text = text.strip()

    if text.startswith("."):
        text = text[1:]

    if not text:
        return ()

    try:
        return tuple(
            int(x)
            for x in text.split(".")
            if x != ""
        )

    except ValueError:
        return None


def handle_get(request_oid):

    entry = find_exact(request_oid)

    if entry is None:
        print("NONE", flush=True)
        return

    print(
        snmp_output(request_oid, entry),
        end="",
        flush=True,
    )


def handle_getnext(request_oid):

    result = find_next(request_oid)

    if result is None:
        print("NONE", flush=True)
        return

    next_oid, entry = result

    print(
        "." + ".".join(map(str, next_oid)),
        flush=True,
    )

    print(
        snmp_output(next_oid, entry),
        end="",
        flush=True,
    )


def handle_set():

    # Read SET payload according to pass_persist protocol.
    # We don't support writes.

    print("not-writable", flush=True)


def snmp_loop():

    """
    Net-SNMP pass_persist protocol.

    Commands:

        PING
        get
        getnext
        set
    """

    print("PONG", flush=True)

    while not STOP_EVENT.is_set():

        line = sys.stdin.readline()

        if not line:
            break

        command = line.strip().lower()

        if command == "ping":
            print("PONG", flush=True)
            continue

        if command == "get":

            oid_line = sys.stdin.readline()

            if not oid_line:
                break

            request_oid = parse_oid(oid_line)

            if request_oid is None:
                print("NONE", flush=True)
            else:
                handle_get(request_oid)

            continue

        if command == "getnext":

            oid_line = sys.stdin.readline()

            if not oid_line:
                break

            request_oid = parse_oid(oid_line)

            if request_oid is None:
                print("NONE", flush=True)
            else:
                handle_getnext(request_oid)

            continue

        if command == "set":

            handle_set()

            continue

        # Unknown command
        print("NONE", flush=True)


# ============================================================
# SIGNALS
# ============================================================

def signal_handler(signum, frame):

    print(
        f"[SYSTEM] signal {signum}, stopping...",
        file=sys.stderr,
    )

    STOP_EVENT.set()


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)


# ============================================================
# MAIN
# ============================================================

def main():

    # BLE runs independently from SNMP stdin/stdout.
    thread = threading.Thread(
        target=ble_thread_main,
        daemon=True,
    )

    thread.start()

    # IMPORTANT:
    # stdout is exclusively for pass_persist protocol.
    # All logging goes to stderr.
    snmp_loop()

    STOP_EVENT.set()

    thread.join(timeout=5)


if __name__ == "__main__":
    main()
