"""
Seed script to populate Manipur's 60 Assembly Constituencies and realistic voter records.
Includes authentic names from Manipur valley and hill districts.
"""

import random
from database import get_connection, init_db, update_constituency_stats

# All 60 Official Manipur Assembly Constituencies with Districts
MANIPUR_ACS = [
    (1, "Khundrakpam", "Imphal East"),
    (2, "Heingang", "Imphal East"),
    (3, "Khurai", "Imphal East"),
    (4, "Kshetrigao", "Imphal East"),
    (5, "Thongju", "Imphal East"),
    (6, "Keirao", "Imphal East"),
    (7, "Andro", "Imphal East"),
    (8, "Lamlai", "Imphal East"),
    (9, "Thangmeiband", "Imphal West"),
    (10, "Uripok", "Imphal West"),
    (11, "Sagolband", "Imphal West"),
    (12, "Keishamthong", "Imphal West"),
    (13, "Singjamei", "Imphal West"),
    (14, "Yaiskul", "Imphal West"),
    (15, "Wangkhei", "Imphal East"),
    (16, "Sekmai (SC)", "Imphal West"),
    (17, "Lamsang", "Imphal West"),
    (18, "Konthoujam", "Imphal West"),
    (19, "Patsoi", "Imphal West"),
    (20, "Langthabal", "Imphal West"),
    (21, "Naoriya Pakhanglakpa", "Imphal West"),
    (22, "Wangoi", "Imphal West"),
    (23, "Mayang Imphal", "Imphal West"),
    (24, "Nambol", "Bishnupur"),
    (25, "Oinam", "Bishnupur"),
    (26, "Bishnupur", "Bishnupur"),
    (27, "Moirang", "Bishnupur"),
    (28, "Thanga", "Bishnupur"),
    (29, "Kumbi", "Bishnupur"),
    (30, "Lilong", "Thoubal"),
    (31, "Thoubal", "Thoubal"),
    (32, "Wangkhem", "Thoubal"),
    (33, "Heirok", "Thoubal"),
    (34, "Wangjing Tentha", "Thoubal"),
    (35, "Khangabok", "Thoubal"),
    (36, "Wabgai", "Kakching"),
    (37, "Kakching", "Kakching"),
    (38, "Hiyanglam", "Kakching"),
    (39, "Sugnu", "Kakching"),
    (40, "Jiribam", "Jiribam"),
    (41, "Chandel (ST)", "Chandel"),
    (42, "Tengnoupal (ST)", "Tengnoupal"),
    (43, "Phungyar (ST)", "Kamjong"),
    (44, "Ukhrul (ST)", "Ukhrul"),
    (45, "Chingai (ST)", "Ukhrul"),
    (46, "Saikul (ST)", "Kangpokpi"),
    (47, "Karong (ST)", "Senapati"),
    (48, "Mao (ST)", "Senapati"),
    (49, "Tadubi (ST)", "Senapati"),
    (50, "Kangpokpi", "Kangpokpi"),
    (51, "Saitu (ST)", "Kangpokpi"),
    (52, "Tamei (ST)", "Tamenglong"),
    (53, "Tamenglong (ST)", "Tamenglong"),
    (54, "Nungba (ST)", "Noney"),
    (55, "Tipaimukh (ST)", "Pherzawl"),
    (56, "Thanlon (ST)", "Pherzawl"),
    (57, "Henglep (ST)", "Churachandpur"),
    (58, "Churachandpur (ST)", "Churachandpur"),
    (59, "Saikot (ST)", "Churachandpur"),
    (60, "Singhat (ST)", "Churachandpur")
]

# Authentic Manipuri Valley & Hill Surnames / Clannames
SURNAMES_VALLEY = [
    "Konsam", "Ningthoujam", "Thokchom", "Laishram", "Sanasam", "Heikrujam",
    "Elangbam", "Haobam", "Sorokhaibam", "Yumnam", "Wahengbam", "Nameirakpam",
    "Mayengbam", "Mutum", "Chungkham", "Oinam", "Sapam", "Takhellambam",
    "Leimapokpam", "Asem", "Salam", "Khoirom", "Nongmaithem", "Moirangthem"
]

GIVEN_NAMES_MALE_VALLEY = [
    "Devananda", "Ningthem", "Sanatomba", "Ronaldo", "Tomba", "Biren", "Ibochouba",
    "Somorjit", "Premjit", "Bikram", "Rajesh", "Chinglen", "Khomei", "Amarjit",
    "Rohit", "Surjit", "Dhanabir", "Ranjit", "Manihar", "Bembem", "Joykumar"
]

GIVEN_NAMES_FEMALE_VALLEY = [
    "Linthoingambi", "Thoi", "Mema", "Sanatombi", "Chaoba", "Ranjita", "Bimola",
    "Geeta", "Priyanka", "Memcha", "Tamphasana", "Shanti", "Ibempishak",
    "Ibemhal", "Pushparani", "Surbala", "Monica", "Premlata", "Sophia"
]

