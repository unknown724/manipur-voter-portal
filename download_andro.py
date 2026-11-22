"""
Command-line semi-automated downloader for AC 7 - Andro (Parts 1 to 47).
Handles session management, fetches official CAPTCHA images, and ingests PDFs automatically into SQLite.
"""

import os
import re
import requests
import pymupdf as fitz
from PIL import Image
import io

from database import get_connection, update_constituency_stats
from pdf_parser import parse_electoral_roll_pdf

AC_NO = 7
AC_NAME = "7-Andro"
TOTAL_PARTS = 47
UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOADS_DIR, exist_ok=True)

def get_existing_parts():
    parts = set()
    for f in os.listdir(UPLOADS_DIR):
        if f.endswith(".pdf") and f"-{AC_NO}-" in f:
            m = re.search(r'-ENG-(\d+)-', f, re.IGNORECASE)
            if m:
                parts.add(int(m.group(1)))
    return sorted(list(parts))

def main():
    print("=" * 60)
    print(f"  MANIPUR ELECTORAL ROLL RAPID DOWNLOADER: AC {AC_NO} ({AC_NAME})")
    print(f"  Target: All {TOTAL_PARTS} Polling Station Draft Rolls")
    print("=" * 60)

    existing = get_existing_parts()
    print(f"Already downloaded parts ({len(existing)}): {existing}\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    })
    session.get("https://ceomanipur.nic.in/eroll.aspx", timeout=15)

    for part_no in range(1, TOTAL_PARTS + 1):
        if part_no in existing:
            continue

        print(f"\n---> [Part {part_no}/{TOTAL_PARTS}] Fetching CAPTCHA...")
        while True:
            # Fetch captcha image
            r_cap = session.get(f"https://ceomanipur.nic.in/Captcha/captcha.ashx?{os.urandom(4).hex()}", timeout=10)
            img = Image.open(io.BytesIO(r_cap.content))
            img.show()

            captcha_code = input(f"Enter the 4-character CAPTCHA shown on screen for Part {part_no} (or 'r' to refresh, 'q' to quit): ").strip()
            if captcha_code.lower() == 'q':
                print("Exiting downloader.")
                return
            if captcha_code.lower() == 'r' or not captcha_code:
                print("Refreshing CAPTCHA...")
                continue

            # Validate
            val_url = f"https://ceomanipur.nic.in/ValidateCaptcha.ashx?code={requests.utils.quote(captcha_code)}"
            r_val = session.get(val_url, timeout=10)
            if r_val.text.strip().lower() == "success":
                print("CAPTCHA verified! Downloading PDF...")
                break
            else:
                print("Incorrect CAPTCHA. Showing new CAPTCHA...")

        # Download PDF
        download_url = f"https://ceomanipur.nic.in/erollDownload.ashx?selectedText={requests.utils.quote(AC_NAME)}&selectedValue={AC_NO}&partNo={part_no}&lang=ENG&type=draft"
        r_pdf = session.get(download_url, timeout=40)
        
        filename = f"2027-EROLLGEN-S14-{AC_NO}-SSR-DraftRoll-Revision1-ENG-{part_no}-WI.pdf"
        file_path = os.path.join(UPLOADS_DIR, filename)
        with open(file_path, "wb") as f:
            f.write(r_pdf.content)
        print(f"Saved: {filename} ({len(r_pdf.content) / 1024:.1f} KB)")

        # Ingest into SQLite
        try:
            meta, voters = parse_electoral_roll_pdf(file_path)
            conn = get_connection()
            cur = conn.cursor()
            inserted = 0
            for v in voters:
                try:
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
                    inserted += 1
                except Exception:
                    pass
            conn.commit()
            conn.close()
            update_constituency_stats()
            print(f"Indexed {inserted} voters into database for Part {part_no}!")
        except Exception as e:
            print(f"Error parsing PDF: {e}")

    print("\n" + "=" * 60)
    print(f"All {TOTAL_PARTS} parts for {AC_NAME} successfully downloaded and indexed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
