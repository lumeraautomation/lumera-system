#!/usr/bin/env python3
# ~/lumera-system/scripts/apollo_import.py
# Converts Apollo CSV export to Lumera dashboard format
# Run: python3 ~/lumera-system/scripts/apollo_import.py

import csv
import glob
import os
import re
from datetime import datetime
from pathlib import Path

DOWNLOADS    = Path("/mnt/c/Users/kory/Downloads")
OUTPUT_DIR   = Path.home() / "lumera-system" / "daily_leads"
OUTPUT_DIR.mkdir(exist_ok=True)

def score_lead(row):
    score = 0
    # Small company = easier to reach decision maker
    employees = row.get("# Employees","")
    try:
        emp = int(str(employees).replace(",","").strip())
        if emp < 10:  score += 2
        elif emp < 50: score += 1
    except: pass
    # Verified email = higher quality
    if row.get("Email Status","").lower() in ("verified","valid"): score += 1
    return score

def build_problem(row):
    parts = []
    industry = row.get("Industry","")
    keywords = row.get("Keywords","")
    employees = row.get("# Employees","")
    if industry: parts.append(f"{industry} agency")
    try:
        emp = int(str(employees).replace(",","").strip())
        if emp < 10: parts.append("small team — likely needs lead gen help")
        elif emp < 50: parts.append("growing agency — lead gen opportunity")
    except: pass
    if keywords:
        kw = keywords[:60]
        parts.append(f"focus: {kw}")
    return "; ".join(parts) if parts else "agency seeking more clients"

def convert_apollo(filepath):
    today    = datetime.now().strftime("%Y-%m-%d")
    out_file = OUTPUT_DIR / f"apollo_agencies_{today}.csv"
    leads    = []
    skipped  = 0

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = row.get("Email","").strip()
            if not email or "@" not in email:
                skipped += 1
                continue

            # Skip bad email statuses
            status = row.get("Email Status","").lower()
            if status in ("invalid","bounced","do not email"):
                skipped += 1
                continue

            first   = row.get("First Name","").strip()
            last    = row.get("Last Name","").strip()
            name    = f"{first} {last}".strip() or row.get("Company Name","—")
            company = row.get("Company Name","").strip()
            title   = row.get("Title","").strip()
            city    = row.get("City","") or row.get("Company City","")
            state   = row.get("State","") or row.get("Company State","")
            location= f"{city}, {state}".strip(", ") if city or state else "United States"
            website = row.get("Website","").strip()
            phone   = (row.get("Work Direct Phone","") or
                      row.get("Corporate Phone","") or
                      row.get("Mobile Phone","")).strip()
            linkedin= row.get("Person Linkedin Url","").strip()

            # Use company name as the display name if available
            display_name = company if company else name

            leads.append({
                "Name":    display_name,
                "City":    location,
                "Website": website or "None listed",
                "Problem": build_problem(row),
                "Email":   email,
                "Phone":   phone,
                "Owner":   f"{first}".strip() or "",
                "Score":   score_lead(row),
            })

    if leads:
        with open(out_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Name","City","Website","Problem","Email","Phone","Owner","Score"])
            writer.writeheader()
            writer.writerows(leads)
        print(f"✅ {len(leads)} leads imported → {out_file}")
        print(f"   {skipped} skipped (no/invalid email)")
    else:
        print("❌ No valid leads found in Apollo export")

    return len(leads)

if __name__ == "__main__":
    # Auto-find the latest Apollo CSV in Downloads
    patterns = [
        str(DOWNLOADS / "apollo*.csv"),
        str(DOWNLOADS / "Apollo*.csv"),
        str(DOWNLOADS / "*apollo*.csv"),
        str(DOWNLOADS / "export*.csv"),
    ]

    found = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))

    if not found:
        print("No Apollo CSV found in Downloads.")
        print("Please specify the file path:")
        filepath = input("Path: ").strip().strip("'\"")
    else:
        # Use most recently modified
        found.sort(key=os.path.getmtime, reverse=True)
        filepath = found[0]
        print(f"Found: {filepath}")

    convert_apollo(filepath)