SURNAMES_HILL = [
    "Haokip", "Kipgen", "Shimray", "Zimik", "Gangte", "Kamei", "Rongmei",
    "Lhungdim", "Sitlhou", "Touthang", "Baite", "Rungsung", "Vashum",
    "Paite", "Simte", "Zou", "Kuki", "Vaiphei", "Luikham", "Kasomwoshi"
]

GIVEN_NAMES_HILL_MALE = [
    "Thangboi", "Lalminlun", "Mangcha", "Wungnaoshang", "Kaphung", "Chonrei",
    "Gaikhangpou", "Poujen", "Gingunmang", "Thangtinlen", "Ngamminlun", "Khamminthang"
]

GIVEN_NAMES_HILL_FEMALE = [
    "Nengneikim", "Nemkhohoi", "Chinghoih", "Lhingneilhing", "Wonreiphy",
    "Vareichon", "Ringyuwon", "Achamliu", "Gaichuiliu", "Dimngaihoih"
]

def generate_voters_for_ac(ac_no: int, ac_name: str, district: str, count: int = 100) -> list:
    is_hill = any(k in district for k in ["Ukhrul", "Senapati", "Tamenglong", "Churachandpur", "Chandel", "Kangpokpi", "Noney", "Pherzawl", "Kamjong", "Tengnoupal"])
    
    voters = []
    polling_stations = [
        f"1/{ac_no} - {ac_name} Primary School (North Wing)",
        f"2/{ac_no} - {ac_name} Community Hall",
        f"3/{ac_no} - Government High School",
        f"4/{ac_no} - Panchayat Bhavan"
    ]
    sections = [
        f"Section 1 - {ac_name} Ward No. 1",
        f"Section 2 - {ac_name} Ward No. 2",
        f"Section 3 - {ac_name} Bazar Area"
    ]

    for i in range(1, count + 1):
        epic_prefix = random.choice(["MNC", "MNP", "MAN", "MZK", "KLM"])
        epic_number = f"{epic_prefix}{ac_no:02d}{random.randint(10000, 99999)}"

        gender = "M" if random.random() < 0.49 else ("F" if random.random() < 0.99 else "O")
        age = random.randint(18, 88)

        if is_hill:
            surname = random.choice(SURNAMES_HILL)
            if gender == "M":
                given = random.choice(GIVEN_NAMES_HILL_MALE)
                full_name = f"{given} {surname}"
                rel_name = f"{random.choice(GIVEN_NAMES_HILL_MALE)} {surname}"
            else:
                given = random.choice(GIVEN_NAMES_HILL_FEMALE)
                full_name = f"{given} {surname}"
                rel_name = f"{random.choice(GIVEN_NAMES_HILL_MALE)} {surname}"
        else:
            surname = random.choice(SURNAMES_VALLEY)
            if gender == "M":
                given = random.choice(GIVEN_NAMES_MALE_VALLEY)
                full_name = f"{surname} {given}"
                rel_name = f"{surname} {random.choice(GIVEN_NAMES_MALE_VALLEY)}"
            else:
                given = random.choice(GIVEN_NAMES_FEMALE_VALLEY)
                full_name = f"{surname} {given}"
                rel_name = f"{surname} {random.choice(GIVEN_NAMES_MALE_VALLEY)}"

        rel_type = "Husband" if (gender == "F" and age > 26 and random.random() < 0.7) else "Father"
        house_no = f"{random.randint(1, 140)}"
        part_no = random.randint(1, 4)
        ps_name = polling_stations[part_no - 1]
        sec_name = random.choice(sections)

        voters.append((
            epic_number,
            full_name,
            rel_name,
            rel_type,
            house_no,
            age,
            gender,
            ac_no,
            ac_name,
            part_no,
            ps_name,
            sec_name
        ))
    return voters

def seed_database():
    print("Initializing Database schema...")
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    # Insert Assembly Constituencies
    print("Inserting 60 Manipur Assembly Constituencies...")
    for ac in MANIPUR_ACS:
        cursor.execute("""
        INSERT OR IGNORE INTO constituencies (ac_no, ac_name, district)
        VALUES (?, ?, ?)
        """, (ac[0], ac[1], ac[2]))
    conn.commit()

    # Check if voters already populated
    cursor.execute("SELECT COUNT(*) FROM voters")
    existing_count = cursor.fetchone()[0]

    if existing_count < 1000:
        print("Populating initial realistic voter dataset across Manipur ACs...")
        total_voters_inserted = 0
        for ac in MANIPUR_ACS:
            ac_no, ac_name, district = ac
            # Generate 70-120 voters per constituency for rich demonstration
            voter_records = generate_voters_for_ac(ac_no, ac_name, district, count=random.randint(70, 110))
            cursor.executemany("""
            INSERT OR IGNORE INTO voters (
                epic_no, name, relative_name, relation_type, house_no, age, gender,
                ac_no, ac_name, part_no, polling_station_name, section_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, voter_records)
            total_voters_inserted += len(voter_records)
        conn.commit()
        print(f"Inserted {total_voters_inserted} voter records.")

    conn.close()
    print("Updating constituency statistics...")
    update_constituency_stats()
    print("Database seeding completed successfully!")

if __name__ == "__main__":
    seed_database()
