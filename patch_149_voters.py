import sqlite3
import glob
import re
import fitz
from pdf_parser import parse_electoral_roll_pdf
from database import get_connection, update_constituency_stats

def main():
    conn = get_connection()
    cur = conn.cursor()

    print("Step 1: Identifying the 102 parts with 149 missing voters...")
    parts_to_reparse = []

    for ac_dir in sorted(glob.glob('uploads/AC_*')):
        m_ac = re.search(r'AC_(\d+)_', ac_dir)
        if not m_ac:
            continue
        ac_no = int(m_ac.group(1))

        for fp in glob.glob(f'{ac_dir}/*.pdf'):
            m = re.search(r'-ENG-(\d+)-', fp)
            if not m:
                continue
            part_no = int(m.group(1))

            cur.execute('SELECT COUNT(*) FROM voters WHERE ac_no = ? AND part_no = ?', (ac_no, part_no))
            db_cnt = cur.fetchone()[0]

            try:
                doc = fitz.open(fp)
                last_text = doc[-1].get_text()
                m_tot = re.findall(r'(\d+)\s*\n\s*Issued by Electoral Registration', last_text)
                if not m_tot:
                    m_tot = re.findall(r'Third\s*Gender\s*Total\s*\n.*?\n(\d+)', last_text, re.DOTALL)
                doc.close()
                if m_tot:
                    expected = int(m_tot[0].strip())
                    if db_cnt < expected:
                        parts_to_reparse.append((ac_no, part_no, fp, expected - db_cnt))
            except Exception:
                pass

    print(f"Found {len(parts_to_reparse)} parts to re-parse.")

    total_added = 0
    for ac_no, part_no, fp, missing_count in parts_to_reparse:
        meta, voters = parse_electoral_roll_pdf(fp)
        for v in voters:
            cur.execute("""
            INSERT OR REPLACE INTO voters (
                epic_no, name, relative_name, relation_type, house_no, age, gender,
                ac_no, ac_name, part_no, polling_station_name, section_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                v["epic_no"], v["name"], v["relative_name"], v["relation_type"],
                v["house_no"], v["age"], v["gender"], v["ac_no"], v["ac_name"],
                v["part_no"], v["polling_station_name"], v["section_name"]
            ))
        conn.commit()
        print(f"Processed AC {ac_no} Part {part_no} (Extracted: {len(voters)})")

    conn.close()

    print("Step 2: Updating constituency summary statistics...")
    update_constituency_stats()

    # Verify final total in DB
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM voters")
    final_total = cur.fetchone()[0]
    cur.execute("SELECT gender, COUNT(*) FROM voters GROUP BY gender")
    gender_breakdown = cur.fetchall()
    conn.close()

    print(f"\n==========================================")
    print(f" FINAL DATABASE TOTAL: {final_total}")
    print(f" OFFICIAL BENCHMARK:   1,994,357")
    print(f" GENDER BREAKDOWN:     {[dict(g) for g in gender_breakdown]}")
    print(f"==========================================")

if __name__ == "__main__":
    main()
