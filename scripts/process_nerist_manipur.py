import json
import sqlite3
import os
import re
import time
import pymupdf

STUDENTS_JSON = r'C:\Users\Richard Konsam\Desktop\DEVANANDA\Student search\src\data\students.json'
DOSSIERS_CACHE_JSON = r'C:\Users\Richard Konsam\Desktop\DEVANANDA\Student search\src\data\dossiers_cache.json'
VOTERS_DB = 'voters.db'
OUTPUT_DIR = 'data'
os.makedirs(OUTPUT_DIR, exist_ok=True)
SLIPS_DIR = os.path.join(OUTPUT_DIR, 'slips')
os.makedirs(SLIPS_DIR, exist_ok=True)

# 1. Load Data
with open(STUDENTS_JSON, 'r', encoding='utf-8') as f:
    all_students = json.load(f)

with open(DOSSIERS_CACHE_JSON, 'r', encoding='utf-8') as f:
    dossiers_cache = json.load(f)

MANIPUR_SURNAMES = {
    'konsam', 'sorokhaibam', 'chingakham', 'thokchom', 'laishram', 'ningthoujam', 'haobam',
    'yumkhaibam', 'sagolsem', 'mayengbam', 'kshetrimayum', 'irom', 'yumnam', 'pebam',
    'oinam', 'salam', 'sapam', 'khundrakpam', 'mutum', 'sanasam', 'thoudam', 'thounaojam',
    'thangjam', 'naorem', 'nongmaithem', 'nongthombam', 'keisham', 'khuraijam', 'moirangthem',
    'maibam', 'khumukcham', 'khundongbam', 'khamnam', 'huidrom', 'hijam', 'hidam', 'irungbam',
    'irengbam', 'potsangbam', 'phurailatpam', 'leishangthem', 'lourembam', 'leitanthem',
    'toijam', 'yambem', 'yaikhom', 'brahmacharimayum', 'rajkumar', 'elam', 'taorem', 'kolom',
    'chabungbam', 'tongbram', 'heikham', 'haokip', 'kipgen', 'shimray', 'gangte', 'baite',
    'paite', 'vaiphei', 'maring', 'tangkhul', 'kamei', 'rongmei', 'serto', 'jaiche'
}

def is_manipur_student(s):
    st = str(s.get('state') or '').lower()
    pin = str(s.get('pincode') or '')
    name = str(s.get('full_name') or '').lower()
    
    if 'manipur' in st or pin.startswith('795'):
        return True, 'explicit'
    
    if s.get('state') is None:
        first_token = name.split()[0] if name else ''
        last_token = name.split()[-1] if name else ''
        if first_token in MANIPUR_SURNAMES or last_token in MANIPUR_SURNAMES:
            return True, 'heuristic'
    return False, 'none'

cohort = []
for s in all_students:
    matched, method = is_manipur_student(s)
    if matched:
        uid = s.get('user_id')
        session = (dossiers_cache.get(uid) or {}).get('session', '2025_2')
        cohort.append({
            'user_id': uid,
            'roll_no': s.get('roll_no'),
            'full_name': s.get('full_name'),
            'student_mobile': s.get('mobile'),
            'email': s.get('email'),
            'department_name': s.get('department_name'),
            'program_name': s.get('program_name'),
            'state': s.get('state'),
            'pincode': s.get('pincode'),
            'session': session,
            'method': method
        })

print(f"Total classified Manipur cohort candidates: {len(cohort)}")

