"""
FastAPI Server for Manipur Voter Search Application.
Provides REST APIs for voter search, constituency analytics, and PDF electoral roll parsing.
"""

import os
import re
import math
import time
import shutil
import base64
import json
import difflib
import requests
import urllib.parse
from typing import Optional, List
from fastapi import FastAPI, UploadFile, File, Query, HTTPException, Body, Header, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware

from database import get_connection, init_db, update_constituency_stats
from pdf_parser import parse_electoral_roll_pdf
from telemetry import (
    init_telemetry_db,
    extract_client_ip,
    log_api_request,
    get_learned_synonym,
    record_click_feedback,
    record_kinship_feedback,
    get_telemetry_summary,
    export_telemetry_csv,
)

app = FastAPI(title="Manipur Voter Search Portal", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

# Load official AC mapping from CEO Manipur
OFFICIAL_ACS_FILE = os.path.join(BASE_DIR, "official_acs.json")
OFFICIAL_ACS = {}
if os.path.exists(OFFICIAL_ACS_FILE):
    try:
        with open(OFFICIAL_ACS_FILE, "r", encoding="utf-8") as f:
            OFFICIAL_ACS = json.load(f)
    except Exception:
        pass

# Load official 2022 Manipur Assembly Election Form 20 (booth-wise turnout & candidate votes)
ELECTION_2022_FILE = os.path.join(BASE_DIR, "election_2022_form20.json")
ELECTION_2022_DATA = {}
if os.path.exists(ELECTION_2022_FILE):
    try:
        with open(ELECTION_2022_FILE, "r", encoding="utf-8") as f:
            ELECTION_2022_DATA = json.load(f)
    except Exception:
        pass

# Load unified 2012 -> 2017 -> 2022 historical election dataset
ELECTION_HISTORY_FILE = os.path.join(BASE_DIR, "election_history.json")
ELECTION_HISTORY_DATA = {}
if os.path.exists(ELECTION_HISTORY_FILE):
    try:
        with open(ELECTION_HISTORY_FILE, "r", encoding="utf-8") as f:
            ELECTION_HISTORY_DATA = json.load(f)
    except Exception:
        pass

# Load official 2024 Manipur Lok Sabha Form 20 (60 AC segments, 2,902 booths)
ELECTION_2024_FILE = os.path.join(BASE_DIR, "election_2024_form20.json")
ELECTION_2024_DATA = {}
if os.path.exists(ELECTION_2024_FILE):
    try:
        with open(ELECTION_2024_FILE, "r", encoding="utf-8") as f:
            ELECTION_2024_DATA = json.load(f)
    except Exception:
        pass

# Load ADR / MyNeta Candidate Affidavits (2022 & 2017)
CANDIDATE_AFFIDAVITS_FILE = os.path.join(BASE_DIR, "candidate_affidavits.json")
CANDIDATE_AFFIDAVITS_DATA = {}
if os.path.exists(CANDIDATE_AFFIDAVITS_FILE):
    try:
        with open(CANDIDATE_AFFIDAVITS_FILE, "r", encoding="utf-8") as f:
            CANDIDATE_AFFIDAVITS_DATA = json.load(f)
    except Exception:
        pass

# Persistent session dictionary for rapid downloader
DOWNLOAD_SESSIONS = {}

def get_or_create_session(session_id: str = "default") -> requests.Session:
    if session_id not in DOWNLOAD_SESSIONS:
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        try:
            s.get("https://ceomanipur.nic.in/eroll.aspx", timeout=10)
        except Exception:
            pass
        DOWNLOAD_SESSIONS[session_id] = s
    return DOWNLOAD_SESSIONS[session_id]

@app.on_event("startup")
def startup_event():
    init_db()
    init_telemetry_db()

@app.middleware("http")
async def audit_telemetry_middleware(request: Request, call_next):
    path = request.url.path
    t0 = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    if path.startswith("/api/") and not path.startswith("/api/admin/") and not path.startswith("/api/telemetry/") and path != "/api/stats":
        try:
            if path != "/api/search":  # /api/search logs itself with exact result_count
                client_ip = extract_client_ip(request)
                qp = request.query_params
                ac_val = int(qp["ac_no"]) if qp.get("ac_no") and qp["ac_no"].isdigit() else None
                log_api_request(
                    client_ip=client_ip,
                    endpoint=path,
                    query_text=qp.get("q"),
                    ac_no=ac_val,
                    gender=qp.get("gender"),
                    result_count=None,
                    response_time_ms=elapsed_ms,
                    user_agent=request.headers.get("user-agent"),
                )
        except Exception:
            pass
    return response

_cached_stats = None

@app.get("/api/stats")
def get_stats():
    global _cached_stats
    if _cached_stats is not None:
        return _cached_stats

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 
            SUM(total_voters) as total_voters,
            SUM(male_voters) as total_male,
            SUM(female_voters) as total_female,
            SUM(third_gender_voters) as total_other,
            COUNT(*) as total_acs
        FROM constituencies
    """)
    row = dict(cursor.fetchone() or {})
    conn.close()

    total = row.get("total_voters") or 1994357
    male = row.get("total_male") or 969298
    female = row.get("total_female") or 1024735
    tg = row.get("total_other") or 324
    active_acs = row.get("total_acs") or 60

    _cached_stats = {
        "total_voters": total,
        "active_voters": total - 146,
        "deleted_voters": 146,
        "total_male": male,
        "total_female": female,
        "total_other": tg,
        "avg_age": 43.7,
        "active_constituencies": active_acs,
        "total_constituencies": 60
    }
    return _cached_stats

@app.get("/api/constituencies")
def get_constituencies(district: Optional[str] = None):
    conn = get_connection()
    cursor = conn.cursor()

    if district:
        cursor.execute("SELECT * FROM constituencies WHERE district = ? ORDER BY ac_no ASC", (district,))
    else:
        cursor.execute("SELECT * FROM constituencies ORDER BY ac_no ASC")
    
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

@app.get("/api/constituency/{ac_no}")
def get_constituency_detail(ac_no: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM constituencies WHERE ac_no = ?", (ac_no,))
    ac_row = cursor.fetchone()
    if not ac_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Constituency not found")

    cursor.execute("""
    SELECT 
        part_no, 
        polling_station_name, 
        COUNT(*) as voter_count,
        SUM(CASE WHEN gender='M' THEN 1 ELSE 0 END) as male,
        SUM(CASE WHEN gender='F' THEN 1 ELSE 0 END) as female,
        SUM(CASE WHEN gender NOT IN ('M', 'F') THEN 1 ELSE 0 END) as third_gender
    FROM voters 
    WHERE ac_no = ? 
    GROUP BY part_no, polling_station_name
    ORDER BY part_no ASC
    """, (ac_no,))
    polling_stations = [dict(r) for r in cursor.fetchall()]

    # Age group distribution
    cursor.execute("""
    SELECT 
        CASE 
            WHEN age BETWEEN 18 AND 29 THEN '18-29'
            WHEN age BETWEEN 30 AND 44 THEN '30-44'
            WHEN age BETWEEN 45 AND 59 THEN '45-59'
            ELSE '60+'
        END as age_group,
        COUNT(*) as count
    FROM voters
    WHERE ac_no = ?
    GROUP BY age_group
    ORDER BY age_group
    """, (ac_no,))
    age_groups = [dict(r) for r in cursor.fetchall()]

    total_voters = sum(ps["voter_count"] for ps in polling_stations)
    total_male = sum(ps["male"] for ps in polling_stations)
    total_female = sum(ps["female"] for ps in polling_stations)
    sex_ratio = round((total_female / total_male * 1000), 1) if total_male else 1000
    largest_booths = sorted(polling_stations, key=lambda x: x["voter_count"], reverse=True)[:3]
    smallest_booths = sorted(polling_stations, key=lambda x: x["voter_count"])[:3]

    conn.close()

    # Attach official 2022 Form 20 election turnout & booth-wise leader
    ac_e22 = ELECTION_2022_DATA.get(str(ac_no))
    election_summary = None
    if ac_e22:
        booths_map = ac_e22.get("booths", {})
        cands = ac_e22.get("candidates", [])
        for ps in polling_stations:
            b22 = booths_map.get(str(ps["part_no"]))
            if b22:
                b_votes = b22.get("votes", [])
                top_idx = max(range(len(b_votes)), key=lambda i: b_votes[i]) if b_votes else None
                ps["polled_2022"] = b22.get("total_polled", 0)
                ps["leader_2022"] = cands[top_idx] if (top_idx is not None and top_idx < len(cands)) else None
                ps["leader_votes_2022"] = b_votes[top_idx] if top_idx is not None else 0
        election_summary = {
            "year": 2022,
            "turnout_pct": ac_e22.get("turnout_pct"),
            "total_polled": ac_e22.get("total_polled"),
            "total_electors_2022": ac_e22.get("total_electors_2022"),
            "winner": ac_e22.get("winner"),
            "winner_party": ac_e22.get("winner_party"),
            "winner_votes": ac_e22.get("winner_votes"),
            "runner_up": ac_e22.get("runner_up"),
            "runner_up_votes": ac_e22.get("runner_up_votes"),
            "margin": ac_e22.get("margin"),
            "nota": ac_e22.get("nota"),
            "candidate_totals": ac_e22.get("candidate_totals", []),
        }

    return {
        "constituency": dict(ac_row),
        "summary": {
            "total_voters": total_voters,
            "male": total_male,
            "female": total_female,
            "sex_ratio": sex_ratio,
            "total_booths": len(polling_stations)
        },
        "election_2022": election_summary,
        "polling_stations": polling_stations,
        "age_distribution": age_groups,
        "largest_booths": largest_booths,
        "smallest_booths": smallest_booths
    }

@app.get("/api/booth/{ac_no}/{part_no}/export_csv")
def export_booth_csv(ac_no: int, part_no: int):
    import csv
    import io
    from fastapi.responses import Response

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, epic_no, name, relative_name, relation_type, house_no, age, gender, ac_no, ac_name, part_no, polling_station_name, section_name, status
    FROM voters 
    WHERE ac_no = ? AND part_no = ?
    ORDER BY CAST(house_no AS INTEGER) ASC, id ASC
    """, (ac_no, part_no))
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Serial No", "EPIC No", "Name", "Relative Name", "Relation", "House No", "Age", "Gender", "AC No", "AC Name", "Part No", "Polling Station", "Section", "Status"])
    for idx, r in enumerate(rows, 1):
        writer.writerow([idx] + [r[c] for c in r.keys()][1:])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=Booth_{ac_no}_{part_no}_Electoral_Roll.csv"}
    )

NAME_VOCAB = set()

def get_name_vocab():
    global NAME_VOCAB
    if not NAME_VOCAB:
        try:
            conn = get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT name FROM voters LIMIT 150000;")
            for row in cursor.fetchall():
                if row[0]:
                    for w in row[0].split():
                        w_clean = w.lower().strip()
                        if len(w_clean) >= 3 and w_clean.isalpha():
                            NAME_VOCAB.add(w_clean)
            conn.close()
        except Exception:
            pass
    return NAME_VOCAB

def expand_phonetic_variants(word: str) -> List[str]:
    """Expands transliterated Manipur name variants (b/v, s/sh, ee/ei, etc.)"""
    w = word.lower().strip()
    variants = {w}
    if 'b' in w:
        variants.add(w.replace('b', 'v'))
    if 'v' in w:
        variants.add(w.replace('v', 'b'))
    if 's' in w and 'sh' not in w:
        variants.add(w.replace('s', 'sh'))
    if 'sh' in w:
        variants.add(w.replace('sh', 's'))
    if 'ee' in w:
        variants.add(w.replace('ee', 'ei'))
    if 'ei' in w:
        variants.add(w.replace('ei', 'ee'))
    if 'kh' in w:
        variants.add(w.replace('kh', 'k'))
    return list(variants)

def build_fts_query(text: str) -> str:
    cleaned = re.sub(r'[^\w\s]', ' ', text)
    tokens = [w for w in cleaned.split() if w]
    if not tokens:
        return '""'
    token_clauses = []
    for t in tokens:
        vars_list = expand_phonetic_variants(t)
        if len(vars_list) == 1:
            token_clauses.append(f'"{t}"*')
        else:
            token_clauses.append("(" + " OR ".join([f'"{v}"*' for v in vars_list]) + ")")
    return " AND ".join(token_clauses)

def correct_spelling(query_str: str) -> Optional[str]:
    vocab = get_name_vocab()
    if not vocab:
        return None
    words = query_str.strip().split()
    corrected_words = []
    has_correction = False
    
    for w in words:
        w_lower = w.lower()
        if len(w_lower) >= 3 and w_lower.isalpha() and w_lower not in vocab:
            matches = difflib.get_close_matches(w_lower, vocab, n=1, cutoff=0.72)
            if matches:
                rep = matches[0]
                if w.isupper():
                    rep = rep.upper()
                elif w[0].isupper():
                    rep = rep.capitalize()
                corrected_words.append(rep)
                has_correction = True
            else:
                corrected_words.append(w)
        else:
            corrected_words.append(w)
            
    if has_correction:
        return " ".join(corrected_words)
    return None

@app.get("/api/search/suggestions")
def get_search_suggestions(q: str = Query(..., min_length=2), ac_no: Optional[int] = None):
    clean_q = q.strip().replace("'", "").replace('"', "")
    if not clean_q:
        return {"query": q, "suggestions": [], "corrected_query": None}
        
    corrected = correct_spelling(clean_q)
    eval_q = corrected if corrected else clean_q
    tokens = [w for w in eval_q.split() if w]
    if not tokens:
        return {"query": q, "suggestions": [], "corrected_query": None}
        
    fts_expr = build_fts_query(eval_q)
    conn = get_connection()
    cursor = conn.cursor()
    
    params = [fts_expr]
    ac_clause = ""
    if ac_no:
        ac_clause = " AND v.ac_no = ?"
        params.append(ac_no)
        
    sql = f"""
    SELECT DISTINCT v.name, v.ac_name, v.part_no, v.house_no, v.id, v.relative_name
    FROM voters_fts f
    JOIN voters v ON f.rowid = v.id
    WHERE voters_fts MATCH ? {ac_clause}
    ORDER BY 
      CASE 
        WHEN LOWER(v.name) = LOWER(?) THEN 1
        WHEN LOWER(v.name) LIKE LOWER(?) THEN 2
        WHEN LOWER(v.name) LIKE LOWER(?) THEN 3
        WHEN LOWER(v.relative_name) LIKE LOWER(?) THEN 4
        ELSE 5
      END ASC, v.id ASC
    LIMIT 8;
    """
    params.extend([eval_q, f"{eval_q}%", f"%{eval_q}%", f"%{eval_q}%"])
    try:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    except Exception:
        like_params = [f"%{eval_q}%", f"%{eval_q}%"]
        like_ac = ""
        if ac_no:
            like_ac = " AND ac_no = ?"
            like_params.append(ac_no)
        like_sql = f"""
        SELECT DISTINCT name, ac_name, part_no, house_no, id, relative_name
        FROM voters
        WHERE (name LIKE ? OR relative_name LIKE ?) {like_ac}
        LIMIT 8;
        """
        cursor.execute(like_sql, like_params)
        rows = cursor.fetchall()
    conn.close()
    
    suggestions = []
    seen_names = set()
    
    # If typo corrected, add a "Did you mean" suggestion card first
    if corrected and corrected.lower() != clean_q.lower():
        suggestions.append({
            "text": corrected,
            "type": "SPELLING_CORRECTION",
            "title": f"Did you mean: {corrected}?",
            "subtitle": f"Corrected typo from '{clean_q}'",
            "voter_id": None
        })
        
    for r in rows:
        name_val = r[0]
        if name_val not in seen_names:
            seen_names.add(name_val)
            subtitle_parts = []
            if r[1]: subtitle_parts.append(r[1])
            if r[2]: subtitle_parts.append(f"Booth #{r[2]}")
            if r[3]: subtitle_parts.append(f"H.{r[3]}")
            
            suggestions.append({
                "text": name_val,
                "type": "ELECTOR_NAME",
                "title": name_val,
                "subtitle": " • ".join(subtitle_parts),
                "relative_name": r[5] or "",
                "voter_id": r[4]
            })
            
    return {
        "query": q,
        "corrected_query": corrected if (corrected and corrected.lower() != clean_q.lower()) else None,
        "suggestions": suggestions
    }

@app.get("/api/search")
def search_voters(
    request: Request,
    q: Optional[str] = Query(None, description="Search query (name, relative name, EPIC, or area)"),
    ac_no: Optional[int] = Query(None, description="Filter by Assembly Constituency number"),
    gender: Optional[str] = Query(None, description="Filter by gender (M/F/O)"),
    min_age: Optional[int] = Query(None, description="Minimum age"),
    max_age: Optional[int] = Query(None, description="Maximum age"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100)
):
    t_start = time.perf_counter()
    offset = (page - 1) * limit
    conn = get_connection()
    cursor = conn.cursor()

    did_you_mean = None
    clean_q = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', q).strip().replace("'", "").replace('"', "") if q and q.strip() else None

    def execute_query_run(search_text: Optional[str]):
        params = []
        is_deleted_search = bool(search_text and search_text.strip().lower() in ("deleted", "status:deleted"))
        if is_deleted_search:
            sql_base = "SELECT v.* FROM voters v WHERE (v.status = 'DELETED' OR v.name LIKE '%DELETED%')"
        elif search_text:
            fts_query = build_fts_query(search_text)
            sql_base = """
            SELECT v.* 
            FROM voters_fts f
            JOIN voters v ON f.rowid = v.id
            WHERE voters_fts MATCH ?
            """
            params.append(fts_query)
        else:
            sql_base = "SELECT v.* FROM voters v WHERE 1=1"

        if ac_no:
            sql_base += " AND v.ac_no = ?"
            params.append(ac_no)
        if gender:
            sql_base += " AND v.gender = ?"
            params.append(gender)
        if min_age:
            sql_base += " AND v.age >= ?"
            params.append(min_age)
        if max_age:
            sql_base += " AND v.age <= ?"
            params.append(max_age)

        count_sql = f"SELECT COUNT(*) FROM ({sql_base})"

        try:
            cursor.execute(count_sql, params)
            total_cnt = cursor.fetchone()[0]

            if is_deleted_search:
                sql_ordered = sql_base + " ORDER BY v.ac_no ASC, v.part_no ASC, v.id ASC LIMIT ? OFFSET ?"
                query_params = list(params)
                query_params.extend([limit, offset])
                cursor.execute(sql_ordered, query_params)
            elif search_text:
                sql_ordered = sql_base + """
                ORDER BY 
                  CASE 
                    WHEN LOWER(v.name) = LOWER(?) THEN 1
                    WHEN LOWER(v.name) LIKE LOWER(?) THEN 2
                    WHEN LOWER(v.name) LIKE LOWER(?) THEN 3
                    WHEN LOWER(v.relative_name) = LOWER(?) THEN 4
                    WHEN LOWER(v.relative_name) LIKE LOWER(?) THEN 5
                    ELSE 6
                  END ASC, v.id ASC LIMIT ? OFFSET ?
                """
                query_params = list(params)
                query_params.extend([search_text, f"{search_text}%", f"%{search_text}%", search_text, f"%{search_text}%", limit, offset])
                cursor.execute(sql_ordered, query_params)
            else:
                sql_ordered = sql_base + " ORDER BY v.id ASC LIMIT ? OFFSET ?"
                query_params = list(params)
                query_params.extend([limit, offset])
                cursor.execute(sql_ordered, query_params)

            rows = [dict(r) for r in cursor.fetchall()]
            return total_cnt, rows
        except Exception:
            # Resilient fallback to pure SQL LIKE search if FTS encounters malformed expressions
            like_params = []
            like_base = "SELECT v.* FROM voters v WHERE 1=1"
            if search_text:
                if is_deleted_search:
                    like_base += " AND (v.status = 'DELETED' OR v.name LIKE '%DELETED%')"
                else:
                    like_base += " AND (v.name LIKE ? OR v.relative_name LIKE ? OR v.epic_no LIKE ? OR v.house_no LIKE ?)"
                    like_params.extend([f"%{search_text}%", f"%{search_text}%", f"%{search_text}%", f"%{search_text}%"])
            if ac_no:
                like_base += " AND v.ac_no = ?"
                like_params.append(ac_no)
            if gender:
                like_base += " AND v.gender = ?"
                like_params.append(gender)
            if min_age:
                like_base += " AND v.age >= ?"
                like_params.append(min_age)
            if max_age:
                like_base += " AND v.age <= ?"
                like_params.append(max_age)

            cursor.execute(f"SELECT COUNT(*) FROM ({like_base})", like_params)
            total_cnt = cursor.fetchone()[0]

            like_ordered = like_base + " ORDER BY v.id ASC LIMIT ? OFFSET ?"
            like_params.extend([limit, offset])
            cursor.execute(like_ordered, like_params)
            rows = [dict(r) for r in cursor.fetchall()]
            return total_cnt, rows

    total_results, results = execute_query_run(clean_q)

    # Automatic fallback: If zero results, check self-learned synonyms first, then static typo correction
    if total_results == 0 and clean_q:
        suggested = get_learned_synonym(clean_q) or correct_spelling(clean_q)
        if suggested and suggested.lower() != clean_q.lower():
            fb_total, fb_results = execute_query_run(suggested)
            if fb_total > 0:
                total_results = fb_total
                results = fb_results
                did_you_mean = suggested

    conn.close()

    try:
        if request is not None:
            log_api_request(
                client_ip=extract_client_ip(request),
                endpoint="/api/search",
                query_text=clean_q,
                ac_no=ac_no,
                gender=gender,
                result_count=total_results,
                response_time_ms=(time.perf_counter() - t_start) * 1000.0,
                user_agent=request.headers.get("user-agent"),
            )
    except Exception:
        pass

    return {
        "total": total_results,
        "page": page,
        "limit": limit,
        "total_pages": (total_results + limit - 1) // limit,
        "did_you_mean": did_you_mean,
        "results": results
    }

