"""
Equitas Bank — Login Page Inspector
=====================================
1.  Fetch proxy + push to AdsPower profile
2.  Start browser (maximized)
3.  Navigate directly to Equitas login portal
4.  Enter username, password, solve captcha  (char by char + mouse moves)
5.  Click Accounts button, then Login button (natural mouse movement)
6.  Wait 2 minutes so you can press F12 and inspect

Run:
    python scripts/test_equitas_inspect.py

Optional:
    --profile <id>   AdsPower profile ID (default: first available)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import base64
import tempfile
import time
import random

from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from app.core.adspower import AdsPowerAPI
from app.core.browser import Browser
from app.core.proxy import ProxyManager
from app.core.settings import Settings
from app.core.twocaptcha import TwoCaptchaClient

LOGIN_URL    = "https://ib.equip.equitasbank.com/login"
USERNAME     = "7891242"
PASSWORD     = "Raam@2025"
INSPECT_WAIT = 120

GREEN = "\033[92m"
RED   = "\033[91m"
CYAN  = "\033[96m"
BOLD  = "\033[1m"
RESET = "\033[0m"

def ok(msg):      print(f"  {GREEN}✓{RESET}  {msg}")
def fail(msg):    print(f"  {RED}✗{RESET}  {msg}"); sys.exit(1)
def info(msg):    print(f"  {CYAN}·{RESET}  {msg}")
def step(n, msg): print(f"\n{BOLD}{CYAN}[{n}]{RESET} {BOLD}{msg}{RESET}")


def get_first_profile_id():
    api = AdsPowerAPI()
    profiles = api._get("/api/v1/user/list").get("list", [])
    if not profiles:
        fail("No AdsPower profiles found")
    return profiles[0]["user_id"]


def safe_get(driver, url, timeout=15):
    """Navigate with a short page load timeout — browser keeps loading on its own."""
    driver.set_page_load_timeout(timeout)
    try:
        driver.get(url)
    except Exception:
        pass
    finally:
        driver.set_page_load_timeout(30)


def wait_for_css(driver, selector, timeout=30, clickable=False):
    condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
    try:
        return WebDriverWait(driver, timeout).until(
            condition((By.CSS_SELECTOR, selector))
        )
    except Exception:
        return None


def wait_for_xpath(driver, xpath, timeout=30, clickable=False):
    condition = EC.element_to_be_clickable if clickable else EC.presence_of_element_located
    try:
        return WebDriverWait(driver, timeout).until(
            condition((By.XPATH, xpath))
        )
    except Exception:
        return None


def random_mouse_move(driver, moves=4):
    """Dispatch random mousemove events across the visible viewport."""
    try:
        width  = driver.execute_script("return window.innerWidth")
        height = driver.execute_script("return window.innerHeight")
        for _ in range(moves):
            x = random.randint(80, max(81, width - 80))
            y = random.randint(80, max(81, height - 80))
            driver.execute_script(
                "document.dispatchEvent(new MouseEvent('mousemove',"
                "{bubbles:true,clientX:arguments[0],clientY:arguments[1]}))",
                x, y
            )
            time.sleep(random.uniform(0.08, 0.25))
    except Exception:
        pass


def human_click(driver, el):
    """Scroll into view, move mouse over element, then JS-click (reliable on MUI)."""
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    time.sleep(random.uniform(0.1, 0.2))
    try:
        x_offset = random.randint(-4, 4)
        y_offset = random.randint(-2, 2)
        ActionChains(driver)\
            .move_to_element_with_offset(el, x_offset, y_offset)\
            .pause(random.uniform(0.1, 0.3))\
            .perform()
    except Exception:
        pass
    time.sleep(random.uniform(0.1, 0.2))
    driver.execute_script("arguments[0].click();", el)


def human_type(field, text):
    """Type text character by character with random human-like delays."""
    for char in text:
        field.send_keys(char)
        time.sleep(random.uniform(0.08, 0.18))


def solve_captcha_b64(b64_str, settings):
    """Decode base64 captcha, save to temp file, solve via 2Captcha."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    try:
        tmp.write(base64.b64decode(b64_str))
        tmp.flush()
        tmp.close()
        client = TwoCaptchaClient(settings.TWOCAPTCHA_API_KEY)
        result = client.solve_image(tmp.name, case_sensitive=True)
        return result["solution"]["text"].strip()
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def run(profile_id):
    settings = Settings()
    username = USERNAME
    password = PASSWORD

    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}{CYAN}  Equitas Bank — Login Page Inspector{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"  Profile  : {profile_id}")
    print(f"  Username : {username}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}\n")

    # ── Step 1: Proxy ─────────────────────────────────────────────────────────
    step(1, "Fetch proxy from Proxy302")
    proxy = ProxyManager().get_proxy()
    if not proxy:
        fail("ProxyManager returned no proxy")
    AdsPowerAPI().update_proxy(profile_id, proxy)
    ok(f"Proxy: {proxy['proxy_type']}://{proxy['proxy_host']}:{proxy['proxy_port']}")

    # ── Step 2: Browser ───────────────────────────────────────────────────────
    step(2, "Start AdsPower browser")
    browser_obj = Browser(AdsPowerAPI())
    browser_obj.open(profile_id)
    driver = browser_obj.driver
    time.sleep(2)
    driver.maximize_window()
    ok("Browser started")

    try:

        # ── Step 3: Navigate to login portal ──────────────────────────────────
        step(3, "Navigate to Equitas login portal")
        safe_get(driver, LOGIN_URL)
        time.sleep(15)
        random_mouse_move(driver, 3)
        try:
            info(f"URL: {driver.current_url}")
        except Exception:
            pass
        ok("Login portal loaded")

        # ── Step 4: Enter username ─────────────────────────────────────────────
        step(4, "Enter username")
        user_field = wait_for_css(driver, '#login_userId_new', timeout=30)
        if not user_field:
            fail("User ID field not found — page may not have loaded")
        random_mouse_move(driver, 2)
        human_click(driver, user_field)
        time.sleep(random.uniform(0.3, 0.7))
        human_type(user_field, username)
        time.sleep(1)
        ok(f"Username entered: {username}")

        # ── Step 5: Enter password ─────────────────────────────────────────────
        step(5, "Enter password")
        random_mouse_move(driver, 2)
        pw_field = wait_for_css(driver, '#login_password', timeout=10)
        if not pw_field:
            fail("Password field not found")
        human_click(driver, pw_field)
        time.sleep(random.uniform(0.3, 0.7))
        human_type(pw_field, password)
        time.sleep(1)
        ok("Password entered")

        # ── Step 6: Extract and solve captcha ─────────────────────────────────
        step(6, "Solve captcha")
        captcha_img = wait_for_css(driver, 'img[src^="data:image/png;base64"]', timeout=15)
        if not captcha_img:
            fail("Captcha image not found")
        src = captcha_img.get_attribute("src")
        captcha_b64 = src.split(",", 1)[1] if "," in src else src
        info("Captcha image extracted, sending to 2Captcha...")
        captcha_text = solve_captcha_b64(captcha_b64, settings)
        if not captcha_text:
            fail("Captcha could not be solved")
        ok(f"Captcha solved: {captcha_text}")

        # ── Step 7: Enter captcha text ────────────────────────────────────────
        step(7, "Enter captcha text")
        random_mouse_move(driver, 2)
        cap_field = wait_for_css(driver, '.captcha-page__text-field input', timeout=10)
        if not cap_field:
            cap_field = wait_for_css(driver, 'input[maxlength="5"]', timeout=5)
        if not cap_field:
            fail("Captcha input field not found")
        human_click(driver, cap_field)
        time.sleep(random.uniform(0.3, 0.6))
        human_type(cap_field, captcha_text)
        time.sleep(1)
        ok("Captcha text entered")

        # ── Step 8: Click Accounts button ─────────────────────────────────────
        step(8, "Click Accounts button")
        random_mouse_move(driver, 3)
        accounts_btn = wait_for_xpath(driver, '//button[normalize-space(.)="Accounts"]', timeout=10, clickable=True)
        if not accounts_btn:
            fail("Accounts button not found")
        human_click(driver, accounts_btn)
        time.sleep(1)
        ok("Accounts button clicked")

        # ── Step 9: Click Login button ────────────────────────────────────────
        step(9, "Click Login button")
        random_mouse_move(driver, 2)
        login_btn = wait_for_css(driver, 'button.login-page__login-button', timeout=10, clickable=True)
        if not login_btn:
            fail("Login button not found")
        human_click(driver, login_btn)
        time.sleep(2)
        ok("Login button clicked")

        # ── Step 10: Wait for inspection ──────────────────────────────────────
        step(10, f"Waiting {INSPECT_WAIT}s — press F12 to inspect")
        time.sleep(INSPECT_WAIT)

    except SystemExit:
        raise
    except Exception as e:
        import traceback
        print(f"\n  {RED}ERROR{RESET}  {e}")
        traceback.print_exc()
    finally:
        step("–", "Stopping browser")
        try:
            browser_obj.close()
        except Exception:
            pass
        ok("Browser stopped")

    print(f"\n{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}")
    print(f"{BOLD}  Done{RESET}")
    print(f"{BOLD}{CYAN}══════════════════════════════════════════════════{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Equitas Bank login page inspector.")
    parser.add_argument("--profile", default=None, help="AdsPower profile ID (default: first)")
    args = parser.parse_args()

    profile = args.profile or get_first_profile_id()
    run(profile_id=profile)