# 2. Parse Ground-Truth from Slips
def parse_slip_pdf(pdf_path):
    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 1000:
        return {}
    try:
        doc = pymupdf.open(pdf_path)
        page = doc[0]
        words = page.get_text("words")
        
        lines = []
        curr_y = None
        curr_line = []
        for w in sorted(words, key=lambda x: (round(x[1] / 4) * 4, x[0])):
            y_bucket = round(w[1] / 4) * 4
            if curr_y is None or abs(curr_y - y_bucket) <= 4:
                curr_y = y_bucket if curr_y is None else curr_y
                curr_line.append(w)
            else:
                curr_line.sort(key=lambda item: item[0])
                lines.append(" ".join(item[4] for item in curr_line))
                curr_y = y_bucket
                curr_line = [w]
        if curr_line:
            curr_line.sort(key=lambda item: item[0])
            lines.append(" ".join(item[4] for item in curr_line))
            
        father_name = None
        mother_name = None
        dob = None
        parents_mobile = None
        mobile = None
        address = None
        pincode = None
        
        for l in lines:
            if "Father's Name:" in l:
                m = re.search(r"Father's Name:\s*(.*?)(?:Mother's Name:|Mobile:|Email:|Parents Mobile:|$|Aadhaar|Address|Parents)", l, re.I)
                if m and m.group(1).strip():
                    father_name = m.group(1).strip()
            if "Mother's Name:" in l:
                m = re.search(r"Mother's Name:\s*(.*?)(?:Father's Name:|Mobile:|Email:|Parents Mobile:|$|Aadhaar|Address)", l, re.I)
                if m and m.group(1).strip():
                    mother_name = m.group(1).strip()
            if "DOB:" in l:
                m = re.search(r"DOB:\s*([0-9\-]+)", l, re.I)
                if m:
                    dob = m.group(1).strip()
            if "Parents Mobile" in l or "Parents Phone" in l:
                m = re.search(r"Parents Mobile\s*[:\+]?\s*([0-9\+]+)", l, re.I)
                if m:
                    parents_mobile = m.group(1).strip()
            if "Mobile:" in l:
                m = re.search(r"Mobile:\s*([0-9\+]+)", l, re.I)
                if m:
                    mobile = m.group(1).strip()
            if "Address:" in l:
                m = re.search(r"Address:\s*(.*?)(?:Parents Mobile|Roll No|$)", l, re.I)
                if m and m.group(1).strip():
                    address = m.group(1).strip()
            
            pin_m = re.search(r"\b(795\d{3})\b", l)
            if pin_m:
                pincode = pin_m.group(1)
                
        return {
            'father_name': father_name,
            'mother_name': mother_name,
            'dob': dob,
            'parents_mobile': parents_mobile,
            'slip_mobile': mobile,
            'address': address,
            'pincode': pincode
        }
    except Exception as e:
        return {'error': str(e)}

print("Extracting dossiers from downloaded slips...")
ground_truth = []
for s in cohort:
    uid = s['user_id']
    formatted_uid = uid.replace('/', '_')
    pdf_path = os.path.join(SLIPS_DIR, f"{formatted_uid}.pdf")
    parsed = parse_slip_pdf(pdf_path)
    
    dob = parsed.get('dob')
    calc_age = None
    if dob:
        try:
            birth_year = int(dob.split('-')[0])
            calc_age = 2026 - birth_year
        except Exception:
            pass
            
    item = {
        'user_id': uid,
        'roll_no': s.get('roll_no') or '',
        'student_name': s.get('full_name') or '',
        'student_mobile': parsed.get('slip_mobile') or s.get('student_mobile') or '',
        'email': s.get('email') or '',
        'father_name': parsed.get('father_name') or '',
        'mother_name': parsed.get('mother_name') or '',
        'parents_mobile': parsed.get('parents_mobile') or '',
        'dob': dob or '',
        'calculated_age': calc_age,
        'address': parsed.get('address') or '',
        'pincode': parsed.get('pincode') or s.get('pincode') or '',
        'department_name': s.get('department_name') or '',
        'program_name': s.get('program_name') or '',
        'session': s.get('session') or ''
    }
    ground_truth.append(item)

print(f"Extracted {len(ground_truth)} student dossiers.")

# 3. High-Speed SQLite FTS5 Linkage Engine
conn = sqlite3.connect(VOTERS_DB)
c = conn.cursor()

def clean_tokens(name):
    if not name:
        return []
    cleaned = re.sub(r'[^a-zA-Z\s]', ' ', name).lower()
    return [t for t in cleaned.split() if t not in {'singh', 'devi', 'meitei', 'meetei', 'chanu', 'late', 'mr', 'mrs', 'dr', 'shri', 'smt', 'md', 'mohd'} and len(t) >= 2]

