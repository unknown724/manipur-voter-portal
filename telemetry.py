"""
Telemetry, IP Audit Logging & Self-Learning Feedback Engine for Manipur Voter Portal.
Uses a separate lightweight SQLite database (telemetry.db) — 100% free, zero external dependencies.
"""

import csv
import io
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

TELEMETRY_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry.db")


def get_telemetry_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(TELEMETRY_DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_telemetry_db() -> None:
    conn = get_telemetry_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            client_ip TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            query_text TEXT,
            ac_no INTEGER,
            gender TEXT,
            result_count INTEGER,
            response_time_ms REAL,
            user_agent TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ip ON audit_logs(client_ip, timestamp DESC)")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS search_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            client_ip TEXT NOT NULL,
            query_text TEXT,
            clicked_voter_id INTEGER,
            clicked_name TEXT,
            action_type TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS learned_synonyms (
            wrong_term TEXT PRIMARY KEY,
            canonical_term TEXT NOT NULL,
            confidence INTEGER DEFAULT 1,
            updated_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS kinship_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            client_ip TEXT NOT NULL,
            target_id INTEGER NOT NULL,
            relative_id INTEGER,
            relationship_type TEXT,
            verdict TEXT NOT NULL,
            note TEXT
        )
        """)
        conn.commit()
    finally:
        conn.close()


def extract_client_ip(request: Any) -> str:
    """Extract real client IP behind Hugging Face / Cloudflare reverse proxies."""
    headers = request.headers
    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    for hdr in ("cf-connecting-ip", "x-real-ip"):
        val = headers.get(hdr)
        if val:
            return val.strip()
    return request.client.host if request.client else "unknown"


def log_api_request(
    client_ip: str,
    endpoint: str,
    query_text: Optional[str] = None,
    ac_no: Optional[int] = None,
    gender: Optional[str] = None,
    result_count: Optional[int] = None,
    response_time_ms: Optional[float] = None,
    user_agent: Optional[str] = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_q = (query_text or "").strip()[:200] or None
    conn = get_telemetry_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_logs (
                timestamp, client_ip, endpoint, query_text, ac_no, gender,
                result_count, response_time_ms, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                client_ip,
                endpoint,
                clean_q,
                ac_no,
                gender,
                result_count,
                round(response_time_ms, 2) if response_time_ms is not None else None,
                (user_agent or "")[:250],
            ),
        )

        # Self-learning reformulation mining:
        # If this search returned results (>0), check if the same IP had a 0-result search just before.
        if endpoint == "/api/search" and clean_q and (result_count or 0) > 0:
            cur.execute(
                """
                SELECT query_text FROM audit_logs
                WHERE client_ip = ? AND endpoint = '/api/search' AND result_count = 0
                  AND query_text IS NOT NULL AND id < last_insert_rowid()
                ORDER BY id DESC LIMIT 1
                """,
                (client_ip,),
            )
            prev = cur.fetchone()
            if prev and prev["query_text"]:
                prev_q = prev["query_text"].strip().lower()
                curr_q = clean_q.lower()
                if prev_q != curr_q and 3 <= len(prev_q) <= 40 and 3 <= len(curr_q) <= 40:
                    # Only learn plausible phonetic/typo reformulations (first char or length similarity)
                    if prev_q[0] == curr_q[0] or abs(len(prev_q) - len(curr_q)) <= 3:
                        cur.execute(
                            """
                            INSERT INTO learned_synonyms (wrong_term, canonical_term, confidence, updated_at)
                            VALUES (?, ?, 1, ?)
                            ON CONFLICT(wrong_term) DO UPDATE SET
                                canonical_term = excluded.canonical_term,
                                confidence = learned_synonyms.confidence + 1,
                                updated_at = excluded.updated_at
                            """,
                            (prev_q, clean_q, now_iso),
                        )

        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def get_learned_synonym(query_str: str) -> Optional[str]:
    if not query_str or not query_str.strip():
        return None
    key = query_str.strip().lower()
    conn = get_telemetry_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT canonical_term FROM learned_synonyms WHERE wrong_term = ? AND confidence >= 2",
            (key,),
        )
        row = cur.fetchone()
        return row["canonical_term"] if row else None
    except Exception:
        return None
    finally:
        conn.close()


def record_click_feedback(
    client_ip: str,
    query_text: Optional[str],
    voter_id: Optional[int],
    voter_name: Optional[str],
    action_type: str,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_telemetry_conn()
    try:
        conn.execute(
            """
            INSERT INTO search_feedback (timestamp, client_ip, query_text, clicked_voter_id, clicked_name, action_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (now_iso, client_ip, (query_text or "")[:200], voter_id, (voter_name or "")[:150], action_type[:50]),
        )
        conn.commit()
    finally:
        conn.close()


def record_kinship_feedback(
    client_ip: str,
    target_id: int,
    relative_id: Optional[int],
    relationship_type: Optional[str],
    verdict: str,
    note: Optional[str] = None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_telemetry_conn()
    try:
        conn.execute(
            """
            INSERT INTO kinship_feedback (timestamp, client_ip, target_id, relative_id, relationship_type, verdict, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (now_iso, client_ip, target_id, relative_id, relationship_type, verdict[:20], (note or "")[:300]),
        )
        conn.commit()
    finally:
        conn.close()


def get_telemetry_summary(limit: int = 100) -> Dict[str, Any]:
    conn = get_telemetry_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM audit_logs")
        total_requests = cur.fetchone()[0]

        cur.execute("SELECT COUNT(DISTINCT client_ip) FROM audit_logs")
        unique_ips = cur.fetchone()[0]

        cur.execute(
            """
            SELECT query_text, COUNT(*) as cnt
            FROM audit_logs
            WHERE query_text IS NOT NULL AND query_text != ''
            GROUP BY LOWER(query_text)
            ORDER BY cnt DESC
            LIMIT 12
            """
        )
        top_queries = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT query_text, COUNT(*) as cnt
            FROM audit_logs
            WHERE endpoint = '/api/search' AND result_count = 0 AND query_text IS NOT NULL
            GROUP BY LOWER(query_text)
            ORDER BY cnt DESC
            LIMIT 10
            """
        )
        zero_result_queries = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT client_ip, COUNT(*) as hits, MAX(timestamp) as last_seen
            FROM audit_logs
            GROUP BY client_ip
            ORDER BY hits DESC
            LIMIT 15
            """
        )
        top_ips = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """
            SELECT id, timestamp, client_ip, endpoint, query_text, ac_no, result_count, response_time_ms, user_agent
            FROM audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        recent_logs = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT wrong_term, canonical_term, confidence, updated_at FROM learned_synonyms ORDER BY confidence DESC LIMIT 20")
        learned = [dict(r) for r in cur.fetchall()]

        cur.execute("SELECT COUNT(*) FROM kinship_feedback")
        kinship_feedback_count = cur.fetchone()[0]

        return {
            "total_requests": total_requests,
            "unique_ips": unique_ips,
            "top_queries": top_queries,
            "zero_result_queries": zero_result_queries,
            "top_ips": top_ips,
            "recent_logs": recent_logs,
            "learned_synonyms": learned,
            "kinship_feedback_count": kinship_feedback_count,
        }
    finally:
        conn.close()


def export_telemetry_csv() -> str:
    conn = get_telemetry_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, timestamp, client_ip, endpoint, query_text, ac_no, gender, result_count, response_time_ms, user_agent FROM audit_logs ORDER BY id DESC"
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Timestamp (UTC)", "Client IP", "Endpoint", "Query", "AC No", "Gender", "Result Count", "Latency (ms)", "User Agent"])
    for r in rows:
        writer.writerow(list(r))
    return output.getvalue()
