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
    """Initializes the SQLite schema for patients and posture/fall logs."""
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
        "CREATE INDEX IF NOT EXISTS idx_posture_logs_patient_id ON posture_logs(patient_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_posture_logs_timestamp ON posture_logs(timestamp)"
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
