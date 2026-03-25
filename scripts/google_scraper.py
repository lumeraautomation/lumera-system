#!/usr/bin/env python3
# ~/lumera-system/scripts/google_scraper.py
# Finds agency + online business leads via DuckDuckGo and Bing
# Google blocks scrapers — DDG and Bing work much better
# Run: python3 ~/lumera-system/scripts/google_scraper.py

import csv
import re
import time
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote_plus, unquote

try:
    from curl_cffi import requests
    IMPERSONATE = "chrome120"
    print("curl-cffi ready")
except ImportError:
    import requests
    IMPERSONATE = None
    print("WARNING: pip install curl-cffi --break-system-packages")

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("pip install beautifulsoup4 --break-system-packages")
    exit(1)

OUTPUT_DIR = Path(__file__).parent.parent / "daily_leads"
OUTPUT_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────
# SEARCH QUERIES — targeted at online agencies
# ─────────────────────────────────────────────
QUERIES = [
    "small digital marketing agency contact email site:*.com",
    "boutique SEO agency USA contact us email",
    "AI automation consultant small business email",
    "email marketing agency small team contact us",
    "social media marketing agency boutique contact",
    "content marketing agency USA hire email",
    "B2B sales consultant contact email website",
    "marketing consultant freelance contact email",
    "CRM consultant small business contact email",
    "online business coach contact email",
    "digital agency Nashville TN contact email",
    "digital agency Austin TX contact email",
    "digital agency Charlotte NC contact email",
    "digital agency Tampa FL contact email",
    "digital agency Denver CO contact email",
    "marketing agency under 10 employees contact",
    "independent marketing consultant email",
    "video production agency small team contact",
    "web design agency small business email",
    "recruiting agency small team contact email",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }

def fetch(url, timeout=12):
    for attempt in range(3):
        try:
            if IMPERSONATE:
                r = requests.get(url, headers=get_headers(), timeout=timeout, impersonate=IMPERSONATE)
            else:
                r = requests.get(url, headers=get_headers(), timeout=timeout)
            if r.status_code == 200:
                return r.text
            elif r.status_code == 429:
                time.sleep(30 + attempt * 15)
            else:
                time.sleep(5)
        except Exception as e:
            time.sleep(3 + attempt * 2)
    return None

def ddg_search(query, num=8):
    """DuckDuckGo HTML search — most scraper-friendly."""
    results = []
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl=us-en"

    html = fetch(url, timeout=15)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    for r in soup.find_all("div", class_="result"):
        try:
            a = r.find("a", class_="result__a", href=True)
            if not a: continue
            href = a.get("href","")

            # DDG wraps URLs in a redirect — extract real URL
            if "uddg=" in href:
                href = unquote(href.split("uddg=")[-1].split("&")[0])
            elif href.startswith("//duckduckgo"):
                continue

            if not href.startswith("http"): continue
            if any(s in href for s in ["youtube.com","facebook.com","wikipedia.org",
                                        "twitter.com","instagram.com","tiktok.com",
                                        "reddit.com","quora.com","pinterest.com"]): continue

            title = a.get_text(strip=True)
            snip_el = r.find(class_="result__snippet")
            snippet = snip_el.get_text(strip=True) if snip_el else ""

            if title and href:
                results.append({"url": href, "title": title, "snippet": snippet})
        except: continue

        if len(results) >= num:
            break

    return results

def bing_search(query, num=8):
    """Bing search as secondary source."""
    results = []
    url = f"https://www.bing.com/search?q={quote_plus(query)}&count={num}&mkt=en-US"

    html = fetch(url, timeout=15)
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    for li in soup.find_all("li", class_="b_algo"):
        try:
            a = li.find("a", href=True)
            if not a: continue
            href = a.get("href","")
            if not href.startswith("http"): continue
            if any(s in href for s in ["youtube.com","facebook.com","wikipedia.org",
                                        "microsoft.com","bing.com","msn.com",
                                        "twitter.com","reddit.com"]): continue
            title = a.get_text(strip=True)
            snip_el = li.find(class_=re.compile(r"b_caption|b_snippet"))
            snippet = snip_el.get_text(strip=True) if snip_el else ""
            if title and href:
                results.append({"url": href, "title": title, "snippet": snippet})
        except: continue

    return results[:num]

SKIP_DOMAINS = {
    "linkedin.com","clutch.co","g2.com","yelp.com","yellowpages.com","bbb.org",
    "indeed.com","glassdoor.com","reddit.com","quora.com","angi.com","thumbtack.com",
    "bark.com","upcity.com","sortlist.com","designrush.com","expertise.com",
    "goodfirms.co","semrush.com","hubspot.com","moz.com","searchenginejournal.com",
    "neilpatel.com","wordstream.com","forbes.com","entrepreneur.com","inc.com",
}

