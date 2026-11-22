"""
Database module for Manipur Voter Search Application.
Uses SQLite with FTS5 (Full-Text Search) for high performance and zero-cost hosting.
"""

import sqlite3
import os
from typing import List, Dict, Any, Optional

DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voters.db")

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Constituencies table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS constituencies (
        ac_no INTEGER PRIMARY KEY,
        ac_name TEXT NOT NULL,
        district TEXT NOT NULL,
        total_voters INTEGER DEFAULT 0,
        male_voters INTEGER DEFAULT 0,
        female_voters INTEGER DEFAULT 0,
        third_gender_voters INTEGER DEFAULT 0,
        polling_stations_count INTEGER DEFAULT 0
    )
    """)

    # Voters table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS voters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        epic_no TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        relative_name TEXT,
        relation_type TEXT DEFAULT 'Father',
        house_no TEXT,
        age INTEGER,
        gender TEXT,
        ac_no INTEGER NOT NULL,
        ac_name TEXT NOT NULL,
        part_no INTEGER DEFAULT 1,
        polling_station_name TEXT,
        section_name TEXT,
        FOREIGN KEY (ac_no) REFERENCES constituencies (ac_no)
    )
    """)

    # Indexes for rapid filtering
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_voters_name ON voters(name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_voters_epic ON voters(epic_no)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_voters_ac_no ON voters(ac_no)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_voters_relative ON voters(relative_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_voters_household ON voters(ac_no, part_no, house_no, age)")

    # FTS5 Virtual Table for full-text search across name, relative_name, epic_no, ac_name
    cursor.execute("""
    CREATE VIRTUAL TABLE IF NOT EXISTS voters_fts USING fts5(
        name,
        relative_name,
        epic_no,
        ac_name,
        polling_station_name,
        content='voters',
        content_rowid='id'
    )
    """)

    # Triggers to keep FTS index synced with voters table
    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS voters_ai AFTER INSERT ON voters BEGIN
        INSERT INTO voters_fts(rowid, name, relative_name, epic_no, ac_name, polling_station_name)
        VALUES (new.id, new.name, new.relative_name, new.epic_no, new.ac_name, new.polling_station_name);
    END;
    """)

    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS voters_ad AFTER DELETE ON voters BEGIN
        INSERT INTO voters_fts(voters_fts, rowid, name, relative_name, epic_no, ac_name, polling_station_name)
        VALUES ('delete', old.id, old.name, old.relative_name, old.epic_no, old.ac_name, old.polling_station_name);
    END;
    """)

    cursor.execute("""
    CREATE TRIGGER IF NOT EXISTS voters_au AFTER UPDATE ON voters BEGIN
        INSERT INTO voters_fts(voters_fts, rowid, name, relative_name, epic_no, ac_name, polling_station_name)
        VALUES ('delete', old.id, old.name, old.relative_name, old.epic_no, old.ac_name, old.polling_station_name);
        INSERT INTO voters_fts(rowid, name, relative_name, epic_no, ac_name, polling_station_name)
        VALUES (new.id, new.name, new.relative_name, new.epic_no, new.ac_name, new.polling_station_name);
    END;
    """)

    # Household and Polling Station Geo tables
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS polling_station_geo (
        ac_no INTEGER NOT NULL,
        part_no INTEGER NOT NULL,
        station_name TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        source TEXT DEFAULT 'OSM_OVERPASS',
        PRIMARY KEY (ac_no, part_no)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS household_geo (
        ac_no INTEGER NOT NULL,
        part_no INTEGER NOT NULL,
        house_no TEXT NOT NULL,
        normalized_house_no TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        confidence TEXT DEFAULT 'ALGORITHMIC_BUILDING_PARCEL',
        building_footprint_geojson TEXT,
        PRIMARY KEY (ac_no, part_no, normalized_house_no)
    )
    """)

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_household_geo_lookup ON household_geo(ac_no, part_no, normalized_house_no)")

    conn.commit()
    conn.close()

def update_constituency_stats():
    """Recalculate summary statistics for all constituencies."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE constituencies
    SET 
        total_voters = (SELECT COUNT(*) FROM voters WHERE voters.ac_no = constituencies.ac_no),
        male_voters = (SELECT COUNT(*) FROM voters WHERE voters.ac_no = constituencies.ac_no AND gender = 'M'),
        female_voters = (SELECT COUNT(*) FROM voters WHERE voters.ac_no = constituencies.ac_no AND gender = 'F'),
        third_gender_voters = (SELECT COUNT(*) FROM voters WHERE voters.ac_no = constituencies.ac_no AND gender = 'O'),
        polling_stations_count = (SELECT COUNT(DISTINCT part_no) FROM voters WHERE voters.ac_no = constituencies.ac_no)
    """)
    conn.commit()
    conn.close()
