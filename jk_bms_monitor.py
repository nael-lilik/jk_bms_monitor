#!/usr/bin/env python3

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from bleak import BleakClient


# ============================================================
# CONFIGURATION
# ============================================================

BMS_MAC = "28:D4:1E:8D:71:2C"

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

FRAME_HEADER = bytes([0x55, 0xAA, 0xEB, 0x90])

FRAME_SIZE = 300

CMD_SETTINGS = 0x96
CMD_DEVICE_INFO = 0x97

REFRESH_INTERVAL = 1.0


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

    voltage: Optional[float] = None
    current: Optional[float] = None
    power: Optional[float] = None
    soc: Optional[float] = None

    temperature: Optional[float] = None
    mos_temperature: Optional[float] = None

    cycles: Optional[float] = None
    capacity: Optional[float] = None

    cells: list[float] = field(default_factory=list)

    min_cell: Optional[float] = None
    max_cell: Optional[float] = None
    delta_cell: Optional[float] = None

    charge_enabled: Optional[bool] = None
    discharge_enabled: Optional[bool] = None
    balancing: Optional[bool] = None

    updated: bool = False


# ============================================================
# GLOBAL STATE
# ============================================================

bms = BMSData()

rx_buffer = bytearray()

connected = False
running = True


# ============================================================
# UTILITIES
# ============================================================

def u16(data: bytes, offset: int) -> int:
    return int.from_bytes(
        data[offset:offset + 2],
        byteorder="little",
        signed=False
    )


def i16(data: bytes, offset: int) -> int:
    return int.from_bytes(
        data[offset:offset + 2],
        byteorder="little",
        signed=True
    )


def u32(data: bytes, offset: int) -> int:
    return int.from_bytes(
        data[offset:offset + 4],
        byteorder="little",
        signed=False
    )


def i32(data: bytes, offset: int) -> int:
    return int.from_bytes(
        data[offset:offset + 4],
        byteorder="little",
        signed=True
    )


def text_field(data: bytes, offset: int, length: int) -> str:
    raw = data[offset:offset + length]

    raw = raw.split(b"\x00", 1)[0]

    try:
        return raw.decode("ascii", errors="ignore").strip()
    except Exception:
        return ""


def crc8(data: bytes) -> int:
    return sum(data) & 0xFF


def build_command(command: int, counter: int = 0) -> bytes:
    """
    JK BLE command:

    AA 55 90 EB
    CMD
    LEN
    DATA x 10
    COUNTER
    RESERVED x 2
    CRC
    """

    frame = bytearray(20)

    frame[0:4] = bytes([0xAA, 0x55, 0x90, 0xEB])
    frame[4] = command
    frame[5] = 0x00

    # bytes 6..15 = 0

    frame[16] = counter & 0xFF

    frame[17] = 0x00
    frame[18] = 0x00

    frame[19] = crc8(frame[:19])

    return bytes(frame)


# ============================================================
# TERMINAL
# ============================================================

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def fmt(value, fmt_string="-"):

    if value is None:
        return "-"

    return format(value, fmt_string)