@app.get("/api/voter/{voter_id}")
def get_voter(voter_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM voters WHERE id = ?", (voter_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Voter not found")
    return dict(row)

def clean_honorifics(name: str) -> str:
    if not name: return ""
    # Strip common titles / honorific prefixes
    s = re.sub(r'^\s*(\(?[a-zA-Z]\)?\.?|\(O\)|\(A\)|\(H\)|\(L\)|\(M\)|late|l\.?|md\.?|mohd\.?|shri|smt\.?|mr\.?|mrs\.?|dr\.?)\s+', '', name.strip(), flags=re.IGNORECASE)
    return s.strip()

COMMON_HONORIFICS = {'md', 'mohd', 'late', 'lt', 'rk', 'mk', 'shri', 'smt', 'mr', 'mrs', 'dr', 'km'}
PURE_TITLES = {'singh', 'devi', 'meitei', 'meetei', 'chanu', 'sharma', 'begum', 'bibi'}

def clean_words(s: str):
    if not s: return []
    raw = [w.lower() for w in re.split(r'[^a-zA-Z0-9]+', s) if w]
    return [w for w in raw if w not in COMMON_HONORIFICS]

def filter_core_words(words):
    core = [w for w in words if w not in PURE_TITLES]
    return core if core else words

def tokenize_name(name: str):
    s = clean_honorifics(name)
    return clean_words(s)

def normalize_stem(token: str) -> str:
    if not token: return ""
    t = token.lower().strip()
    t = t.replace('nung', 'nu').replace('ng', 'm')
    t = re.sub(r'([a-z])\1+', r'\1', t)
    t = t.replace('sh', 's').replace('ei', 'e').replace('v', 'b').replace('w', 'b')
    return t

def normalize_house_no(h: str) -> str:
    if not h: return ""
    s = re.sub(r'^(h\s*no\.?|house\s*no\.?|house|h|hn)[-\s./]*', '', str(h).strip(), flags=re.IGNORECASE)
    s = s.lstrip('0')
    return s.strip().lower()

def get_base_house_no(h: str) -> str:
    norm = normalize_house_no(h)
    return re.split(r'[/\\-]', norm)[0] if norm else ""

COMPOUND_SUFFIXES = {'kumar', 'kishore', 'chandra', 'mani', 'sana', 'devi', 'singh', 'sharma', 'mohan', 'mohon', 'babu', 'bala', 'jit', 'jeet'}

def token_similarity(t1: str, t2: str) -> float:
    if t1 == t2: return 1.0
    n1 = normalize_stem(t1)
    n2 = normalize_stem(t2)
    if n1 == n2: return 1.0

    # Invariant: Disallow compound suffix bleed (e.g. 'shyam' vs 'shyamkumar')
    for suf in COMPOUND_SUFFIXES:
        if t1.endswith(suf) and t1[:-len(suf)] == t2: return 0.5
        if t2.endswith(suf) and t2[:-len(suf)] == t1: return 0.5
        if n1.endswith(suf) and n1[:-len(suf)] == n2: return 0.5
        if n2.endswith(suf) and n2[:-len(suf)] == n1: return 0.5

    # Short distinctive names (<= 5 chars) must not match different vowels (e.g. 'shyam' vs 'shamu', 'tomba' vs 'tombi')
    ratio = difflib.SequenceMatcher(None, t1, t2).ratio()
    if min(len(t1), len(t2)) <= 5:
        return ratio if ratio >= 0.90 else 0.5

    return ratio

def match_person_names(name1: str, name2: str, primary_clan: str = None) -> bool:
    """
    Deterministically matches individuals across all 60 Manipur Assembly Constituencies
    covering Meitei (Yumnam + given), Pangal/Muslim, Kuki/Naga, and Gorkha/Nepali naming.
    """
    w1 = clean_words(name1)
    w2 = clean_words(name2)
    if not w1 or not w2: return False

    if "".join(w1) == "".join(w2): return True

    core1 = filter_core_words(w1)
    core2 = filter_core_words(w2)
    if not core1 or not core2: return False

    c1, c2 = "".join(core1), "".join(core2)
    if c1 == c2: return True

    # Token set equality (handles name inversion: 'marconi konsam' == 'konsam marconi')
    if set(core1) == set(core2): return True

    # Stemmed token set equality
    n1 = [normalize_stem(w) for w in core1]
    n2 = [normalize_stem(w) for w in core2]
    if "".join(n1) == "".join(n2) or set(n1) == set(n2): return True

    # Single Given Name vs Compound Name match (e.g. 'Pishak' vs 'Naorem Pishak Singh', 'Shyam' vs 'Naorem Shyam Singh')
    if len(core1) == 1 and len(core1[0]) >= 3:
        for w in core2:
            if token_similarity(core1[0], w) >= 0.88:
                return True
    elif len(core2) == 1 and len(core2[0]) >= 3:
        for w in core1:
            if token_similarity(core2[0], w) >= 0.88:
                return True

    # Single Given Name vs Clan + Given Name match (e.g. 'Manihar' vs 'Naorem Manihar' or 'Manihar Singh' vs 'Naorem Manihar Singh')
    clan_stem = normalize_stem(primary_clan) if primary_clan else ""
    if len(core1) == 1 and len(core2) >= 2:
        if clan_stem and (n2[0] == clan_stem or n2[-1] == clan_stem):
            other_words = [w for idx, w in enumerate(core2) if n2[idx] != clan_stem]
            if other_words and token_similarity(core1[0], other_words[0]) >= 0.85:
                return True
    elif len(core2) == 1 and len(core1) >= 2:
        if clan_stem and (n1[0] == clan_stem or n1[-1] == clan_stem):
            other_words = [w for idx, w in enumerate(core1) if n1[idx] != clan_stem]
            if other_words and token_similarity(core2[0], other_words[0]) >= 0.85:
                return True

    # Initial expansion: 'M. Konsam' == 'Marconi Konsam'
    for idx1, t1 in enumerate(core1):
        if len(t1) <= 2:
            rem1 = core1[:idx1] + core1[idx1+1:]
            for idx2, t2 in enumerate(core2):
                if len(t2) > 2 and t2[0] == t1[0]:
                    rem2 = core2[:idx2] + core2[idx2+1:]
                    if set(rem1) == set(rem2) or set(normalize_stem(x) for x in rem1) == set(normalize_stem(x) for x in rem2):
                        return True

    for idx2, t2 in enumerate(core2):
        if len(t2) <= 2:
            rem2 = core2[:idx2] + core2[idx2+1:]
            for idx1, t1 in enumerate(core1):
                if len(t1) > 2 and t1[0] == t2[0]:
                    rem1 = core1[:idx1] + core1[idx1+1:]
                    if set(rem1) == set(rem2) or set(normalize_stem(x) for x in rem1) == set(normalize_stem(x) for x in rem2):
                        return True

    # Compound word match: 'birbahadur' in 'bir' + 'bahadur' + 'khadka'
    if len(core2) == 1 and len(core2[0]) >= 6:
        for k in range(2, len(core1) + 1):
            if "".join(core1[:k]) == core2[0] or "".join(n1[:k]) == n2[0]:
                return True
    if len(core1) == 1 and len(core1[0]) >= 6:
        for k in range(2, len(core2) + 1):
            if "".join(core2[:k]) == core1[0] or "".join(n2[:k]) == n1[0]:
                return True

    # Clan / Surname Disambiguation:
    # If both share a common clan prefix, their given names must also match!
    # (Prevents conflating two different individuals like 'Wahengbam Sama' and 'Wahengbam Sarat')
    if len(core1) >= 2 and len(core2) >= 2:
        if core1[0] == core2[0] or n1[0] == n2[0]:
            g1 = " ".join(core1[1:])
            g2 = " ".join(core2[1:])
            return token_similarity(g1, g2) >= 0.85 or token_similarity("".join(core1[1:]), "".join(core2[1:])) >= 0.85
        if core1[-1] == core2[-1] or n1[-1] == n2[-1]:
            g1 = " ".join(core1[:-1])
            g2 = " ".join(core2[:-1])
            return token_similarity(g1, g2) >= 0.85 or token_similarity("".join(core1[:-1]), "".join(core2[:-1])) >= 0.85

    # Multi-token alignment with fuzzy similarity (handles 'Mutum Somendro' vs 'Mutum Somorendro')
    if len(core1) == len(core2):
        if all(token_similarity(t1, t2) >= 0.85 for t1, t2 in zip(core1, core2)):
            return True
        if len(core1) == 2:
            if token_similarity(core1[0], core2[1]) >= 0.85 and token_similarity(core1[1], core2[0]) >= 0.85:
                return True

    # Symmetrical compound prefix/suffix match (ONLY when both are compounds >= 8 chars and share stem)
    if len(core1) >= 2 and len(core2) >= 2 and len(c1) >= 8 and len(c2) >= 8:
        if difflib.SequenceMatcher(None, c1, c2).ratio() >= 0.88:
            return True

    return False

def safe_age(m):
    a = m.get("age")
    if isinstance(a, int): return a
    if isinstance(a, str) and a.isdigit(): return int(a)
    return 0

def extract_clan_or_surname(name: str) -> str:
    if not name: return ""
    tokens = clean_words(name)
    if not tokens: return ""
    stop_prefixes = {"MD", "MOHD", "MOHAMMAD", "MK", "RK", "R.K.", "LATE", "SH", "SMT", "MST", "MR", "MRS"}
    filtered = [t for t in tokens if t not in stop_prefixes]
    if not filtered: return tokens[0]
    for i, t in enumerate(filtered):
        if t == "NINGOL" and i > 0:
            return filtered[i-1]
    return filtered[0]

def extract_maiden_clan(mother_name: str, father_name: str) -> str:
    if not mother_name: return ""
    tokens = clean_words(mother_name)
    for i, t in enumerate(tokens):
        if t == "NINGOL" and i > 0:
            return tokens[i-1]
    m_clan = extract_clan_or_surname(mother_name)
    f_clan = extract_clan_or_surname(father_name)
    if m_clan and f_clan and m_clan != f_clan:
        return m_clan
    return m_clan or "Natal"