def is_valid_email(email):
    if not email or "@" not in email: return False

    # Must match valid email pattern
    if not re.match(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$", email):
        return False

    # Length check
    if len(email) > 80: return False

    # TLD must be real (not .png .jpg .svg etc)
    tld = email.split(".")[-1].lower()
    if tld in ("png","jpg","jpeg","gif","svg","webp","ico","pdf","zip","js","css","html","php"):
        return False

    # Skip big company / platform emails that aren't leads
    bad_prefixes = ["press@","noreply@","no-reply@","donotreply@","support@",
                    "abuse@","security@","legal@","privacy@","careers@","jobs@",
                    "billing@","payments@","notifications@","alerts@","system@"]
    if any(email.lower().startswith(p) for p in bad_prefixes):
        return False

    # Skip big platforms entirely
    bad_domains = ["example.com","sentry.io","wixpress.com","squarespace.com",
                   "wordpress.org","shopify.com","amazonaws.com","cloudflare.com",
                   "google.com","facebook.com","twitter.com","instagram.com",
                   "linkedin.com","hubspot.com","mailchimp.com","w3.org","schema.org",
                   "apple.com","microsoft.com","godaddy.com","namecheap.com",
                   "duckduckgo.com","bing.com","yahoo.com","upwork.com","fiverr.com",
                   "toptal.com","apollo.io","zoominfo.com","salesforce.com",
                   "rocketreach.co","leadium.com","aeroleads.com","saleshandy.com",
                   "wiza.co","saleshive.com","marketerhire.com","agencyspotter.com",
                   "umbrex.com","webflow.com","thesocialshepherd.com"]
    domain = email.split("@")[1].lower()
    if any(b in domain for b in bad_domains): return False

    # Detect ROT13 obfuscated emails (common spam protection)
    # ROT13 domains have unusual character distributions
    local_part = email.split("@")[0].lower()
    if len(local_part) > 15 and not any(v in local_part for v in "aeiou"):
        return False  # No vowels in long local part = likely encoded

    return True

def find_email(website_url):
    if not website_url or not website_url.startswith("http"): return None
    domain = urlparse(website_url).netloc.replace("www.","")

    for path in ["/contact", "/contact-us", "/about", "/about-us", "/team", ""]:
        url = website_url.rstrip("/") + path
        try:
            html = fetch(url, timeout=8)
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
                # Prefer domain-matching email
                domain_match = [e for e in emails if domain in e.split("@")[1]]
                preferred = [e for e in emails if any(
                    e.startswith(p) for p in ["info@","contact@","hello@","hi@","team@","hey@"]
                )]
                if domain_match: return domain_match[0]
                if preferred: return preferred[0]
                return sorted(emails)[0]

            time.sleep(0.5)
        except: continue
    return None

def extract_info(result):
    title = result.get("title","")
    snippet = result.get("snippet","")
    url = result.get("url","")

    # Clean name
    name = re.split(r'\s*[\|\-–—·]\s*', title)[0].strip()
    if len(name) > 60: name = name[:60]
    if not name or len(name) < 3:
        name = urlparse(url).netloc.replace("www.","").split(".")[0].title()

    # Location
    loc = re.search(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)?),?\s*([A-Z]{2})\b', snippet)
    city = loc.group(0) if loc else "United States"

    # Problem
    text = (title + " " + snippet).lower()
    if "small" in text or "boutique" in text or "independent" in text:
        problem = "small agency — needs consistent lead pipeline"
    elif "startup" in text:
        problem = "startup — needs customer acquisition system"
    elif "consultant" in text or "freelance" in text:
        problem = "consultant — needs automated outreach"
    elif "coach" in text:
        problem = "coach — needs more clients"
    else:
        problem = "online agency — lead generation opportunity"

    return name, city, problem

def run():
    today = datetime.now().strftime("%Y-%m-%d")
    output_file = OUTPUT_DIR / f"google_agencies_{today}.csv"
    all_leads = []
    seen_emails = set()
    seen_domains = set()

    print(f"\nAgency Scraper (DDG + Bing) — {len(QUERIES)} queries")
    print("=" * 55)

    for i, query in enumerate(QUERIES):
        print(f"\n[{i+1}/{len(QUERIES)}] {query[:55]}...")

        # Try DDG first, then Bing
        results = ddg_search(query, num=8)
        if not results:
            print("  DDG returned nothing — trying Bing...")
            results = bing_search(query, num=8)

        print(f"  {len(results)} results")

        for result in results:
            url = result.get("url","")
            if not url: continue

            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.","").lower()
            base_domain = ".".join(domain.split(".")[-2:])

            if base_domain in SKIP_DOMAINS: continue
            if domain in seen_domains: continue
            seen_domains.add(domain)

            name, city, problem = extract_info(result)
            if len(name) < 3: continue

            print(f"  → {name[:40]} ({domain})")
            email = find_email(f"{parsed.scheme}://{parsed.netloc}")
            time.sleep(random.uniform(1.5, 3))

            if not email or email in seen_emails:
                print(f"    no email")
                continue
            seen_emails.add(email)

            # Score
            score = 1
            if "@gmail" not in email and "@yahoo" not in email: score += 1
            if any(k in (result.get("title","") + result.get("snippet","")).lower()
                   for k in ["small","boutique","independent","solo","local"]): score += 1

            all_leads.append({
                "Name": name, "City": city, "Website": url,
                "Problem": problem, "Email": email,
                "Phone": "", "Owner": "", "Score": score,
            })
            print(f"    ✅ {email} (score: {score})")

        # Polite delay
        wait = random.uniform(5, 10)
        print(f"  Waiting {wait:.0f}s...")
        time.sleep(wait)

    if all_leads:
        with open(output_file, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["Name","City","Website","Problem","Email","Phone","Owner","Score"])
            w.writeheader()
            w.writerows(all_leads)
        print(f"\n{'='*55}")
        print(f"✅ Done — {len(all_leads)} agency leads saved")
        print(f"Saved: {output_file}")
    else:
        print(f"\n No leads found. Check your internet connection.")

    return len(all_leads)

if __name__ == "__main__":
    run()
