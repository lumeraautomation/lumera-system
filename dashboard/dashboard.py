# ~/lumera-system/dashboard/dashboard.py
# Lumera Lead Engine — Full Dashboard
# Layout: admin sidebar style | Colors: agencies black/grain/blue-indigo scheme

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pandas as pd
import sqlite3
import secrets
import json
import re
import os
import pytz
import random

load_dotenv(Path(__file__).parent.parent / "config.env")

OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY")
RESEND_API_KEY       = os.getenv("RESEND_API_KEY")
FROM_EMAIL           = os.getenv("FROM_EMAIL", "kory@lumeraautomation.com")
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON")
CALENDAR_ID          = os.getenv("CALENDAR_ID")
MEET_LINK            = os.getenv("MEET_LINK", "https://meet.google.com/new")
DB_PATH              = Path(__file__).parent.parent / "outreach.db"
DAILY_LEADS_DIR      = Path(__file__).parent.parent / "daily_leads"
DAILY_LEADS_DIR.mkdir(exist_ok=True)  # Create if not exists (important on Render)
SCRIPTS_DIR          = Path(__file__).parent.parent / "scripts"
CENTRAL              = pytz.timezone("America/Chicago")
CALL_DURATION_MINS   = 30

app = FastAPI(title="Lumera Lead Engine")

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
ADMIN_USER = "admin"
ADMIN_PASS = "lumera2024"
# Use DB-backed sessions so they survive Render restarts
def get_current_user(request: Request):
    token = request.cookies.get("lumera_token")
    if not token:
        return None
    rows = db_query("SELECT username FROM sessions WHERE token=? AND expires_at > datetime('now')", (token,))
    return rows[0]["username"] if rows else None

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS outreach (
            id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE,
            name TEXT, business TEXT, niche TEXT, city TEXT, problem TEXT,
            status TEXT NOT NULL DEFAULT 'sent', step INTEGER NOT NULL DEFAULT 1,
            enrolled_at TEXT NOT NULL, last_sent_at TEXT NOT NULL,
            next_send_at TEXT, replied INTEGER NOT NULL DEFAULT 0,
            unsubscribed INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            email TEXT NOT NULL, business TEXT, start_time TEXT NOT NULL,
            meet_link TEXT, status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL, niche TEXT NOT NULL DEFAULT '*',
            email TEXT, business TEXT, monthly_fee REAL DEFAULT 497.0,
            setup_fee REAL DEFAULT 1000.0, status TEXT DEFAULT 'active',
            start_date TEXT, notes TEXT, created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sales_pipeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT, business TEXT NOT NULL,
            contact TEXT, email TEXT, value REAL DEFAULT 0,
            stage TEXT DEFAULT 'prospect', notes TEXT,
            created_at TEXT NOT NULL, updated_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT,
            business TEXT, niche TEXT, challenge TEXT, notes TEXT,
            status TEXT DEFAULT 'new', created_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, username TEXT NOT NULL,
            expires_at TEXT NOT NULL)""")
        try:
            conn.execute("ALTER TABLE bookings ADD COLUMN meet_link TEXT")
        except: pass
        conn.commit()

init_db()

def db_query(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def db_run(sql, params=()):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(sql, params)
        conn.commit()

def get_clients():     return db_query("SELECT * FROM clients ORDER BY created_at DESC")
def get_all_outreach():return db_query("SELECT * FROM outreach ORDER BY enrolled_at DESC")
def get_all_bookings():return db_query("SELECT * FROM bookings ORDER BY start_time ASC")
def get_pipeline():    return db_query("SELECT * FROM sales_pipeline ORDER BY created_at DESC")

def enroll_lead(email, name, business, niche, city, problem):
    now = datetime.now()
    next_send = now + timedelta(days=3)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO outreach(email,name,business,niche,city,problem,
            status,step,enrolled_at,last_sent_at,next_send_at)
            VALUES(?,?,?,?,?,?,'sent',1,?,?,?)
            ON CONFLICT(email) DO UPDATE SET last_sent_at=excluded.last_sent_at,
            status='sent',step=1,next_send_at=excluded.next_send_at,replied=0,unsubscribed=0""",
            (email,name,business,niche,city,problem,
             now.isoformat(),now.isoformat(),next_send.isoformat()))
        conn.commit()

