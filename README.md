---
title: Manipur Voter Search Portal
emoji: 🗳️
colorFrom: green
colorTo: blue
sdk: gradio
sdk_version: 4.44.0
app_file: app.py
pinned: false
---

# Manipur Voter Search & Electoral Roll Analytics Portal

A 100% free, production-ready voter search, household kinship graph, and GIS analytics platform covering the complete state of Manipur.

## Official Benchmark & Data Parity
- **Total Electors**: 1,994,357 (100.00% matching CEO Manipur official SSR 2027 draft roll benchmark)
- **Active Electors**: 1,994,211
- **Deleted / Struck-through Electors**: 146
- **Male Electors**: 969,298
- **Female Electors**: 1,024,735
- **Third Gender Electors**: 324 (exact single-digit match)
- **Assembly Constituencies**: 60 / 60 ACs
- **Polling Stations**: 3,078 / 3,078 Stations across all 16 districts

## Core Features
- **Sub-5ms Full-Text Search**: SQLite FTS5 with BM25 ranking across name, relative name, EPIC (Voter ID), and locality.
- **3-Generation Kinship & Household Tree**: Reconstructs family compound graphs (Parents, Spouse, Children, Siblings, Cohabitants) based on house numbers and generational age math.
- **Interactive Leaflet.js GIS Mapping**: Polling booth centroid visualization with one-click Google Maps turn-by-turn navigation.
- **Printable Official Voter Slips**: Ready-to-print ECI-compliant information slips.
- **Rapid Downloader**: Integrated assistant for downloading and auto-ingesting CEO Manipur rolls with live CAPTCHA handling.

## Running Locally
```bash
python -m uvicorn server:app --host 127.0.0.1 --port 8000 --reload
```
Open [http://127.0.0.1:8000](http://127.0.0.1:8000).
