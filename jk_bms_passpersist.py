#!/usr/bin/env python3

import asyncio
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from bleak import BleakClient


# ============================================================
# CONFIGURATION
# ============================================================

BMS_MAC = "28:D4:1E:8D:71:2C"

BLE_SERVICE = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_CHAR = "0000ffe1-0000-1000-8000-00805f9b34fb"

# Private enterprise OID
SNMP_ROOT = (1, 3, 6, 1, 4, 1, 55555, 1)

# BLE
BLE_CONNECT_TIMEOUT = 15
BLE_RECONNECT_DELAY = 5

# Request interval
BMS_REQUEST_INTERVAL = 10

# Consider BMS data stale after this period
DATA_TIMEOUT = 120

# Runtime log interval
RUNTIME_LOG_INTERVAL = 10


# ============================================================
# GLOBAL STOP
# ============================================================

STOP_EVENT = threading.Event()


# ============================================================
# BMS DATA
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

    cells: List[float] = field(
        default_factory=lambda: [0.0] * 32
    )

    min_cell: float = 0.0

    max_cell: float = 0.0

    delta_cell: float = 0.0

    balance_current: float = 0.0

    balancing: bool = False

    charge_enabled: bool = False

    discharge_enabled: bool = False

    connected: bool = False

    last_update: float = 0.0

    runtime_frames: int = 0

    last_runtime_log: float = 0.0


DATA = BMSData()

DATA_LOCK = threading.RLock()


# ============================================================
# LOGGING
# ============================================================

def log(message: str):

    """
    IMPORTANT:

    pass_persist uses stdout for SNMP protocol.

    Therefore ALL diagnostic output must go to stderr.
    """

    print(
        message,
        file=sys.stderr,
        flush=True,
    )


# ============================================================
# BYTE HELPERS
# ============================================================

def u16(data: bytes, offset: int) -> int:

    return int.from_bytes(
        data[offset:offset + 2],
        "little",
        signed=False,
    )


def i16(data: bytes, offset: int) -> int:

    return int.from_bytes(
        data[offset:offset + 2],
        "little",
        signed=True,
    )


def u32(data: bytes, offset: int) -> int:

    return int.from_bytes(
        data[offset:offset + 4],
        "little",
        signed=False,
    )


def i32(data: bytes, offset: int) -> int:

    return int.from_bytes(
        data[offset:offset + 4],
        "little",
        signed=True,
    )


# ============================================================
# CHECKSUM
# ============================================================

def checksum_ok(frame: bytes) -> bool:

    if len(frame) < 2:
        return False

    expected = sum(frame[:-1]) & 0xFF

    actual = frame[-1]

    return expected == actual


# ============================================================
# JK BLE REQUEST
# ============================================================

def build_request(command: int) -> bytes:

    """
    Newer JK-BMS BLE protocol.

    Request:

        AA 55 90 EB ...

    Commands:

        0x96 = settings
        0x97 = device information
    """

    packet = bytearray(20)

    packet[0:4] = bytes.fromhex(
        "AA 55 90 EB"
    )

    packet[4] = command

    # Remaining bytes remain zero.

    packet[-1] = (
        sum(packet[:-1]) & 0xFF
    )

    return bytes(packet)


# ============================================================
# BLE FRAME EXTRACTION
# ============================================================

JK_FRAME_HEADER = bytes.fromhex(
    "55 AA EB 90"
)


def extract_frames(buffer: bytearray) -> List[bytes]:

    """
    BLE notifications may be fragmented because of MTU.

    JK-BMS response is 300 bytes.

    Extract all complete frames from buffer.
    """

    frames = []

    while True:

        if len(buffer) < 4:
            break

        position = buffer.find(
            JK_FRAME_HEADER
        )

        if position < 0:

            # Preserve possible fragmented header.
            if len(buffer) > 3:
                del buffer[:-3]

            break

        # Remove bytes before header.
        if position > 0:
            del buffer[:position]

        # Complete JK frame is 300 bytes.
        if len(buffer) < 300:
            break

        frame = bytes(
            buffer[:300]
        )

        del buffer[:300]

        if checksum_ok(frame):
            frames.append(frame)

        else:
            log(
                "[BLE] invalid checksum, "
                "discarding frame"
            )

    return frames


# ============================================================
# RUNTIME FRAME PARSER
# ============================================================

