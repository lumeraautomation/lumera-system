#!/usr/bin/env python3
# ~/lumera-system/scripts/parse_json_leads.py
# Parses Perplexity JSON response and saves to CSV
# Now includes LeadRadar-style signals: rating, reviews, has_booking, hours

import sys
import csv
import json
import re

raw_json   = sys.argv[1]
output_csv = sys.argv[2]
city       = sys.argv[3] if len(sys.argv) > 3 else ""

try:
    data = json.loads(raw_json)
    text = data['choices'][0]['message']['content']
except (KeyError, IndexError, json.JSONDecodeError):
    print("Failed to parse Perplexity response")
    sys.exit(1)

text = text.replace("```json", "").replace("```", "").strip()
start = text.find('[')
end   = text.rfind(']') + 1

if start == -1 or end == 0:
    print("No JSON array found")
    sys.exit(1)

try:
    leads_raw = json.loads(text[start:end])
except json.JSONDecodeError as e:
    # Sometimes Perplexity returns multiple arrays — try merging them
    try:
        import re as _re
        arrays = _re.findall(r'\[.*?\]', text, _re.DOTALL)
        merged = []
        for arr in arrays:
            try:
                merged.extend(json.loads(arr))
            except: pass
        if merged:
            leads_raw = merged
            print(f"Merged {len(arrays)} arrays into {len(merged)} leads")
        else:
            print(f"JSON parse error: {e}")
            sys.exit(1)
    except Exception as e2:
        print(f"JSON parse error: {e}")
        sys.exit(1)

def valid_email(email):
    if not email: return False
    email = email.strip()
    if any(b in email.lower() for b in ["example.com","none","placeholder","test@"]): return False
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))

def leadradar_score(problem, website, phone, has_booking, reviews, rating, hours):
    score = 0
    p = problem.lower()
    w = (website or "").lower()

    if any(x in p for x in ["no website","phone-dependent","facebook only","no web"]): score += 2
    if not w or w in ["none","none listed","n/a","","nan"]: score += 1
    if phone and phone.strip() not in ["","—","None","nan"]: score += 1

    booking = str(has_booking).lower()
    if any(x in booking for x in ["no","false","none","n/a"]): score += 1

    try:
        rev_count = int(re.sub(r"[^\d]", "", str(reviews)))
        if rev_count >= 50: score += 1
        if rev_count >= 100: score += 1
    except: pass

    try:
        rat = float(re.sub(r"[^\d.]", "", str(rating)))
        if rat >= 4.0: score += 1
    except: pass

    h = str(hours).lower()
    if any(x in h for x in ["closes at 5","closes at 4","closed weekend","limited hours","after hours"]): score += 1
    if any(x in p for x in ["low review","few review","no review"]): score += 1
    if any(x in p for x in ["high call","busy","high demand"]): score += 1

    return min(score, 5)

leads = []
seen_emails = set()

for l in leads_raw:
    if not isinstance(l, dict): continue  # skip ints/nulls from merged arrays
    email = (l.get("email") or "").strip()
    if not valid_email(email): continue
    if email in seen_emails: continue
    seen_emails.add(email)

    business    = l.get("business") or l.get("name") or ""
    website     = l.get("website") or "None listed"
    problem     = l.get("problem") or "Weak online presence"
    phone       = l.get("phone") or ""
    owner       = l.get("name") or ""
    rating      = l.get("rating") or ""
    reviews     = l.get("reviews") or ""
    has_booking = l.get("has_booking") or "unknown"
    hours       = l.get("hours") or ""

    score = leadradar_score(problem, website, phone, has_booking, reviews, rating, hours)
    leads.append([business, city, website, problem, email, phone, owner,
                  score, rating, reviews, has_booking, hours])

with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Name","City","Website","Problem","Email","Phone","Owner",
                     "Score","Rating","Reviews","HasBooking","Hours"])
    writer.writerows(leads)

print(f"{len(leads)} leads saved to {output_csv}")
