import sys
import csv
import json
import re

# get input JSON and output CSV path from command line
raw_json = sys.argv[1]
output_csv = sys.argv[2]

data = json.loads(raw_json)

# helper function to generate emails if missing
def auto_email(website, name):
    # Use domain if website exists
    if website and website.lower() not in ["none listed", "n/a"] and website != "":
        domain = website.replace("http://","").replace("https://","").split("/")[0]
        return f"contact@{domain}"

    # fallback: clean the name, remove spaces & special chars, lowercase
    name_clean = re.sub(r'[^A-Za-z0-9]', '', name.replace(" ", "").lower())
    return f"{name_clean}@example.com"

# parse the table from Perplexity
leads = []
try:
    table_text = data['choices'][0]['message']['content']
except (KeyError, IndexError):
    table_text = ""

# extract rows from Markdown table
lines = table_text.splitlines()
for line in lines[2:]:  # skip the header
    if "|" in line:
        parts = [p.strip() for p in line.split("|")[1:-1]]  # remove leading/trailing empty
        if len(parts) == 5:
            name, city, website, problem, email = parts

            # generate email if missing
            email = auto_email(website, name) if email.lower() in ["none listed", "n/a", ""] else email

            # simple scoring
            score = 0
            if "no website" in problem.lower():
                score += 2
            if "low review" in problem.lower():
                score += 1

            leads.append([name, city, website, problem, email, score])

# save to CSV
with open(output_csv, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["Name","City","Website","Problem","Email","Score"])
    writer.writerows(leads)

print(f"✅ {len(leads)} leads processed and saved to {output_csv}")
