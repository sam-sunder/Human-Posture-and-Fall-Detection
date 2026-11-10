#!/usr/bin/env python3
"""
ESP32 Firmware Uploader & Vitals Telemetry Utility
Antigravity Healthcare Telemetry Subsystem

Features:
  1. Detect connected serial ports and ESP32 boards.
  2. Flash compiled firmware binaries to ESP32 using esptool.
  3. Compile and flash using arduino-cli if installed.
  4. Live interactive Serial Monitor for debugging sensor logs.
  5. Simulated ESP32 telemetry transmitter for cloud/local UI testing.

Usage:
  python upload_esp32.py --ports
  python upload_esp32.py --flash firmware.bin --port /dev/ttyUSB0
  python upload_esp32.py --monitor --port /dev/ttyUSB0
  python upload_esp32.py --simulate --client-id bed_01
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from typing import List, Optional, Tuple

try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False

try:
    import urllib.request
    import urllib.error
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False


def print_banner() -> None:
    print("=" * 64)
    print("      ESP32 Clinical Vitals Firmware & Telemetry Tool       ")
    print("         MAX30102 (HR/SpO2) + DS18B20 (Body Temp)           ")
    print("=" * 64)


def list_serial_ports() -> List[Tuple[str, str, str]]:
    """Scan and list all accessible serial/COM ports."""
    if not HAS_SERIAL:
        print("[!] Warning: 'pyserial' is not installed. Install via: pip install pyserial")
        return []

    ports = serial.tools.list_ports.comports()
    results = []
    print("\nScanning connected serial ports...")
    if not ports:
        print("  No serial devices found. (Ensure ESP32 USB cable is securely connected)")
        return []

    print(f"  Found {len(ports)} connected port(s):")
    for p in ports:
        dev = p.device
        desc = p.description or "Generic USB Serial"
        hwid = p.hwid or ""
        is_esp = any(k in (desc + hwid).lower() for k in ["cp210", "ch340", "ftdi", "uart", "esp32", "silicon"])
        tag = "[ESP32 Candidate]" if is_esp else ""
        print(f"   * {dev:<18} : {desc} {tag}")
        results.append((dev, desc, hwid))

    return results


def auto_detect_esp32_port() -> Optional[str]:
    """Find the most likely ESP32 port automatically."""
    ports = list_serial_ports()
    if not ports:
        return None

    # Priority to CP210x, CH340, USB-UART
    for dev, desc, hwid in ports:
        s = (desc + hwid).lower()
        if "cp210" in s or "ch340" in s or "esp32" in s or "silicon labs" in s:
            return dev

    return ports[0][0]


def run_serial_monitor(port: str, baud_rate: int = 115200) -> None:
    """Connect to serial port and print incoming logs in real time."""
    if not HAS_SERIAL:
        print("[!] Error: pyserial is required for the serial monitor.")
        return

    print(f"\n[Monitor] Opening {port} at {baud_rate} baud...")
    print("[Monitor] Press Ctrl+C to disconnect from serial monitor.\n")
    try:
        with serial.Serial(port, baud_rate, timeout=1.0) as ser:
            time.sleep(0.1)
            ser.reset_input_buffer()
            while True:
                line = ser.readline().decode("utf-8", errors="replace")
                if line:
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"[{timestamp}] {line.rstrip()}")
    except KeyboardInterrupt:
        print("\n[Monitor] Disconnected gracefully.")
    except Exception as e:
        print(f"\n[Monitor] Port communication error: {e}")


def flash_binary(
    binary_path: str,
    port: Optional[str] = None,
    baud: int = 460800,
    chip: str = "esp32",
    address: str = "0x10000",
    erase_first: bool = False,
) -> bool:
    """Flash a compiled binary to the ESP32 using esptool."""
    if not os.path.isfile(binary_path):
        print(f"[!] Error: Binary file '{binary_path}' does not exist.")
        return False

    if not port:
        port = auto_detect_esp32_port()
        if not port:
            print("[!] Error: No serial port specified and no ESP32 could be auto-detected.")
            print("    Please connect your board or specify --port /dev/ttyUSB0")
            return False

    print(f"\n[Flasher] Target chip:   {chip}")
    print(f"[Flasher] Serial port:   {port}")
    print(f"[Flasher] Baud rate:     {baud}")
    print(f"[Flasher] Firmware bin:  {binary_path} at offset {address}")

    if erase_first:
        print("[Flasher] Erasing entire flash before upload...")
        erase_cmd = [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            chip,
            "--port",
            port,
            "--baud",
            str(baud),
            "erase_flash",
        ]
        ret = subprocess.run(erase_cmd)
        if ret.returncode != 0:
            print("[!] Flash erase failed.")
            return False

    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(baud),
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        "write_flash",
        "-z",
        address,
        binary_path,
    ]

    print(f"[Flasher] Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print("\n[✓] Firmware successfully uploaded to ESP32!")
        return True
    else:
        print(f"\n[!] Firmware upload failed with exit code {result.returncode}")
        return False


def compile_and_upload_arduino(
    sketch_dir: str,
    port: Optional[str] = None,
    fqbn: str = "esp32:esp32:esp32",
) -> bool:
    """Compile and upload using arduino-cli if installed."""
    cli_found = subprocess.run(["which", "arduino-cli"], capture_output=True).returncode == 0
    if not cli_found:
        print("\n[!] 'arduino-cli' is not installed in the system PATH.")
        print("    You can either:")
        print("    1. Compile and upload directly from the Arduino IDE GUI (see firmware/README.md).")
        print("    2. Export a compiled binary from Arduino IDE (Sketch -> Export Compiled Binary),")
        print("       then flash it with: python upload_esp32.py --flash <path_to_bin>")
        print("    3. Install arduino-cli automatically via: curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh")
        return False

    if not port:
        port = auto_detect_esp32_port()
        if not port:
            print("[!] No target serial port found. Specify --port.")
            return False

    print(f"\n[Arduino-CLI] Compiling sketch '{sketch_dir}' for FQBN '{fqbn}'...")
    comp_cmd = ["arduino-cli", "compile", "--fqbn", fqbn, sketch_dir]
    comp_res = subprocess.run(comp_cmd)
    if comp_res.returncode != 0:
        print("[!] Compilation failed.")
        return False

    print(f"\n[Arduino-CLI] Uploading to {port}...")
    up_cmd = ["arduino-cli", "upload", "-p", port, "--fqbn", fqbn, sketch_dir]
    up_res = subprocess.run(up_cmd)
    return up_res.returncode == 0


def simulate_esp32_node(
    server_url: str = "http://localhost:8000",
    client_id: str = "bed_01",
    device_id: str = "ESP32-VITALS-SIM01",
    interval: float = 1.5,
) -> None:
    """
    Simulate an ESP32 hardware node sending device announcements and
    real-time MAX30102 & DS18B20 telemetry to the FastAPI server.
    """
    server_url = server_url.rstrip("/")
    announce_url = f"{server_url}/api/devices/announce"
    vitals_url = f"{server_url}/api/vitals"

    print(f"\n[Simulator] Starting Simulated ESP32 Vitals Node:")
    print(f"  Target Server: {server_url}")
    print(f"  Device ID:     {device_id}")
    print(f"  Client ID:     {client_id}")
    print(f"  Push Cadence:  {interval}s")
    print("  Press Ctrl+C to terminate simulation.\n")

    # Step 1: Announce
    announce_payload = {
        "device_id": device_id,
        "client_id": client_id,
        "ip_address": "192.168.1.142",
        "mac_address": "24:0A:C4:58:91:DE",
        "firmware_version": "1.0.0-sim",
        "sensors": "MAX30102,DS18B20",
    }

    try:
        req = urllib.request.Request(
            announce_url,
            data=json.dumps(announce_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            resp_body = resp.read().decode("utf-8")
            print(f"[Simulator] Initial announcement successful: {resp_body}")
    except Exception as e:
        print(f"[!] Server announcement warning: {e} (Ensure FastAPI server is running on {server_url})")

    # Step 2: Telemetry loop
    hr = 72.0
    spo2 = 98.2
    temp_c = 36.65
    step = 0

    try:
        while True:
            step += 1
            # Natural physiological wander
            hr += random.uniform(-1.2, 1.2)
            hr = max(58.0, min(108.0, hr))

            spo2 += random.uniform(-0.25, 0.25)
            spo2 = max(94.0, min(100.0, spo2))

            temp_c += random.uniform(-0.04, 0.04)
            temp_c = max(36.1, min(37.4, temp_c))
            temp_f = (temp_c * 9.0 / 5.0) + 32.0

            finger_detected = True

            vitals_payload = {
                "device_id": device_id,
                "client_id": client_id,
                "heart_rate": round(hr, 1),
                "spo2": round(spo2, 1),
                "temperature_c": round(temp_c, 2),
                "temperature_f": round(temp_f, 2),
                "finger_detected": finger_detected,
            }

            try:
                req = urllib.request.Request(
                    vitals_url,
                    data=json.dumps(vitals_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=2.5) as r:
                    code = r.getcode()
                    t_str = time.strftime("%H:%M:%S")
                    print(
                        f"[{t_str}] Push #{step:<4} | HR: {round(hr, 1):>5.1f} BPM | SpO2: {round(spo2, 1):>4.1f}% | "
                        f"Temp: {round(temp_c, 2):>5.2f}°C ({round(temp_f, 2):>5.2f}°F) | Server HTTP {code}"
                    )
            except Exception as err:
                print(f"[{time.strftime('%H:%M:%S')}] Push failed: {err}")

            # Periodic re-announcement every 30 pushes (~45 seconds)
            if step % 30 == 0:
                try:
                    req = urllib.request.Request(
                        announce_url,
                        data=json.dumps(announce_payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    urllib.request.urlopen(req, timeout=2.0)
                    print(f"[{time.strftime('%H:%M:%S')}] Periodic device announce refreshed.")
                except Exception:
                    pass

            time.sleep(interval)

    except KeyboardInterrupt:
        print("\n[Simulator] Simulation stopped by user.")


def main() -> None:
    print_banner()

    parser = argparse.ArgumentParser(
        description="ESP32 Clinical Vitals Firmware Uploader and Telemetry Utility."
    )
    parser.add_argument("--ports", "--scan", action="store_true", help="Scan and list connected serial ports.")
    parser.add_argument("--port", "-p", type=str, default=None, help="Serial port to use (e.g. /dev/ttyUSB0 or COM3).")
    parser.add_argument("--baud", "-b", type=int, default=115200, help="Serial monitor baud rate (default: 115200).")
    parser.add_argument("--flash", type=str, default=None, help="Path to compiled firmware .bin file to flash via esptool.")
    parser.add_argument("--erase", action="store_true", help="Erase ESP32 flash memory before writing binary.")
    parser.add_argument("--address", type=str, default="0x10000", help="Flash offset address for the binary (default: 0x10000).")
    parser.add_argument("--chip", type=str, default="esp32", help="Target chip type: esp32, esp32s2, esp32s3, esp32c3 (default: esp32).")
    parser.add_argument("--compile-upload", type=str, default=None, help="Path to sketch directory to compile and upload using arduino-cli.")
    parser.add_argument("--monitor", "-m", action="store_true", help="Start live Serial Monitor on the specified or detected port.")
    parser.add_argument("--simulate", action="store_true", help="Simulate ESP32 device announcement and streaming telemetry without hardware.")
    parser.add_argument("--server", type=str, default="http://localhost:8000", help="FastAPI Server URL for simulation (default: http://localhost:8000).")
    parser.add_argument("--client-id", type=str, default="bed_01", help="Client ID for simulation telemetry (default: bed_01).")

    args = parser.parse_args()

    if args.ports:
        list_serial_ports()
        return

    if args.flash:
        flash_binary(
            binary_path=args.flash,
            port=args.port,
            baud=460800,
            chip=args.chip,
            address=args.address,
            erase_first=args.erase,
        )
        return

    if args.compile_upload:
        compile_and_upload_arduino(
            sketch_dir=args.compile_upload,
            port=args.port,
        )
        return

    if args.monitor:
        port = args.port or auto_detect_esp32_port()
        if not port:
            print("[!] Could not auto-detect a serial port. Please specify with --port <device>")
            sys.exit(1)
        run_serial_monitor(port=port, baud_rate=args.baud)
        return

    if args.simulate:
        simulate_esp32_node(
            server_url=args.server,
            client_id=args.client_id,
        )
        return

    # If no flags passed, show help and scan ports
    parser.print_help()
    print()
    list_serial_ports()


if __name__ == "__main__":
    main()