def render():
    """
    Render full BMS dashboard.
    """

    clear_screen()

    width = 58

    def line(text=""):
        print("║ " + text.ljust(width - 2)[:width - 2] + " ║")

    print("╔" + "═" * width + "╗")

    title = "JK BMS MONITOR"
    print("║" + title.center(width) + "║")

    info = (
        f"{bms.vendor} | "
        f"HW {bms.hardware} | "
        f"SW {bms.software} | "
        f"{bms.cell_count or len(bms.cells)}S"
    )

    print("║" + info.center(width)[:width] + "║")

    print("╠" + "═" * width + "╣")

    line(f"Voltage       : {fmt(bms.voltage, '.2f')} V")
    line(f"Current       : {fmt(bms.current, '.2f')} A")
    line(f"Power         : {fmt(bms.power, '.0f')} W")
    line(f"SOC           : {fmt(bms.soc, '.0f')} %")
    line(f"Temperature   : {fmt(bms.temperature, '.1f')} °C")
    line(f"MOS Temp      : {fmt(bms.mos_temperature, '.1f')} °C")
    line(f"Cycles        : {fmt(bms.cycles, '.0f')}")
    line(f"Capacity      : {fmt(bms.capacity, '.1f')} Ah")

    print("╠" + "═" * width + "╣")

    line("CELL VOLTAGE")

    print(
        "╠══════╦══════════╦══════════╦══════════════════════════════╣"
    )
    print(
        "║ Cell ║ Voltage  ║ Diff     ║                              ║"
    )
    print(
        "╠══════╬══════════╬══════════╬══════════════════════════════╣"
    )

    if bms.cells:

        min_v = min(bms.cells)
        max_v = max(bms.cells)

        for index, voltage in enumerate(bms.cells, start=1):

            diff_mv = (voltage - min_v) * 1000

            diff_text = f"{diff_mv:+.0f} mV"

            print(
                f"║ {index:02d}   "
                f"║ {voltage:>7.3f} V "
                f"║ {diff_text:>8} "
                f"║                              ║"
            )

    else:

        print(
            "║ --   ║    ---   ║    ---   ║                              ║"
        )

    print(
        "╠══════╩══════════╩══════════╩══════════════════════════════╣"
    )

    if bms.cells:

        min_v = min(bms.cells)
        max_v = max(bms.cells)
        delta = max_v - min_v

        summary = (
            f"MIN {min_v:.3f} V │ "
            f"MAX {max_v:.3f} V │ "
            f"DELTA {delta:.3f} V"
        )

        print("║ " + summary.center(width - 2) + " ║")

    else:

        print("║" + "Waiting for cell data...".center(width) + "║")

    print("╚" + "═" * width + "╝")

    print()
    print(f"BLE : {BMS_MAC}")
    print("CTRL+C untuk keluar")


# ============================================================
# FRAME PARSER
# ============================================================

def parse_frame(frame: bytes):

    if len(frame) < FRAME_SIZE:
        return

    if frame[0:4] != FRAME_HEADER:
        return

    # Validate checksum
    expected_crc = frame[299]
    calculated_crc = crc8(frame[:299])

    if expected_crc != calculated_crc:
        print(
            f"CRC ERROR: expected {expected_crc:02X}, "
            f"calculated {calculated_crc:02X}"
        )
        return

    frame_type = frame[4]

    if frame_type == 0x01:
        parse_settings(frame)

    elif frame_type == 0x02:
        parse_cell_info(frame)

    elif frame_type == 0x03:
        parse_device_info(frame)


# ============================================================
# DEVICE INFO
# ============================================================

def parse_device_info(frame: bytes):

    bms.vendor = text_field(frame, 6, 16) or bms.vendor

    bms.hardware = text_field(frame, 22, 8) or bms.hardware

    bms.software = text_field(frame, 30, 8) or bms.software

    bms.device_name = text_field(frame, 46, 16) or bms.device_name

    bms.serial = text_field(frame, 86, 16) or bms.serial


# ============================================================
# SETTINGS
# ============================================================

def parse_settings(frame: bytes):

    # JK02 settings
    #
    # 114 = Cell count
    # 118 = Charge switch
    # 122 = Discharge switch
    # 130 = Nominal capacity, 0.001 Ah

    try:

        cell_count = frame[114]

        if 1 <= cell_count <= 32:
            bms.cell_count = cell_count

        bms.charge_enabled = bool(frame[118])

        bms.discharge_enabled = bool(frame[122])

        capacity = u32(frame, 130) * 0.001

        if capacity > 0:
            bms.capacity = capacity

    except Exception as e:

        print(f"Settings parse error: {e}")


# ============================================================
# CELL / RUNTIME INFO
# ============================================================

