#!/usr/bin/env python3
# ~/lumera-system/scripts/parse_json_leads.py
# Parses Perplexity JSON response (same format as live dashboard)
# and saves to CSV with scoring

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
    print("❌ Failed to parse Perplexity response")
    sys.exit(1)

# Strip markdown fences if present
text = text.replace("```json", "").replace("```", "").strip()

# Extract JSON array from response
start = text.find('[')
end   = text.rfind(']') + 1

if start == -1 or end == 0:
    print("❌ No JSON array found in response")
    sys.exit(1)

try:
    leads_raw = json.loads(text[start:end])
except json.JSONDecodeError as e:
    print(f"❌ JSON parse error: {e}")
    sys.exit(1)

# Score each lead based on problem signals
def score_lead(problem: str, website: str) -> int:
    score = 0
    p = problem.lower()
    w = (website or "").lower()

    if any(x in p for x in ["no website", "no web"]):
        score += 2
    if any(x in p for x in ["low review", "bad review", "negative review", "poor review"]):
        score += 1
    if any(x in p for x in ["weak online", "no online", "limited online"]):
        score += 1
    if any(x in p for x in ["no booking", "missing booking", "no appointment"]):
        score += 1
    if any(x in p for x in ["outdated", "old website"]):
        score += 1
    if not w or w in ["none", "none listed", "n/a", ""]:
        score += 1

    return score

# Clean and validate email
def valid_email(email: str) -> bool:
    if not email:
        return False
    email = email.strip()
    if "example.com" in email or "none" in email.lower():
        return False
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email))

leads = []
seen_emails = set()

for l in leads_raw:
    email = (l.get("email") or "").strip()

    # Skip if no valid email or duplicate
    if not valid_email(email):
        continue
    if email in seen_emails:
        continue
    seen_emails.add(email)

    business = l.get("business") or l.get("name") or ""
    website  = l.get("website") or "None listed"
    problem  = l.get("problem") or "Weak online presence"
    phone    = l.get("phone") or ""
    owner    = l.get("name") or ""
    score    = score_lead(problem, website)

    leads.append([business, city, website, problem, email, phone, owner, score])

# Save to CSV
with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Name", "City", "Website", "Problem", "Email", "Phone", "Owner", "Score"])
    writer.writerows(leads)

print(f"✅ {len(leads)} leads with real emails saved to {output_csv}")