def parse_runtime(frame: bytes) -> bool:

    """
    JK02_32S runtime frame.

    Known offsets:

      Cells:
        byte 6 onward
        2 bytes per cell
        * 0.001 V

      Enabled cell mask:
        bytes 70-73

      MOS temperature:
        bytes 144-145
        signed int16
        * 0.1 C

      Battery voltage:
        bytes 150-153
        uint32
        * 0.001 V

      Current:
        bytes 158-161
        signed int32
        * 0.001 A

      Temperature 1:
        bytes 162-163
        signed int16
        * 0.1 C

      Temperature 2:
        bytes 164-165

      Balance current:
        bytes 170-171
        signed int16
        * 0.001 A

      Balance action:
        byte 172

      SOC:
        byte 173

      Remaining capacity:
        bytes 174-177
        uint32
        * 0.001 Ah

      Nominal capacity:
        bytes 178-181
        uint32
        * 0.001 Ah

      Cycles:
        bytes 182-185
        uint32
    """

    if len(frame) < 190:
        return False

    if frame[0:4] != JK_FRAME_HEADER:
        return False

    # --------------------------------------------------------
    # Cells
    # --------------------------------------------------------

    parsed_cells = []

    for index in range(32):

        offset = 6 + (
            index * 2
        )

        if offset + 2 > len(frame):
            break

        voltage = (
            u16(frame, offset)
            * 0.001
        )

        parsed_cells.append(
            voltage
        )

    # --------------------------------------------------------
    # Enabled cell mask
    # --------------------------------------------------------

    enabled_count = 0

    if len(frame) >= 74:

        cell_mask = int.from_bytes(
            frame[70:74],
            "little",
            signed=False,
        )

        for index in range(
            min(32, len(parsed_cells))
        ):

            if cell_mask & (
                1 << index
            ):
                enabled_count += 1

    # This BMS is known to be 15S.
    if enabled_count == 0:
        enabled_count = 15

    enabled_count = min(
        enabled_count,
        32,
    )

    # --------------------------------------------------------
    # Extract runtime values
    # --------------------------------------------------------

    mos_temp = (
        i16(frame, 144)
        * 0.1
    )

    voltage = (
        u32(frame, 150)
        * 0.001
    )

    current = (
        i32(frame, 158)
        * 0.001
    )

    temp1 = (
        i16(frame, 162)
        * 0.1
    )

    balance_current = (
        i16(frame, 170)
        * 0.001
    )

    balancing = (
        frame[172] != 0
    )

    soc = frame[173]

    remaining_capacity = (
        u32(frame, 174)
        * 0.001
    )

    nominal_capacity = (
        u32(frame, 178)
        * 0.001
    )

    cycles = u32(
        frame,
        182,
    )

    power = (
        voltage * current
    )

    # --------------------------------------------------------
    # Cell min/max
    # --------------------------------------------------------

    active_cells = [
        value
        for value in parsed_cells[
            :enabled_count
        ]
        if value > 0
    ]

    if active_cells:

        min_cell = min(
            active_cells
        )

        max_cell = max(
            active_cells
        )

        delta_cell = (
            max_cell - min_cell
        )

    else:

        min_cell = 0.0
        max_cell = 0.0
        delta_cell = 0.0

    # --------------------------------------------------------
    # Update cache
    # --------------------------------------------------------

    with DATA_LOCK:

        DATA.cell_count = (
            enabled_count
        )

        DATA.voltage = voltage

        DATA.current = current

        DATA.power = power

        DATA.soc = soc

        DATA.temp = temp1

        DATA.mos_temp = mos_temp

        DATA.balance_current = (
            balance_current
        )

        DATA.balancing = balancing

        DATA.remaining_capacity = (
            remaining_capacity
        )

        DATA.capacity = (
            nominal_capacity
        )

        DATA.cycles = cycles

        DATA.min_cell = min_cell

        DATA.max_cell = max_cell

        DATA.delta_cell = delta_cell

        for index in range(32):
            DATA.cells[index] = 0.0

        for index, value in enumerate(
            parsed_cells[:32]
        ):
            DATA.cells[index] = value

        DATA.last_update = time.time()

        DATA.runtime_frames += 1

        now = time.time()

        should_log = (
            now - DATA.last_runtime_log
            >= RUNTIME_LOG_INTERVAL
        )

        if should_log:

            DATA.last_runtime_log = now

    # --------------------------------------------------------
    # Runtime diagnostic
    # --------------------------------------------------------

    if should_log:

        log(
            "[BLE] runtime: "
            f"{voltage:.3f}V "
            f"{current:.3f}A "
            f"{power:.1f}W "
            f"SOC={soc}% "
            f"T={temp1:.1f}C "
            f"MOS={mos_temp:.1f}C "
            f"cells={enabled_count} "
            f"min={min_cell:.3f}V "
            f"max={max_cell:.3f}V "
            f"delta={delta_cell * 1000:.0f}mV "
            f"balance={balance_current:.3f}A "
            f"balancing={balancing} "
            f"cycles={cycles}"
        )

    return True