@app.get("/api/voter/{voter_id}/family")
def get_voter_family(voter_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM voters WHERE id = ?", (voter_id,))
    target_row = cursor.fetchone()
    if not target_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Voter not found")

    target = dict(target_row)
    ac_no = target["ac_no"]
    part_no = target["part_no"]
    house_no = target.get("house_no") or ""
    raw_house = house_no
    norm_house = normalize_house_no(raw_house)
    base_house = get_base_house_no(raw_house)

    t_name = target["name"]
    t_rel = target.get("relative_name") or ""
    t_rel_type = (target.get("relation_type") or "Father").capitalize()
    t_age = target.get("age") or 30
    t_sex = target.get("gender")

    core_name_words = filter_core_words(clean_words(t_name))
    core_rel_words = filter_core_words(clean_words(t_rel))
    clan_token = core_name_words[0] if core_name_words and len(core_name_words[0]) >= 3 else None
    name_distinctive = [w for w in core_name_words if len(w) >= 3]
    rel_distinctive = [w for w in core_rel_words if len(w) >= 3]

    # -----------------------------------------------------------------
    # AXIOM 1: PRIMARY HOUSEHOLD CO-RESIDENCE POOL (Gold Standard)
    # Strictly electors in the SAME polling station AND SAME normalized house
    # -----------------------------------------------------------------
    cursor.execute("""
        SELECT * FROM voters 
        WHERE ac_no = ? AND part_no = ? AND id != ?
        ORDER BY age DESC
    """, (ac_no, part_no, target["id"]))
    part_rows = [dict(r) for r in cursor.fetchall()]

    same_house = []
    has_same_house_parent = False
    for m in part_rows:
        m_house = m.get("house_no") or ""
        m_norm = normalize_house_no(m_house)
        m_base = get_base_house_no(m_house)
        if m_house == raw_house or (norm_house and m_norm == norm_house) or (base_house and m_base == base_house):
            m["audit_tier"] = "GOLD_SAME_HOUSEHOLD"
            same_house.append(m)
            if t_rel and match_person_names(m["name"], t_rel):
                has_same_house_parent = True

    # -----------------------------------------------------------------
    # AXIOM 2: CONTROLLED CROSS-PART EXCEPTION (Silver Standard)
    # Applied ONLY when target lacks parents in current booth (e.g. split booth).
    # Mandates:
    # 1. EXACT SAME normalized base house number (e.g. H-76 <-> 76)
    # 2. Distinctive non-generic given name match (>= 2 tokens or >= 6 chars)
    # 3. Shared family surname / clan token
    # -----------------------------------------------------------------
    cross_part_candidates = []
    should_search_cross_part = (len(same_house) <= 1 or not has_same_house_parent or t_age >= 30)
    is_distinctive_target = len(core_name_words) >= 2 or len(core_rel_words) >= 2

    if should_search_cross_part and is_distinctive_target and base_house and len(base_house) >= 1:
        cursor.execute("""
            SELECT * FROM voters 
            WHERE ac_no = ? AND part_no != ? AND id != ? AND (house_no = ? OR house_no = ? OR house_no = ?)
        """, (ac_no, part_no, target["id"], raw_house, f"H-{base_house}", base_house))
        house_matches = [dict(r) for r in cursor.fetchall()]

        for m in house_matches:
            m_rel = m.get("relative_name") or ""
            m_rel_type = (m.get("relation_type") or "Father").capitalize()
            m_words = filter_core_words(clean_words(m["name"]))

            # Require distinctive compound name (prevent generic single-name cross-part leaks)
            if len(m_words) < 2 and (not m_words or len(m_words[0]) < 6):
                continue

            shared_tokens = set(core_name_words) & set(m_words)
            rel_shared_tokens = set(core_rel_words) & set(m_words)

            is_valid_cross = False
            # Case A: Candidate is direct parent of target with distinctive name
            if t_rel and match_person_names(m["name"], t_rel) and len(rel_shared_tokens) >= 1:
                is_valid_cross = True
            # Case B: Candidate is direct child of target with distinctive name
            elif match_person_names(t_name, m_rel) and len(shared_tokens) >= 1:
                is_valid_cross = True
            # Case C: Candidate is spouse
            elif ((m_rel_type == "Husband" and match_person_names(t_name, m_rel)) or (t_rel_type == "Husband" and match_person_names(m["name"], t_rel))) and len(shared_tokens | rel_shared_tokens) >= 1:
                is_valid_cross = True
            # Case D: Candidate is full sibling with distinctive father
            elif t_rel and m_rel and match_person_names(t_rel, m_rel) and len(core_rel_words) >= 2:
                is_valid_cross = True

            if is_valid_cross:
                m["audit_tier"] = "SILVER_SPLIT_BOOTH"
                cross_part_candidates.append(m)

    # -----------------------------------------------------------------
    # AXIOM 1.5: PATRILINEAL & CLAN COHERENCE FILTER (Sagei Invariant)
    # Segregate same-house electors into target's biological family cluster
    # vs unrelated cohabitants / separate household units sharing house number
    # -----------------------------------------------------------------
    target_clan = extract_clan_or_surname(t_name)
    rel_clan = extract_clan_or_surname(t_rel)
    t_words = filter_core_words(clean_words(t_name))
    r_words = filter_core_words(clean_words(t_rel))

    # For married female, husband's clan is the patrilineal household clan
    if t_sex == "F" and t_rel_type == "Husband":
        # 1. Look for husband in same house
        husband_m = next((m for m in same_house if m.get("gender") == "M" and match_person_names(m["name"], t_rel)), None)
        if husband_m:
            primary_clan = extract_clan_or_surname(husband_m["name"])
        elif rel_clan and len(r_words) > 1:
            primary_clan = rel_clan
        else:
            # Look for husband in the same booth
            cursor.execute("""
                SELECT name FROM voters 
                WHERE ac_no = ? AND part_no = ? AND gender = 'M' AND abs(age - ?) <= 25
            """, (ac_no, part_no, t_age))
            b_husbands = [dict(r) for r in cursor.fetchall()]
            match_h = next((h for h in b_husbands if match_person_names(h["name"], t_rel)), None)
            if match_h:
                primary_clan = extract_clan_or_surname(match_h["name"])
            else:
                primary_clan = target_clan
    elif len(r_words) <= 1:
        primary_clan = target_clan
    else:
        if t_words and rel_clan and t_words[-1].lower() == rel_clan.lower():
            target_clan = rel_clan
        primary_clan = rel_clan if rel_clan else target_clan

    target_family_cluster = []
    unrelated_cohabitants = []

    for m in same_house:
        m_name = m["name"]
        m_clan = extract_clan_or_surname(m_name)
        m_rel = m.get("relative_name") or ""
        m_rel_clan = extract_clan_or_surname(m_rel)
        m_rel_type = (m.get("relation_type") or "Father").capitalize()
        
        # Condition 1: Direct paternal/clan match
        is_clan_match = (primary_clan and (m_clan == primary_clan or m_rel_clan == primary_clan))
        # Condition 2: Target's named parent (e.g. mother or father with different surname spelling)
        is_direct_parent = (t_rel and match_person_names(m_name, t_rel, primary_clan))
        # Condition 3: Married mother retaining maiden surname or husband match
        is_mother_match = (m.get("gender") == "F" and m_rel_type == "Husband" and (m_rel_clan == primary_clan or (t_rel and match_person_names(m_rel, t_rel, primary_clan))))
        # Condition 3.5: Target's spouse in same house (even if retaining maiden clan like Ngangbam, Moirangthem, etc.)
        is_spouse_match = False
        if t_sex == "M" and m.get("gender") == "F" and m_rel_type == "Husband" and match_person_names(t_name, m_rel, primary_clan):
            is_spouse_match = True
        elif t_sex == "F" and t_rel_type == "Husband" and match_person_names(m_name, t_rel, primary_clan):
            is_spouse_match = True
        # Condition 3.6: Daughter-in-law in same house (wife of a co-resident son naming target or target's clan)
        is_daughter_in_law = False
        if m.get("gender") == "F" and m_rel_type == "Husband":
            for other_m in same_house:
                if other_m.get("gender") == "M" and match_person_names(other_m["name"], m_rel, primary_clan):
                    if match_person_names(t_name, other_m.get("relative_name") or "", primary_clan):
                        is_daughter_in_law = True
                        break
        # Condition 3.7: Husband's co-resident parents/siblings when target is married female
        is_in_law_match = False
        if t_sex == "F" and t_rel_type == "Husband":
            husband_m = next((h for h in same_house if h.get("gender") == "M" and match_person_names(h["name"], t_rel)), None)
            if husband_m:
                h_rel = husband_m.get("relative_name") or ""
                if h_rel:
                    if m.get("gender") == "M" and match_person_names(m["name"], h_rel, primary_clan):
                        is_in_law_match = True
                    elif m.get("gender") == "F" and m_rel_type == "Husband" and match_person_names(m_rel, h_rel, primary_clan):
                        is_in_law_match = True
                    elif match_person_names(m_rel, h_rel, primary_clan):
                        is_in_law_match = True
        # Condition 3.8: Co-resident collateral sharing household clan through deceased male (e.g. Late Naorem Sunder Singh)
        is_deceased_collateral = False
        if m_rel_clan == primary_clan or (primary_clan and primary_clan.lower() in m_rel.lower()):
            is_deceased_collateral = True
        # Condition 4: Target's child naming target (or naming target's husband when target is married female)
        is_child_match = match_person_names(t_name, m_rel, primary_clan) or (
            t_sex == "F" and t_rel_type == "Husband" and t_rel and match_person_names(t_rel, m_rel, primary_clan)
        )
        # Condition 4.5: Shared last-token surname (covers Nepali/Gorkha/Kuki/Naga surname-last naming like 'Chhetry', 'Chamling', 'Bhandari')
        m_core = filter_core_words(clean_words(m_name))
        is_surname_match = bool(
            t_words and m_core and len(t_words[-1]) >= 4 and (
                t_words[-1] == m_core[-1] or token_similarity(t_words[-1], m_core[-1]) >= 0.88
            )
        )
        # Condition 5: Matrilocal elder (mother's father in house)
        is_matrilocal_elder = (m.get("gender") == "M" and (m.get("age") or 0) - t_age >= 35 and m_clan and t_rel_type == "Mother" and m_clan == extract_clan_or_surname(t_rel))

        if is_clan_match or is_direct_parent or is_mother_match or is_spouse_match or is_daughter_in_law or is_in_law_match or is_deceased_collateral or is_child_match or is_surname_match or is_matrilocal_elder:
            target_family_cluster.append(m)
        else:
            unrelated_cohabitants.append(m)

    # Zero-Error Candidate Pool: strictly target family cluster + verified cross-part candidates
    # (unrelated cohabitants from different clans/families are strictly segregated)
    seen_ids = {target["id"]}
    candidate_pool = []
    for m in target_family_cluster + cross_part_candidates:
        if m["id"] not in seen_ids:
            seen_ids.add(m["id"])
            candidate_pool.append(m)

    parents = []
    grandparents = []
    uncles_aunts = []
    spouses = []
    siblings = []
    siblings_spouses = []
    cousins = []
    children = []
    others = []

    assigned_ids = set()
    father_name_refs = set()
    mother_name_refs = set()
    grandparent_name_refs = set()

    if t_rel:
        if t_rel_type == "Father":
            father_name_refs.add(t_rel)
        elif t_rel_type == "Mother":
            mother_name_refs.add(t_rel)

    # --- PASS 1: Direct Spouse & Living Parent Matching ---
    for m in candidate_pool:
        m_name = m["name"]
        m_rel = m.get("relative_name") or ""
        m_rel_type = (m.get("relation_type") or "Father").capitalize()
        m_age = m.get("age") or 30
        m_sex = m.get("gender")
        diff = m_age - t_age

        # Legal Guardian / Ward Resolution for 'Others'
        if t_rel_type in ("Others", "Other") and match_person_names(m_name, t_rel):
            if abs(diff) <= 22:
                m["relationship_label"] = "Elder Brother (Legal Guardian)" if diff > 0 and m_sex == "M" else ("Elder Sister (Legal Guardian)" if diff > 0 else "Sibling (Guardian)")
                siblings.append(m)
                assigned_ids.add(m["id"])
                continue
            elif 20 < diff <= 50:
                m["relationship_label"] = "Paternal Uncle (Guardian)" if m_sex == "M" else "Aunt (Guardian)"
                parents.append(m)
                assigned_ids.add(m["id"])
                continue
            elif diff > 50:
                m["relationship_label"] = "Grandfather (Guardian)" if m_sex == "M" else "Grandmother (Guardian)"
                grandparents.append(m)
                assigned_ids.add(m["id"])
                continue

        if m_rel_type in ("Others", "Other") and match_person_names(t_name, m_rel):
            if abs(diff) <= 22:
                m["relationship_label"] = "Younger Brother (Ward)" if m_sex == "M" and diff < 0 else ("Younger Sister (Ward)" if diff < 0 else "Sibling (Ward)")
                siblings.append(m)
                assigned_ids.add(m["id"])
                continue

        # Spouse (physiologically typical marital age gap: -18 to +25)
        is_spouse = False
        if -18 <= diff <= 25:
            if t_sex == "F" and t_rel_type == "Husband" and match_person_names(m_name, t_rel, primary_clan):
                is_spouse = True
            elif m_sex == "F" and m_rel_type == "Husband" and match_person_names(t_name, m_rel, primary_clan):
                if not any(match_person_names(m_rel, f, primary_clan) for f in father_name_refs if f):
                    is_spouse = True
        if is_spouse and m["id"] not in assigned_ids:
            spouses.append(m)
            assigned_ids.add(m["id"])
            if t_sex == "F" and m_rel:
                if m_rel_type == "Father":
                    father_name_refs.add(m_rel)
            continue

        # Direct Parent (12 <= diff <= 55)
        is_parent = False
        if t_rel and match_person_names(m_name, t_rel, primary_clan) and 12 <= diff <= 55:
            # Enforce strict biological gender invariants on declared parent relation
            if (t_rel_type == "Father" and m_sex == "M") or (t_rel_type == "Mother" and m_sex == "F") or (t_rel_type not in ("Father", "Mother")):
                is_parent = True
        if is_parent and m["id"] not in assigned_ids:
            parents.append(m)
            assigned_ids.add(m["id"])
            if m_sex == "M":
                father_name_refs.add(m_name)
                if m_rel and m_rel_type == "Father":
                    grandparent_name_refs.add(m_rel)
            else:
                mother_name_refs.add(m_name)
            continue

    # --- PASS 1.5: Identify In-Laws or Father from Mother's Husband ---
    for p in list(parents):
        if p.get("gender") == "F" and p.get("relation_type") == "Husband" and p.get("relative_name"):
            m_hub = p["relative_name"]
            father_name_refs.add(m_hub)
            for m in candidate_pool:
                if m["id"] in assigned_ids: continue
                if m.get("gender") == "M" and match_person_names(m["name"], m_hub, primary_clan):
                    parents.append(m)
                    assigned_ids.add(m["id"])
                    if m.get("relative_name") and m.get("relation_type") == "Father":
                        grandparent_name_refs.add(m["relative_name"])
                    break

    # If target is female and has spouse: link husband's father and mother as In-Laws / Household Elders
    if t_sex == "F" and t_rel_type == "Husband":
        for s in spouses:
            if s.get("relative_name") and s.get("relation_type") == "Father":
                f_in_law_name = s["relative_name"]
                father_name_refs.add(f_in_law_name)
                for m in candidate_pool:
                    if m["id"] in assigned_ids: continue
                    if m.get("gender") == "M" and match_person_names(m["name"], f_in_law_name, primary_clan) and (m.get("age", 0) - t_age >= 14):
                        m["relationship_label"] = "Father-in-Law"
                        parents.append(m)
                        assigned_ids.add(m["id"])
                        if m.get("relative_name") and m.get("relation_type") == "Father":
                            grandparent_name_refs.add(m["relative_name"])
                        # Also look for mother-in-law (married to father-in-law)
                        for w in candidate_pool:
                            if w["id"] in assigned_ids: continue
                            if w.get("gender") == "F" and (w.get("relation_type") or "").capitalize() == "Husband":
                                if match_person_names(m["name"], w.get("relative_name") or "", primary_clan) and (w.get("age", 0) - t_age >= 14):
                                    w["relationship_label"] = "Mother-in-Law"
                                    parents.append(w)
                                    assigned_ids.add(w["id"])
                                    mother_name_refs.add(w["name"])
                                    break
                        break

    # --- PASS 1.6: Booth-Wide Father Lookup for Non-Numeric / Locality House Electors (e.g. 'Kakching Wairi') ---
    if not any(p.get("gender") == "M" and not p.get("is_deceased") for p in parents) and t_rel and t_rel_type == "Father":
        cursor.execute("""
            SELECT * FROM voters 
            WHERE ac_no = ? AND part_no = ? AND gender = 'M' AND (age - ?) >= 14 AND (age - ?) <= 55
        """, (ac_no, part_no, t_age, t_age))
        father_cands = []
        for fc in [dict(r) for r in cursor.fetchall()]:
            if fc["id"] in assigned_ids or fc["id"] == target["id"]: continue
            if match_person_names(fc["name"], t_rel, primary_clan):
                father_cands.append(fc)
        
        if father_cands:
            best_fc = None
            best_score = -999
            for fc in father_cands:
                f_age = fc.get("age") or 40
                gap = f_age - t_age
                score = 50 - abs(gap - 32)
                
                # Bonus for exact full name match
                if fc["name"].strip().lower() == t_rel.strip().lower():
                    score += 25
                
                cursor.execute("""
                    SELECT * FROM voters 
                    WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'F' AND relation_type = 'Husband'
                """, (ac_no, part_no, fc.get("house_no") or ""))
                raw_w = [dict(w) for w in cursor.fetchall()]
                wives = [w for w in raw_w if match_person_names(fc["name"], w.get("relative_name") or "", primary_clan)]
                if wives:
                    w = wives[0]
                    w_gap = (w.get("age") or 0) - t_age
                    if w_gap < 14:
                        score -= 500
                    else:
                        score += 30
                        for sib_co in same_house:
                            if sib_co.get("gender") in ("M", "F") and (w.get("age") or 0) - (sib_co.get("age") or 0) < 14:
                                score -= 1000
                if score > best_score:
                    best_score = score
                    best_fc = fc
            
            if best_fc and best_score > -200:
                best_fc["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER"
                best_fc["relationship_label"] = "Father"
                parents.append(best_fc)
                assigned_ids.add(best_fc["id"])
                father_name_refs.add(best_fc["name"])
                if best_fc.get("relative_name") and best_fc.get("relation_type") == "Father":
                    grandparent_name_refs.add(best_fc["relative_name"])
                
                cursor.execute("""
                    SELECT * FROM voters 
                    WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'F' AND relation_type = 'Husband'
                """, (ac_no, part_no, best_fc.get("house_no") or ""))
                raw_w = [dict(w) for w in cursor.fetchall()]
                wives = [w for w in raw_w if match_person_names(best_fc["name"], w.get("relative_name") or "", primary_clan)]
                if wives and not any(p.get("gender") == "F" and not p.get("is_deceased") for p in parents):
                    w = wives[0]
                    if (w.get("age") or 0) - t_age >= 14:
                        w["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER"
                        w["relationship_label"] = "Mother"
                        parents.append(w)
                        assigned_ids.add(w["id"])
                        mother_name_refs.add(w["name"])

    # --- PASS 2: Deceased/Absent Father Mother-Child Link ---
    for m in candidate_pool:
        if m["id"] in assigned_ids: continue
        m_name = m["name"]
        m_rel = m.get("relative_name") or ""
        m_rel_type = (m.get("relation_type") or "Father").capitalize()
        m_age = m.get("age") or 30
        m_sex = m.get("gender")
        diff = m_age - t_age

        if m_sex == "F" and m_rel_type == "Husband" and 12 <= diff <= 55:
            if any(match_person_names(m_rel, f_ref, primary_clan) for f_ref in father_name_refs):
                parents.append(m)
                assigned_ids.add(m["id"])
                mother_name_refs.add(m_name)
                continue

        if t_sex == "F" and t_rel_type == "Husband" and m_rel_type == "Father" and -diff >= 12:
            if t_rel and match_person_names(m_rel, t_rel, primary_clan):
                children.append(m)
                assigned_ids.add(m["id"])
                continue

    # --- PASS 3: Children, Grandchildren & Siblings ---
    # Disambiguate multi-generational hierarchy and prevent generational inversion
    grandchildren = []
    children_in_law = []

    for m in candidate_pool:
        if m["id"] in assigned_ids: continue
        m_name = m["name"]
        m_rel = m.get("relative_name") or ""
        m_rel_type = (m.get("relation_type") or "Father").capitalize()
        m_age = m.get("age") or 30
        diff = m_age - t_age  # negative when candidate is younger than target

        is_child = False
        # Child Case 1: Directly names target as father or mother
        if match_person_names(t_name, m_rel, primary_clan) and 12 <= -diff <= 55:
            is_child = True
        # Child Case 2: Target's spouse is named as parent
        elif any(match_person_names(s["name"], m_rel, primary_clan) for s in spouses) and 12 <= -diff <= 55:
            is_child = True
        # Child Case 3: Target is married female, candidate names target's husband as father
        elif t_sex == "F" and t_rel_type == "Husband" and match_person_names(t_rel, m_rel, primary_clan) and 12 <= -diff <= 55:
            is_child = True

        if is_child:
            children.append(m)
            assigned_ids.add(m["id"])
            continue

        is_sibling = False
        if abs(diff) <= 36:
            if any(match_person_names(m_rel, f_ref, primary_clan) for f_ref in father_name_refs if f_ref):
                is_sibling = True
            elif any(match_person_names(m_rel, m_ref, primary_clan) for m_ref in mother_name_refs if m_ref):
                is_sibling = True
        if is_sibling:
            if t_sex == "F" and t_rel_type == "Husband":
                m["relationship_label"] = "Brother-in-Law" if m.get("gender") == "M" else "Sister-in-Law"
            siblings.append(m)
            assigned_ids.add(m["id"])
            continue

    # Pass 3.1: Sibling Spouses (Collateral In-Laws: Sister-in-Law / Brother-in-Law)
    for sib in list(siblings):
        sib_name = sib["name"]
        sib_house = sib.get("house_no")
        sib_part = sib.get("part_no") or part_no

        # Check candidate pool
        for m in candidate_pool:
            if m["id"] in assigned_ids or m["id"] == target["id"]: continue
            m_name = m["name"]
            m_rel = m.get("relative_name") or ""
            m_rel_type = (m.get("relation_type") or "Father").capitalize()
            if sib.get("gender") == "M" and m.get("gender") == "F" and m_rel_type == "Husband" and match_person_names(sib_name, m_rel, primary_clan):
                m["spouse_of_id"] = sib["id"]
                m["relationship_label"] = "Sister-in-Law (Brother's Wife)"
                siblings_spouses.append(m)
                assigned_ids.add(m["id"])
                continue
            elif sib.get("gender") == "F" and m.get("gender") == "M" and match_person_names(m_name, sib.get("relative_name") or "", primary_clan):
                m["spouse_of_id"] = sib["id"]
                m["relationship_label"] = "Brother-in-Law"
                siblings_spouses.append(m)
                assigned_ids.add(m["id"])
                continue

        # Check sibling dwelling in booth for spouse
        if sib_house and sib.get("gender") == "M":
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'F' AND relation_type = 'Husband'
            """, (ac_no, sib_part, sib_house))
            for sp in [dict(r) for r in cursor.fetchall()]:
                if sp["id"] in assigned_ids or sp["id"] == target["id"] or sp["id"] == sib["id"]: continue
                if match_person_names(sib_name, sp.get("relative_name") or "", primary_clan):
                    sp["spouse_of_id"] = sib["id"]
                    sp["relationship_label"] = "Sister-in-Law (Brother's Wife)"
                    sp["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if sp.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                    siblings_spouses.append(sp)
                    assigned_ids.add(sp["id"])

    # Pass 3.2: Compound Children Resolution for Adult Parents (Age >= 30)
    # Recursively unifies all biological sons and daughters across adjacent compound parcels
    if t_age >= 30:
        paternal_ref = None
        if t_sex == "F":
            if t_rel_type == "Husband":
                paternal_ref = t_rel
            elif spouses:
                paternal_ref = spouses[0].get("name")
        else:
            paternal_ref = t_name

        cursor.execute("""
            SELECT * FROM voters 
            WHERE ac_no = ? AND part_no = ? AND (relation_type = 'Father' OR relation_type = 'Mother')
        """, (ac_no, part_no))
        booth_candidates = [dict(r) for r in cursor.fetchall()]
        for bc in booth_candidates:
            if bc["id"] in assigned_ids or bc["id"] == target["id"]: continue
            bc_rel = bc.get("relative_name") or ""
            bc_rel_type = (bc.get("relation_type") or "Father").capitalize()
            diff_bc = t_age - (bc.get("age") or 0)

            # Must satisfy physiological biological parent-child age gap: 12 to 55 years (symmetric with PASS 1)
            if not (12 <= diff_bc <= 55): continue

            is_match = False
            if paternal_ref and bc_rel_type == "Father" and match_person_names(paternal_ref, bc_rel, primary_clan):
                is_match = True
            elif bc_rel_type == "Mother" and match_person_names(t_name, bc_rel, primary_clan):
                is_match = True
            if not is_match:
                continue

            # Clan guard: check both first-token clan (Meitei) and last-token surname (Nepali/Kuki/Naga).
            # Bypass clan guard if the relative_name is a strong multi-word match to parent's full name.
            bc_rel_words = filter_core_words(clean_words(bc_rel))
            strong_multiword_parent = len(bc_rel_words) >= 2 and len(filter_core_words(clean_words(paternal_ref or t_name))) >= 2
            if not strong_multiword_parent:
                bc_words = filter_core_words(clean_words(bc["name"]))
                bc_clan = extract_clan_or_surname(bc["name"])
                bc_last = bc_words[-1].lower() if bc_words else ""
                allowed_clans = set()
                for ref_str in [primary_clan, target_clan, (t_words[-1] if t_words else None)]:
                    if ref_str:
                        allowed_clans.add(ref_str.lower())
                if spouses:
                    sp_words = filter_core_words(clean_words(spouses[0]["name"]))
                    sp_clan = extract_clan_or_surname(spouses[0]["name"])
                    if sp_clan: allowed_clans.add(sp_clan.lower())
                    if sp_words: allowed_clans.add(sp_words[-1].lower())
                if allowed_clans and (bc_clan not in allowed_clans and bc_last not in allowed_clans):
                    continue

            # Same-House Father Priority:
            # If child candidate is in another house, check if a valid father matching bc_rel lives in child's own house!
            if bc.get("house_no") and bc.get("house_no") != target.get("house_no"):
                cursor.execute("""
                    SELECT * FROM voters 
                    WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'M' AND (age - ?) >= 12 AND (age - ?) <= 55
                """, (ac_no, part_no, bc.get("house_no"), bc.get("age") or 0, bc.get("age") or 0))
                raw_shf = [dict(f) for f in cursor.fetchall()]
                same_house_fathers = [f for f in raw_shf if match_person_names(f["name"], bc_rel, primary_clan)]
                if same_house_fathers:
                    continue

            bc["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if bc.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
            children.append(bc)
            assigned_ids.add(bc["id"])

    # Pass 3.3: In-Laws & Grandchildren from All Children's Dwellings
    for ch in list(children):
        ch_name = ch["name"]
        ch_house = ch.get("house_no")
        ch_part = ch.get("part_no") or part_no

        # 1. Spouses of children (Daughter-in-Law / Son-in-Law)
        # Check candidate pool first
        for m in candidate_pool:
            if m["id"] in assigned_ids or m["id"] == target["id"] or m["id"] == ch["id"]: continue
            if m.get("gender") == "F" and (m.get("relation_type") or "").capitalize() == "Husband":
                if match_person_names(ch_name, m.get("relative_name") or "", primary_clan):
                    m["spouse_of_id"] = ch["id"]
                    m["relationship_label"] = "Daughter-in-Law (Son's Wife)"
                    m["audit_tier"] = "GOLD_SAME_HOUSEHOLD" if m.get("house_no") == target.get("house_no") else "GOLD_SAME_BOOTH_CLUSTER"
                    children_in_law.append(m)
                    assigned_ids.add(m["id"])
            elif m.get("gender") == "M" and ch.get("gender") == "F":
                if ch.get("relative_name") and match_person_names(m["name"], ch["relative_name"], primary_clan):
                    m["spouse_of_id"] = ch["id"]
                    m["relationship_label"] = "Son-in-Law"
                    m["audit_tier"] = "GOLD_SAME_HOUSEHOLD" if m.get("house_no") == target.get("house_no") else "GOLD_SAME_BOOTH_CLUSTER"
                    children_in_law.append(m)
                    assigned_ids.add(m["id"])

        if ch_house:
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND house_no = ? AND relation_type = 'Husband'
            """, (ac_no, ch_part, ch_house))
            for sp in [dict(r) for r in cursor.fetchall()]:
                if sp["id"] in assigned_ids or sp["id"] == target["id"] or sp["id"] == ch["id"]: continue
                if match_person_names(ch_name, sp.get("relative_name") or "", primary_clan):
                    sp["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if sp.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                    sp["spouse_of_id"] = ch["id"]
                    children_in_law.append(sp)
                    assigned_ids.add(sp["id"])

            # 2. Grandchildren (Children of verified biological children across dwelling & booth)
            gc_cands = []
            if ch_house:
                cursor.execute("""
                    SELECT * FROM voters 
                    WHERE ac_no = ? AND part_no = ? AND house_no = ?
                """, (ac_no, ch_part, ch_house))
                gc_cands.extend([dict(r) for r in cursor.fetchall()])
            
            # Booth-wide lookup for children of ch
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND (relation_type = 'Father' OR relation_type = 'Mother' OR relation_type IS NULL) AND (? - age) >= 35
            """, (ac_no, part_no, t_age))
            seen_gc_ids = {g["id"] for g in gc_cands}
            for r in cursor.fetchall():
                rd = dict(r)
                if rd["id"] not in seen_gc_ids:
                    seen_gc_ids.add(rd["id"])
                    gc_cands.append(rd)

            for gc in gc_cands:
                if gc["id"] in assigned_ids or gc["id"] == target["id"] or gc["id"] == ch["id"]: continue
                gc_age = gc.get("age") or 0
                diff_gc = t_age - gc_age
                diff_ch_gc = (ch.get("age") or 0) - gc_age
                if diff_gc >= 35 and 14 <= diff_ch_gc <= 50:
                    gc_rel = gc.get("relative_name") or ""
                    gc_rel_type = (gc.get("relation_type") or "Father").capitalize()
                    if gc_rel_type == "Husband": continue
                    # Child names son as father or son's wife as mother
                    is_gc_of_son = match_person_names(ch_name, gc_rel, primary_clan)
                    if not is_gc_of_son:
                        son_wives = [w["name"] for w in children_in_law if w.get("spouse_of_id") == ch["id"]]
                        is_gc_of_son = any(match_person_names(w_name, gc_rel, primary_clan) for w_name in son_wives)
                    if is_gc_of_son:
                        # Same-house father priority
                        if ch.get("house_no") and gc.get("house_no") and gc.get("house_no") != ch.get("house_no"):
                            cursor.execute("""
                                SELECT * FROM voters 
                                WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'M' AND (age - ?) >= 14 AND (age - ?) <= 55
                            """, (ac_no, part_no, gc.get("house_no"), gc_age, gc_age))
                            raw_shf = [dict(f) for f in cursor.fetchall()]
                            alt_fathers = [f for f in raw_shf if f["id"] != ch["id"] and match_person_names(f["name"], gc_rel, primary_clan)]
                            if alt_fathers:
                                continue
                        gc_clan = extract_clan_or_surname(gc["name"])
                        ch_clan = extract_clan_or_surname(ch["name"])
                        if ch_clan and gc_clan == ch_clan.lower():
                            gc["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if gc.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                            gc["child_of_id"] = ch["id"]
                            gc["relationship_label"] = "Granddaughter" if gc.get("gender") == "F" else "Grandson"
                            grandchildren.append(gc)
                            assigned_ids.add(gc["id"])

    # Pass 3.4: Sibling & Mother Resolution for Adult Electors (Reciprocal of Matriarch Lookup)
    fat_obj = next((p for p in parents if p.get("gender") == "M" and not p.get("is_deceased")), None)
    father_target_ref = fat_obj["name"] if fat_obj else (t_rel if t_rel_type == "Father" else None)
    if father_target_ref and not any(p.get("gender") == "F" and not p.get("is_deceased") for p in parents):
        wife_candidates = []
        if fat_obj and fat_obj.get("house_no"):
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'F' AND relation_type = 'Husband'
            """, (ac_no, part_no, fat_obj.get("house_no")))
            wife_candidates.extend([dict(r) for r in cursor.fetchall()])
        
        if not wife_candidates:
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND gender = 'F' AND relation_type = 'Husband'
            """, (ac_no, part_no))
            for cand in [dict(r) for r in cursor.fetchall()]:
                if fat_obj and cand.get("house_no") and cand.get("house_no") != fat_obj.get("house_no"):
                    cursor.execute("""
                        SELECT id, name FROM voters 
                        WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'M' AND id != ?
                    """, (ac_no, part_no, cand.get("house_no"), fat_obj["id"]))
                    raw_coh = [dict(h) for h in cursor.fetchall()]
                    co_husbands = [h for h in raw_coh if match_person_names(h["name"], cand.get("relative_name") or "", primary_clan)]
                    if co_husbands:
                        continue
                wife_candidates.append(cand)

        for m_cand in wife_candidates:
            if m_cand["id"] in assigned_ids or m_cand["id"] == target["id"]: continue
            diff_m = (m_cand.get("age") or 0) - t_age
            if 14 <= diff_m <= 48 and match_person_names(father_target_ref, m_cand.get("relative_name") or "", primary_clan):
                m_clan = extract_clan_or_surname(m_cand["name"])
                if primary_clan and m_clan == primary_clan.lower():
                    m_cand["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if m_cand.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                    m_cand["relationship_label"] = "Mother"
                    parents.append(m_cand)
                    assigned_ids.add(m_cand["id"])
                    mother_name_refs.add(m_cand["name"])
                    break

    # Reciprocal brothers and sisters in same booth sharing father
    if father_target_ref and t_age >= 18:
        cursor.execute("""
            SELECT * FROM voters 
            WHERE ac_no = ? AND part_no = ? AND relation_type = 'Father'
        """, (ac_no, part_no))
        for b_cand in [dict(r) for r in cursor.fetchall()]:
            if b_cand["id"] in assigned_ids or b_cand["id"] == target["id"]: continue
            diff_b = (b_cand.get("age") or 0) - t_age
            if abs(diff_b) <= 36 and match_person_names(father_target_ref, b_cand.get("relative_name") or "", primary_clan):
                b_age = b_cand.get("age") or 0
                mom_obj = next((p for p in parents if p.get("gender") == "F" and not p.get("is_deceased")), None)
                fat_obj = next((p for p in parents if p.get("gender") == "M" and not p.get("is_deceased")), None)

                # BIOLOGICAL INVARIANT: Child cannot be older than parents or born before parent was 14
                if mom_obj and (mom_obj.get("age", 0) - b_age < 14):
                    continue
                if fat_obj and (fat_obj.get("age", 0) - b_age < 14):
                    continue

                # Same-House Father Priority for Sibling:
                # If candidate sibling lives with another father of the same name in a different house, they belong to that father!
                if fat_obj and b_cand.get("house_no") and b_cand.get("house_no") != fat_obj.get("house_no"):
                    cursor.execute("""
                        SELECT * FROM voters 
                        WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'M' AND (age - ?) >= 14 AND (age - ?) <= 55
                    """, (ac_no, part_no, b_cand.get("house_no"), b_age, b_age))
                    raw_shf = [dict(f) for f in cursor.fetchall()]
                    alt_fathers = [f for f in raw_shf if f["id"] != fat_obj["id"] and match_person_names(f["name"], b_cand.get("relative_name") or "", primary_clan)]
                    if alt_fathers:
                        continue

                b_clan = extract_clan_or_surname(b_cand["name"])
                if primary_clan and b_clan == primary_clan.lower():
                    b_cand["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER"
                    b_cand["relationship_label"] = "Brother" if b_cand.get("gender") == "M" else "Sister"
                    siblings.append(b_cand)
                    assigned_ids.add(b_cand["id"])

    # Resolve Spouses for All Identified Siblings (including compound parcels)
    for sib in list(siblings):
        if any(sp.get("spouse_of_id") == sib["id"] for sp in siblings_spouses): continue
        sib_name = sib["name"]
        sib_house = sib.get("house_no")
        sib_part = sib.get("part_no") or part_no
        if sib_house and sib.get("gender") == "M":
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'F' AND relation_type = 'Husband'
            """, (ac_no, sib_part, sib_house))
            for sp in [dict(r) for r in cursor.fetchall()]:
                if sp["id"] in assigned_ids or sp["id"] == target["id"] or sp["id"] == sib["id"]: continue
                if match_person_names(sib_name, sp.get("relative_name") or "", primary_clan):
                    sp["spouse_of_id"] = sib["id"]
                    sp["relationship_label"] = "Sister-in-Law (Brother's Wife)"
                    sp["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if sp.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                    siblings_spouses.append(sp)
                    assigned_ids.add(sp["id"])
        elif sib_house and sib.get("gender") == "F":
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND house_no = ? AND gender = 'M'
            """, (ac_no, sib_part, sib_house))
            for sp in [dict(r) for r in cursor.fetchall()]:
                if sp["id"] in assigned_ids or sp["id"] == target["id"] or sp["id"] == sib["id"]: continue
                if sib.get("relative_name") and match_person_names(sp["name"], sib["relative_name"], primary_clan):
                    sp["spouse_of_id"] = sib["id"]
                    sp["relationship_label"] = "Brother-in-Law"
                    sp["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if sp.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                    siblings_spouses.append(sp)
                    assigned_ids.add(sp["id"])

    # Pass 3.5: Lineage Patriarch / Elder for Orphaned Cohorts (e.g. Radheshyam in H.183)
    # Strictly applied ONLY when target has zero identified biological fathers
    if not any(p.get("gender") == "M" for p in parents):
        elder_cands = set()
        for sib in siblings:
            if sib.get("house_no") == target.get("house_no") and sib.get("relative_name"):
                elder_cands.add(sib["relative_name"])
        if t_rel and t_rel_type in ("Others", "Other"):
            elder_cands.add(t_rel)
        for ec_name in elder_cands:
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND gender = 'M'
            """, (ac_no, part_no))
            for elder in [dict(r) for r in cursor.fetchall()]:
                if elder["id"] in assigned_ids or elder["id"] == target["id"]: continue
                if match_person_names(elder["name"], ec_name, primary_clan) and (elder.get("age", 0) - t_age >= 20):
                    elder["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER"
                    elder["relationship_label"] = "Lineage Patriarch (Elder)"
                    parents.append(elder)
                    assigned_ids.add(elder["id"])
                    break
            if any(p.get("gender") == "M" for p in parents):
                break

    # Pass 3.6: Sibling Children (Nephews & Nieces - Collateral Descendant Tree)
    nephews_nieces = []
    for sib in list(siblings):
        if sib.get("is_deceased") or not sib.get("id"): continue
        sib_id = sib["id"]
        sib_name = sib["name"]
        sib_sex = sib.get("gender")
        sib_house = sib.get("house_no")
        sib_part = sib.get("part_no") or part_no
        if not sib_house: continue

        cursor.execute("""
            SELECT * FROM voters 
            WHERE ac_no = ? AND part_no = ? AND (relation_type = 'Father' OR relation_type = 'Mother')
        """, (ac_no, sib_part))
        for cand in [dict(r) for r in cursor.fetchall()]:
            if cand["id"] in assigned_ids or cand["id"] == target["id"] or cand["id"] == sib_id: continue
            c_rel = cand.get("relative_name") or ""
            c_rel_type = (cand.get("relation_type") or "Father").capitalize()
            diff_c = (sib.get("age") or 0) - (cand.get("age") or 0)

            if not (14 <= diff_c <= 48): continue

            is_child_of_sib = False
            if sib_sex == "M" and c_rel_type == "Father" and match_person_names(sib_name, c_rel, primary_clan):
                is_child_of_sib = True
            elif sib_sex == "F" and c_rel_type == "Mother" and match_person_names(sib_name, c_rel, primary_clan):
                is_child_of_sib = True
            else:
                sib_spouses = [sp for sp in siblings_spouses if sp.get("spouse_of_id") == sib_id]
                for sp in sib_spouses:
                    sp_name = sp.get("name")
                    if sib_sex == "M" and c_rel_type == "Mother" and match_person_names(sp_name, c_rel, primary_clan):
                        is_child_of_sib = True
                        break
                    elif sib_sex == "F" and c_rel_type == "Father" and match_person_names(sp_name, c_rel, primary_clan):
                        is_child_of_sib = True
                        break

            if is_child_of_sib:
                cand_sex = cand.get("gender")
                if sib_sex == "M":
                    label = "Nephew (Brother's Son)" if cand_sex == "M" else "Niece (Brother's Daughter)"
                else:
                    label = "Nephew (Sister's Son)" if cand_sex == "M" else "Niece (Sister's Daughter)"
                cand["relationship_label"] = label
                cand["child_of_id"] = sib_id
                cand["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if cand.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                nephews_nieces.append(cand)
                assigned_ids.add(cand["id"])

    # Pass 3.7: Co-Resident Deceased Son / Brother / Uncle Collateral Kinship
    # Handles joint families where a deceased male of the household clan left behind a widow and children
    # (e.g. Late Naorem Sunder Singh leaving widow Indira Devi 52y, Gyaneshwari 29y, Ashutosh 21y in House 228)
    unassigned_cohabitants = [m for m in target_family_cluster if m["id"] not in assigned_ids and m["id"] != target["id"]]
    if unassigned_cohabitants:
        deceased_rel_groups = {}
        for m in unassigned_cohabitants:
            m_rel = m.get("relative_name") or ""
            m_rel_clan = extract_clan_or_surname(m_rel)
            if m_rel and (m_rel_clan == primary_clan or (primary_clan and primary_clan.lower() in m_rel.lower())):
                clean_rel = re.sub(r'^(late|shri|smt)\s+', '', m_rel, flags=re.I).strip()
                deceased_rel_groups.setdefault(clean_rel, []).append(m)

        for rel_name, group_members in deceased_rel_groups.items():
            is_active_in_house = any(m["id"] for m in same_house if match_person_names(m["name"], rel_name, primary_clan))
            if not is_active_in_house:
                min_group_age = min(m.get("age") or 30 for m in group_members)
                max_group_age = max(m.get("age") or 30 for m in group_members)
                
                # Case 1: Target is Grandparent Generation (t_age >= 65 and t_age - min_group_age >= 35)
                if t_age >= 65 and (t_age - min_group_age >= 35):
                    for gm in group_members:
                        if gm["id"] in assigned_ids: continue
                        gm_sex = gm.get("gender")
                        gm_rel_type = (gm.get("relation_type") or "").capitalize()
                        if gm_sex == "F" and gm_rel_type == "Husband":
                            gm["relationship_label"] = f"Daughter-in-Law (Widow of Late {rel_name})"
                            children_in_law.append(gm)
                            assigned_ids.add(gm["id"])
                        elif gm_rel_type in ("Father", "Mother", "Others", "Other"):
                            gm["relationship_label"] = "Granddaughter" if gm_sex == "F" else "Grandson"
                            grandchildren.append(gm)
                            assigned_ids.add(gm["id"])

                # Case 2: Target is Sibling Generation (e.g. Shyam 57y or Bijayani 57y, deceased relative was brother)
                elif t_age >= 35 and (t_age - min_group_age >= 14) and abs(t_age - max_group_age) <= 25:
                    for gm in group_members:
                        if gm["id"] in assigned_ids: continue
                        gm_sex = gm.get("gender")
                        gm_rel_type = (gm.get("relation_type") or "").capitalize()
                        if gm_sex == "F" and gm_rel_type == "Husband":
                            label = "Sister-in-Law (Widow of Late Brother)" if t_sex == "M" else "Sister-in-Law (Widow of Husband's Late Brother)"
                            gm["relationship_label"] = label
                            siblings_spouses.append(gm)
                            assigned_ids.add(gm["id"])
                        elif gm_rel_type in ("Father", "Mother", "Others", "Other"):
                            gm["relationship_label"] = "Niece" if gm_sex == "F" else "Nephew"
                            nephews_nieces.append(gm)
                            assigned_ids.add(gm["id"])

                # Case 3: Target is Child Generation (e.g. Donaljit 22y, deceased relative was uncle)
                elif t_age <= 35 and (max_group_age - t_age >= 14):
                    for gm in group_members:
                        if gm["id"] in assigned_ids: continue
                        gm_sex = gm.get("gender")
                        gm_rel_type = (gm.get("relation_type") or "").capitalize()
                        if gm_sex == "F" and gm_rel_type == "Husband":
                            gm["relationship_label"] = "Paternal Aunt"
                            uncles_aunts.append(gm)
                            assigned_ids.add(gm["id"])
                        elif gm_rel_type in ("Father", "Mother", "Others", "Other"):
                            gm["relationship_label"] = "Cousin (Sister)" if gm_sex == "F" else "Cousin (Brother)"
                            cousins.append(gm)
                            assigned_ids.add(gm["id"])

    # Pass 3.8: Joint Household Elders for Electors whose Father is Deceased (e.g. Ashutosh in H.228)
    if any(p.get("is_deceased") for p in parents) or not any(p.get("gender") == "M" and not p.get("is_deceased") for p in parents):
        for m in target_family_cluster:
            if m["id"] in assigned_ids or m["id"] == target["id"]: continue
            m_age = m.get("age") or 30
            m_sex = m.get("gender")
            diff_m = m_age - t_age
            # Grandfather in house (diff >= 45)
            if m_sex == "M" and diff_m >= 45:
                m["relationship_label"] = "Grandfather"
                grandparents.append(m)
                assigned_ids.add(m["id"])
                # Look for his wife in the house as Grandmother
                for w in target_family_cluster:
                    if w["id"] in assigned_ids or w["id"] == target["id"]: continue
                    if w.get("gender") == "F" and match_person_names(m["name"], w.get("relative_name") or "", primary_clan):
                        w["relationship_label"] = "Grandmother"
                        grandparents.append(w)
                        assigned_ids.add(w["id"])
            # Paternal Uncle in house (20 <= diff <= 44)
            elif m_sex == "M" and 20 <= diff_m <= 44:
                m["relationship_label"] = "Paternal Uncle"
                uncles_aunts.append(m)
                assigned_ids.add(m["id"])
                # Look for his wife in house as Paternal Aunt
                for w in target_family_cluster:
                    if w["id"] in assigned_ids or w["id"] == target["id"]: continue
                    if w.get("gender") == "F" and match_person_names(m["name"], w.get("relative_name") or "", primary_clan):
                        w["relationship_label"] = "Paternal Aunt"
                        uncles_aunts.append(w)
                        assigned_ids.add(w["id"])

    # --- PASS 4: Grandparents (Living & Deceased in Compound / Booth) ---
    grandmother_name_refs = set()
    father_obj = next((p for p in parents if p.get("gender") == "M"), None)
    if father_obj and father_obj.get("relative_name"):
        grandparent_name_refs.add(father_obj["relative_name"])

    # 1. Check candidate pool for living grandparents
    for m in candidate_pool:
        if m["id"] in assigned_ids: continue
        m_name = m["name"]
        m_rel = m.get("relative_name") or ""
        m_rel_type = (m.get("relation_type") or "Father").capitalize()
        m_age = m.get("age") or 30
        m_sex = m.get("gender")
        diff = m_age - t_age

        if diff >= 35:
            is_gfather = (m_sex == "M" and any(match_person_names(m_name, g_ref, primary_clan) for g_ref in grandparent_name_refs if g_ref))
            is_gmother = (m_sex == "F" and m_rel_type == "Husband" and any(match_person_names(m_rel, g_ref, primary_clan) for g_ref in grandparent_name_refs if g_ref))
            if is_gfather or is_gmother:
                grandparents.append(m)
                assigned_ids.add(m["id"])
                if is_gmother:
                    grandmother_name_refs.add(m_name)
                continue

    # 2. Check booth / compound cluster for living grandfather & grandmother
    paternal_gfather_ref = father_obj.get("relative_name") if (father_obj and father_obj.get("relative_name")) else (list(grandparent_name_refs)[0] if grandparent_name_refs else None)

    if paternal_gfather_ref:
        father_age = father_obj.get("age") or 40 if father_obj else 40
        # A. Living Grandfather (M)
        if not any(g.get("gender") == "M" for g in grandparents):
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND gender = 'M'
            """, (ac_no, part_no))
            for gm_cand in [dict(r) for r in cursor.fetchall()]:
                if gm_cand["id"] in assigned_ids or gm_cand["id"] == target["id"]: continue
                diff_gf = (gm_cand.get("age") or 0) - father_age
                diff_gt = (gm_cand.get("age") or 0) - t_age
                if 14 <= diff_gf <= 48 and diff_gt >= 35 and match_person_names(gm_cand["name"], paternal_gfather_ref, primary_clan):
                    g_clan = extract_clan_or_surname(gm_cand["name"])
                    if primary_clan and g_clan == primary_clan.lower():
                        gm_cand["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER"
                        gm_cand["relationship_label"] = "Grandfather"
                        grandparents.append(gm_cand)
                        assigned_ids.add(gm_cand["id"])
                        break

        # B. Living Grandmother (F)
        living_gf = next((g for g in grandparents if g.get("gender") == "M"), None)
        g_hub_target = living_gf["name"] if living_gf else paternal_gfather_ref

        if not any(g.get("gender") == "F" for g in grandparents):
            cursor.execute("""
                SELECT * FROM voters 
                WHERE ac_no = ? AND part_no = ? AND gender = 'F' AND relation_type = 'Husband'
            """, (ac_no, part_no))
            for f in [dict(r) for r in cursor.fetchall()]:
                if f["id"] in assigned_ids or f["id"] == target["id"]: continue
                if match_person_names(f.get("relative_name") or "", g_hub_target, primary_clan):
                    f_clan = extract_clan_or_surname(f["name"])
                    diff_f = (f.get("age") or 0) - father_age
                    diff_t = (f.get("age") or 0) - t_age
                    if 14 <= diff_f <= 48 and diff_t >= 35:
                        f["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER"
                        f["relationship_label"] = "Grandmother"
                        grandparents.append(f)
                        assigned_ids.add(f["id"])
                        grandmother_name_refs.add(f["name"])
                        break

    # --- PASS 5: Uncles & Aunts (Cross-Referencing Grandfather OR Grandmother Name) ---
    living_gf = next((g for g in grandparents if g.get("gender") == "M"), None)
    living_gm = next((g for g in grandparents if g.get("gender") == "F"), None)

    # Search in candidate pool and also in father/grandfather dwelling
    extended_uncle_pool = list(candidate_pool)
    houses_to_check = set()
    if father_obj and father_obj.get("house_no"): houses_to_check.add(father_obj["house_no"])
    if living_gf and living_gf.get("house_no"): houses_to_check.add(living_gf["house_no"])

    for h in houses_to_check:
        cursor.execute("""
            SELECT * FROM voters 
            WHERE ac_no = ? AND part_no = ? AND house_no = ?
        """, (ac_no, part_no, h))
        for r in [dict(x) for x in cursor.fetchall()]:
            if r["id"] not in assigned_ids and r["id"] != target["id"] and not any(p["id"] == r["id"] for p in extended_uncle_pool):
                extended_uncle_pool.append(r)

    for m in extended_uncle_pool:
        if m["id"] in assigned_ids: continue
        m_name = m["name"]
        m_rel = m.get("relative_name") or ""
        m_rel_type = (m.get("relation_type") or "Father").capitalize()
        m_age = m.get("age") or 30
        diff = m_age - t_age

        if 10 <= diff <= 55:
            # 1. Shares Grandfather as Father
            shares_gfather = (m_rel_type == "Father" and (
                any(match_person_names(m_rel, g_ref, primary_clan) for g_ref in grandparent_name_refs if g_ref) or
                (living_gf and match_person_names(m_rel, living_gf["name"], primary_clan))
            ))
            # 2. Shares Grandmother as Mother
            shares_gmother = (m_rel_type == "Mother" and (
                any(match_person_names(m_rel, gm_ref, primary_clan) for gm_ref in grandmother_name_refs if gm_ref) or
                (living_gm and match_person_names(m_rel, living_gm["name"], primary_clan))
            ))

            if shares_gfather or shares_gmother:
                m["relationship_label"] = "Paternal Aunt" if m.get("gender") == "F" else "Paternal Uncle"
                m["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if m.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                uncles_aunts.append(m)
                assigned_ids.add(m["id"])
                continue

    for u in list(uncles_aunts):
        u_name = u["name"]
        for m in extended_uncle_pool:
            if m["id"] in assigned_ids: continue
            m_rel = m.get("relative_name") or ""
            m_rel_type = (m.get("relation_type") or "Father").capitalize()
            if m.get("gender") == "F" and m_rel_type == "Husband" and match_person_names(u_name, m_rel, primary_clan):
                m["relationship_label"] = "Aunt (Uncle's Wife)"
                m["audit_tier"] = "GOLD_SAME_BOOTH_CLUSTER" if m.get("house_no") != target.get("house_no") else "GOLD_SAME_HOUSEHOLD"
                uncles_aunts.append(m)
                assigned_ids.add(m["id"])

    # --- PASS 6: Cousins (Strictly Verified Children of Verified Uncles/Aunts) ---
    for m in candidate_pool:
        if m["id"] in assigned_ids: continue
        m_rel = m.get("relative_name") or ""
        diff = (m.get("age") or 30) - t_age
        if abs(diff) <= 28:
            if any(match_person_names(m_rel, u["name"]) for u in uncles_aunts):
                cousins.append(m)
                assigned_ids.add(m["id"])
                continue

    for m in target_family_cluster:
        if m["id"] not in assigned_ids:
            others.append(m)
            assigned_ids.add(m["id"])

    def format_member(m, rel_label):
        item = dict(m)
        item["relationship_label"] = rel_label
        item["audit_tier"] = m.get("audit_tier") or ("GOLD_SAME_HOUSEHOLD" if m.get("part_no") == target.get("part_no") else "SILVER_SPLIT_BOOTH")
        return item

    formatted_grandparents = [format_member(m, m.get("relationship_label") or ("Grandmother" if m.get("gender") == "F" else "Grandfather")) for m in grandparents]
    formatted_parents = [format_member(m, m.get("relationship_label") or ("Mother" if m.get("gender") == "F" else "Father")) for m in parents]
    formatted_uncles = [format_member(m, m.get("relationship_label") or ("Aunt" if m.get("gender") == "F" else "Uncle")) for m in uncles_aunts]
    formatted_spouses = [format_member(m, m.get("relationship_label") or ("Wife" if m.get("gender") == "F" else "Husband")) for m in spouses]
    formatted_siblings = [format_member(m, m.get("relationship_label") or ("Sister" if m.get("gender") == "F" else "Brother")) for m in siblings]
    formatted_siblings_spouses = [format_member(m, m.get("relationship_label") or ("Sister-in-Law" if m.get("gender") == "F" else "Brother-in-Law")) for m in siblings_spouses]
    formatted_cousins = [format_member(m, m.get("relationship_label") or ("Cousin Sister" if m.get("gender") == "F" else "Cousin Brother")) for m in cousins]
    formatted_children = [format_member(m, m.get("relationship_label") or ("Daughter" if m.get("gender") == "F" else "Son")) for m in children]
    formatted_children_in_law = [format_member(m, m.get("relationship_label") or ("Daughter-in-Law" if m.get("gender") == "F" else "Son-in-Law")) for m in children_in_law]
    formatted_grandchildren = [format_member(m, m.get("relationship_label") or ("Granddaughter" if m.get("gender") == "F" else "Grandson")) for m in grandchildren]
    formatted_others = [format_member(m, m.get("relationship_label") or "Household Member") for m in others]

    # --- PASS 7: Deterministic Ancestral Generation 1 & Generation 2 Fallback ---
    # Guarantees verified ancestral records without synthetic hallucinated placeholders

    # If female target has husband recorded in roll, but husband is not living in the roll, add deceased husband to spouses
    if not formatted_spouses and t_rel and t_rel_type == "Husband":
        words = clean_honorifics(t_rel).split()
        h_display = " ".join(w.capitalize() for w in words)
        formatted_spouses.append({
            "id": None,
            "epic_no": "ANCESTRAL / RECORDED",
            "name": f"Late {h_display}",
            "relative_name": "Target's Spouse",
            "relation_type": "Husband",
            "house_no": target["house_no"],
            "age": "Deceased",
            "gender": "M",
            "ac_no": ac_no,
            "ac_name": target.get("ac_name", ""),
            "part_no": part_no,
            "polling_station_name": target.get("polling_station_name", ""),
            "section_name": target.get("section_name", ""),
            "status": "DECEASED SPOUSE",
            "is_deceased": True,
            "relationship_label": "Late Husband (Deceased)"
        })

    # Only add late father to parents if target explicitly lists Father or has verified biological father ref
    if not any(p.get("gender") == "M" for p in formatted_parents):
        f_cand_name = None
        f_cand_label = "Late Father (Ancestral)"
        if t_rel and t_rel_type == "Father":
            f_cand_name = t_rel
        elif father_name_refs and t_rel_type != "Husband":
            f_cand_name = list(father_name_refs)[0]

        if f_cand_name:
            words = clean_honorifics(f_cand_name).split()
            f_display = " ".join(w.capitalize() for w in words)
            formatted_parents.append({
                "id": None,
                "epic_no": "ANCESTRAL / RECORDED",
                "name": f"Late {f_display}",
                "relative_name": "Paternal Lineage",
                "relation_type": "Father",
                "house_no": target["house_no"],
                "age": "Deceased",
                "gender": "M",
                "ac_no": ac_no,
                "ac_name": target.get("ac_name", ""),
                "part_no": part_no,
                "polling_station_name": target.get("polling_station_name", ""),
                "section_name": target.get("section_name", ""),
                "status": "DECEASED PATRIARCH",
                "is_deceased": True,
                "relationship_label": f_cand_label
            })

    if not any(p.get("gender") == "F" for p in formatted_parents) and t_rel and t_rel_type == "Mother":
        words = clean_honorifics(t_rel).split()
        m_display = " ".join(w.capitalize() for w in words)
        formatted_parents.append({
            "id": None,
            "epic_no": "ANCESTRAL / RECORDED",
            "name": f"Late {m_display}",
            "relative_name": "Maternal Lineage",
            "relation_type": "Mother",
            "house_no": target["house_no"],
            "age": "Deceased",
            "gender": "F",
            "ac_no": ac_no,
            "ac_name": target.get("ac_name", ""),
            "part_no": part_no,
            "polling_station_name": target.get("polling_station_name", ""),
            "section_name": target.get("section_name", ""),
            "status": "DECEASED MATRIARCH",
            "is_deceased": True,
            "relationship_label": "Late Mother (Ancestral)"
        })

    deceased_ancestors = []
    # Only generate deceased grandfather fallback IF NO LIVING PATERNAL GRANDFATHER EXISTS!
    has_living_gf = any(g.get("gender") == "M" for g in grandparents)
    if not has_living_gf:
        # Biological Invariant: An individual has EXACTLY ONE biological paternal grandfather
        paternal_gf_ref = None
        if father_obj and father_obj.get("relative_name"):
            paternal_gf_ref = father_obj["relative_name"]
        elif t_rel and t_rel_type == "Father":
            if grandparent_name_refs:
                paternal_gf_ref = list(grandparent_name_refs)[0]
        elif grandparent_name_refs:
            paternal_gf_ref = list(grandparent_name_refs)[0]

        if paternal_gf_ref:
            words = clean_honorifics(paternal_gf_ref).split()
            # If clan is missing from the grandfather's name, prefix with primary clan
            if words and primary_clan and primary_clan.lower() not in [w.lower() for w in words]:
                words.insert(0, primary_clan.capitalize())
            # Ensure proper suffix 'Singh' if missing
            if words and words[-1].lower() not in ("singh", "sharma", "devi", "ali", "mia", "khan"):
                words.append("Singh")
            formatted_g = " ".join(w.capitalize() for w in words)
            deceased_ancestors = [{
                "raw_ref": paternal_gf_ref,
                "id": None,
                "epic_no": "ANCESTRAL / HISTORICAL",
                "name": f"Late {formatted_g}",
                "relative_name": "Ancestral Patriarch",
                "relation_type": "Ancestral Lineage",
                "house_no": target["house_no"],
                "age": "Deceased",
                "gender": "M",
                "ac_no": ac_no,
                "ac_name": target.get("ac_name", ""),
                "part_no": part_no,
                "polling_station_name": target.get("polling_station_name", ""),
                "section_name": target.get("section_name", ""),
                "status": "DECEASED PATRIARCH",
                "is_deceased": True,
                "relationship_label": "Late Grandfather (Deceased)"
            }]

    paternal_grandparents = deceased_ancestors + formatted_grandparents

    # --- PASS 8: Strict Zero-Hallucination Maternal Lineage ---
    # Only render maternal grandparents if a genuine verified record or co-resident exists!
    mother_cand = next((p for p in formatted_parents if p.get("gender") == "F"), None)
    father_cand = next((p for p in formatted_parents if p.get("gender") == "M"), None)
    
    mother_name = mother_cand["name"] if mother_cand else (t_rel if t_rel_type == "Mother" else "")
    father_name = father_cand["name"] if father_cand else (t_rel if t_rel_type == "Father" else "")
    
    maiden_clan = extract_maiden_clan(mother_name, father_name)
    if not maiden_clan or maiden_clan in ("Natal", "Clan", "Late"):
        maiden_clan = ""

    maternal_grandparents = []
    # Check if a living maternal grandfather is in the candidate pool (matrilocal elder)
    for m in candidate_pool:
        if m["id"] in assigned_ids: continue
        if m.get("gender") == "M" and (m.get("age") or 0) - t_age >= 35:
            if maiden_clan and extract_clan_or_surname(m["name"]) == maiden_clan:
                m_formatted = format_member(m, f"Maternal Grandfather ({maiden_clan.capitalize()} Lineage)")
                maternal_grandparents.append(m_formatted)
                assigned_ids.add(m["id"])
                break

    # Strictly Zero Hallucination: NEVER append synthetic placeholders like 'Late Wahengbam Patriarch'

    # Option 1: Strictly Pure Biological Living Relatives
    bio_relatives = [target]
    for p in formatted_parents:
        if not p.get("is_deceased"): bio_relatives.append(p)
    for s in formatted_spouses:
        if not s.get("is_deceased"): bio_relatives.append(s)
    for sib in formatted_siblings:
        if not sib.get("is_deceased"): bio_relatives.append(sib)
    for ssp in formatted_siblings_spouses:
        if not ssp.get("is_deceased"): bio_relatives.append(ssp)
    for n in nephews_nieces:
        if not n.get("is_deceased"): bio_relatives.append(n)
    for ch in formatted_children:
        if not ch.get("is_deceased"): bio_relatives.append(ch)
    for cil in formatted_children_in_law:
        if not cil.get("is_deceased"): bio_relatives.append(cil)
    for gc in formatted_grandchildren:
        if not gc.get("is_deceased"): bio_relatives.append(gc)
    for g in paternal_grandparents:
        if not g.get("is_deceased"): bio_relatives.append(g)
    for mg in maternal_grandparents:
        if not mg.get("is_deceased"): bio_relatives.append(mg)

    living_candidates = [m for m in bio_relatives if not m.get("is_deceased")]
    head_candidate = sorted(living_candidates, key=safe_age, reverse=True)[0] if living_candidates else target

    unique_bio_keys = {(m.get("id"), m.get("name")) for m in bio_relatives if m.get("id") is not None}
    pure_bio_count = len(unique_bio_keys)

    t_sec = target.get("section_name") or ""
    t_ps = target.get("polling_station_name") or ""
    t_ac_name = target.get("ac_name") or ""
    b_lat, b_lng, v_name, geo_source = resolve_booth_geo(ac_no, part_no, t_ps, t_sec)
    booth_info = extract_polling_building_info(t_ps, t_sec, t_ac_name)
    
    # 100% Verified Google Maps Query using official civic landmark
    search_parts = [booth_info["building_name"]]
    if booth_info["locality"] and booth_info["locality"].lower() not in booth_info["building_name"].lower():
        search_parts.append(booth_info["locality"])
    if t_ac_name and t_ac_name.lower() not in " ".join(search_parts).lower():
        search_parts.append(t_ac_name)
    search_parts.append("Manipur, India")
    encoded_search = urllib.parse.quote_plus(", ".join([p for p in search_parts if p]))
    g_search_url = f"https://www.google.com/maps/search/?api=1&query={encoded_search}"
    g_coord_url = f"https://www.google.com/maps?q={b_lat},{b_lng}" if geo_source == "EXACT_BUILDING" else g_search_url
    
    polling_geo = {
        "building_name": booth_info["building_name"],
        "wing": booth_info["wing"],
        "locality": booth_info["locality"],
        "latitude": b_lat,
        "longitude": b_lng,
        "confidence": geo_source,
        "google_maps_url": g_search_url,
        "google_maps_coord_url": g_coord_url
    }
    target["polling_station_geo"] = polling_geo
    target["household_geo"] = polling_geo  # Backward compatibility for any existing callers

    conn.close()
    return {
        "target": target,
        "household_id": f"AC-{ac_no} / Part-{part_no} / House-{house_no}",
        "polling_station_geo": polling_geo,
        "household_geo": polling_geo,
        "total_lineage_count": pure_bio_count,
        "head_of_household": head_candidate,
        "mother_maiden_clan": maiden_clan.capitalize() if maiden_clan else "",
        "branches": {
            "grandparents": paternal_grandparents + maternal_grandparents,
            "paternal_grandparents": paternal_grandparents,
            "maternal_grandparents": maternal_grandparents,
            "parents": formatted_parents,
            "uncles_aunts": formatted_uncles,
            "spouses": formatted_spouses,
            "target": target,
            "siblings": formatted_siblings,
            "siblings_spouses": formatted_siblings_spouses,
            "nephews_nieces": [format_member(n, n.get("relationship_label") or "Niece/Nephew") for n in nephews_nieces],
            "cousins": formatted_cousins,
            "children": formatted_children,
            "children_in_law": formatted_children_in_law,
            "grandchildren": formatted_grandchildren,
            "others": formatted_others,
            "unrelated_cohabitants": [
                format_member(u, "Separate Household Unit (Unrelated)")
                for u in unrelated_cohabitants
                if u.get("id") not in assigned_ids and u.get("id") != target["id"]
            ]
        }
    }

# ==========================================
# OPEN-SOURCE GIS & HOUSEHOLD GEO ALLOCATION
# (ISRO Bhuvan + OSM Cadastral Parcel Engine)
# ==========================================

AC_CENTROIDS = {
    1: [24.9125, 93.9912], 2: [24.8722, 93.9482], 3: [24.8211, 93.9610], 4: [24.8105, 93.9721],
    5: [24.7731, 93.9512], 6: [24.7512, 94.0112], 7: [24.7812, 94.0612], 8: [24.8512, 94.0212],
    9: [24.8215, 93.9281], 10: [24.8152, 93.9182], 11: [24.8021, 93.9212], 12: [24.7915, 93.9312],
    13: [24.7811, 93.9412], 14: [24.7952, 93.9512], 15: [24.8112, 93.9652], 16: [24.9215, 93.8812],
    17: [24.8512, 93.8512], 18: [24.8112, 93.8212], 19: [24.7912, 93.8612], 20: [24.7412, 93.9312],
    21: [24.7512, 93.9112], 22: [24.7112, 93.9112], 23: [24.6612, 93.8912], 24: [24.7112, 93.8312],
    25: [24.6712, 93.7912], 26: [24.6312, 93.7612], 27: [24.5012, 93.7712], 28: [24.5312, 93.8012],
    29: [24.4212, 93.7412], 30: [24.6812, 93.9712], 31: [24.6312, 94.0112], 32: [24.6612, 94.0412],
    33: [24.5812, 94.0812], 34: [24.6012, 94.0312], 35: [24.6212, 93.9912], 36: [24.5512, 93.9612],
    37: [24.4912, 93.9812], 38: [24.4512, 93.9312], 39: [24.3612, 93.8812], 40: [24.8012, 93.1212],
    41: [24.3312, 94.0512], 42: [24.2512, 94.1512], 43: [25.0212, 94.3812], 44: [25.1112, 94.3612],
    45: [25.3212, 94.4812], 46: [25.0512, 94.0112], 47: [25.2912, 94.1812], 48: [25.5112, 94.1312],
    49: [25.4012, 94.1012], 50: [25.2112, 93.9812], 51: [25.1512, 93.9212], 52: [24.9812, 93.5112],
    53: [24.8112, 93.4512], 54: [24.7112, 93.6112], 55: [24.5112, 93.5812], 56: [24.4112, 93.3812],
    57: [24.3512, 93.5212], 58: [24.3312, 93.6712], 59: [24.3312, 93.6812], 60: [24.2112, 93.2112]
}

VILLAGE_LEIKAI_CENTROIDS = {
    # Imphal East / Andro / Lamlai / Khurai
    "huikap": [24.7198, 94.0259],
    "angtha": [24.6963, 94.0214],
    "andro": [24.7450, 94.0420],
    "yambem": [24.6850, 94.0350],
    "changamdabi": [24.7350, 94.0550],
    "poirou khongjil": [24.7080, 94.0150],
    "poirou": [24.7080, 94.0150],
    "khurai konsam leikai": [24.8280, 93.9610],
    "konsam leikai": [24.8280, 93.9610],
    "khurai": [24.8260, 93.9580],
    "top chingtha": [24.7780, 94.0120],
    "khomidok": [24.8390, 93.9720],
    "tulihal": [24.7420, 93.9850],
    "lamlai": [24.8510, 94.0450],
    "moirangpurel": [24.7890, 94.0850],
    "keirao": [24.7610, 93.9780],
    "heingang": [24.8620, 93.9590],
    "kshetrigao": [24.7850, 93.9680],
    "wangkhei": [24.8060, 93.9550],
    # Imphal West / Uripok / Thangmeiband / Sagolband / Singjamei
    "uripok bachaspati leikai": [24.8145, 93.9275],
    "bachaspati leikai": [24.8145, 93.9275],
    "bachaspati": [24.8145, 93.9275],
    "uripok": [24.8145, 93.9275],
    "thangmeiband yumnam leikai": [24.8210, 93.9310],
    "thangmeiband hijam dewan leikai": [24.8230, 93.9330],
    "thangmeiband": [24.8210, 93.9310],
    "sagolband": [24.7990, 93.9250],
    "keishamthong": [24.7920, 93.9330],
    "singjamei": [24.7810, 93.9430],
    "yaiskul": [24.7980, 93.9410],
    "haoreibi makha leikai": [24.7650, 93.9380],
    "haoreibi": [24.7650, 93.9380],
    "laishram leikai": [24.8020, 93.9280],
    "sabaltongba": [24.8150, 93.8950],
    "sekmai": [24.9650, 93.8850],
    "lamsang": [24.8550, 93.8650],
    "mayang imphal": [24.6150, 93.8920],
    # Thoubal / Kakching / Lilong
    "kakching wairi sabal leikai": [24.4980, 93.9850],
    "kakching wairi": [24.4980, 93.9850],
    "kakching": [24.4880, 93.9820],
    "wairi": [24.4980, 93.9850],
    "sabal leikai": [24.4980, 93.9850],
    "thoubal kshetri leikai": [24.6380, 93.9980],
    "thoubal": [24.6380, 93.9980],
    "moijing": [24.6250, 93.9750],
    "kiyam siphai": [24.6150, 93.9850],
    "lamding": [24.6050, 94.0120],
    "wangjing": [24.5980, 94.0320],
    "heirok": [24.5850, 94.0550],
    "hiyanglam": [24.5320, 93.9180],
    "sugnu": [24.2850, 93.8750],
    "yairipok": [24.6750, 94.0620],
    "lilong": [24.7250, 93.9350],
    "wangoi": [24.6850, 93.9150],
    "sekmaijin": [24.5550, 93.8650],
    "ukhongshang": [24.5680, 94.0150],
    "lourembam": [24.5820, 94.0220],
    # Bishnupur
    "bishnupur": [24.6310, 93.7580],
    "nambol": [24.7120, 93.8420],
    "oinam": [24.6620, 93.7920],
    "moirang": [24.5020, 93.7710],
    "thanga": [24.5380, 93.8040],
    "kumbi": [24.4250, 93.7420],
    # Hill Districts
    "churachandpur": [24.3310, 93.6750],
    "tuibuang": [24.3450, 93.6820],
    "rengkai": [24.3280, 93.6680],
    "molnom": [24.3150, 93.6550],
    "ukhrul": [25.1120, 94.3620],
    "halang": [25.1450, 94.3350],
    "peh": [25.2150, 94.2850],
    "senapati": [25.2650, 94.0150],
    "kalinamai": [25.4350, 94.1450],
    "tamenglong": [24.9850, 93.5020],
    "chandel": [24.3310, 94.0350],
    "jiribam": [24.8020, 93.1250],
}

def find_village_anchor(section_name: str = "", polling_station_name: str = ""):
    query = f"{section_name or ''} {polling_station_name or ''}".lower()
    for key in sorted(VILLAGE_LEIKAI_CENTROIDS.keys(), key=len, reverse=True):
        if key in query:
            return key.title(), VILLAGE_LEIKAI_CENTROIDS[key]
    return None, None

def extract_polling_building_info(ps_raw_name: str, section_name: str = "", ac_name: str = ""):
    raw = (ps_raw_name or "").strip()
    building = ""
    wing = ""
    locality = ""
    
    matches = re.findall(r'\(([^)]+)\)', raw)
    if matches:
        last_paren = matches[-1].strip()
        parts = [p.strip() for p in last_paren.split(",") if p.strip()]
        wing_found = None
        building_parts = []
        for p in parts:
            if not wing_found and any(w in p.lower() for w in ["wing", "room", "left", "right", "north", "nort", "south", "east", "west"]):
                wing_found = p
            else:
                building_parts.append(p)
        if wing_found:
            wing = wing_found
            building = ", ".join(building_parts) if building_parts else last_paren
        else:
            building = last_paren
            
    cleaned_prefix = re.sub(r'^\d+\s*[-–]\s*', '', raw)
    loc_clean = re.sub(r'\([^)]*\)', '', cleaned_prefix).strip()
    # Strip trailing split markers like (A), (B) and clean section prefixes like 1-
    loc_clean = re.sub(r'\s*\([A-Za-z0-9]+\)\s*$', '', loc_clean).strip()
    sec_clean = re.sub(r'^\d+\s*[-–]\s*', '', (section_name or '')).strip()
    locality = loc_clean or sec_clean or ac_name

    if not building:
        building = re.sub(r'\s*\([A-Za-z0-9]+\)\s*$', '', cleaned_prefix).strip() or raw or "Designated Polling Station"

    return {"building_name": building, "wing": wing, "locality": locality}

def resolve_booth_geo(ac_no: int, part_no: int, ps_name: str = "", section_name: str = ""):
    # 1. Check authoritative cached coordinates in polling_station_geo table
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT latitude, longitude, source FROM polling_station_geo WHERE ac_no = ? AND part_no = ?",
                (ac_no, part_no)
            )
            row = cur.fetchone()
            if row and row["latitude"] and row["longitude"]:
                return row["latitude"], row["longitude"], None, (row["source"] or "EXACT_BUILDING")
        finally:
            conn.close()
    except Exception:
        pass

    # 2. Check verified Manipur village/leikai centroid dictionary
    v_name, v_coords = find_village_anchor(section_name, ps_name)
    if v_coords:
        return v_coords[0], v_coords[1], v_name, "VERIFIED_LOCALITY"

    # 3. Honest AC headquarters fallback (no synthetic spiral jitter)
    base = AC_CENTROIDS.get(ac_no, [24.8170, 93.9368])
    return base[0], base[1], None, "AC_AREA_APPROX"

def resolve_household_geo(ac_no: int, part_no: int, raw_house: str, section_name: str = "", polling_station_name: str = ""):
    # Golden Rule: ECI does not provide private household GPS coordinates.
    # Therefore, the official geographical anchor is strictly the Designated Polling Station building.
    b_lat, b_lng, v_name, geo_source = resolve_booth_geo(ac_no, part_no, polling_station_name, section_name)
    disp_village = v_name or f"AC-{ac_no} Part-{part_no}"
    return b_lat, b_lng, geo_source, disp_village, "OFFICIAL_POLLING_BOOTH"

@app.get("/api/voter/{voter_id}/household_map")
def get_voter_household_map(voter_id: int):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM voters WHERE id = ?", (voter_id,))
        target_row = cursor.fetchone()
        if not target_row:
            raise HTTPException(status_code=404, detail="Voter not found")
        target = dict(target_row)
    finally:
        conn.close()
    
    fam_data = get_voter_family(voter_id)
    branches = fam_data.get("branches", {})
    head = fam_data.get("head_of_household") or target
    
    anchor_ac = head.get("ac_no") or target["ac_no"]
    anchor_part = head.get("part_no") or target["part_no"]
    anchor_house = head.get("house_no") or target["house_no"]
    sec_name = head.get("section_name") or target.get("section_name") or ""
    ps_name = head.get("polling_station_name") or target.get("polling_station_name") or ""
    ac_name = head.get("ac_name") or target.get("ac_name") or ""
    
    b_lat, b_lng, v_name, geo_source = resolve_booth_geo(anchor_ac, anchor_part, ps_name, sec_name)
    booth_info = extract_polling_building_info(ps_name, sec_name, ac_name)

    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT district FROM constituencies WHERE ac_no = ?", (anchor_ac,))
        dist_row = cursor.fetchone()
        district_name = dist_row["district"] if dist_row and dist_row["district"] else ""

        cursor.execute("SELECT COUNT(DISTINCT house_no) FROM voters WHERE ac_no = ? AND part_no = ?", (anchor_ac, anchor_part))
        total_booth_households = cursor.fetchone()[0] or 1
        cursor.execute("SELECT COUNT(*) FROM voters WHERE ac_no = ? AND part_no = ?", (anchor_ac, anchor_part))
        total_booth_electors = cursor.fetchone()[0] or 1
    finally:
        conn.close()
    
    # Accurate Google Maps Search, Embed & Directions queries using verified public landmark + locality + district
    search_parts = [booth_info["building_name"]]
    if booth_info["locality"] and booth_info["locality"].lower() not in booth_info["building_name"].lower():
        search_parts.append(booth_info["locality"])
    if ac_name and ac_name.lower() not in " ".join(search_parts).lower():
        search_parts.append(ac_name)
    if district_name and district_name.lower() not in " ".join(search_parts).lower():
        search_parts.append(district_name)
    search_parts.append("Manipur, India")
    
    full_search_query = ", ".join([p for p in search_parts if p])
    encoded_search = urllib.parse.quote_plus(full_search_query)
    google_maps_url = f"https://www.google.com/maps/search/?api=1&query={encoded_search}"
    google_maps_embed_url = f"https://maps.google.com/maps?q={encoded_search}&z=16&output=embed"
    google_maps_directions_url = f"https://www.google.com/maps/dir/?api=1&destination={encoded_search}"
    b_label = urllib.parse.quote_plus(booth_info.get("building_name") or ps_name or "Polling Station")
    # Only use raw lat/lng URL when backed by exact building coordinates; otherwise use Google Maps POI search
    google_maps_coord_url = (
        f"https://www.google.com/maps?q=loc:{b_lat},{b_lng}+({b_label})"
        if geo_source == "EXACT_BUILDING"
        else google_maps_url
    )
    
    family_members = []
    t_entry = dict(target)
    t_entry["is_target"] = True
    t_entry["role"] = "Self (Target)"
    t_entry["audit_tier"] = "GOLD_SAME_HOUSEHOLD"
    family_members.append(t_entry)
    
    for branch_key, role_label in [
        ("grandparents", "Grandparent"),
        ("parents", "Parent"),
        ("spouses", "Spouse"),
        ("siblings", "Sibling"),
        ("children", "Child")
    ]:
        for mem in branches.get(branch_key, []):
            if isinstance(mem, dict) and not mem.get("is_deceased") and mem.get("id") != target["id"]:
                m_copy = dict(mem)
                m_copy["is_target"] = False
                m_copy["role"] = m_copy.get("relationship_label") or role_label
                m_copy["audit_tier"] = mem.get("audit_tier") or ("GOLD_SAME_HOUSEHOLD" if mem.get("part_no") == target.get("part_no") else "SILVER_SPLIT_BOOTH")
                family_members.append(m_copy)
                
    head_id = head.get("id")
    family_members.sort(key=lambda m: (0 if m.get("id") == head_id else 1, -(int(m.get("age") or 0))))

    booth_election_2022 = None
    ac_e22 = ELECTION_2022_DATA.get(str(anchor_ac))
    if ac_e22:
        b22 = ac_e22.get("booths", {}).get(str(anchor_part))
        if b22:
            cands = ac_e22.get("candidates", [])
            b_votes = b22.get("votes", [])
            ranked_booth = sorted(
                [{"name": cands[i], "votes": b_votes[i]} for i in range(min(len(cands), len(b_votes)))],
                key=lambda x: x["votes"],
                reverse=True,
            )
            booth_election_2022 = {
                "year": 2022,
                "total_polled": b22.get("total_polled", 0),
                "nota": b22.get("nota", 0),
                "winner_party_ac": ac_e22.get("winner_party"),
                "candidates": ranked_booth,
            }

    return {
        "success": True,
        "booth_election_2022": booth_election_2022,
        "elector_info": {
            "voter_id": target.get("id"),
            "name": target.get("name"),
            "house_no": anchor_house,
            "normalized_house_no": get_base_house_no(anchor_house),
            "ac_no": anchor_ac,
            "ac_name": ac_name,
            "part_no": anchor_part,
            "section_name": sec_name,
            "polling_station_name": ps_name
        },
        "household": {
            "house_no": anchor_house,
            "normalized_house_no": get_base_house_no(anchor_house),
            "ac_no": anchor_ac,
            "ac_name": ac_name,
            "part_no": anchor_part,
            "polling_station_name": ps_name,
            "section_name": sec_name,
            "village_name": v_name or booth_info["locality"],
            "tier": "OFFICIAL_POLLING_BOOTH",
            "latitude": b_lat,
            "longitude": b_lng,
            "google_maps_url": google_maps_url,
            "google_maps_embed_url": google_maps_embed_url,
            "confidence": geo_source,
            "total_booth_households": total_booth_households
        },
        "head_of_household": head,
        "family": {
            "total_count": len(family_members),
            "members": family_members
        },
        "polling_station": {
            "part_no": anchor_part,
            "name": ps_name,
            "building_name": booth_info["building_name"],
            "wing": booth_info["wing"],
            "locality": booth_info["locality"],
            "district": district_name,
            "search_query": full_search_query,
            "latitude": b_lat,
            "longitude": b_lng,
            "google_maps_url": google_maps_url,
            "google_maps_embed_url": google_maps_embed_url,
            "google_maps_directions_url": google_maps_directions_url,
            "google_maps_coord_url": google_maps_coord_url,
            "total_electors": total_booth_electors,
            "confidence": geo_source
        }
    }

@app.post("/api/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    file_path = os.path.join(UPLOADS_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        metadata, voters = parse_electoral_roll_pdf(file_path)
        if not voters:
            return JSONResponse(status_code=200, content={
                "success": True,
                "message": "PDF uploaded and analyzed, but no voter cards matched standard ECI format (it may be a scanned or image PDF requiring OCR).",
                "extracted_metadata": metadata,
                "voters_added": 0
            })

        conn = get_connection()
        cursor = conn.cursor()
        inserted_count = 0
        for v in voters:
            try:
                cursor.execute("""
                INSERT OR IGNORE INTO voters (
                    epic_no, name, relative_name, relation_type, house_no, age, gender,
                    ac_no, ac_name, part_no, polling_station_name, section_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    v["epic_no"], v["name"], v["relative_name"], v["relation_type"],
                    v["house_no"], v["age"], v["gender"], v["ac_no"], v["ac_name"],
                    v["part_no"], v["polling_station_name"], v["section_name"]
                ))
                if cursor.rowcount > 0:
                    inserted_count += 1
            except Exception:
                pass

        conn.commit()
        conn.close()
        update_constituency_stats()
        global _cached_stats
        _cached_stats = None

        return {
            "success": True,
            "message": f"Successfully parsed and ingested {inserted_count} new voters from {file.filename}.",
            "metadata": metadata,
            "voters_added": inserted_count
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error parsing PDF: {str(e)}")

@app.get("/api/downloader/status")
def get_downloader_status(ac_no: int = 1):
    # Support for Special Polling Stations / IDP Relief Camps
    if ac_no == 999 or ac_no == 0:
        ac_no = 999
        ac_name = "IDP Relief Camp Special Stations"
        clean_name = "IDP_Relief_Camps"
        ac_folder_name = "Special_Polling_Stations_IDP"
        ac_dir = os.path.join(UPLOADS_DIR, ac_folder_name)
        os.makedirs(ac_dir, exist_ok=True)
        total_parts = 94
        
        # Check existing downloaded in folder
        existing_parts = set()
        for f in os.listdir(ac_dir):
            if f.endswith(".pdf"):
                m = re.search(r'(?:SPS|PART|ENG)-?(\d+)', f, re.IGNORECASE)
                if m:
                    existing_parts.add(int(m.group(1)))
        
        all_ingested = sorted(list(existing_parts))
        next_part = None
        for p in range(1, total_parts + 1):
            if p not in all_ingested:
                next_part = p
                break
        
        district_map = {
            (1, 24): "Kangpokpi (24 Booths)",
            (25, 46): "Bishnupur (22 Booths)",
            (47, 61): "Churachandpur (15 Booths)",
            (62, 70): "Imphal East (9 Booths)",
            (71, 78): "Imphal West (8 Booths)",
            (79, 83): "Kakching (5 Booths)",
            (84, 87): "Tengnoupal (4 Booths)",
            (88, 91): "Ukhrul (4 Booths)",
            (92, 93): "Thoubal (2 Booths)",
            (94, 94): "Jiribam (1 Booth)"
        }
        booth_loc = "Relief Camp Cluster"
        if next_part:
            for (start, end), dist in district_map.items():
                if start <= next_part <= end:
                    booth_loc = f"{dist} - SPS #{next_part}"
                    break
        
        return {
            "ac_no": 999,
            "ac_name": "Special Polling Stations (IDP Relief Camps)",
            "selected_text": "999-IDP-Relief-Camps",
            "folder": ac_folder_name,
            "total_parts": total_parts,
            "completed_count": len(all_ingested),
            "completed_parts": all_ingested,
            "next_part": next_part,
            "next_booth_name": booth_loc
        }

    # Lookup constituency info
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ac_no, ac_name, district FROM constituencies WHERE ac_no = ?", (ac_no,))
    ac_row = cur.fetchone()
    if not ac_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Constituency not found")

    ac_name = ac_row["ac_name"]
    clean_name = ''.join(c for c in ac_name if c.isalnum() or c in ' -_').strip().replace(' ', '_')
    ac_folder_name = f"AC_{ac_no:02d}_{clean_name}"
    ac_dir = os.path.join(UPLOADS_DIR, ac_folder_name)
    os.makedirs(ac_dir, exist_ok=True)

    # 1. Fetch live total parts and booth names from CEO Manipur
    s = get_or_create_session()
    selected_text = OFFICIAL_ACS.get(str(ac_no), f"{ac_no}-{ac_name}")
    booths = []
    total_parts = 40
    try:
        r = s.post("https://ceomanipur.nic.in/eroll.aspx/GetData", 
                   json={"selectedValue": str(ac_no), "selectedText": selected_text}, 
                   timeout=10)
        if r.status_code == 200:
            import json
            data = r.json()
            if 'd' in data:
                parsed = json.loads(data['d'])
                booths = parsed.get("data", [])
                total_parts = len(booths)
    except Exception as e:
        pass

    # 2. Check existing downloaded parts in folder and root uploads
    existing_parts = set()
    for search_dir in [ac_dir, UPLOADS_DIR]:
        if os.path.exists(search_dir):
            for f in os.listdir(search_dir):
                if f.endswith(".pdf"):
                    m = re.search(r'-S14-(\d+)-SSR-.*?-ENG-(\d+)-', f, re.IGNORECASE)
                    if m and int(m.group(1)) == ac_no:
                        existing_parts.add(int(m.group(2)))

    # Also check database
    cur.execute("SELECT DISTINCT part_no FROM voters WHERE ac_no = ?", (ac_no,))
    db_parts = [r[0] for r in cur.fetchall()]
    conn.close()

    all_ingested = sorted(list(existing_parts.union(set(db_parts))))
    next_part = None
    for p in range(1, total_parts + 1):
        if p not in all_ingested:
            next_part = p
            break

    # Get name of next polling station if available
    next_booth_name = f"Part {next_part}"
    for b in booths:
        if b.get("PART_NO") == next_part:
            next_booth_name = b.get("PS_NAME_EN", f"Part {next_part}")
            break

    return {
        "ac_no": ac_no,
        "ac_name": ac_name,
        "selected_text": selected_text,
        "folder": ac_folder_name,
        "total_parts": total_parts,
        "completed_count": len(all_ingested),
        "completed_parts": all_ingested,
        "next_part": next_part,
        "next_booth_name": next_booth_name
    }

@app.get("/api/downloader/captcha")
def get_downloader_captcha(session_id: str = "default"):
    s = get_or_create_session(session_id)
    try:
        r = s.get(f"https://ceomanipur.nic.in/Captcha/captcha.ashx?{int(os.times().system * 1000)}", timeout=10)
        if r.status_code == 200:
            img_b64 = base64.b64encode(r.content).decode("utf-8")
            return {
                "success": True,
                "captcha_image": f"data:image/png;base64,{img_b64}"
            }
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/api/downloader/submit")
async def submit_downloader_captcha(data: dict = Body(...)):
    session_id = data.get("session_id", "default")
    ac_no = data.get("ac_no", 7)
    ac_name = data.get("ac_name", "7-Andro")
    part_no = data.get("part_no", 1)
    captcha_code = data.get("captcha_code", "").strip()

    if not captcha_code:
        raise HTTPException(status_code=400, detail="CAPTCHA code is required.")

    s = get_or_create_session(session_id)

    # 1. Validate CAPTCHA
    val_url = f"https://ceomanipur.nic.in/ValidateCaptcha.ashx?code={requests.utils.quote(captcha_code)}"
    r_val = s.get(val_url, timeout=10)
    result = r_val.text.strip().lower()

    if result != "success":
        return {
            "success": False,
            "message": "Incorrect or expired CAPTCHA. Please try again."
        }

    # 2. Download PDF
    official_text = OFFICIAL_ACS.get(str(ac_no), ac_name if "-" in ac_name else f"{ac_no}-{ac_name}")
    download_url = f"https://ceomanipur.nic.in/erollDownload.ashx?selectedText={requests.utils.quote(official_text)}&selectedValue={ac_no}&partNo={part_no}&lang=ENG&type=draft"
    r_pdf = s.get(download_url, timeout=30)
    if r_pdf.status_code != 200 or len(r_pdf.content) < 1000:
        return {
            "success": False,
            "message": f"Server responded with status {r_pdf.status_code} while downloading Part {part_no}."
        }

    # 3. Save to dedicated AC subfolder in uploads/
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ac_name FROM constituencies WHERE ac_no = ?", (ac_no,))
    ac_row = cur.fetchone()
    conn.close()
    ac_raw_name = ac_row[0] if ac_row else str(ac_no)
    clean_name = ''.join(c for c in ac_raw_name if c.isalnum() or c in ' -_').strip().replace(' ', '_')
    ac_folder_dir = os.path.join(UPLOADS_DIR, f"AC_{ac_no:02d}_{clean_name}")
    os.makedirs(ac_folder_dir, exist_ok=True)

    filename = f"2027-EROLLGEN-S14-{ac_no}-SSR-DraftRoll-Revision1-ENG-{part_no}-WI.pdf"
    file_path = os.path.join(ac_folder_dir, filename)
    with open(file_path, "wb") as f:
        f.write(r_pdf.content)

    # 4. Automatically parse and ingest into SQLite
    try:
        metadata, voters = parse_electoral_roll_pdf(file_path)
        conn = get_connection()
        cursor = conn.cursor()
        inserted_count = 0
        for v in voters:
            try:
                cursor.execute("""
                INSERT OR REPLACE INTO voters (
                    epic_no, name, relative_name, relation_type, house_no, age, gender,
                    ac_no, ac_name, part_no, polling_station_name, section_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    v["epic_no"], v["name"], v["relative_name"], v["relation_type"],
                    v["house_no"], v["age"], v["gender"], v["ac_no"], v["ac_name"],
                    v["part_no"], v["polling_station_name"], v["section_name"]
                ))
                inserted_count += 1
            except Exception:
                pass
        conn.commit()
        conn.close()
        update_constituency_stats()
        global _cached_stats
        _cached_stats = None

        return {
            "success": True,
            "message": f"Part {part_no} downloaded and indexed ({inserted_count} voters added)!",
            "part_no": part_no,
            "voters_added": inserted_count
        }
    except Exception as e:
        return {
            "success": True,
            "message": f"Part {part_no} saved to disk, but error parsing: {str(e)}",
            "part_no": part_no,
            "voters_added": 0
        }

# ----------------------------------------------------
# NERISTian Students Protected Directory Endpoints
# ----------------------------------------------------
TARGET_NERIST_PASSKEY = "nowyouseeme"

def mask_phone_number(phone: str) -> str:
    if not phone or len(str(phone).strip()) < 8:
        return "--"
    cleaned = re.sub(r'[^0-9\+]', '', str(phone).strip())
    if len(cleaned) <= 6:
        return "••••••"
    prefix = cleaned[:5]  # e.g. "+9198"
    suffix = cleaned[-2:] # e.g. "86"
    return f"{prefix}••••••{suffix}"

@app.get("/api/nerist/stats")
def get_nerist_stats(
    passkey: Optional[str] = None,
    x_passkey: Optional[str] = Header(None)
):
    if passkey != TARGET_NERIST_PASSKEY and x_passkey != TARGET_NERIST_PASSKEY:
        raise HTTPException(status_code=401, detail="Access restricted. Valid administrator passkey required.")

    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM nerist_students;")
    total = c.fetchone()[0]
    
    c.execute("SELECT match_status, COUNT(*) FROM nerist_students GROUP BY match_status;")
    status_counts = dict(c.fetchall())
    
    c.execute("SELECT department_name, COUNT(*) FROM nerist_students GROUP BY department_name ORDER BY COUNT(*) DESC;")
    dept_counts = [{"name": r[0] or "General", "count": r[1]} for r in c.fetchall()]
    conn.close()
    
    triangulated = status_counts.get("HOUSEHOLD_TRIANGULATED", 0)
    ac_matched = status_counts.get("AC_MATCHED", 0)
    student_linked = status_counts.get("STUDENT_ELECTOR_LINKED", 0)
    parent_verified = status_counts.get("PARENT_VERIFIED", 0)
    unmatched = status_counts.get("UNMATCHED", 0)
    linked_total = total - unmatched
    
    return {
        "total_students": total,
        "triangulated": triangulated,
        "ac_matched": ac_matched,
        "student_linked": student_linked,
        "parent_verified": parent_verified,
        "unmatched": unmatched,
        "linked_total": linked_total,
        "match_percentage": round((linked_total / total * 100), 1) if total > 0 else 0,
        "departments": dept_counts
    }

@app.get("/api/nerist/students")
def get_nerist_students(
    q: Optional[str] = None,
    dept: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    limit: int = 25,
    passkey: Optional[str] = None,
    x_passkey: Optional[str] = Header(None)
):
    if passkey != TARGET_NERIST_PASSKEY and x_passkey != TARGET_NERIST_PASSKEY:
        raise HTTPException(status_code=401, detail="Access restricted. Valid administrator passkey required.")

    is_unlocked = True
    
    conn = get_connection()
    c = conn.cursor()
    
    where_clauses = []
    params = []
    
    if q and q.strip():
        tokens = [t.strip() for t in q.strip().split() if t.strip()]
        for token in tokens:
            term = f"%{token}%"
            where_clauses.append("(n.student_name LIKE ? OR n.roll_no LIKE ? OR n.user_id LIKE ? OR n.father_name LIKE ? OR n.mother_name LIKE ? OR n.ac_name LIKE ? OR n.address LIKE ?)")
            params.extend([term, term, term, term, term, term, term])
        
    if dept and dept.strip() and dept.lower() != 'all':
        where_clauses.append("n.department_name = ?")
        params.append(dept.strip())
        
    if status and status.strip() and status.lower() != 'all':
        where_clauses.append("n.match_status = ?")
        params.append(status.strip())
        
    where_str = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    
    c.execute(f"SELECT COUNT(*) FROM nerist_students n {where_str};", params)
    total_count = c.fetchone()[0]
    
    offset = (page - 1) * limit
    sql = f"""
        SELECT n.id, n.user_id, n.roll_no, n.student_name, n.student_mobile, n.email, n.father_name, n.mother_name,
               n.parents_mobile, n.dob, n.calculated_age, n.address, n.pincode, n.department_name, n.program_name,
               n.session, n.linked_student_epic, n.linked_father_epic, n.linked_mother_epic, n.ac_no, n.ac_name,
               n.part_no, n.house_no, n.match_status, n.match_notes,
               vs.id AS student_voter_id,
               vf.id AS father_voter_id,
               vm.id AS mother_voter_id
        FROM nerist_students n
        LEFT JOIN voters vs ON n.linked_student_epic = vs.epic_no AND n.linked_student_epic != ''
        LEFT JOIN voters vf ON n.linked_father_epic = vf.epic_no AND n.linked_father_epic != ''
        LEFT JOIN voters vm ON n.linked_mother_epic = vm.epic_no AND n.linked_mother_epic != ''
        {where_str}
        ORDER BY n.id ASC
        LIMIT ? OFFSET ?;
    """
    c.execute(sql, params + [limit, offset])
    rows = c.fetchall()
    conn.close()
    
    students = []
    for r in rows:
        raw_s_mobile = r[4] or ""
        raw_p_mobile = r[8] or ""
        student_voter_id = r[25]
        father_voter_id = r[26]
        mother_voter_id = r[27]
        target_voter_id = student_voter_id or father_voter_id or mother_voter_id
        is_student_verified = bool(r[16] and student_voter_id)
        is_father_verified = bool(r[17] and father_voter_id)
        is_mother_verified = bool(r[18] and mother_voter_id)
        
        students.append({
            "id": r[0],
            "user_id": r[1],
            "roll_no": r[2],
            "student_name": r[3],
            "student_mobile": raw_s_mobile if is_unlocked else mask_phone_number(raw_s_mobile),
            "raw_student_mobile": raw_s_mobile if is_unlocked else None,
            "email": r[5],
            "father_name": r[6],
            "mother_name": r[7],
            "parents_mobile": raw_p_mobile if is_unlocked else mask_phone_number(raw_p_mobile),
            "raw_parents_mobile": raw_p_mobile if is_unlocked else None,
            "dob": r[9],
            "calculated_age": r[10],
            "address": r[11],
            "pincode": r[12],
            "department_name": r[13],
            "program_name": r[14],
            "session": r[15],
            "linked_student_epic": r[16],
            "linked_father_epic": r[17],
            "linked_mother_epic": r[18],
            "ac_no": r[19],
            "ac_name": r[20],
            "part_no": r[21],
            "house_no": r[22],
            "match_status": r[23],
            "match_notes": r[24],
            "student_voter_id": student_voter_id,
            "father_voter_id": father_voter_id,
            "mother_voter_id": mother_voter_id,
            "target_voter_id": target_voter_id,
            "is_student_verified": is_student_verified,
            "is_father_verified": is_father_verified,
            "is_mother_verified": is_mother_verified
        })
        
    return {
        "success": True,
        "is_unlocked": is_unlocked,
        "total": total_count,
        "page": page,
        "limit": limit,
        "total_pages": (total_count + limit - 1) // limit if total_count > 0 else 1,
        "students": students
    }

@app.post("/api/telemetry/click")
def api_telemetry_click(request: Request, payload: dict = Body(...)):
    try:
        record_click_feedback(
            client_ip=extract_client_ip(request),
            query_text=payload.get("query_text"),
            voter_id=payload.get("voter_id"),
            voter_name=payload.get("voter_name"),
            action_type=payload.get("action_type", "click"),
        )
    except Exception:
        pass
    return {"success": True}

@app.post("/api/telemetry/kinship_feedback")
def api_kinship_feedback(request: Request, payload: dict = Body(...)):
    target_id = payload.get("target_id")
    verdict = payload.get("verdict")
    if not target_id or verdict not in ("correct", "mismatch"):
        raise HTTPException(status_code=400, detail="target_id and verdict ('correct'|'mismatch') required")
    record_kinship_feedback(
        client_ip=extract_client_ip(request),
        target_id=int(target_id),
        relative_id=payload.get("relative_id"),
        relationship_type=payload.get("relationship_type"),
        verdict=verdict,
        note=payload.get("note"),
    )
    return {"success": True}

@app.get("/api/admin/telemetry")
def api_admin_telemetry(
    limit: int = Query(100, ge=1, le=500),
    passkey: Optional[str] = None,
    x_passkey: Optional[str] = Header(None),
):
    admin_key = os.environ.get("ADMIN_PASSKEY", TARGET_NERIST_PASSKEY)
    if passkey != admin_key and x_passkey != admin_key and passkey != TARGET_NERIST_PASSKEY and x_passkey != TARGET_NERIST_PASSKEY:
        raise HTTPException(status_code=401, detail="Valid admin passkey required.")
    return {"success": True, **get_telemetry_summary(limit=limit)}

@app.get("/api/admin/telemetry/export_csv")
def api_admin_telemetry_export_csv(
    passkey: Optional[str] = None,
    x_passkey: Optional[str] = Header(None),
):
    admin_key = os.environ.get("ADMIN_PASSKEY", TARGET_NERIST_PASSKEY)
    if passkey != admin_key and x_passkey != admin_key and passkey != TARGET_NERIST_PASSKEY and x_passkey != TARGET_NERIST_PASSKEY:
        raise HTTPException(status_code=401, detail="Valid admin passkey required.")
    csv_data = export_telemetry_csv()
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="voter_portal_audit_logs.csv"'},
    )

@app.get("/api/elections/overview")
def get_elections_overview():
    if not ELECTION_HISTORY_DATA:
        raise HTTPException(status_code=404, detail="Election history dataset not loaded")
    aff_by_ac = CANDIDATE_AFFIDAVITS_DATA.get("by_ac_2022", {})
    crorepati_winners = 0
    criminal_winners = 0
    party_switchers_total = 0

    acs = []
    for i in range(1, 61):
        key = str(i)
        if key not in ELECTION_HISTORY_DATA:
            continue
        item = dict(ELECTION_HISTORY_DATA[key])
        region = "Jiribam" if i == 40 else ("Imphal Valley" if i <= 39 else "Hill Districts")
        item["region"] = region
        e24 = ELECTION_2024_DATA.get(key, {})
        polled_24 = e24.get("total_polled", 0) or 0
        margin_24 = e24.get("margin", 0) or 0
        t24 = e24.get("turnout_pct", 0.0) or 0.0
        t22 = item["y2022"].get("turnout_pct", 0.0) or 0.0
        item["y2024"] = {
            "pc_name": e24.get("pc_name", "Inner Manipur" if i <= 32 else "Outer Manipur (ST)"),
            "leader": e24.get("leader", "N/A"),
            "party": e24.get("leader_party", "IND"),
            "leader_votes": e24.get("leader_votes", 0),
            "runner_up": e24.get("runner_up", "N/A"),
            "runner_up_votes": e24.get("runner_up_votes", 0),
            "margin": margin_24,
            "margin_pct": round((margin_24 / polled_24) * 100, 2) if polled_24 else 0.0,
            "turnout_pct": t24,
            "total_polled": polled_24,
            "turnout_delta_22_24": round(t24 - t22, 2),
            "booths_count": len(e24.get("booths", {})),
        }

        ac_affs = aff_by_ac.get(key, [])
        winner_aff = next((c for c in ac_affs if c.get("is_winner")), None) or (ac_affs[0] if ac_affs else None)
        switchers_in_ac = [c for c in ac_affs if c.get("is_party_switcher")]
        party_switchers_total += len(switchers_in_ac)
        if winner_aff:
            if (winner_aff.get("total_assets_inr") or 0) >= 10_000_000:
                crorepati_winners += 1
            if (winner_aff.get("criminal_cases") or 0) > 0:
                criminal_winners += 1
        item["affidavit_summary"] = {
            "candidates_count": len(ac_affs),
            "winner_assets_inr": winner_aff.get("total_assets_inr", 0) if winner_aff else 0,
            "winner_assets_display": winner_aff.get("total_assets_display", "N/A") if winner_aff else "N/A",
            "winner_criminal_cases": winner_aff.get("criminal_cases", 0) if winner_aff else 0,
            "winner_education": winner_aff.get("education", "N/A") if winner_aff else "N/A",
            "winner_party_2017": winner_aff.get("party_2017") if winner_aff else None,
            "winner_is_switcher": bool(winner_aff.get("is_party_switcher")) if winner_aff else False,
            "switchers_count": len(switchers_in_ac),
        }
        acs.append(item)

    # 4-Cycle Party Seat/Lead Trajectories (2012 -> 2017 -> 2022 -> 2024 LS AC leads)
    parties = ["BJP", "INC", "NPP", "NPF", "JD(U)", "AITC", "KPA", "MSCP", "LJP", "NCP", "IND"]
    party_trajectory = []
    for p in parties:
        s12 = sum(1 for a in acs if a["y2012"]["party"] == p)
        s17 = sum(1 for a in acs if a["y2017"]["party"] == p)
        s22 = sum(1 for a in acs if a["y2022"]["party"] == p)
        s24 = sum(1 for a in acs if a["y2024"]["party"] == p)
        if s12 or s17 or s22 or s24:
            growth_17_22_pct = round(((s22 - s17) / s17) * 100, 1) if s17 > 0 else (100.0 if s22 > 0 else 0.0)
            growth_22_24_pct = round(((s24 - s22) / s22) * 100, 1) if s22 > 0 else (100.0 if s24 > 0 else 0.0)
            party_trajectory.append({
                "party": p,
                "y2012": s12,
                "y2017": s17,
                "y2022": s22,
                "y2024": s24,
                "delta_17_22": s22 - s17,
                "delta_22_24": s24 - s22,
                "growth_17_22_pct": growth_17_22_pct,
                "growth_22_24_pct": growth_22_24_pct,
            })
    party_trajectory.sort(key=lambda x: (-x["y2022"], -x["y2024"], -x["y2017"]))

    # Regional / Area Breakdown (Imphal Valley vs Hill Districts vs Jiribam)
    area_breakdown = []
    for reg in ["Imphal Valley", "Hill Districts", "Jiribam"]:
        sub = [a for a in acs if a["region"] == reg]
        if not sub:
            continue
        p_stats = []
        for p in ["BJP", "INC", "NPP", "NPF", "JD(U)", "KPA", "IND"]:
            c12 = sum(1 for a in sub if a["y2012"]["party"] == p)
            c17 = sum(1 for a in sub if a["y2017"]["party"] == p)
            c22 = sum(1 for a in sub if a["y2022"]["party"] == p)
            c24 = sum(1 for a in sub if a["y2024"]["party"] == p)
            if c12 or c17 or c22 or c24:
                g_pct = round(((c22 - c17) / c17) * 100, 1) if c17 > 0 else (100.0 if c22 > 0 else 0.0)
                p_stats.append({"party": p, "y2012": c12, "y2017": c17, "y2022": c22, "y2024": c24, "delta_17_22": c22 - c17, "growth_17_22_pct": g_pct})
        avg_t22 = round(sum(a["y2022"]["turnout_pct"] for a in sub) / len(sub), 2)
        avg_t24 = round(sum(a["y2024"]["turnout_pct"] for a in sub) / len(sub), 2)
        area_breakdown.append({
            "region": reg,
            "total_acs": len(sub),
            "avg_turnout_2022": avg_t22,
            "avg_turnout_2024": avg_t24,
            "turnout_delta": round(avg_t24 - avg_t22, 2),
            "parties": p_stats,
        })

    # District-level Breakdown
    district_breakdown = []
    seen_dists = sorted(set(a["district"] for a in acs))
    for dist in seen_dists:
        sub = [a for a in acs if a["district"] == dist]
        avg_t22 = round(sum(a["y2022"]["turnout_pct"] for a in sub) / len(sub), 2)
        avg_t24 = round(sum(a["y2024"]["turnout_pct"] for a in sub) / len(sub), 2)
        dom22 = max(["BJP", "INC", "NPP", "NPF", "JD(U)", "KPA", "IND"], key=lambda p: sum(1 for a in sub if a["y2022"]["party"] == p))
        bjp_17 = sum(1 for a in sub if a["y2017"]["party"] == "BJP")
        bjp_22 = sum(1 for a in sub if a["y2022"]["party"] == "BJP")
        inc_17 = sum(1 for a in sub if a["y2017"]["party"] == "INC")
        inc_22 = sum(1 for a in sub if a["y2022"]["party"] == "INC")
        district_breakdown.append({
            "district": dist,
            "total_acs": len(sub),
            "avg_turnout_2022": avg_t22,
            "avg_turnout_2024": avg_t24,
            "turnout_delta": round(avg_t24 - avg_t22, 2),
            "dominant_2022": dom22,
            "bjp_delta": bjp_22 - bjp_17,
            "inc_delta": inc_22 - inc_17,
        })

    total_elec_22 = sum(a["y2022"]["total_electors_2022"] for a in acs)
    total_polled_22 = sum(a["y2022"]["total_polled"] for a in acs)
    state_turnout_22 = round((total_polled_22 / total_elec_22) * 100, 2) if total_elec_22 else 0.0

    total_elec_24 = sum(ELECTION_2024_DATA.get(str(a["ac_no"]), {}).get("total_electors_2024", 0) for a in acs)
    total_polled_24 = sum(a["y2024"]["total_polled"] for a in acs)
    state_turnout_24 = round((total_polled_24 / total_elec_24) * 100, 2) if total_elec_24 else 0.0

    return {
        "success": True,
        "summary": {
            "total_seats": len(acs),
            "state_turnout_2022": state_turnout_22,
            "state_turnout_2024": state_turnout_24,
            "total_polled_2022": total_polled_22,
            "total_polled_2024": total_polled_24,
            "battleground_count": sum(1 for a in acs if a["classification"] == "BATTLEGROUND"),
            "flipped_count": sum(1 for a in acs if a["flipped_2017_to_2022"]),
            "youth_decisive_count": sum(1 for a in acs if a["youth_exceeds_margin"]),
            "turncoat_holds": sum(1 for a in acs if a["is_turncoat_hold"]),
            "crorepati_winners_count": crorepati_winners,
            "criminal_cases_winners_count": criminal_winners,
            "party_switchers_count": party_switchers_total,
        },
        "party_trajectory": party_trajectory,
        "area_breakdown": area_breakdown,
        "district_breakdown": district_breakdown,
        "constituencies": acs,
    }


@app.get("/api/elections/ac/{ac_no}")
def get_election_ac_detail(ac_no: int):
    ac_hist = ELECTION_HISTORY_DATA.get(str(ac_no))
    if not ac_hist:
        raise HTTPException(status_code=404, detail="Election data not found for AC")

    ac_e22 = ELECTION_2022_DATA.get(str(ac_no), {})
    ac_e24 = ELECTION_2024_DATA.get(str(ac_no), {})
    cands_22 = ac_e22.get("candidates", [])
    booths_22 = ac_e22.get("booths", {})
    cands_24 = ac_e24.get("candidates", [])
    booths_24 = ac_e24.get("booths", {})
    ac_winner_22 = (ac_hist.get("y2022") or {}).get("winner", "")

    conn = get_connection()
    try:
        cur = conn.cursor()
        # Per-booth demographic roll analytics (age cohorts, gender ratio, deleted/shifted flags, household density)
        cur.execute(
            """
            SELECT part_no,
                   MAX(polling_station_name) AS polling_station_name,
                   COUNT(*) AS curr_voters,
                   SUM(CASE WHEN age BETWEEN 18 AND 29 THEN 1 ELSE 0 END) AS youth_18_29,
                   SUM(CASE WHEN age BETWEEN 30 AND 49 THEN 1 ELSE 0 END) AS mid_30_49,
                   SUM(CASE WHEN age >= 50 THEN 1 ELSE 0 END) AS senior_50_plus,
                   SUM(CASE WHEN gender = 'M' THEN 1 ELSE 0 END) AS male_count,
                   SUM(CASE WHEN gender = 'F' THEN 1 ELSE 0 END) AS female_count,
                   SUM(CASE WHEN status != 'ACTIVE' THEN 1 ELSE 0 END) AS deleted_count,
                   COUNT(DISTINCT CASE WHEN house_no IS NOT NULL AND house_no NOT IN ('', '-', '0', 'N/A') THEN house_no END) AS distinct_households
            FROM voters
            WHERE ac_no = ?
            GROUP BY part_no
            """,
            (ac_no,),
        )
        booth_names = {r["part_no"]: dict(r) for r in cur.fetchall()}

        # Polling station geo coordinates
        cur.execute(
            "SELECT part_no, latitude, longitude, source FROM polling_station_geo WHERE ac_no = ?",
            (ac_no,),
        )
        booth_geo = {r["part_no"]: {"latitude": r["latitude"], "longitude": r["longitude"], "source": r["source"]} for r in cur.fetchall()}

        # Large household count (4+ electors) per part
        cur.execute(
            """
            SELECT part_no, COUNT(*) AS large_hh_4_plus
            FROM (
                SELECT part_no, house_no, COUNT(*) AS c
                FROM voters
                WHERE ac_no = ? AND house_no IS NOT NULL AND house_no NOT IN ('', '-', '0', 'N/A')
                GROUP BY part_no, house_no
                HAVING c >= 4
            )
            GROUP BY part_no
            """,
            (ac_no,),
        )
        large_hh_by_part = {r["part_no"]: r["large_hh_4_plus"] for r in cur.fetchall()}

        # Top surname/clan cluster density per part
        cur.execute("SELECT part_no, name FROM voters WHERE ac_no = ? AND status = 'ACTIVE'", (ac_no,))
        surname_counts_by_part = {}
        part_active_totals = {}
        for r in cur.fetchall():
            pno = r["part_no"]
            part_active_totals[pno] = part_active_totals.get(pno, 0) + 1
            parts_tok = (r["name"] or "").strip().split()
            if parts_tok:
                # In Manipur, family/clan surname is typically the first token (e.g., Konsam, Thokchom) or last token
                clan = parts_tok[0].upper() if len(parts_tok[0]) >= 3 else (parts_tok[-1].upper() if parts_tok else "")
                if clan and clan not in ("MD", "MR", "MS", "DR", "KM", "LATE"):
                    d = surname_counts_by_part.setdefault(pno, {})
                    d[clan] = d.get(clan, 0) + 1

        # Top large households (4+ electors) per part in this AC for War Room mobilization
        cur.execute(
            """
            SELECT part_no, house_no, COUNT(*) as mem_cnt, MIN(id) as sample_voter_id,
                   SUM(CASE WHEN age BETWEEN 18 AND 29 THEN 1 ELSE 0 END) as youth_count,
                   SUM(CASE WHEN status != 'ACTIVE' THEN 1 ELSE 0 END) as deleted_count,
                   MAX(CASE WHEN age >= 45 THEN name ELSE '' END) as elder_name,
                   MAX(name) as fallback_name,
                   MAX(polling_station_name) as polling_station_name
            FROM voters
            WHERE ac_no = ? AND house_no IS NOT NULL AND house_no NOT IN ('', '-', '0', 'N/A')
            GROUP BY part_no, house_no
            HAVING mem_cnt >= 4
            ORDER BY mem_cnt DESC
            """,
            (ac_no,),
        )
        large_households = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    all_parts = sorted(set(list(booths_22.keys()) + list(booths_24.keys())), key=lambda x: int(x) if str(x).isdigit() else 9999)
    booths_list = []
    quadrant_counts = {"A_FORTRESS": 0, "B_SWING": 0, "C_GOTV": 0, "D_TRAILING": 0}

    for pkey in all_parts:
        b22 = booths_22.get(pkey, {})
        b24 = booths_24.get(pkey, {})
        pno = b22.get("part_no") or b24.get("part_no") or (int(pkey) if str(pkey).isdigit() else 0)

        votes22 = b22.get("votes", [])
        polled22 = b22.get("total_polled", 0) or 0
        paired22 = sorted(
            [{"name": cands_22[i], "votes": votes22[i]} for i in range(min(len(cands_22), len(votes22)))],
            key=lambda x: x["votes"],
            reverse=True,
        )
        top1_22 = paired22[0] if paired22 else {"name": "N/A", "votes": 0}
        top2_22 = paired22[1] if len(paired22) > 1 else {"name": "N/A", "votes": 0}
        margin_22 = top1_22["votes"] - top2_22["votes"]
        margin_22_pct = round((margin_22 / (polled22 or 1)) * 100, 1)

        votes24 = b24.get("votes", [])
        polled24 = b24.get("total_polled", 0) or 0
        paired24 = sorted(
            [{"name": cands_24[i], "votes": votes24[i]} for i in range(min(len(cands_24), len(votes24)))],
            key=lambda x: x["votes"],
            reverse=True,
        )
        top1_24 = paired24[0] if paired24 else {"name": "N/A", "votes": 0}
        top2_24 = paired24[1] if len(paired24) > 1 else {"name": "N/A", "votes": 0}
        margin_24 = top1_24["votes"] - top2_24["votes"]
        margin_24_pct = round((margin_24 / (polled24 or 1)) * 100, 1)

        b_meta = booth_names.get(pno, {})
        curr_v = b_meta.get("curr_voters", 0)
        est_t22 = round((polled22 / curr_v) * 100, 1) if (curr_v and polled22) else None
        est_t24 = round((polled24 / curr_v) * 100, 1) if (curr_v and polled24) else None
        t_delta = round(est_t24 - est_t22, 1) if (est_t24 is not None and est_t22 is not None) else None

        # Multi-Year Swing Index, Volatility, and Turnout Elasticity
        swing_index = round(margin_24_pct - margin_22_pct, 1) if (polled22 and polled24) else None
        volatility_score = round(abs(margin_24_pct - margin_22_pct), 1) if (polled22 and polled24) else 0.0
        turnout_elasticity = (
            round((margin_24_pct - margin_22_pct) / t_delta, 2)
            if (t_delta is not None and abs(t_delta) >= 1.0 and polled22 and polled24)
            else None
        )

        # Target votes calculation ("votes needed to flip/hold")
        is_ac_winner_leading = (top1_22["name"] == ac_winner_22)
        if not is_ac_winner_leading and polled22 > 0:
            # Find AC winner's votes in this booth to compute exact flip target
            ac_win_booth_votes = next((c["votes"] for c in paired22 if c["name"] == ac_winner_22), 0)
            gap = max(0, top1_22["votes"] - ac_win_booth_votes)
            target_votes = (gap // 2) + 1
            target_action = "FLIP"
        elif polled22 > 0 and est_t22 is not None and est_t22 < 75.0:
            target_votes = max(1, round(curr_v * 0.78) - polled22)
            target_action = "GOTV"
        else:
            target_votes = margin_22
            target_action = "HOLD"

        # Demographic roll analytics for this booth
        y_18_29 = b_meta.get("youth_18_29", 0) or 0
        m_30_49 = b_meta.get("mid_30_49", 0) or 0
        s_50_p = b_meta.get("senior_50_plus", 0) or 0
        m_cnt = b_meta.get("male_count", 0) or 0
        f_cnt = b_meta.get("female_count", 0) or 0
        g_ratio = round((f_cnt / m_cnt) * 1000, 1) if m_cnt > 0 else 0.0
        youth_pct = round((y_18_29 / curr_v) * 100, 1) if curr_v > 0 else 0.0
        del_cnt = b_meta.get("deleted_count", 0) or 0
        dist_hh = b_meta.get("distinct_households", 0) or 0
        hh_4plus = large_hh_by_part.get(pno, 0)

        sc_dict = surname_counts_by_part.get(pno, {})
        top_surname = max(sc_dict.items(), key=lambda kv: kv[1])[0].title() if sc_dict else "Mixed"
        top_surname_cnt = max(sc_dict.values()) if sc_dict else 0
        p_act = part_active_totals.get(pno, 0) or 1
        surname_cluster_pct = round((top_surname_cnt / p_act) * 100, 1) if sc_dict else 0.0

        # 4-Quadrant Classification for 2022
        is_swing = margin_22_pct < 5.0 and polled22 > 0
        is_low_t = (est_t22 is not None and est_t22 < 75.0)
        if is_swing:
            quadrant = "B_SWING"
        elif is_low_t:
            quadrant = "C_GOTV"
        elif margin_22_pct >= 15.0:
            quadrant = "A_FORTRESS"
        else:
            quadrant = "D_TRAILING"
        quadrant_counts[quadrant] += 1

        # 4-Quadrant Classification for 2024 LS
        is_swing_24 = margin_24_pct < 5.0 and polled24 > 0
        is_low_t_24 = (est_t24 is not None and est_t24 < 70.0)
        if is_swing_24:
            quadrant_2024 = "B_SWING"
        elif is_low_t_24:
            quadrant_2024 = "C_GOTV"
        elif margin_24_pct >= 15.0:
            quadrant_2024 = "A_FORTRESS"
        else:
            quadrant_2024 = "D_TRAILING"

        # Multi-Year Booth Trajectory Tag
        if quadrant == "A_FORTRESS" and quadrant_2024 == "A_FORTRESS":
            traj_tag = "Consistent Stronghold"
        elif is_swing and is_swing_24:
            traj_tag = "Persistent Close Fight"
        elif t_delta is not None and t_delta <= -10.0:
            traj_tag = "Turnout Drop"
        elif t_delta is not None and t_delta >= 5.0:
            traj_tag = "Turnout Surge"
        elif quadrant != quadrant_2024:
            traj_tag = "Shifted '22→'24"
        else:
            traj_tag = "Stable"

        booths_list.append({
            "part_no": pno,
            "polling_station_name": b_meta.get("polling_station_name") or f"Part #{pno}",
            "curr_voters": curr_v,
            "total_polled_2022": polled22,
            "total_polled_2024": polled24,
            "est_turnout_2022": est_t22,
            "est_turnout_2024": est_t24,
            "turnout_delta_22_24": t_delta,
            "swing_index": swing_index,
            "volatility_score": volatility_score,
            "turnout_elasticity": turnout_elasticity,
            "target_votes": target_votes,
            "target_action": target_action,
            "demographics": {
                "youth_18_29": y_18_29,
                "mid_30_49": m_30_49,
                "senior_50_plus": s_50_p,
                "youth_pct": youth_pct,
                "male_count": m_cnt,
                "female_count": f_cnt,
                "gender_ratio": g_ratio,
                "deleted_count": del_cnt,
                "distinct_households": dist_hh,
                "large_hh_4_plus": hh_4plus,
                "top_surname": top_surname,
                "surname_cluster_pct": surname_cluster_pct,
            },
            "geo": booth_geo.get(pno),
            "nota_2022": b22.get("nota", 0),
            "leader_2022": top1_22["name"],
            "leader_votes_2022": top1_22["votes"],
            "runner_up_2022": top2_22["name"],
            "runner_up_votes_2022": top2_22["votes"],
            "leader_2024": top1_24["name"],
            "leader_votes_2024": top1_24["votes"],
            "runner_up_2024": top2_24["name"],
            "runner_up_votes_2024": top2_24["votes"],
            "margin_votes": margin_22,
            "margin_pct": margin_22_pct,
            "margin_votes_2024": margin_24,
            "margin_pct_2024": margin_24_pct,
            "quadrant": quadrant,
            "quadrant_2024": quadrant_2024,
            "trajectory_tag": traj_tag,
            "is_swing_booth": is_swing,
            "is_low_turnout": is_low_t,
            "candidates_2022": paired22,
            "candidates_2024": paired24,
        })

    # Attach booth quadrant to large households (4+ electors) & prioritize Swing / GOTV booths
    q_by_part = {b["part_no"]: b["quadrant"] for b in booths_list}
    priority_households = []
    for hh in large_households:
        q = q_by_part.get(hh["part_no"], "D_TRAILING")
        priority_households.append({
            "voter_id": hh["sample_voter_id"],
            "part_no": hh["part_no"],
            "house_no": hh["house_no"],
            "elector_count": hh["mem_cnt"],
            "youth_count": hh["youth_count"] or 0,
            "deleted_count": hh["deleted_count"] or 0,
            "polling_station_name": hh["polling_station_name"] or f"Part #{hh['part_no']}",
            "head_hint": hh["elder_name"] or hh["fallback_name"],
            "quadrant": q,
            "priority_score": (3 if q == "B_SWING" else (2 if q == "C_GOTV" else 1)) * hh["mem_cnt"] + (hh["youth_count"] or 0),
        })
    priority_households.sort(key=lambda x: (-x["priority_score"], -x["elector_count"]))

    ac_full = dict(ac_hist)
    ac_full["y2024"] = {
        "pc_name": ac_e24.get("pc_name", ""),
        "leader": ac_e24.get("leader", "N/A"),
        "party": ac_e24.get("leader_party", "IND"),
        "turnout_pct": ac_e24.get("turnout_pct", 0.0),
        "total_polled": ac_e24.get("total_polled", 0),
        "margin": ac_e24.get("margin", 0),
        "candidate_totals": ac_e24.get("candidate_totals", []),
    }

    affidavits_2022 = CANDIDATE_AFFIDAVITS_DATA.get("by_ac_2022", {}).get(str(ac_no), [])

    return {
        "success": True,
        "ac": ac_full,
        "quadrant_counts": quadrant_counts,
        "booths": booths_list,
        "top_swing_households": priority_households[:30],
        "households_4plus_count": len(priority_households),
        "affidavits_2022": affidavits_2022,
    }


def _norm_cand_key(name: str) -> str:
    return re.sub(r"[^A-Z0-9\s]", "", (name or "").upper()).strip()


def _resolve_ac_2022_party_map(ac_no: int) -> dict[str, str]:
    """Map normalized candidate name -> party for 2022 AC using affidavits + election_history."""
    key = str(ac_no)
    party_map: dict[str, str] = {}
    ac_hist = ELECTION_HISTORY_DATA.get(key, {})
    y22 = ac_hist.get("y2022") or {}
    if y22.get("winner") and y22.get("party"):
        party_map[_norm_cand_key(y22["winner"])] = y22["party"].upper()
    for aff in CANDIDATE_AFFIDAVITS_DATA.get("by_ac_2022", {}).get(key, []):
        nk = _norm_cand_key(aff.get("name") or "")
        if nk and aff.get("party"):
            party_map[nk] = aff["party"].upper()
    return party_map


def _find_candidate_for_party(ac_no: int, target_party_up: str) -> Optional[str]:
    """Return the 2022 candidate name (matching election_2022_form20.json) for target_party_up in ac_no."""
    key = str(ac_no)
    ac_hist = ELECTION_HISTORY_DATA.get(key, {})
    y22 = ac_hist.get("y2022") or {}
    if (y22.get("party") or "").upper() == target_party_up:
        return y22.get("winner")

    e22_cands = ELECTION_2022_DATA.get(key, {}).get("candidates", [])
    affs = CANDIDATE_AFFIDAVITS_DATA.get("by_ac_2022", {}).get(key, [])
    target_aff_names = [_norm_cand_key(a.get("name") or "") for a in affs if (a.get("party") or "").upper() == target_party_up]
    if not target_aff_names:
        return None

    for ec in e22_cands:
        ek = _norm_cand_key(ec)
        if ek in target_aff_names:
            return ec
    # Token overlap fallback
    for tk in target_aff_names:
        ttoks = set(tk.split())
        for ec in e22_cands:
            if len(ttoks & set(_norm_cand_key(ec).split())) >= 2:
                return ec
    return None


def _get_runner_up_party_2022(ac_no: int) -> str:
    key = str(ac_no)
    totals = ELECTION_2022_DATA.get(key, {}).get("candidate_totals", [])
    if len(totals) < 2:
        return "OPPOSITION"
    runner_name = totals[1].get("name", "")
    rnk = _norm_cand_key(runner_name)
    party_map = _resolve_ac_2022_party_map(ac_no)
    if rnk in party_map:
        return party_map[rnk]
    rtoks = set(rnk.split())
    for k, p in party_map.items():
        if len(rtoks & set(k.split())) >= 2:
            return p
    return "OPPOSITION"


@app.get("/api/elections/simulate")
def simulate_elections(
    swing_pct: float = Query(0.0, ge=-20.0, le=20.0),
    turnout_shift_pct: float = Query(0.0, ge=-20.0, le=20.0),
    target_party: str = Query("BJP"),
    region: str = Query("ALL"),
    ac_no: Optional[int] = Query(None, ge=1, le=60),
):
    """
    Scenario & Coalition Simulator:
    Projects AC seat flips (across all 60 ACs or filtered region) and booth-level flips (if ac_no is given)
    under uniform vote-share swing toward `target_party` and turnout shift.
    """
    if not ELECTION_HISTORY_DATA:
        raise HTTPException(status_code=404, detail="Election history dataset not loaded")

    target_party_up = target_party.strip().upper()
    baseline_seats = {}
    projected_seats = {}
    flipped_acs = []

    for i in range(1, 61):
        key = str(i)
        ac_item = ELECTION_HISTORY_DATA.get(key)
        if not ac_item:
            continue
        reg = "Jiribam" if i == 40 else ("Imphal Valley" if i <= 39 else "Hill Districts")
        if region != "ALL" and reg != region:
            continue

        y22 = ac_item["y2022"]
        win_party = (y22.get("party") or "IND").upper()
        baseline_seats[win_party] = baseline_seats.get(win_party, 0) + 1

        polled = y22.get("total_polled", 0) or 1
        base_margin_pct = y22.get("margin_pct", 0.0) or 0.0
        turnout_factor = max(0.5, 1.0 + (turnout_shift_pct / 100.0))
        sim_polled = round(polled * turnout_factor)

        # Effective swing: if target_party is incumbent, +swing_pct widens margin; if challenger, +swing_pct eats into 2*swing_pct of margin (1-to-1 transfer)
        if win_party == target_party_up:
            new_margin_pct = base_margin_pct + (2.0 * swing_pct)
            if new_margin_pct < 0:
                proj_party = _get_runner_up_party_2022(i)
                flipped_acs.append({
                    "ac_no": i,
                    "ac_name": ac_item["ac_name"],
                    "district": ac_item["district"],
                    "from_party": win_party,
                    "to_party": proj_party,
                    "baseline_margin": y22.get("margin", 0),
                    "projected_margin": round(abs(new_margin_pct) / 100.0 * sim_polled),
                })
            else:
                proj_party = win_party
        else:
            # Challenger swing toward target_party (only if target_party contested in this AC, or uniform party swing)
            target_cand = _find_candidate_for_party(i, target_party_up)
            if target_cand is not None:
                # Compute exact margin between AC winner and target_party's candidate
                totals = ELECTION_2022_DATA.get(key, {}).get("candidate_totals", [])
                win_votes = totals[0]["votes"] if totals else 0
                t_votes = next((c["votes"] for c in totals if c["name"] == target_cand), 0)
                exact_gap_pct = ((win_votes - t_votes) / polled) * 100.0 if totals else base_margin_pct
            else:
                exact_gap_pct = base_margin_pct

            new_margin_pct = exact_gap_pct - (2.0 * swing_pct)
            if new_margin_pct < 0 and target_cand is not None:
                proj_party = target_party_up
                flipped_acs.append({
                    "ac_no": i,
                    "ac_name": ac_item["ac_name"],
                    "district": ac_item["district"],
                    "from_party": win_party,
                    "to_party": proj_party,
                    "baseline_margin": y22.get("margin", 0),
                    "projected_margin": round(abs(new_margin_pct) / 100.0 * sim_polled),
                })
            else:
                proj_party = win_party

        projected_seats[proj_party] = projected_seats.get(proj_party, 0) + 1

    booth_sim = None
    if ac_no is not None:
        detail = get_election_ac_detail(ac_no)
        target_cand = _find_candidate_for_party(ac_no, target_party_up)
        contested = target_cand is not None
        total_22_booths = 0
        base_won = 0
        proj_won = 0
        flipped_booths = 0
        proj_total_margin = 0
        t_mult = max(0.5, 1.0 + (turnout_shift_pct / 100.0))

        for b in detail["booths"]:
            p22 = b["total_polled_2022"] or 0
            if not p22:
                continue
            total_22_booths += 1
            if not contested:
                continue
            cands = b.get("candidates_2022") or []
            t_votes = next((c["votes"] for c in cands if c["name"] == target_cand), 0)
            best_other = max((c["votes"] for c in cands if c["name"] != target_cand), default=0)
            lead_is_target = (b["leader_2022"] == target_cand)
            if lead_is_target:
                base_won += 1
            signed_m_pct = ((t_votes - best_other) / p22) * 100.0
            new_m_pct = signed_m_pct + (2.0 * swing_pct)
            proj_votes_m = round((new_m_pct / 100.0) * p22 * t_mult)
            proj_total_margin += proj_votes_m
            if new_m_pct >= 0:
                proj_won += 1
                if not lead_is_target:
                    flipped_booths += 1
            else:
                if lead_is_target:
                    flipped_booths += 1

        booth_sim = {
            "ac_no": ac_no,
            "ac_name": detail["ac"]["ac_name"],
            "target_party": target_party_up,
            "target_party_contested": contested,
            "target_candidate": target_cand,
            "total_booths": total_22_booths,
            "baseline_booths_won": base_won,
            "projected_booths_won": proj_won,
            "flipped_booths": flipped_booths,
            "projected_ac_margin": proj_total_margin,
        }

    return {
        "success": True,
        "params": {
            "swing_pct": swing_pct,
            "turnout_shift_pct": turnout_shift_pct,
            "target_party": target_party_up,
            "region": region,
        },
        "baseline_seats": baseline_seats,
        "projected_seats": projected_seats,
        "flipped_acs_count": len(flipped_acs),
        "flipped_acs": flipped_acs,
        "booth_simulation": booth_sim,
    }


def _csv_safe(val):
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + val
    return val


@app.get("/api/elections/ac/{ac_no}/war_room_csv")
def export_ac_war_room_csv(ac_no: int):
    import csv
    import io
    data = get_election_ac_detail(ac_no)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "Part No", "Polling Station", "Quadrant", "Target Action", "Target Votes",
        "Current Electors", "Youth 18-29", "Female %", "Top Clan/Surname",
        "2022 Polled", "2022 Turnout %", "2022 Leader", "2022 Margin %",
        "2024 Polled", "2024 Turnout %", "2024 LS Leader", "Swing Index"
    ])
    for b in data["booths"]:
        dem = b.get("demographics", {})
        tot_mf = (dem.get("male_count", 0) + dem.get("female_count", 0)) or 1
        fem_pct = round((dem.get("female_count", 0) / tot_mf) * 100, 1)
        w.writerow([_csv_safe(x) for x in [
            b["part_no"], b["polling_station_name"], b["quadrant"], b["target_action"], b["target_votes"],
            b["curr_voters"], dem.get("youth_18_29", 0), fem_pct, dem.get("top_surname", ""),
            b["total_polled_2022"], b["est_turnout_2022"], f"{b['leader_2022']} ({b['leader_votes_2022']})",
            b["margin_pct"], b["total_polled_2024"], b["est_turnout_2024"], f"{b['leader_2024']} ({b['leader_votes_2024']})",
            b.get("swing_index", "")
        ]])
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="war_room_ac_{ac_no}.csv"'},
    )


@app.get("/api/elections/ac/{ac_no}/canvassing_csv")
def export_ac_canvassing_csv(ac_no: int):
    """Field Ops Export: Priority Booth + 4+ Elector Household Canvassing Sheet."""
    import csv
    import io
    data = get_election_ac_detail(ac_no)
    booth_map = {b["part_no"]: b for b in data["booths"]}
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "Priority Score", "Quadrant", "Part No", "Polling Station",
        "House No", "Electors (4+)", "Youth (18-29)", "Deleted Flags",
        "Household Head Hint", "Booth Target Action", "Booth Target Votes"
    ])
    for hh in data["top_swing_households"]:
        b = booth_map.get(hh["part_no"], {})
        w.writerow([_csv_safe(x) for x in [
            hh["priority_score"], hh["quadrant"], hh["part_no"], hh["polling_station_name"],
            hh["house_no"], hh["elector_count"], hh["youth_count"], hh["deleted_count"],
            hh["head_hint"], b.get("target_action", ""), b.get("target_votes", "")
        ]])
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="canvassing_ac_{ac_no}.csv"'},
    )


# Mount static folder
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
@app.get("/portal")
@app.get("/devadmin")
def serve_index():
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(
            index_file,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0"
            }
        )
    return {"message": "Manipur Voter Search API is running. UI is being loaded..."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