def save_booking(name, email, business, start_time, meet_link=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO bookings(name,email,business,start_time,meet_link,status,created_at)
            VALUES(?,?,?,?,?,'confirmed',?)""",
            (name,email,business,start_time,meet_link,datetime.now().isoformat()))
        conn.commit()

def mark_replied(email):     db_run("UPDATE outreach SET replied=1,status='replied' WHERE email=?",(email,))
def mark_unsubscribed(email):db_run("UPDATE outreach SET unsubscribed=1,status='unsubscribed' WHERE email=?",(email,))

def mark_followup_sent(lead_id, new_step):
    now = datetime.now()
    next_send = now + timedelta(days=2) if new_step == 2 else None
    status = "followup_2" if new_step == 3 else "followup_1"
    db_run("UPDATE outreach SET step=?,status=?,last_sent_at=?,next_send_at=? WHERE id=?",
           (new_step,status,now.isoformat(),next_send.isoformat() if next_send else None,lead_id))

def get_pending_followups():
    return db_query("""SELECT * FROM outreach
        WHERE replied=0 AND unsubscribed=0 AND step<3 AND next_send_at<=?""",
        (datetime.now().isoformat(),))

# ─────────────────────────────────────────────
# GOOGLE CALENDAR
# ─────────────────────────────────────────────
def get_calendar_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SERVICE_ACCOUNT_JSON),
        scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar","v3",credentials=creds)

def get_available_slots(days_ahead=7):
    try:
        svc = get_calendar_service()
        now = datetime.now(CENTRAL)
        end = now + timedelta(days=days_ahead)
        result = svc.freebusy().query(body={
            "timeMin":now.isoformat(),"timeMax":end.isoformat(),
            "items":[{"id":CALENDAR_ID}]}).execute()
        busy = result.get("calendars",{}).get(CALENDAR_ID,{}).get("busy",[])
        busy_ranges = [(datetime.fromisoformat(b["start"]).astimezone(CENTRAL),
                        datetime.fromisoformat(b["end"]).astimezone(CENTRAL)) for b in busy]
        slots = []
        cur = now.replace(minute=0,second=0,microsecond=0)+timedelta(hours=1)
        while cur <= end:
            if 0<=cur.weekday()<=4 and 9<=cur.hour<17:
                slot_end = cur+timedelta(minutes=CALL_DURATION_MINS)
                if not any(bs<slot_end and be>cur for bs,be in busy_ranges):
                    slots.append({"start":cur.isoformat(),"end":slot_end.isoformat(),
                        "display":cur.strftime("%A, %B %d at %I:%M %p")+" CT"})
            cur += timedelta(minutes=30)
        return slots[:20]
    except Exception as e:
        print(f"Slots error: {e}")
        return []

def get_upcoming_events(max_results=10):
    try:
        svc = get_calendar_service()
        now = datetime.now(CENTRAL).isoformat()
        result = svc.events().list(calendarId=CALENDAR_ID,timeMin=now,
            maxResults=max_results,singleEvents=True,orderBy="startTime").execute()
        events = []
        for e in result.get("items",[]):
            start = e["start"].get("dateTime",e["start"].get("date",""))
            try:
                dt = datetime.fromisoformat(start).astimezone(CENTRAL)
                display = dt.strftime("%a %b %d · %I:%M %p CT")
            except: display = start
            events.append({"summary":e.get("summary","Untitled"),"display":display,"start":start})
        return events
    except Exception as e:
        print(f"Events error: {e}")
        return []

def create_booking(name, email, business, start_iso):
    try:
        svc = get_calendar_service()
        start_dt = datetime.fromisoformat(start_iso).astimezone(CENTRAL)
        end_dt   = start_dt+timedelta(minutes=CALL_DURATION_MINS)
        svc.events().insert(calendarId=CALENDAR_ID,body={
            "summary":f"Lumera Strategy Call — {name}",
            "description":f"Name: {name}\nBusiness: {business}\nEmail: {email}\n\nMeet: {MEET_LINK}",
            "start":{"dateTime":start_dt.isoformat(),"timeZone":"America/Chicago"},
            "end":{"dateTime":end_dt.isoformat(),"timeZone":"America/Chicago"},
        }).execute()
        return MEET_LINK
    except Exception as e:
        print(f"Booking error: {e}")
        return None

def send_booking_confirmation(name, email, time_str, meet_link=None):
    if not RESEND_API_KEY: return
    import resend as r
    r.api_key = RESEND_API_KEY
    meet_html = f"""<div style="margin:20px 0;padding:16px;background:#0a0a0a;border:1px solid rgba(99,102,241,0.3);border-radius:8px;">
        <p style="margin:0 0 6px;font-size:11px;font-weight:700;color:#6366f1;text-transform:uppercase;letter-spacing:.1em;">Google Meet</p>
        <a href="{meet_link}" style="color:#3b82f6;font-size:13px;">{meet_link}</a>
    </div>""" if meet_link else ""
    try:
        r.Emails.send({
            "from":f"Kory @ Lumera Automation <{FROM_EMAIL}>",
            "to":email,
            "subject":"Your Lumera Strategy Call is Confirmed!",
            "html":f"""<div style="font-family:'Helvetica Neue',sans-serif;max-width:540px;margin:0 auto;background:#080808;color:#ffffff;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.07);">
                <div style="font-size:20px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:20px;">LUMERA</div>
                <h2 style="font-size:20px;font-weight:700;margin-bottom:8px;">You're booked, {name}!</h2>
                <p style="color:rgba(255,255,255,0.5);margin-bottom:20px;">Your free 30-minute strategy call is confirmed for<br><strong style="color:#fff;">{time_str}</strong></p>
                {meet_html}
                <p style="color:rgba(255,255,255,0.45);font-size:14px;">We'll walk through your business and show exactly how Lumera can work for you.</p>
                <br><p style="color:rgba(255,255,255,0.3);font-size:13px;">— Kory @ Lumera Automation</p>
            </div>"""
        })
    except Exception as e: print(f"Email error: {e}")

# ─────────────────────────────────────────────
# LEADS
# ─────────────────────────────────────────────
def rescore_lead(row):
    """LeadRadar-style scoring using all available signals."""
    import re as _re
    score = 0
    email       = str(row.get("Email","")).lower()
    phone       = str(row.get("Phone","")).strip()
    website     = str(row.get("Website","")).lower()
    problem     = str(row.get("Problem","")).lower()
    has_booking = str(row.get("HasBooking","")).lower()
    reviews     = str(row.get("Reviews","")).strip()
    rating      = str(row.get("Rating","")).strip()
    hours       = str(row.get("Hours","")).lower()

    # Phone listed = relies on calls
    if phone and phone not in ["—","","nan","None"]: score += 1

    # No website = phone-dependent
    if website in ["none listed","none","n/a","","nan"]: score += 2
    elif "no website" in problem or "phone-dependent" in problem: score += 1

    # No online booking = all inbound calls
    if any(x in has_booking for x in ["no","false","none","n/a"]): score += 1
    if "no booking" in problem or "no online booking" in problem: score += 1

    # High review count = busy business = missing calls
    try:
        rev = int(_re.sub(r"[^\d]","",reviews))
        if rev >= 50:  score += 1
        if rev >= 100: score += 1
    except: pass

    # Good rating = popular = high demand
    try:
        rat = float(_re.sub(r"[^\d.]","",rating))
        if rat >= 4.0: score += 1
    except: pass

    # Limited hours = goes dark after 5pm
    if any(x in hours for x in ["closes at 5","closes at 4","closed weekend","limited hours"]): score += 1
    if "limited hours" in problem or "after hours" in problem: score += 1

    # High call volume problem signals
    if any(x in problem for x in ["high call","busy","high demand","phone-dependent",
                                    "low reviews","few reviews","no website"]): score += 1

    # Email quality
    if "@" in email and any(e in email for e in ["gmail","yahoo","icloud"]): score += 1  # owner reachable
    elif "@" in email: score += 1  # business email

    return min(score, 5)  # cap at 5

def heat_from_score(score):
    try: s=int(score)
    except: return "cold"
    return "hot" if s>=3 else "warm" if s>=2 else "cold"

def load_all_leads():
    leads=[]; idx=0
    for csv_file in sorted(DAILY_LEADS_DIR.glob("*.csv")):
        if "_hot" in csv_file.name: continue
        try:
            df=pd.read_csv(csv_file).fillna("")
            # Detect niche from filename - handle multi-word names like apollo_agencies
            stem = csv_file.stem
            if "apollo" in stem.lower():
                niche = "Apollo Agencies"
            else:
                niche = stem.split("_")[0].capitalize()
            for row in df.to_dict(orient="records"):
                # Rescore every lead dynamically
                new_score = rescore_lead(row)
                row["Score"] = new_score
                row["_niche"]=niche; row["_heat"]=heat_from_score(new_score); row["_idx"]=idx
                idx+=1; leads.append(row)
        except Exception as e: print(f"CSV error: {e}")
    return leads

# ─────────────────────────────────────────────
# NOISE TEXTURE (agencies style)
# ─────────────────────────────────────────────
NOISE_SVG = "data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='1'/%3E%3C/svg%3E"

# ─────────────────────────────────────────────
# HTML SHELL — Admin layout + Agencies colors
# ─────────────────────────────────────────────
NAV_SECTIONS = [
    ("MAIN", [
        ("overview",  "fa-house",          "Overview"),
    ]),
    ("ANALYTICS", [
        ("analytics", "fa-chart-line",     "Analytics"),
        ("sales",     "fa-handshake",      "Sales"),
    ]),
    ("PIPELINE", [
        ("leads",     "fa-crosshairs",     "Leads"),
        ("outreach",  "fa-paper-plane",    "Outreach"),
        ("calendar",  "fa-calendar-days",  "Calendar"),
    ]),
    ("SYSTEM", [
        ("system",       "fa-gear",           "System"),
        ("team",         "fa-users",          "Team"),
        ("revenue",      "fa-dollar-sign",    "Revenue"),
        ("bookings",     "fa-calendar-check", "Bookings"),
        ("applications", "fa-inbox",          "Applications"),
    ]),
]

def shell(content: str, active: str = "overview", user: str = "admin") -> str:
    nav_html = ""
    for section_label, items in NAV_SECTIONS:
        nav_html += f'<div class="nav-section-lbl">{section_label}</div>'
        for key, icon, label in items:
            cls = "active" if key == active else ""
            nav_html += f'<button class="nav-item {cls}" onclick="window.location=\'/{key}\'">'
            nav_html += f'<i class="fa-solid {icon}"></i>{label}</button>'
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Lumera · {active.capitalize()}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800&display=swap" rel="stylesheet"/>
<style>
:root {{
  --black:   #080808;
  --surface: #111111;
  --surface2:#181818;
  --border:  rgba(255,255,255,0.07);
  --border2: rgba(255,255,255,0.14);
  --text:    #ffffff;
  --muted:   rgba(255,255,255,0.45);
  --muted2:  rgba(255,255,255,0.25);
  --blue:    #3b82f6;
  --indigo:  #6366f1;
  --purple:  #8b5cf6;
  --green:   #22c55e;
  --red:     #f43f5e;
  --amber:   #f59e0b;
  --grad:    linear-gradient(135deg,#3b82f6,#6366f1);
  --grad-r:  linear-gradient(135deg,#6366f1,#3b82f6);
  --navy:    #080808;
  --font:    'Montserrat',sans-serif;
  --sidebar: 240px;
}}
/* LIGHT MODE */
body.light {{
  --black:   #f4f6fb;
  --surface: #ffffff;
  --surface2:#f1f5f9;
  --border:  rgba(0,0,0,0.08);
  --border2: rgba(0,0,0,0.14);
  --text:    #0f172a;
  --muted:   rgba(0,0,0,0.45);
  --muted2:  rgba(0,0,0,0.30);
  --navy:    #f4f6fb;
}}
body.light .sidebar{{background:#0f172a;}}
body.light .nav-item{{color:rgba(255,255,255,0.55);}}
body.light .nav-item:hover{{background:rgba(255,255,255,0.08);color:rgba(255,255,255,0.9);}}
body.light .sb-logo{{border-bottom-color:rgba(255,255,255,0.08);}}
body.light .sb-footer{{border-top-color:rgba(255,255,255,0.08);}}
body.light .nav-section-lbl{{color:rgba(255,255,255,0.25);}}
body.light .user-chip{{background:rgba(255,255,255,0.06);}}
body.light .u-name{{color:rgba(255,255,255,0.85);}}
body.light .u-role{{color:rgba(255,255,255,0.35);}}
body.light .logout-link{{border-color:rgba(255,255,255,0.1);color:rgba(255,255,255,0.4);}}
body.light .topbar{{background:rgba(255,255,255,0.92);border-bottom-color:rgba(0,0,0,0.08);}}
body.light .topbar-title{{color:#0f172a;}}
body.light .tb-pill{{background:#f1f5f9;color:#64748b;border-color:#e2e8f0;}}
body.light .metric-card{{background:#ffffff;border-color:#e2e8f0;box-shadow:0 2px 8px rgba(0,0,0,0.06);}}
body.light .card{{background:#ffffff;border-color:#e2e8f0;box-shadow:0 2px 8px rgba(0,0,0,0.04);}}
body.light thead th{{background:#f8fafc;color:#94a3b8;border-bottom-color:#e2e8f0;}}
body.light tbody td{{color:#334155;border-bottom-color:#f1f5f9;}}
body.light tbody tr:hover td{{background:#f8fafc;}}
body.light .tbl-wrap{{border-color:#e2e8f0;}}
body.light .modal{{background:#ffffff;border-color:#e2e8f0;}}
body.light .form-field input,body.light .form-field select,body.light .form-field textarea{{background:#f8fafc;border-color:#e2e8f0;color:#0f172a;}}
body.light .cal-event{{background:#f8fafc;border-color:#e2e8f0;}}
body.light .sys-row{{background:#f8fafc;border-color:#e2e8f0;}}
body.light .sys-info h4{{color:#0f172a;}}
body.light .sys-info p{{color:#64748b;}}
body.light .heat-btn{{border-color:#e2e8f0;color:#64748b;background:#ffffff;}}
body.light .bulk-bar{{background:#eff6ff;border-color:#bfdbfe;}}
body.light .search-input{{background:#ffffff;border-color:#e2e8f0;color:#0f172a;}}
body.light .filter-select{{background:#ffffff;border-color:#e2e8f0;color:#334155;}}
body.light .m-delta{{color:#64748b;}}
body.light .m-value{{color:#0f172a;}}
body.light .m-label{{color:#94a3b8;}}
body.light td.bold{{color:#0f172a;}}
body.light .nav-section-lbl{{color:rgba(255,255,255,0.25);}}
body.light .tb-avatar{{background:var(--grad);}}

*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%;overflow:hidden}}
body{{font-family:var(--font);background:var(--black);color:var(--text);display:flex;height:100vh;
  background-image:url("{NOISE_SVG}");background-size:200px;background-repeat:repeat;
  background-blend-mode:overlay;}}

/* SIDEBAR — exact admin style with agencies colors */
.sidebar{{width:var(--sidebar);flex-shrink:0;background:var(--black);display:flex;flex-direction:column;
  height:100vh;border-right:1px solid var(--border);position:relative;z-index:10;}}
.sb-logo{{padding:26px 22px 22px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);}}
.sb-icon{{width:34px;height:34px;border-radius:10px;background:var(--grad);display:flex;align-items:center;
  justify-content:center;font-size:15px;box-shadow:0 4px 14px rgba(99,102,241,0.45);flex-shrink:0;}}
.sb-logo-text{{font-size:15px;font-weight:700;background:var(--grad);-webkit-background-clip:text;
  -webkit-text-fill-color:transparent;background-clip:text;}}
.sb-nav{{flex:1;padding:18px 12px;overflow-y:auto;display:flex;flex-direction:column;gap:2px;}}
.sb-nav::-webkit-scrollbar{{width:0}}
.nav-section-lbl{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
  color:var(--muted2);padding:0 10px;margin:14px 0 5px;}}
.nav-item{{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;font-size:13px;
  font-weight:600;color:var(--muted);cursor:pointer;transition:all .2s;border:none;background:none;
  width:100%;text-align:left;font-family:var(--font);}}
.nav-item i{{width:16px;font-size:13px;text-align:center;}}
.nav-item:hover{{background:rgba(255,255,255,0.06);color:rgba(255,255,255,0.82);}}
.nav-item.active{{background:var(--grad);color:white;box-shadow:0 4px 14px rgba(99,102,241,0.38);}}
.sb-footer{{padding:14px 12px;border-top:1px solid var(--border);}}
.user-chip{{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:10px;
  background:rgba(255,255,255,0.04);}}
.u-avatar{{width:32px;height:32px;border-radius:50%;background:var(--grad);display:flex;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;color:white;flex-shrink:0;}}
.u-name{{font-size:12px;font-weight:700;color:rgba(255,255,255,0.82);}}
.u-role{{font-size:11px;color:var(--muted2);}}
.logout-link{{display:block;text-align:center;margin-top:8px;padding:6px;border-radius:8px;
  border:1px solid var(--border);color:var(--muted);font-size:11px;text-decoration:none;
  font-weight:600;transition:all .2s;letter-spacing:.06em;}}
.logout-link:hover{{border-color:var(--red);color:var(--red);}}

/* MAIN */
.main{{flex:1;display:flex;flex-direction:column;height:100vh;overflow:hidden;}}
.topbar{{height:62px;flex-shrink:0;background:rgba(8,8,8,0.85);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;
  padding:0 28px;}}
.topbar-title{{font-size:15px;font-weight:700;color:rgba(255,255,255,0.82);}}
.topbar-right{{display:flex;align-items:center;gap:10px;}}
.tb-pill{{padding:5px 13px;font-size:11px;font-weight:700;background:rgba(255,255,255,0.05);
  color:var(--muted);border-radius:20px;border:1px solid var(--border);}}
.online-dot{{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--green);
  margin-right:5px;box-shadow:0 0 0 3px rgba(34,197,94,0.2);animation:pulse 2s ease-in-out infinite;}}
@keyframes pulse{{0%,100%{{box-shadow:0 0 0 3px rgba(34,197,94,0.2)}}50%{{box-shadow:0 0 0 6px rgba(34,197,94,0.05)}}}}
.tb-avatar{{width:35px;height:35px;border-radius:50%;background:var(--grad);display:flex;align-items:center;
  justify-content:center;font-size:11px;font-weight:700;color:white;}}

/* CONTENT */
.content{{flex:1;overflow-y:auto;padding:26px;}}
.content::-webkit-scrollbar{{width:4px;}}
.content::-webkit-scrollbar-thumb{{background:rgba(255,255,255,0.1);border-radius:2px;}}

/* PAGE HEADER */
.page-hdr{{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:22px;gap:16px;flex-wrap:wrap;}}
.page-title{{font-size:20px;font-weight:800;color:var(--text);margin-bottom:4px;}}
.page-sub{{font-size:11px;color:var(--muted);font-weight:500;}}

/* METRIC CARDS — exact admin style with agencies colors */
.metrics-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px;margin-bottom:22px;}}
.metric-card{{background:var(--surface);border-radius:16px;padding:20px;border:1px solid var(--border);
  transition:border-color .2s;}}
.metric-card:hover{{border-color:var(--border2);}}
.m-icon{{width:38px;height:38px;border-radius:10px;background:var(--grad);display:flex;align-items:center;
  justify-content:center;color:white;font-size:15px;margin-bottom:12px;box-shadow:0 5px 14px rgba(99,102,241,0.3);}}
.m-label{{font-size:11px;font-weight:700;color:var(--muted);margin-bottom:5px;text-transform:uppercase;letter-spacing:.06em;}}
.m-value{{font-size:24px;font-weight:700;color:var(--text);}}
.m-delta{{font-size:11px;font-weight:600;margin-top:4px;color:var(--muted);}}
.m-delta.up{{color:var(--green);}} .m-delta.dn{{color:var(--red);}}

/* CARDS */
.card{{background:var(--surface);border-radius:18px;padding:24px;border:1px solid var(--border);
  margin-bottom:18px;}}
.card-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;}}
.card-title{{font-size:14px;font-weight:700;color:var(--text);}}
.card-sub{{font-size:11px;font-weight:600;color:var(--muted);}}
.charts-row{{display:grid;grid-template-columns:2fr 1fr;gap:18px;margin-bottom:18px;}}
.grid-2{{display:grid;grid-template-columns:1fr 1fr;gap:18px;}}
.grid-3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;}}

/* TABLES */
table{{width:100%;border-collapse:collapse;font-size:13px;font-weight:500;}}
thead th{{font-size:10px;font-weight:700;color:var(--muted);text-align:left;padding:8px 14px;
  border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.06em;
  background:rgba(255,255,255,0.02);}}
tbody td{{font-size:13px;font-weight:500;color:rgba(255,255,255,0.7);padding:11px 14px;
  border-bottom:1px solid var(--border);}}
tbody tr:last-child td{{border-bottom:none;}}
tbody tr:hover td{{background:rgba(255,255,255,0.03);}}
td.bold{{color:var(--text);font-weight:700;}}
.tbl-wrap{{border-radius:12px;border:1px solid var(--border);overflow:hidden;}}

/* BADGES */
.badge{{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:20px;
  font-size:11px;font-weight:700;}}
.b-hot{{background:rgba(244,63,94,.12);color:var(--red);border:1px solid rgba(244,63,94,.2);}}
.b-warm{{background:rgba(245,158,11,.1);color:var(--amber);border:1px solid rgba(245,158,11,.2);}}
.b-cold{{background:rgba(255,255,255,.06);color:var(--muted);border:1px solid var(--border);}}
.b-active{{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2);}}
.b-sent{{background:rgba(59,130,246,.1);color:var(--blue);border:1px solid rgba(59,130,246,.2);}}
.b-replied{{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2);}}
.b-followup_1{{background:rgba(245,158,11,.1);color:var(--amber);border:1px solid rgba(245,158,11,.2);}}
.b-followup_2{{background:rgba(139,92,246,.1);color:var(--purple);border:1px solid rgba(139,92,246,.2);}}
.b-unsubscribed{{background:rgba(244,63,94,.1);color:var(--red);border:1px solid rgba(244,63,94,.2);}}
.b-confirmed{{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2);}}
.b-prospect{{background:rgba(59,130,246,.1);color:var(--blue);border:1px solid rgba(59,130,246,.2);}}
.b-qualified{{background:rgba(99,102,241,.1);color:var(--indigo);border:1px solid rgba(99,102,241,.2);}}
.b-proposal{{background:rgba(245,158,11,.1);color:var(--amber);border:1px solid rgba(245,158,11,.2);}}
.b-closed{{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2);}}

/* BUTTONS */
.btn{{padding:8px 16px;border-radius:10px;font-size:12px;font-weight:700;cursor:pointer;border:none;
  font-family:var(--font);transition:all .2s;letter-spacing:.02em;}}
.btn-primary{{background:var(--grad);color:white;box-shadow:0 4px 14px rgba(99,102,241,0.3);}}
.btn-primary:hover{{opacity:.9;transform:translateY(-1px);}}
.btn-ghost{{background:transparent;color:var(--muted);border:1px solid var(--border2);}}
.btn-ghost:hover{{border-color:rgba(255,255,255,0.3);color:var(--text);}}
.btn-danger{{background:transparent;color:var(--red);border:1px solid rgba(244,63,94,.2);}}
.btn-danger:hover{{background:rgba(244,63,94,.1);}}
.btn-green{{background:transparent;color:var(--green);border:1px solid rgba(34,197,94,.2);}}
.btn-green:hover{{background:rgba(34,197,94,.1);}}
.btn-sm{{padding:5px 11px;font-size:11px;}}
.btn:disabled{{opacity:.4;cursor:wait;}}

/* FORM */
.form-field{{margin-bottom:14px;}}
.form-field label{{display:block;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:6px;}}
.form-field input,.form-field select,.form-field textarea{{
  width:100%;background:var(--black);border:1px solid var(--border2);border-radius:10px;
  padding:9px 12px;color:var(--text);font-family:var(--font);font-size:12px;outline:none;}}
.form-field input:focus,.form-field select:focus,.form-field textarea:focus{{
  border-color:var(--indigo);box-shadow:0 0 0 3px rgba(99,102,241,.12);}}
.form-field textarea{{resize:vertical;min-height:80px;}}
.form-field select option{{background:var(--surface);}}

/* FILTER BAR */
.filter-bar{{display:flex;align-items:center;gap:10px;margin-bottom:18px;flex-wrap:wrap;}}
.search-input{{flex:1;min-width:180px;padding:9px 12px 9px 32px;font-size:12px;font-weight:600;
  border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,0.04);
  color:var(--text);font-family:var(--font);outline:none;}}
.search-input:focus{{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(99,102,241,.1);}}
.search-wrap{{position:relative;flex:1;min-width:180px;}}
.search-wrap i{{position:absolute;left:11px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:12px;}}
.filter-select{{padding:9px 10px;font-size:12px;font-weight:600;border:1px solid var(--border);
  border-radius:10px;background:rgba(255,255,255,0.04);color:rgba(255,255,255,0.7);
  font-family:var(--font);cursor:pointer;outline:none;}}
.filter-select option{{background:var(--surface);}}
.heat-btn{{padding:7px 14px;border-radius:8px;border:1px solid var(--border);background:transparent;
  font-size:11px;font-weight:700;cursor:pointer;color:var(--muted);font-family:var(--font);transition:all .18s;}}
.heat-btn:hover,.heat-btn.active{{border-color:var(--indigo);color:white;background:rgba(99,102,241,.15);}}

/* BULK BAR */
.bulk-bar{{display:none;align-items:center;gap:10px;margin-bottom:16px;padding:10px 14px;
  background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.2);border-radius:10px;}}
.bulk-bar.show{{display:flex;}}
.bulk-bar span{{font-size:12px;font-weight:700;color:var(--indigo);}}

/* SCORE BAR */
.score-wrap{{display:flex;align-items:center;gap:8px;}}
.sbar{{flex:1;height:4px;background:rgba(255,255,255,.08);border-radius:2px;min-width:40px;}}
.sbar-fill{{height:100%;border-radius:2px;}}
.snum{{font-size:11px;font-weight:700;min-width:14px;text-align:right;}}

/* STEP DOTS */
.step-dots{{display:flex;gap:4px;}}
.dot{{width:8px;height:8px;border-radius:50%;background:rgba(255,255,255,.1);}}
.dot.done{{background:var(--indigo);box-shadow:0 0 4px rgba(99,102,241,.5);}}
.dot.replied{{background:var(--green);}}

/* CAL EVENTS */
.cal-event{{display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:10px;
  border:1px solid var(--border);background:rgba(255,255,255,.03);margin-bottom:8px;
  transition:border-color .2s;}}
.cal-event:hover{{border-color:var(--border2);}}
.cal-dot{{width:8px;height:8px;border-radius:50%;background:var(--grad);flex-shrink:0;
  box-shadow:0 0 6px rgba(99,102,241,.6);}}
.cal-title{{font-size:12px;font-weight:600;color:var(--text);}}
.cal-time{{font-size:11px;color:var(--muted);margin-top:2px;}}

/* SYSTEM CARDS */
.sys-row{{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding:16px 18px;border-radius:12px;border:1px solid var(--border);
  background:rgba(255,255,255,.03);margin-bottom:10px;}}
.sys-info h4{{font-size:13px;font-weight:700;color:var(--text);margin-bottom:3px;}}
.sys-info p{{font-size:11px;color:var(--muted);}}
.sys-tag{{font-size:10px;font-weight:700;padding:3px 9px;border-radius:20px;letter-spacing:.06em;}}
.sys-ok{{background:rgba(34,197,94,.1);color:var(--green);border:1px solid rgba(34,197,94,.2);}}
.sys-warn{{background:rgba(245,158,11,.1);color:var(--amber);border:1px solid rgba(245,158,11,.2);}}

/* FUNNEL BARS */
.funnel-row{{margin-bottom:16px;}}
.funnel-label{{display:flex;justify-content:space-between;margin-bottom:5px;}}
.funnel-label span:first-child{{font-size:12px;font-weight:600;color:rgba(255,255,255,.7);}}
.funnel-label span:last-child{{font-size:12px;font-weight:700;color:var(--text);}}
.funnel-track{{height:6px;background:rgba(255,255,255,.06);border-radius:3px;overflow:hidden;}}
.funnel-fill{{height:100%;border-radius:3px;background:var(--grad);}}

/* MODAL */
.modal-overlay{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:1000;
  align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(6px);}}
.modal-overlay.open{{display:flex;}}
.modal{{background:var(--surface);border:1px solid var(--border2);border-radius:18px;
  padding:28px;width:100%;max-width:500px;max-height:90vh;overflow-y:auto;}}
.modal h3{{font-size:15px;font-weight:800;margin-bottom:20px;color:var(--text);}}
.modal-btns{{display:flex;gap:10px;justify-content:flex-end;margin-top:20px;}}
.email-modal{{max-width:580px;}}

/* TOAST */
.toast{{position:fixed;bottom:24px;right:24px;background:var(--surface);border:1px solid var(--border2);
  color:var(--text);padding:11px 18px;border-radius:12px;font-size:12px;font-weight:600;
  z-index:9999;opacity:0;transform:translateY(8px);transition:all .25s;pointer-events:none;
  box-shadow:0 8px 24px rgba(0,0,0,.5);}}
.toast.show{{opacity:1;transform:translateY(0);}}
.toast.ok{{border-color:rgba(34,197,94,.3);color:var(--green);}}
.toast.err{{border-color:rgba(244,63,94,.3);color:var(--red);}}

.empty-state{{padding:50px;text-align:center;color:var(--muted);font-size:13px;}}
input[type=checkbox]{{accent-color:var(--indigo);width:14px;height:14px;cursor:pointer;}}
a{{color:var(--blue);text-decoration:none;}}
a:hover{{text-decoration:underline;}}
</style>
</head>
<body>

<!-- SIDEBAR -->
<div class="sidebar">
  <div class="sb-logo">
    <img src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAQ0AAAFVCAYAAAD1z7qSAAABCGlDQ1BJQ0MgUHJvZmlsZQAAeJxjYGA8wQAELAYMDLl5JUVB7k4KEZFRCuwPGBiBEAwSk4sLGHADoKpv1yBqL+viUYcLcKakFicD6Q9ArFIEtBxopAiQLZIOYWuA2EkQtg2IXV5SUAJkB4DYRSFBzkB2CpCtkY7ETkJiJxcUgdT3ANk2uTmlyQh3M/Ck5oUGA2kOIJZhKGYIYnBncAL5H6IkfxEDg8VXBgbmCQixpJkMDNtbGRgkbiHEVBYwMPC3MDBsO48QQ4RJQWJRIliIBYiZ0tIYGD4tZ2DgjWRgEL7AwMAVDQsIHG5TALvNnSEfCNMZchhSgSKeDHkMyQx6QJYRgwGDIYMZAKbWPz9HbOBQAACcpElEQVR4nOz96ZMcR5blC/6uqpm5e+wIILDvC8GdTDKTuVdWVlcvr99Iy0jLkzcyIvP3jch8HHnTPd3SXa+qq7oqN2YySYLggh2IBUBEIPZwdzNVve+Dqpl7gGRuJIAA6YcCRoS7ubm5uem1u5x7LowwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII4wwwggjjDDCCCOMMMIII3wbMH1GmTqjz/owRni+Ic/6AEb4mjF7Vhkbh4lJxqZnmZ6dw+YFU+MzjHcmsArdzV12N7cpqx7La0vs7qzB+iNY+XR0PYzwRzG6SL4JaJ9V2uMUJ05w+sWXOHT2PAePn2Tq8DHIWlib0c7GaGdtrILvVrhuH+8rHnU3WF1Z4OGd6yzd+oi161dh4f3RdTHCl2J0cTzvmHld2xdf4cKLr3Dy8kXmTp+ndeAgvtWGYgyvBqcgDowXbDBkATIErKGvFeq7SH+T3Yd3efDZe9z6/S9Y/OQ93MObo+tjhM9hdFE8zzj6uk6+8TNe+/5f8/Kbb5JPTtFVS0+hj8GbDMnbCBYLmDD8Twmq7PoKK4FWqGiHHdq9DdbufcbVX/w9n773C/o3fzW6RkbYg+xZH8AIfyFe+oGeeevHvPzT/8DsqRfRiQnWen36mpG1Otgiwwjs9BQjYAAUjIIREAMhKHasRfCwtavs9HOm8gOMnXqVc7QZmzvNh784qjv3PoNHH4+MxwjAyGg8n7j4tr78k5/y+s/+LdnRS7hinLXKEUxO3mqDZOzulPTKksmpCURAPTg8QUCsARGMGjY2tmkXHYr2GJp36GqgKg7QOjPNidkzhIkj3P7gVzx8f0x58NuR4RhhZDSeO1z+gV74yd9y+fs/pXPkPFu2RaUWMotg8JJR9h0YYXZ2gu0tR55lGANWDEpAVXG+JFSBifEOwUMVAkFBvIBabD5NPtPh7HemaU8eJMvbLL6fK/O/HBmObzlGRuN5wvEX9fjP/h2v/uv/SNaeo29nUSuIGFQVVYPzHpNZrELVV4rcYlRRLwiCKIBiQ441gPeYEKkbVuM2ihDEUEmL0kxw6NwbtFotvK94sP5Q2R4lSL/NMM/6AEb4E3H2RT3x13/Lubd+iD1wnNA+SJ8CpzlBBbT+KgdfqapiEpVLNP4zapAQQxOjBoLE1wTFEBBCs5+AxXQmkPFZpg6f58zLbzN56Y2n+alH2IcYeRrPCU6+9l2+//N/R37mRVzRQX2O84Ctt4jeBkRjgQogzWOSfheV9FzcTsmS9yFo0GREhPQyggfUMjY5y8UXXkMfLfLuw3uqC++OvI1vKUaexnOA4nv/Wl/6wd9y8PRlXNaiHwSxg+djaDJkMIYef/z3+qH4t6ABNABq4z8EgmAUJIBz4ILgvKGYOMSJ869w/PwbMHZpREf/lmJkNPY7jr2mL3z/bzj64lv0sil6UuAEHGBykvcQv8bGkwggIYUmoTYCggQZ2g6aSIRoTFQFTZ4GySRYCzYz9NXSCwVjh85w+uUfkJ9+DezZkeH4FmIUnuxzZBde5tDF1yk7s/RDgR2bwHmlLB2FiV/fH/IuhNpQDB6L4QvUngYMch4CqAZEDV6BelMynLQYnzzM8Rfe4uzaFteqChZF6d4ahSrfIoyMxn7G1Dk9dO4y2dwJ+vkkfdPCigUcIrFsKqb+ClPOAppKSnxM0rN8QU5j8FbR09CUCB3KaQRwAYyxSNamChntg6c5/+ZP6UxMMf/hLNp/TXu9krLsIUHR4AjOo95Brxd3HjSSRVTjThXQEtzdkcF5zjAyGvsZWcH44eNk03PYsSlwUJUORGm3Cnq7XYTPext/CI/nNKI1ib8iAQ2KqKIIBsFmMa8hFpSc7X5JRovJ4xcYm5rhyInT+N0Ndnd7VFUf0Rj3+MoRnCO4ihACwXmccwRXpp8eocT31lVCn6qqqPol/X6fquzhnENdhYiABlRBQgBVQnAx7IoNNRD6sDnydp4WRkZjH8MePsSx02cJWYcygDWxChK8i01oUq8TwVAbAoNEt2LPvjSFIzBkMADCwGakLQkoghIQNBiMMXgftyUfowoOgseMH6NzehbRwLQaVH3j0RgF1OMrlzwc3xiPEAKiAVVPWfbQ4HDOUVUlrqzwVRWNhneUZYmoRz2E4PDO4X2FOkV8H+mvQTQ6WpY9yrLElT2qqkK9w3sf38sHgq/A++jphAChC34LNHlD/ZHX86dgZDT2MVrtNtIeI9icxL/CBI8EBQOK+cKOwzpM+fzjX9ChKCHmMkJKYNTbEhAVAposjo0RhYJiCQiYArIxQJA6qRpizsTUWVarMdseooegKhA8IeVW8glBBTJVOko0EHU4A1ghhTZxe3U+7seD8T10dwPrKrz3OF/iK4dzJcHH89Tvd+NB+2S01EUDEgIm9DF+AwldvFecK7WqKoIr8d5DiMcSQoiG2jl82Y8/vUdDifNdlAp8IKiPj/uABgeqiAENIYZmxLyREoDn10CNjMY+RmtijLzTxuZZrIgoZASU6FHE1ESsjjRsjBANhqgklz7uS+qkZu1xpEUpCgZFRSNP4zEPRepwJgRQQSUyRlVMbICrt5FklCRWX0SHSGbxaYzN0CCICU0FJ4T6AGMjXf24SUuq1+3uOQ4JCsFHQ2c6dGZOYNTEhaoefEiJ3ridlciCVfVIMgDRsMWFXBhBgk/OR20M0sJXxVcVqr4Jr9RXTXiFVlBtgfYJLhqM2kOKRkfpd3cIIYZr3lcE32ynIZSU1Q6KRzWkY3OoVukxh/ddYq2sj6dLoARuP1ODMzIa+xjt8TGKdgsxBvFgfSCTeJ9yyXyYII33MJwIrZOdNM8N/z7wRBoP47HwJRodjYZANYYmaQGGEF9CAPGDun3tIUiz3/S+QQc+TJDU/1Ibuiwdh0TvIy3W+vVZNj441trb0vozWsrSoql7F9GYe1HFBsAqwSevaXgnNno0qKFUA2KQ9CFEAlmi2wtQVf2GVUvDsI3nw6rH+l6Ta/E+ekH4MDA6rozeUW1QnMe5EucCPpQE7aHq0BCNXgguJpK1QrTEVV2UErSL87uU/W22d9Z1Y2Odrq7gWeVpey0jo7GPYbOCLM8JIkgI0WhkSgl4AScSCaFhYBE0MUHj7zQRR5PmCLVhEUyqt0qIoY6qEpKHIPhYrg3RaAiCeo2LXSMhTBSy0Meop2akNgzU2oEQ2xzEwBjF7b0KSk7AoEg0SiYawZq8VpU+5mtC7THUBi9VgWx8oxg6+cE2MSbC+yggIsmw1MZBg6JiCJqWQBgcY6TUp1DCmPg6JXl48Z+IELRAtY3BxPBJFBFFrWIkHmcrl2afqooRAQL4eLyk3I6k50MdyqhDtMKIJ2gfQgXap6p22NhYZXnlPutbd3mw8VtKWuq59tQMx8ho7GMEak6FxwTBasBqDBOcRsPhw+BLrHkZf4qnkQIGJMTchWgKOerng40OSF0eTd5Ivfjqhx6HSPJQTPxpAUwkjSlKEE0GKhqJ4AN+6OBkEK2gQTAmbisSKz3SGEgB0cZQ1eS0OicSq0AgRjBqozyADIU5JqaPjRSE4ZxMbTR0rwGpF32z+A2oGoIU8dTEjxMN2tD5cSE0hs5ArEqJRMOi4H0VjbLEZzMRVHwKJz0ZoKGPaokxylgn0La7jNt1tqfvkBUVazufsrFr1PF0NF5HRmMfo1/5WCUJITI8m/g83hidQovaVa95GY9VTYYXYUP2GoQe0d02zbbJ90gXd1ykSrpLp/4Uo5EUFgQqKaIUWNq/BmnyG3GRx+UyuEfH/0il1HZusGGw+EOznxTSqE35h/qzNcEQYJpU8KCOlCFJdEgEgtNmActwmJSMQp1TqY1Ek8NJ4Yn6eEySjJBJnlw0DoaAIxgz8HAkfj4xBlHFmJQ/CopPnlZovApPblJiKIRYA/OC1RxC6jh2HnyOaMBolGm01tOaOsLU1AzZ+CZ37zvKxZJt51S58cQNx8ho7GM0XAWfCFdJLyM2msVetS+jZ9QGofYpgMbgQDICiehVL5DGDa8X/GOPN/ve8z46SMxqTSaThqKuDPYXP4XEcEvBiKWsHCb4oT3WRkGSXVEk1M12Ji3MZCgEfDX4PMOfvQ7NjIl9NEaJ2dUweE6AVmq3IVVhJQySpaghq5MhoilMMwOPQ6L6WXyx7jmO2ruo9xU9pBROpn1aMQTtYzSgwaLBYhSCT4nkIBByTDor4pXKafNemXWcOvwmu90Nlh+ssOvWGT6TTwojo7GPUW5vU3Z3KZxHbEbloFuCz4VCFHyF1QzUJFIVMdGoGjU0YEhDY1BRqe+oquDdXoMQjUQkZUQX2QwZm5Ss1FjBEQLW1AsmXvSoiQYrGY0mugF8Stx6Bu6/wWBk0KHbHD+kfUXfP97ZkwtTL0zASv0pk3eQwgkN0hiGxojUL2IQvVW1J9aEHxJzFMSch68NjoKKJAOWqkQqTbgkdZiUmv2i0QGjdiiBapowyAQFcYhYJBhEM1QN+AwJFkJ635D+pc9tghn6HBP4rSOcnP4e7lSfj64vs/FnXF9/KUZGYx+j7G7ju9tYVVwVb1CmaOO0pJAQE2977rCDXpMag1JlWlT1Aki3/3rxxL9cTMpJHW7E500dXtRutYCm9CXq0wK3TaK19l7q9xUJcYGn8MLWVRmivkdzLOlOPkgKEL0LNLXsD3sUQwdO7QUMHhdCDJNCbTQ/77UP54CGnw2QDAdDFaSBRxafkGQU6sejQYv5IQaGIkRmbdPb05TABciQ4FL+xkCw0VMb9vgUZNj9G/pOCTm5OYRnh8n2EcY7B9nofu5jfu0YGY19DL/+iO3Vhxw4U6Kuj9gWNgd83dlqGq+ixudb5GWPMRlUOCKD01uXyEYDAlLcp0GwKZ5PC8FEhiiqaJNrsOlGKClIkT0hUWOUGk8iVW9g4EkwHCrVx9+8cO9na/5Mr4PGy/oyIzB4/VBuJ6mU1byWQdWpPj+Pn7ehwwmDzzi8z+Hj3JtDGhiAwY6icQhkGAKqtvGMSH6cAIiPIVp6VMVEDkud1dUcK+NMjB9kauIwq90L2n/CeY2R0djPuPeRPFq4q+de62HtNJUPEEyKo4XgPHa44pFQX7R1ci7+q6sq9aKI23txkHRD480y9Z2oSXc8m3IbWbxLhrjAYr5kKHehpglN4t/1T5/cjTpRaNBgBrmB+tj3GIy9HtNeYzFAvHvvfWJ4H3Xepv78X3ae9r6PNpT82jPQ9IEaqYGhY95zPEEbEt6AZJeOfWg/cQe1N2ZwWrcBSPJAYugXv+Xo+XmVZCw8vvaD1OAceJPRLqbptKeA4vMn6mvGyGjsc6zcvk7YfMTkoTm2vcNVBis29WLEikp9l2yYkNBUTYYVvAYXerzDBtEYc9cao4HUbyKoZhBMDA8SmUv8wMU2Qxd8TDXIYGEPu/G1pQmDJGJtBAwy5PonzyFVDRruSRhom+7hgaS12CRH6/fVgacEtYEb4njURqThjqQjbRa5GZxHHeyzDkOGreLAe6gX+5CnUp+Px6pX1OFLCOm8DSpL0rxvbTRSLw8hlrLD4x6USZ5HjjEFRloI+Z99jf25GBmNfY6de7dZv3uDgzNnyGSMygdMkeO9YiXby8ZMiHe5x+6sacHJkHERFUyw6WmhIW4lzVGpjUUyFKKxqVRgqDoiw+toKBavb6+atqqPJeUmiIZt2LX/svDiyzwNHnvt4z8Hi33w+s97GnslBRqDp6n6lMq0tac2CDtqL2co15FKq8PYs9/kiYQhr6X5pCHZ80Rvj+2CQ6FUQz6rDROAJ8syvLU4HyhdxdPQ1RoZjf2O+3e59+kHnDjzGtnUSURyrAj9fkWWZVHDc2hRmlAzNgdpwTrBKJpKfymnQVByn2FU8BqvVwvx9/SvZkZKqBOZGkukIfahGCxe9tyAGXjuUahYmju7DPIjjxkM0xitxzwAbT4Fe8qu6f0GicmBh1FvR/3Zh7yZhrQV05PpOCRFUEPvwyBBO7wfakNav62mtsHGWJjE6Rh4d/H8DTwVSeXcpirE0ONSdwjXx21SYlUGIVL6TmIaqIfKLt3eOrvdDWKfypPFSO5vv2PluqzeucHuxipCmRiXUPaqREnei+E7af13UwJ87PGa3Wl87NWwyZNoRjeSvAsCIhVKH+iDuPivvuMlL2RYPvCLjmXoifQTBovyTzsdX7TPL3ufmpD1B4/li16ne19b//65fSRV9+HXDfM0BrwX/cLnIYV8HmyIeQ3jNTFSTTIYBgkWqTVc1cZkNAbBUYZNHI/YLR+y3V/G0f+TP+dfipGn8Rxg/e5tFu/e4MyRM4TWGMFDkbfw1ZDX3oQFe6sOe7pN6+B5UL8gaIkAQS2qhqCRmi5GsUZwrsTjsAh5kYFXyr6LzFAxWKHJr4AMtDxEMWKTe6/JoMQKSS1cXB93k4+BJtHYdOGG2r03g5BBIxUdSHTxmsdBk0sYVFQez0nU76l7PJO4rSaeSNo+pMpU0CbpK/WJHjYWIb5vvf9Ydk37bEqudSii6bnIerVDKV/VNEKi5oyoiSGkZHgfvxibWxAIvsIUnvb4Lo+255lf+pBHu7dxT6EHZWQ0ngesr7C0dIeDO5vY4iBeIZNEQard1C+4kwoMKgFfUISMqQvFo6AZxhoKiYSnsgz0+13G2jmKxTlHr9dDgmBNgbUFoobgQly+UhsiSZyO2DtjUpJUhkKTurIDX5yqGM5p/EEP4Y94EtIYiL37/kM/YcCAlSEPrU687tl/w1ZN5zq93yAxu1fxpHm+/pvoZaSdMTArIdZHxGIlIzgAwVpL5R2KIzOCZH12+/M8WL3CytZ1/FOhdo2MxvOBtU9l/t5tPb2zxewhg6uUPBeC92BMurAHVYQmJg91wmyQrGwSd6nbVPOcyjnKXoX2K4xmWDGIUQpbUPZKbBZpzSYIaIGhwFdQ9gKtvMJopHar5DFONxKZoCqRJdrELfV7D0qzDYmreQ4GpKnBHf1zfIqhrtQYRu3dTprEZc3QZOhxBka0OY60fTP3ZS9ZrK7CkPY53PovMGh4AwalWWnyH4PnpQnnNAwbmKgzIFLT5OO/AFQ+kOdRka3qlxjrkJbBmy2WH13j7oPfsbp7jfAU+k5gZDSeG4SNNfpVD4xQBU9HM5xz2Czf42kMEnZfEvsP8RVUDDv9HpktaHVszKFVIN6TBY/gCb6LuFrpymANseLiLZl6xPUQqxjN8EFjmCMWI4YgBtWQSrfxPW1TIpXHjm3oGId+Np+peWxoQdcdqEPb76lWKA3PYrjkuWf/Omwa6pM9SJjW5V+jwl6jTCoHDwxHPTqift1wr8+gujJ4J1EQGewjbjKwpgL4EPASjUZk4pYUYwZbVKxtPWBx9SrLj67jn1KHK4yMxvOD1L9UaUAl3nUGrrkMdWnu9TgGPAQYrgSgqZ5hcoJG70VcoAiBQgPidqDcxnU32NhYZntjE1UhtxMU+TRj4wcZn5yicl2kk2FNh+ACwWeJuThQSR/kLPb+jC5/WrT6uFHYy4sY9jBqj6Puum1Cg/Q564Wesizp9XWH6t7wp0adl2DIgxg2UE2Z9TEeiepQj8jjn1drDyp9D7WnosNjJIZTMYN4KiSSRxCDaQumA0pF1vJ0ZoR+tcny4nUWH35KT5f/9Ovoa8DIaDwnGJubIyss/crHsQWGpDWhIHvZlMM/698fX4Q1cmtR71HnyHygIwFTbrP18C7ry7dZuPkBaysLbG9tIhiKPBqNmakjTMwdZ+bseToHDjExIeS2g4Y48kAAn3Qw9vJD6tLn457G4Lj3GLs9x/95j+PznsZensdevgZDRmhvUuVxBmptGGrW6WC/e4/DDvWkNJ5M3UafHmu6hOvt6veReK6iiYi9RIjG7zUxfa215HmUY/VSURSg7W0ebd5l4dE1HnXvEp6y/N/IaDwHmDz+sp69dJGp6WlKDZg8UckltlbtvRsON34NL8ZUX9ehi1sDzm1TGKGdgQ19dHudR4vXufXRL1m4+R479z6E3VvNRVkBO8Ba+wVl/BhTKz9j7uzrnDr9ApOTRxAzTiam0RDVMLwgPz8+sl5AjVEZwiCceJy/8VhOQx97Dew5L/Xn37vf4W7R+PznjE16v2HG6ud/Pv6+9d91Hmlv7qPZ1kcvq5FINQI2w5iAzTOMTbogeUpb2RDL3EWPR70lbj24yvzyZwR2edoYGY3nAGNHznPszCt0Jo7T95ZMIFRV1KM0NlUuas2NIZah1qrgpmE0G8BqSR5KMu0j2kW7O2wtr/Lw7k2W795gffEmG0ufwvJvvvwO1vtM6H3G5i+3dHdzgwkjZCcs2bjBZLV8n92zKMEQxNcKfbHcOVSTMMMLt0lopidTuDAgcX3+kIZzJY/zJD7HNJWQOmgHxrRmbdbl26aC0rxBaplPYeDw8TUJVh16bfocyS9CGYyHUCMEDVTGo0bJjGJyg2Q5pojeBemrVcD5HmL6ILusby5wf+Uqu9s34CnK/NUYGY39juM/0As//Y9MHn+HjXImVTIA9eQW+qIE9fFCVHBpjLwFMhW0KimKNi6Dfk8xWjJJl053CbN5j7UHt1l/cJd7t26xeO82/bVl2Lz+p1+I5e/EfdLT99dWeeGd/4WXvv+3uEzY2V5nrDNDYGA4vAcjJqpZOaXf65O3YoNV7Jjlc+FLarxtFmVdtmyMB0PuvhLlCQFqPY0mwehSxWIoDMJgfBQ3Mg2/o/ZiTCOc05DXlD29MFYAxyDh2byfEKngA4X1kA5aB9YJtZB1CtQaiswgmTQq7GUT3gSscSAVmQl412f1/k1Wlt4D3n3qBgNGRmN/48BZPf7mj+icuIy2ZyFdqDbdEbUe7T5Eea5LefEiDxgTqyxVsFFcp9xkbeUGdz75BWs3f8fC9Q9xvQ2q1a8QF/euCmvT+vDOBxw4dpLZEy8x3upgBJyrgAwfQNNwWCeGXIRWq0VIjVuDRrPBHTve0VOiNLDXA6kTko32x5BLEHRP0rPR+IwPpMclSQrYuG0YeDjx/YY8jrR9I4iT9hMUcAErEsvUqbqs6bkQ3KDYbAQxYLAYG48tWIXCEKyiJnoVNctf0qFkRlEcVjze77Cxfo9Hqzcp+0t/8df1VTEyGvsY+dELvPTOjzlw4iQhB6roQUT3PovhR+I8BCyiig0ltqGAGzAZZRlrqW3tsbt6h9vv/RO33v07wr2Pv76pYpu/kPVbLV2YPMBYNsn0kRfwKuTW4pO+JcZGw+ECzoSUyIW9OYu6srA3ZyFDHsjePEJa5E3PSQoEdKjnAwYhQ8ONqEMdiNroQ4pcYShHlHpNainP+ljr54zU1ZDo5QQS/0WVYGJSEyMYazEGrDVRlFhirqKuipGOcRA/xYMLIW5nBHZ215hfusrSw0+AP8Mb/JoxMhr7FXOv6umX32Hu3Eu4iSkqjQpPGVm6+2VR+FdjLiN6GUoeSiT4qDWJpcLiVWlrH7d1nwef/Iobv/k7uPFfv/6LbvkfZPnaIT0weYyxfBY7dQyMJMVzQ2YsqFB5oQqxka3p3XismrG36qFNODLc1BY3NGicBzAoc4Yhg9HkH6QJN2TIC6mrSnVlpfFGdNDlijbtbRiphZFjYGSswXuP93EqW5BY1TKZxWYGk2cggrVA/PjUEy6Bgdq7muEYCGM0ao+UAWsNQo/NzXkWlj5gZ/f21/7V/TkYGY19iqkzr3H+O3+FThyibwsqKeNFqApeULUNT8FInCFCcEhQMp8UuMRQ+opObpGtDRY//jU3fv1fn4zBSOgvfsT9zhwTE8c40pqErMD5GFIFUYwUaRyBbWY418Zj2MMYiNbEP5swo95GTROaSOrYrcOWJr9Qd/aqGbyWIX5LqH0Hhnpjam+kqXuk/3vQ2GMj1ItdE29G8eIJVhFryPIMW+SYDBpVYNOkWVBiJ3FMtHoGGh4BY2IoIiStUmPIbaDbXWV55RNWHz1bLwNGRmNfojjzV3rujR9x9NLrbBdjVBhElMyAdVEKH83ieIOGPg21Ax1qUR1VMgKZr9hYvMat3/8Pdq78H0/2gnMfy+qdAzo5e5qJ6Vkmj5wny1px7GHlqFRjHVGyOBOoGRhUMyn3VjsaGrZ+3ojEZEgYyofQ5EMk1ESqZDB8HYJoEw/ExZ+OO+1XhrgZaPQaCFFyT5NSuSGkUQ0BR0CMYHNLkWWYzCJZmthm4pgJ0ldTG41aJ5igZDZ2FGu9Uf3ZQqy1FNbi/Rara9e5//Aj+tWvn6nBgJHR2H848paef+evOf/2T/BjM2jWQr3Hqk8qXYpK7EnAk7JngyYrj4l6TxIv9kIcGwufcff9f2Tts3efykcIvX+RpRszmo8VXGy3yCaOk+dj9NOQZlELwRAqsMYO9CMgToBKOYxB3iHpY+jnGa/UuYvG26j1LMygoU0bpyQamqbkWx9weqo2HHu4GCFVPiCIB5uK25kgYrEmQ6zBWouxJiWTkoHQ2kDooBVNY3k8DoCK1kIlIMYkTktSdA+KUILtsrV9h7vzv2N55ZMn+K396RgZjf2GI2c5/sYPOHj+Mg92fWyFDo6YgKtp0zFTH5KKVr3ogiqeIg5zVshEyfrrrFx/j/kr/wzL7z+1u1T34X+Wu5/lOn3wMJNHS8ZmT+MlJ6hgrGBqLc3aKDCUoEyoPY20DqNr/1iCFMxjnsag96aumsQqSEqIptCjLt0Oz3SpE6iaSheatDkVj4oSTEyU2sxgJEMs2MI2kxUq3XvcQCwvpw3qEETr3AkhkvQURB2GDKP1sO+orRjCGptbt3m4fIXS/QHezFPEyGjsJ1z8G337P/w/OPDimyzt9inGpnCVoyU2udvpDiaRsoWB4JTxltDbruKdql3gAjHr3ttl7e4n3P7dP1Be++9P/YLr3vv/ypWsrRfeLDk+Po7PZ6nIKbCxbNwHadFokJLieFWNfSwhkEkWjUWjsDWk4AWNp2Fk4GmQEp4aoiZI3bQWUkJWJFYjDCBVNBwhWSVBCOoJong8PoQ4cKnIyHKDzSwpn4sKca5uHU5o7FCtjbiqNsYRgKBpwNXA8FnqoVAe55WCgsKCqqWqunQmtvnsxj/yYPX3T/Or+4MYGY39guNv64l3fs70uVcoxw4QMHiJ6kw2pOll9fyROhiXWMLb3q7INTA+1majhH4FRQb97WVuffjP7C49O7d29+EHzN+cQjvjHDj+Jln7KOIUrYSWidWJMHRnru/BIsN9KwOa+R7Lp8N9HQNehTLITahnYFyGOkodsXSdMcghBHycnyshMjZNFDvCgmQWk4FacDXhlpiz8E0+hGZmbDPsKGFYR8QMhUNRwsAhouTGQhVFkIx6CNvcuvNbNrY+hafcX/KHMDIa+wTTF17l5e//NZ1jZ9gxRRrP52h9wbbDFUdroQyBVmEpMii3uuTW0vIlC3c+4e7v/ycsPhvmIAA7H8vaLdG+ybmYjXPkxAEyiaMYBXBJnUpqsoIMUoJSc6gZCkfqbGL9RM38FGnEimtORkyu+mSAkoBy/d9QpSZSM0JjLMSaWAWxULQsIZVLSfyKZKMIRIMRkrEIBmyo9TDqjxPDp5qQR600plE6WMTgPRSZNNIoSsCHTXZ25/nk039kbf3GE/yC/nyMjMY+gJz+iZ559R2mj56iawr63pOb6MLHvN1QYm5YFzTEaWftdgvE0e92Kfwu41lGd+Uud9//B3iwDy64zauye3NS7+WztHWaublLFMbi1WJknIAZTJuvF/JQD8ne+SM0j5u0IGPu4vM5D0jVD+IGTfesxLGS3sSKCKKIFbA2krAyiYlOSzQYZpA/HSQ3A544NGr4OONb6WB+LClvMpyUTeFVPYu3/hxxxoknzyt2thdZevAhSw+u4PXWvvEyYGQ0nj2mXtSLP/hXXHj9e0g+hlaOTtZJwk+GSgVrAiaV4EiNUnVcXznoFNDvldiqy4GiIqzf4/7v/571D/8Bdp6OmtMfxcavZO2Tjt7qGfKXdjly9BRFcYBAG0sRF6Yq6get8TUPRRkwPptyaR2ShIE3AoOEamM8bDRGIdRGI20ngmaBIIIxFmtjqdTYAVNTZBB61G0oTbJUNfFLkkHzA86HTWI8NVekpvgPmKjpbgCgQmYKJHic65GbCrEbrG9d4c7iL6n0GXqJX4KR0XjGyF54k5Pf+TFzJ8+xVgpZSt51PbigYGzsQVCPIaTGKouqTe5tdI8r5+nkholql+uf/pJ77/7/YWl/ZNsbbN5k+bownkGn9X0OHB1DnYszaYc8qJqI9bjn8Hk9C2IDWROp1OXMgdEIYbDIQyrFGBOrGsEabKsAEz0NE7v54+T1muAVLVZ8g6FKDxCTmClG0dS/YoezLo1XMXwSkgcl8dhDgFYG3gkaHNhNtrZusfTwPZZXrnxNJ/7rxchoPEO0Tr2il3/0N4wdP483BVZK2kFxlaMbMoIYKgEjgmiI9HAicSnOAAVMvMjVZnjf5eHtj7j57t+z9uF/218GA4C7wu5dFm4ZNUXB8TDB2Ow4Qdpk1jaJyvBY6RUGi7WeQhbv3QMlMKhb6xO3Q3RQ2ky0lUwMxqRuUmNQA6EN3g76WYzWXkV8n3qMQ3yuTrbWVqrOVZjGixGNhlyCNO7JsPJ6qD0ojaLLtdiyOoMh4MImiw8/4s7Cr/H6+334HY6MxjPF9IkXuPTWT+h1Ztnqe3KTY4ziXIUxBmtjabVOppnEPVAJTWztnNKygRae/voD7nz8Lguf/u4Zf7I/jGrt7+TejVy9GF6cPIixgjCOmBa+VgARnxZrwHiLDXaIhBUrHPVEdh2ilIcUQgQJICHuy4CxFptHEpZNOhXeQmUjnyzud+AZGAExgkZprTS7lqYLtqncBA+1CRvqyI0YGIzPVX5q4lpDWa/Is4oqrLOyeo2NjX2Qi/oSjIzGM0J2+cf6w//w/6I9dZJgJ1Dt4aTEq8MbiarUHnLi3WyMNpaSylWIsZCDq6BlIat2yfur3PnsXa786n/A6rPtTfhTUC3/F7mzvaSdXDj/0r9jov0Sm32FzEBmKattchOTnVlokVXSUMkDmgZXg0jUJA0h4DTWOWMFJBqLsU4bNSn8SeFH3bAeJ8OZKIxTJysZUMgfhw55CTFt6VEqBA9YMFnqC5IhASCIuZbaaCgSTGMARcAHR6tT4dwjFpY+4v6DT4Gvqfv4CWBkNJ4FDlzUi2//NWPHLuGkBRpHHHoyAj6N/aUp1VkPwVcQPKKCJ+A9WAP9zXWmW57N5Xvc+/g9WJ5/xh/uz0D3fXlw55x2Ogc5dmqKrHMKpzGHE0z0FmziazTqWkQPIFZQ4/xSEUEyG7tBbdK1SNo/LgvJYNS3/8GsgUjGMo2SGAyYokJtROq1W8v9DMn+qKLimpymSbFQU0SJhxGrM+pAopL7IKCCvACoENlkc2eBhaVP2Npa/Kpn9oliZDSeASZffJNLb/2EfOogXfEgLhK3VPCaoyYmOkUSTdyCqicQIn05COphzIKIp1p/yM0Pf8vilfdhc59US/5ErM1/hLTm6Eyc5cj4ISTLcV7JTA4acMmNdylZqpQE4sAgEYHcYoxgbYZkgrWpVCpDZVKGKtVDIswoGD8QJ64TnUAz16RGsx8dGIzIsUuhoxo0jTRAGDTCNS1qtWuh0YskA0LsPzE9+tUayw9vcW/hM3zY/lrP8deNkdF42jj7A337r/49Y0fOshNyJIuCsT5YgloCtiELWQUNAZNJvGWJiRoLHowLWA0c6lg++O3vufqbf4ZHz07N6S+GvyaPlo/o1todjp58jTybQq0QsjHKXp9gBReiwJBIwItHTCCkvERR5BixiDGxFT01i9WDy2oiVn1rH+5zsRishkE5lyGdi8cOc1jDY5CfMKnL2BD1Q9ObxERUzMmIJ7UXJk8kJD0OBXGUbpss32Ft/RbzSx/RLx8CN/e14R8ZjaeJ6fN6/q2/4uQr36c/cYRusHQS0ciREbCoRm6ATV6G9yXeRGk61UDwMQFYBE/e32Xn0XWu/+5/EG58COXTF5n9WrC7wOb6PcruKu3WIbI8I1ihtBl5J0Midw0NYCSPCmBJFSvL8mY3nkS+GjYQyV0bOBcm6nYmvrlVMCEk+njMWwxXZOqyb1PqTUpdzYxczaKQUBjQxId1QKPLMRT/QGytF49IH9VNnFtl/v6HzC9dATaf3Hn+mjAyGk8Rh159hxe++zP62QyhmEGdI0gv6imI4IamgRFimbCVZ3jfx6duSXWOMVtgg8evLfPhP/4XHnz6Oyif3oStrx39W9Lrb2q/3CEPfULIEhMzQ3IwHsjTnTrNA1ETS7RN63mQyMOo2egiMRRJb1FXPgYdpgPsabUXSb09w96Faf4/GBtJDFGG5p403bYoQiyRi6SRlrVzoT6S9MSBdDFmk9W1GywufkDwC8Bn+/57HBmNpwRz/sf60vf/lqmTl9mihQ1CwDYy/kFtzOYnfc+MWGLN8wxX9hFTYK2gZUlLAuXOCvOfvsuVX/53WN5/rME/CxMXtTU2Tt4qkMw2TWBSxMYyMbXnFQV7oo5nkukzmlrXZY8xiHkMGbgdYTAlXoY28uojA1QNniQILKnWITX/I7E9U2ObVUml1lqXdFApqQ2LaAxLRON3LMEmdXFJ1RaHSJ9ub4k7t99l5dE14OPn4nscGY2nhJe+/zeceOm7VO2D5O1pulVyU80gno6LgCagNupQZ3B9T2FtNBqqiNulu3qPex//Bl34x+fiQvuDmDjKxPRh2hPTZK02WsYGMlsYqsojJi5CqyHph0Sd1MbLSLf5mJgcEikOMXHctK1rTdhK0+tFUDH4FFv4WpRYoiFvVNFSpCHJw7CB2J1GTdsYkLiiTF9MfkpjREyjji4a9TcEj5FdtrfnmV/8EMLTHa34VTAyGk8Bh374v+nZN39Ka+4ca12D7zsmxnKqbiCKxxlCojdDrA4arRAUX3nGO1N4D1WvZJyK7qMlbl75Z+599M/P9oN9HZh6SY+dfZsTZ18na89QhsisCuLxVSqn1iXLkISIg0bvXmJvR4xfanq5JgchSQFrZNSitV5oCjtUosiOGcpjCIjYAaNTIZQxTiwErEqirYeo7SFChYvSfzUTlZjTEKJR8pXQyg1VH1ypjI0ZjLX0+7uU1SM+/vSXwDr7qfX9j2FkNJ4w2hd/pJe++3OmTl5m3Wdo1qbTNrEyArGPRLLIOJQ6JnbJhQXRjFApGmAiMxT9PrdufcSdj38D93/13FxoX4Ts2Dt69pWfc+jYdxmbOUOwnSiHZyGzBjWB4Bya1K4iwoBLESJXxQ8v+qEYxTQGYuApoIpNrw8IaiO1u1EtTxVScdF4F9ZEikcV2bdUqQcIaYbbxSZ3MwhP0r+kV06ooJVDLkIInsI6dnWL+fkrrK3fptt98DRP+1fGyGg8YZx+40ecff1H2OnjbG5WZO3oTpTdXTJjE6ErdUdqAC1jiU4CaBZd6ODIvdKxnp21eW5f+SXb156O3ufXiuyCks1gxg5y8uQLzJ26zPHLb2I6x8jtUSrfiureNoZuPvhIsDJDScgmNzGUME7/BYYMRfIyaHIOMqB+N+EfKfkcSVtGk5fnIdFnKCyEEkKp+LKPBo9ByfMcJPWZiDTDnOSx8q4xBl9CUUTCaL/sEsIWO915rt9+l52defYz+/OLMDIaTxDyws/19Os/gqkjbHkhmDyK5vQ92u3RmZrG1cpSQbHiQFy6qE2MfY2AegpT0d9Y4c6VXzP/yW9ga3/X8vcgP69MneLAoYscOnSBg7NnOTR3hvHZI1SdMZwdp+9buBBdBYMSgkO9Q8jTsOQQPY5Uiq4rHBAXaiCGDanFbI9hqFE3xNXGw0tMbprUNGZTA5qpwMSvguAguIArK7x3sTOmyLBYrLEErQZvMHQ8RmL+JTOCk6jnGoWFdtjaXWBp+UMernzEs5jF+lUxMhpPCObiT/Xi9/8Vs+deZ1vG6DsoWlkSXgnYVosgBke81jJCIhrF14dgU8eCkklJS3s8XLjKx7/9e/T2Pz0fF9rky5odusDswQscPf4SB+cuMjt9hrGxOUTbdL1ShYpgMowYrK2rHh6jgqHAh6TOLT4xQQNqQpTTo05ExvQjxPwEDMhZNQ182GBg0rR2VfLUB1J7FjaOTUVKoFJ8VUVtUR+wAibPyMSCtWhSFqvDpZoFqs30ZyVoic1ygheM9VjTZe3BNeYXfoP6Zz+O4C/ByGg8CUye1qMvvsWl7/0NfvIwVeggNiot+NLTynNyydjt9/F5O7naAaMB4xUlIxhLwJAZxWaB3toS87feZ/P2B8/60/1xzLylY8cuMTV7mpOnXmP28AWmZk5hzASEDpVtQ4AKRbMMjEUShdOojx6GCEYzgsujAagXKX28CWAdGrKGmTlMyBpGEJp8EemnBE0q4EqOicSxMiCVQhl/p1TEKRJCNPQicaK7MXHcABIFhUll1D25FIEQMKIEV5FbizEWY3p0eyssLX/Mw0cfP8Ev4MliZDSeACYuvs6pV95h4tgFNkOLkEXti1BpXBRBqdLIRFJ/hAkaZ7CGDMXiJItlwNBH+5ss3HifT678Mzz6ZN/encyBt3Rm7jJHz77OibNvMnXoPGonscUkagt6lacKJcZU5LmBwiIahyaFxMxUdbFSogaVHPGJaiEQ6syjeIzxcR6qswxSjxF7xjYSnQsLUQUs9aIHApkHX/axpaJlIFRKVgnWCeIEcVGDI0hAbdTzFGtRG0PHkHIlw1PdYHAoqj6ObQxdsqxD5TZZWPqM+cWrhN7Vffs9/jGMjMbXjdPf1xOv/IC5c6/Rt+OUIcOaGBtblCK3VFWFD4HOeEHX0xABAh4Rk4Y5x/ja9bbRjWXuXf0N1Xv/ef9daBOXtZg4xqHDZzl68kUOHXmBiYNnse0jqJ1E7Bg9FysPNje02gVe+vEc+BKrk43ylSTpPbyiWMLQJLQYBtQzDgeoPYkaTcPZkLaFEY0J1STBh1eMF6wX/GaJqZTgFPEBfIYoZGrjvKkQUilWUCPYJN4TW00kCnIk1BWTyM+InmWRt+ntbCP5DmV5n/mld1l7tD8Vuf5UjIzG14nitM688D1e/+v/O9XkMXZDFH5RHw2GGMV7n1zcjLIXtR/aLcGFkjJ0aeVjaPB4VzFuLG0D/+2//WeW/vm/P+tPtxdTL+rU4QuceulvGDtwjkMHjzM2NovNJ8B0CNqiStwFMQaDJ4RAr4rduqKCoRWVcHx0750KhjyK6gAYSQteCMGlEQGxO7QW9DWJCRqCT/J/iohtFm8uYLwQiS4eypC8ilgVafkM4+tZKXWyMs4y0aTbYZJ+qM1NahwkDTSKE9NyK6hRnHOEEAddW9OBoJQ7yli7g/ol7s7/C0sP/pHngSr+hzAyGl8nzrzAS2//BBk/xK632LE2LsXqQzKTDMfgUgXiuF+LSk4ZAkYdxgXwfa5/8C9s3f0M1vdBWJKf1+LQGY6euMTJMy8wc+wyZeskpn2IrNWhIqMMMeRAs+Q91Hfi1Aruo5sPJMKV7GkpjZyqwUzToL5ZyNEYJKFgDYQQaeAQPYGoXWEbWrcNoA40GQypfMxdOCFzBvGGTPMhab50WKk/JHK5BAzYDMSCSKSaa11bDUIVAkiFqMdIHo/RmyZZa03Jwv2r3L7zS3a7v3323+NXxMhofF2YeUFfevMdzl9+mSrLsCGPGXoGjUygNOrUGhdALoJ48DIOFFFo1igmbNFbucMHv/gvbC1cfZafDKYu6+TRyxw78ybHTrzBzMGLFO05vGlRtOIsU5Aojks90d7EppEkRGGE5EEMpPsRjcpXJpDkyCO5SyRVQQyIjcOUIMnbpMqESSVWJxiR2JuS+BKaKiHBgziN5LgqoKVHfORhWE1T2Wu1vljI3StCDBhrosBPloGVIe3R2PlqbXRiJAjGGqwocWB07HExWZ/Sr3Bv6XfMP3z/6X5vTwgjo/E14egrb/Py2z/AtsbpBuiMj7G565HM7m2kUiUkfXwJ0X32Pmb1g8+xRulkSm/zAfMf/4Kta+/Cyu+e/t1p4oLK2CwHjp7j0LEXOHr6VaYOXaQ9fhLsLGXIKSvA9+PqJCUFJccmH34wbpGmdXzP7yJNx6fWA0aAZtBIKpgGBO/rZrUY5okIVkz814w1iAxSKsVVISYyNUOcgSqLyc0Q58haY6L5qpl1cQ8D9qkYgjiyLIvaoknTxCuEesiKhMjVCLHka4hJ2qBR0i+zAdU1lleusrT8AfDhc+9lwMhofC3IL/8rffNH/4q505fYkjZkLYJXbE08YKjfgTTImRgeB1emPogcUSgQpLfJyu0P+ORX/xVWnnItf+xtnZi7yJGTLzNz5AJHTl2kPXmYbHyarho2vAAeW2RQCPSLhqWZmbqJSwgemlml0OQBINGrFdDUsEesGEUWp00Cv6T2dPZoVUBkiSKKeoMNGaaK2wQX0MqjzmN8QCrBEgcqG5dByFKjWjQwBsGHOvSh8TLqFnkRSdPgY1J0b00mwnmfJqYZQvCY4FEfMCYD02Vz6zbXbv8jD9e+GV4GjIzGV8eB1/SF7/0Nxy+/Sd90kNYERgrWd3bpjI9TVTp0wafmKEzTHVlqhRVDbnPEQFHtsrb0GfNXf8XOlf/j6RiMg+d1/MBZpg+cZ2b2MrNzL3Dg0CVaU0cJtoMUbSoDnoBYjxootYcrAxPZOKGKQ41cGIgFxQUnhKCYWnLbgKQW9GReGuGbeG6iDgUqmKRTYTS2xRuxUfcQiwaHdxXiDKGnqBPUedS5yI8IinghUxtzGcHunQ6vJNq5R5KYaLRNcQyBSsxfGGsxuUWMpnZ5jedAaRThNUAmNqqHhTg7xeQeCY6g6yyvfsjC/d8Squc7+TmMkdH4ijj05o8598aP8eNz9ClQcnpOsSaPMTXDF2tMntU/g0CVCYqnEEfhS/or17n13t9z7+Mn3MHafkFlchadPMLhM5c5c/ZVDs6epTN5POYrtEOvMpFk5sG7CsUjWawaWDyW2FehPhGcTEwcBnWR4WmEEMo4bqAmWdVt6EETo7IeZxZSF2qaB6I1g1PAp5JokNRRqphg4rChvkNcrFCJtzH0CFHGz4QoFlp3rNbfQ5y7Gsl0g0JpnXhV1MSGOZNLHPosEie0iaY+lYESl0jyKAOAYCQns5ayWmZr9wbzS++ys3v9yX6XTxkjo/FVMPeSvvrDv2Hy5CW6pkCKcXad0isdB+fGWV1zZCZWD2qJuFBXDoixejBRmctWW9BbZ3vhKgtX/onq9hOajjb2hjJxlENHTjN99CwTx19g+shZpqcO4ynY0YI+BcFYelKR2ZhgzEyGSJyb4J0Hb2L+IvVw2MisjpUFFXyoYs5CQpOpcIQmfSBJN0TIkt5E1PMzlAgOg8NIwFUloe9x3ZKqH5le1hQUtsBIG+/HMaGFBosJNpZP1TfGxkqeVLkSzTsWPBBcnGtSfxcKQZMnAXHsgbWIBa+BQIhzX03NA0mfA+KOvcGVAZMZgjp2d5dYXfuUpQe/I/g73xgvA0ZG4yvh7Z//L5x68Tts0kFMgXoL1lC0LRsbSewllfJEbGw+S81LtZZlZQuoKg6YQLVyj+u//js2P/z/ff0X2fgLysxpZo+9xsmzrzN35ALFxBxm8jDOFpSYqBxmJNK02cV2IGiZOBQZxhnE5VifNxMBgiQdkJAWnoAxGargUmxf062NtFANkauCkJsialQ4cFWFoU/RCrSykt7uQ3Y3lll9uEBve4udjS2qXoWvoJOPMXfwKAcOnqUzfpGqrwRfINpOw6AtmhrJYgI1JWbrc6EB1SqyUDFRWcvQdMvaDGxuyVtCFTSVV+O5Gc5sGCAvCnq7fdpZm1bL4LpQZJ7KPeTqJ39Ht/cMkthPGCOj8Rfi0s//n3rypbcp83Fc1oncA7EEHeJgiJBZg3NxoUAcC2jrhQZ0K+iQ0V1d5sHH77F682tmCx5+XcdmTzJ7/BIHj73EzNwlxqdPk7dmCYxRYvGagYR0l63noGrq4oq70RCFbki6VBANX2aJbWQhNMYhpMynkWww0DnEbKMxObnJwSu+hCzE9vOJsYyq32dr9S53Hl5n+eGnbK8vsr58j6q3Df0yJkRDRp63Wb8/y8T0ec5c+DeMjV2kUxxDS8VVka8hkuGCw0os52oal5iOHJWh5kCSaLMoxgpkcX6KJ+Yuwp5lH8cnxkHcgapf0s4zgqsgwPhYTuW3WHrwEeub177e73KfYGQ0/hKc/Yle/v7fcuj8a6xlHYJt7Ul0QkwI1o0lojXjMP4zVhqR6kKh5WFz8R43P3yPnflffvU709gr2j50kvEDR7nw8luMHzjGxNxJ8onDqJ2koqDrM1wVKMRg1dFIh/lErUjMSzEhlYkjockaBwje2DjJ3juoQxCJi9Mm4yiAD5CnOSRVD1wVy5iFlciS9ZuUu+usb99nbfUm95c+5sHih1Trd2D383dpBco+LPdhY+NtlWycEydh/OA4QWZQV8SPYiUNC9AoaCSmCZM0VWCSiUPVEDSgmWAyaZifruaEDHsXSiyvJh6J9xWtsRa729sojqw1weqDWywsvofz73/jvAwYGY0/H7Ov66nXfsjUmZco2wfRbIwgsfbfqFIPsQt9YoRaawd9EV7R4DDOMSmW/uo97l//iLV7X/HONPWGTh2+yJHTL3H45AtMzBxj7MAxpDOBFBOUauj7qAsmxmJbFkqaeag2SBT0FVJVo14zvpGwc1LFLk/1sWQcKnI1iM2SSHLcR3Cx3Kqlh9ySAxkVNofcOqp+L4Ug11h9eI2l+WusProNW4vQfwj6x4VpSvdbWZo/oq1iisniIGNFC2siQS7abgsSE7HiLT4J7URORUp+CmmqXYh098JiC8EbJQyYX40+KMMJWhSDxVceYxzIDts7D7mz+GuW1z75at/lPsbIaPw5aJ/Wyctv8uI7PyNMHqcnE1RJUxK0yf7Hqz1elM57sixu431KiKJkAm2pMNtLPPz0F9y9+ktYv/+XHdfR7+jM8cucOP0ax46/yuSh8xRjhwhmnK4DsW2CChWBYHyc6kUgBMVqO1KeaaqieK2l6tKcD9FErSY5TzGjqCZ6FIOKkI9hQAiNlmYe+pgyUuMzcbSN4nvbPJi/w+LC+6yt/IqN9etUD3//F92Vu7s3eLg0zWRxmBOHZ8mzacp+PNemMFELQyHKBMYhzSaYOAM2zZwJkR6CzQTJ0lhHieXiyOlIHxwaNfNadFhRut0dWu2AsMOdO7/gxu2/p3RPKJG9DzAyGn8O5o5z/pU3OXbhVTbNNGU2jqaOVCCWDfeM8zMYYxNxKPVAaMywWwm02WVr/n1Wrv4j5d33ofoz1LgOv60yOcfc6YscmDvN0RMXmT5whrwzB2aaHh285GgRQ4MQQL0Sp4JFT8dVIbIrTSJRxY+QyFdxzql4TRWHoVwNULd6ioD6WJIUEaxJdOrMY6nIWwHjd+jvrLC+Ms/Gw7usrSyw+nCBjfVb+K2vqqb+qWyst3WlfZ7Z8UvMTJ3EyCD5WnNBJIUhUaXLYLQF4qlEEQtZakgzNbNc/efeqamYaNINFYOx0PceK312+w9ZePgua5vfHCLXF2FkNP5EZEde0EvfeZvzL79ByCfpmwlcao4yaGxW0qheG+d6pl4TiSSlpg9FBFVPv9dFt+6z9MF/Z+PG/4CtP1H2bfKi5kcvc/Ls6xw8+gKHz7+JHZvFtjq4kLHpBB8EySMRq/IeEwwaAnjBqqWwhlxyfAE7vodmJRIshCgWM9DUrOuTNuqXSkwAqtQ0bxKpKy7I2FPi0VDRL7fAbeP7K/juMmsPrnP/7kfcv/cRfuPrFUR2/n3Z2nhdd7dXmO70sWQ4BXzASySXqXoMkRFqAphgCEZQW4K1ZEUczBQrtbHEGkRTg/sAMvwzlWmzzLK9s8HdxY9YWv4d+32s4lfFyGj8iegcOsOl7/yUw2de4UFlIctIkpZDIQkxjk/zwn2iGtZTxa1AywSM67GxtUx1/xbzn/yatYU/bjAmTn5PZ09e5NCZVzhw7BKT0yeR4hAhP4Kz4wSVOCS5Y8gyg1dPz1WoKLk1GHJSUgKXRhw6Bc1IDE2fJpalHgozyM1ISvqZUFO9aXKDUdUqdolZraDcpbezwvb6At2tJdYf3mD94S3WH16DrSdHiS/dI7r9FSq3i83GMSoE77E2zR/RLIYlNTVdQMXhqbAWTJY1/XUhRElBKyb1z5hkeJIHonG4laAEv4O1WzxYu8XNW++yufYv32iDASOj8SchP/OOvvXv/nemLr7Ng7KAziTioxR+MIKX+mL0jZsPqesyh50dx1iR0SlAtzfJwzbZ2m2u/f6feHD9S6ajTbyqTBziwNHTHDlxgRPnX2Rq7iRZ+wCad1AKVMcxOkkmBh/AJL0OFyoCBisWj+BC5IvE1nHAQjA+hhghT9yGyGMwScPCe4/zFVlqChM1uCBkqU/Dewj9isL2yeii2qXafcT68i0W7n7I/YWrdDfmYWOJp3Hn3S1X2K0eUOkO6uZSfilg66lqiVuixI/tpUswffKJAnIbKy4knkmatUpQgstoFxaCwVU9bBYwUhAqg2hJLlsQ7rOy/B4PviFdrH8MI6PxJ+DCd37K3MXv0G/N4u0EYg02hEgv3sv3ARJdXKIydq8bY33vPf2yRyv0kP4uywu3uP7hF+h9zryuY4fPMHv8EhNzp5k7fpHW9CE6k4fR1jgVrTgoWkyiVNNMGgtpvGAUuImS/tlwaB4Uk+KkOAbA0zJxgHKl4JzHhwCZwRYZeR4nmYkLhLIPlUPFUJiMtgGKXXK/zvrKHZbmr/Pw/nU2Vm/T27gH/RUon16/RaU9nO4SNNLdqenmNuYgJKQQMXkYWIdmHnLADiKxeG6INFfiOEV8zIkYExWIAxVIHv/WLR6tXuPR6qfEoUfffIyMxh/B2Kv/Vi+9/RPaB0+yoTnYPLr3zmHyLA4AlhjbR7pyTdiIehJFEekPJsSmK983LDx4wPWb87DWhfyy0rJwYI6ZQyeYO3mRo2deYPboeVpTcwQ7gTcFnoIq2NgRaqLBcFIiZjuFQTnqC6zGrk6r8Z+GmGsRFAkAJqplpU5b50qsFTLJ0hDqONynqlLc7kpaJlCYirzok/tdqLpUuxuU3TVuzX/IysotFu/dIqwvQrUJevvpu+jqCS51wBqPiqlTtoNNtO4a0ZisLXKyLIv8Eokhm6KpTT+26luicrkxDjE+KoipYlQRHP1qnbvzH7N0/ybQf+of+1lgZDT+ALIz39XXf/rvmTx5mU2X088K2hn0dxyZumgwUsJQFDTUpK40tQtAoCwBV1EEz/ryKh++9xFbd+7B4bPY6Zc4fPgwR0+c48Dh44zPHKY1MYvaCXa94Gjjg0VDRhCDkWiogoLXdLELSQovzgaxahrDIcE2zVoxR2EaEVwlYC1goxJWrPbEYxaN1ZZMAjk9crcJ5Sq7m0tsLN9h9f5dtjYWWbj3IfTXoX/rmcfyIpK0NiAychsxTxKXLhaLjWIywWQZRqLKedCQZAsU1Dbs0TgcOqBUGKIodKSFObzusrx6k/vLn1GyyvM29Ogvxcho/AGceeMnnHvrr/CTx9h1OabVwhMvziIvCCGk8mTs0ISaEQqxtAm+SgN3fMAE6PYqdnoVzB7j7IVzHD95nskDB5mYmsXkLUpyeppRBkM/KFneJpAhJtGgQ90URvQuQgFoqg4EDBUiSRhXJVZEkkYFxEhKxOGNAwK5sXivuMoRAuTSIsssRh3Bd2nbkt7mIqvLN1h78Cnr96+xsXKbrbV56H26bxaJYGNzmiQLWidsMVGKABoDazOLZBoNJhYNsYEtTmNLS6LW/rDgfEluoh6pB3JrkVCy23vEnfn3Wd+6zfMy8f3rwMhofAnMpZ/r6Ve/j04epWcnKFoZCvR6kBmQzFJ2S8RkjRq2NFnQSBvXdLsqCsC1UAcHZo/wyhvfpfA9jh87gbSmoRgj5G1Kr/RdFOQhy+i0hCrNFE1pkpjN9yFpT5rEOBVEM4SAELUqI6kg9pSEoX6RkI4vxOnJ9KoSQyA3ccZKTkDLPt2tFbqbyyw8vMP22l0eLX/G5spN3NYSuP03FcyQk9kCYwzBh9iMZiKlX1Ogoknr0+QWk/k4NKmmzdezc6Hh1UDKg4QQ8yJEweLcKt1qjdVHn7K4dIVu9XzNYv2qGBmNL0B+5k196Uf/hgNnX2Nb2pRByAVcBSZUGCv0+hViiiF6sUYOQ63ionHmV78f4owPHz2OVmeSM2cukWlFkRWEbIJKM7yLi9xaoXQKpY/0dGxsdgsheRDRchgFS0HwdjDZCwO0YnyfJGPUOBKTBFUhkKFq0NCKs0tlCyuKDbtI1aXcWmNrdYG1pRtsrdxj/uYVqt4qlGvAjX1nLGpk0iHPxrDW4nxoNEZVTDMKwVgwMS2FWtPogDaMT9FBq0ni1mS2NiZ5DFtCF++7rG9e4+btf2G7fxd49qHZ08TIaHwBpk++yKW3/4r84HG8ncJViukH1PUYKwoUYbtbMjM9QdmNbsDw7I0aqnHqOMlDyK0lsx3yPMOGkuAUVUtVptCgZSmyHCtQudgYlhmofKRo58aSJ+6AugpCRRb72RtpmCAQiAN+IovTo1RpG4lsUM2jwVCPuF2qcpWtjRU2l+fZvH+L9fvX2bh/E7+1AFQ8D7F6ZsfJs3GsyXFGkqyfpJxG5JKoMZg40I1gouBOPVIxCnwMSk0KoBVIwIpgJUddhascqutsbF7n3uJvgCv7/tx83RgZjcdx5sd6+Xt/y/jxS6xUGSC0rJCHXpzqVe3iyCnaE+z26hNYt1/rnuRo01aemsAIBiVQqpAFGxW6Q6BlbaRMeMX7Eg2CTQkMKyb2RxB9Zh8CmnIpVgD6iDEEYhWgij2d0WAIVJWj1W6RqVD1+ogPsVwalKr7iGr3Nsv3rnDv5ids3L8JG/eguwI8gwrIV8DUxDGmp45GLw3IsoxeVaYZrlEY2KbSqoem4cy51Llq4/cVz54nS+Un70raRQffB9Qy1m7x8NEidxd+jdd7z+4DP0OMjMYwJi/oyz/41xy59CYbVYbpTBA8WO/JQxwXGMTgJTRMT90jtlDP80h/BhqBXGkeirFxEBNVq4lhTKNppUnSKt0hXRWi+G1SMEcsAZcy/AabxbmkzldUIgRj42UfIARHkecY51DXI3d9Mtenv73Bw8V5Hi3dYPPhp/Q2Fig3HsYQ5LkcF3hBi+wgRiYRyVNvamS4QmyTt1awBZgsemM+DBTFgNixBqnM5EE8ooEiF8puD1yLdkvY2Vlm6f5VHq1/yn4O154kRkZjCGMXvsPF7/6csaMXuN81dIwgLiQ9zHgR+SREM0zqMhqp2DEhCQ0fGZr2cpOoiEqU/XMaS4OGaDw0NYQFsUkFIpZGnSqYjCwN7fEadThFlYCl9AFrDBSAOjRmSmnbnMy0MN4Ryl3CziP6W0s8WrnByuKnPFj4lN3VBXi0AuH57pXIOMRY5whZNoNIa/CESeMerSFrQZaldIVSq2zw+FDF5KfF0ESVvBijv7tNyxqsdaxt3GR+4X12dv7puT5nXwUjo5EgZ3+mr/343zB25DzbvgU2w1WR6xCH30jStYp3cnncYNTS+3WYgqS/U9wcUilQ645RadrQB+0c8fGQmtxo+Aa1hmUiKNWMT4WilXIcCoWRqHURPKHcRvu79HbX2F1dZO3BNTYefMb68mfsrt6AtW+OOvZY6xBjY0fJ7TSQEeLZT4roWexgtVFeI7WOpBxGiDJ/X5CPiq3vgVA5MqMURZ9eeZ/7D95jZf2bq5Xxp2BkNAAOvqmXv/+3nH/zx/j2DLvekOVxQppJXkBFRpSRMagWjTKXqm+YoCqCV02iNnUzW2rLFkkGpg5pJHEHUrdovT9oBHCAZiZH3CbanSyVS40o6iuqfh8qT1a0mMgLQr/P+sodtlZvsTx/heXFq6wtfARrH3xjDMUAF3Vi/Djj48cQmcKHmIcCj1ghz2zUM0nt/41AjwrGZhgX81CRzUYSGzJE1y3Q7zlaLQvmEfdX3+Pe/V/i+HbmMmqMjMbYRT3zzs+59J2fIhOHcXYcEcG5KKUf7zeGYAxeBS9ZIgPFxqZoMBKVPHkYXuMYwjiMOKpYm2YITxKtSYalRqz6pURJqJkFJGGc+FoJijGGLL2XhgpLSZE7kAq/s8qDlfus3r/J2tJndNfusLx4BV1/7xtoLCJyM8nE5BHG23MIbVSj6yDWYK0lzwciSUGVeri0SNQCiUSwva6GJq0QozHvkWcVmzvzLD34HevbV3neksRfN771RuPAS29x8Y0fM3X0HFs+w6eBN64MGKOpkSnDicGl/gQjYIOPrEmBunISUklTEh1ZIDaJ1calUbgaeBKxk2rQt9IoZqW/JalHxW0CWTDRqFV9XHeHzDpctc3OowWW733MwrX3WLt3FR59vZoV+xXt9gzjEwdpd2aAdkqAhthPk2WNgHMsPctgmhqAT2MeqbfRFKoYjM9AIM9yut2HLNz/iKXl3xPYB4O4nzG+3UZj9gV99Z2/YezweXwxg9KiqgJYQyuPA3CCBDwGB03oQAo5akHdkPQzTE2iwsfchtBQmNP9LQrSRk84ivaEuiOirqDEztkB70NBXZwkrxU5AfEVvruDbj/i9t3PePTwNvfvfIxfuQm9Zeh9e+6EmT1KKz+OzcbjoGgfYi3aCpINxhakNDWhLkKlUGTQ0xYVzZAo0BMfDhjtsrm1wMLSFdY2bz6DT7j/sP+NxtxFJS8gz2P3UFAoe9DtwfpfvjgOn/ueXvjuv+bcG3/NVnGYnmshWYs8jV/VUNfzzaAhjPQEUXRW8ZgQmlAFNfg0Y8OrYNREtyQkwyEpsaqKDSlI0RLVgMOAZKjNwET6ePAVRW7j5622sNojlFtsLy+yeOc6j+5f5/6nv46dpdv7pw/k6eENnZ39LkX7ApVRgtuEzJIXRZx6ZlL1CYhmexDyxYluQuUc7aIAD84LmRGs8RD6GLtLnu1w796vWZj/gG8b8/PLsD+Nxpm3dPrUOaYOHWX80Bx5e5zx8XE6rRbqA+XuDtubW7jdTV2+c4udtQfsPlyA1T+hdJif0c75l7n4w7/izKs/otuawWVjiFiG8mGpLEdzJ4p3fk1iOzFZZrSI6Xhitl1V4vCfoU7S2hWOI/+ANH9DLFhiq3xe5Fib4byyW/YJIWAzQ7sA192gxS64NbYe3OHBzSss3fqY9ft3YfshdPdfH8jTgjXTtIpjZNksYjKwsZ9EjG8GywwNUARi8ng4h5HZHO/i9x4HcitiHNb2ENliYekD1jauUbH8ND/avsb+MBoTF5XWOBw5wYXX3uKFN7/LgeOn0GIMzQrIWik+jSP3QlXiqgrnHP2qx/LCPPMfv8f81feU25/A6hcQlNrnlKmjtF94i4vv/BXTL75Jd+owHgPiUAKKw6dFHzkXplGdFmLVo5b7j63wQ5dfkxwl9YYEhEDlHMKA3l0iIAaR2PxkijaVA3UxXOkYiR2X/S3M7jZ+Y4mVldss3viAxZsf4e7fgt19ZijsacXniJnk7NmzzB2a4OHKNW7ffLJ5lfHOISYnpilaLUQkzlqxNo6LSNs0BK7UfSzN77F/J8uEfr8kkwJroaw8RhQxgdJtcvPub1lZ+wT45pSovyqevdE48IaaV97m1e98lxNnLzJ1+ARjM4fxeYeuD9jOBM4rXe/x3kemRAvyjsVkFl9WHJg5y/TJS5x95R02b3zI6o2PdHPhFr2tNarSYzpTTBw9z9GX3ubIK+8wduIS/fYMW5VnIgeLQ0NqchJJosAmnpw0/Kgha4U0yHiI9VkzxiVtY9XETlMChRGwcfpalVyXoIms7KGTRWfF+D4ZfaS/yc7aAutLN9ldW2Rl/hM2V27TX7oJvX1GwpLTikwxd+wFTh69xImjpzgwM8HK6g0WF5/s0OPcvqLTk8cZnzxAUbQxxhIIWGuj16iDQdvN4aafTVdy83iIk9g0hplFboA+Gxt3ebDyETvV3Sf6WZ43PFujceYdnf3+v+XFt37My6+/SdGZZG2nx6NK49CerI3vJTIUJrYoStR+7AbF9IV+lWHV0Jo+ybEDRzn1wqvsri6xvHiL9ZUVer0e7c4U04dPc+j4eezkEXZDgfqcdisnaJUo4SlznggWNhGEQohdotKQt4YMRgo/aoNRexqRu5Hc4xDToAGoQoiejMR9BdfHFhWETaruJlvrD1idv8b9G1d4NP8ZfvM+7Mty6UvamTrJxPQxzpx9mYOzJzly8AyHZg6ws/mAB4sf8OD+yhM9gk5+jANTZ+i0ZzBZHm26iQyuoJFaH5XVB6fPDH1vJEHE4KIco7GB4DzGKFmubO+usvjgKlu9m3xb6eJfhmdnNF75uZ5+56d8/9/+75jxWXbEsr7bJZDTmpyMU64UNndJRKaa5BRZmShoUGamMlwJvrLsBEHsFObwGLMHz3DAO8peSZa3yLI2SkFfLNbmZEQPwXmLTwIKQp2cpE63D8YtEmexqgr1sD8RMKkxsp4VEp0JkxJwNhq/4FEfkBDoWEtmQX3Ahw26y7dYW/qMxTvXWVu8Rf/hAqwtws4+LO1lr+v0gUvMzV3i0MGzdCYOcfbCy5T9ACFjda3H0vwi9xbmqdz1JxyaHOXAzGmK1hjGWkIy08ak70jDnu2NDjE/m3J3HBBtbApixGMyT1VtsvroGgtLH1KF/Wi0ny2ejdE4/xN95Wf/jhd//HMYG4MigywjCxbnBVfu0usZfBDGijYB8M6n/g4Qo1gTbyy7Wz0yY7FZhoaCKiilA4yNczkjsY+ed6h3tCzkNmo99vseMWOJzMNQBlSTrFviZyeDEcShIqlHxGC8JxOP1Si9BwYvGV5BTcx7ZIUhlxh+iCvJvSNs77CzvsL22h3mb/4L9+9+QPf2DdjaZ+EHQHFWYZb22CkOH36ZU6fe4PDcRdqdQ0DB4tIWE5OTTI9lbD56xK27n7K8cucJH9QLOt45wtTEMazpgM3iECQRjLF4Hw2GJH6+YWAwZLjKqpFyLwgheDAlyC5rW7eYv/8eKxvfbrr4l+GpGw058Za+8bN/wws/+hvyuRNUeRuHIfg0gEcsagQjFmNNcwHEaeuCj50f+OBQlHanwDlHWZao5GS2RVZA5aAq4/BhHyA3GTbLCK6k293BiqfIx+mncZ2DCymJ1ahP4cegF1IBrxqHi6kfhCbDzM4mjo63Nl/uklHFwUE7a/Q3H7DxYJ6Fm5+wdPcK/YdXYWuf8irMZZ2Yfom5Qy9z/MQbzB66RJEfROlQugJjCyanD2Ckx9rGAjdvfcjtxQ+AR0/2sOiQ59O02tOIyRuuvYg03ihqIit3CKJ1onr4sVjGD8aRiyOwwcbOdZbXruL5kvES33I8XaMxfVZf+9Hf8ObP/pbu1CF2zRilZnixSRTW1MqvcVaFSpP+DpCavWzq8ygQDQQXwwCxGhOQlccotNVE3QjfyFqhPrIrs6yItZIUWjR5imHjgYnuay35byT2liBNblQwVGVSs1YovSMEh80gE4eEXbTcJPTX6a7d5eHtj5j/7Pc8uvMJrOxX8ZZzWhw4zfHjr3Lo4IvMHX6Zduc41h4kMAYmjx2iwcVqVhCyHFbWbnF7/tdU/ZvAk63uKBmTE7NktoNkBS7ObiCEkCbZCdaY2Dv0Ba+PDcORsh8rLVFUOM89m7uL3Ln7S5bXf/8kP8JzjadqNE68/g6nXn8LMz1HNnmIHWyUuiMqRxti34DRAZPPCkPS8zV/omZV2tRUtpd4JcSJYWitHE30IBAkCEqcvxekaSRNBiPRt+uERs3jlNhAHULsePKakmcKzrkksqMUmdIqBAlddjaX2Vlf5OG9T9ldvcvqwifsLF2Hpff3n7EoXtJi8jCzsyc4ePA8B2fPMz5xlrx1mFZxGJEpghYEH/tsPJ6gBqsBGxyPVm6zeP8Km9vXoHzyxtBmLYr2GNbkQzNbvxx142Adppg6b0VqE/AVNivZ2lri1r13WV77mG/6aMWvgqdnNE5/V89998ccfekNeu1JfNam6nlCJs1hxOqE7Lnr19eEaZ6vhwOl0mctdCOg6Y5TSSzNGoll1EZNCzNoWAqJ/yM9EG06H6PZyAgSy65l6bBGsNaQSUzCZqHmbgRsoUjoR+3Qqku1tcHWw3ss3rnKo4XrrNz7DDbvw6OP9uFFeFnHjl9m6sAZjh27yMGD5xlvnyDPZrHZDMaM4byhUvDqGvGJoAFsLFWjj1ha+oB7936FbnzVYc5/GrLMkOeWKlRxxuxQtYpkDCJPRvZUT0iNfzq0HQSMKTGmy9rWLW7d/Q3b3afzOZ5XPB2jMXleZy68xqGLrxBmDrPjDKHv0pyQOHdUEmFKmirG3inldWepBG1cziCSvvZByTMkUhYEvMYwR1NPCAxynUFqCpei2keIw4KiUE6UZ5GgTIy1CA7UBTQ4jCaRX/WgfcbaStlbZ2ttibX791hfuMnq/HW27t+Etfuws9/KdS9oMXmCI0fPM33wFGOzZxmbOsrMzGla+QGqaox+3xL6rVSJUJyW2Eyx1oAPyXYYNHTpbd9mbfkDyvUbT+8jiMdT4Xw3epT1w5LiRuqrpn5CE7dm+CoApSKEPlnRpVfd5+HqZ6xv335an+K5xdMxGodPc+473yc/eoYt06LMM6oyUBStOKQXEA1pVODwlztcKosJK6N1kkPwIlRGCCaGCupr0RyDCaZJesXUKbHpLLWvxOHNSkZd/YiehYqJWp6awiQPuBJT9cm0pDAglISqT6g2WV24ztrKHZZu3+DBnWvwcGF/ehVjL2s+eYa5wy9x4vgrHDp0lvbEEcimqEKO0w7qO1hbkHVSIrmKxkJ8ic3AilBVJUYtqMH1V7k//xs2H30I1YdP7TM718OHXfIsNvx9Ud6iwR6FneiqevXxhiEeMT2QDR6ufMDi0nt4fbJJ3G8CnorRmD12nuMvvoafnGVbLaboIFWPTC0+xEoE1OHI4EuuPY2ANh5I3b5sVFCJPaRBUxZcItXbJpe0KdXLIGFeFzdiqBNALSEN81Stx/ENJqX5sktBRTt3tMVBf5ut1SUe3r/H9uo97t76Hdur96hWHsDafvMqoHXsZzp94DRzR19i9tBFJidPUXQOojJO5VvYbAK8JQSDb8Ku+D9TFBgTPUIJiuIglFiboZVja3Oe2zd/wfbmtaf6mZzv0u2tY6yLE+traYEvPfuabhKDay0m0ytaeWCz+4CF+1dY2fiMUVPaH8eTNxrZBT12+jztA0fp2hY7GHJVxmyBK+Mg3ZASjfGLDAPDIZH/kDpBkuEwjfwdQcmCixRgND2YZvdKnKUaYZp92qCgNI1lop24SeojqcOgOGox0MoE60rcziNWN5ZYW7zBwu2rzN/8BP/wDqztsyqInNV8/DgTk6cpOse49NL3GDtwkomZEwTToe+F7TST1mYFpYPMCiYXnPNUfYd3SiYZmcmQkOGrNpkGRPqIV6ztsrWzzPy999hYvcJTX2ihx+qjJdYPrzIzcTLyaURSWDscgDyGWhBaozgSBEq3wfzixzxY+RRl9Sl+iOcXT95oTE5y8NgJMIYQAmIsvgrkLUuv7zD1FLzUl5FSnkM7SPoSxJJnGq2Ztkz6FJio4ylAinJDXRZVEBxWYylWUw3fYRsFcB3KrFtVrHps6FNon521RfrrD3i0eJ37dz5mdf4m5eo8dNdhe3/dlWTsOzo+cZa5o69w7ORrzB66hC0OxilutoM3GWKVzAhOA33nKUxOWTl86GEQ8rygk2f4CpxTrI2VLcEioYp9OtUGa6tXuXnjn3g2jVy35dHGZ7q2/hkHpi4iYSJVvRJtOMkZNOdFiTcjougOqbKGbLG+8ynzi79mfecGz8N8l/2AJ280picx4wXTrThsxitIkeMCeGM/R/clCLXyFQwWc10tqb2JOL9CULLUcAQmaKSESxYHMEvU+OxkBuv7iK8wWYuuU3rBkI0J3W48CS0Ltiop3CYdevith2zev8X1K79h+d51Nudvwvba/hO4yS8pU2eYPnyeuSMXOXDwApPTJxmfOEZWzFK5DCWLJesQG+ViIlfJyfBOQHJyKxgv2CpOnTcKhQhl1WNsrIVWJa7sMZYH1ldvM3/3H/C7Hz+zj7219Xdy9WPVTn6Wmemc8bGZeE2FjCxvoUHo9fqpO1pQXxJ8RTszFG1Dr7vD1tY1Pvr0/8NW9/d8m2axflU8eaPRzsnbMRNvFXIMie4AuUVdM/EDGOQ1Bs1FmlSuPv+d1poXdbWEmmeRDAbEeZ6+8oiryCRggoM0l1P7hnEDWXDkro/2Vtl9eIelxWssXnuf+zeuIL11qoXf7bsLSmYu6+TsaSYPv0Fn8jQHD5/hwIGTFO3DKGP40KHvssiYTEYXMyC81EnigKRkcxbDvyGCmwoUWU5V9bEaKHLY3n7AwvyHrK18DPpsczilv8vHn/5nzp3pcebsi7TaM/T6lqpqY+wYY+M5zgWKTDFFrJIFv8X61irLD++ysv47NrausLE9Mhh/Dp680cgNrVaLYHJ6zhDyFpUapNJ4B4Amg6U1QWMPhmjcQ8nQCGn+VqIaeFBBUvuSRtIfLijWFqCOflVhxTIhGVL1aLsN/O4juuurLN3+hBtXfsvmnU9h8wFs7zMNheKyMn6SmYNnOXb0EtNzJzh07BSmNU5RzGDsJJ4OzhWEkGESUxVIVYRkWDU0LnudMaoZbioOEr8FjXyI7m6fPHdktsva+k3u3nuf7ubSszoLDXq963K/p1oxTy+8zuFDLzLWOUueHUbDDOpzLBZX9YB1KveQ7Z2bPFj+mIXFj1hfv4kPz+NwqGeLJ240jAaMMZRq6XqL5hllUKTqMdZux16Ox+yEDj0mj/39+epKfbkPkp0y5KkEr2RYTJ7hyz6+6tFpGTLp0t1cYvfBNeY/fY/rn3zE9v07sLGy/3QrOueUiVPMHnmJoyfeZO7wi0xOniAvxig68Ty4kNPvWqqQatRisNbgfM1wDUnkWAdmOWhTkWw8O3EMitQW7y2GgJE+2ztL3F/6kM21m8CdfXKObsjqoxtsbt3SuQMfc3juVSYnToNO43yOtZaq3KRfPqRf3md75yaPNm+wszPqXv1L8cSNRvAVpXf0g1CaFmrAOSFTBa2oaYYDj6FGGtz7mJGQIeMAdb5DU7l04I1kJMl6hCBx8RCEwma0xbHz8B6LV3/F9V/8J9bufUbYj8ODOi+qmT3J7NEXOHX2TQ4deZG8cxwxB1CZwEmGKzci1V4KxOZkJsdrZNW6ZjBQnQyM3bu101EPhDRhMGcM8SiOeoiC944sE1y1xtLiBywu/g6q3+67c1VVH8niw49YeXRNs2ySLJ+kKDqEEOh2t3HVFqq7uJFn8ZXx5MOT7g693W0Kr4SsIHWQYywQwqAtfQhazwiBJin6RZ5GXZ8XjWMSYxVEkBBSiAJGMnyAfghkCpk19NaWuPPhL7j6P/8Tu5/85/11Eck5ZeYEs0cvMD5zggNHzjI9d5bZw+exrVl6VU63zEAKCoHMTsVksuRAIkYnkXNVjeeZWDloJqTXvlnKdQw0Q4jsN6krWIE8D2TaY2XtFgvz79Nbf7KKXF8VpbsipQN6z/pIvrl48kZje42dzU06VYUpoJ8ScbmxqHeIZlGVK3kMA05G3E5Umr8HM0N06PnayAzKpwA2dac6EyBI2rfS311n6ZMP+Ozd/7G/DMbYq1pMnGBm7gJHTr7M0VOvMHPoDD1toVmbUjLKHnFodA6YCuchkxxfWVxFIsoltquAGCHNdNv7XkExGr96UYkdn3XPj0Sim4Q47dTSY2fnHvcXf8/yww/A7bPQbYSnjidvNFZuSX9nXan6jRivSU1mwVVg48DePR7EF+CLnjfN43uv46i8FZl/oQrYLMa2meuz82iZe9feZ/Pmh1/Hp/vKMId+pOPTJzh89CKHj7/AzMELtMaPQT5NT8aoJCekZa3qotq2QAglZRnLRxLyGMxZmqlwkAyoxiFLkqyJaK18Nuj1SVunH1lz/oxUVP0HrD36lPuL7xHWv71Dj0cY4KnQyG99fIXzf1WRFdDbgXYHyu0+E3lU2hrOZKB7Own2him1QRg0nhkxTcNiw+UgNi8ZlCKpfPWqLi0pWV27z+K1D2DzWZXZzikyTmf2JAePXuTky99nfPYkkzNHMPkEPrRx0iJIgcOmnEzkmxgTh/gYD4QMrMTOXqNpCLQkSr2i+ESFD+n81JPfJA4Dqk95EKwlNoF5Bc2wQiRIVY/odxe49uk/sHH//WdzukbYd3gqRmNr4S73PvmQY+2DtOlgqoLMZDjnUcn2GI3HPYqGM/AFOY0g9RN756I2r1UwIeB727RwuO4qD25/DI+eUblw5rt64NDLHDl6mWPHLjN96BzV+AHoTKF2jL4KTkzUCcGk5T7gsViN4QRBsMEkgpuPk8XqCcepKcuEQdOfpNbxOv9Tw2jkyrnKIdYhYgkOELCUBN1lcf59dtavQbkPNUtHeCZ4Ol2u9++ycOU3HD11kYmZk1S9QJ4ZnA9ROi8RjICBAdG6ehL/bJrVHjMQdWN0o6/BIMehGsglACV56PNo+Q5L196HR79/ugvgyMs6e/Q1Tp55i8PH3mJ6+hzGzlFRYMbaOANdl6aBSZwlq0DwsYPTJBUiUcF4iwSDTaMTvPWocalhr9YOGZoNW/frqMEESWS4wRiGLANXgTUmqeYpmQTEb7O7dY/5m7+mv/EU295H2Pd4OkZj646s3PhAt+5+hyPj02xrhbTHQDMade/HPYUm6Tn8kD5GBKufiI+JGQ5fDOAR7yioqDaXuP/Z79haeEodmQde0nzyEMfOvsjM3HkOHX2BzvQZsvwYzkwT/Bg9LwQfcEHREOX2jYBPJVH1gERhIKvETjw1zYKvDSRSV5vieTBp8rxJ2iJCbSQGCeRhcSMjGSJVNDC2T25KtjfvsnD3t6yvfgZun1HnR3imeGrKXe72Jyx8+AuOHDvD+NQxytBpmotIjWbwmPxBfKTpQGW4J0WJ6uQSX783ExLnjASU4EtwW6zcvsqN9/8Z7v/myS2A/LzK5GkOHL7I0dOvcvD4RSZmjtGePEjWmaTnDbsuAB5T9FNC00c9VNuKgkGBKPrjQbBRHFcHlVKf7KaYxlamkCVK9xu1qZJUvyiAhsjPYGA86nmRPrlq6hSvPYq8wrDO+sYH3L71T1COxHVH2IunJ/e3fk0WP3lPz7zwJnMvTpN3DuKkhVP/pS8x6Y4LQ57FHg9k0GOCpM5njTKxKBiv5FbYeviAxRtX2Lj1hCTpZy/rgblLzB6+yNTsZSYPXmBy5hzticP0QobPOjgVnHhM7hFRgnqqsk+WFZFnoR6Cot4gXsgEMhvFcExyIuJirxv1IFoFE/UuMDF5GWJ5WkLdKm5S+bR+RRzHEIWTJSq1W1BvQALG7rC7c4fl5ffYWnt2DWkj7F88VWHh7uIdrv7mn7mQH+DAi4cInTb1QORBLiP+qPMT9d97e04G7NGanxE0IBrSwgmEEDDes765wvX3f8vSZx/B1terki0HX9F85iKHT73CmfOvcXDuLPnYUTSbpnQt1kuh6HSoPFTdiqAeK5BbwZJhxRAqF4lYwZFJXmsjY4hfTlCSxognGFIznmmGA2XeRtp8GAydrqUDNClzS+LBkDQkomcX0GBjI6EF5zNEwPsNlu5fYfH+byGMkp8jfB5Pd4TBxg25/9sxLSZnmTl0hGL2FNoaozQ2yuw1mf2B8qMxiWYeonBKrb4kUm8XX5MpSPBIKBHvyHxF4Xe5c+19Pv3l/wkLV7+ez9B+Uc3sYeaOnWLiwGlmT73NxOxppqYO42mxowWGcaQoyCzsVrH3Jm/l2GQUQuWi5JwKNm+BekTi4GIxgqsU5xUfFBFp1KYeRxQtGibC0Uy939NTIimk0ViRqUdH1qokqqChh5Etqu4iD5Y+ZGtxxMkY4Yvx9CesbXwod/9npa3eA1788b+lfeG7lPkkkCGmIGAJEqsfXgVrJeo/4DHBY3FkErAGMmlRVTFU8QGsr2hpl7Z26a0t8Gj+M377n/7fsLkI5VdssJKXlEMXOHDudU6ef425kxcZnz6KZhMEzamSvqiojb0fPs5wzS2gGr2JWl3MCBJsnCTvIXbKxOY61RBlDG3kxobY0w6A8ekj6CC3gzg0dbOa5EjE4ENBXFQOV0CL2E+iRONrQiLYQeUC7bxHJ9/h/Y9/ze2rv/pKp2qEbzaezVjG1U9k6SPRsuxytA/FiReZnp3D+4puKbHxqjVGK8vo9h1WDFYsmcRKgLqKqu8IYYcsM/heSWYcE7mn7btsPbzFZ+/+Ax+/+4+wvgjrX83NHj/37/X05e8zdfhFsulTtCePkY/N4bI2LgBiB3yS9N+eu3kaLj0smByaI0oUdzFNzmIoldPwKqIHoU1J1SrRuFKXmGm2i6K5qVtVkg5qSnxGGyZp+lggswAlVrZYmP+I+/Mfg9/+KqdrhG84ntkA6O35j2V72+v9zYKLb25y9PW3mTgwR5+CykNvt6JXOWbanWbOSWxIs4i2MQJZ5qDapJU7WlTsrCxy5ZPfcfvq73h0+xNYufvVlLYmLuj44Zd48Tt/zYXXfoKMzdF1bbATKDll5TFiCXs5rcAQk/ULSGnD5VFJSmUN6ia8UL8+eRX6ZXkdbcY/DJLGkNwL4gxaM7BCca8p7+HSYO1tev1lbl7/gAfzt8DvLxnDEfYXnt3UeID1z6T/Yaafrm/Qvf0Zs8dPceDwaaaPnmJm5ihSjNHtVThSU1tqi6/5Bla3Kdwy6/dvsDR/i4f3brB0+1NYnody5ytL8+XT53j9h/83Zk6+jB8/Stdl9JzG7tKgaOmhMJ8zGVpbOGD4qXrxN78z6OKtHxsmsQ1vN/h90PVrkNiXsmebvcpbfsi7qY+nrqMYKoQdvFvmwcLHLC1+Cm7tq5yyEb4FeLZGA6C8Ku7GVW7eOau3Zg5xYO40syfPMnPkNGMzR+hMzWKKMbL2eBzD5xy7u7vs7OwQuqtsP/yYpVtXWJ+/A9vr0P16JOiywz/RU5d+wKFz71C1DrIexqgw5IXFmBYmxHmhPihq6oRlmstSl4OHFmxdyYAvMgTDPyW9Nnki9XPpcWno4SYmhJOBqXVURUgT7+sMKE2lpe7PSUeB0qfIN1lbv8Wtm7+mt3kP+HTkZYzwB/HsjUYNd1t05TaPVt7l0e0LSmsSOlNkEzNI3iYvWmAz1Hl6vR7a7UK5AduLsH39673Q2xf01MV3uPjaX2PGTlLJGCUWyQ1BlbIKqAcjOe4xz2H457BnMUxMG97u856G/EFPYxCi1AnSkLpX40PSlFZj3kRDJMCpNlMVY84lBIzsEtwyKw+ucP/eB+BWvtbTOMI3E/vHaAyje0PoAutxKhpA9RTf/uD573D+lZ8ydeRldmWGYHKCUYJ3+KrEuICGnNyAGsOwoHq9qBvPol7ztdehaXpb7YMM5zRUBzmOx3Iata5IM6qyMVaDsunevEdNLU9Errj7ZDgc0Ad2WV3+iDu3fgn9e8D+G/Y0wv7D/jQazxIzb+vxC+9w6NQbdM0B+i5HMzASF22WZVhjsC5PreSyh/v+l+iCfNXHjQ4/DpHtObRxGBiaqK3Rw0gXK9s8fPAxywvvM5osNsKfipHReAxHL/+Ik5d/yFq/hUiBmDRP1jkQF/kPIWqPYqQpndbVjuHSJzBUW63Vw0jbDcITAIttqiFxgzqnYfaGL7Wn0nA1huS6hvYd0hg6a3N63Yp2VmANhP42nVYfCVusLl/n6gf/E9zIYIzwp2NkNIYwdu5/1ZMXvosdO45pz9FPs01tc4fWlKuIDXGapDSHE55/Cr7Ie/hLPY1BtWVv5cQoUVC5CrSyHGsA16fISgq7y8qj29y68S6u+/DPOPIRRhgZjQHGX9Wzl3/I0bPfIRQHkTyn7EJhwKjD4kE9isXXXbQaF+dweDAscrOnelJXRcKAoxGfMI3knqacxvDrmi1TrqOunmhKgtZJ06ZXJ5ASn5IYn56slWG8EtwONt/Flcs8XLrC7Ru/G7W9j/BnY2Q0EmbPvM7h06+RjR1lR8didcQA4hCNvepGDZ4shibRBnyOh/F41WN4sNNeIxIfN495Ep97XR3usFdch5QsbRKvw+XUZDggTpjDB0LokUmJ+nVWl6/xYPEK7N77Ws7dCN8ujIwGIKf/Ws+9+hOm5i7hmcJkOb1KabUFX5YECYhanFoCGRrJEEP+ghksZB4PKeocxOPT4QbQMBAPqrcxtcdS5zYaA1FrpQ4bl4EIT2Nr0uusGLzrk+MoisDu+kPu3nmPh0tXgZGy+Ah/Pswf3+QbjsnzOn3qRY5e+A555wjet8gA9SWKQ8XF0Y6a4yniog0AUeK/EbVJeNwofNlzmpKXGvY+9zjPo+F7/IF9fxlBrO5DUe/ITAC/y+rKXZbufULoPkExohG+0Rh5GscvcOzSd8gmjlKFsajq7cAacOUOQQJeBCPpVIU4JkDUQ5pENqBNDVBXORrS9p4FndS2mm1pGs/q1z2+omtDMBySxMdrUZ16u/RLYoWGEBCjoI61Rw+4e+sTdtYWvsoZG+Fbjm+90Zg99SaHT7+Fk0mqYBAbJfDyVk6v6iKZIXaJRqJlk6AcKqkOUzU0PTfsFdTlWBgkPePje5vQPm8oBjmSxx+vt4+KXD4Zj4HxMho7U/CBQgK4DR4tf8b9+1eAbw7zM+OMGlpktiDLWuRZi9y2sabAGENVVThfUlU9ymqbih5uRGL7Svj2Go32aZ259GNeees/ErLTlGYSiiJOKTNK8Io1YzS11RD1MYzUCUdLzFekB3SQ14j5iZrlOfAqVJPoL/XCT0zOFKI0r2tYpIPn6r4REtM0GpSA813yPEdVcD5EIZ+kGWqxUDomxmBx/iNufPZ/EnY/Bu4+94sm44IKs8y0zzMxcYTZmYNMTszSzifJpU1uO2S2TdntUVa7bPfW2OmusNNdZnPrgW72H9LlAZ5nNf/m+cW31mi05i5y+uIPyIpjuGwGL0VSwoi3/9h4NhgHaVI9s6mcpMY0eSzPMMhx1GFI/bg8lnuo29lTFYU9hZgvyF/sPX6jkfhltUC8BaJcX6SwBwweq4KRLltrd1h+8CG7W58Bz3eJteCcTo+d4/DBF5mZusDM1Hla+Syd1ji5bWN8C0KGCQVChicQ2iWzE7tUfoN+9Yjt3Qds7SyzXd5lfmVCN/wov/Pn4NtpNMZe0LlTr3Ly/Hfw7SkwcXhyCOFzqzP+Oewd7K2QPF5C/ZyOBoOSaHx+kKSscxdm6HV1j4oMeRt178nnGtuCwcokmqbDx/04CA7BI6aP8pD7y+9xb/5dqu6vn+PFcVknzVmOzb3ByWMvc+jAWcbac8Akom2sZKlDz0DIkGBAMwqbIaIgFWIqAl36kxt0e+t0w30mxudYXD6jD7euU7KOPudG9Wng22c02me1dfoNjp59m2LqBL2sQEQIITCcgKz/Nk2z12NGIW33x3gYX6aH0fAvdKDGsbfLlT3bfVGlpJ6aFlxsicdCCIrBYaQk6BrbWzd4+PBDNlc//TrO3jNBm+/q3PRbHJ97ixNH3mB25jSZHcc5S6gMqjaOowz195VhEk8leAhBUM0QsYjJyEybidYBOjLLROsA0xOnGF/4PYurV9ghU8/X3DX9DcO3z2gcPM/5l3/K3Lm36csMXjN8iOMDTBy1njyDaDTqJV33gOztLqVhacLeqkadwKzDksY0hMcMiw4bpVpgJ1LVm1TnkBHZE+akfEcIYLK4ddCANYrVPlW1xtLShyyvfAL6fMbuBW/pqWM/5NKZf83czOvkZg51LVzSS82zOI4yBI+xA+FpvEuzYzKE2HUc8EgQjCmwNsOaAmvGOTl7hMnWCTqtQ9xZ+hc2VNWPkqVfim+X0Whf0Knjr3L0zNu0pk6z2RO8FwIhCe4OhR6PeQhfxPAUHa6MfAknYyjaGfYk4PNVlsdLrX/c0xgkRwUF9Wgosdajocv2xiKL9z9md+v2X3a+njE69m198fzfcmT2bQ7PvkJhjuDLqMsqBjIB54EQMChBQiw1q0d8MqYEIMMY2xh61YB3As5iTAdftZnIM84dK8hNixuLwnqo1H8DEsZPAt8uo3HsFc5c+CGtqTNs+4zKxtkj0uhhxMFC+BBHJBoTtXmHcxBDnsEgw0GTi9DUbdp0szJ4Xa2T8fiVONx0NtDTSD+TwXo8kSpqYsetCmICxni87+OrEjLFVbusry+ytXUPyqvP3cU/O/4zPTT9BudO/yva2SlUp+n2A0Ifk2eAUrqADRkGkwy+IMajKlgMaqDsekQCNs2zhRiyaBqSYXJL6OWEMMtka4wzR1oA3Hq4y3J59xl9+v2Nb4/RmPmunjj1XY6f+S6mmGOn7JO1cwgWkTDwJjQSoqzEQULBh8/t6ouYmV+0KocToF+2D/MFr/yiHpYv34fEHIYqVQhx3EMAX1bs7G6ibvNLX79fkfOazk6+wqkjP8T4E+T5CXIzgTOe0pUYp7EvKE2VI7L6o4EOg7DSaIYxGSIG/ECISESwkuSIyj5F1oLQIYRJ2hI4NPmIre4Ndlfv685I/vBz+NYYjckjr/PKa39L0TrOdt+QdzKq0EW0E0uX9RQyhdzmsdvUDSls1XyMlGeoO1uFEPVBQ93rUYcVqSuVgRDygJsxnBB9LPcRBnmM5n2heZxkUCRoMxwpqKBqsOQENaCeqnJ0d3ahepqaZ18HXtDZ6Vc4fvT7HDv6Fi17Ctdv0y3B2IzMCkEdwRusZHEUg1cCgSy3IOCcQ4OgxiBGBh5GgDiYSqK2qgmo9MEIRgqCg4xZDk6+hOMBlV/j+vrzm0B+Uvh2GI2D39Pjp94ka50g6BiSgTEeo5ExyVCF5Ms8hi9iZg4//8XNal/uKQy4HzVZ7E/H53IfHlRsnDyPR3wgM9NMjB1F8mNo2FTcVxwW9cRxWoVDHJ56hdPHvsfs1GWMzuKrnODj2TcCiI0VEklqagrGRl5sVfVwvkR9QMRSSMBruiWYDCuxTB1CwHmH4jCZw2hF0CL16uS0zCGm2hc5NPU6K+u3dZ0Rj2MY33yj0Tqrxy/9kNOXvk8xdpgusTxpBLKUaDQ6EOatPYFBhaPugYfawxhQKqKnYL7IYDQan3tLtoNu1XofiV/RzF1NRqHW1Qh1lWa4ejL0fkEJHrymheUzgoPcHOTA1GXmZt9iZ2uK3a1Tqr6b9uRAyngipA2aDQ6mxpdERX9uh+PnJ8LA4JNCnrVwriDnIDMT5zl19A1OHH6JyfFjoG1c8GDsnu9IE6dfjMdrSHNnKtQ4bOb/r/bO/EmO4zrQ38us6u45gcENkIJA8JSWlGTJsuVYb4Rjf9jY/3ojHBu7oQ3LYWtlmxZJySTFA4M5eqavujLf/vCyqnsGoECuJQsA82MgZqa7urqq2fny3Q+PEKOljtstTUBHoB5SlbB3I5wIkQ5Va3+gUqAKheywN3rIrf0ZX0w+oqnmusyZowMvvdBwd/4Tr7z2Uw5uv04lE0KXrJC2s13r0qJ+FpsCo68i3Xz95ZDrk9GRTdNDLyxC1fUbfHVF6+a5QCSdI5glI5ijz8s+V/Yf8M7b/4168QNW82O6tiLGDiUQpUFEKPyEC+ZQ4sIs2EuPbx4b45M+n6fxVZW64CjcHhN/l53JK+zt3GVnfAOnI/qCP3EtxBF97l3UYIlaqnRdzah0+KKh7ZbU7YIQa0LT0jaBrfEeI7dD6fYIYYTGAokmMIpiRBsDSmvJX7R0weO7gkIO2Cnvc33veyyqz1jyL1/rPr8NvNxCY++nev+tv+Hq3XcJk12qJkBpu1Rswfsx65qRNP6wn3A29K0gPb7uuWOPr8N3PZuawWZo1l6/7ib+RBJYqjcRsAI41XV6+rB45QmBhQYbgZ0mGYhCly5SXMFk6yb721dh/x20S53HNBIJqFOcJ5lncUPYXWRTeGwu+P64i02Nv9oU2/y3SQjKuNxn7K/j2AUtTPMSUDpEWiCg1Kh6JCreKc4pQosvG7RoWdRnHJ18yuPjj1ksT3ESGbkx167cYW9yi/3tV5gUN/DFLrFxhM6Eq5RbdjcuJA00EILi3IhCrnFt73WOz97nrPmutjzvJt5/DC+v0Nh6T/2dH/Kd1/+KYu9VVgita/EFaBdxOsJrP7fEFv7Fxbw+Ve9zeFYk5PLvm3/LU3bzXmCsz7/Oy3jyPdav6Y8TIkqL947Ym019/RwNzlvLQi9bFH6MQwgaLNwooERUOqtjGVJhL16Ak428EtkQWCQB65KwZajMsVtIIWl0HZUackvSeUHZ2i4p3Qhhgsa1g1kjhCApw1NBI0rEe2dFeQhBlxTjJY+O/pWPP/1nDo9+w7I5pGMGtAieL46uc3X3Fe7deJc7B29zZfIao4kn1iNCB4VCjCVeBF8EYhK+Ip7SbXOwe5fd7XuMm2u0fPz0/znfMl5eoXHjDd5672/Yvf4WwV+hiR3RN+AjEj3oNnTm24juoqV+MeR58bRr3wfDYugf33SIrl94SfPoF86GubH59/D+vQZ0Sehc9L1EglYUfmQZjynpSaiIMiM4xRUj0JIQlZDETBSljUKMUDKx7oCXNIlh8W78PTy/IVhcsNClRE3tA578GdDU6Bg8gjqxFHhnwquNHaoz8ycwonAjVB0aHTAGCUCLR1MvE+gaqMKCs9O/58NP/5bfPvpHAic4ZsSNcQxLhdnsB7pcfUxdf8H9W3MOdh9Sbl1HmgkSR0T1eE3XJC22iUScg+3tfXbGB0y4SR6LbbycQmP8ju7cfIP7b/85YXSFlpK+WY10aSKZ5Vltdv7/Rjwrf0KAvl15X8G6Nm/stdbzYkMQaQrR2knWz20cs7lwHdJX7PfbI+IiQoelwbs0ZS2iIe3UhSC+pHC2k0tMw6F1EHf2fk/9mbQGy1e3x703zcan67Ox9OCSg9eFoctHIYLzljTnffLDaocrLOoTQkBDS4jgXUk58sSmH/qNOTtV6WJLVa9Yto/51a//B8fzvyfwOfBv8jQPS80v5bBbqT6qKMuSsizZHZXgCpwbIRGsI5tD1DKEHRWRQOn3GRUHeDdmFO9rk7NEX1Khcfst3vrxf6UZHxC9DWguwghRj4ugEgm+Td27U4l7lIuL+gmHZjINLrXnu9zvQjb9E0Ouhv2+Wc06ZHUGTf6RZKZEGd7Lzt/nacR0PQxRFKRAZI+mUyDYDh4DUT3CrjlEY4GKwxWCdT6P0Ckeh1fLht2Y9bQRHbrkGCWZJxsPKEKbkt9E1mtJXMq2AsqUvWkyTa2LGCH1TBUzzzpv5ewA2pnPiRWkURGxKxi5EUSPas14S1muTvjk87/jdPZrVvyvZy7kjg9kHnb0t5//AidXePvV11CETsF5rJQgRrwUFK4DWdjjcY+DK99l+3zMo1kWGPAyCo0bf6EPvv8zdg++g4x26Vh3/HbR4dQTiai0qWFweaH13tO4nEdxuf5j87H1QWtbH7gUJUlp5WpHDJmdyaWxqVX0JfGbUZrhWtSh0cr6ZVO7SeFJl1LhByGoachTVHppJ5eCqKaPXfLRSLT3StqaqqVqK5igEgvbiAiFmBYn4sxUSvdomsKGhB3uRczB2b+llkALojgC4jwx6iCUVTuadsGy/oLZ8lM6vn7G64x/ENde0/nyEXW3YuRBQyQ6h9/wJzlN/hkV0C0Kt4MfPdnS8dvKSyc0Du5+j7fe/hl+5zpVBOdiCjFGxPUzTwWJpSUL9bNS+xPEtVnQL1rZeHzwVPQ1JBut/OApTtEN4XE547Pf0F06cUgH2o5uGlJvDsSNyWp2bgbB0HtA++t1/dqMkkrGGe4bbEG7pBm0XT8ttw/BOGRDybf7iXjZcJZiWZYiEfHRNAjxQ3q2iDUelF7h6LUX9cNAuKC93yRakt0gvATwoDZxzjlHlGialrQgSwIzquaIxfLYwqXfgIpT5ssjquaU8fZdCOX63on23jjQEo0OFU85njAeb32j93mZebmExsFf661XfsTW3iu0bkJoI5RWsBRdAHH4AKoFGm3n0GFr/2rNYfD6az9f5GmNf3tzpj9OLzy+9mVc6pchtrCVzWY8moTCV/XnsEedChI3w7E6CI7016C99EKo39Ejphm0WLd1h6Sfa3PDoiqSIiQO55IGkQSD4FOeyIYnIXb0bQxN3jkuSNvhw9j89DbqQhREPaIjkGgZnCHYtTvFOUFdRFwEabBB1t+Emro7p2lnRK0RN7aws/TSVQgKouPkjSop/JiyLL/h+7y8vDxCY/yu3n3zr7n72l9Q6xWcTCjEEoGiBFQ6gtiEd5ECF+x78qQjtM8Ivdx8B8yE6AVHHyWxHxf7bGyEKYfcDJcWUW8mMJzfTi2EDSfj2pciw3ms3mTt/yDa93y9CDcny9vfVgxjz5tDtEtT3uzeZeRMAREzM5yzilFJKdoi63+9RiQbvQkldcsa8k8EVAMxXUM/o1ZIpemXrLhBw0h5MDIIm2Ljun26PhBKkJLCbzGZbBObxTO/GhdpEdcgvkFcDSHiREE707DEGklrKMCnjSUP+rjASyM0/O3v8eB7f83V29/jvCoY6QjnuqEEWgU0KuoUr+tiL+mfe2Y0pC8w0wu7/eYxxu/v4PVkgtM6JCuXftI7CzeK01STBtC/PFV39iHN3pwAh0qk6daagzhBvAMneITgQEo3NDEfhEP6/eK4ycEtkkyNZB4FcNFeqBo2/CH2vrE331SxeI9j3Wd14/NNXtYLH016H99/RlFMQ2JE6a+wu3Mdd/7NVrRnxKTcY1RO8L6k660bsWR0SSaK4q3JdGEFcO0LV/j3x+PlEBq3f6r33/kZV+5+n9bvE0voUjctK29Xgnirok7hVlgvtKF3Bevfh7yE2O/yQ80qoGtfxsZUeFj7Oob5JZdqR/r+HE9kjA6vtU5U0vsq4ro2RuKmTyNpC31imrWhMf/H4JOIdBIsxFk6nPcW6vRJAXE83SOQTBozYewyeuXCTKmYNB2hCOtMVcSj6lJ/jySUXUiXEtf2R/82up48h/j0UfQanLsgQCQ9F4LHuy0m5U2u7t1n5+QWTf3R078XT2HCLfa2v8PW6C7EHUSKteOl/9xS0+j+Uqp2QdWsvvZ7vOy8+ELj6ps6vvaQV974CcXubc4bKEaeGJpUkyHgC1wsCK5fYBsa/aYP7hn8Pm2kf/6roilf9dp1iX36nsaNjM+NfxeO23ifQUBITH5OTT8DEZjsTBCf5tKmn0l2kQbFDYvThKQSVIeOZA5zcMbhWk0rEG+rynIt2FB9rKDOzKqIG9SYdBMhXeOmtgTm5OyPwzQqITlOUZBiWNROSibjA/Z37rM7eZ0uLnTW/vKZ4dB9fqK75QN2xvcZ+TvEdtvUTNq1ppPqBZSkgbqWLi5ou+WzTv+t4cUXGlt3ee/P/zvb1+4zqxUZeTqBsRdi6IAREn3a/SNKd8E5Kb0u3m8yG6cWSWHFC+ZAb5f3O35qQNybOn3nrsEXsdYwNk2UdaFcTFpNv8PauXzS4EMfbk0LWlkv6CjQEQgE67jtBO89ZVlSFA68mJBI2oKKhRLDxm4vIQ6VuPTvJcPtWWSjFypR19cZ7TrNB5IUCTsoaSp2krBR7Wtyw3wcXRvpYkQ1UnjwXkDMrxC1wzlvDlgBVziILaoBX1i9ilBw89qb3Dv/G+Ron/rsQBs+Bz54qvAY867ub73Dg1v/hdvXfkxXH9j1OAgh4FykcELUgGqHE28h47JiUT9mUZ99xRfw28eLLTRG9/XWgz9j9/qblFvX6HRCLEA76xdpzrdUVg3WJxJFUKKE5PTqNdEnv2vPUCy+lubx+xDAxbUpM7xGdbB++m7oIubU1BBtlke0wjPGDhmyLP3wr9cqut6K0rX6HZIUGHwjKQ9jIK6fh96fsU5h7xPARCFoh1MzLQTsWnrbBp/8MljmKcnUE2E0MiEiAWKntKECUayPjid0kRBaxsXYTiYB6EPWFhIl7PPde/+Fyeg6hVzj8fRXLNjRyAn9QKhdeVdH/iZXtl7nzsEPuX7lHSajW4OPJqZsUJHUjpFgWpQDdQ2BU1b1I9ouJ5H3vNBCQ+485JU3/pLJ1e8Q3cgWUoROGwtXSmFONAWfIg3RFTg6kIBKRLTg4gS0DUcfDF59S7Xe6OmgfS/PyGYP0SfyNYY8EPMKyEZkBEi9PkmORKVPu+xzNKL0oxSSE1UUvC1O8R6Z2CjJXli41MRcgU5NqxhSv1M166DUpJyRzW7qa4GRHLBDNMetc1O0T05rUW1Rlxy1eNMkkpbhok/CKPk9AnS9NSXmf9UWMyF1C1cEpDd/Uko8UcyZrV36nyBYDscYdIud0RXuXr/C1vgm16avcXL6AeeLzyDWWhQj9rfvsLt9hxv7D7m694Cd0T0822hIp9LkJKYEgrUXkA5cS+SMqvsd58t/o25fvLaJfyxeXKFx5yf62vf/iiu33oLigKpLarp3aAhEV+LFD+aGC+mLqhDEFqNzbp3Tk9hUDjajJpv9MS7nW1zmq6InFyIjpNqTwdxJfgcss92EVprHQqBJ8VXnHUVRUJYlrhCCh+h1yMoMyZ6KsbPhz96tbavBVFibQ78vH2UtRNy667oqlo6e7sF35oBVU+2JoxQKthCrd8VgBpjvNJjwEeuo1QQoHZRFkpdBidF6fzrnbHFbqhjDsOvUTEeAeiWUxQ1uXp2wv3ubWwdvMF8eEZoWEHYnN5iMDtjbusGo3LM+HVHB1dY/tHMoBU4DMaaB2QWILGnDMWfNx5wuPqbOs1AGXkyhsfWuXnv1h3znrb9itHuP4CbEdkFRCDghOuuVoVGIKYToAdR8BF4cnVfzkaZIhe3kdnrtHQD9Dsm6HkRY787J75hek3bXQXVf+yj6TE7V9XGb9R7Q+x1sVzVffkgRjIi6iDrBuQJXFriiQAogzTrpy9djb9Yk34PiBt8JMCz0IY0dgL4lXnoudRDrcy8kOTbXVbbpb7VIknP2u6iz4rkIlghmXcKbqqIQQb21WCxEwbXmuFWlmBTps+gsmcp5tBuj7ZguwDqnqmDwWKvvbcpU+CaobjHydzjYvcbBrlUyizokFDgpca6AACHWIC3OdyAlhIlpGeqIMXUo9x3qWtpwwvHZ+yyq3/1/fElfXl48oeHfUW6+xd0HP2bv2kPq4godJUqwqVoxINERndB16aumIMRkowuBtCMDPoX2+jV8ceftHZlsaA4Xd+S15vD0nZuNhbqpoQy+lMHZamJC1aIgKqm0fGTRn3JU4ktzbEaBRjHHoDfHYv+fICkPw1LFQwi4uBYAF5yumK9jUyvabBRkz8vGPV7SUMQRQ4EQIaaFTIHHD76Yycij2gINgo1FDLqibStiV+MIVPWSrmkp/BY7W7cYFzfxeLr09eyttuH/kvRCHYoy+SWiA51QyC5FuqGuW7dSXLcNKJJ/xD6/4X++Aql7ufM1dZixrL7k8ckHNPr4a3wxvz28WEJDHih7d3nwxo949cGfEfw1AuOkdUcckdAxfIG7AK4ApMWnogdPSUgpjZqyP/3Ggt/s2GXRkIs+jZ6Li2jj9Zeev9xz9PLckwCDU05RcBeTsIqR+SyksHuJyQQJKbXaohVJ9++1oD5+q1akR5ThHvts083+H5F+1ELy04gQcYOQjclUkk2hIoLTIjWtSd3co3Vl72JEg7XRmxSRNsxpmilde0bXndG2M6rahjHX9SOa5py2sWbIV3a/y/Urb3Ft523Go1vEMEofb4118jKfBm4LRGjCCu8F70rz0cRITKnnbddSOj/UxYgITgrTwkJhGmAfWRMTKAqE0DJfHHN49DHHZ5/Q5vmuF3ixhMZon+LgVe589wdcv/MOjxcFofDWu8GPLQ1YfdotLGwJ/c4dk7OxX+huCANuDmPuuZSzBaR1eSEiss5/lPURw++uFwRq5oLvNZq+pb5ARzTnphWRgAc/8paM5YRybIJFUZoU11TBci9UIGz05EgeUI12UKcMkaM+HUXFzJQ+ozvGtdAD03Bc0mL6W7UIixvuvQ8nExUXFCedNdeRQNSOrlvRtEs0Lvns+FOa+oTl8pCmPSV0U5p2xqqe0jQndHoCUpvpyIQducft65/Q3Ztx+/p7FP4GxElKfU8JYKLmrKRD48IStIjEIGgn4EYUhacoPLENZkL14Z2YtD760QZWRxMFoq8InNOFR0xXH/H47H0WHH7db+e3hhdLaOxe440f/2duv/YDZvUWEWs7b6kBE9oAKiYQuhaKoq8CFRopTWggRFWKznbOMrWYi2o5DGa+9JEL0o4KSKpG6SMoppQPbfb6XApRu6AC02A06CAk+kYvyVdJEFhJi46VsiwoR33mphKdOQgq8wSmha6WsZrCl049PmiaGt9HHXofByZDpL/GMIRcTSAoMTgKP6ZP8LKokeWyeJfuNyoxtmgTcUAhhY0OiALa4ajp2jnL5YzF4ojF8oRqdULTnBDiGSfTT4jhjLab0nVndCzQy07FDTm80F/y6dHv1PlzRJbcuf7neO6humfOUYEQW4JWlONA4WpUa1QbvBsjowkERVv7XpSFJ82QAlGcBBtbqR1oOVQAR9fgx1Pq8Ds+P/4HfvvFz/ly8U+Er8j7+Dbz4giN8Tu6/+D7HNx9nc7v0HQelb7HgYKWyYHYT9da7/pRzCGogy2cOonHdb+Knk2B8VX0oVknlnepzhyfVhmqfRGIeeIdEONQNxHF+kKJ86iPbG2PUa9IIdboVyJBTGCohqQ22y2KuiQ0JDlaFV8IOLXJ6P0uutGE2Pu0KFJ9iFPr5Bkk2vt1lvvgsEQukYDQ4aJpDV47ChcotgKikdjUVMsF5+fnrJZT6sUxsZtT1TOqasqqPqGuTwjdlKBzlBnCik6/vorf8is5me2pc46trV32J56yKIntNiFgahaO0CnCBEc0X4UWFtrui9s8LJeWRu8ciEvfjgDizGTpCPgyIqMzFu1nfHn8f/n00S84mv8LS/5PFhhP4cURGrde48FbP+HG7Ye0jOhChR8XRC425t0owEwOR7eeXrYROiUJhq5PG07Pa3IIDDUjMRVwJUHQTzZzUVCJOKnsC+qcpUwndb+NvrcZwEeiS53CkgnivFCIZ2trYqHVdM0hWkGXqjO/gvjkWzHNiCQcJNrFdTSWyCaCWvcYGF7n0S4OGpDrBQ/rMvyRayxCEyISAxoDMdSEbkXsFlTtnBhmNM2Upj5luTxhPj9iMTuhqk5ZLo5Aa2JsINZABX8AH8DJ8n/LctloDI7XXl1x50aHczfp6gkaJgg7eA+hTZnpCYciLiBimlW5pWauijfBGgpiKKxBkVeKrRlRTpg3j3g8fZ9PH/0Dn0/fp8pmyVfyYgiN/R/o3Qc/4ua97yPlFdrWGlCKhNQX42kOSvNRXG69fzlfoutlSMrmkqhrZ6HZLCn92R7vVXYrKAuMvJq/xHR7VIvkg3REn/IsRHCuBB9xJbjCNAQvBRJSdEfXjWk0DTP2uHUtFeaMVFKJuk/mjk9Nc7x5SCU1PxWUQiKhbQa/TUHvELXrjVoTmyWxXVCvljTVkqZeUlfnVKtT2vaMuko/V49Zro5pmil0c4gV8Js/6k5c8Qv57WGrkRVNN+Pq7pvsjB9Q6E00eDT2MZq1y2XIpxHrlSqlINpYwplYjLbvBRLdHPxnnM4+5MvHv+bLk19zOvuImkO6P/K9vci8EEJj95W3uffGTxnv32fVbRHwFCOzvS3nwa8Fw9Apay080i8Mhj69M4x1WXxabC4d55Pw6Dtj9UJD+r4YatmSPnqUSIjYtK8+FImFffEe10c/ytSiobDLECCal3Nwzm1cIipY7UbSJJxi2Wkb19+pOX+lL/4IETobbUCESVngYkBCh4RAaDtCW9M1LSHMmJ79hqo6Yjk/Y7E4ZlVNWa2OqVan0J6BLrBGNx/9SRZR4JfyyeFSj08/4ZVbP+b+3b9kb+sNJNyk9AeEOMKJG5Q6jYEYrV2gl4h2ASHgvI0/sIK9jrquqJpDjo9/weH0n/ny8CPOw+dEpoQcLfm9PP9CY/tdvXH/Pa698g5xfEDVjmzuha/pugZ4sqPS5UjI5VDopqYR0kJ1vUkS1ynUTl3ye6Swqa5fHwAXPRoEok9CSqw+xDm8F1wBboxpRcU649PMIRMyFMHUZjXfgqZGQXaxltxlGkYczB1JTk1VwVNC8kkIincRyojEGqFBm5q2qaiXU1bzKcv5lPlsSjVf0LanLJaf0LantM2ctp1DqIAlbIwB+FPT8aGctR/SfTHT+fKY/a03uX7lHW7feBv8DiLb+GKEiEeDWn8PzGQsU2NppQapabpzZvMTTqaPOF99zKPTnzNvPmUZT4l/IsH4ovF8C43Jm7r/xk+5cf+H+J07rHRM9NawtunCkEB00SS5ZKoMO9Da92F/Yw6z1CdDeiGhigTFDcKDoZYi9m7WtOgjHml96hth0RpKbFRHythMyYYMRSuQyt+t4KwprKZC1ZvfIjrUCZaRmb78krpYJsEV1JykLngm4qENdKFFY00XV8T2nLY+patPOTv9jKY6ZjV/zHJ+yGJxRLWcoqsFMOMP4X/4j2IRfy7L02Mdnf4r0/kHVOE37O3ep/RXGMd9Cr+LxLFFRRjhcNRtRYhLmnBK3R6zXH7B6dmnnEw/Y9Z8yoqPCfz6hfkMngeeb6HhrvHqG3/B/q3XaYsdVo1FGDSCtoHtUTmUXn9dNv0bkpykJJvfhT5dwqIg1kE75VSk14UUxgzJsajepnRRgpuAjMBPQEqs61O0zhVKMPMmJZ55HCKFDQqyeK05MyVFfzATyLmYkqoiTjpUW3zsLLIRA6vzOV21YLWcs6qmNKtz5vND5mefs1ocEeOc2E7R6hTaX73wi0P5UGo+5NH5kR6fv8/ezquMywO2t64xKvfwuo2TbZyMERGaemFCo5tSNY9ZNY9Y1UdUTAlM0TzH5BvzHAuNN/Xqgx9x78GPaP0BXRDcSIYmLmW5RejSFg9PzErtqys3dAs7LmVouj6PO65DqDYDRC5kf7ZtY30dvPkoVJRONGVRNownDld6irElYmmJHUMgEgjSWW2LWkNeJwX0dR8RHLuEoBa9EKEQ8ESLRnQ1nhaNFYQlISxoqhnz+QnnZ49pFo+pph8TmzNWqwWr5Qytl9AuLKxAB+HlnD/a8r60wCq1CJXpQ3WMEUpc+icIgRalQalRVoQsJP7dPLdCw125zbVbD9HRAVrsos6nLtEh5S34lC79zTSNTYSUNZosB6FPhFJCMO1ga29EG6BqG5qusSK3ScFoPKaYCJMtUjVcpBGIsSNomhamyrhM3a4DgzPWp3wRjzJWNS1EImiAtiY0C5rlOV19ztn0c7p6SrU6ploeU1dTVstT6uocmlM4+XleBIDyGwnPPizzB+C5FRrb+7e5fu8hbnTF9H0A7dDYmk2At5b7/QSyXpNQl6Id6UTPkCmhTaYJqQo8xWnVEiE4Xy1QL0QfcRPHeDQyoZFMkVoqOmnRpE2Q2v2LjPDq6FqF1qIXFjQJoC3aBbRbMfINhIq2XlGtZlTzKYvzxyymh9SrY6Ynn9E1J1TLY6hOoXk/C4nMn5TnVmjI1g5b+wdE58FZJqPXiKVo9yP8/jDvpZpqQDTZK06tfV6awOVLx9bWmPFWSTECvAmYFpJwUVM2RG2QsAoudniFcVmi0hGbFYQKbWuqas5iPiWspsyOPiLWM1aLBavFGcv5KfXSnqNO4c6QU5kzzw/Pp9AYPVRGY7T01tJOW7wW5pwUj08zPjdSGp5Ncnj2GaMmbyJFabUJXQzW+SulgDofwcOVgz1UbL6ouoYukCopIyFGimJkQiwlTzlV6Gq0raFd0oUlbXXGanHIcv6Y5eKY87NHzM6PaJaH1IcfwFlu8JJ5cXg+hUa5jRYl0VvPKtUOF8UGFqsfOkf/e21YFahDY797hRF4b0VjPuVVuK2Y8jhs/KB31kFKY0fZBSQ0hLamqWpWqwXN8ox6PqWaH9PV58xOPqdZnbBaHLJaHRPrM2jnMM+CIvNi8nwKDS1oNNBoy7azEmivJRJSlyhRolMUax78dJ6xJm0ICsEpzoEvHcXI40qHH4FzShQlxmYo+faxI3SB0NRUqwWhOofqMe3ymPPTI85PD1mdHrKYHtLMjmB1DjTQ5KShzMvD8yk02hqaCtfUjICAR4JHYzGkfndSW5m7s8xMQ1IR67oArB+MpMnTGaRvrqtAx3gCvlBGZUExEkQCURo0Nkho0VBTVzNWsynL2ZTl+Snz2TnL8zPa5RHV9DeE6gjmZ1AtockaRObl5jkVGv8i8eihzn/7O25ffY+qKGj9iJBa3UXXolLjRZGwgw4jAFI+6LpfDF1Kx7By9xYkzbgoHaWPXN31aFsRuhk+WtfrbjVldvqY+fkRs+MvaBZnzM8OOZ8e0s7OoFpAbK1oa/HiJ0xlMt+E51NoAHH5iPMvP0SXh4x2dqDYsb6eAmVZ4hnRti3jorQS984a3sS+P0awVOuoDd4FyjGURUgNZVuiNjitOPv8hNX8lPOzY5bnU1bLc6rZKfOzQ9r5iUUw2jk0C1j8axYQmW89z6/QWPydfPH5RK98cItbD1ccvPIeJbssFoFY23CbUraoUw8MSUlTXgKiLUINccXWSNE4JzZndLMpq+UJs9ljzmfH1Isp58ePqVYz6sUcqjl0lSVvtCuoXpy6jEzmP4rnVmgAdIf/U/7pH53O6xlv+MDutde44vdAt/CUROeotLLuSyjEhtAuqVdTmsUx2sz4+OQzYn1CvXhMtThitTqlWpyYFnGeNYdM5pvyXAsNAP3sb+XfuqUu5ke89sbPeOXeu2xPbkE3oosB9XOIC6rFjPn5CfPTx5yffsn89BHNakr1+FNo5tCcQcyNVTKZfy8vziLae1t377zNzRtvsz25ScEeUgjL7oiqmzKfz1jNT6jm5+jyFOoZNEsTi83LWbSVyfwpePEWU/lQkS3w+/jSE6pj0CW0WTBkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDKZTCaTyWQymUwmk8lkMplMJpPJZDIvD/8PnsnAsLibOdgAAAAASUVORK5CYII=" style="width:34px;height:34px;object-fit:contain;flex-shrink:0;"/>
    <span class="sb-logo-text">LUMERA</span>
  </div>
  <div class="sb-nav">
    {nav_html}
  </div>
  <div class="sb-footer">
    <div class="user-chip">
      <div class="u-avatar">K</div>
      <div>
        <div class="u-name">{user.capitalize()}</div>
        <div class="u-role">administrator</div>
      </div>
    </div>
    <a href="/logout" class="logout-link">LOG OUT</a>
  </div>
</div>

<!-- MAIN -->
<div class="main">
  <div class="topbar">
    <div class="topbar-title">{active.upper()}</div>
    <div class="topbar-right">
      <div class="tb-pill"><span class="online-dot"></span>LIVE</div>
      <div class="tb-pill">localhost:8000</div>
      <button id="mode-toggle" onclick="toggleMode()" title="Toggle light/dark mode"
        style="width:35px;height:35px;border-radius:50%;border:1px solid var(--border2);
        background:transparent;color:var(--muted);cursor:pointer;font-size:15px;
        display:flex;align-items:center;justify-content:center;transition:all .2s;">
        &#9788;
      </button>
      <div class="tb-avatar">K</div>
    </div>
  </div>
  <div class="content">
    {content}
  </div>
</div>

<!-- EMAIL PREVIEW MODAL -->
<div class="modal-overlay" id="emailModal">
  <div class="modal email-modal">
    <h3><i class="fa-solid fa-envelope" style="color:var(--indigo);margin-right:8px"></i>Email Preview</h3>
    <div class="form-field"><label>To</label><input type="text" id="pv-to" readonly/></div>
    <div class="form-field"><label>Subject</label><input type="text" id="pv-subject"/></div>
    <div class="form-field"><label>Body</label><textarea id="pv-body" style="min-height:180px"></textarea></div>
    <div id="pv-status" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="closeEmailModal()">Cancel</button>
      <button class="btn btn-ghost" onclick="regenEmail()"><i class="fa-solid fa-rotate-right"></i> Regen</button>
      <button class="btn btn-primary" id="pv-send-btn" onclick="sendEmail()">Send + Enroll</button>
    </div>
  </div>
</div>

<!-- BULK MODAL -->
<div class="modal-overlay" id="bulkModal">
  <div class="modal">
    <h3><i class="fa-solid fa-paper-plane" style="color:var(--indigo);margin-right:8px"></i>Bulk Send</h3>
    <p style="font-size:12px;color:var(--muted);margin-bottom:16px">
      Sending to <strong id="bulk-count" style="color:var(--text)">0</strong> leads · auto-enroll in Day 3/5 sequence
    </p>
    <div class="form-field"><label>Tone / Instructions (optional)</label>
      <textarea id="bulk-tone" rows="3" placeholder="e.g. Friendly, mention their low reviews..."></textarea>
    </div>
    <div id="bulk-status" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="document.getElementById('bulkModal').classList.remove('open')">Cancel</button>
      <button class="btn btn-primary" id="bulk-btn" onclick="runBulkSend()">Send All</button>
    </div>
  </div>
</div>

<!-- PIPELINE MODAL -->
<div class="modal-overlay" id="pipeModal">
  <div class="modal">
    <h3><i class="fa-solid fa-handshake" style="color:var(--indigo);margin-right:8px"></i>Add Deal</h3>
    <div class="form-field"><label>Business Name *</label><input type="text" id="pipe-biz" placeholder="Nashville Roofing Co"/></div>
    <div class="form-field"><label>Contact Name</label><input type="text" id="pipe-contact" placeholder="John Smith"/></div>
    <div class="form-field"><label>Email</label><input type="email" id="pipe-email" placeholder="john@example.com"/></div>
    <div class="form-field"><label>Deal Value ($)</label><input type="number" id="pipe-value" placeholder="1497"/></div>
    <div class="form-field"><label>Stage</label>
      <select id="pipe-stage">
        <option value="prospect">Prospect</option>
        <option value="qualified">Qualified</option>
        <option value="proposal">Proposal Sent</option>
        <option value="closed">Closed Won</option>
      </select>
    </div>
    <div class="form-field"><label>Notes</label><textarea id="pipe-notes" rows="2"></textarea></div>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="document.getElementById('pipeModal').classList.remove('open')">Cancel</button>
      <button class="btn btn-primary" onclick="savePipeline()">Add Deal</button>
    </div>
  </div>
</div>

<!-- CLIENT MODAL -->
<div class="modal-overlay" id="clientModal">
  <div class="modal">
    <h3><i class="fa-solid fa-user-plus" style="color:var(--indigo);margin-right:8px"></i>Add Client</h3>
    <div class="form-field"><label>Username *</label><input type="text" id="cl-user" placeholder="roofing_client"/></div>
    <div class="form-field"><label>Password *</label><input type="text" id="cl-pass" placeholder="secure password"/></div>
    <div class="form-field"><label>Niche (CSV keyword or *)</label><input type="text" id="cl-niche" placeholder="roofing"/></div>
    <div class="form-field"><label>Business Name</label><input type="text" id="cl-biz" placeholder="Nashville Roofing Co"/></div>
    <div class="form-field"><label>Email</label><input type="email" id="cl-email" placeholder="client@example.com"/></div>
    <div class="form-field"><label>Monthly Fee ($)</label><input type="number" id="cl-monthly" value="497"/></div>
    <div class="form-field"><label>Setup Fee ($)</label><input type="number" id="cl-setup" value="1000"/></div>
    <div class="modal-btns">
      <button class="btn btn-ghost" onclick="document.getElementById('clientModal').classList.remove('open')">Cancel</button>
      <button class="btn btn-primary" onclick="saveClient()">Add Client</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let currentLead=null, selectedLeads={{}};

function toast(msg,type=''){{
  const t=document.getElementById('toast');
  t.textContent=msg; t.className='toast show'+(type?' '+type:'');
  setTimeout(()=>t.className='toast',3000);
}}
function filterLeads(){{
  const q=(document.getElementById('searchBox')?.value||'').toLowerCase();
  const h=document.getElementById('heatFilter')?.value||'all';
  // Support both card and table row layouts
  document.querySelectorAll('[data-heat]').forEach(el=>{{
    const mQ=!q||el.textContent.toLowerCase().includes(q);
    const mH=h==='all'||el.dataset.heat===h;
    el.style.display=(mQ&&mH)?'':'none';
  }});
}}
function setHeat(val){{
  document.getElementById('heatFilter').value=val;
  document.querySelectorAll('.heat-btn').forEach(b=>b.classList.toggle('active',b.dataset.heat===val));
  filterLeads();
}}
function toggleRow(idx,lead){{
  const card=document.getElementById('card-'+idx)||document.getElementById('row-'+idx);
  const cb=document.getElementById('cb-'+idx)||document.getElementById('sel-'+idx);
  if(selectedLeads[idx]){{delete selectedLeads[idx];card?.classList.remove('sel');if(cb)cb.checked=false;}}
  else{{selectedLeads[idx]=lead;card?.classList.add('sel');if(cb)cb.checked=true;}}
  updateBulkBar();
}}
function toggleAll(src){{
  document.querySelectorAll('[data-idx]:not([style*="none"])').forEach(el=>{{
    const idx=el.dataset.idx,cb=document.getElementById('cb-'+idx)||document.getElementById('sel-'+idx);
    if(src.checked){{selectedLeads[idx]=JSON.parse(el.dataset.lead);el.classList.add('sel');if(cb)cb.checked=true;}}
    else{{delete selectedLeads[idx];el.classList.remove('sel');if(cb)cb.checked=false;}}
  }});
  updateBulkBar();
}}
function updateBulkBar(){{
  const n=Object.keys(selectedLeads).length;
  const bar=document.getElementById('bulkBar');
  const cnt=document.getElementById('selCount');
  if(bar)bar.className='bulk-bar'+(n>0?' show':'');
  if(cnt)cnt.textContent=n+' lead'+(n!==1?'s':'')+' selected';
}}
function clearSel(){{
  selectedLeads={{}};
  document.querySelectorAll('.sel').forEach(r=>r.classList.remove('sel'));
  document.querySelectorAll('input[type=checkbox]').forEach(cb=>cb.checked=false);
  updateBulkBar();
}}
function genEmailB64(idx, b64){{
  try {{
    const json = atob(b64);
    genEmail(idx, json.replace(/&quot;/g,'"'));
  }} catch(e) {{
    toast('Failed to parse lead data','err');
  }}
}}
async function genEmail(idx,leadJson){{
  const lead=typeof leadJson==='string'?JSON.parse(leadJson):leadJson;currentLead=lead;
  const btn=document.getElementById('gen-'+idx);
  if(btn){{btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i>';btn.disabled=true;}}
  try{{
    const res=await fetch('/api/generate-email',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(lead)}});
    const data=await res.json();
    if(!res.ok)throw new Error(data.detail);
    document.getElementById('pv-to').value=lead.Email||'';
    document.getElementById('pv-subject').value=data.subject;
    document.getElementById('pv-body').value=data.body;
    document.getElementById('pv-status').textContent='';
    document.getElementById('emailModal').classList.add('open');
  }}catch(e){{toast('Generate failed: '+e.message,'err');}}
  finally{{if(btn){{btn.innerHTML='<i class="fa-solid fa-envelope"></i>';btn.disabled=false;}}}}
}}
async function regenEmail(){{
  if(!currentLead)return;
  document.getElementById('pv-status').textContent='Regenerating...';
  try{{
    const res=await fetch('/api/generate-email',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(currentLead)}});
    const d=await res.json();
    document.getElementById('pv-subject').value=d.subject;
    document.getElementById('pv-body').value=d.body;
    document.getElementById('pv-status').textContent='';
  }}catch{{document.getElementById('pv-status').textContent='Failed';}}
}}
async function sendEmail(){{
  const to=document.getElementById('pv-to').value.trim();
  const subject=document.getElementById('pv-subject').value.trim();
  const body=document.getElementById('pv-body').value.trim();
  if(!to||!subject||!body){{toast('Fill all fields','err');return;}}
  const btn=document.getElementById('pv-send-btn');
  btn.disabled=true;btn.textContent='Sending...';
  try{{
    const res=await fetch('/api/send-email',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{to,subject,body,lead:currentLead}})}});
    const d=await res.json();
    if(!res.ok)throw new Error(d.detail);
    closeEmailModal();toast('Sent + enrolled in sequence','ok');
  }}catch(e){{document.getElementById('pv-status').textContent='&#10060; '+e.message;}}
  finally{{btn.disabled=false;btn.textContent='Send + Enroll';}}
}}
function closeEmailModal(){{document.getElementById('emailModal').classList.remove('open');currentLead=null;}}
async function runBulkSend(){{
  const leads=Object.values(selectedLeads);
  const tone=document.getElementById('bulk-tone').value.trim();
  const btn=document.getElementById('bulk-btn');
  const status=document.getElementById('bulk-status');
  btn.disabled=true;
  let sent=0,failed=0;
  for(const lead of leads){{
    status.textContent=`Sending ${{sent+failed+1}} of ${{leads.length}}...`;
    try{{
      const g=await fetch('/api/generate-email',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{...lead,_tone:tone}})}});
      const gd=await g.json();if(!g.ok)throw new Error();
      const to=lead.Email||'';
      if(!to||!to.includes('@')||to.includes('example.com')||to.includes('None')){{failed++;continue;}}
      const s=await fetch('/api/send-email',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{to,subject:gd.subject,body:gd.body,lead}})}});
      if(!s.ok)throw new Error();sent++;
    }}catch{{failed++;}}
    await new Promise(r=>setTimeout(r,400));
  }}
  status.textContent=`Done: ${{sent}} sent, ${{failed}} skipped`;
  btn.disabled=false;clearSel();toast(`${{sent}} sent, ${{failed}} skipped`,'ok');
}}
async function markReplied(email){{
  await fetch('/api/outreach/replied',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email}})}});
  toast('Marked as replied','ok');setTimeout(()=>location.reload(),600);
}}
async function markUnsub(email){{
  await fetch('/api/outreach/unsubscribed',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email}})}});
  toast('Marked as unsubscribed');setTimeout(()=>location.reload(),600);
}}
async function savePipeline(){{
  const b={{business:document.getElementById('pipe-biz').value.trim(),
    contact:document.getElementById('pipe-contact').value.trim(),
    email:document.getElementById('pipe-email').value.trim(),
    value:parseFloat(document.getElementById('pipe-value').value)||0,
    stage:document.getElementById('pipe-stage').value,
    notes:document.getElementById('pipe-notes').value.trim()}};
  if(!b.business){{toast('Business name required','err');return;}}
  const res=await fetch('/api/pipeline',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});
  if(res.ok){{document.getElementById('pipeModal').classList.remove('open');toast('Deal added','ok');setTimeout(()=>location.reload(),600);}}
  else toast('Failed','err');
}}
async function saveClient(){{
  const b={{username:document.getElementById('cl-user').value.trim(),
    password:document.getElementById('cl-pass').value.trim(),
    niche:document.getElementById('cl-niche').value.trim()||'*',
    business:document.getElementById('cl-biz').value.trim(),
    email:document.getElementById('cl-email').value.trim(),
    monthly_fee:parseFloat(document.getElementById('cl-monthly').value)||497,
    setup_fee:parseFloat(document.getElementById('cl-setup').value)||1000}};
  if(!b.username||!b.password){{toast('Username + password required','err');return;}}
  const res=await fetch('/api/clients',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});
  if(res.ok){{document.getElementById('clientModal').classList.remove('open');toast('Client added','ok');setTimeout(()=>location.reload(),600);}}
  else{{const d=await res.json();toast(d.detail||'Failed','err');}}
}}
async function deleteClient(username){{
  if(!confirm('Remove client '+username+'?'))return;
  await fetch('/api/clients/'+username,{{method:'DELETE'}});
  toast('Client removed');setTimeout(()=>location.reload(),600);
}}
async function runScraper(){{
  const btn=document.getElementById('scraper-btn');
  btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Running...';
  const res=await fetch('/api/system/scrape',{{method:'POST'}});
  const d=await res.json();
  btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-play"></i> Run Scraper Now';
  toast(d.message||'Done','ok');
}}
function toggleMode(){{
  const body = document.body;
  const btn  = document.getElementById('mode-toggle');
  if(body.classList.contains('light')){{
    body.classList.remove('light');
    localStorage.setItem('lumera_mode','dark');
    if(btn) btn.innerHTML='&#9788;';
  }} else {{
    body.classList.add('light');
    localStorage.setItem('lumera_mode','light');
    if(btn) btn.innerHTML='&#9790;';
  }}
}}
// Apply saved mode on load
(function(){{
  const saved = localStorage.getItem('lumera_mode');
  if(saved === 'light'){{
    document.body.classList.add('light');
    const btn = document.getElementById('mode-toggle');
    if(btn) btn.innerHTML='&#9790;';
  }}
}})();
async function sendAllPending(){{
  const btn=document.getElementById('send-pending-btn');
  const status=document.getElementById('pending-status');
  if(!btn)return;
  if(!confirm('Generate and send emails to ALL leads not yet contacted?'))return;
  btn.disabled=true;
  btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Sending...';
  if(status)status.textContent='Working...';
  try{{
    const res=await fetch('/api/send-all-pending',{{method:'POST'}});
    const d=await res.json();
    if(status)status.textContent=d.message||'Done';
    toast(d.message||'Done','ok');
    setTimeout(()=>location.reload(),1500);
  }}catch(e){{
    toast('Failed: '+e.message,'err');
    if(status)status.textContent='Failed';
  }}finally{{
    btn.disabled=false;
    btn.innerHTML='<i class="fa-solid fa-paper-plane"></i> Send All Pending';
  }}
}}
async function runFollowups(){{
  const btn=document.getElementById('followup-btn');
  btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Running...';
  const res=await fetch('/cron/followups',{{method:'POST'}});
  const d=await res.json();
  btn.disabled=false;btn.innerHTML='<i class="fa-solid fa-play"></i> Send Due Follow-ups';
  toast(`Follow-ups: ${{d.sent}} sent, ${{d.failed}} failed`,'ok');
}}
</script>
</body>
</html>"""

# ─────────────────────────────────────────────
# METRIC CARD HELPER
# ─────────────────────────────────────────────
def mcard(icon, label, value, delta="", icon_color=""):
    return f"""<div class="metric-card">
      <div class="m-icon" style="{'background:'+icon_color if icon_color else ''}">{icon}</div>
      <div class="m-label">{label}</div>
      <div class="m-value">{value}</div>
      {'<div class="m-delta">'+delta+'</div>' if delta else ''}
    </div>"""

# ─────────────────────────────────────────────
# LOGIN
# ─────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    if get_current_user(request): return RedirectResponse("/overview")
    err = f'<p style="color:var(--red);font-size:12px;margin-top:12px;text-align:center">{error}</p>' if error else ""
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8"/>
<title>Lumera · Login</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@600;700;800&display=swap" rel="stylesheet"/>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
:root{{--black:#080808;--surface:#111111;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.14);
  --text:#ffffff;--muted:rgba(255,255,255,0.45);--indigo:#6366f1;--blue:#3b82f6;--red:#f43f5e;
  --grad:linear-gradient(135deg,#3b82f6,#6366f1);--font:'Montserrat',sans-serif}}
body{{font-family:var(--font);background:var(--black);color:var(--text);min-height:100vh;
  display:flex;align-items:center;justify-content:center;
  background-image:url("{NOISE_SVG}");background-size:200px;background-repeat:repeat;background-blend-mode:overlay;
  background-image:url("{NOISE_SVG}"),radial-gradient(ellipse,rgba(99,102,241,0.1) 0%,transparent 70%);}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:44px 40px;width:100%;max-width:400px;}}
.logo-wrap{{display:flex;align-items:center;gap:10px;margin-bottom:28px;}}
.logo-icon{{width:38px;height:38px;border-radius:12px;background:var(--grad);display:flex;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(99,102,241,0.45);}}
.logo-text{{font-size:16px;font-weight:800;background:var(--grad);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}}
label{{display:block;font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:6px;}}
input{{width:100%;background:var(--black);border:1px solid var(--border2);border-radius:10px;padding:11px 14px;color:var(--text);font-family:var(--font);font-size:13px;outline:none;margin-bottom:14px;}}
input:focus{{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(99,102,241,.12);}}
.btn{{width:100%;padding:12px;background:var(--grad);color:white;border:none;border-radius:10px;font-family:var(--font);font-weight:700;font-size:13px;cursor:pointer;box-shadow:0 4px 14px rgba(99,102,241,0.35);}}
.btn:hover{{opacity:.9;}}
</style></head><body>
<div class="card">
  <div class="logo-wrap">
    <img src="data:image/png;base64,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" style="width:38px;height:38px;object-fit:contain;border-radius:10px;"/>
    <span class="logo-text">LUMERA</span>
  </div>
  <form method="post" action="/login">
    <label>Username</label><input type="text" name="username" autocomplete="username" required/>
    <label>Password</label><input type="password" name="password" autocomplete="current-password" required/>
    <button class="btn" type="submit">Access Dashboard &rarr;</button>
  </form>{err}
</div></body></html>""")

@app.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    from datetime import timedelta
    expires = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if username == ADMIN_USER and password == ADMIN_PASS:
        token = secrets.token_hex(32)
        db_run("INSERT OR REPLACE INTO sessions(token,username,expires_at) VALUES(?,?,?)",
               (token, username, expires))
        resp = RedirectResponse("/overview", status_code=303)
        resp.set_cookie("lumera_token", token, httponly=True, max_age=86400*7)
        return resp
    clients = db_query("SELECT * FROM clients WHERE username=?", (username,))
    if clients and clients[0]["password"] == password:
        token = secrets.token_hex(32)
        db_run("INSERT OR REPLACE INTO sessions(token,username,expires_at) VALUES(?,?,?)",
               (token, username, expires))
        resp = RedirectResponse("/client-home", status_code=303)
        resp.set_cookie("lumera_token", token, httponly=True, max_age=86400*7)
        return resp
    return RedirectResponse("/login?error=Invalid+credentials", status_code=303)

@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get("lumera_token")
    if token:
        db_run("DELETE FROM sessions WHERE token=?", (token,))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("lumera_token")
    return resp

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/overview" if get_current_user(request) else "/login")


# ─────────────────────────────────────────────
# OVERVIEW
# ─────────────────────────────────────────────
@app.get("/overview", response_class=HTMLResponse)
def overview(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    leads=load_all_leads(); outreach=get_all_outreach()
    bookings=get_all_bookings(); pipeline=get_pipeline(); clients=get_clients()

    total=len(leads); hot=sum(1 for l in leads if l["_heat"]=="hot")
    enrolled=len(outreach); replied=sum(1 for r in outreach if r["replied"])
    total_book=len(bookings); mrr=sum(c.get("monthly_fee",0) for c in clients if c.get("status","active")=="active")

    events=get_upcoming_events(5)
    events_html="".join(f"""<div class="cal-event">
      <div class="cal-dot"></div>
      <div><div class="cal-title">{e['summary']}</div><div class="cal-time">{e['display']}</div></div>
    </div>""" for e in events) or '<div class="empty-state">No upcoming events</div>'

    recent_rows="".join(f"""<tr>
      <td class="bold">{r.get('business','—')}</td>
      <td><span class="badge b-{r.get('status','sent')}">{r.get('status','sent').replace('_',' ')}</span></td>
      <td style="color:var(--muted);font-size:11px">{r.get('enrolled_at','')[:10]}</td>
    </tr>""" for r in outreach[:5]) or '<tr><td colspan="3" class="empty-state">No outreach yet</td></tr>'

    content = f"""
    <div class="page-hdr">
      <div><div class="page-title">Good morning, Kory &#9728;</div>
      <div class="page-sub">{datetime.now().strftime("%A, %B %d %Y")} &nbsp;·&nbsp; lumera lead engine</div></div>
    </div>
    <div class="metrics-grid">
      {mcard('<i class="fa-solid fa-crosshairs"></i>','Total Leads',total,'all niches')}
      {mcard('<i class="fa-solid fa-fire"></i>','Hot Leads',hot,'score 2+','linear-gradient(135deg,#f43f5e,#f97316)')}
      {mcard('<i class="fa-solid fa-paper-plane"></i>','In Sequence',enrolled,'enrolled')}
      {mcard('<i class="fa-solid fa-reply"></i>','Replied',replied,f'{round(replied/max(enrolled,1)*100,1)}% reply rate','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-calendar-check"></i>','Bookings',total_book,'strategy calls')}
      {mcard('<i class="fa-solid fa-dollar-sign"></i>','MRR',f'${mrr:,.0f}','monthly recurring','linear-gradient(135deg,#22c55e,#16a34a)')}
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-header"><div class="card-title">Upcoming Calls</div>
          <a href="/calendar" style="font-size:11px;color:var(--muted)">View all &rarr;</a></div>
        {events_html}
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Recent Outreach</div>
          <a href="/outreach" style="font-size:11px;color:var(--muted)">View all &rarr;</a></div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Business</th><th>Status</th><th>Date</th></tr></thead>
          <tbody>{recent_rows}</tbody>
        </table></div>
      </div>
    </div>"""
    return HTMLResponse(shell(content, "overview", user))


# ─────────────────────────────────────────────
# CALENDAR
# ─────────────────────────────────────────────
@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    events = get_upcoming_events(20)
    events_html = "".join(f"""<div class="cal-event">
      <div class="cal-dot"></div>
      <div style="flex:1">
        <div class="cal-title">{e['summary']}</div>
        <div class="cal-time">{e['display']}</div>
      </div>
    </div>""" for e in events) or '<div class="empty-state">No upcoming events</div>'
    book_url = "https://app.lumeraautomation.com/book"
    content = f"""
    <div class="page-hdr"><div>
      <div class="page-title">Calendar</div>
      <div class="page-sub">Next 20 events from Google Calendar</div>
    </div></div>
    <div class="grid-2">
      <div class="card">
        <div class="card-header"><div class="card-title">Upcoming Strategy Calls</div>
          <span class="badge b-active">{len(events)} events</span></div>
        {events_html}
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Booking Page</div></div>
        <p style="font-size:12px;color:var(--muted);margin-bottom:14px">Share this link in your outreach emails as the CTA</p>
        <div style="background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:10px;padding:12px 14px;margin-bottom:14px">
          <a href="{book_url}" target="_blank" style="font-size:12px">{book_url}</a>
        </div>
        <button class="btn btn-ghost" onclick="navigator.clipboard.writeText('{book_url}');toast('Copied!','ok')">
          <i class="fa-solid fa-copy"></i> Copy Link
        </button>
        <div style="margin-top:20px;padding-top:20px;border-top:1px solid var(--border)">
          <a href="{book_url}" target="_blank" class="btn btn-primary" style="display:inline-block">
            Open Booking Page <i class="fa-solid fa-arrow-up-right-from-square"></i>
          </a>
        </div>
      </div>
    </div>"""
    return HTMLResponse(shell(content, "calendar", user))


# ─────────────────────────────────────────────
# ANALYTICS
# ─────────────────────────────────────────────
@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    leads=load_all_leads(); outreach=get_all_outreach(); bookings=get_all_bookings()
    total=len(leads); hot=sum(1 for l in leads if l["_heat"]=="hot")
    warm=sum(1 for l in leads if l["_heat"]=="warm")
    enrolled=len(outreach); replied=sum(1 for r in outreach if r["replied"])
    reply_rate=round(replied/enrolled*100,1) if enrolled else 0
    booked=len(bookings); book_rate=round(booked/enrolled*100,1) if enrolled else 0

    niche_map={}
    for l in leads: niche_map[l.get("_niche","Other")]=niche_map.get(l.get("_niche","Other"),0)+1

    niche_rows="".join(f"""<tr>
      <td class="bold">{niche}</td>
      <td style="color:var(--text);font-weight:700">{count}</td>
      <td style="width:40%">
        <div style="display:flex;align-items:center;gap:8px">
          <div style="flex:1;height:4px;background:rgba(255,255,255,.06);border-radius:2px">
            <div style="width:{int(count/max(total,1)*100)}%;height:100%;background:var(--grad);border-radius:2px"></div>
          </div>
          <span style="font-size:11px;color:var(--muted)">{int(count/max(total,1)*100)}%</span>
        </div>
      </td>
    </tr>""" for niche,count in sorted(niche_map.items(),key=lambda x:-x[1]))

    f1=sum(1 for r in outreach if r.get("step",1)>=2)
    f2=sum(1 for r in outreach if r.get("step",1)>=3)
    unsub=sum(1 for r in outreach if r.get("unsubscribed"))

    funnel_items=[("Initial Sent",enrolled,"var(--grad)"),("Follow-up 1",f1,"var(--indigo)"),
                  ("Follow-up 2",f2,"var(--blue)"),("Replied",replied,"var(--green)"),
                  ("Booked",booked,"var(--green)"),("Unsubscribed",unsub,"var(--red)")]

    funnel_html="".join(f"""<div class="funnel-row">
      <div class="funnel-label"><span>{label}</span><span>{val}</span></div>
      <div class="funnel-track"><div class="funnel-fill" style="width:{round(val/max(enrolled,1)*100)}%;background:{color}"></div></div>
    </div>""" for label,val,color in funnel_items)

    content = f"""
    <div class="page-hdr"><div>
      <div class="page-title">Analytics</div>
      <div class="page-sub">Lead generation + outreach performance</div>
    </div></div>
    <div class="metrics-grid">
      {mcard('<i class="fa-solid fa-crosshairs"></i>','Total Leads',total,'all niches')}
      {mcard('<i class="fa-solid fa-fire"></i>','Hot Leads',hot,'score 2+','linear-gradient(135deg,#f43f5e,#f97316)')}
      {mcard('<i class="fa-solid fa-temperature-half"></i>','Warm Leads',warm,'score 1','linear-gradient(135deg,#f59e0b,#f97316)')}
      {mcard('<i class="fa-solid fa-paper-plane"></i>','Enrolled',enrolled,'in sequence')}
      {mcard('<i class="fa-solid fa-reply"></i>','Reply Rate',f'{reply_rate}%',f'{replied} replied','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-calendar-check"></i>','Book Rate',f'{book_rate}%',f'{booked} booked','linear-gradient(135deg,#22c55e,#16a34a)')}
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="card-header"><div class="card-title">Leads by Niche</div></div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Niche</th><th>Leads</th><th>Share</th></tr></thead>
          <tbody>{niche_rows or '<tr><td colspan="3" class="empty-state">No leads yet</td></tr>'}</tbody>
        </table></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">Sequence Funnel</div></div>
        {funnel_html}
      </div>
    </div>"""
    return HTMLResponse(shell(content, "analytics", user))


# ─────────────────────────────────────────────
# SALES
# ─────────────────────────────────────────────
@app.get("/sales", response_class=HTMLResponse)
def sales_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    pipeline=get_pipeline()
    stages=["prospect","qualified","proposal","closed"]
    sc={s:sum(1 for p in pipeline if p.get("stage")==s) for s in stages}
    closed_val=sum(p.get("value",0) for p in pipeline if p.get("stage")=="closed")
    pipe_val=sum(p.get("value",0) for p in pipeline)

    rows="".join(f"""<tr>
      <td class="bold">{p.get('business','—')}</td>
      <td style="color:var(--muted);font-size:12px">{p.get('contact','—')}</td>
      <td><a href="mailto:{p.get('email','')}" style="font-size:12px">{p.get('email','—')}</a></td>
      <td style="color:var(--green);font-weight:700">${p.get('value',0):,.0f}</td>
      <td><span class="badge b-{p.get('stage','prospect')}">{p.get('stage','prospect')}</span></td>
      <td style="color:var(--muted);font-size:11px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{p.get('notes','')}</td>
      <td style="color:var(--muted);font-size:11px">{p.get('created_at','')[:10]}</td>
    </tr>""" for p in pipeline)

    content = f"""
    <div class="page-hdr">
      <div><div class="page-title">Sales Pipeline</div>
      <div class="page-sub">Track deals from prospect to close</div></div>
      <button class="btn btn-primary" onclick="document.getElementById('pipeModal').classList.add('open')">
        <i class="fa-solid fa-plus"></i> Add Deal
      </button>
    </div>
    <div class="metrics-grid">
      {mcard('<i class="fa-solid fa-binoculars"></i>','Prospects',sc['prospect'])}
      {mcard('<i class="fa-solid fa-check-double"></i>','Qualified',sc['qualified'])}
      {mcard('<i class="fa-solid fa-file-contract"></i>','Proposals',sc['proposal'])}
      {mcard('<i class="fa-solid fa-trophy"></i>','Closed Won',sc['closed'],'deals won','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-dollar-sign"></i>','Closed Rev',f'${closed_val:,.0f}','collected','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-funnel-dollar"></i>','Pipeline Val',f'${pipe_val:,.0f}','total')}
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">All Deals</div>
        <span class="badge b-active">{len(pipeline)} deals</span></div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Business</th><th>Contact</th><th>Email</th><th>Value</th><th>Stage</th><th>Notes</th><th>Added</th></tr></thead>
        <tbody>{rows or '<tr><td colspan="7" class="empty-state">No deals yet. Add your first deal.</td></tr>'}</tbody>
      </table></div>
    </div>"""
    return HTMLResponse(shell(content, "sales", user))


# ─────────────────────────────────────────────
# LEADS
# ─────────────────────────────────────────────
@app.get("/leads", response_class=HTMLResponse)
def leads_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    leads = load_all_leads()
    total = len(leads)
    hot   = sum(1 for l in leads if l["_heat"] == "hot")
    warm  = sum(1 for l in leads if l["_heat"] == "warm")

    niche_map = {}
    for l in leads:
        niche_map.setdefault(l["_niche"], []).append(l)

    import base64 as _b64

    def need_label(score):
        if score >= 4: return ("HIGH NEED",   "#f43f5e", "#f43f5e22")
        if score >= 3: return ("HIGH NEED",   "#f97316", "#f9731622")
        if score >= 2: return ("MEDIUM NEED", "#f59e0b", "#f59e0b22")
        return              ("LOW NEED",    "#6366f1", "#6366f122")

    def signal_tags(row, score):
        tags = []
        problem = str(row.get("Problem","")).lower()
        phone   = str(row.get("Phone","")).strip()
        website = str(row.get("Website","")).lower()
        reviews = str(row.get("Reviews","")).strip()
        rating  = str(row.get("Rating","")).strip()
        hours   = str(row.get("Hours","")).lower()

        if any(k in problem for k in ["no website","no online","no booking"]): tags.append(("No Website","#f43f5e","#f43f5e18"))
        if any(k in problem for k in ["low reviews","few reviews","limited reviews","no reviews"]): tags.append(("Few Reviews","#f97316","#f9731618"))
        if phone and phone not in ["—","","nan","None"]: tags.append(("Phone Listed","#6366f1","#6366f118"))
        if any(k in problem for k in ["phone","call volume","call-dependent","no website"]): tags.append(("High Call Volume","#f43f5e","#f43f5e18"))
        if website in ["none listed","none","n/a","","nan"]: tags.append(("Phone-Dependent","#f97316","#f9731618"))
        if any(k in problem for k in ["limited hours","after hours","closes at","closed weekends"]): tags.append(("Limited Hours","#8b5cf6","#8b5cf618"))
        if any(k in problem for k in ["busy","high demand","high volume","active","growing"]): tags.append(("High Demand","#22c55e","#22c55e18"))
        if score >= 3: tags.append(("Active & Growing","#22c55e","#22c55e18"))

        return "".join(
            f'<span style="font-size:10px;font-weight:700;padding:3px 9px;border-radius:20px;background:{bg};color:{fg};border:1px solid {fg}33;white-space:nowrap">{label}</span>'
            for label, fg, bg in tags[:5]
        )

    def score_gauge(score):
        """Circular SVG gauge like LeadRadar."""
        label, color, _ = need_label(score)
        # Map score 0-5 to 0-100
        pct = min(int(score / 5 * 100), 100)
        # Display score as 0-100 like LeadRadar
        display = pct
        r = 28
        circ = 2 * 3.14159 * r
        dash = circ * pct / 100
        gap  = circ - dash
        return f'''<div style="display:flex;flex-direction:column;align-items:center;gap:4px">
          <svg width="72" height="72" viewBox="0 0 72 72">
            <circle cx="36" cy="36" r="{r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="5"/>
            <circle cx="36" cy="36" r="{r}" fill="none" stroke="{color}" stroke-width="5"
              stroke-dasharray="{dash:.1f} {gap:.1f}" stroke-linecap="round"
              transform="rotate(-90 36 36)" style="transition:stroke-dasharray .6s ease"/>
            <text x="36" y="40" text-anchor="middle" font-family="Montserrat,sans-serif"
              font-size="14" font-weight="800" fill="{color}">{display}</text>
          </svg>
          <div style="font-size:9px;font-weight:700;color:{color};text-transform:uppercase;letter-spacing:.06em;text-align:center">{label}</div>
        </div>'''

    niches_html = ""
    import random as _random

    for niche, rows in sorted(niche_map.items()):
        rows_sorted = sorted(rows, key=lambda r: -int(r.get("Score", 0) or 0))
        cards_html = ""

        for row in rows_sorted:
            heat = row.get("_heat", "cold")
            idx  = row.get("_idx", 0)
            try: si = int(row.get("Score", 0))
            except: si = 0

            name    = str(row.get("Name","—"))
            city    = str(row.get("City","—"))
            website = str(row.get("Website","—"))
            problem = str(row.get("Problem","—"))
            email   = re.sub(r'\[\d+\]', '', str(row.get("Email",""))).strip()
            phone   = str(row.get("Phone","")) or "—"
            rating  = str(row.get("Rating","")) or "—"
            owner   = str(row.get("Owner",""))

            has_email = "@" in email and "example.com" not in email and "None" not in email
            has_web   = website.lower() not in ["none listed","none","n/a","","nan"]

            lead_data = {"Name":name,"City":city,"Website":website,"Problem":problem,
                "Email":email,"Phone":phone,"Owner":owner,"Score":si,"_niche":row.get("_niche","")}
            lead_b64  = _b64.b64encode(json.dumps(lead_data).encode()).decode()
            lead_json = json.dumps(lead_data).replace('"', '&quot;')

            tags_html  = signal_tags(row, si)
            gauge_html = score_gauge(si)
            label, color, _ = need_label(si)

            web_link = f'<a href="{website}" target="_blank" style="color:var(--blue);font-size:12px">{website[:30]}...</a>' if has_web else '<span style="color:var(--muted);font-size:12px">No website</span>'
            email_el = f'<a href="mailto:{email}" style="color:var(--blue);font-size:12px">{email}</a>' if has_email else '<span style="color:var(--muted);font-size:12px">—</span>'

            send_btn = (
                f'<button class="btn btn-primary btn-sm" id="gen-{idx}" onclick="genEmailB64({idx},\'{lead_b64}\')" style="width:100%"><i class="fa-solid fa-envelope"></i> Send Email</button>'
                if has_email else
                '<button class="btn btn-ghost btn-sm" disabled style="width:100%;opacity:.4">No Email</button>'
            )

            sel_id = f"sel-{idx}"
            cards_html += f'''
            <div class="lead-card" data-heat="{heat}" data-idx="{idx}" data-lead="{lead_json}" id="card-{idx}">
              <!-- TOP ROW -->
              <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;margin-bottom:12px">
                <div style="flex:1;min-width:0">
                  <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap">
                    <input type="checkbox" id="{sel_id}" onclick="toggleRow({idx},JSON.parse(document.getElementById('card-{idx}').dataset.lead))" style="flex-shrink:0"/>
                    <span style="font-size:15px;font-weight:800;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{name}</span>
                    <span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;background:rgba(99,102,241,.15);color:var(--indigo);border:1px solid rgba(99,102,241,.3)">{niche}</span>
                  </div>
                  <div style="font-size:11px;color:var(--muted);margin-bottom:10px"><i class="fa-solid fa-location-dot" style="margin-right:4px"></i>{city}</div>
                  <div style="display:flex;gap:6px;flex-wrap:wrap">{tags_html}</div>
                </div>
                <div style="flex-shrink:0">{gauge_html}</div>
              </div>
              <!-- INFO GRID -->
              <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;padding:12px;background:rgba(255,255,255,.03);border-radius:10px;border:1px solid var(--border)">
                <div>
                  <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:3px">Phone</div>
                  <div style="font-size:12px;color:var(--text);font-weight:600">{phone}</div>
                </div>
                <div>
                  <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:3px">Website</div>
                  <div>{web_link}</div>
                </div>
                <div>
                  <div style="font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:3px">Email</div>
                  <div>{email_el}</div>
                </div>
              </div>
              <!-- PROBLEM -->
              <div style="font-size:11px;color:var(--muted);margin-bottom:12px;line-height:1.5;padding:8px 12px;background:rgba(255,255,255,.02);border-radius:8px;border-left:2px solid {color}">
                {problem}
              </div>
              <!-- ACTIONS -->
              <div style="display:flex;gap:8px">
                {send_btn}
                <button class="btn btn-ghost btn-sm" onclick="window.open('https://www.google.com/maps/search/{name} {city}','_blank')" style="flex-shrink:0"><i class="fa-solid fa-map-location-dot"></i></button>
              </div>
            </div>'''

        niches_html += f'''
        <div style="margin-bottom:32px">
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:8px">
            <i class="fa-solid fa-layer-group" style="color:var(--indigo)"></i>{niche}
            <span style="color:var(--muted2)">{len(rows)} leads</span>
          </div>
          <div class="leads-grid">{cards_html}</div>
        </div>'''

    content = f"""
    <style>
    .leads-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:14px;}}
    .lead-card{{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:18px 20px;transition:border-color .2s,transform .2s;}}
    .lead-card:hover{{border-color:var(--border2);transform:translateY(-2px);}}
    .lead-card[data-heat="hot"]{{border-left:3px solid #f43f5e;}}
    .lead-card[data-heat="warm"]{{border-left:3px solid #f59e0b;}}
    </style>
    <div class="page-hdr">
      <div><div class="page-title">Leads</div>
      <div class="page-sub">{total} leads &nbsp;·&nbsp; {hot} high need &nbsp;·&nbsp; {warm} medium need</div></div>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn btn-ghost" id="send-pending-btn" onclick="sendAllPending()">
          <i class="fa-solid fa-paper-plane"></i> Send All Pending
        </button>
        <div id="pending-status" style="font-size:11px;color:var(--muted);align-self:center;font-family:monospace"></div>
      </div>
    </div>
    <div class="filter-bar">
      <div class="search-wrap">
        <i class="fa-solid fa-magnifying-glass"></i>
        <input class="search-input" id="searchBox" placeholder="Search leads..." oninput="filterLeads()"/>
      </div>
      <input type="hidden" id="heatFilter" value="all"/>
      <button class="heat-btn active" data-heat="all" onclick="setHeat('all')">All</button>
      <button class="heat-btn" data-heat="hot" onclick="setHeat('hot')"><i class="fa-solid fa-fire"></i> High Need</button>
      <button class="heat-btn" data-heat="warm" onclick="setHeat('warm')">Medium Need</button>
      <button class="heat-btn" data-heat="cold" onclick="setHeat('cold')">Low Need</button>
    </div>
    <div class="bulk-bar" id="bulkBar">
      <span id="selCount">0 selected</span>
      <button class="btn btn-primary btn-sm" onclick="document.getElementById('bulk-count').textContent=Object.keys(selectedLeads).length;document.getElementById('bulkModal').classList.add('open')">
        <i class="fa-solid fa-paper-plane"></i> Bulk Send
      </button>
      <button class="btn btn-ghost btn-sm" onclick="clearSel()">Clear</button>
    </div>
    {niches_html or '<div class="empty-state">No leads. Run your scraper first.</div>'}"""
    return HTMLResponse(shell(content, "leads", user))


# ─────────────────────────────────────────────
# OUTREACH
# ─────────────────────────────────────────────
@app.get("/outreach", response_class=HTMLResponse)
def outreach_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    rows=get_all_outreach()
    total=len(rows); replied=sum(1 for r in rows if r["replied"])
    pending=sum(1 for r in rows if not r["replied"] and not r["unsubscribed"] and r.get("step",1)<3)
    complete=sum(1 for r in rows if r.get("step",1)>=3 and not r["replied"])

    rows_html=""
    for r in rows:
        step=r.get("step",1); status=r.get("status","sent")
        replied_flag=r.get("replied",0); email=r.get("email","")
        dots="".join(f'<div class="dot {"replied" if replied_flag and i<=step else "done" if i<=step else ""}"></div>' for i in range(1,4))
        try:
            ns=datetime.fromisoformat(r.get("next_send_at","")) if r.get("next_send_at") else None
            nxt=ns.strftime("%b %d") if ns else "Done"
        except: nxt="—"
        actions=""
        if not replied_flag and status not in ("replied","unsubscribed"):
            actions=f"""<div style="display:flex;gap:6px">
              <button class="btn btn-green btn-sm" onclick="markReplied('{email}')"><i class="fa-solid fa-check"></i></button>
              <button class="btn btn-danger btn-sm" onclick="markUnsub('{email}')">Unsub</button>
            </div>"""
        rows_html+=f"""<tr>
          <td class="bold">{r.get('business','—')}</td>
          <td style="font-size:11px;color:var(--muted)">{r.get('niche','—')}</td>
          <td><a href="mailto:{email}" style="font-size:12px">{email}</a></td>
          <td><span class="badge b-{status}">{status.replace('_',' ')}</span></td>
          <td><div class="step-dots">{dots}</div></td>
          <td style="font-size:11px;color:var(--muted)">{nxt}</td>
          <td style="font-size:11px;color:var(--muted)">{r.get('enrolled_at','')[:10]}</td>
          <td>{actions}</td>
        </tr>"""

    content = f"""
    <div class="page-hdr"><div>
      <div class="page-title">Outreach Sequences</div>
      <div class="page-sub">Day 1 &rarr; Day 3 &rarr; Day 5 automated follow-up</div>
    </div></div>
    <div class="metrics-grid">
      {mcard('<i class="fa-solid fa-users"></i>','Enrolled',total)}
      {mcard('<i class="fa-solid fa-reply"></i>','Replied',replied,'','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-clock"></i>','Pending',pending,'','linear-gradient(135deg,#f59e0b,#f97316)')}
      {mcard('<i class="fa-solid fa-circle-check"></i>','Complete',complete)}
    </div>
    <div class="card">
      <div class="tbl-wrap"><table>
        <thead><tr><th>Business</th><th>Niche</th><th>Email</th><th>Status</th><th>Sequence</th><th>Next Send</th><th>Enrolled</th><th>Actions</th></tr></thead>
        <tbody>{rows_html or '<tr><td colspan="8" class="empty-state">No leads enrolled. Send emails from Leads tab.</td></tr>'}</tbody>
      </table></div>
    </div>"""
    return HTMLResponse(shell(content, "outreach", user))


# ─────────────────────────────────────────────
# BOOKINGS
# ─────────────────────────────────────────────
@app.get("/bookings", response_class=HTMLResponse)
def bookings_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    bookings=get_all_bookings()
    total=len(bookings)
    upcoming=sum(1 for b in bookings if b["start_time"]>=datetime.now().isoformat() and b["status"]=="confirmed")

    rows=""
    for b in bookings:
        try: dts=datetime.fromisoformat(b["start_time"]).strftime("%a %b %d · %I:%M %p CT")
        except: dts=b["start_time"][:16]
        meet=b.get("meet_link","")
        meet_cell=(f'<a href="{meet}" target="_blank" class="btn btn-green btn-sm"><i class="fa-solid fa-video"></i> Join</a>'
                  if meet else '—')
        rows+=f"""<tr>
          <td class="bold">{b.get('name','—')}</td>
          <td style="color:var(--muted);font-size:12px">{b.get('business','—')}</td>
          <td><a href="mailto:{b.get('email','')}" style="font-size:12px">{b.get('email','—')}</a></td>
          <td style="font-size:12px;color:rgba(255,255,255,.7)">{dts}</td>
          <td>{meet_cell}</td>
          <td><span class="badge b-{b.get('status','confirmed')}">{b.get('status','confirmed')}</span></td>
          <td style="font-size:11px;color:var(--muted)">{b.get('created_at','')[:10]}</td>
        </tr>"""

    book_url="https://app.lumeraautomation.com/book"
    content = f"""
    <div class="page-hdr">
      <div><div class="page-title">Bookings</div>
      <div class="page-sub">{total} total &nbsp;·&nbsp; {upcoming} upcoming</div></div>
      <a href="{book_url}" target="_blank" class="btn btn-primary">
        <i class="fa-solid fa-calendar-plus"></i> Open Booking Page
      </a>
    </div>
    <div class="card" style="display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:18px 24px">
      <div>
        <div style="font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Booking Link</div>
        <a href="{book_url}" target="_blank" style="font-size:12px">{book_url}</a>
      </div>
      <button class="btn btn-ghost btn-sm" onclick="navigator.clipboard.writeText('{book_url}');toast('Copied!','ok')">
        <i class="fa-solid fa-copy"></i> Copy
      </button>
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">All Bookings</div></div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Name</th><th>Business</th><th>Email</th><th>Call Time</th><th>Meet</th><th>Status</th><th>Booked</th></tr></thead>
        <tbody>{rows or '<tr><td colspan="7" class="empty-state">No bookings yet</td></tr>'}</tbody>
      </table></div>
    </div>"""
    return HTMLResponse(shell(content, "bookings", user))


# ─────────────────────────────────────────────
# SYSTEM
# ─────────────────────────────────────────────
@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    csv_files=list(DAILY_LEADS_DIR.glob("*.csv"))
    main_csvs=[f for f in csv_files if "_hot" not in f.name]
    last_run=max((f.stat().st_mtime for f in main_csvs),default=0)
    last_run_str=datetime.fromtimestamp(last_run).strftime("%b %d at %I:%M %p") if last_run else "Never"

    csv_rows=""
    for f in sorted(main_csvs):
        try: count=len(pd.read_csv(f))
        except: count=0
        csv_rows+=f"""<tr>
          <td style="font-size:12px;color:rgba(255,255,255,.7)">{f.name}</td>
          <td style="font-size:12px;color:var(--muted)">{count} leads</td>
          <td style="font-size:11px;color:var(--muted)">{datetime.fromtimestamp(f.stat().st_mtime).strftime("%b %d %H:%M")}</td>
        </tr>"""

    def status_tag(ok): return f'<span class="sys-tag {"sys-ok" if ok else "sys-warn"}">{"CONNECTED" if ok else "NOT SET"}</span>'

    clients = get_clients()
    client_opts = "".join(f'<option value="{c["username"]}">{c.get("business") or c["username"]} ({c.get("niche","—")})</option>' for c in clients)

    content = f"""
    <div class="page-hdr"><div>
      <div class="page-title">System</div>
      <div class="page-sub">Lead engine &nbsp;·&nbsp; API status &nbsp;·&nbsp; data files</div>
    </div></div>

    <!-- LEAD ENGINE CARD -->
    <div class="card" style="margin-bottom:20px;border-color:rgba(99,102,241,.3);background:linear-gradient(135deg,rgba(99,102,241,.06),var(--surface))">
      <div class="card-header">
        <div class="card-title"><i class="fa-solid fa-bolt" style="color:var(--indigo);margin-right:8px"></i>Lead Engine</div>
        <span class="badge b-active">Powered by Perplexity</span>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;margin-bottom:16px">
        <div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:6px">Niche</div>
          <select id="scrape-niche" style="width:100%;background:var(--black);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--font);font-size:13px;outline:none;cursor:pointer">
            <option value="restaurant">Restaurant</option>
            <option value="medspa">MedSpa</option>
            <option value="roofing">Roofing</option>
            <option value="dentist">Dentist</option>
            <option value="chiropractor">Chiropractor</option>
            <option value="HVAC">HVAC</option>
            <option value="landscaping">Landscaping</option>
            <option value="cleaning service">Cleaning Service</option>
            <option value="plumber">Plumber</option>
            <option value="digital marketing agency">Agency</option>
          </select>
        </div>
        <div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:6px">City / State</div>
          <input id="scrape-city" type="text" placeholder="e.g. Hoboken NJ" value="Nashville TN"
            style="width:100%;background:var(--black);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--font);font-size:13px;outline:none"/>
        </div>
        <div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:6px">Leads to pull</div>
          <select id="scrape-count" style="width:100%;background:var(--black);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--font);font-size:13px;outline:none;cursor:pointer">
            <option value="7">7 leads</option>
            <option value="10" selected>10 leads</option>
            <option value="15">15 leads</option>
            <option value="20">20 leads</option>
          </select>
        </div>
        <div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:6px">Client</div>
          <select id="scrape-client" style="width:100%;background:var(--black);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--font);font-size:13px;outline:none;cursor:pointer">
            <option value="">— My own leads —</option>
            {client_opts}
          </select>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <button class="btn btn-primary" id="engine-scrape-btn" onclick="engineScrape()">
          <i class="fa-solid fa-magnifying-glass"></i> Scrape Leads
        </button>
        <button class="btn btn-ghost" id="engine-send-btn" onclick="engineSend()" style="display:none">
          <i class="fa-solid fa-paper-plane"></i> Send Outreach to These Leads
        </button>
        <div id="engine-status" style="font-size:12px;color:var(--muted);font-family:monospace"></div>
      </div>
      <div id="engine-results" style="margin-top:16px;display:none">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted2);margin-bottom:10px">Scraped Leads Preview</div>
        <div class="tbl-wrap"><table>
          <thead><tr><th>Business</th><th>City</th><th>Phone</th><th>Email</th><th>Problem</th><th>Score</th></tr></thead>
          <tbody id="engine-leads-tbody"></tbody>
        </table></div>
      </div>
    </div>

    <div class="grid-2">
      <div>
        <div class="sys-row">
          <div class="sys-info"><h4><i class="fa-solid fa-spider" style="color:var(--indigo);margin-right:8px"></i>Lead Scraper</h4>
          <p>Perplexity AI &nbsp;·&nbsp; Nashville niches &nbsp;·&nbsp; Last run: {last_run_str}</p></div>
          <div style="display:flex;align-items:center;gap:10px">
            <span class="sys-tag sys-ok">READY</span>
            <button class="btn btn-primary btn-sm" id="scraper-btn" onclick="runScraper()"><i class="fa-solid fa-play"></i> Run Now</button>
          </div>
        </div>
        <div class="sys-row">
          <div class="sys-info"><h4><i class="fa-solid fa-rotate" style="color:var(--indigo);margin-right:8px"></i>Follow-up Cron</h4>
          <p>Daily at 9am &nbsp;·&nbsp; Day 3 + Day 5 sequences</p></div>
          <div style="display:flex;align-items:center;gap:10px">
            <span class="sys-tag sys-ok">ACTIVE</span>
            <button class="btn btn-ghost btn-sm" id="followup-btn" onclick="runFollowups()"><i class="fa-solid fa-play"></i> Run Now</button>
          </div>
        </div>
        <div class="sys-row">
          <div class="sys-info"><h4><i class="fa-solid fa-envelope" style="color:var(--indigo);margin-right:8px"></i>Email (Resend)</h4>
          <p>{FROM_EMAIL}</p></div>
          {status_tag(bool(RESEND_API_KEY))}
        </div>
        <div class="sys-row">
          <div class="sys-info"><h4><i class="fa-solid fa-robot" style="color:var(--indigo);margin-right:8px"></i>OpenAI GPT-4o-mini</h4>
          <p>Email generation</p></div>
          {status_tag(bool(OPENAI_API_KEY))}
        </div>
        <div class="sys-row">
          <div class="sys-info"><h4><i class="fa-brands fa-google" style="color:var(--indigo);margin-right:8px"></i>Google Calendar</h4>
          <p>Service account &nbsp;·&nbsp; {(CALENDAR_ID or '')[:24]}...</p></div>
          {status_tag(bool(SERVICE_ACCOUNT_JSON))}
        </div>
      </div>
      <div>
        <div class="card" style="margin-bottom:18px">
          <div class="card-header"><div class="card-title">Upload Leads CSV</div></div>
          <p style="font-size:12px;color:var(--muted);margin-bottom:14px">Upload any CSV file (Apollo, Google, custom) directly into your leads dashboard.</p>
          <div id="upload-zone" style="border:2px dashed var(--border2);border-radius:10px;padding:28px;text-align:center;cursor:pointer;transition:border-color .2s"
               onclick="document.getElementById('csv-file-input').click()"
               ondragover="event.preventDefault();this.style.borderColor='var(--indigo)'"
               ondragleave="this.style.borderColor='var(--border2)'"
               ondrop="handleDrop(event)">
            <i class="fa-solid fa-cloud-arrow-up" style="font-size:28px;color:var(--muted);margin-bottom:10px;display:block"></i>
            <div style="font-size:13px;font-weight:600;color:var(--muted2)">Click or drag &amp; drop a CSV file</div>
            <div style="font-size:11px;color:var(--muted2);margin-top:4px">Apollo, Google Sheets, custom exports</div>
          </div>
          <input type="file" id="csv-file-input" accept=".csv" style="display:none" onchange="uploadCSV(this.files[0])"/>
          <div id="upload-status" style="margin-top:12px;font-size:12px;color:var(--muted);font-family:monospace;min-height:18px"></div>
          <div style="margin-top:14px">
            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted2);margin-bottom:8px">Niche label for this file</div>
            <input type="text" id="upload-niche" placeholder="e.g. Apollo Agencies, Google Leads, Custom" 
              style="width:100%;background:var(--black);border:1px solid var(--border2);border-radius:8px;padding:9px 12px;color:var(--text);font-family:var(--font);font-size:12px;outline:none"/>
          </div>
        </div>
        <div class="card">
          <div class="card-header">
            <div class="card-title">Lead Files</div>
            <span class="badge b-active">{len(main_csvs)} files</span>
          </div>
          <div class="tbl-wrap"><table>
            <thead><tr><th>File</th><th>Leads</th><th>Modified</th><th></th></tr></thead>
            <tbody>{csv_rows or '<tr><td colspan="4" class="empty-state">No CSV files yet</td></tr>'}</tbody>
          </table></div>
        </div>
      </div>
    </div>
    <script>
    function handleDrop(e){{
      e.preventDefault();
      document.getElementById('upload-zone').style.borderColor='var(--border2)';
      const file = e.dataTransfer.files[0];
      if(file && file.name.endsWith('.csv')) uploadCSV(file);
      else toast('Please drop a CSV file','err');
    }}
    async function uploadCSV(file){{
      if(!file) return;
      const niche = document.getElementById('upload-niche').value.trim() || 'Uploaded Leads';
      const status = document.getElementById('upload-status');
      status.textContent = 'Uploading...';
      const form = new FormData();
      form.append('file', file);
      form.append('niche', niche);
      try{{
        const res = await fetch('/api/upload-csv', {{method:'POST', body:form}});
        const d = await res.json();
        if(!res.ok) throw new Error(d.detail||'Upload failed');
        status.textContent = '✅ ' + d.message;
        toast(d.message, 'ok');
        setTimeout(()=>location.reload(), 1500);
      }}catch(e){{
        status.textContent = '❌ ' + e.message;
        toast(e.message, 'err');
      }}
    }}
    </script>"""
    return HTMLResponse(shell(content, "system", user))


# ─────────────────────────────────────────────
# REVENUE
# ─────────────────────────────────────────────
@app.get("/revenue", response_class=HTMLResponse)
def revenue_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    clients=get_clients()
    active=[c for c in clients if c.get("status","active")=="active"]
    mrr=sum(c.get("monthly_fee",0) for c in active)
    arr=mrr*12; setup=sum(c.get("setup_fee",0) for c in clients)
    total=mrr+setup
    pipeline=get_pipeline()
    closed_val=sum(p.get("value",0) for p in pipeline if p.get("stage")=="closed")

    rows=""
    for c in clients:
        rows+=f"""<tr>
          <td class="bold">{c.get('business') or c.get('username','—')}</td>
          <td><a href="mailto:{c.get('email','')}" style="font-size:12px">{c.get('email','—')}</a></td>
          <td style="font-size:12px;color:var(--muted)">{c.get('niche','—')}</td>
          <td style="color:var(--green);font-weight:700">${c.get('monthly_fee',0):,.0f}/mo</td>
          <td style="color:var(--amber);font-weight:600">${c.get('setup_fee',0):,.0f}</td>
          <td><span class="badge b-{'active' if c.get('status','active')=='active' else 'unsubscribed'}">{c.get('status','active')}</span></td>
          <td style="font-size:11px;color:var(--muted)">{c.get('start_date') or c.get('created_at','')[:10]}</td>
        </tr>"""

    content = f"""
    <div class="page-hdr"><div>
      <div class="page-title">Revenue</div>
      <div class="page-sub">MRR &nbsp;·&nbsp; client billing &nbsp;·&nbsp; financial overview</div>
    </div></div>
    <div class="metrics-grid">
      {mcard('<i class="fa-solid fa-arrow-trend-up"></i>','MRR',f'${mrr:,.0f}','monthly recurring','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-calendar-days"></i>','ARR',f'${arr:,.0f}','annualized','linear-gradient(135deg,#22c55e,#16a34a)')}
      {mcard('<i class="fa-solid fa-receipt"></i>','Setup Revenue',f'${setup:,.0f}','one-time fees','linear-gradient(135deg,#f59e0b,#f97316)')}
      {mcard('<i class="fa-solid fa-sack-dollar"></i>','Total Revenue',f'${total:,.0f}','')}
      {mcard('<i class="fa-solid fa-users"></i>','Active Clients',len(active),'')}
      {mcard('<i class="fa-solid fa-trophy"></i>','Closed Deals',f'${closed_val:,.0f}','pipeline revenue','linear-gradient(135deg,#22c55e,#16a34a)')}
    </div>
    <div class="card">
      <div class="card-header">
        <div class="card-title">Client Billing</div>
        <a href="/team" style="font-size:11px;color:var(--muted)">Manage clients &rarr;</a>
      </div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Business</th><th>Email</th><th>Niche</th><th>Monthly</th><th>Setup</th><th>Status</th><th>Start</th></tr></thead>
        <tbody>{rows or '<tr><td colspan="7" class="empty-state">No clients yet. Add them in Team.</td></tr>'}</tbody>
      </table></div>
    </div>"""
    return HTMLResponse(shell(content, "revenue", user))


# ─────────────────────────────────────────────
# TEAM
# ─────────────────────────────────────────────
@app.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    clients=get_clients()

    rows=""
    for c in clients:
        rows+=f"""<tr>
          <td class="bold" style="font-family:monospace">{c.get('username','—')}</td>
          <td style="color:rgba(255,255,255,.7)">{c.get('business','—')}</td>
          <td><a href="mailto:{c.get('email','')}" style="font-size:12px">{c.get('email','—')}</a></td>
          <td style="font-size:12px;color:var(--muted)">{c.get('niche','*')}</td>
          <td style="color:var(--green);font-weight:700">${c.get('monthly_fee',0):,.0f}/mo</td>
          <td><span class="badge b-{'active' if c.get('status','active')=='active' else 'unsubscribed'}">{c.get('status','active')}</span></td>
          <td style="font-size:11px;color:var(--muted)">{c.get('created_at','')[:10]}</td>
          <td><button class="btn btn-danger btn-sm" onclick="deleteClient('{c.get('username')}')">
            <i class="fa-solid fa-trash"></i>
          </button></td>
        </tr>"""

    content = f"""
    <div class="page-hdr">
      <div><div class="page-title">Team</div>
      <div class="page-sub">Manage client logins &nbsp;·&nbsp; {len(clients)} clients</div></div>
      <button class="btn btn-primary" onclick="document.getElementById('clientModal').classList.add('open')">
        <i class="fa-solid fa-user-plus"></i> Add Client
      </button>
    </div>
    <div class="card">
      <div class="card-header">
        <div class="card-title">Client Accounts</div>
        <div class="card-sub">Each client logs in to see only their niche leads</div>
      </div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Username</th><th>Business</th><th>Email</th><th>Niche Access</th><th>Monthly Fee</th><th>Status</th><th>Created</th><th>Action</th></tr></thead>
        <tbody>{rows or '<tr><td colspan="8" class="empty-state">No clients yet. Add your first client.</td></tr>'}</tbody>
      </table></div>
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">Admin Account</div></div>
      <div style="display:flex;align-items:center;gap:14px;padding:4px 0">
        <div class="u-avatar" style="width:36px;height:36px;font-size:14px">K</div>
        <div>
          <div style="font-size:13px;font-weight:700">Kory (admin)</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">username: admin &nbsp;·&nbsp; full access to all sections</div>
        </div>
        <span class="badge b-active" style="margin-left:auto">SUPERADMIN</span>
      </div>
    </div>"""
    return HTMLResponse(shell(content, "team", user))


# ─────────────────────────────────────────────
# PUBLIC BOOKING PAGE
# ─────────────────────────────────────────────
@app.get("/book", response_class=HTMLResponse)
def book_page():
    NOISE_URL = f"{NOISE_SVG}"
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Get Started · Lumera</title>
<link href="https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700;800;900&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css"/>
<style>
:root{{--black:#080808;--surface:#111111;--surface2:#181818;--border:rgba(255,255,255,0.07);--border-hover:rgba(255,255,255,0.14);--text:#ffffff;--muted:rgba(255,255,255,0.45);--muted2:rgba(255,255,255,0.25);--indigo:#6366f1;--blue:#3b82f6;--green:#22c55e;--grad:linear-gradient(135deg,#3b82f6,#6366f1);--font:'Montserrat',sans-serif;}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
html{{scroll-behavior:smooth;}}
body{{font-family:var(--font);background:var(--black);color:var(--text);min-height:100vh;overflow-x:hidden;-webkit-font-smoothing:antialiased;}}
body::before{{content:'';position:fixed;inset:0;background-image:url("{NOISE_URL}");opacity:0.025;pointer-events:none;z-index:9999;}}
body::after{{content:'';position:fixed;inset:0;background-image:radial-gradient(rgba(255,255,255,0.04) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0;}}
.navbar{{position:fixed;top:16px;left:50%;transform:translateX(-50%);width:92%;max-width:1100px;height:56px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;background:rgba(15,15,15,0.75);backdrop-filter:blur(20px);border:1px solid var(--border);border-radius:16px;box-shadow:0 4px 32px rgba(0,0,0,0.4);z-index:1000;transition:background 0.3s;}}
.navbar.scrolled{{background:rgba(10,10,10,0.92);}}
.nav-logo{{display:flex;align-items:center;gap:10px;text-decoration:none;}}
.nav-logo img{{height:32px;}}
.nav-logo-text{{font-size:17px;font-weight:700;color:var(--text);}}
.nav-back{{font-size:13px;font-weight:600;color:var(--muted);text-decoration:none;padding:6px 14px;border:1px solid var(--border-hover);border-radius:8px;transition:all 0.2s;}}
.nav-back:hover{{color:var(--text);border-color:rgba(255,255,255,0.3);}}
.page{{min-height:100vh;display:grid;grid-template-columns:1fr 1fr;position:relative;z-index:1;padding-top:88px;}}
.left{{padding:72px 56px 64px;display:flex;flex-direction:column;justify-content:center;}}
.page-badge{{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--border-hover);border-radius:100px;padding:6px 16px;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:28px;letter-spacing:0.5px;text-transform:uppercase;width:fit-content;animation:badgeFloat 3s ease-in-out infinite;}}
.live-dot{{width:6px;height:6px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s ease-in-out infinite;}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.4}}}}
@keyframes badgeFloat{{0%,100%{{transform:translateY(0)}}50%{{transform:translateY(-4px)}}}}
@keyframes gradShift{{0%{{background-position:0% 50%}}50%{{background-position:100% 50%}}100%{{background-position:0% 50%}}}}
@keyframes heroIn{{0%{{opacity:0;transform:translateY(20px);filter:blur(6px)}}100%{{opacity:1;transform:translateY(0);filter:blur(0)}}}}
.page-headline{{font-size:clamp(28px,3.5vw,48px);font-weight:800;line-height:1.1;letter-spacing:-1.5px;margin-bottom:20px;animation:heroIn 0.8s ease 0.2s both;}}
.grad-animate{{background:linear-gradient(270deg,#3b82f6,#6366f1,#8b5cf6,#6366f1,#3b82f6);background-size:300% 300%;-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;animation:gradShift 6s ease infinite;}}
.page-sub{{font-size:16px;color:var(--muted);line-height:1.7;max-width:400px;margin-bottom:48px;animation:heroIn 0.8s ease 0.35s both;}}
.stats-row{{display:flex;gap:36px;margin-bottom:48px;animation:heroIn 0.8s ease 0.45s both;}}
.stat-item{{display:flex;flex-direction:column;gap:4px;}}
.stat-icon{{width:36px;height:36px;border-radius:10px;background:rgba(99,102,241,0.12);border:1px solid rgba(99,102,241,0.2);display:flex;align-items:center;justify-content:center;color:var(--indigo);font-size:14px;margin-bottom:10px;}}
.stat-val{{font-size:22px;font-weight:800;letter-spacing:-0.5px;}}
.stat-lbl{{font-size:12px;color:var(--muted);font-weight:500;}}
.what-list{{display:flex;flex-direction:column;gap:10px;animation:heroIn 0.8s ease 0.55s both;}}
.what-item{{display:flex;align-items:center;gap:12px;font-size:13px;color:var(--muted);font-weight:500;}}
.what-item i{{color:var(--indigo);font-size:12px;width:16px;flex-shrink:0;}}
.right{{padding:72px 48px 64px;display:flex;flex-direction:column;justify-content:center;}}
.form-card{{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:40px 36px;box-shadow:0 24px 80px rgba(0,0,0,0.4);animation:heroIn 0.8s ease 0.3s both;}}
.step-bar{{display:flex;align-items:center;margin-bottom:20px;}}
.step-dot{{width:28px;height:28px;border-radius:50%;border:2px solid var(--border-hover);background:var(--surface2);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--muted2);transition:all 0.3s;flex-shrink:0;}}
.step-dot.active{{border-color:var(--indigo);color:var(--indigo);background:rgba(99,102,241,0.1);}}
.step-dot.done{{border-color:var(--green);background:var(--green);color:white;}}
.step-line{{flex:1;height:2px;background:var(--border);transition:background 0.3s;}}
.step-line.done{{background:var(--green);}}
.step-label-row{{display:flex;justify-content:space-between;margin-bottom:24px;}}
.step-lbl{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted2);}}
.step-lbl.active{{color:var(--indigo);}} .step-lbl.done{{color:var(--green);}}
.form-title{{font-size:20px;font-weight:800;margin-bottom:6px;letter-spacing:-0.3px;}}
.form-sub{{font-size:13px;color:var(--muted);margin-bottom:24px;line-height:1.6;}}
.form-wrap{{display:flex;flex-direction:column;gap:14px;}}
.form-row{{display:grid;grid-template-columns:1fr 1fr;gap:12px;}}
.field{{display:flex;flex-direction:column;gap:5px;}}
.field label{{font-size:11px;font-weight:700;color:var(--muted2);text-transform:uppercase;letter-spacing:0.5px;}}
.field input,.field select,.field textarea{{padding:11px 14px;border:1.5px solid var(--border-hover);border-radius:10px;font-size:13px;font-family:var(--font);color:var(--text);background:var(--surface2);outline:none;transition:border-color 0.2s,box-shadow 0.2s;width:100%;}}
.field input::placeholder,.field textarea::placeholder{{color:var(--muted2);}}
.field input:focus,.field select:focus,.field textarea:focus{{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(99,102,241,0.15);}}
.field select{{cursor:pointer;}} .field select option{{background:var(--surface2);color:var(--text);}}
.field textarea{{resize:vertical;min-height:80px;}}
.submit-btn{{width:100%;padding:14px;border:none;border-radius:12px;background:var(--text);color:var(--black);font-weight:700;font-size:15px;font-family:var(--font);cursor:pointer;transition:opacity 0.2s,transform 0.2s;margin-top:4px;display:flex;align-items:center;justify-content:center;gap:8px;}}
.submit-btn:hover{{opacity:0.88;transform:translateY(-1px);}} .submit-btn:disabled{{opacity:0.5;cursor:not-allowed;transform:none;}}
.back-btn{{width:100%;padding:12px;border:1px solid var(--border-hover);border-radius:12px;background:transparent;color:var(--muted);font-weight:600;font-size:14px;font-family:var(--font);cursor:pointer;transition:all 0.2s;margin-top:8px;}}
.back-btn:hover{{border-color:rgba(255,255,255,0.3);color:var(--text);}}
.form-note{{font-size:11px;color:var(--muted2);text-align:center;margin-top:10px;}}
.form-error{{color:#f87171;font-size:12px;margin-top:2px;display:none;}}
.gs-confirm{{display:none;text-align:center;padding:32px 0;}}
.gs-confirm .emoji{{font-size:52px;margin-bottom:16px;}}
.gs-confirm h3{{font-size:22px;font-weight:800;margin-bottom:10px;}}
.gs-confirm p{{color:var(--muted);font-size:14px;line-height:1.7;max-width:320px;margin:0 auto 20px;}}
.confirm-summary{{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:16px 20px;text-align:left;margin-bottom:16px;}}
.sum-row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;}}
.sum-row:last-child{{border-bottom:none;}}
.sum-row span:first-child{{color:var(--muted);}} .sum-row span:last-child{{color:var(--text);font-weight:600;}}
.next-steps{{background:var(--surface2);border:1px solid var(--border);border-radius:12px;padding:16px 20px;text-align:left;}}
.next-steps h4{{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted2);margin-bottom:12px;}}
.next-step-item{{display:flex;gap:10px;margin-bottom:10px;align-items:flex-start;font-size:12px;}}
.next-step-item:last-child{{margin-bottom:0;}}
.ns-num{{width:20px;height:20px;border-radius:50%;background:var(--grad);color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;margin-top:1px;}}
.next-step-item div strong{{display:block;color:var(--text);font-size:12px;margin-bottom:1px;}}
.next-step-item div span{{color:var(--muted);font-size:11px;}}
@media(max-width:768px){{
  .page{{grid-template-columns:1fr;}}
  .left{{padding:48px 24px 32px;}}
  .right{{padding:0 20px 56px;}}
  .form-card{{padding:28px 20px;border-radius:16px;}}
  .form-row{{grid-template-columns:1fr;}}
  .stats-row{{gap:20px;flex-wrap:wrap;}}
  .navbar{{top:10px;width:94%;padding:0 16px;border-radius:14px;}}
}}
</style>
</head>
<body>
<canvas id="heroBurst" style="position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;"></canvas>
<nav class="navbar" id="navbar">
  <a href="https://lumeraautomation.com" class="nav-logo">
    <img src="https://lumeraautomation.com/favicon.png" alt="Lumera">
    <span class="nav-logo-text">Lumera</span>
  </a>
  <a href="https://lumeraautomation.com" class="nav-back"><i class="fa-solid fa-arrow-left"></i> Back to site</a>
</nav>
<div class="page">
  <div class="left">
    <div class="page-badge"><span class="live-dot"></span> Free Strategy Call</div>
    <h1 class="page-headline">Let's build your<br><span class="grad-animate">lead engine together.</span></h1>
    <p class="page-sub">Fill out the form and we'll reach out within 24 hours to schedule a free 30-minute strategy call — no pitch, just a real conversation about what Lumera can do for your business.</p>
    <div class="stats-row">
      <div class="stat-item">
        <div class="stat-icon"><i class="fa-solid fa-clock"></i></div>
        <div class="stat-val">&lt;24hrs</div>
        <div class="stat-lbl">Response time</div>
      </div>
      <div class="stat-item">
        <div class="stat-icon"><i class="fa-solid fa-crosshairs"></i></div>
        <div class="stat-val">100+</div>
        <div class="stat-lbl">Leads/week</div>
      </div>
      <div class="stat-item">
        <div class="stat-icon"><i class="fa-solid fa-bolt"></i></div>
        <div class="stat-val">48hrs</div>
        <div class="stat-lbl">Setup time</div>
      </div>
    </div>
    <div class="what-list">
      <div class="what-item"><i class="fa-solid fa-check"></i> We identify your best target niches and cities</div>
      <div class="what-item"><i class="fa-solid fa-check"></i> Live demo of your lead gen dashboard</div>
      <div class="what-item"><i class="fa-solid fa-check"></i> Walk through outreach + follow-up system</div>
      <div class="what-item"><i class="fa-solid fa-check"></i> Clear plan to start generating leads this week</div>
      <div class="what-item"><i class="fa-solid fa-check"></i> No contracts — cancel anytime</div>
    </div>
  </div>
  <div class="right">
    <div class="form-card">
      <div id="step1-wrap">
        <div class="step-bar">
          <div class="step-dot active">1</div>
          <div class="step-line"></div>
          <div class="step-dot">2</div>
          <div class="step-line"></div>
          <div class="step-dot">&#10003;</div>
        </div>
        <div class="step-label-row">
          <span class="step-lbl active">Contact</span>
          <span class="step-lbl">Your Business</span>
          <span class="step-lbl">Done</span>
        </div>
        <h2 class="form-title">About you</h2>
        <p class="form-sub">Tell us who you are — we'll reach out within 24 hours.</p>
        <div class="form-wrap">
          <div class="form-row">
            <div class="field"><label>First Name *</label><input id="b-fname" placeholder="Jane"/></div>
            <div class="field"><label>Last Name *</label><input id="b-lname" placeholder="Smith"/></div>
          </div>
          <div class="form-row">
            <div class="field"><label>Email *</label><input id="b-email" type="email" placeholder="jane@yourbusiness.com"/></div>
            <div class="field"><label>Phone</label><input id="b-phone" type="tel" placeholder="(615) 555-0100"/></div>
          </div>
          <p id="f-error1" class="form-error">Please fill in your name and email.</p>
          <button type="button" class="submit-btn" onclick="goStep2()">Continue <i class="fa-solid fa-arrow-right"></i></button>
          <p class="form-note">No spam, ever. We'll reach out within 24 hours.</p>
        </div>
      </div>
      <div id="step2-wrap" style="display:none;">
        <div class="step-bar">
          <div class="step-dot done">&#10003;</div>
          <div class="step-line done"></div>
          <div class="step-dot active">2</div>
          <div class="step-line"></div>
          <div class="step-dot">&#10003;</div>
        </div>
        <div class="step-label-row">
          <span class="step-lbl done">Contact</span>
          <span class="step-lbl active">Your Business</span>
          <span class="step-lbl">Done</span>
        </div>
        <h2 class="form-title">Your business</h2>
        <p class="form-sub">Help us understand your situation so we come prepared.</p>
        <div class="form-wrap">
          <div class="field"><label>Business Name</label><input id="b-biz" placeholder="Nashville Roofing Co"/></div>
          <div class="field">
            <label>Your niche / industry *</label>
            <select id="b-niche">
              <option value="" disabled selected>Select your niche...</option>
              <option>Roofing</option>
              <option>MedSpa / Aesthetics</option>
              <option>Dentist / Dental Office</option>
              <option>Chiropractor</option>
              <option>HVAC</option>
              <option>Landscaping</option>
              <option>Cleaning Service</option>
              <option>Plumbing</option>
              <option>Marketing Agency</option>
              <option>Digital Agency</option>
              <option>Other</option>
            </select>
          </div>
          <div class="field">
            <label>Biggest challenge right now *</label>
            <select id="b-challenge">
              <option value="" disabled selected>Select your main challenge...</option>
              <option>Not enough leads coming in</option>
              <option>Leads not converting to clients</option>
              <option>Spending too much time prospecting manually</option>
              <option>No consistent outreach system</option>
              <option>Struggling to follow up with leads</option>
              <option>Just starting out and need a system</option>
            </select>
          </div>
          <div class="field">
            <label>Anything else we should know?</label>
            <textarea id="b-notes" placeholder="Goals, current situation, questions..."></textarea>
          </div>
          <p id="f-error2" class="form-error">Please fill in your niche and biggest challenge.</p>
          <button type="button" class="submit-btn" id="submit-btn" onclick="submitForm()">Submit Application <i class="fa-solid fa-paper-plane"></i></button>
          <button type="button" class="back-btn" onclick="goBack()">&#8592; Back</button>
        </div>
      </div>
      <div class="gs-confirm" id="gs-confirm">
        <div class="emoji">&#129309;</div>
        <h3>Application received!</h3>
        <p>We'll review your details and reach out within 24 hours — usually much faster.</p>
        <div class="confirm-summary" id="gs-summary"></div>
        <div class="next-steps">
          <h4>What happens next</h4>
          <div class="next-step-item"><div class="ns-num">1</div><div><strong>We review your application</strong><span>Expect a response within 24 hours — usually same day.</span></div></div>
          <div class="next-step-item"><div class="ns-num">2</div><div><strong>We schedule your strategy call</strong><span>30 minutes, no pitch. Just a real walkthrough of what Lumera can do.</span></div></div>
          <div class="next-step-item"><div class="ns-num">3</div><div><strong>Your system goes live</strong><span>If it's a fit, we can have leads flowing into your dashboard within 48 hours.</span></div></div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
function goStep2(){{
  const fname=document.getElementById('b-fname').value.trim();
  const email=document.getElementById('b-email').value.trim();
  if(!fname||!email){{document.getElementById('f-error1').style.display='block';return;}}
  document.getElementById('f-error1').style.display='none';
  document.getElementById('step1-wrap').style.display='none';
  document.getElementById('step2-wrap').style.display='block';
  window.scrollTo({{top:0,behavior:'smooth'}});
}}
function goBack(){{
  document.getElementById('step2-wrap').style.display='none';
  document.getElementById('step1-wrap').style.display='block';
  window.scrollTo({{top:0,behavior:'smooth'}});
}}
async function submitForm(){{
  const niche=document.getElementById('b-niche').value;
  const challenge=document.getElementById('b-challenge').value;
  if(!niche||!challenge){{document.getElementById('f-error2').style.display='block';return;}}
  document.getElementById('f-error2').style.display='none';
  const btn=document.getElementById('submit-btn');
  btn.disabled=true;btn.innerHTML='<i class="fa-solid fa-spinner fa-spin"></i> Submitting...';
  const fname=document.getElementById('b-fname').value.trim();
  const lname=document.getElementById('b-lname').value.trim();
  const email=document.getElementById('b-email').value.trim();
  const phone=document.getElementById('b-phone').value.trim();
  const biz=document.getElementById('b-biz').value.trim();
  const notes=document.getElementById('b-notes').value.trim();
  const name=(fname+' '+lname).trim();
  try{{
    const res=await fetch('/book',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name,email,phone,business:biz,niche,challenge,notes}})}});
    const d=await res.json();
    if(!res.ok)throw new Error(d.detail||'Submission failed');
    document.getElementById('gs-summary').innerHTML=`
      <div class="sum-row"><span>Name</span><span>${{name}}</span></div>
      <div class="sum-row"><span>Email</span><span>${{email}}</span></div>
      ${{biz?`<div class="sum-row"><span>Business</span><span>${{biz}}</span></div>`:''}}
      <div class="sum-row"><span>Niche</span><span>${{niche}}</span></div>
      <div class="sum-row"><span>Challenge</span><span>${{challenge}}</span></div>
    `;
    document.getElementById('step2-wrap').style.display='none';
    document.getElementById('gs-confirm').style.display='block';
  }}catch(e){{
    document.getElementById('f-error2').style.display='block';
    document.getElementById('f-error2').textContent=e.message;
    btn.disabled=false;btn.innerHTML='Submit Application <i class="fa-solid fa-paper-plane"></i>';
  }}
}}
window.addEventListener('scroll',()=>document.getElementById('navbar').classList.toggle('scrolled',window.scrollY>40));
(function(){{
  const canvas=document.getElementById('heroBurst');if(!canvas)return;
  const ctx=canvas.getContext('2d');let W,H,t=0,enterT=0,entering=true;
  function resize(){{W=canvas.offsetWidth;H=canvas.offsetHeight;canvas.width=W;canvas.height=H;}}
  resize();window.addEventListener('resize',resize);
  const layers=[{{color:'#0a0a2e'}},{{color:'#0d0d4a'}},{{color:'#12106a'}},{{color:'#1a1488'}},{{color:'#241aa0'}},{{color:'#3a22b8'}},{{color:'#5530c8'}},{{color:'#7040c8'}},{{color:'#9050c4'}},{{color:'#b060b8'}},{{color:'#c85898'}},{{color:'#d85878'}},{{color:'#e06080'}},{{color:'#d870a8'}},{{color:'#c878c8'}}];
  function eob(x){{const c1=1.70158,c3=c1+1;return 1+c3*Math.pow(x-1,3)+c1*Math.pow(x-1,2);}}
  function eoe(x){{return x===1?1:1-Math.pow(2,-10*x);}}
  function buildPath(d,time,sx,sy,ox,oy){{
    const ph=time*(0.0004+d*0.00003),sh=1-d*0.040;
    const ax=W*(1.08-d*0.008)*sx+ox,ay=H*(0.52+d*0.010)*sy+oy,rx=W*0.80*sh*sx,ry=H*0.95*sh*sy;
    ctx.beginPath();const ps=[];
    for(let i=0;i<=280;i++){{const a=(i/280)*Math.PI*2,nx=Math.cos(a),ny=Math.sin(a),r=1+Math.sin(a*4+ph)*0.10+Math.sin(a*7.3+ph*1.6)*0.06+Math.sin(a*12.1+ph*0.9)*0.04+Math.sin(a*19.7+ph*2.1)*0.025+Math.sin(a*31.3+ph*1.4)*0.015;ps.push([ax+nx*rx*r,ay+ny*ry*r]);}}
    ctx.moveTo(ps[0][0],ps[0][1]);for(let i=1;i<ps.length;i++)ctx.lineTo(ps[i][0],ps[i][1]);ctx.closePath();
  }}
  function draw(){{
    ctx.clearRect(0,0,W,H);let sx=1,sy=1,ox=0,oy=0,ga=1;
    if(entering){{const p=Math.min(enterT/75,1),e=eob(p);sx=0.3+e*0.7;sy=0.3+e*0.7;ox=(1-e)*W*0.4;oy=(1-e)*H*0.1;ga=eoe(Math.min(enterT/40,1));enterT++;if(enterT>75)entering=false;}}
    ctx.globalAlpha=ga;
    layers.forEach((l,i)=>{{buildPath(i,t+i*600,sx,sy,ox,oy);ctx.shadowColor='rgba(0,0,0,0.5)';ctx.shadowBlur=12;ctx.shadowOffsetX=-4;ctx.shadowOffsetY=6;ctx.fillStyle=l.color;ctx.globalAlpha=ga*0.82;ctx.fill();ctx.shadowBlur=0;ctx.shadowOffsetX=0;ctx.shadowOffsetY=0;ctx.strokeStyle='rgba(255,255,255,0.08)';ctx.lineWidth=0.9;ctx.stroke();ctx.globalAlpha=ga;}});
    layers.forEach((l,i)=>{{if(i%2!==0)return;const ph=(t+i*300)*0.0005*Math.PI*2;const cx=(W*(0.75+Math.sin(ph*1.1+i)*0.10))*sx+ox;const cy=(H*(0.08+i*0.065+Math.cos(ph*0.9+i)*0.025))*sy+oy;const r=(3+i*0.35)*sx;ctx.beginPath();ctx.arc(cx,cy,r,0,Math.PI*2);ctx.fillStyle=l.color;ctx.shadowColor=l.color;ctx.shadowBlur=14;ctx.globalAlpha=ga*0.9;ctx.fill();ctx.shadowBlur=0;ctx.globalAlpha=ga;}});
    ctx.globalAlpha=1;
    const fade=ctx.createLinearGradient(0,0,W*0.52,0);fade.addColorStop(0,'rgba(8,8,8,1)');fade.addColorStop(0.55,'rgba(8,8,8,0.85)');fade.addColorStop(1,'rgba(8,8,8,0)');
    ctx.fillStyle=fade;ctx.globalCompositeOperation='destination-out';ctx.fillRect(0,0,W*0.52,H);
    ctx.globalCompositeOperation='source-over';t+=1;requestAnimationFrame(draw);
  }}
  draw();
}})();
</script>
</body>
</html>""")


@app.post("/book")
async def book_submit(request: Request):
    data = await request.json()
    name     = data.get("name","").strip()
    email    = data.get("email","").strip()
    phone    = data.get("phone","").strip()
    business = data.get("business","").strip()
    niche    = data.get("niche","").strip()
    challenge= data.get("challenge","").strip()
    notes    = data.get("notes","").strip()

    if not name or not email:
        return JSONResponse({"detail":"Missing required fields"}, status_code=400)
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
        return JSONResponse({"detail":"Invalid email"}, status_code=400)

    # Send notification email to Kory
    if RESEND_API_KEY:
        try:
            import resend as r
            r.api_key = RESEND_API_KEY
            r.Emails.send({
                "from": f"Lumera Lead Engine <{FROM_EMAIL}>",
                "to": "lumeraautomation@gmail.com",
                "subject": f"New Strategy Call Application — {name}",
                "html": f"""<div style="font-family:sans-serif;max-width:560px;margin:0 auto;background:#080808;color:#fff;padding:36px;border-radius:16px;border:1px solid rgba(255,255,255,0.07)">
                    <div style="font-size:18px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:20px">LUMERA</div>
                    <h2 style="font-size:18px;font-weight:700;margin-bottom:16px">New Strategy Call Application</h2>
                    <table style="width:100%;border-collapse:collapse;font-size:13px">
                        <tr><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.5);width:40%">Name</td><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-weight:600">{name}</td></tr>
                        <tr><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.5)">Email</td><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-weight:600">{email}</td></tr>
                        <tr><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.5)">Phone</td><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-weight:600">{phone or "—"}</td></tr>
                        <tr><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.5)">Business</td><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-weight:600">{business or "—"}</td></tr>
                        <tr><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.5)">Niche</td><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-weight:600">{niche}</td></tr>
                        <tr><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.5)">Challenge</td><td style="padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-weight:600">{challenge}</td></tr>
                        <tr><td style="padding:8px 0;color:rgba(255,255,255,0.5)">Notes</td><td style="padding:8px 0;font-weight:600">{notes or "—"}</td></tr>
                    </table>
                    <div style="margin-top:24px;padding:14px 16px;background:rgba(99,102,241,0.1);border:1px solid rgba(99,102,241,0.25);border-radius:10px;font-size:13px;color:rgba(255,255,255,0.7)">
                        Reply to this email or reach out to <strong style="color:#fff">{email}</strong> within 24 hours.
                    </div>
                </div>"""
                    })
        except Exception as e:
            print(f"Notification email failed: {e}")

    # Send confirmation to applicant
    if RESEND_API_KEY:
        try:
            import resend as r
            r.api_key = RESEND_API_KEY
            r.Emails.send({
                "from": f"Kory @ Lumera Automation <{FROM_EMAIL}>",
                "to": email,
                "subject": "We received your application — talk soon!",
                "html": f"""<div style="font-family:sans-serif;max-width:540px;margin:0 auto;background:#080808;color:#fff;padding:40px;border-radius:16px;border:1px solid rgba(255,255,255,0.07)">
                    <div style="font-size:20px;font-weight:800;background:linear-gradient(135deg,#3b82f6,#6366f1);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:20px">LUMERA</div>
                    <h2 style="font-size:18px;font-weight:700;margin-bottom:8px">Hey {name.split()[0]}! Application received.</h2>
                    <p style="color:rgba(255,255,255,0.5);font-size:14px;line-height:1.7;margin-bottom:20px">Thanks for reaching out. We'll review your details and get back to you within 24 hours to schedule your free strategy call.</p>
                    <p style="color:rgba(255,255,255,0.5);font-size:14px;line-height:1.7;margin-bottom:24px">On the call we'll walk through your niche, your targets, and show you exactly how Lumera works — no pitch, just a real conversation.</p>
                    <p style="color:rgba(255,255,255,0.3);font-size:13px">— Kory @ Lumera Automation</p>
                </div>"""
                    })
        except Exception as e:
            print(f"Confirmation email failed: {e}")

    # Save to applications DB
    import sqlite3 as _sq3
    with _sq3.connect(DB_PATH) as _conn:
        _conn.execute("""INSERT INTO applications(name,email,phone,business,niche,challenge,notes,status,created_at)
            VALUES(?,?,?,?,?,?,?,'new',?)""",
            (name,email,phone,business,niche,challenge,notes,datetime.now().isoformat()))
        _conn.commit()

    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# APPLICATIONS
# ─────────────────────────────────────────────
@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")

    apps = db_query("SELECT * FROM applications ORDER BY created_at DESC")
    total = len(apps)
    new_count = sum(1 for a in apps if a.get("status","new") == "new")

    rows = ""
    for a in apps:
        status = a.get("status","new")
        badge_cls = "b-active" if status == "contacted" else "b-sent"
        app_id = a.get("id")
        name = a.get("name","—")
        email = a.get("email","—")
        phone = a.get("phone","") or "—"
        business = a.get("business","") or "—"
        niche = a.get("niche","—")
        challenge = a.get("challenge","—")
        notes = a.get("notes","") or "—"
        created = a.get("created_at","")[:10]

        import json as _json
        app_data = _json.dumps({
            "id": app_id, "name": name, "email": email, "phone": phone,
            "business": business, "niche": niche, "challenge": challenge,
            "notes": notes, "status": status, "created": created
        }).replace("'", "\'")

        rows += f"""<tr style="cursor:pointer" onclick="openApp(JSON.parse(this.dataset.app))" data-app='{app_data}'>
          <td class="bold">{name}</td>
          <td style="font-size:12px;color:rgba(255,255,255,.7)">{email}</td>
          <td style="font-size:12px;color:rgba(255,255,255,.7)">{phone}</td>
          <td style="font-size:12px;color:rgba(255,255,255,.7)">{niche}</td>
          <td><span class="badge {badge_cls}">{status}</span></td>
          <td style="font-size:11px;color:var(--muted)">{created}</td>
        </tr>"""

    content = f"""
    <div class="page-hdr">
      <div><div class="page-title">Applications</div>
      <div class="page-sub">Strategy call form submissions · click any row to view details</div></div>
    </div>
    <div class="metrics-grid">
      {mcard('<i class="fa-solid fa-inbox"></i>','Total Applications',total)}
      {mcard('<i class="fa-solid fa-bell"></i>','New — Needs Reply',new_count,'','linear-gradient(135deg,#f59e0b,#f97316)')}
      {mcard('<i class="fa-solid fa-circle-check"></i>','Contacted',total-new_count,'','linear-gradient(135deg,#22c55e,#16a34a)')}
    </div>
    <div class="card">
      <div class="card-header"><div class="card-title">All Applications</div></div>
      <div class="tbl-wrap"><table>
        <thead><tr>
          <th>Name</th><th>Email</th><th>Phone</th><th>Niche</th><th>Status</th><th>Date</th>
        </tr></thead>
        <tbody>{rows if rows else '<tr><td colspan="6" class="empty-state">No applications yet. Share your booking page link to start getting submissions.</td></tr>'}</tbody>
      </table></div>
    </div>

    <!-- APPLICATION DETAIL MODAL -->
    <div class="modal-overlay" id="appModal">
      <div class="modal" style="max-width:540px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
          <h3 style="margin:0" id="app-modal-name">Application</h3>
          <button onclick="document.getElementById('appModal').classList.remove('open')"
            style="background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;padding:4px">&#10005;</button>
        </div>
        <div style="display:flex;flex-direction:column;gap:12px;margin-bottom:20px">
          <div class="app-field"><div class="app-label">Email</div><div class="app-val" id="app-email"></div></div>
          <div class="app-field"><div class="app-label">Phone</div><div class="app-val" id="app-phone"></div></div>
          <div class="app-field"><div class="app-label">Business</div><div class="app-val" id="app-business"></div></div>
          <div class="app-field"><div class="app-label">Niche</div><div class="app-val" id="app-niche"></div></div>
          <div class="app-field"><div class="app-label">Biggest Challenge</div><div class="app-val" id="app-challenge"></div></div>
          <div class="app-field"><div class="app-label">Notes</div><div class="app-val" id="app-notes"></div></div>
          <div class="app-field"><div class="app-label">Submitted</div><div class="app-val" id="app-date"></div></div>
          <div class="app-field"><div class="app-label">Status</div><div id="app-status"></div></div>
        </div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <a id="app-reply-btn" href="#" class="btn btn-primary" style="flex:1;justify-content:center">
            <i class="fa-solid fa-envelope"></i> Reply via Email
          </a>
          <button id="app-contact-btn" class="btn btn-green" onclick="markAppContacted()" style="flex:1">
            <i class="fa-solid fa-check"></i> Mark as Contacted
          </button>
        </div>
      </div>
    </div>

    <style>
    .app-field{{display:flex;flex-direction:column;gap:3px;padding:10px 14px;background:var(--surface2);border-radius:8px;border:1px solid var(--border);}}
    .app-label{{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);}}
    .app-val{{font-size:13px;color:var(--text);font-weight:500;}}
    tbody tr:hover td{{background:rgba(99,102,241,0.05)!important;}}
    </style>

    <script>
    let currentAppId = null;
    function openApp(a){{
      currentAppId = a.id;
      document.getElementById('app-modal-name').textContent = a.name;
      document.getElementById('app-email').textContent = a.email;
      document.getElementById('app-phone').textContent = a.phone;
      document.getElementById('app-business').textContent = a.business;
      document.getElementById('app-niche').textContent = a.niche;
      document.getElementById('app-challenge').textContent = a.challenge;
      document.getElementById('app-notes').textContent = a.notes;
      document.getElementById('app-date').textContent = a.created;
      document.getElementById('app-status').innerHTML = a.status === 'contacted'
        ? '<span class="badge b-active">contacted</span>'
        : '<span class="badge b-sent">new</span>';
      document.getElementById('app-reply-btn').href = 'mailto:' + a.email
        + '?subject=Re: Your Lumera Strategy Call Application'
        + '&body=Hey ' + a.name.split(' ')[0] + ',%0D%0A%0D%0AThanks for applying! I'd love to set up a quick call to walk you through how Lumera works for your ' + a.niche + ' business.%0D%0A%0D%0AWhat time works best for you?%0D%0A%0D%0A— Kory%0D%0ALumera Automation';
      const contactBtn = document.getElementById('app-contact-btn');
      contactBtn.style.display = a.status === 'contacted' ? 'none' : '';
      document.getElementById('appModal').classList.add('open');
    }}
    async function markAppContacted(){{
      if(!currentAppId) return;
      await fetch('/api/applications/'+currentAppId+'/contacted',{{method:'POST'}});
      toast('Marked as contacted','ok');
      document.getElementById('appModal').classList.remove('open');
      setTimeout(()=>location.reload(),600);
    }}
    </script>"""
    return HTMLResponse(shell(content, "applications", user))

@app.post("/api/applications/{app_id}/contacted")
async def mark_app_contacted(app_id: int):
    db_run("UPDATE applications SET status='contacted' WHERE id=?", (app_id,))
    return JSONResponse({"ok": True})


# ─────────────────────────────────────────────
# CSV UPLOAD
# ─────────────────────────────────────────────
@app.post("/api/upload-csv")
async def upload_csv(request: Request, file: "UploadFile" = None, niche: str = "Uploaded Leads"):
    from fastapi import UploadFile, File
    import io

    user = get_current_user(request)
    if not user:
        return JSONResponse({"detail":"Not authenticated"}, status_code=401)

    # Parse form data
    form = await request.form()
    file = form.get("file")
    niche = form.get("niche", "Uploaded Leads").strip() or "Uploaded Leads"

    if not file:
        return JSONResponse({"detail":"No file provided"}, status_code=400)

    filename = file.filename
    if not filename.endswith(".csv"):
        return JSONResponse({"detail":"Only CSV files allowed"}, status_code=400)

    contents = await file.read()

    # Try to parse it
    try:
        df = pd.read_csv(io.StringIO(contents.decode("utf-8-sig"))).fillna("")
    except Exception as e:
        return JSONResponse({"detail":f"Could not parse CSV: {e}"}, status_code=400)

    # Detect if it's an Apollo export and remap columns
    cols = [c.lower() for c in df.columns]
    is_apollo = "first name" in cols or "apollo contact id" in cols

    if is_apollo:
        # Remap Apollo columns to Lumera format
        out_rows = []
        for _, row in df.iterrows():
            email = str(row.get("Email","")).strip()
            if not email or "@" not in email: continue
            status = str(row.get("Email Status","")).lower()
            if status in ("invalid","bounced","do not email"): continue

            first = str(row.get("First Name","")).strip()
            last  = str(row.get("Last Name","")).strip()
            company = str(row.get("Company Name","")).strip()
            city = str(row.get("City","") or row.get("Company City","")).strip()
            state = str(row.get("State","") or row.get("Company State","")).strip()
            location = f"{city}, {state}".strip(", ") if city or state else "United States"
            website = str(row.get("Website","")).strip()
            phone = str(row.get("Work Direct Phone","") or row.get("Corporate Phone","") or row.get("Mobile Phone","")).strip()
            industry = str(row.get("Industry","")).strip()
            out_rows.append({
                "Name": company or f"{first} {last}".strip(),
                "City": location,
                "Website": website or "None listed",
                "Problem": f"{industry} agency — lead generation opportunity" if industry else "agency seeking more clients",
                "Email": email,
                "Phone": phone,
                "Owner": first,
                "Score": 2 if status == "verified" else 1,
            })
        df_out = pd.DataFrame(out_rows)
        niche_slug = niche.replace(" ","_")
        save_name = f"{niche_slug}_{datetime.now().strftime('%Y-%m-%d')}.csv"
        save_path = DAILY_LEADS_DIR / save_name
        df_out.to_csv(save_path, index=False)
        return JSONResponse({"ok":True, "message":f"{len(df_out)} Apollo leads imported as '{niche}'"})

    else:
        # Generic CSV — check for required columns
        required = ["Name","Email"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            # Try case-insensitive match
            col_map = {c.lower():c for c in df.columns}
            rename = {}
            for req in required:
                if req.lower() in col_map:
                    rename[col_map[req.lower()]] = req
            if rename:
                df = df.rename(columns=rename)
            else:
                return JSONResponse({"detail":f"CSV must have columns: {', '.join(required)}. Found: {', '.join(df.columns)}"}, status_code=400)

        # Add missing columns with defaults
        for col, default in [("City","United States"),("Website","None listed"),
                              ("Problem","lead generation opportunity"),
                              ("Phone",""),("Owner",""),("Score",1)]:
            if col not in df.columns:
                df[col] = default

        niche_slug = niche.replace(" ","_")
        save_name = f"{niche_slug}_{datetime.now().strftime('%Y-%m-%d')}.csv"
        save_path = DAILY_LEADS_DIR / save_name
        df[["Name","City","Website","Problem","Email","Phone","Owner","Score"]].to_csv(save_path, index=False)
        count = len(df[df["Email"].str.contains("@",na=False)])
        return JSONResponse({"ok":True, "message":f"{count} leads imported as '{niche}'"})


# ─────────────────────────────────────────────
# LEAD ENGINE API
# ─────────────────────────────────────────────
@app.post("/api/engine-scrape")
async def engine_scrape(request: Request):
    user = get_current_user(request)
    if not user: return JSONResponse({"detail":"Not authenticated"}, status_code=401)

    data    = await request.json()
    niche   = data.get("niche","restaurant").strip()
    city    = data.get("city","Nashville TN").strip()
    count   = min(int(data.get("count",10)), 20)
    client  = data.get("client","").strip()

    # Build Perplexity prompt
    prompt = (
        f"Search for {count} local {niche} businesses in {city} that would benefit from lead generation or AI automation. "
        f"For each find: 1) Real contact email from their website Contact/About page or Google listing "
        f"2) Phone number 3) Owner first name if available 4) Google Maps rating 5) Approximate review count "
        f"6) Whether they have online booking - yes or no 7) Business hours especially if limited "
        f"8) Their biggest visible weakness or problem. "
        f"Only include businesses with a confirmed real email. "
        f'Return ONLY a valid JSON array: [{{"business":"Name","website":"https://...","email":"info@...","name":"FirstName","phone":"","rating":"4.2","reviews":"47","has_booking":"no","hours":"closes 5pm","problem":"specific weakness"}}]. '
        f"Do not include businesses without a confirmed real email."
    )

    import urllib.request as _req
    import json as _json

    try:
        payload = _json.dumps({
            "model": "sonar",
            "messages": [{"role": "user", "content": prompt}]
        }).encode()
        req = _req.Request(
            "https://api.perplexity.ai/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {os.getenv('PERPLEXITY_KEY','')}",
                "Content-Type": "application/json"
            }
        )
        with _req.urlopen(req, timeout=30) as resp:
            raw = _json.loads(resp.read())
        text = raw["choices"][0]["message"]["content"]
    except Exception as e:
        return JSONResponse({"detail": f"Perplexity error: {e}"}, status_code=500)

    # Parse JSON from response
    text = text.replace("```json","").replace("```","").strip()
    start = text.find("["); end = text.rfind("]") + 1
    if start == -1: return JSONResponse({"detail":"No leads found in response"}, status_code=400)

    try:
        leads_raw = _json.loads(text[start:end])
    except:
        # Try merging multiple arrays
        import re as _re
        arrays = _re.findall(r'\[.*?\]', text, _re.DOTALL)
        leads_raw = []
        for arr in arrays:
            try:
                items = _json.loads(arr)
                leads_raw.extend([i for i in items if isinstance(i, dict)])
            except: pass

    # Process leads
    seen = set()
    leads_out = []
    for l in leads_raw:
        if not isinstance(l, dict): continue
        email = (l.get("email") or "").strip().lower()
        if not email or "@" not in email or email in seen: continue
        seen.add(email)

        phone   = l.get("phone","") or ""
        website = l.get("website","") or "None listed"
        problem = l.get("problem","") or ""
        reviews = l.get("reviews","") or ""
        rating  = l.get("rating","") or ""
        has_bk  = l.get("has_booking","") or ""
        hours   = l.get("hours","") or ""

        # Score
        score = 0
        if phone: score += 1
        if website.lower() in ["none listed","none","n/a",""]: score += 2
        if any(x in has_bk.lower() for x in ["no","false","none"]): score += 1
        try:
            if int(''.join(c for c in reviews if c.isdigit()) or 0) >= 50: score += 1
        except: pass
        if any(x in (hours+problem).lower() for x in ["closes","limited","no booking","phone"]): score += 1
        score = min(score, 5)

        leads_out.append({
            "Name": l.get("business","") or l.get("name",""),
            "City": city,
            "Website": website,
            "Problem": problem,
            "Email": email,
            "Phone": phone,
            "Owner": l.get("name",""),
            "Score": score,
            "Rating": rating,
            "Reviews": reviews,
            "HasBooking": has_bk,
            "Hours": hours,
        })

    # Save to CSV
    import csv as _csv
    niche_slug = niche.replace(" ","_")
    client_slug = f"_{client}" if client else ""
    filename = f"{niche_slug}_{city.replace(' ','_')}{client_slug}_{datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    filepath = DAILY_LEADS_DIR / filename
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["Name","City","Website","Problem","Email","Phone","Owner","Score","Rating","Reviews","HasBooking","Hours"])
        w.writeheader()
        w.writerows(leads_out)

    return JSONResponse({"ok": True, "count": len(leads_out), "file": filename, "leads": leads_out})


@app.post("/api/engine-send")
async def engine_send(request: Request):
    user = get_current_user(request)
    if not user: return JSONResponse({"detail":"Not authenticated"}, status_code=401)
    if not RESEND_API_KEY: return JSONResponse({"detail":"RESEND_API_KEY not set"}, status_code=500)
    if not OPENAI_API_KEY: return JSONResponse({"detail":"OPENAI_API_KEY not set"}, status_code=500)

    data   = await request.json()
    fname  = data.get("file","").strip()
    client = data.get("client","").strip()

    # Load the CSV
    filepath = DAILY_LEADS_DIR / fname
    if not filepath.exists():
        return JSONResponse({"detail": f"File not found: {fname}"}, status_code=404)

    try:
        df = pd.read_csv(filepath).fillna("")
        leads = df.to_dict(orient="records")
    except Exception as e:
        return JSONResponse({"detail": f"Could not read file: {e}"}, status_code=400)

    # Get client booking link
    booking_url = "https://app.lumeraautomation.com/book"
    from_name   = "Kory"
    if client:
        client_rows = db_query("SELECT * FROM clients WHERE username=?", (client,))
        if client_rows:
            c = client_rows[0]
            # Try to find their booking link in notes or use default
            if c.get("notes") and "http" in str(c.get("notes","")):
                import re as _re
                urls = _re.findall(r'https?://\S+', c.get("notes",""))
                if urls: booking_url = urls[0]
            # Use their niche for email context
            client_niche = c.get("niche","")
            client_biz   = c.get("business","")

    from openai import OpenAI
    _oai = OpenAI(api_key=OPENAI_API_KEY)
    import resend as _r
    _r.api_key = RESEND_API_KEY

    sent = failed = 0
    for lead in leads:
        email = str(lead.get("Email","")).strip()
        if not email or "@" not in email: continue

        name    = str(lead.get("Name","there"))
        owner   = str(lead.get("Owner","")) or name.split()[0]
        city    = str(lead.get("City",""))
        problem = str(lead.get("Problem",""))
        niche   = str(lead.get("City","")).lower()
        reviews = str(lead.get("Reviews",""))
        has_bk  = str(lead.get("HasBooking",""))
        hours   = str(lead.get("Hours",""))

        is_restaurant = "restaurant" in fname.lower() or "restaurant" in niche

        if is_restaurant:
            prompt = (
                f"Write a short cold email pitching an AI phone receptionist to a restaurant.\n"
                f"Restaurant: {name}, {city}\n"
                f"Problem: {problem}\n"
                f"Reviews: {reviews}\nHas online booking: {has_bk}\nHours: {hours}\n"
                f"Owner name: {owner}\n\n"
                "Pain points: missed calls during dinner rush = lost reservations. No online booking = 100% phone dependent. "
                "High review count means busy and missing calls. Goes dark after hours.\n\n"
                "Rules: Address owner by first name. Subject under 10 words. 3 short paragraphs. Conversational not salesy. "
                f"Reference their specific situation. CTA: check it out at {booking_url}\n"
                "Sign off: Kory. No 'I hope this finds you well'.\n"
                'Return ONLY JSON: {"subject":"...","body":"..."}'
            )
        else:
            prompt = (
                f"Write a short cold outreach email for a local service business.\n"
                f"Business: {name}, {city}\nProblem: {problem}\nOwner: {owner}\n\n"
                "Rules: Address by first name. Subject under 10 words. 3 short paragraphs. Conversational not salesy.\n"
                f"CTA: get started at {booking_url}\nSign off: Kory, Lumera Automation.\n"
                'Return ONLY JSON: {"subject":"...","body":"..."}'
            )

        try:
            res = _oai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":prompt}],
                max_tokens=500, temperature=0.8
            )
            raw = res.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
            email_data = json.loads(raw)
            subject = email_data.get("subject","Following up on your business")
            body    = email_data.get("body","")

            _r.Emails.send({
                "from": f"{from_name} @ Lumera Automation <{FROM_EMAIL}>",
                "to": email,
                "subject": subject,
                "html": f"<div style='font-family:sans-serif;max-width:560px;margin:0 auto;color:#222;line-height:1.7'>{body.replace(chr(10),'<br>')}</div>"
            })
            enroll_lead(email, name, name, fname.split("_")[0], city, problem)
            sent += 1
        except Exception as e:
            print(f"Send error for {email}: {e}")
            failed += 1

    return JSONResponse({"ok": True, "sent": sent, "failed": failed})


