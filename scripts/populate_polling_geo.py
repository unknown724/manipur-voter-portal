"""
Populate polling_station_geo table in voters.db for all distinct (ac_no, part_no)
using verified Manipur village/leikai centroids and AC headquarters centroids.
Preserves any existing OSM_OVERPASS / EXACT_BUILDING entries.
"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DB_FILE = os.path.join(ROOT, "voters.db")

from server import AC_CENTROIDS, find_village_anchor


def main():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT ac_no, part_no,
               MAX(polling_station_name) AS ps_name,
               MAX(section_name) AS sec_name
        FROM voters
        GROUP BY ac_no, part_no
    """)
    rows = cur.fetchall()
    print(f"Found {len(rows)} distinct (ac_no, part_no) polling stations in voters.db")

    cur.execute("SELECT ac_no, part_no, source FROM polling_station_geo")
    existing = {(r["ac_no"], r["part_no"]): r["source"] for r in cur.fetchall()}

    inserted = 0
    locality_hits = 0
    for r in rows:
        ac_no = r["ac_no"]
        part_no = r["part_no"]
        ps_name = r["ps_name"] or f"Part #{part_no}"
        sec_name = r["sec_name"] or ""

        if (ac_no, part_no) in existing and existing[(ac_no, part_no)] in ("OSM_OVERPASS", "EXACT_BUILDING"):
            continue

        v_name, v_coords = find_village_anchor(sec_name, ps_name)
        if v_coords:
            lat, lng = v_coords[0], v_coords[1]
            src = "VERIFIED_LOCALITY"
            locality_hits += 1
        else:
            base = AC_CENTROIDS.get(ac_no, [24.8170, 93.9368])
            lat, lng = base[0], base[1]
            src = "AC_AREA_APPROX"

        cur.execute("""
            INSERT OR REPLACE INTO polling_station_geo (ac_no, part_no, station_name, latitude, longitude, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (ac_no, part_no, ps_name, lat, lng, src))
        inserted += 1

    conn.commit()
    conn.close()
    print(f"Populated {inserted} rows into polling_station_geo ({locality_hits} verified locality matches)")


if __name__ == "__main__":
    main()