# ============================================================
# BLE REQUEST
# ============================================================

async def request_bms(
    client: BleakClient,
    command: int,
):

    packet = build_request(
        command
    )

    try:

        await client.write_gatt_char(
            BLE_CHAR,
            packet,
            response=False,
        )

        log(
            f"[BLE] command 0x{command:02X} sent"
        )

    except Exception as first_error:

        log(
            f"[BLE] write without response "
            f"failed for 0x{command:02X}: "
            f"{first_error}"
        )

        try:

            await client.write_gatt_char(
                BLE_CHAR,
                packet,
                response=True,
            )

            log(
                f"[BLE] command 0x{command:02X} "
                f"sent with response"
            )

        except Exception as second_error:

            log(
                f"[BLE] command "
                f"0x{command:02X} failed: "
                f"{second_error}"
            )


# ============================================================
# BLE WORKER
# ============================================================

async def ble_worker():

    while not STOP_EVENT.is_set():

        rx_buffer = bytearray()

        def notification_handler(
            sender,
            data,
        ):

            nonlocal rx_buffer

            try:

                chunk = bytes(data)

            except Exception:

                return

            # JK sometimes sends:
            #
            # AT\r\n
            #
            # Ignore it.

            if chunk == b"AT\r\n":
                return

            rx_buffer.extend(
                chunk
            )

            frames = extract_frames(
                rx_buffer
            )

            for frame in frames:

                try:

                    parse_runtime(
                        frame
                    )

                except Exception as error:

                    log(
                        "[BLE] runtime parser "
                        f"error: {error}"
                    )

        try:

            log(
                f"[BLE] connecting to "
                f"{BMS_MAC}"
            )

            async with BleakClient(
                BMS_MAC,
                timeout=BLE_CONNECT_TIMEOUT,
            ) as client:

                log(
                    "[BLE] connected"
                )

                with DATA_LOCK:
                    DATA.connected = True

                # ------------------------------------------------
                # Start notification
                # ------------------------------------------------

                await client.start_notify(
                    BLE_CHAR,
                    notification_handler,
                )

                log(
                    f"[BLE] notifications enabled "
                    f"on {BLE_CHAR}"
                )

                # ------------------------------------------------
                # Initial requests
                # ------------------------------------------------

                await request_bms(
                    client,
                    0x96,
                )

                await asyncio.sleep(
                    1
                )

                await request_bms(
                    client,
                    0x97,
                )

                # ------------------------------------------------
                # Keep connection alive
                # ------------------------------------------------

                last_request = time.monotonic()

                while (
                    not STOP_EVENT.is_set()
                    and client.is_connected
                ):

                    await asyncio.sleep(
                        1
                    )

                    now = (
                        time.monotonic()
                    )

                    if (
                        now - last_request
                        >= BMS_REQUEST_INTERVAL
                    ):

                        await request_bms(
                            client,
                            0x96,
                        )

                        await asyncio.sleep(
                            1
                        )

                        await request_bms(
                            client,
                            0x97,
                        )

                        last_request = now

        except Exception as error:

            log(
                f"[BLE] error: {error}"
            )

        finally:

            with DATA_LOCK:
                DATA.connected = False

            log(
                "[BLE] disconnected"
            )

        if not STOP_EVENT.is_set():

            log(
                f"[BLE] reconnecting in "
                f"{BLE_RECONNECT_DELAY}s"
            )

            # Event-aware sleep.
            for _ in range(
                BLE_RECONNECT_DELAY
            ):

                if STOP_EVENT.is_set():
                    break

                await asyncio.sleep(
                    1
                )


def ble_thread_main():

    try:

        asyncio.run(
            ble_worker()
        )

    except Exception as error:

        log(
            f"[BLE] worker stopped: "
            f"{error}"
        )


# ============================================================
# SNMP OID DEFINITION
# ============================================================

SNMPEntry = Dict[str, object]

