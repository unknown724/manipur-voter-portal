"""
Ingest public ADR / MyNeta candidate affidavit records for Manipur (2022 & 2017 Assembly Elections).
Zero paid APIs — uses public HTML tables from https://www.myneta.info/Manipur2022/ and /Manipur2017/.
Saves structured JSON to `candidate_affidavits.json` and loads into `voters.db` (`candidate_affidavits` table).
"""

import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ELECTION_HISTORY_FILE = os.path.join(ROOT, "election_history.json")
OUTPUT_JSON = os.path.join(ROOT, "candidate_affidavits.json")
DB_FILE = os.path.join(ROOT, "voters.db")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ManipurElectionIntel/1.0"
}


def norm_ac_name(s: str) -> str:
    s = re.sub(r"\([^)]*\)", "", s or "")
    s = re.sub(r"[^A-Za-z0-9]", "", s).upper()
    return s


def norm_cand_name(s: str) -> str:
    s = re.sub(r"\b(winner|dr|shri|smt|mr|mrs|ms|late)\b\.?", "", s or "", flags=re.I)
    s = re.sub(r"[^A-Za-z0-9\s]", " ", s)
    return " ".join(s.upper().split())


def parse_inr(text: str) -> int:
    # Example: "Rs 1,56,44,103 ~ 1 Crore+" -> 15644103
    if not text:
        return 0
    m = re.search(r"Rs\.?\s*([0-9,]+)", text, flags=re.I)
    if not m:
        return 0
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return 0


def format_inr_short(val: int) -> str:
    if val >= 10_000_000:
        return f"₹{val / 10_000_000:.2f} Cr"
    if val >= 100_000:
        return f"₹{val / 100_000:.2f} L"
    if val > 0:
        return f"₹{val:,}"
    return "₹0"


def build_ac_lookup():
    with open(ELECTION_HISTORY_FILE, "r", encoding="utf-8") as f:
        eh = json.load(f)
    lookup = {}
    for k, v in eh.items():
        ac_no = int(k)
        ac_name = v["ac_name"]
        lookup[norm_ac_name(ac_name)] = (ac_no, ac_name)
    # Known spelling variations on MyNeta vs ECI
    aliases = {
        "THONGJU": "THONGJU",
        "KEISAMTHONG": "KEISHAMTHONG",
        "KEISHAMTHONG": "KEISHAMTHONG",
        "LAMSANG": "LAMSHANG",
        "LAMSHANG": "LAMSHANG",
        "KARONG": "KARONG",
        "MAO": "MAO",
        "TADUBI": "TADUBI",
        "TAMEI": "TAMEI",
        "TAMENGLONG": "TAMENGLONG",
        "NUNGBA": "NUNGBA",
        "TIPAMUKH": "TIPAIMUKH",
        "TIPAIMUKH": "TIPAIMUKH",
        "THANLON": "THANLON",
        "HENGLEP": "HENGLEP",
        "CHURACHANDPUR": "CHURACHANDPUR",
        "SAIKOT": "SAIKOT",
        "SINGHAT": "SINGHAT",
        "SAITU": "SAITU",
        "KANGPOKPI": "KANGPOKPI",
        "SAIKUL": "SAIKUL",
        "TENGNOUPAL": "TENGNOUPAL",
        "PHUNGYAR": "PHUNGYAR",
        "UKHRUL": "UKHRUL",
        "CHINGAI": "CHINGAI",
        "CHANDEL": "CHANDEL",
        "WANGJINGTENTHA": "WANGJINGTENTHA",
        "KAKCHING": "KAKCHING",
        "HIYANGLAM": "HIYANGLAM",
        "SUGNU": "SUGNOO",
        "SUGNOO": "SUGNOO",
        "WANGOI": "WANGOI",
        "MAYANGIMPHAL": "MAYANGIMPHAL",
        "NAMBOL": "NAMBOL",
        "OINAM": "OINAM",
        "BISHNUPUR": "BISHNUPUR",
        "BISHENPUR": "BISHNUPUR",
        "KHETRIGAO": "KSHETRIGAO",
        "NAORIAPAKHANGLAKPA": "NAORIYAPAKHANGLAKPA",
        "MOIRANG": "MOIRANG",
        "THANGA": "THANGA",
        "KUMBI": "KUMBI",
        "LILONG": "LILONG",
        "THOUBAL": "THOUBAL",
        "WANGKHEM": "WANGKHEM",
        "HEIROK": "HEIROK",
        "KHANGABOK": "KHANGABOK",
        "WABGAI": "WABGAI",
        "SUGNOO": "SUGNU",
    }
    return lookup, aliases


def resolve_ac(raw_name: str, lookup, aliases):
    n = norm_ac_name(raw_name)
    if n in lookup:
        return lookup[n]
    if n in aliases and aliases[n] in lookup:
        return lookup[aliases[n]]
    # Fuzzy prefix/substring fallback
    for k, val in lookup.items():
        if n in k or k in n:
            return val
    return None, raw_name


