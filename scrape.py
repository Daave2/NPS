#!/usr/bin/env python3
# scrape.py  – NPS Looker-Studio scraper with headless auto-login,
#              verbose logging, and failure screenshots
import os, sys, csv, time, logging, re, requests, configparser, datetime
from pathlib import Path
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)

# ─────────────────────────────────────────────────────────────── LOGGING ──
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"scrape_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"

logging.basicConfig(
    level=logging.INFO,                # change to DEBUG for full dumps
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("scraper")
logging.getLogger("playwright").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────── CONFIG ──
cfg = configparser.ConfigParser()
cfg.read("config.ini", encoding="utf-8")
def opt(k): return os.getenv(k) or cfg["DEFAULT"].get(k, "")

GOOGLE_EMAIL, GOOGLE_PASSWORD = opt("GOOGLE_EMAIL"), opt("GOOGLE_PASSWORD")
MAIN_WEBHOOK, ALERT_WEBHOOK   = opt("MAIN_WEBHOOK"), opt("ALERT_WEBHOOK")
if not (GOOGLE_EMAIL and GOOGLE_PASSWORD and MAIN_WEBHOOK):
    logger.critical("Missing GOOGLE_EMAIL, GOOGLE_PASSWORD or MAIN_WEBHOOK.")
    sys.exit(1)

# ─────────────────────────────────────────────────────────── CONSTANTS ──
AUTH_STATE_PATH   = "auth_state.json"
COMMENTS_LOG_PATH = "comments_log.csv"
LOOKER_URL        = "https://lookerstudio.google.com/reporting/b69cfd73-8c0a-453d-9c10-6561fa953f7c/page/p_bghtutfsbd"

NAV_TIMEOUT   = 60_000
SEL_TIMEOUT   = 30_000
LOGIN_TIMEOUT = 120_000
TWOFA_TIMEOUT = 180_000

SS_DIR = Path("screens")
SS_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────── ALERTS ──
def send_alert(msg):
    if not ALERT_WEBHOOK or "chat.googleapis.com" not in ALERT_WEBHOOK:
        logger.warning("ALERT_WEBHOOK not configured")
        return
    try:
        requests.post(ALERT_WEBHOOK, json={"text": msg}, timeout=15).raise_for_status()
        logger.info("Alert sent")
    except Exception as e:
        logger.error(f"Alert failed: {e}")

# ─────────────────────────────────────────────── HEADLESS LOGIN ──
def auto_login_and_save(ctx) -> bool:
    """Return True if login succeeded and state saved."""
    page = ctx.new_page()
    try:
        logger.info("Auto-login → opening accounts.google.com")
        page.goto("https://accounts.google.com/", timeout=NAV_TIMEOUT)

        page.wait_for_selector("input[type='email']", timeout=SEL_TIMEOUT)
        page.fill("input[type='email']", GOOGLE_EMAIL)
        page.get_by_role("button", name=re.compile("Next", re.I)).click()

        page.wait_for_selector("input[type='password']", timeout=SEL_TIMEOUT)
        page.fill("input[type='password']", GOOGLE_PASSWORD)
        page.get_by_role("button", name=re.compile("Next", re.I)).click()

        logger.info("Waiting for 2-factor push approval …")
        page.wait_for_url("https://myaccount.google.com/?pli=1", timeout=TWOFA_TIMEOUT)

        ctx.storage_state(path=AUTH_STATE_PATH)
        logger.info(f"Login OK → state saved → {AUTH_STATE_PATH}")
        return True

    except PlaywrightTimeoutError as e:
        ss = SS_DIR / f"login_timeout_{int(time.time())}.png"
        page.screenshot(path=ss)
        logger.error(f"Login timeout: {e}. Screenshot: {ss}")
    except Exception as e:
        ss = SS_DIR / f"login_error_{int(time.time())}.png"
        page.screenshot(path=ss)
        logger.error(f"Login error: {e}. Screenshot: {ss}", exc_info=True)
    return False
    finally:
        page.close()

# ────────────────────────────────────────────── SCRAPE PAGE ──
def fetch_page_lines(ctx):
    page = ctx.new_page()
    try:
        logger.info("Opening Looker Studio report …")
        page.goto(LOOKER_URL, timeout=NAV_TIMEOUT, wait_until="load")
        if "accounts.google.com" in page.url:
            logger.warning("Redirected to login")
            return None
        page.wait_for_timeout(10_000)
        if "accounts.google.com" in page.url:
            logger.warning("Redirected to login after wait")
            return None
        text = page.locator("body").inner_text()
        return text.splitlines()
    except Exception as e:
        ss = SS_DIR / f"scrape_error_{int(time.time())}.png"
        page.screenshot(path=ss)
        logger.error(f"Scrape error: {e}. Screenshot: {ss}", exc_info=True)
        return []
    finally:
        page.close()

# ────────────────────────────────────────────── UTILITIES ──
store_re, score_re = re.compile(r"^\d+\s+.*"), re.compile(r"^[0-9]{1,2}$")
def parse_comments(lines):
    out=[]; i=0; n=len(lines)
    while i<n:
        if store_re.match(lines[i].strip()):
            store = lines[i].strip()
            i+=1; ts = lines[i].strip() if i<n else ""
            i+=1; body=[]; score=""
            while i<n and not score_re.match(lines[i].strip()):
                body.append(lines[i].strip()); i+=1
            if i<n: score=lines[i].strip(); i+=1
            out.append({"store":store,"timestamp":ts,"comment":"\n".join(body),"score":score})
        else: i+=1
    return out

def read_seen():
    seen=set()
    if Path(COMMENTS_LOG_PATH).exists():
        with open(COMMENTS_LOG_PATH, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f, fieldnames=["store","timestamp","comment","score"]):
                seen.add((r["store"],r["timestamp"],r["comment"]))
    return seen

def append_comments(new):
    with open(COMMENTS_LOG_PATH,"a",newline="",encoding="utf-8") as f:
        w=csv.writer(f)
        for c in new: w.writerow([c["store"],c["timestamp"],c["comment"],c["score"]])

def post_chat(c):
    try: score=int(c["score"] or 0)
    except ValueError: score=0
    emo,lab = ("🔴","Detractor") if score<=4 else ("🟠","Passive") if score<=7 else ("🟢","Promoter")
    payload={"cards":[{"header":{"title":"New NPS Comment","subtitle":f"{emo} {c['store']} ({lab})"},"sections":[{"widgets":[
        {"keyValue":{"topLabel":"Timestamp","content":c["timestamp"]}},
        {"keyValue":{"topLabel":"Score","content":str(score)}},
        {"textParagraph":{"text":c["comment"].replace('\n','<br>')}}
    ]}]}]}
    try:
        requests.post(MAIN_WEBHOOK,json=payload,timeout=15).raise_for_status()
        logger.info(f"Posted comment {c['timestamp']}")
    except Exception as e:
        logger.error(f"Post failed: {e}")

# ─────────────────────────────────────────────── MAIN RUN ──
def scrape_once():
    with sync_playwright() as p:
        # ── ensure valid auth ──
        if not Path(AUTH_STATE_PATH).exists():
            logger.warning("No auth_state.json → attempting headless auto-login")
            ctx = p.chromium.launch(headless=True).new_context()
            if not auto_login_and_save(ctx):
                send_alert("Auto-login failed - manual intervention required")
                ctx.close(); return
            ctx.close()

        # ── headless scrape ──
        ctx = p.chromium.launch(headless=True).new_context(storage_state=AUTH_STATE_PATH)
        lines = fetch_page_lines(ctx)
        ctx.close()

    if lines is None:                       # auth rejected
        logger.warning("Auth rejected → trying one re-login cycle")
        Path(AUTH_STATE_PATH).unlink(missing_ok=True)
        scrape_once(); return
    if not lines:
        logger.info("No text lines – nothing to parse"); return

    new=[c for c in parse_comments(lines) if (c["store"],c["timestamp"],c["comment"]) not in read_seen()]
    if not new: logger.info("No new comments"); return

    logger.info(f"{len(new)} new comments → sending …")
    for c in new: post_chat(c); time.sleep(1)
    append_comments(new)
    logger.info("Done")

# ─────────────────────────────────────────────── CLI ENTRY ──
if __name__ == "__main__":
    if len(sys.argv)>1 and sys.argv[1]=="login":
        Path(AUTH_STATE_PATH).unlink(missing_ok=True)
    scrape_once()