OID_MAP: Dict[
    Tuple[int, ...],
    SNMPEntry
] = {}


def register_oid(
    suffix: Tuple[int, ...],
    value_type: str,
    getter: Callable[
        [BMSData],
        object
    ],
):

    full_oid = (
        SNMP_ROOT
        + tuple(suffix)
    )

    OID_MAP[full_oid] = {
        "type": value_type,
        "getter": getter,
    }


# ============================================================
# BASIC BMS METRICS
# ============================================================

register_oid(
    (1,),
    "Gauge32",
    lambda d:
        round(d.voltage * 1000),
)

register_oid(
    (2,),
    "Integer32",
    lambda d:
        round(d.current * 1000),
)

register_oid(
    (3,),
    "Integer32",
    lambda d:
        round(d.power),
)

register_oid(
    (4,),
    "Gauge32",
    lambda d:
        d.soc,
)

register_oid(
    (5,),
    "Integer32",
    lambda d:
        round(d.temp * 10),
)

register_oid(
    (6,),
    "Integer32",
    lambda d:
        round(d.mos_temp * 10),
)

register_oid(
    (7,),
    "Counter32",
    lambda d:
        max(0, d.cycles),
)

register_oid(
    (8,),
    "Gauge32",
    lambda d:
        round(d.capacity * 1000),
)

register_oid(
    (9,),
    "Integer32",
    lambda d:
        d.cell_count,
)

register_oid(
    (10,),
    "Gauge32",
    lambda d:
        round(d.min_cell * 1000),
)

register_oid(
    (11,),
    "Gauge32",
    lambda d:
        round(d.max_cell * 1000),
)

register_oid(
    (12,),
    "Gauge32",
    lambda d:
        round(d.delta_cell * 1000),
)

register_oid(
    (13,),
    "Integer32",
    lambda d:
        round(d.balance_current * 1000),
)

register_oid(
    (14,),
    "Integer32",
    lambda d:
        1 if d.balancing else 0,
)

register_oid(
    (15,),
    "Integer32",
    lambda d:
        1 if d.charge_enabled else 0,
)

register_oid(
    (16,),
    "Integer32",
    lambda d:
        1 if d.discharge_enabled else 0,
)

register_oid(
    (17,),
    "Gauge32",
    lambda d:
        round(
            d.remaining_capacity * 1000
        ),
)

register_oid(
    (18,),
    "Integer32",
    lambda d:
        1
        if (
            d.connected
            and (
                time.time()
                - d.last_update
                < DATA_TIMEOUT
            )
        )
        else 0,
)


# ============================================================
# DEVICE INFORMATION
# ============================================================

register_oid(
    (100,),
    "STRING",
    lambda d:
        d.vendor,
)

register_oid(
    (101,),
    "STRING",
    lambda d:
        d.hardware,
)

register_oid(
    (102,),
    "STRING",
    lambda d:
        d.software,
)

register_oid(
    (103,),
    "STRING",
    lambda d:
        d.serial,
)

register_oid(
    (104,),
    "STRING",
    lambda d:
        d.device_name,
)


# ============================================================
# CELL VOLTAGES
# ============================================================

for cell_index in range(1, 33):

    register_oid(
        (20, cell_index),
        "Gauge32",
        lambda d, index=cell_index - 1:
            round(
                d.cells[index] * 1000
            ),
    )


# ============================================================
# SORT OIDS
# ============================================================

SORTED_OIDS = sorted(
    OID_MAP.keys()
)


# ============================================================
# OID HELPERS
# ============================================================

def parse_oid(
    text: str,
) -> Optional[Tuple[int, ...]]:

    text = text.strip()

    if text.startswith("."):
        text = text[1:]

    if not text:
        return ()

    try:

        return tuple(
            int(part)
            for part in text.split(".")
            if part
        )

    except ValueError:

        return None


def oid_to_string(
    value: Tuple[int, ...],
) -> str:

    return (
        "."
        + ".".join(
            str(x)
            for x in value
        )
    )


# ============================================================
# GET VALUE
# ============================================================

def get_entry_value(
    entry: SNMPEntry,
):

    getter = entry["getter"]

    with DATA_LOCK:

        value = getter(DATA)

    return value


# ============================================================
# PASS_PERSIST GET
# ============================================================

def handle_get(
    request_oid: Tuple[int, ...],
):

    entry = OID_MAP.get(
        request_oid
    )

    if entry is None:

        print(
            "NONE",
            flush=True,
        )

        return

    value_type = entry["type"]

    value = get_entry_value(
        entry
    )

    print(
        str(value_type),
        flush=True,
    )

    print(
        str(value),
        flush=True,
    )