def fetch_constituency_candidates(year: int, base_url: str, href: str, link_text: str, lookup, aliases):
    url = base_url.rstrip("/") + "/" + href.lstrip("/")
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    h3 = soup.find("h3")
    raw_ac = link_text
    if h3:
        m = re.search(r"List of Candidates\s*-\s*([^:]+)", h3.get_text(" ", strip=True), flags=re.I)
        if m:
            raw_ac = m.group(1).strip()

    ac_no, ac_name = resolve_ac(raw_ac, lookup, aliases)
    if ac_no is None:
        return None, []

    candidates = []
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        hdrs = [th.get_text(" ", strip=True).lower() for th in rows[0].find_all(["th", "td"])]
        if "candidate" not in hdrs or "criminal cases" not in hdrs:
            continue
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue
            cand_cell = tds[1]
            cand_raw = cand_cell.get_text(" ", strip=True)
            is_winner = "winner" in cand_raw.lower()
            cand_name = re.sub(r"\bWinner\b", "", cand_raw, flags=re.I).strip()
            a_tag = cand_cell.find("a", href=True)
            cand_url = (base_url.rstrip("/") + "/" + a_tag["href"].lstrip("/")) if a_tag else url

            party = tds[2].get_text(" ", strip=True)
            crim_txt = tds[3].get_text(" ", strip=True)
            try:
                crim_cases = int(re.search(r"\d+", crim_txt).group(0)) if re.search(r"\d+", crim_txt) else 0
            except Exception:
                crim_cases = 0
            edu = tds[4].get_text(" ", strip=True)
            age_txt = tds[5].get_text(" ", strip=True)
            try:
                age = int(re.search(r"\d+", age_txt).group(0)) if re.search(r"\d+", age_txt) else None
            except Exception:
                age = None
            assets_txt = tds[6].get_text(" ", strip=True)
            liab_txt = tds[7].get_text(" ", strip=True) if len(tds) > 7 else ""
            assets_inr = parse_inr(assets_txt)
            liab_inr = parse_inr(liab_txt)

            candidates.append({
                "year": year,
                "ac_no": ac_no,
                "ac_name": ac_name,
                "name": cand_name,
                "norm_name": norm_cand_name(cand_name),
                "party": party,
                "is_winner": is_winner,
                "criminal_cases": crim_cases,
                "education": edu,
                "age": age,
                "total_assets_inr": assets_inr,
                "total_assets_display": format_inr_short(assets_inr),
                "liabilities_inr": liab_inr,
                "liabilities_display": format_inr_short(liab_inr),
                "source_url": cand_url,
            })
        break

    return ac_no, candidates


def scrape_year(year: int, base_url: str, lookup, aliases):
    print(f"Fetching MyNeta Manipur {year} index: {base_url}")
    r = requests.get(base_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "action=show_candidates" in href and "constituency_id=" in href and href not in seen:
            seen.add(href)
            links.append((href, a.get_text(" ", strip=True)))
    print(f"Found {len(links)} constituency links for {year}")

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_constituency_candidates, year, base_url, href, txt, lookup, aliases) for href, txt in links]
        for fut in as_completed(futs):
            ac_no, cands = fut.result()
            if ac_no is not None:
                results.extend(cands)
    print(f"Year {year}: parsed {len(results)} candidates across {len(set(c['ac_no'] for c in results))} ACs")
    return results


def main():
    lookup, aliases = build_ac_lookup()
    cands_2022 = scrape_year(2022, "https://www.myneta.info/Manipur2022/", lookup, aliases)
    cands_2017 = []
    try:
        cands_2017 = scrape_year(2017, "https://www.myneta.info/manipur2017/", lookup, aliases)
    except Exception as e:
        print(f"2017 scrape warning (non-fatal): {e}")

    # Detect candidate-level party switch (2017 -> 2022)
    hist_2017_by_name = {}
    for c in cands_2017:
        key = (c["ac_no"], c["norm_name"])
        hist_2017_by_name[key] = c["party"]

    for c in cands_2022:
        prev_party = hist_2017_by_name.get((c["ac_no"], c["norm_name"]))
        c["party_2017"] = prev_party
        c["is_party_switcher"] = bool(prev_party and prev_party.upper() != c["party"].upper())

    all_cands = cands_2022 + cands_2017
    by_ac = {}
    for c in cands_2022:
        by_ac.setdefault(str(c["ac_no"]), []).append(c)

    payload = {
        "source": "ADR / MyNeta Open Public Affidavits",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_2022_candidates": len(cands_2022),
        "total_2017_candidates": len(cands_2017),
        "acs_covered_2022": len(by_ac),
        "by_ac_2022": by_ac,
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Saved {OUTPUT_JSON}")

    # Also persist to SQLite voters.db
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS candidate_affidavits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER NOT NULL,
        ac_no INTEGER NOT NULL,
        ac_name TEXT NOT NULL,
        name TEXT NOT NULL,
        norm_name TEXT NOT NULL,
        party TEXT NOT NULL,
        is_winner INTEGER DEFAULT 0,
        criminal_cases INTEGER DEFAULT 0,
        education TEXT,
        age INTEGER,
        total_assets_inr INTEGER DEFAULT 0,
        total_assets_display TEXT,
        liabilities_inr INTEGER DEFAULT 0,
        liabilities_display TEXT,
        party_2017 TEXT,
        is_party_switcher INTEGER DEFAULT 0,
        source_url TEXT
    )
    """)
    cur.execute("DELETE FROM candidate_affidavits")
    for c in all_cands:
        cur.execute("""
        INSERT INTO candidate_affidavits (
            year, ac_no, ac_name, name, norm_name, party, is_winner,
            criminal_cases, education, age, total_assets_inr, total_assets_display,
            liabilities_inr, liabilities_display, party_2017, is_party_switcher, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            c["year"], c["ac_no"], c["ac_name"], c["name"], c["norm_name"], c["party"],
            1 if c["is_winner"] else 0, c["criminal_cases"], c["education"], c["age"],
            c["total_assets_inr"], c["total_assets_display"],
            c["liabilities_inr"], c["liabilities_display"],
            c.get("party_2017"), 1 if c.get("is_party_switcher") else 0, c["source_url"]
        ))
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cand_aff_ac_year ON candidate_affidavits(ac_no, year)")
    conn.commit()
    conn.close()
    print(f"Inserted {len(all_cands)} rows into voters.db -> candidate_affidavits")


if __name__ == "__main__":
    main()
