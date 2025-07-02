#!/usr/bin/env python3
# scrape.py  –  NPS Looker-Studio scraper with headless auto-login
import os, sys, csv, time, logging, re, requests, configparser
from getpass import getpass
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)

# ─────────────────────────────────────────────────────────────────── LOGGING ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("scrape.log", encoding="utf-8"),
              logging.StreamHandler()],
)
logger = logging.getLogger(__name__)
logging.getLogger("playwright").setLevel(logging.WARNING)

# ───────────────────────────────────────────────────────────────── CONFIG ──
cfg = configparser.ConfigParser()
cfg.read("config.ini", encoding="utf-8")
def opt(key): return os.getenv(key) or cfg["DEFAULT"].get(key, "")

GOOGLE_EMAIL, GOOGLE_PASSWORD = opt("GOOGLE_EMAIL"), opt("GOOGLE_PASSWORD")
MAIN_WEBHOOK, ALERT_WEBHOOK   = opt("MAIN_WEBHOOK"), opt("ALERT_WEBHOOK")

if not GOOGLE_EMAIL or not GOOGLE_PASSWORD:
    logger.critical("GOOGLE_EMAIL and/or GOOGLE_PASSWORD are missing.")
    sys.exit(1)
if not MAIN_WEBHOOK:
    logger.critical("MAIN_WEBHOOK missing; cannot post comments.")
    sys.exit(1)

# ────────────────────────────────────────────────────────────── CONSTANTS ──
AUTH_STATE_PATH     = "auth_state.json"
COMMENTS_LOG_PATH   = "comments_log.csv"
LOOKER_STUDIO_URL   = "https://lookerstudio.google.com/reporting/b69cfd73-8c0a-453d-9c10-6561fa953f7c/page/p_bghtutfsbd"

DEFAULT_NAV_TIMEOUT   = 60_000
DEFAULT_SEL_TIMEOUT   = 30_000
LOGIN_SUCCESS_TIMEOUT = 120_000
TWO_FA_WAIT_TIMEOUT   = 180_000      # wait up to 3 min for push approval

# ─────────────────────────────────────────────────────────────── ALERTS ──
def send_alert(url, msg):
    if not url or "chat.googleapis.com" not in url:
        logger.warning("Alert webhook invalid")
        return
    try:
        requests.post(url, json={"text": msg}, timeout=15).raise_for_status()
        logger.info("Alert sent")
    except Exception as e:
        logger.error(f"Alert failed: {e}")

def alert_login_needed(reason):
    send_alert(ALERT_WEBHOOK, f"🚨 LOGIN NEEDED: {reason}")

# ───────────────────────────────────────────────  HEADLESS AUTO-LOGIN ──
def auto_login_and_save_state(pw_page) -> bool:
    """
    Fully headless login with email & password secrets.
    Assumes 2-factor is a push notification you approve on your phone.
    """
    logger.info("Auto-login: navigating to Google sign-in page …")
    try:
        pw_page.goto("https://accounts.google.com/", timeout=DEFAULT_NAV_TIMEOUT)

        # 1) Email
        email_sel = "input[type='email']"
        pw_page.wait_for_selector(email_sel, timeout=DEFAULT_SEL_TIMEOUT)
        pw_page.fill(email_sel, GOOGLE_EMAIL)
        pw_page.get_by_role("button", name=re.compile("Next", re.I)).click()

        # 2) Password
        pwd_sel = "input[type='password']"
        pw_page.wait_for_selector(pwd_sel, timeout=DEFAULT_SEL_TIMEOUT)
        pw_page.fill(pwd_sel, GOOGLE_PASSWORD)
        pw_page.get_by_role("button", name=re.compile("Next", re.I)).click()

        logger.info("Password submitted — waiting for your phone push (≤3 min)…")
        pw_page.wait_for_url("https://myaccount.google.com/?pli=1",
                             timeout=TWO_FA_WAIT_TIMEOUT)
        pw_page.context.storage_state(path=AUTH_STATE_PATH)
        logger.info(f"Auto-login succeeded; state saved → {AUTH_STATE_PATH}")
        return True

    except PlaywrightTimeoutError:
        logger.error("Auto-login timed-out (2FA not approved in time?)")
    except Exception as e:
        logger.error(f"Auto-login error: {e}", exc_info=True)
    return False