# ============================================================
# PASS_PERSIST GETNEXT
# ============================================================

def find_next_oid(
    request_oid: Tuple[int, ...],
):

    for candidate in SORTED_OIDS:

        if candidate > request_oid:

            return (
                candidate,
                OID_MAP[candidate],
            )

    return None


def handle_getnext(
    request_oid: Tuple[int, ...],
):

    result = find_next_oid(
        request_oid
    )

    if result is None:

        print(
            "NONE",
            flush=True,
        )

        return

    next_oid, entry = result

    value_type = entry["type"]

    value = get_entry_value(
        entry
    )

    print(
        oid_to_string(
            next_oid
        ),
        flush=True,
    )

    print(
        str(value_type),
        flush=True,
    )

    print(
        str(value),
        flush=True,
    )


# ============================================================
# PASS_PERSIST SET
# ============================================================

def handle_set():

    """
    JK-BMS SNMP implementation is read-only.

    We consume the SET request and report
    not-writable.
    """

    print(
        "not-writable",
        flush=True,
    )


# ============================================================
# PASS_PERSIST MAIN LOOP
# ============================================================

def snmp_loop():

    """
    Net-SNMP pass_persist protocol.

    IMPORTANT:

    Do NOT print anything to stdout except
    valid pass_persist responses.
    """

    log(
        "[SNMP] pass_persist backend started"
    )

    while not STOP_EVENT.is_set():

        line = sys.stdin.readline()

        if not line:

            log(
                "[SNMP] stdin closed"
            )

            break

        command = line.strip().lower()

        # ----------------------------------------------------
        # PING
        # ----------------------------------------------------

        if command == "ping":

            print(
                "PONG",
                flush=True,
            )

            continue

        # ----------------------------------------------------
        # GET
        # ----------------------------------------------------

        if command == "get":

            oid_line = (
                sys.stdin.readline()
            )

            if not oid_line:
                break

            request_oid = parse_oid(
                oid_line
            )

            if request_oid is None:

                print(
                    "NONE",
                    flush=True,
                )

            else:

                handle_get(
                    request_oid
                )

            continue

        # ----------------------------------------------------
        # GETNEXT
        # ----------------------------------------------------

        if command == "getnext":

            oid_line = (
                sys.stdin.readline()
            )

            if not oid_line:
                break

            request_oid = parse_oid(
                oid_line
            )

            if request_oid is None:

                print(
                    "NONE",
                    flush=True,
                )

            else:

                handle_getnext(
                    request_oid
                )

            continue

        # ----------------------------------------------------
        # SET
        # ----------------------------------------------------

        if command == "set":

            handle_set()

            continue

        # ----------------------------------------------------
        # UNKNOWN
        # ----------------------------------------------------

        log(
            f"[SNMP] unknown command: "
            f"{command!r}"
        )

        print(
            "NONE",
            flush=True,
        )


# ============================================================
# SIGNAL HANDLING
# ============================================================

def signal_handler(
    signum,
    frame,
):

    log(
        f"[SYSTEM] received signal "
        f"{signum}"
    )

    STOP_EVENT.set()


signal.signal(
    signal.SIGTERM,
    signal_handler,
)

signal.signal(
    signal.SIGINT,
    signal_handler,
)


# ============================================================
# MAIN
# ============================================================

def main():

    log(
        "[SYSTEM] JK-BMS pass_persist starting"
    )

    log(
        f"[SYSTEM] BMS MAC: {BMS_MAC}"
    )

    log(
        "[SYSTEM] SNMP root: "
        + oid_to_string(SNMP_ROOT)
    )

    # --------------------------------------------------------
    # Start BLE worker
    # --------------------------------------------------------

    ble_thread = threading.Thread(
        target=ble_thread_main,
        name="JK-BMS-BLE",
        daemon=True,
    )

    ble_thread.start()

    # --------------------------------------------------------
    # Run pass_persist protocol
    # --------------------------------------------------------

    try:

        snmp_loop()

    except Exception as error:

        log(
            f"[SNMP] fatal error: {error}"
        )

    finally:

        STOP_EVENT.set()

        log(
            "[SYSTEM] stopping BLE worker"
        )

        ble_thread.join(
            timeout=5
        )

        log(
            "[SYSTEM] stopped"
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()
