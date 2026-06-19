from __future__ import annotations

import aiosqlite

TABLES = [
    """
    CREATE TABLE IF NOT EXISTS devices (
        device_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        ip TEXT DEFAULT '',
        port INTEGER DEFAULT 0,
        status TEXT DEFAULT 'online',
        protocol TEXT DEFAULT 'bacnet'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id INTEGER NOT NULL,
        object_type TEXT NOT NULL,
        object_instance INTEGER NOT NULL,
        object_name TEXT NOT NULL,
        description TEXT DEFAULT '',
        present_value TEXT DEFAULT '0',
        units TEXT DEFAULT '',
        cov_increment REAL DEFAULT 0.0,
        FOREIGN KEY (device_id) REFERENCES devices(device_id),
        UNIQUE(device_id, object_type, object_instance)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS endpoints (
        id TEXT PRIMARY KEY,
        url TEXT NOT NULL,
        secret TEXT NOT NULL,
        enabled INTEGER DEFAULT 1,
        event_types TEXT DEFAULT '[]',
        created_at TEXT,
        last_delivery_at TEXT,
        failure_count INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        payload TEXT NOT NULL,
        delivered INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alarms (
        id TEXT PRIMARY KEY,
        device_id INTEGER NOT NULL,
        point_name TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL,
        raised_at TEXT NOT NULL,
        cleared_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS assets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        device_id INTEGER,
        make TEXT DEFAULT '',
        model TEXT DEFAULT '',
        serial TEXT DEFAULT '',
        install_date TEXT,
        criticality INTEGER DEFAULT 3,
        location TEXT DEFAULT '',
        parent_id TEXT,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        device_id INTEGER NOT NULL,
        points TEXT NOT NULL
    )
    """,
]


async def run_migrations(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        for table_sql in TABLES:
            await db.execute(table_sql)
        # Migrate existing database to add protocol column
        try:
            await db.execute("ALTER TABLE devices ADD COLUMN protocol TEXT DEFAULT 'bacnet'")
        except aiosqlite.OperationalError:
            pass  # Column already exists
        await db.commit()
