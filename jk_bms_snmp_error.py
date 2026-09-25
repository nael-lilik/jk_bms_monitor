#!/usr/bin/env python3

import asyncio
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakClient

# ============================================================
# PySNMP
# ============================================================

from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.smi import instrum
from pysnmp.proto import rfc1902


# ============================================================
# CONFIGURATION
# ============================================================

BMS_MAC = os.getenv(
    "BMS_MAC",
    "28:D4:1E:8D:71:2C"
)

BLE_CHAR_UUID = os.getenv(
    "BLE_CHAR_UUID",
    "0000ffe1-0000-1000-8000-00805f9b34fb"
)

SNMP_HOST = os.getenv(
    "SNMP_HOST",
    "0.0.0.0"
)

SNMP_PORT = int(
    os.getenv("SNMP_PORT", "1161")
)

SNMP_COMMUNITY = os.getenv(
    "SNMP_COMMUNITY",
    "public"
)

# Private enterprise OID
SNMP_ROOT = (1, 3, 6, 1, 4, 1, 55555, 1)

REFRESH_INTERVAL = 1.0

FRAME_HEADER = bytes([
    0x55,
    0xAA,
    0xEB,
    0x90
])

FRAME_SIZE = 300

CMD_SETTINGS = 0x96
CMD_DEVICE_INFO = 0x97


# ============================================================
# BMS DATA
# ============================================================

@dataclass
class BMSData:

    vendor: str = "JK-BMS"
    hardware: str = "-"
    software: str = "-"

    serial: str = "-"
    device_name: str = "-"

    cell_count: int = 0

    voltage: float = 0.0
    current: float = 0.0
    power: float = 0.0

    soc: float = 0.0

    temperature: float = 0.0
    mos_temperature: float = 0.0

    cycles: int = 0
    capacity: float = 0.0
    remaining_capacity: float = 0.0

    cells: list[float] = field(
        default_factory=list
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


bms = BMSData()


# ============================================================
# HELPERS
# ============================================================

def u16(data, offset):
    return int.from_bytes(
        data[offset:offset + 2],
        "little",
        signed=False
    )


def i16(data, offset):
    return int.from_bytes(
        data[offset:offset + 2],
        "little",
        signed=True
    )


def u32(data, offset):
    return int.from_bytes(
        data[offset:offset + 4],
        "little",
        signed=False
    )


def i32(data, offset):
    return int.from_bytes(
        data[offset:offset + 4],
        "little",
        signed=True
    )


def text_field(data, offset, length):

    raw = data[
        offset:offset + length
    ]

    raw = raw.split(
        b"\x00",
        1
    )[0]

    return raw.decode(
        "ascii",
        errors="ignore"
    ).strip()


def crc8(data):

    return sum(data) & 0xFF


# ============================================================
# JK COMMAND
# ============================================================

def build_command(command):

    frame = bytearray(20)

    frame[0:4] = bytes([
        0xAA,
        0x55,
        0x90,
        0xEB
    ])

    frame[4] = command
    frame[5] = 0

    frame[16] = 0
    frame[17] = 0
    frame[18] = 0

    frame[19] = crc8(
        frame[:19]
    )

    return bytes(frame)


# ============================================================
# FRAME PARSER
# ============================================================

def parse_device_info(frame):

    bms.vendor = (
        text_field(frame, 6, 16)
        or bms.vendor
    )

    bms.hardware = (
        text_field(frame, 22, 8)
        or bms.hardware
    )

    bms.software = (
        text_field(frame, 30, 8)
        or bms.software
    )

    bms.device_name = (
        text_field(frame, 46, 16)
        or bms.device_name
    )

    bms.serial = (
        text_field(frame, 86, 16)
        or bms.serial
    )


def parse_settings(frame):

    try:

        cells = frame[114]

        if 1 <= cells <= 32:
            bms.cell_count = cells

        bms.charge_enabled = bool(
            frame[118]
        )

        bms.discharge_enabled = bool(
            frame[122]
        )

        capacity = (
            u32(frame, 130)
            * 0.001
        )

        if capacity > 0:
            bms.capacity = capacity

    except Exception as e:

        print(
            "Settings parse error:",
            e
        )


def parse_cell_info(frame):

    try:

        # ====================================================
        # CELL COUNT
        # ====================================================

        cell_count = bms.cell_count

        if not cell_count:

            enabled_mask = u32(
                frame,
                70
            )

            cell_count = (
                enabled_mask.bit_count()
            )

        if (
            cell_count <= 0
            or cell_count > 32
        ):
            cell_count = 15

        bms.cell_count = cell_count

        # ====================================================
        # CELL VOLTAGE
        # ====================================================

        cells = []

        for i in range(cell_count):

            offset = 6 + (
                i * 2
            )

            voltage = (
                u16(
                    frame,
                    offset
                ) * 0.001
            )

            if voltage > 0:
                cells.append(
                    voltage
                )

        if cells:

            bms.cells = cells

            bms.min_cell = min(
                cells
            )

            bms.max_cell = max(
                cells
            )

            bms.delta_cell = (
                bms.max_cell
                - bms.min_cell
            )

        # ====================================================
        # MOS TEMPERATURE
        # ====================================================

        bms.mos_temperature = (
            i16(frame, 144)
            * 0.1
        )

        # ====================================================
        # BATTERY VOLTAGE
        # ====================================================

        bms.voltage = (
            u32(frame, 150)
            * 0.001
        )

        # ====================================================
        # CURRENT
        # ====================================================

        bms.current = (
            i32(frame, 158)
            * 0.001
        )

        # ====================================================
        # POWER
        # ====================================================

        bms.power = (
            bms.voltage
            * bms.current
        )

        # ====================================================
        # TEMPERATURE
        # ====================================================

        bms.temperature = (
            i16(frame, 162)
            * 0.1
        )

        # ====================================================
        # BALANCE CURRENT
        # ====================================================

        bms.balance_current = (
            i16(frame, 170)
            * 0.001
        )

        # ====================================================
        # BALANCE STATUS
        # ====================================================

        balance_action = frame[172]

        bms.balancing = (
            balance_action != 0
        )

        # ====================================================
        # SOC
        # ====================================================

        bms.soc = frame[173]

        # ====================================================
        # REMAINING CAPACITY
        # ====================================================

        bms.remaining_capacity = (
            u32(frame, 174)
            * 0.001
        )

        # ====================================================
        # NOMINAL CAPACITY
        # ====================================================

        nominal_capacity = (
            u32(frame, 178)
            * 0.001
        )

        if nominal_capacity > 0:

            bms.capacity = (
                nominal_capacity
            )

        # ====================================================
        # CYCLES
        # ====================================================

        bms.cycles = u32(
            frame,
            182
        )

        bms.last_update = time.time()

    except Exception as e:

        print(
            "Cell info parse error:",
            e
        )


def parse_frame(frame):

    if len(frame) != FRAME_SIZE:
        return

    if frame[:4] != FRAME_HEADER:
        return

    expected = frame[299]

    calculated = crc8(
        frame[:299]
    )

    if expected != calculated:
        return

    frame_type = frame[4]

    if frame_type == 0x01:

        parse_settings(frame)

    elif frame_type == 0x02:

        parse_cell_info(frame)

    elif frame_type == 0x03:

        parse_device_info(frame)


# ============================================================
# BLE BUFFER
# ============================================================

rx_buffer = bytearray()


def feed_notification(data):

    global rx_buffer

    # JK BLE module noise
    if data == b"AT\r\n":
        return

    rx_buffer.extend(data)

    while True:

        pos = rx_buffer.find(
            FRAME_HEADER
        )

        if pos < 0:

            if len(rx_buffer) > 3:

                rx_buffer = (
                    rx_buffer[-3:]
                )

            return

        if pos > 0:

            del rx_buffer[:pos]

        if len(rx_buffer) < FRAME_SIZE:
            return

        frame = bytes(
            rx_buffer[:FRAME_SIZE]
        )

        del rx_buffer[
            :FRAME_SIZE
        ]

        parse_frame(frame)


def notification_handler(
    sender,
    data
):

    feed_notification(
        bytes(data)
    )


# ============================================================
# BLE
# ============================================================

async def send_command(
    client,
    command
):

    packet = build_command(
        command
    )

    await client.write_gatt_char(
        BLE_CHAR_UUID,
        packet,
        response=False
    )


async def ble_monitor():

    while True:

        try:

            print(
                f"\nConnecting JK BMS "
                f"{BMS_MAC}..."
            )

            async with BleakClient(
                BMS_MAC,
                timeout=15
            ) as client:

                bms.connected = True

                print(
                    "JK BMS connected."
                )

                await client.start_notify(
                    BLE_CHAR_UUID,
                    notification_handler
                )

                await asyncio.sleep(
                    0.5
                )

                await send_command(
                    client,
                    CMD_SETTINGS
                )

                await asyncio.sleep(
                    1
                )

                await send_command(
                    client,
                    CMD_DEVICE_INFO
                )

                await asyncio.sleep(
                    2
                )

                while client.is_connected:

                    await asyncio.sleep(
                        1
                    )

        except asyncio.CancelledError:

            break

        except Exception as e:

            print(
                "BLE error:",
                e
            )

        finally:

            bms.connected = False

        print(
            "BLE disconnected."
        )

        await asyncio.sleep(
            5
        )


# ============================================================
# CLI
# ============================================================

def render():

    os.system(
        "cls"
        if os.name == "nt"
        else "clear"
    )

    width = 58

    def line(text=""):

        print(
            "║ "
            + text.ljust(
                width - 2
            )[:width - 2]
            + " ║"
        )

    print(
        "╔"
        + "═" * width
        + "╗"
    )

    print(
        "║"
        + "JK BMS MONITOR".center(width)
        + "║"
    )

    info = (
        f"{bms.vendor} | "
        f"HW {bms.hardware} | "
        f"SW {bms.software} | "
        f"{bms.cell_count}S"
    )

    print(
        "║"
        + info.center(width)[:width]
        + "║"
    )

    print(
        "╠"
        + "═" * width
        + "╣"
    )

    line(
        f"Voltage       : "
        f"{bms.voltage:.2f} V"
    )

    line(
        f"Current       : "
        f"{bms.current:.2f} A"
    )

    line(
        f"Power         : "
        f"{bms.power:.0f} W"
    )

    line(
        f"SOC           : "
        f"{bms.soc:.0f} %"
    )

    line(
        f"Temperature   : "
        f"{bms.temperature:.1f} °C"
    )

    line(
        f"MOS Temp      : "
        f"{bms.mos_temperature:.1f} °C"
    )

    line(
        f"Cycles        : "
        f"{bms.cycles}"
    )

    line(
        f"Capacity      : "
        f"{bms.capacity:.1f} Ah"
    )

    print(
        "╠"
        + "═" * width
        + "╣"
    )

    line(
        "CELL VOLTAGE"
    )

    print(
        "╠══════╦══════════╦══════════╦══════════════════════════════╣"
    )

    print(
        "║ Cell ║ Voltage  ║ Diff     ║                              ║"
    )

    print(
        "╠══════╬══════════╬══════════╬══════════════════════════════╣"
    )

    for i, voltage in enumerate(
        bms.cells,
        start=1
    ):

        diff = (
            voltage
            - bms.min_cell
        ) * 1000

        print(
            f"║ {i:02d}   "
            f"║ {voltage:7.3f} V "
            f"║ {diff:+6.0f} mV "
            f"║                              ║"
        )

    print(
        "╠══════╩══════════╩══════════╩══════════════════════════════╣"
    )

    summary = (
        f"MIN {bms.min_cell:.3f} V │ "
        f"MAX {bms.max_cell:.3f} V │ "
        f"DELTA {bms.delta_cell:.3f} V"
    )

    print(
        "║"
        + summary.center(width)
        + "║"
    )

    print(
        "╚"
        + "═" * width
        + "╝"
    )

    print(
        f"\nBLE : {BMS_MAC}"
    )

    print(
        f"SNMP: {SNMP_HOST}:{SNMP_PORT}"
    )


async def cli_monitor():

    while True:

        render()

        await asyncio.sleep(
            REFRESH_INTERVAL
        )


# ============================================================
# SNMP DATA
# ============================================================

def oid(*parts):

    return tuple(
        SNMP_ROOT + tuple(parts)
    )


def snmp_value(
    value,
    value_type="integer"
):

    if value_type == "string":

        return rfc1902.OctetString(
            str(value)
        )

    if value_type == "counter":

        return rfc1902.Counter32(
            int(value)
        )

    if value_type == "unsigned":

        return rfc1902.Gauge32(
            int(value)
        )

    return rfc1902.Integer32(
        int(value)
    )


# ============================================================
# CUSTOM SNMP INSTRUMENTATION
# ============================================================

class JKBSMMibController(
    instrum.AbstractMibInstrumController
):

    def __init__(self):

        self.objects = {}

        self.build_objects()

    def build_objects(self):

        # ----------------------------------------------------
        # General information
        # ----------------------------------------------------

        self.objects[
            oid(1)
        ] = lambda: snmp_value(
            bms.voltage * 1000
        )

        self.objects[
            oid(2)
        ] = lambda: snmp_value(
            bms.current * 1000
        )

        self.objects[
            oid(3)
        ] = lambda: snmp_value(
            bms.power
        )

        self.objects[
            oid(4)
        ] = lambda: snmp_value(
            bms.soc
        )

        self.objects[
            oid(5)
        ] = lambda: snmp_value(
            bms.temperature * 10
        )

        self.objects[
            oid(6)
        ] = lambda: snmp_value(
            bms.mos_temperature * 10
        )

        self.objects[
            oid(7)
        ] = lambda: snmp_value(
            bms.cycles
        )

        self.objects[
            oid(8)
        ] = lambda: snmp_value(
            bms.capacity * 1000
        )

        self.objects[
            oid(9)
        ] = lambda: snmp_value(
            bms.cell_count
        )

        self.objects[
            oid(10)
        ] = lambda: snmp_value(
            bms.min_cell * 1000
        )

        self.objects[
            oid(11)
        ] = lambda: snmp_value(
            bms.max_cell * 1000
        )

        self.objects[
            oid(12)
        ] = lambda: snmp_value(
            bms.delta_cell * 1000
        )

        self.objects[
            oid(13)
        ] = lambda: snmp_value(
            bms.balance_current * 1000
        )

        self.objects[
            oid(14)
        ] = lambda: snmp_value(
            int(bms.balancing)
        )

        self.objects[
            oid(15)
        ] = lambda: snmp_value(
            int(bms.charge_enabled)
        )

        self.objects[
            oid(16)
        ] = lambda: snmp_value(
            int(bms.discharge_enabled)
        )

        self.objects[
            oid(17)
        ] = lambda: snmp_value(
            bms.remaining_capacity * 1000
        )

        self.objects[
            oid(18)
        ] = lambda: snmp_value(
            int(bms.connected)
        )

        # ----------------------------------------------------
        # Device information
        # ----------------------------------------------------

        self.objects[
            oid(100)
        ] = lambda: snmp_value(
            bms.vendor,
            "string"
        )

        self.objects[
            oid(101)
        ] = lambda: snmp_value(
            bms.hardware,
            "string"
        )

        self.objects[
            oid(102)
        ] = lambda: snmp_value(
            bms.software,
            "string"
        )

        self.objects[
            oid(103)
        ] = lambda: snmp_value(
            bms.serial,
            "string"
        )

        self.objects[
            oid(104)
        ] = lambda: snmp_value(
            bms.device_name,
            "string"
        )

        # ----------------------------------------------------
        # Cell voltage
        #
        # .20.1 = Cell 1
        # .20.2 = Cell 2
        # ...
        # .20.32 = Cell 32
        # ----------------------------------------------------

        for i in range(1, 33):

            def cell_value(
                index=i
            ):

                if (
                    index <= len(
                        bms.cells
                    )
                ):

                    return snmp_value(
                        bms.cells[
                            index - 1
                        ] * 1000
                    )

                return snmp_value(0)

            self.objects[
                oid(20, i)
            ] = cell_value

    # ========================================================
    # GET
    # ========================================================

    def read_variables(
        self,
        *var_binds,
        **context
    ):

        result = []

        for name, value in var_binds:

            requested = tuple(
                int(x)
                for x in name
            )

            getter = self.objects.get(
                requested
            )

            if getter is None:

                result.append(
                    (
                        name,
                        rfc1902.NoSuchInstance()
                    )
                )

            else:

                result.append(
                    (
                        name,
                        getter()
                    )
                )

        return result

    # ========================================================
    # GETNEXT / WALK
    # ========================================================

    def read_next_variables(
        self,
        *var_binds,
        **context
    ):

        sorted_oids = sorted(
            self.objects.keys()
        )

        result = []

        for name, value in var_binds:

            requested = tuple(
                int(x)
                for x in name
            )

            next_oid = None

            for candidate in sorted_oids:

                if candidate > requested:

                    next_oid = candidate
                    break

            if next_oid is None:

                result.append(
                    (
                        name,
                        rfc1902.EndOfMibView()
                    )
                )

            else:

                result.append(
                    (
                        next_oid,
                        self.objects[
                            next_oid
                        ]()
                    )
                )

        return result

    def write_variables(
        self,
        *var_binds,
        **context
    ):

        raise Exception(
            "JK BMS SNMP is read-only"
        )


# ============================================================
# SNMP AGENT
# ============================================================

def start_snmp():

    snmp_engine = engine.SnmpEngine()

    # --------------------------------------------------------
    # UDP/161
    # --------------------------------------------------------

    config.add_transport(
        snmp_engine,
        udp.DOMAIN_NAME,
        udp.UdpTransport().open_server_mode(
            (
                SNMP_HOST,
                SNMP_PORT
            )
        )
    )

    # --------------------------------------------------------
    # SNMP v2c
    # --------------------------------------------------------

    config.add_v1_system(
        snmp_engine,
        "jk-bms",
        SNMP_COMMUNITY
    )

    # --------------------------------------------------------
    # VACM
    # --------------------------------------------------------

    config.add_vacm_user(
        snmp_engine,
        2,
        "jk-bms",
        "noAuthNoPriv",
        SNMP_ROOT,
        SNMP_ROOT
    )

    # --------------------------------------------------------
    # Context
    # --------------------------------------------------------

    snmp_context = context.SnmpContext(
        snmp_engine
    )

    mib_controller = JKBSMMibController()

    snmp_context.register_context_name(
        b"",
        mib_controller
    )

    # --------------------------------------------------------
    # Responders
    # --------------------------------------------------------

    cmdrsp.GetCommandResponder(
        snmp_engine,
        snmp_context
    )

    cmdrsp.NextCommandResponder(
        snmp_engine,
        snmp_context
    )

    cmdrsp.BulkCommandResponder(
        snmp_engine,
        snmp_context
    )

    # --------------------------------------------------------
    # Dispatcher
    # --------------------------------------------------------

    snmp_engine.transport_dispatcher.job_started(
        1
    )

    print(
        f"SNMP listening on "
        f"{SNMP_HOST}:{SNMP_PORT}"
    )

    return snmp_engine


async def snmp_monitor():

    snmp_engine = start_snmp()

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    finally:

        try:
            snmp_engine.close_dispatcher()
        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

running = True


def signal_handler(
    signum,
    frame
):

    global running

    running = False

    print(
        "\nStopping..."
    )


async def main():

    signal.signal(
        signal.SIGINT,
        signal_handler
    )

    signal.signal(
        signal.SIGTERM,
        signal_handler
    )

    # --------------------------------------------------------
    # Run BLE, CLI and SNMP concurrently
    # --------------------------------------------------------

    await asyncio.gather(
        ble_monitor(),
        cli_monitor(),
        snmp_monitor()
    )


if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        pass

    print(
        "JK BMS SNMP stopped."
    )