# ─────────────────────────────────────────────
# ONE-TIME SETUP
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# CLIENT PORTAL
# ─────────────────────────────────────────────
def is_client(username: str) -> bool:
    if not username: return False
    if username == ADMIN_USER: return False
    rows = db_query("SELECT id FROM clients WHERE username=?", (username,))
    return len(rows) > 0

@app.get("/client-home", response_class=HTMLResponse)
def client_home(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    if not is_client(user): return RedirectResponse("/overview")

    outreach = get_all_outreach()
    bookings = get_all_bookings()
    total_sent    = len(outreach)
    replied       = sum(1 for r in outreach if r.get("replied"))
    pending       = sum(1 for r in outreach if not r.get("replied") and not r.get("unsubscribed") and r.get("step",1) < 3)
    upcoming_book = [b for b in bookings if b["start_time"] >= datetime.now().isoformat() and b["status"] == "confirmed"]

    actions_html = ""
    if upcoming_book:
        b = upcoming_book[0]
        try: dts = datetime.fromisoformat(b["start_time"]).strftime("%A %b %d at %I:%M %p")
        except: dts = b["start_time"][:16]
        meet = b.get("meet_link","")
        meet_btn = f'<a href="{meet}" target="_blank" class="btn btn-grad"><i class="fa-solid fa-video"></i> Join Call</a>' if meet else ""
        actions_html += f'''<div class="action-card" style="border-left-color:#22c55e">
          <h3><i class="fa-solid fa-calendar-check" style="color:#22c55e;margin-right:8px"></i>You have a call coming up</h3>
          <p>Your strategy call is scheduled for <strong>{dts} CT</strong>. Make sure you're ready 5 minutes early.</p>
          {meet_btn}</div>'''
    if replied:
        actions_html += f'''<div class="action-card" style="border-left-color:#3b82f6">
          <h3><i class="fa-solid fa-reply" style="color:#3b82f6;margin-right:8px"></i>{replied} lead{"s" if replied>1 else ""} replied to your outreach</h3>
          <p>Someone responded. Check your email inbox or view details below.</p>
          <a href="/client-emails" class="btn btn-grad"><i class="fa-solid fa-envelope"></i> View Replies</a></div>'''
    if pending > 0:
        actions_html += f'''<div class="action-card">
          <h3><i class="fa-solid fa-rotate" style="color:var(--indigo);margin-right:8px"></i>{pending} follow-up emails going out soon</h3>
          <p>Your automated follow-up sequence is running. These emails go out automatically — no action needed.</p></div>'''
    if not actions_html:
        actions_html = '''<div class="action-card" style="border-left-color:var(--muted)">
          <h3><i class="fa-solid fa-circle-check" style="color:var(--muted);margin-right:8px"></i>Everything is running smoothly</h3>
          <p>Your outreach is active. New leads are added weekly and emails go out automatically. Check back soon for updates.</p></div>'''

    _ci = db_query("SELECT * FROM clients WHERE username=?", (user,))
    _niche = _ci[0].get("niche","") if _ci else ""
    _biz   = _ci[0].get("business","") if _ci else ""

    def mcard(icon, label, value, sub="", color="#818cf8", bg="rgba(99,102,241,.12)"):
        return (
            f'<div class="metric-card"><div class="m-icon" style="background:{bg};color:{color}">{icon}</div>'
            f'<div class="m-label">{label}</div><div class="m-val" style="color:{color}">{value}</div>'
            f'<div class="m-sub">{sub}</div></div>'
        )

    nb = f'<span style="font-weight:700;color:var(--indigo)">{_niche}</span><span style="color:var(--muted)"> &middot; </span>' if _niche else ""
    bb = f'<span>{_biz}</span><span style="color:var(--muted)"> &middot; </span>' if _biz else ""

    content_html = (
        f'<div class="page-hdr"><div><div class="page-title">Welcome back, {user} \U0001f44b</div>'
        f'<div class="page-sub">{nb}{bb}<span style="color:var(--green);font-weight:700">&#9679; Active</span></div></div>'
        '<a href="/book" class="btn btn-grad"><i class="fa-solid fa-rocket"></i> Get Started</a></div>'
        '<div class="metrics-grid">'
        + mcard('<i class="fa-solid fa-paper-plane"></i>', "Emails Sent", total_sent, "total outreach sent")
        + mcard('<i class="fa-solid fa-reply"></i>', "Replies Received", replied, "leads interested", "#4ade80", "rgba(34,197,94,.12)")
        + mcard('<i class="fa-solid fa-rotate"></i>', "Follow-ups Pending", pending, "going out automatically", "#fbbf24", "rgba(245,158,11,.12)")
        + mcard('<i class="fa-solid fa-calendar-check"></i>', "Calls Booked", len(upcoming_book), "upcoming", "#60a5fa", "rgba(59,130,246,.12)")
        + '</div>'
        + '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--muted2);margin-bottom:12px;display:flex;align-items:center;gap:6px">'
        + '<i class="fa-solid fa-bell" style="color:var(--indigo)"></i> What needs your attention</div>'
        + actions_html
    )
    return HTMLResponse(shell_client(content_html, "client-home", user))


@app.get("/client-leads", response_class=HTMLResponse)
def client_leads(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    if not is_client(user): return RedirectResponse("/leads")

    client_rows = db_query("SELECT * FROM clients WHERE username=?", (user,))
    client_niche = client_rows[0]["niche"] if client_rows else "*"
    leads = load_all_leads()
    if client_niche != "*":
        leads = [l for l in leads if l.get("_niche","").lower() == client_niche.lower()]

    total = len(leads)
    hot   = sum(1 for l in leads if l["_heat"] == "hot")
    rows_html = ""
    for l in leads:
        name    = str(l.get("Name","—"))
        city    = str(l.get("City","—"))
        phone   = str(l.get("Phone","")) or "—"
        website = str(l.get("Website","—"))
        problem = str(l.get("Problem","—"))
        heat    = l.get("_heat","cold")
        heat_color = "#f43f5e" if heat=="hot" else "#f59e0b" if heat=="warm" else "#6366f1"
        heat_label = "High Need" if heat=="hot" else "Medium Need" if heat=="warm" else "Low Need"
        has_web = website.lower() not in ["none listed","none","n/a","","nan"]
        web_cell = f'<a href="{website}" target="_blank" style="color:var(--blue)">{website[:28]}...</a>' if has_web else '<span style="color:var(--muted)">No website</span>'
        rows_html += f'''<tr>
          <td style="font-weight:700">{name}</td>
          <td style="color:var(--muted);font-size:12px">{city}</td>
          <td style="font-size:12px">{phone}</td>
          <td>{web_cell}</td>
          <td style="font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{problem}</td>
          <td><span style="font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;background:{heat_color}22;color:{heat_color}">{heat_label}</span></td>
        </tr>'''

    content_html = (
        '<div class="page-hdr"><div><div class="page-title">My Leads</div>'
        f'<div class="page-sub">{total} leads &nbsp;&middot;&nbsp; {hot} high priority</div></div></div>'
        '<div class="card"><div class="card-title">Your Lead Pipeline</div>'
        '<div class="tbl-wrap"><table>'
        '<thead><tr><th>Business</th><th>City</th><th>Phone</th><th>Website</th><th>Why They Need You</th><th>Priority</th></tr></thead>'
        f'<tbody>{rows_html or '<tr><td colspan="6" class="empty-state">Your leads will appear here once your first batch is ready.</td></tr>'}</tbody>'
        '</table></div></div>'
    )
    return HTMLResponse(shell_client(content_html, "client-leads", user))


@app.get("/client-emails", response_class=HTMLResponse)
def client_emails(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login")
    if not is_client(user): return RedirectResponse("/outreach")

    outreach = get_all_outreach()
    total   = len(outreach)
    replied = sum(1 for r in outreach if r.get("replied"))
    pending = sum(1 for r in outreach if not r.get("replied") and not r.get("unsubscribed") and r.get("step",1) < 3)

    rows_html = ""
    for r in outreach:
        replied_f = r.get("replied",0)
        unsub     = r.get("unsubscribed",0)
        step      = r.get("step",1)
        email     = r.get("email","")
        business  = r.get("business","—")
        enrolled  = r.get("enrolled_at","")[:10]
        if replied_f:
            status_html = '<span class="badge b-replied">Replied ✓</span>'
            next_html   = '<span style="color:#22c55e;font-size:12px">Done</span>'
        elif unsub:
            status_html = '<span class="badge b-unsubscribed">Unsubscribed</span>'
            next_html   = '—'
        else:
            status_html = '<span class="badge b-sent">Active</span>'
            try:
                ns = datetime.fromisoformat(r.get("next_send_at","")) if r.get("next_send_at") else None
                next_html = f'<span style="font-size:12px;color:var(--muted)">{ns.strftime("%b %d") if ns else "Done"}</span>'
            except: next_html = '—'
        step_dots = "".join(
            f'<div style="width:8px;height:8px;border-radius:50%;background:{"#6366f1" if i<=step else "rgba(255,255,255,0.1)"}"></div>'
            for i in range(1,4)
        )
        rows_html += f'''<tr>
          <td style="font-weight:700">{business}</td>
          <td style="font-size:12px;color:var(--muted)">{email}</td>
          <td>{status_html}</td>
          <td><div style="display:flex;gap:4px;align-items:center">{step_dots}<span style="font-size:11px;color:var(--muted);margin-left:4px">Step {step}/3</span></div></td>
          <td>{next_html}</td>
          <td style="font-size:11px;color:var(--muted)">{enrolled}</td>
        </tr>'''

    def mcard(icon, label, value, sub="", color="#818cf8", bg="rgba(99,102,241,.12)"):
        return (
            f'<div class="metric-card"><div class="m-icon" style="background:{bg};color:{color}">{icon}</div>'
            f'<div class="m-label">{label}</div><div class="m-val" style="color:{color}">{value}</div>'
            f'<div class="m-sub">{sub}</div></div>'
        )

    content_html = (
        '<div class="page-hdr"><div><div class="page-title">My Emails</div>'
        '<div class="page-sub">Track every email sent on your behalf</div></div></div>'
        '<div class="metrics-grid">'
        + mcard('<i class="fa-solid fa-paper-plane"></i>', "Total Sent", total, "emails sent")
        + mcard('<i class="fa-solid fa-reply"></i>', "Replied", replied, "interested leads", "#4ade80", "rgba(34,197,94,.12)")
        + mcard('<i class="fa-solid fa-rotate"></i>', "Pending", pending, "in sequence", "#fbbf24", "rgba(245,158,11,.12)")
        + '</div>'
        + '<div class="card"><div class="tbl-wrap"><table>'
        + '<thead><tr><th>Business</th><th>Email</th><th>Status</th><th>Sequence</th><th>Next Send</th><th>Enrolled</th></tr></thead>'
        + f'<tbody>{rows_html or '<tr><td colspan="6" class="empty-state">No emails sent yet.</td></tr>'}</tbody>'
        + '</table></div></div>'
    )
    return HTMLResponse(shell_client(content_html, "client-emails", user))


@app.get("/check-veturnai")
def check_veturnai():
    rows = db_query("SELECT username, password, business, niche, status FROM clients WHERE username='veturnai'")
    # Also test password match directly
    match = False
    if rows:
        match = rows[0]["password"] == "trial2026"
    return JSONResponse({"rows": rows, "password_match": match, "stored_pw_repr": repr(rows[0]["password"]) if rows else None})

@app.get("/setup-veturnai")
def setup_veturnai():
    try:
        # First check what columns exist
        with __import__('sqlite3').connect(DB_PATH) as conn:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]

        # Build insert with only existing columns
        data = {
            "username": "veturnai",
            "password": "trial2026",
            "status": "active",
            "created_at": datetime.now().isoformat(),
        }
        optional = {
            "niche": "restaurant",
            "email": "kory@lumeraautomation.com",
            "business": "Veturn AI",
            "monthly_fee": 0,
            "setup_fee": 0,
            "start_date": datetime.now().strftime("%Y-%m-%d"),
            "notes": "Trial client — NJ restaurants — booking: veturn.ai/contact",
        }
        for k, v in optional.items():
            if k in cols:
                data[k] = v

        keys = ", ".join(data.keys())
        placeholders = ", ".join(["?" for _ in data])
        db_run(f"INSERT OR REPLACE INTO clients ({keys}) VALUES ({placeholders})", list(data.values()))
        return JSONResponse({"ok": True, "message": "veturnai client created — login at /login", "cols": cols})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ─────────────────────────────────────────────
# API ROUTES
# ─────────────────────────────────────────────
@app.post("/api/generate-email")
async def generate_email(request: Request):
    if not OPENAI_API_KEY:
        return JSONResponse({"detail":"OPENAI_API_KEY not set"},status_code=500)
    from openai import OpenAI
    lead=await request.json()
    niche = lead.get('_niche','').lower()
    is_restaurant = any(x in niche for x in ['restaurant','food','dining','cafe','bar'])

    if is_restaurant:
        prompt=f"""You are an outreach specialist helping VeturnAI sell AI phone receptionists to restaurants.

Write a short cold outreach email to a restaurant owner:
- Restaurant: {lead.get('Name','there')}
- City: {lead.get('City','')}
- Problem: {lead.get('Problem','')}
- Reviews: {lead.get('Reviews','')} (high review count = high call volume = missing calls)
- Has online booking: {lead.get('HasBooking','unknown')}
- Hours: {lead.get('Hours','')}
{"- Owner: "+lead.get('Owner','') if lead.get('Owner') else ""}

The pitch: VeturnAI is an AI phone receptionist that answers calls 24/7, takes reservations, answers menu questions, and handles to-go orders — even during dinner rush and after close.

Pain points to reference (use whichever fits their data):
- Missed calls during dinner rush = lost reservations and revenue
- Voicemail after 10pm = customers calling the next place that picks up
- No online booking = 100% phone dependent, every missed call is a missed table
- High review count means they are busy and definitely missing calls

Rules: Address by first name if known. Subject under 10 words. Conversational, not salesy.
Body: 3 short paragraphs. Make it feel like you noticed a specific problem with their restaurant.
CTA: Check it out at veturn.ai/contact
Sign off: Kory. No "I hope this finds you well". No corporate speak.
Return ONLY valid JSON: {{"subject":"...","body":"..."}}"""
    else:
        prompt=f"""You are an outreach specialist for Lumera Automation, which builds AI lead generation systems for local service businesses.

Write a short personalised cold outreach email:
- Business: {lead.get('Name','there')}
- City: {lead.get('City','')}
- Niche: {lead.get('_niche','local business')}
- Problem: {lead.get('Problem','')}
- Website: {lead.get('Website','')}
{"- Owner: "+lead.get('Owner','') if lead.get('Owner') else ""}
{"- Extra: "+lead.get('_tone','') if lead.get('_tone') else ""}

Rules: Address by first name if known. Subject under 10 words specific to their problem.
Body: 3 short paragraphs, conversational, not salesy. Reference their problem.
CTA: get started at https://app.lumeraautomation.com/book
Sign off: Kory, Lumera Automation. No "I hope this finds you well".
Return ONLY valid JSON: {{"subject":"...","body":"..."}}"""
    try:
        client=OpenAI(api_key=OPENAI_API_KEY)
        res=client.chat.completions.create(model="gpt-4o-mini",
            messages=[{"role":"user","content":prompt}],max_tokens=500,temperature=0.8)
        raw=res.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        return JSONResponse(json.loads(raw))
    except Exception as e:
        return JSONResponse({"detail":str(e)},status_code=500)

@app.post("/api/send-email")
async def send_email(request: Request):
    if not RESEND_API_KEY:
        return JSONResponse({"detail":"RESEND_API_KEY not set"},status_code=500)
    import resend as r; r.api_key=RESEND_API_KEY
    data=await request.json()
    to=data.get("to","").strip(); subject=data.get("subject","").strip()
    body=data.get("body","").strip(); lead=data.get("lead",{})
    if not re.match(r"[^@]+@[^@]+\.[^@]+",to):
        return JSONResponse({"detail":"Invalid email"},status_code=400)
    try:
        r.Emails.send({"from":f"Kory @ Lumera Automation <{FROM_EMAIL}>","to":to,"subject":subject,
            "html":f"<div style='font-family:sans-serif;max-width:560px;margin:0 auto;color:#222;line-height:1.6'>{body.replace(chr(10),'<br>')}</div>"})
        enroll_lead(to,lead.get("Name",""),lead.get("Name",""),lead.get("_niche",""),lead.get("City",""),lead.get("Problem",""))
        return JSONResponse({"ok":True})
    except Exception as e:
        return JSONResponse({"detail":str(e)},status_code=500)

@app.post("/api/outreach/replied")
async def api_replied(request: Request):
    data=await request.json(); mark_replied(data.get("email","")); return JSONResponse({"ok":True})

@app.post("/api/outreach/unsubscribed")
async def api_unsub(request: Request):
    data=await request.json(); mark_unsubscribed(data.get("email","")); return JSONResponse({"ok":True})

@app.post("/api/pipeline")
async def add_pipeline(request: Request):
    data=await request.json()
    if not data.get("business"):
        return JSONResponse({"detail":"Business required"},status_code=400)
    now=datetime.now().isoformat()
    db_run("""INSERT INTO sales_pipeline(business,contact,email,value,stage,notes,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (data["business"],data.get("contact",""),data.get("email",""),
         data.get("value",0),data.get("stage","prospect"),data.get("notes",""),now,now))
    return JSONResponse({"ok":True})

@app.post("/api/clients")
async def add_client(request: Request):
    data=await request.json()
    if not data.get("username") or not data.get("password"):
        return JSONResponse({"detail":"Username and password required"},status_code=400)
    if db_query("SELECT id FROM clients WHERE username=?",(data["username"],)):
        return JSONResponse({"detail":"Username already exists"},status_code=409)
    db_run("""INSERT INTO clients(username,password,niche,email,business,monthly_fee,setup_fee,status,start_date,created_at)
        VALUES(?,?,?,?,?,?,?,'active',?,?)""",
        (data["username"],data["password"],data.get("niche","*"),data.get("email",""),
         data.get("business",""),data.get("monthly_fee",497),data.get("setup_fee",1000),
         datetime.now().strftime("%Y-%m-%d"),datetime.now().isoformat()))
    return JSONResponse({"ok":True})

@app.delete("/api/clients/{username}")
async def delete_client_route(username: str):
    db_run("DELETE FROM clients WHERE username=?",(username,)); return JSONResponse({"ok":True})

@app.post("/api/system/scrape")
async def run_scraper():
    import subprocess
    script=SCRIPTS_DIR/"daily-leads.sh"
    if not script.exists():
        return JSONResponse({"message":"Scraper script not found"},status_code=404)
    try:
        result=subprocess.run(["bash",str(script)],capture_output=True,text=True,timeout=120)
        return JSONResponse({"message":result.stdout.strip() or "Scraper completed","ok":True})
    except subprocess.TimeoutExpired:
        return JSONResponse({"message":"Scraper timed out (running in background)"},status_code=200)
    except Exception as e:
        return JSONResponse({"message":str(e)},status_code=500)


@app.post("/api/send-all-pending")
async def send_all_pending(request: Request):
    """Generate and send emails to all leads with valid emails not yet in outreach DB."""
    if not RESEND_API_KEY or not OPENAI_API_KEY:
        return JSONResponse({"detail": "API keys not set"}, status_code=500)

    from openai import OpenAI
    import resend as r
    r.api_key = RESEND_API_KEY
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Get already-enrolled emails
    enrolled = {row["email"] for row in get_all_outreach()}

    # Load all leads with valid emails not yet enrolled
    leads = load_all_leads()
    pending = []
    for lead in leads:
        email = re.sub(r"\[\d+\]", "", str(lead.get("Email", ""))).strip()
        if (email and "@" in email and "example.com" not in email
                and "None" not in email and email not in enrolled):
            lead["_email_clean"] = email
            pending.append(lead)

    if not pending:
        return JSONResponse({"ok": True, "sent": 0, "failed": 0, "message": "No pending leads found"})

    sent = failed = 0
    for lead in pending:
        email = lead["_email_clean"]
        prompt = f"""You are an outreach specialist for Lumera Automation, which builds AI lead generation systems for local service businesses.

Write a short personalised cold outreach email:
- Business: {lead.get('Name','there')}
- City: {lead.get('City','')}
- Niche: {lead.get('_niche','local business')}
- Problem: {lead.get('Problem','')}
- Website: {lead.get('Website','')}
{"- Owner: "+lead.get('Owner','') if lead.get('Owner') else ""}

Rules: Address by first name if known. Subject under 10 words specific to their problem.
Body: 3 short paragraphs, conversational, not salesy. Reference their problem.
CTA: get started at https://app.lumeraautomation.com/book
Sign off: Kory, Lumera Automation. No "I hope this finds you well".
Return ONLY valid JSON: {{"subject":"...","body":"..."}}"""
        try:
            res = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500, temperature=0.8)
            raw = res.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
            data = json.loads(raw)
            r.Emails.send({
                "from": f"Kory @ Lumera Automation <{FROM_EMAIL}>",
                "to": email,
                "subject": data["subject"],
                "html": f"<div style='font-family:sans-serif;max-width:560px;margin:0 auto;color:#222;line-height:1.6'>{data['body'].replace(chr(10),'<br>')}</div>"
            })
            enroll_lead(email, lead.get("Name",""), lead.get("Name",""),
                        lead.get("_niche",""), lead.get("City",""), lead.get("Problem",""))
            sent += 1
        except Exception as e:
            print(f"Send all pending failed for {email}: {e}")
            failed += 1

    return JSONResponse({"ok": True, "sent": sent, "failed": failed,
                         "message": f"{sent} sent, {failed} failed"})

@app.post("/cron/followups")
async def run_followups():
    if not RESEND_API_KEY or not OPENAI_API_KEY:
        return JSONResponse({"detail":"API keys not set"},status_code=500)
    from openai import OpenAI
    import resend as r; r.api_key=RESEND_API_KEY
    client=OpenAI(api_key=OPENAI_API_KEY)
    due=get_pending_followups(); sent=failed=0
    for lead in due:
        new_step=lead["step"]+1
        label="follow-up" if new_step==2 else "final follow-up"
        prompt=f"""Write a short {label} email to {lead['business']} who didn't reply to our first outreach.
Problem: {lead['problem']} | Niche: {lead['niche']}
#{new_step-1} of 2. 2 paragraphs max. Friendly, not pushy. Reference previous outreach.
CTA: book at https://app.lumeraautomation.com/book. Sign off: Kory, Lumera Automation.
Return ONLY JSON: {{"subject":"...","body":"..."}}"""
        try:
            res=client.chat.completions.create(model="gpt-4o-mini",
                messages=[{"role":"user","content":prompt}],max_tokens=400,temperature=0.8)
            data=json.loads(res.choices[0].message.content.strip().replace("```json","").replace("```","").strip())
            r.Emails.send({"from":f"Kory @ Lumera Automation <{FROM_EMAIL}>","to":lead["email"],
                "subject":data["subject"],
                "html":f"<div style='font-family:sans-serif;max-width:560px;margin:0 auto;color:#222;line-height:1.6'>{data['body'].replace(chr(10),'<br>')}</div>"})
            mark_followup_sent(lead["id"],new_step); sent+=1
        except Exception as e:
            print(f"Follow-up failed: {e}"); failed+=1
    return JSONResponse({"ok":True,"sent":sent,"failed":failed,"total_due":len(due)})
