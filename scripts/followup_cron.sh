#!/bin/bash
# ~/lumera-system/scripts/followup_cron.sh
# Run this daily via cron to auto-send follow-up emails
# Add to crontab: 0 9 * * * bash ~/lumera-system/scripts/followup_cron.sh >> ~/lumera-system/logs/followup.log 2>&1

echo "=== Lumera Follow-up Cron: $(date) ==="

# Hit the dashboard follow-up endpoint
RESULT=$(curl -s -X POST http://127.0.0.1:8000/cron/followups)

echo "Result: $RESULT"
echo "=== Done ==="