def search_fts_name(tokens, limit=5):
    if not tokens:
        return []
    fts_expr = " ".join(f"name:{t}" for t in tokens)
    try:
        c.execute("""
            SELECT v.id, v.epic_no, v.name, v.relative_name, v.age, v.gender, v.ac_no, v.ac_name, v.part_no, v.house_no
            FROM voters_fts f
            JOIN voters v ON f.rowid = v.id
            WHERE voters_fts MATCH ?
            LIMIT ?;
        """, (fts_expr, limit))
        return c.fetchall()
    except Exception:
        try:
            fts_expr = f"name:{tokens[0]}"
            c.execute("""
                SELECT v.id, v.epic_no, v.name, v.relative_name, v.age, v.gender, v.ac_no, v.ac_name, v.part_no, v.house_no
                FROM voters_fts f
                JOIN voters v ON f.rowid = v.id
                WHERE voters_fts MATCH ?
                LIMIT ?;
            """, (fts_expr, limit))
            return c.fetchall()
        except Exception:
            return []

matched_count = 0
triangulated_count = 0
t0 = time.time()

for item in ground_truth:
    s_name = item['student_name']
    f_name = item['father_name']
    m_name = item['mother_name']
    s_age = item['calculated_age']
    
    student_elector = None
    father_elector = None
    mother_elector = None
    
    s_tokens = clean_tokens(s_name)
    f_tokens = clean_tokens(f_name)
    m_tokens = clean_tokens(m_name)
    
    # 1. Search Student in FTS5
    if s_tokens and (s_age is None or s_age >= 18):
        s_candidates = search_fts_name(s_tokens, limit=10)
        for cand in s_candidates:
            rel = (cand[3] or '').lower()
            rel_ok = any(ft in rel for ft in f_tokens) or any(mt in rel for mt in m_tokens)
            if rel_ok:
                student_elector = cand
                break
        if not student_elector and s_candidates:
            if s_age:
                student_elector = min(s_candidates, key=lambda c: abs(c[4] - s_age))
            else:
                student_elector = s_candidates[0]
                
    # 2. Search Mother in FTS5
    if m_tokens:
        m_candidates = [c for c in search_fts_name(m_tokens, limit=10) if c[5] == 'F']
        if m_candidates:
            mother_elector = m_candidates[0]
            
    # 3. Search Father in FTS5
    if f_tokens:
        f_candidates = [c for c in search_fts_name(f_tokens, limit=10) if c[5] == 'M']
        if f_candidates:
            father_elector = f_candidates[0]
            
    # 4. Household Triangulation & Grading
    status = 'UNMATCHED'
    notes = 'No elector found'
    ac_no = None
    ac_name = None
    part_no = None
    house_no = None
    
    p_cand = father_elector or mother_elector
    
    if student_elector and p_cand and student_elector[6] == p_cand[6] and student_elector[8] == p_cand[8] and str(student_elector[9]).strip() == str(p_cand[9]).strip():
        status = 'HOUSEHOLD_TRIANGULATED'
        notes = f"Verified Household: Student & Parent co-located in AC #{student_elector[6]} ({student_elector[7]}), Part {student_elector[8]}, House {student_elector[9]}"
        ac_no, ac_name, part_no, house_no = student_elector[6], student_elector[7], student_elector[8], student_elector[9]
        triangulated_count += 1
        matched_count += 1
    elif student_elector and p_cand and student_elector[6] == p_cand[6]:
        status = 'AC_MATCHED'
        notes = f"Matched in same AC #{student_elector[6]} ({student_elector[7]}); Polling parts {student_elector[8]} vs {p_cand[8]}"
        ac_no, ac_name, part_no, house_no = student_elector[6], student_elector[7], student_elector[8], student_elector[9]
        matched_count += 1
    elif student_elector:
        status = 'STUDENT_ELECTOR_LINKED'
        notes = f"Student enrolled as elector in AC #{student_elector[6]} ({student_elector[7]}), Part {student_elector[8]}, House {student_elector[9]}"
        ac_no, ac_name, part_no, house_no = student_elector[6], student_elector[7], student_elector[8], student_elector[9]
        matched_count += 1
    elif p_cand:
        status = 'PARENT_VERIFIED'
        notes = f"Parent verified in AC #{p_cand[6]} ({p_cand[7]}), Part {p_cand[8]}, House {p_cand[9]}"
        ac_no, ac_name, part_no, house_no = p_cand[6], p_cand[7], p_cand[8], p_cand[9]
        matched_count += 1
    else:
        if s_age and s_age < 18:
            notes = f"Student underage ({s_age}y on SSR 2027 cutoff); parent unlisted or residing outside Manipur"
        elif not f_name and not m_name:
            notes = "Registration slip lacked parental names"
        else:
            notes = f"Parent names ('{f_name}' / '{m_name}') not found in SSR 2027 roll (likely out-of-state residence, deceased parent, or spelling variation)"
            
    item['linked_student_epic'] = student_elector[1] if student_elector else ''
    item['linked_father_epic'] = father_elector[1] if father_elector else ''
    item['linked_mother_epic'] = mother_elector[1] if mother_elector else ''
    item['ac_no'] = ac_no
    item['ac_name'] = ac_name or ''
    item['part_no'] = part_no
    item['house_no'] = str(house_no) if house_no is not None else ''
    item['match_status'] = status
    item['match_notes'] = notes