# ─────────────────────────────────────────────── PAGE TEXT EXTRACT ──
def copy_looker_text(page):
    page.goto(LOOKER_STUDIO_URL, timeout=DEFAULT_NAV_TIMEOUT, wait_until="load")
    if "accounts.google.com" in page.url:
        logger.warning("Redirected to login (auth invalid)")
        return None
    page.wait_for_timeout(10_000)
    if "accounts.google.com" in page.url:
        logger.warning("Redirected to login after wait (auth invalid)")
        return None
    try:
        return page.locator("body").inner_text().splitlines()
    except Exception as e:
        logger.error(f"Could not read body text: {e}")
        return []

# ──────────────────────────────────────────────────── PARSER + IO ──
store_re = re.compile(r"^\d+\s+.*")
score_re = re.compile(r"^[0-9]{1,2}$")

def parse_comments(lines):
    comments, idx, n = [], 0, len(lines)
    while idx < n:
        line = lines[idx].strip()
        if store_re.match(line):
            store = line
            idx += 1; ts = lines[idx].strip() if idx < n else ""
            idx += 1; text_lines = []; score = ""
            while idx < n and not score_re.match(lines[idx].strip()):
                text_lines.append(lines[idx].strip()); idx += 1
            if idx < n:
                score = lines[idx].strip(); idx += 1
            comments.append({"store":store,"timestamp":ts,
                             "comment":"\n".join(text_lines).strip(),
                             "score":score})
        else: idx += 1
    return comments

def read_seen():
    seen=set()
    if not os.path.exists(COMMENTS_LOG_PATH): return seen
    with open(COMMENTS_LOG_PATH, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f,
                fieldnames=["store","timestamp","comment","score"]):
            seen.add((r["store"],r["timestamp"],r["comment"]))
    return seen

def append_comments(new):
    with open(COMMENTS_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w=csv.writer(f); [w.writerow([c["store"],c["timestamp"],
                                      c["comment"],c["score"]]) for c in new]

# ─────────────────────────────────────────────── GOOGLE CHAT POST ──
def post_comment(c):
    if not MAIN_WEBHOOK or "chat.googleapis.com" not in MAIN_WEBHOOK:
        logger.error("MAIN_WEBHOOK invalid"); return
    try: score=int(c["score"] or 0)
    except ValueError: score=0
    emoji,label = ("🔴","Detractor") if score<=4 else ("🟠","Passive") if score<=7 else ("🟢","Promoter")
    payload={
      "cards":[{
        "header":{"title":"New NPS Comment",
                  "subtitle":f"{emoji} {c['store']} ({label})"},
        "sections":[{"widgets":[
          {"keyValue":{"topLabel":"Timestamp","content":c["timestamp"]}},
          {"keyValue":{"topLabel":"Score","content":str(score)}},
          {"textParagraph":{"text":c["comment"].replace('\n','<br>')}}
        ]}]
      }]
    }
    try:
        requests.post(MAIN_WEBHOOK, json=payload, timeout=15).raise_for_status()
        logger.info(f"Posted comment {c['timestamp']}")
    except Exception as e:
        logger.error(f"Post failed: {e}")

# ────────────────────────────────────────────────────── MAIN FLOW ──
def scrape_once():
    # Ensure we have a valid auth_state.json (auto-login if missing)
    if not os.path.exists(AUTH_STATE_PATH):
        logger.warning("auth_state.json missing ⇒ headless auto-login")
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context()
            if not auto_login_and_save_state(ctx.new_page()):
                alert_login_needed("Auto-login failed")
                ctx.close(); br.close(); return
            ctx.close(); br.close()

    # Headless scrape using stored auth
    with sync_playwright() as p:
        br  = p.chromium.launch(headless=True)
        ctx = br.new_context(storage_state=AUTH_STATE_PATH)
        lines = copy_looker_text(ctx.new_page())
        ctx.close(); br.close()

    if lines is None:
        logger.warning("Auth rejected ⇒ trying one auto-login refresh")
        os.remove(AUTH_STATE_PATH)
        scrape_once()            # single retry
        return
    if not lines:
        logger.info("No text lines found")
        return

    comments = parse_comments(lines)
    seen = read_seen()
    new  = [c for c in comments if (c["store"],c["timestamp"],c["comment"]) not in seen]
    if not new:
        logger.info("No new comments")
        return

    logger.info(f"{len(new)} new comments → posting …")
    for c in new:
        post_comment(c); time.sleep(1.0)
    append_comments(new)
    logger.info("All done")

# ────────────────────────────────────────────────────── CLI ENTRY ──
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower()=="login":
        if os.path.exists(AUTH_STATE_PATH): os.remove(AUTH_STATE_PATH)
        scrape_once()
    else:
        scrape_once()
