from __future__ import annotations

import datetime
import os
import sqlite3
from typing import Any, Dict, List, Optional

from config import DB_PATH


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initializes the SQLite schema for patients, posture/fall logs, and sensor vitals."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            age INTEGER NOT NULL,
            gender TEXT NOT NULL,
            room_number TEXT NOT NULL,
            client_id TEXT NOT NULL UNIQUE,
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS posture_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            posture TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            timestamp TEXT NOT NULL,
            details TEXT DEFAULT '',
            FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vitals_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            heart_rate REAL,
            spo2 REAL,
            temperature_c REAL,
            temperature_f REAL,
            finger_detected INTEGER DEFAULT 1,
            status TEXT DEFAULT 'NORMAL',
            timestamp TEXT NOT NULL,
            FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS sensor_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT UNIQUE NOT NULL,
            client_id TEXT NOT NULL,
            ip_address TEXT,
            mac_address TEXT,
            firmware_version TEXT,
            sensors TEXT,
            last_seen TEXT NOT NULL,
            status TEXT DEFAULT 'ONLINE'
        )
        """
    )

    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_posture_logs_patient_id ON posture_logs(patient_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_posture_logs_timestamp ON posture_logs(timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vitals_logs_patient_id ON vitals_logs(patient_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vitals_logs_timestamp ON vitals_logs(timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_sensor_devices_client_id ON sensor_devices(client_id)"
    )

    conn.commit()
    conn.close()


def create_patient(
    name: str,
    age: int,
    gender: str,
    room_number: str,
    client_id: str,
    notes: str = "",
) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
    cursor.execute(
        """
        INSERT INTO patients (name, age, gender, room_number, client_id, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (name.strip(), age, gender.strip(), room_number.strip(), client_id.strip(), notes.strip(), now_str),
    )
    patient_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return patient_id


def get_all_patients() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients ORDER BY id ASC")
    rows = cursor.fetchall()
    patients = [dict(row) for row in rows]
    conn.close()
    return patients


def get_patient_by_id(patient_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE id = ?", (patient_id,))
    row = cursor.fetchone()
    patient = dict(row) if row else None
    conn.close()
    return patient


def get_patient_by_client_id(client_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM patients WHERE client_id = ?", (client_id.strip(),))
    row = cursor.fetchone()
    patient = dict(row) if row else None
    conn.close()
    return patient


def log_posture_event(
    patient_id: int,
    event_type: str,
    posture: str,
    duration_seconds: float,
    details: str = "",
) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cursor.execute(
        """
        INSERT INTO posture_logs (patient_id, event_type, posture, duration_seconds, timestamp, details)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (patient_id, event_type, posture, round(duration_seconds, 2), now_str, details),
    )
    conn.commit()
    conn.close()


def get_patient_logs(patient_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM posture_logs 
        WHERE patient_id = ? 
        ORDER BY id DESC 
        LIMIT ?
        """,
        (patient_id, limit),
    )
    rows = cursor.fetchall()
    logs = [dict(row) for row in rows]
    conn.close()
    return logs


# --- ESP32 Device Announcement & Vitals Management ---

def register_or_update_device(
    device_id: str,
    client_id: str,
    ip_address: str = "",
    mac_address: str = "",
    firmware_version: str = "1.0.0",
    sensors: str = "MAX30102,DS18B20",
) -> Dict[str, Any]:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    cursor.execute(
        """
        INSERT INTO sensor_devices (device_id, client_id, ip_address, mac_address, firmware_version, sensors, last_seen, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'ONLINE')
        ON CONFLICT(device_id) DO UPDATE SET
            client_id = excluded.client_id,
            ip_address = excluded.ip_address,
            mac_address = excluded.mac_address,
            firmware_version = excluded.firmware_version,
            sensors = excluded.sensors,
            last_seen = excluded.last_seen,
            status = 'ONLINE'
        """,
        (device_id.strip(), client_id.strip(), ip_address, mac_address, firmware_version, sensors, now_str),
    )
    conn.commit()

    cursor.execute("SELECT * FROM sensor_devices WHERE device_id = ?", (device_id.strip(),))
    row = cursor.fetchone()
    device = dict(row) if row else {}
    conn.close()
    return device


def get_all_sensor_devices() -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sensor_devices ORDER BY last_seen DESC")
    rows = cursor.fetchall()
    devices = [dict(row) for row in rows]
    conn.close()
    return devices


def log_vitals_record(
    patient_id: int,
    client_id: str,
    heart_rate: Optional[float],
    spo2: Optional[float],
    temperature_c: Optional[float],
    temperature_f: Optional[float],
    finger_detected: bool = True,
    status: str = "NORMAL",
) -> Optional[int]:
    conn = get_db_connection()
    cursor = conn.cursor()
    now_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    hr_val = round(heart_rate, 1) if heart_rate is not None else None
    spo2_val = round(spo2, 1) if spo2 is not None else None
    temp_c_val = round(temperature_c, 2) if temperature_c is not None else None
    temp_f_val = round(temperature_f, 2) if temperature_f is not None else None

    cursor.execute(
        """
        INSERT INTO vitals_logs (patient_id, client_id, heart_rate, spo2, temperature_c, temperature_f, finger_detected, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (patient_id, client_id, hr_val, spo2_val, temp_c_val, temp_f_val, 1 if finger_detected else 0, status, now_str),
    )
    conn.commit()
    inserted_id = cursor.lastrowid
    conn.close()
    return inserted_id


def get_patient_vitals_logs(patient_id: int, limit: int = 40) -> List[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM vitals_logs 
        WHERE patient_id = ? 
        ORDER BY id DESC 
        LIMIT ?
        """,
        (patient_id, limit),
    )
    rows = cursor.fetchall()
    logs = [dict(row) for row in rows]
    conn.close()
    return logs


def get_latest_patient_vitals(patient_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM vitals_logs 
        WHERE patient_id = ? 
        ORDER BY id DESC 
        LIMIT 1
        """,
        (patient_id,),
    )
    row = cursor.fetchone()
    vitals = dict(row) if row else None
    conn.close()
    return vitals