print(f"FTS5 Linkage completed in {time.time()-t0:.2f}s!")

# 4. Populate Dedicated Database Table: nerist_students
c.execute("DROP TABLE IF EXISTS nerist_students;")
c.execute("""
CREATE TABLE nerist_students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT UNIQUE,
    roll_no TEXT,
    student_name TEXT,
    student_mobile TEXT,
    email TEXT,
    father_name TEXT,
    mother_name TEXT,
    parents_mobile TEXT,
    dob TEXT,
    calculated_age INTEGER,
    address TEXT,
    pincode TEXT,
    department_name TEXT,
    program_name TEXT,
    session TEXT,
    linked_student_epic TEXT,
    linked_father_epic TEXT,
    linked_mother_epic TEXT,
    ac_no INTEGER,
    ac_name TEXT,
    part_no INTEGER,
    house_no TEXT,
    match_status TEXT,
    match_notes TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
""")

for item in ground_truth:
    c.execute("""
    INSERT INTO nerist_students (
        user_id, roll_no, student_name, student_mobile, email, father_name, mother_name,
        parents_mobile, dob, calculated_age, address, pincode, department_name, program_name,
        session, linked_student_epic, linked_father_epic, linked_mother_epic, ac_no, ac_name,
        part_no, house_no, match_status, match_notes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, (
        item['user_id'], item['roll_no'], item['student_name'], item['student_mobile'], item['email'],
        item['father_name'], item['mother_name'], item['parents_mobile'], item['dob'], item['calculated_age'],
        item['address'], item['pincode'], item['department_name'], item['program_name'], item['session'],
        item['linked_student_epic'], item['linked_father_epic'], item['linked_mother_epic'],
        item['ac_no'], item['ac_name'], item['part_no'], item['house_no'], item['match_status'], item['match_notes']
    ))

conn.commit()

# Save JSON ground truth
with open(os.path.join(OUTPUT_DIR, 'nerist_manipur_ground_truth.json'), 'w', encoding='utf-8') as f:
    json.dump(ground_truth, f, indent=2)

print("\n================= LINKAGE SUMMARY =================")
print(f"Total Cohort Candidates: {len(ground_truth)}")
print(f"Total Linked with Voters/Parents: {matched_count} ({matched_count/len(ground_truth)*100:.1f}%)")
print(f"Household Triangulations: {triangulated_count}")
print(f"Populated table 'nerist_students' in {VOTERS_DB} successfully.")
conn.close()