def parse_cell_info(frame: bytes):

    try:

        # ====================================================
        # JK02_32S FRAME
        #
        # Cell voltage:
        #   6-69
        #
        # Enabled cells:
        #   70-73
        #
        # MOS temperature:
        #   144-145
        #
        # Battery voltage:
        #   150-153
        #
        # Battery power:
        #   154-157
        #
        # Current:
        #   158-161
        #
        # Temperature 1:
        #   162-163
        #
        # Temperature 2:
        #   164-165
        #
        # Balance current:
        #   170-171
        #
        # Balance action:
        #   172
        #
        # SOC:
        #   173
        #
        # Remaining capacity:
        #   174-177
        #
        # Nominal capacity:
        #   178-181
        #
        # Cycles:
        #   182-185
        # ====================================================

        # ----------------------------------------------------
        # CELL COUNT
        # ----------------------------------------------------

        cell_count = bms.cell_count

        if not cell_count:
            enabled_mask = u32(frame, 70)

            cell_count = enabled_mask.bit_count()

        if cell_count <= 0 or cell_count > 32:
            cell_count = 15

        bms.cell_count = cell_count

        # ----------------------------------------------------
        # CELL VOLTAGES
        # ----------------------------------------------------

        cells = []

        for i in range(cell_count):

            offset = 6 + (i * 2)

            voltage = u16(frame, offset) * 0.001

            if voltage > 0:
                cells.append(voltage)

        if cells:

            bms.cells = cells

            bms.min_cell = min(cells)
            bms.max_cell = max(cells)
            bms.delta_cell = (
                bms.max_cell -
                bms.min_cell
            )

        # ----------------------------------------------------
        # MOS TEMPERATURE
        # ----------------------------------------------------

        bms.mos_temperature = (
            i16(frame, 144) * 0.1
        )

        # ----------------------------------------------------
        # TOTAL BATTERY VOLTAGE
        # ----------------------------------------------------

        bms.voltage = (
            u32(frame, 150) * 0.001
        )

        # ----------------------------------------------------
        # CURRENT
        # ----------------------------------------------------

        bms.current = (
            i32(frame, 158) * 0.001
        )

        # ----------------------------------------------------
        # POWER
        #
        # JK current is signed.
        # Positive/negative direction depends on BMS.
        # ----------------------------------------------------

        bms.power = (
            bms.voltage *
            bms.current
        )

        # ----------------------------------------------------
        # TEMPERATURE SENSOR 1
        # ----------------------------------------------------

        bms.temperature = (
            i16(frame, 162) * 0.1
        )

        # ----------------------------------------------------
        # TEMPERATURE SENSOR 2
        # ----------------------------------------------------
        #
        # Bisa dipakai jika nanti ingin menampilkan
        # sensor temperatur kedua.
        # ----------------------------------------------------

        temperature_2 = (
            i16(frame, 164) * 0.1
        )

        # ----------------------------------------------------
        # BALANCE CURRENT
        # ----------------------------------------------------

        balance_current = (
            i16(frame, 170) * 0.001
        )

        # ----------------------------------------------------
        # BALANCING STATUS
        #
        # 0 = off
        # 1 = charging balancer
        # 2 = discharging balancer
        # ----------------------------------------------------

        balance_action = frame[172]

        bms.balancing = (
            balance_action != 0
        )

        # ----------------------------------------------------
        # SOC
        # ----------------------------------------------------

        bms.soc = frame[173]

        # ----------------------------------------------------
        # REMAINING CAPACITY
        # ----------------------------------------------------

        remaining_capacity = (
            u32(frame, 174) * 0.001
        )

        # ----------------------------------------------------
        # NOMINAL CAPACITY
        # ----------------------------------------------------

        nominal_capacity = (
            u32(frame, 178) * 0.001
        )

        if nominal_capacity > 0:

            bms.capacity = (
                nominal_capacity
            )

        elif remaining_capacity > 0:

            bms.capacity = (
                remaining_capacity
            )

        # ----------------------------------------------------
        # CYCLE COUNT
        # ----------------------------------------------------

        bms.cycles = (
            u32(frame, 182)
        )

        bms.updated = True

    except Exception as e:

        print(
            f"Cell info parse error: {e}"
        )

