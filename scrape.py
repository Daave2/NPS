#!/usr/bin/env python3
import os
import sys
import csv
import time
import logging
import re
import requests
import configparser
from getpass import getpass
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

############################################
# LOGGING SETUP
############################################
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("scrape.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

############################################
# CONFIG: ENV or config.ini
############################################
config = configparser.ConfigParser()
config.read("config.ini")

def get_opt(key: str) -> str:
    return os.environ.get(key) or config["DEFAULT"].get(key, "")

GOOGLE_EMAIL    = get_opt("GOOGLE_EMAIL")
GOOGLE_PASSWORD = get_opt("GOOGLE_PASSWORD")
MAIN_WEBHOOK    = get_opt("MAIN_WEBHOOK")
ALERT_WEBHOOK   = get_opt("ALERT_WEBHOOK")

if not GOOGLE_EMAIL or not GOOGLE_PASSWORD:
    logger.warning("Google credentials missing: GOOGLE_EMAIL/GOOGLE_PASSWORD.")
if not MAIN_WEBHOOK:
    logger.warning("MAIN_WEBHOOK missing.")
if not ALERT_WEBHOOK:
    logger.warning("ALERT_WEBHOOK missing.")

############################################
# CONSTANTS
############################################
AUTH_STATE_PATH   = "auth_state.json"
COMMENTS_LOG_PATH = "comments_log.csv"
LOOKER_STUDIO_URL = "https://lookerstudio.google.com/reporting/b69cfd73-8c0a-453d-9c10-6561fa953f7c/page/p_bghtutfsbd"

############################################
# ALERT WHEN LOGIN NEEDED
############################################
def alert_login_needed(reason="Unknown"):
    if not ALERT_WEBHOOK or "chat.googleapis.com" not in ALERT_WEBHOOK:
        logger.warning("No valid ALERT_WEBHOOK.")
        return
    payload = {"text": f"⚠️ New Google login required: {reason}"}
    try:
        r = requests.post(ALERT_WEBHOOK, json=payload)
        if r.status_code == 200:
            logger.info("Login alert sent.")
        else:
            logger.error(f"Alert failed ({r.status_code}): {r.text}")
    except Exception as e:
        logger.error(f"Alert exception: {e}")

############################################
# MANUAL LOGIN & SAVE STATE
############################################
def login_and_save_state(page):
    logger.info("Starting manual Google login...")
    page.goto("https://accounts.google.com/")
    try:
        page.wait_for_selector("input[type='email']", timeout=15000)
    except PlaywrightTimeoutError:
        logger.error("Email input not found.")
        return False
    page.fill("input[type='email']", GOOGLE_EMAIL or input("Email: "))
    page.press("input[type='email']", "Enter")
    try:
        page.wait_for_selector("input[type='password']", timeout=30000)
    except PlaywrightTimeoutError:
        logger.error("Password input not found.")
        return False
    page.fill("input[type='password']", GOOGLE_PASSWORD or getpass("Password: "))
    page.press("input[type='password']", "Enter")
    logger.info("Complete any 2FA in browser window.")
    try:
        page.wait_for_url("https://myaccount.google.com/?pli=1", timeout=120000)
        page.context.storage_state(path=AUTH_STATE_PATH)
        logger.info(f"Auth state saved to {AUTH_STATE_PATH}")
        return True
    except PlaywrightTimeoutError:
        logger.error("Login did not complete.")
        return False

############################################
# FETCH LOOKER STUDIO TEXT
############################################
def copy_page_text(page):
    logger.info("Loading Looker Studio report...")
    try:
        page.goto(LOOKER_STUDIO_URL, timeout=30000)
    except PlaywrightTimeoutError:
        logger.error("Cannot load report URL.")
        return []
    time.sleep(5)
    if "accounts.google.com" in page.url:
        logger.warning("Redirected to login; auth invalid.")
        return None
    time.sleep(10)
    try:
        return page.inner_text("body").splitlines()
    except Exception as e:
        logger.error(f"Copy failed: {e}")
        return []

############################################
# PARSE COMMENTS
############################################
def parse_comments_from_lines(lines):
    if not lines:
        return []
    comments, idx, n = [], 0, len(lines)
    store_re = re.compile(r"^\d+\s+")
    score_re = re.compile(r"^[0-9]{1,2}$")
    while idx < n:
        line = lines[idx].strip()
        if store_re.match(line):
            store = line
            idx += 1
            ts   = lines[idx].strip() if idx < n else ""
            idx += 1
            text_lines = []
            score = ""
            while idx < n and not score_re.match(lines[idx].strip()):
                text_lines.append(lines[idx].strip())
                idx += 1
            if idx < n:
                score = lines[idx].strip()
                idx += 1
            comments.append({
                "store": store,
                "timestamp": ts,
                "comment": "\n".join(text_lines).strip(),
                "score": score
            })
        else:
            idx += 1
    logger.info(f"Found {len(comments)} total comments.")
    return comments

############################################
# STATE: READ/WRITE CSV
############################################
def read_existing_comments():
    seen = set()
    if not os.path.exists(COMMENTS_LOG_PATH):
        return seen
    with open(COMMENTS_LOG_PATH, newline="", encoding="utf-8") as f:
        rdr = csv.DictReader(f, fieldnames=["store","timestamp","comment","score"])
        for r in rdr:
            seen.add((r["store"], r["timestamp"], r["comment"]))
    return seen

def append_new_comments(new_comments):
    with open(COMMENTS_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for c in new_comments:
            w.writerow([c["store"], c["timestamp"], c["comment"], c["score"]])

############################################
# POST TO GOOGLE CHAT
############################################
def send_to_google_chat(c):
    if not MAIN_WEBHOOK or "chat.googleapis.com" not in MAIN_WEBHOOK:
        logger.warning("No valid MAIN_WEBHOOK.")
        return
    try:
        score = int(c["score"] or 0)
    except ValueError:
        score = 0
    if score <= 4:
        emoji, label = "🔴", "Detractor"
    elif score <= 7:
        emoji, label = "🟠", "Passive"
    else:
        emoji, label = "🟢", "Promoter"
    header = f"{emoji} {c['store']} ({label})"
    payload = {
        "cards": [{
            "header": {"title":"New NPS Comment","subtitle":header,"imageStyle":"IMAGE"},
            "sections": [{"widgets":[
                {"keyValue":{"topLabel":"Timestamp","content":c["timestamp"]}},
                {"keyValue":{"topLabel":"Score","content":str(score)}},
                {"textParagraph":{"text":c["comment"].replace('\n','<br>')}}
            ]}]
        }]
    }
    try:
        r = requests.post(MAIN_WEBHOOK, json=payload)
        if r.status_code==200:
            logger.info(f"Posted: {c['timestamp']}")
        else:
            logger.error(f"Chat post failed ({r.status_code}): {r.text}")
    except Exception as e:
        logger.error(f"Post exception: {e}")

############################################
# MAIN SCRAPE LOGIC
############################################
def run_scrape():
    with sync_playwright() as p:
        # auth
        if not os.path.exists(AUTH_STATE_PATH):
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            if not login_and_save_state(page):
                alert_login_needed("Login failed")
                return
        else:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=AUTH_STATE_PATH)
            page = ctx.new_page()

        lines = copy_page_text(page)
        if lines is None:
            alert_login_needed("Auth expired")
            return
        if not lines:
            return

        comments = parse_comments_from_lines(lines)
        seen     = read_existing_comments()
        new      = [c for c in comments if (c["store"],c["timestamp"],c["comment"]) not in seen]

        if new:
            logger.info(f"{len(new)} new comments; posting…")
            for c in new:
                send_to_google_chat(c)
            append_new_comments(new)
        else:
            logger.info("No new comments.")

if __name__ == "__main__":
    # run exactly once and exit
    run_scrape()
