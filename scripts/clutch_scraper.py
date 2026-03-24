#!/usr/bin/env python3
# ~/lumera-system/scripts/clutch_scraper.py
# Scrapes agency listings from Clutch.co city pages then finds emails from websites
# Uses curl-cffi to bypass Cloudflare

import csv
import re
import time
import random
import json
from datetime import datetime
from pathlib import Path
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests
    IMPERSONATE = "chrome120"
    print("curl-cffi ready")
except ImportError:
    import requests
    IMPERSONATE = None
    print("WARNING: Install curl-cffi: pip install curl-cffi --break-system-packages")

OUTPUT_DIR = Path(__file__).parent.parent / "daily_leads"
OUTPUT_DIR.mkdir(exist_ok=True)

# City-specific Clutch URLs — smaller cities = smaller agencies = easier to find emails
# Pattern: clutch.co/agencies/{service}/{city}
CLUTCH_SEARCHES = [
    ("https://clutch.co/agencies/digital-marketing/nashville",     "Digital Marketing Agency", "Nashville TN"),
    ("https://clutch.co/agencies/digital-marketing/charlotte",     "Digital Marketing Agency", "Charlotte NC"),
    ("https://clutch.co/agencies/digital-marketing/atlanta",       "Digital Marketing Agency", "Atlanta GA"),
    ("https://clutch.co/agencies/seo/nashville",                   "SEO Agency",               "Nashville TN"),
    ("https://clutch.co/agencies/seo/austin",                      "SEO Agency",               "Austin TX"),
    ("https://clutch.co/agencies/social-media-marketing/nashville","Social Media Agency",       "Nashville TN"),
    ("https://clutch.co/agencies/social-media-marketing/denver",   "Social Media Agency",       "Denver CO"),
    ("https://clutch.co/agencies/web-design/nashville",            "Web Design Agency",         "Nashville TN"),
    ("https://clutch.co/agencies/web-design/charlotte",            "Web Design Agency",         "Charlotte NC"),
    ("https://clutch.co/agencies/content-marketing/austin",        "Content Marketing Agency",  "Austin TX"),
    ("https://clutch.co/agencies/video-production/nashville",      "Video Production Agency",   "Nashville TN"),
    ("https://clutch.co/agencies/digital-marketing/tampa",         "Digital Marketing Agency",  "Tampa FL"),
    ("https://clutch.co/agencies/seo/miami",                       "SEO Agency",                "Miami FL"),
    ("https://clutch.co/agencies/digital-marketing/phoenix",       "Digital Marketing Agency",  "Phoenix AZ"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

def get_page(url, timeout=15):
    for attempt in range(3):
        try:
            if IMPERSONATE:
                resp = requests.get(url, headers=HEADERS, timeout=timeout, impersonate=IMPERSONATE)
            else:
                resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code == 200:
                return resp.text
            elif resp.status_code in (403, 429):
                wait = 20 + attempt * 10
                print(f"  Blocked ({resp.status_code}) — waiting {wait}s...")
                time.sleep(wait)
            elif resp.status_code == 404:
                print(f"  404 — URL may have changed")
                return None
            else:
                print(f"  Status {resp.status_code}")
                time.sleep(5)
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(5)
    return None

def is_valid_email(email):
    if not email or "@" not in email:
        return False
    bad = ["example.com","sentry.io","wixpress.com","squarespace.com","wordpress.org",
           "shopify.com","amazonaws.com","cloudflare.com","google.com","facebook.com",
           "twitter.com","instagram.com","linkedin.com","hubspot.com","mailchimp.com",
           "w3.org","schema.org","apple.com","microsoft.com","yahoo.com","gmail.com",
           "hotmail.com","outlook.com","icloud.com"]
    domain = email.split("@")[1].lower()
    if any(b in domain for b in bad): return False
    if len(email) > 80: return False
    return bool(re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email))

def find_email_from_website(website_url):
    if not website_url or website_url in ("N/A","None listed",""):
        return None
    if not website_url.startswith("http"):
        website_url = "https://" + website_url

    for path in ["/contact", "/contact-us", "/about", "/about-us", ""]:
        url = website_url.rstrip("/") + path
        try:
            html = get_page(url, timeout=8)
            if not html: continue
            emails = set()
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                if a["href"].startswith("mailto:"):
                    e = a["href"][7:].split("?")[0].strip().lower()
                    if is_valid_email(e): emails.add(e)
            for m in re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html):
                e = m.lower().strip(".,;'\"()")
                if is_valid_email(e): emails.add(e)
            if emails:
                preferred = [e for e in emails if any(
                    e.startswith(p) for p in ["info@","contact@","hello@","hi@","team@","hey@","reach@","get@"]
                )]
                return preferred[0] if preferred else sorted(emails)[0]
            time.sleep(0.5)
        except: continue
    return None

