"""
Ingest all 47 PDF electoral rolls for AC 7 - Andro.
"""

import os
import glob
from database import get_connection, update_constituency_stats
from pdf_parser import parse_electoral_roll_pdf

def main():
    pdf_files = sorted(
        glob.glob(os.path.join("uploads", "2027-EROLLGEN-S14-7-SSR-DraftRoll-Revision1-ENG-*-WI.pdf")),
        key=lambda x: int(x.split("-ENG-")[1].split("-")[0]) if "-ENG-" in x else 0
    )
    print(f"Found {len(pdf_files)} PDF rolls for AC 7 - Andro.")

    conn = get_connection()
    cur = conn.cursor()

    # Clear previous records for AC 7 to ensure clean 100% official data
    cur.execute("DELETE FROM voters WHERE ac_no = 7")
    conn.commit()

    total_voters = 0
    male_count = 0
    female_count = 0
    other_count = 0

    for idx, f in enumerate(pdf_files, 1):
        try:
            meta, voters = parse_electoral_roll_pdf(f)
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
                if v["gender"] == "M":
                    male_count += 1
                elif v["gender"] == "F":
                    female_count += 1
                else:
                    other_count += 1

            total_voters += len(voters)
            p_no = meta.get("part_no", idx)
            ps_name = meta.get("polling_station_name", "Polling Station")
            print(f"[{idx:02d}/47] Part {p_no:02d} ({ps_name[:35]}): {len(voters)} electors")
        except Exception as e:
            print(f"Error on {f}: {e}")

    conn.commit()
    conn.close()

    print("\nUpdating database constituency statistics...")
    update_constituency_stats()

    print("\n" + "=" * 55)
    print(f"  INGESTION SUMMARY FOR AC 7 - ANDRO (ALL 47 BOOTHS)")
    print("=" * 55)
    print(f"  Total Registered Voters: {total_voters:,}")
    print(f"  Male Voters:             {male_count:,}")
    print(f"  Female Voters:           {female_count:,}")
    print(f"  Third Gender Voters:     {other_count:,}")
    print(f"  Gender Ratio (F per 1000 M): {(female_count / male_count * 1000):.0f}" if male_count else "")
    print(f"  Polling Stations:        {len(pdf_files)} booths")
    print("=" * 55)

if __name__ == "__main__":
    main()