# ============================================================
# BLE FRAME ASSEMBLER
# ============================================================

def feed_notification(data: bytes):

    global rx_buffer

    # --------------------------------------------------------
    # Ignore AT\r\n noise from BLE module
    # --------------------------------------------------------

    if data == b"AT\r\n":
        return

    rx_buffer.extend(data)

    while True:

        # ----------------------------------------------------
        # Find JK response header
        # ----------------------------------------------------

        index = rx_buffer.find(FRAME_HEADER)

        if index < 0:

            # Keep last 3 bytes because header is 4 bytes.
            if len(rx_buffer) > 3:
                rx_buffer = rx_buffer[-3:]

            return

        # Drop anything before header
        if index > 0:
            del rx_buffer[:index]

        # Need full 300-byte frame
        if len(rx_buffer) < FRAME_SIZE:
            return

        # Extract one frame
        frame = bytes(rx_buffer[:FRAME_SIZE])

        # Remove frame from buffer
        del rx_buffer[:FRAME_SIZE]

        parse_frame(frame)


# ============================================================
# BLE NOTIFICATION CALLBACK
# ============================================================

def notification_handler(sender, data):

    feed_notification(bytes(data))


# ============================================================
# SEND COMMAND
# ============================================================

async def send_command(client: BleakClient, command: int):

    packet = build_command(command)

    await client.write_gatt_char(
        CHAR_UUID,
        packet,
        response=False
    )


# ============================================================
# CONNECTION LOOP
# ============================================================

async def monitor():

    global connected

    while running:

        try:

            print()
            print("=" * 60)
            print("Connecting to JK BMS...")
            print(BMS_MAC)
            print("=" * 60)

            async with BleakClient(
                BMS_MAC,
                timeout=15.0
            ) as client:

                connected = True

                print("Connected.")

                # ------------------------------------------------
                # Start notification first
                # ------------------------------------------------

                await client.start_notify(
                    CHAR_UUID,
                    notification_handler
                )

                print("Notifications enabled.")

                await asyncio.sleep(0.5)

                # ------------------------------------------------
                # Request settings
                # ------------------------------------------------

                print("Requesting settings...")

                await send_command(
                    client,
                    CMD_SETTINGS
                )

                await asyncio.sleep(1.0)

                # ------------------------------------------------
                # Request device info
                # ------------------------------------------------

                print("Requesting device info...")

                await send_command(
                    client,
                    CMD_DEVICE_INFO
                )

                await asyncio.sleep(2.0)

                # ------------------------------------------------
                # BMS automatically streams 0x02 after
                # 0x96 + 0x97.
                # ------------------------------------------------

                print("Receiving live data...")

                while running and client.is_connected:

                    render()

                    await asyncio.sleep(
                        REFRESH_INTERVAL
                    )

        except asyncio.CancelledError:

            break

        except Exception as e:

            connected = False

            print()
            print(f"BLE error: {e}")
            print("Reconnecting in 5 seconds...")

            await asyncio.sleep(5)

        finally:

            connected = False


# ============================================================
# SIGNAL HANDLER
# ============================================================

def stop_handler(signum, frame):

    global running

    running = False

    print()
    print()
    print("Stopping JK BMS monitor...")


# ============================================================
# MAIN
# ============================================================

async def main():

    signal.signal(
        signal.SIGINT,
        stop_handler
    )

    signal.signal(
        signal.SIGTERM,
        stop_handler
    )

    render()

    await monitor()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        pass

    finally:

        print()
        print("JK BMS Monitor stopped.")
