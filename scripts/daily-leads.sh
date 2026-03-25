#!/bin/bash
# Lumera Lead Engine — Weekly Monday Scraper
# Part 1: Local service businesses via Perplexity
# Part 2: Agency leads via Apollo import (manual) / Clutch
# Part 3: Send summary email to Kory

source ~/lumera-system/config.env

mkdir -p ~/lumera-system/daily_leads
mkdir -p ~/lumera-system/logs

# Clear old leads
rm -f ~/lumera-system/daily_leads/*.csv 2>/dev/null

echo "================================================"
echo "Lumera Weekly Lead Scrape — $(date)"
echo "================================================"
TOTAL=0
LOCAL_TOTAL=0
declare -A NICHE_COUNTS

# ── PART 1: Local service businesses via Perplexity ──────────────────────────
echo ""
echo "PART 1: Local service businesses (Perplexity)"
echo "------------------------------------------------"

LOCAL_NICHES=(
  "medspa Nashville TN"
  "roofing Nashville TN"
  "dentists Nashville TN"
  "chiropractors Nashville TN"
  "HVAC company Nashville TN"
  "landscaping company Nashville TN"
  "cleaning service Nashville TN"
  "plumber Nashville TN"
)

LEADS_PER_LOCAL=7

run_perplexity() {
    local QUERY="$1"
    local CITY=$(echo "$QUERY" | awk '{$1=""; print $0}' | xargs)
    local FILENAME=$(echo "$QUERY" | tr ' ' '_')_$(date +%Y-%m-%d).csv
    local FILEPATH=~/lumera-system/daily_leads/$FILENAME

    echo "  Processing: $QUERY..."

    local PROMPT="Search for $LEADS_PER_LOCAL $QUERY businesses that have a contact email publicly listed. For each find their actual email from their website Contact page, About page, or Google listing. Also find their phone number and the owner's first name if available. Only include businesses with a confirmed real email. Return ONLY a valid JSON array, no markdown: [{\"business\":\"Name\",\"website\":\"https://...\",\"email\":\"info@...\",\"name\":\"FirstName\",\"phone\":\"\",\"problem\":\"their visible weakness e.g. low reviews, no website, no online booking\"}]. Do not include businesses without a confirmed email."

    local RAW_JSON=$(curl -s "https://api.perplexity.ai/chat/completions" \
      -H "Authorization: Bearer $PERPLEXITY_KEY" \
      -H "Content-Type: application/json" \
      -d "{
        \"model\": \"sonar\",
        \"messages\": [
          {\"role\": \"user\", \"content\": $(echo "$PROMPT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}
        ]
      }")

    python3 ~/lumera-system/scripts/parse_json_leads.py "$RAW_JSON" "$FILEPATH" "$CITY"

    if [ -f "$FILEPATH" ]; then
        local COUNT=$(tail -n +2 "$FILEPATH" | wc -l)
        TOTAL=$((TOTAL + COUNT))
        LOCAL_TOTAL=$((LOCAL_TOTAL + COUNT))
        NICHE_COUNTS["$QUERY"]=$COUNT
        echo "    $COUNT leads saved"
    fi
    sleep 2
}

for NICHE in "${LOCAL_NICHES[@]}"; do
    run_perplexity "$NICHE"
done

echo ""
echo "Part 1 complete — $LOCAL_TOTAL local leads"

# ── PART 2: Agency leads via Google Search ───────────────────────────────────
echo ""
echo "PART 2: Agency leads (Google Search)"
echo "------------------------------------------------"

pip install beautifulsoup4 curl-cffi --quiet --break-system-packages 2>/dev/null
python3 ~/lumera-system/scripts/google_scraper.py

GOOGLE_COUNT=0
GOOGLE_FILE=$(ls ~/lumera-system/daily_leads/google_agencies_*.csv 2>/dev/null | head -1)
if [ -f "$GOOGLE_FILE" ]; then
    GOOGLE_COUNT=$(tail -n +2 "$GOOGLE_FILE" | wc -l)
    TOTAL=$((TOTAL + GOOGLE_COUNT))
    echo "Google agency leads: $GOOGLE_COUNT"
fi

# ── PART 3: Agency leads via Clutch ──────────────────────────────────────────
echo ""
echo "PART 3: Agency leads (Clutch.co)"
echo "------------------------------------------------"

python3 ~/lumera-system/scripts/clutch_scraper.py

CLUTCH_COUNT=0
CLUTCH_FILE=$(ls ~/lumera-system/daily_leads/agencies_clutch_*.csv 2>/dev/null | head -1)
if [ -f "$CLUTCH_FILE" ]; then
    CLUTCH_COUNT=$(tail -n +2 "$CLUTCH_FILE" | wc -l)
    TOTAL=$((TOTAL + CLUTCH_COUNT))
    echo "Clutch leads: $CLUTCH_COUNT"
fi

# ── PART 3: Count hot leads ───────────────────────────────────────────────────
HOT_COUNT=$(python3 -c "
import csv, glob, os
hot = 0
for f in glob.glob(os.path.expanduser('~/lumera-system/daily_leads/*.csv')):
    if '_hot' in f: continue
    try:
        with open(f) as fh:
            for row in csv.DictReader(fh):
                try:
                    if int(row.get('Score',0) or 0) >= 2: hot += 1
                except: pass
    except: pass
print(hot)
" 2>/dev/null || echo "0")

# ── PART 4: Send summary email via Resend ────────────────────────────────────
echo ""
echo "Sending weekly summary email..."

# Build niche breakdown for email
NICHE_ROWS=""
for NICHE in "${LOCAL_NICHES[@]}"; do
    COUNT=${NICHE_COUNTS["$NICHE"]:-0}
    NICHE_ROWS="${NICHE_ROWS}<tr><td style='padding:6px 12px;border-bottom:1px solid #1a2540;color:#94a3b8;font-size:13px'>${NICHE}</td><td style='padding:6px 12px;border-bottom:1px solid #1a2540;color:#38bdf8;font-weight:700;font-size:13px;text-align:right'>${COUNT}</td></tr>"
done

DATE_STR=$(date "+%A, %B %d %Y")

EMAIL_HTML="<div style='font-family:sans-serif;max-width:600px;margin:0 auto;background:#080808;color:#e8edf8;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.07)'>
<div style='font-size:22px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:6px'>LUMERA</div>
<div style='font-size:11px;color:rgba(255,255,255,0.4);margin-bottom:28px;letter-spacing:.1em'>WEEKLY LEAD REPORT</div>
<h2 style='font-size:18px;font-weight:700;margin-bottom:4px'>Weekly Leads Ready</h2>
<p style='color:rgba(255,255,255,0.5);font-size:13px;margin-bottom:28px'>${DATE_STR}</p>

<div style='display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:28px'>
  <div style='background:#111;border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:16px;text-align:center'>
    <div style='font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px'>Total Leads</div>
    <div style='font-size:28px;font-weight:800;color:#38bdf8'>${TOTAL}</div>
  </div>
  <div style='background:#111;border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:16px;text-align:center'>
    <div style='font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px'>Hot Leads</div>
    <div style='font-size:28px;font-weight:800;color:#f43f5e'>${HOT_COUNT}</div>
  </div>
  <div style='background:#111;border:1px solid rgba(255,255,255,0.07);border-radius:10px;padding:16px;text-align:center'>
    <div style='font-size:11px;color:rgba(255,255,255,0.4);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px'>Agency Leads</div>
    <div style='font-size:28px;font-weight:800;color:#6366f1'>${CLUTCH_COUNT}</div>
  </div>
</div>

<div style='background:#111;border:1px solid rgba(255,255,255,0.07);border-radius:10px;margin-bottom:28px;overflow:hidden'>
  <div style='padding:14px 12px;border-bottom:1px solid rgba(255,255,255,0.07);font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:rgba(255,255,255,0.4)'>Local Niche Breakdown</div>
  <table style='width:100%;border-collapse:collapse'>
    ${NICHE_ROWS}
    <tr><td style='padding:6px 12px;color:#94a3b8;font-size:13px'>Clutch Agencies</td><td style='padding:6px 12px;color:#38bdf8;font-weight:700;font-size:13px;text-align:right'>${CLUTCH_COUNT}</td></tr>
  </table>
</div>

<a href='http://127.0.0.1:8000/leads' style='display:inline-block;padding:12px 24px;background:linear-gradient(135deg,#3b82f6,#6366f1);color:white;border-radius:10px;font-weight:700;font-size:13px;text-decoration:none'>Open Dashboard →</a>

<p style='margin-top:28px;font-size:11px;color:rgba(255,255,255,0.25)'>Lumera Lead Engine · Auto-sent every Monday</p>
</div>"

# Send via Resend API
python3 -c "
import os, sys
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path.home() / 'lumera-system' / 'config.env')

api_key = os.getenv('RESEND_API_KEY')
from_email = os.getenv('FROM_EMAIL','kory@lumeraautomation.com')
to_email = from_email  # Send to yourself

if not api_key:
    print('RESEND_API_KEY not set — skipping email')
    sys.exit(0)

import resend
resend.api_key = api_key

html = open('/tmp/lumera_summary.html').read()

try:
    resend.Emails.send({
        'from': f'Lumera Lead Engine <{from_email}>',
        'to': to_email,
        'subject': f'Weekly Leads Ready — $TOTAL leads scraped',
        'html': html
    })
    print(f'Summary email sent to {to_email}')
except Exception as e:
    print(f'Email failed: {e}')
"

# Write HTML to temp file for Python to read
echo "$EMAIL_HTML" > /tmp/lumera_summary.html

# Now actually run the python
python3 -c "
import os, sys
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path.home() / 'lumera-system' / 'config.env')

api_key = os.getenv('RESEND_API_KEY')
from_email = os.getenv('FROM_EMAIL','kory@lumeraautomation.com')

if not api_key:
    print('RESEND_API_KEY not set — skipping email')
    sys.exit(0)

import resend
resend.api_key = api_key

with open('/tmp/lumera_summary.html') as f:
    html = f.read()

try:
    resend.Emails.send({
        'from': f'Lumera Lead Engine <{from_email}>',
        'to': from_email,
        'subject': 'Weekly Leads Ready — $TOTAL leads scraped',
        'html': html
    })
    print(f'Summary email sent to {from_email}')
except Exception as e:
    print(f'Email failed: {e}')
"

# ── SUMMARY ───────────────────────────────────────────────────────────────────
echo ""
echo "================================================"
echo "Weekly scrape complete!"
echo "Local leads:   $LOCAL_TOTAL"
echo "Agency leads:  $CLUTCH_COUNT"
echo "Total leads:   $TOTAL"
echo "Hot leads:     $HOT_COUNT"
echo "================================================"