def parse_clutch_page(html):
    """Extract company names + websites from a Clutch listing page."""
    companies = []
    soup = BeautifulSoup(html, "html.parser")

    # Method 1: JSON-LD structured data
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle ItemList
            items = []
            if isinstance(data, list): items = data
            elif data.get("@type") == "ItemList": items = data.get("itemListElement", [])
            elif isinstance(data.get("itemListElement"), list): items = data["itemListElement"]

            for item in items:
                if isinstance(item, dict):
                    # item could be the company or wrapped in "item" key
                    company = item.get("item", item)
                    name = company.get("name","")
                    url  = company.get("url","")
                    if name and len(name) > 2 and "clutch" not in name.lower():
                        companies.append({"name": name, "website": url, "location": ""})
        except: pass

    if companies:
        print(f"    Parsed {len(companies)} via JSON-LD")
        return companies

    # Method 2: Find company profile links on Clutch
    # Clutch profile links look like: /profile/company-name
    profile_links = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if href.startswith("/profile/") and len(text) > 2 and len(text) < 80:
            if href not in profile_links:
                profile_links[href] = text

    # For each profile, look for website in same container
    for href, name in list(profile_links.items())[:15]:
        # Try to find outbound link near this profile
        website = ""
        # Find the <a> element
        link_el = soup.find("a", href=href)
        if link_el:
            # Walk up to parent container and look for external links
            parent = link_el.parent
            for _ in range(5):
                if parent is None: break
                for ext_a in parent.find_all("a", href=True):
                    ext_href = ext_a.get("href","")
                    if ext_href.startswith("http") and "clutch.co" not in ext_href:
                        website = ext_href
                        break
                if website: break
                parent = parent.parent

        companies.append({"name": name, "website": website, "location": ""})

    if companies:
        print(f"    Parsed {len(companies)} via profile links")
        return companies

    # Method 3: Any outbound links with reasonable company-name text
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if (href.startswith("http") and "clutch.co" not in href
                and 4 < len(text) < 60 and text not in seen
                and not any(s in text.lower() for s in
                           ["clutch","sign in","log in","menu","privacy","terms",
                            "cookie","home","about","blog","contact","resources"])):
            seen.add(text)
            companies.append({"name": text, "website": href, "location": ""})
        if len(companies) >= 15: break

    print(f"    Parsed {len(companies)} via outbound links")
    return companies

def run_clutch_scrape():
    today = datetime.now().strftime("%Y-%m-%d")
    output_file = OUTPUT_DIR / f"agencies_clutch_{today}.csv"
    all_leads   = []
    seen_emails = set()
    seen_names  = set()

    print(f"\nClutch scrape — {len(CLUTCH_SEARCHES)} searches")
    print("=" * 50)

    for clutch_url, niche_label, city in CLUTCH_SEARCHES:
        print(f"\n[{niche_label} · {city}]")
        html = get_page(clutch_url)
        if not html:
            print("  Skipping — could not fetch")
            continue

        companies = parse_clutch_page(html)
        print(f"  Processing {len(companies)} companies...")

        for company in companies[:10]:
            name = company.get("name","").strip()
            if not name or name.lower() in seen_names:
                continue
            seen_names.add(name.lower())

            website = company.get("website","")
            print(f"  → {name} ({website[:40] if website else 'no website'})")

            email = find_email_from_website(website) if website else None
            time.sleep(random.uniform(1, 2.5))

            if not email or email in seen_emails:
                print(f"    no email")
                continue
            seen_emails.add(email)

            all_leads.append({
                "Name":    name,
                "City":    city,
                "Website": website or "None listed",
                "Problem": "agency seeking more clients; lead generation opportunity",
                "Email":   email,
                "Phone":   "",
                "Owner":   "",
                "Score":   1,
            })
            print(f"    ✅ {email}")

        time.sleep(random.uniform(4, 8))

    if all_leads:
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["Name","City","Website","Problem","Email","Phone","Owner","Score"])
            writer.writeheader()
            writer.writerows(all_leads)
        print(f"\n{'='*50}")
        print(f"✅ Done — {len(all_leads)} agency leads saved to {output_file}")
    else:
        print(f"\n No leads found — Clutch may be fully blocking scraping")

    return len(all_leads)

if __name__ == "__main__":
    run_clutch_scrape()
