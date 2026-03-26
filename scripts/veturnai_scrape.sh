#!/bin/bash
# ~/lumera-system/scripts/veturnai_scrape.sh
# Scrapes NJ restaurant leads for veturnai trial client
# Run: bash ~/lumera-system/scripts/veturnai_scrape.sh

source ~/lumera-system/config.env

OUTPUT_DIR=~/lumera-system/daily_leads
mkdir -p $OUTPUT_DIR

TODAY=$(date +%Y-%m-%d)

# NJ restaurant targets — cities with dense restaurant scenes
NJ_QUERIES=(
  "restaurant Hoboken NJ"
  "restaurant Jersey City NJ"
  "restaurant Newark NJ"
  "restaurant Montclair NJ"
  "restaurant Princeton NJ"
  "restaurant Red Bank NJ"
  "restaurant Morristown NJ"
  "restaurant Asbury Park NJ"
)

LEADS_PER_QUERY=10

echo "================================================"
echo "VeturnAI — NJ Restaurant Lead Scrape — $TODAY"
echo "================================================"
TOTAL=0

run_query() {
    local QUERY="$1"
    local CITY=$(echo "$QUERY" | sed 's/restaurant //')
    local FILENAME="restaurant_$(echo "$QUERY" | tr ' ' '_')_${TODAY}.csv"
    local FILEPATH="$OUTPUT_DIR/$FILENAME"

    echo ""
    echo "Scraping: $QUERY..."

    local PROMPT="Search for $LEADS_PER_QUERY independent local restaurants in $CITY that would benefit from an AI phone receptionist. Focus on restaurants that: have high call volume, no online booking system, get busy during dinner rush, go to voicemail after hours. For each restaurant find: 1) Real contact email from their website or Google listing 2) Phone number (required) 3) Owner or manager first name if available 4) Google Maps rating 5) Approximate review count 6) Whether they have online booking like OpenTable or Resy - yes or no 7) Business hours especially if they close before 10pm or are closed certain days 8) Type of restaurant (Italian, American, etc). Only include restaurants with a confirmed real email AND phone number. Return ONLY a valid JSON array: [{\"business\":\"Name\",\"website\":\"https://...\",\"email\":\"info@...\",\"name\":\"OwnerFirstName\",\"phone\":\"(201) 555-0100\",\"rating\":\"4.3\",\"reviews\":\"89\",\"has_booking\":\"no\",\"hours\":\"closes at 10pm, closed Mondays\",\"cuisine\":\"Italian\",\"problem\":\"89 reviews means high call volume, no online booking, goes to voicemail after 10pm and during dinner rush — perfect for AI receptionist\"}]. Do not include chains, franchises, or restaurants without a confirmed email."

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
        echo "  $COUNT restaurant leads saved → $FILENAME"
    fi
    sleep 3
}

for QUERY in "${NJ_QUERIES[@]}"; do
    run_query "$QUERY"
done

echo ""
echo "================================================"
echo "Done — $TOTAL NJ restaurant leads total"
echo "Files saved to: $OUTPUT_DIR"
echo "================================================"
