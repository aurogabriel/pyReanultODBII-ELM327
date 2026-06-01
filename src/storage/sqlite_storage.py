"""Storage. SRP: persiste TelemetrySample. SQLite WAL pra durabilidade.
ISP: interface IStorage só com métodos que importam ao caller."""

import csv
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Optional


@dataclass
class TelemetrySample:
    session_id: str
    pid: str
    name: str
    raw_response: str
    parsed_value: Optional[float]
    unit: str
    timestamp: float
    transport_delay_ms: float
    status: str


class IStorage(ABC):
    @abstractmethod
    def open_session(self, session_id: str, metadata: dict) -> None: ...

    @abstractmethod
    def write_sample(self, sample: TelemetrySample) -> None: ...

    @abstractmethod
    def write_batch(self, samples: Iterable[TelemetrySample]) -> None: ...

    @abstractmethod
    def export_csv(self, session_id: str, csv_path: str | Path) -> int: ...

    @abstractmethod
    def close(self) -> None: ...


class SqliteStorage(IStorage):
    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        started_at REAL NOT NULL,
        vin TEXT,
        protocol TEXT,
        metadata_json TEXT
    );

    CREATE TABLE IF NOT EXISTS samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        pid TEXT NOT NULL,
        name TEXT NOT NULL,
        raw_response TEXT,
        parsed_value REAL,
        unit TEXT,
        timestamp REAL NOT NULL,
        transport_delay_ms REAL,
        status TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );

    CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id);
    CREATE INDEX IF NOT EXISTS idx_samples_pid ON samples(pid);
    CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(timestamp);
    """

    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def open_session(self, session_id: str, metadata: dict) -> None:
        import json
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions(session_id, started_at, vin, protocol, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session_id,
                time.time(),
                metadata.get("vin"),
                metadata.get("protocol"),
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        self._conn.commit()

    def write_sample(self, sample: TelemetrySample) -> None:
        self._conn.execute(
            "INSERT INTO samples(session_id, pid, name, raw_response, parsed_value, "
            "unit, timestamp, transport_delay_ms, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                sample.session_id, sample.pid, sample.name, sample.raw_response,
                sample.parsed_value, sample.unit, sample.timestamp,
                sample.transport_delay_ms, sample.status,
            ),
        )

    def write_batch(self, samples: Iterable[TelemetrySample]) -> None:
        rows = [
            (
                s.session_id, s.pid, s.name, s.raw_response, s.parsed_value,
                s.unit, s.timestamp, s.transport_delay_ms, s.status,
            )
            for s in samples
        ]
        if not rows:
            return
        self._conn.executemany(
            "INSERT INTO samples(session_id, pid, name, raw_response, parsed_value, "
            "unit, timestamp, transport_delay_ms, status) VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self._conn.commit()

    def export_csv(self, session_id: str, csv_path: str | Path) -> int:
        cur = self._conn.execute(
            "SELECT pid, name, raw_response, parsed_value, unit, timestamp, "
            "transport_delay_ms, status FROM samples WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        )
        rows = cur.fetchall()
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "pid", "name", "raw_response", "parsed_value", "unit",
                "timestamp", "transport_delay_ms", "status",
            ])
            writer.writerows(rows)
        return len(rows)

    def session_stats(self, session_id: str) -> dict:
        cur = self._conn.execute(
            "SELECT COUNT(*), AVG(transport_delay_ms), "
            "SUM(CASE WHEN status='SUCCESS' THEN 1 ELSE 0 END) "
            "FROM samples WHERE session_id=?",
            (session_id,),
        )
        total, avg_delay, success = cur.fetchone()
        return {
            "total_samples": total or 0,
            "avg_delay_ms": avg_delay or 0.0,
            "success_count": success or 0,
            "reliability": (success / total) if total else 0.0,
        }

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass
