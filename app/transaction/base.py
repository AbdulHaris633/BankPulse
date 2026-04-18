import re
import os
import base64
import json
import time
import random
from datetime import datetime
from multiprocessing import Queue, Value
from urllib.parse import urlparse

import requests
from pyclick import HumanClicker
from selenium.webdriver.remote.webelement import WebElement

from app.core.adspower import AdsPowerAPI
from app.core.browser import Browser
from app.core.twocaptcha import TwoCaptchaClient
from app.core.anticaptcha import AntiCaptchaClient
from app.core.settings import Settings
from app.services.logger import debug, info, warn, error
from app.utils.functions import (
    get_p2p_proxy,
    proxy_hangup,
    get_random_file_name,
    upload_screenshot,
    mysql_exec,
    BENEFICIARY_EXISTS,
    ACCOUNT_BALANCE_TOO_LOW,
)

# Load settings once at module level
_settings = Settings()


class TransactionManager(Browser):
    """
    Base class for all bank-specific transaction bots.

    Extends Browser (which extends AdsPowerAPI) to add:
    - Transaction lifecycle: init(), run(), shutdown_handler()
    - Captcha solving: solve() — 2Captcha first, AntiCaptcha fallback
    - Bank operations (overridden in subclasses): login(), sync_transactions(),
      add_beneficiary(), payout(), quick_transfer()
    - Server reporting: report_status(), report_sync_status()
    - Database updates: update_browser_info(), update_last_failed_reason()
    - Profile management: update_profile() — replaces a blocked browser profile
    - OTP, screenshot, balance helpers

    Inheritance chain:
        KotakTransactionManager → TransactionManager → Browser → (AdsPowerAPI)

    V1 location: transaction_manager.py (all-in-one)
    V2 location: app/transaction/base.py (infrastructure split into Browser,
                 AdsPowerAPI, TwoCaptchaClient, AntiCaptchaClient)

    Key changes from V1:
    - driver_manager parameter REMOVED — AdsPowerAPI created internally
    - get_ads_power_driver() REMOVED — logic is now in Browser.open()
    - clean_windows() REMOVED — moved to Browser
    - click_displayed() REMOVED — moved to Browser
    - small_sleep() REMOVED — replaced with self.random_sleep(1.5, 4)
    - solve() updated: twocaptcha result['code'] → result['solution']['text'] (new API)
    - close() calls super().close() (Browser) instead of close_adspower_driver()
    - update_profile() uses self.api methods instead of raw requests
    - get_serial_number() delegates to self.api.get_serial_number()
    - update_fingerprint() delegates to self.api.update_fingerprint()
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # Create the AdsPowerAPI client internally — V1 received driver_manager
        # from outside; V2 each TransactionManager owns its own API instance.
        api = AdsPowerAPI()

        # Initialise Browser base class with the API client
        super().__init__(api)

        # --- Command fields ---
        self.command: dict = command
        self.update_flag: Value = update_flag
        self.instruction_id: int = int(command["instruction_id"])
        self.operation: str = command["operation"]
        self.trader_id: int = int(command["trader_id"])
        self.bot_id: str = command["bot_id"]
        self.device_id: str = command["device_id"]
        self.login_details: dict = command["login_details"]
        self.username: str = self.login_details["username"]
        self.password: str = self.login_details["password"]

        # --- Runtime state ---
        self.proxy: dict = {}
        self.account_no: int = 0
        # data dict is sent to the command server in every report call
        self.data = {
            "instruction_id": command["instruction_id"],
            "query": self.operation,
            "action": "login",
            "success": 0,
            "trader_id": command["trader_id"],
            "device_id": command["device_id"],
            "bot_id": command["bot_id"],
            "screenshot_url": "",
            "account_number": "",
        }

        # --- Bank-specific settings (overridden in subclasses) ---
        # clear_cache: close browser with cache cleared before reopen — most banks True
        self.clear_cache: bool = True
        # dynamic_fingerprint: regenerate browser fingerprint before each session
        self.dynamic_fingerprint: bool = False
        self.institution_name: str = ""
        # url: the bank portal URL, used by update_profile() for domain detection
        self.url: str = ""
        self.serial_no: int = 0
        # interface_type: "web" or "mobile" — stored in DB for monitoring
        self.interface_type: str = ""

        # --- Flow tracking ---
        self.last_failed_reason: str = ""
        # status: multiprocessing Queue to signal "alive" to the parent process
        # so the 15-min watchdog in main.py knows the child is still running
        self.status: Queue = child_status
        self.update_started: bool = False
        self.init_has_run: bool = False
        # reached_tip: True when sync has caught up to the latest transaction
        self.reached_tip: bool = False
        self.sync_mode: dict = {}
        # do_full_sync: True when server sends latest_tx_hash=0x00 (full re-sync)
        self.do_full_sync: bool = False

        # --- Captcha solvers ---
        # 2Captcha is tried first; AntiCaptcha is the fallback
        # Pass API key from settings — TwoCaptchaClient requires it at init
        self.twocaptcha = TwoCaptchaClient(api_key=_settings.TWOCAPTCHA_API_KEY)
        self.anticaptcha = AntiCaptchaClient(api_key=_settings.ANTICAPTCHA_API_KEY)

    # ─── INIT ────────────────────────────────────────────────────────────────

    def init(self) -> bool:
        """
        Set up everything needed before the bank login can begin:
        1. Signal alive to parent watchdog
        2. Optionally update browser fingerprint (if dynamic_fingerprint=True)
        3. Fetch a working P2P proxy
        4. Push the proxy config into the AdsPower profile
        5. Open the browser (Browser.open()) and clean extra tabs

        Returns True on success, False on any failure.
        Called at the start of every operation in run().

        V1 location: TransactionManager.init()
        V2 change: get_ads_power_driver() replaced with self.open()
                   update_adspower_proxy() replaced with self.api.update_proxy()
        """
        try:
            self.info("it")
            self.update()

            # Detect full-sync requirement — server sends latest_tx_hash=0x00
            # when it wants the bot to re-sync all transactions from scratch
            if self.operation == "sync":
                if self.command.get("latest_tx_hash"):
                    if re.search(r'0x00', self.command.get("latest_tx_hash")):
                        self.do_full_sync = True

            # Optional: randomise browser fingerprint before opening
            # (Chrome version, UA string) — prevents fingerprint-based detection
            if self.dynamic_fingerprint:
                if not self.api.update_fingerprint(self.bot_id):
                    raise Exception("Could not update browser fingerprint")

            # Fetch a rotating P2P proxy for this transaction
            self.update()
            self.debug("Fetching proxy")
            self.proxy = get_p2p_proxy(self.command)
            if not self.proxy:
                raise Exception("Could not fetch proxy")
            self.debug("Proxy fetched: " + str(self.proxy))
            self.data.update({"proxy": self.proxy})

            # Push the proxy into the AdsPower profile so the browser uses it
            self.debug("Updating proxy in AdsPower profile")
            if not self.api.update_proxy(self.bot_id, self.proxy):
                raise Exception("Could not update proxy")
            self.debug("Proxy update complete")

            # Open the browser — Browser.open() handles chromedriver detection,
            # CDP attachment, and window maximisation
            self.debug("Opening browser")
            self.open(self.bot_id, clear_cache=self.clear_cache)
            self.debug("Browser opened")

            self.update()
            # Close any extra tabs (popups) that may have opened
            self.clean_windows()

            self.info("Init success")
            return True
        except Exception as e:
            self.error("Could not initialize bot")
            self.error(e)
            return False

    # ─── BANK OPERATION STUBS ────────────────────────────────────────────────
    # These are overridden in each bank's TransactionManager subclass.
    # Returning True here means the base class is safe to call without subclass.

    def login(self) -> bool:
        return True

    def sync_transactions(self) -> bool:
        return True

    def scrape_balance(self) -> None:
        return

    def add_beneficiary(self) -> bool:
        return True

    def payout(self) -> bool:
        return True

    def quick_transfer(self) -> bool:
        return True

    def close_dialogs(self) -> bool:
        return True

    def logout(self) -> bool:
        return True

    # ─── RUN ─────────────────────────────────────────────────────────────────

    def run(self) -> bool:
        """
        Main entry point — dispatches to the correct operation based on
        self.operation. Called from main.py in the child process.

        Operations:
        - login_check   : login only, no transaction
        - add_beneficiary: login + add_beneficiary()
        - payout        : login + payout()
        - quick_transfer_sbi: login + quick_transfer()
        - sync          : login + sync_transactions() with status reporting

        All operations call shutdown_handler() at the end (success or failure).

        V1 location: TransactionManager.run()
        """
        try:
            # LOGIN CHECK
            if self.operation == "login_check":
                self.update()
                self.info("Starting login check")
                self.data["query"] = "login_check"
                self.data["action"] = "login"
                if not self.init():
                    raise Exception("Init failed")
                self.init_has_run = True
                self.update()
                self.update_browser_info()
                if not self.login():
                    raise Exception("Run failed, login check")
                self.update()
                self.update_browser_info()
                self.data["account_number"] = str(self.account_no)
                self.info("Login successful")
                self.logout()

            # ADD BENEFICIARY
            elif self.operation == "add_beneficiary":
                self.update()
                self.info("Starting add beneficiary")
                self.data["query"] = "add_beneficiary"
                self.data["action"] = "login"
                self.data.update({"new": 1})
                self.update()
                if not self.init():
                    raise Exception("Init failed")
                self.init_has_run = True
                self.update_browser_info()
                self.update()
                try:
                    if not self.login():
                        raise Exception("Run failed, login check")
                    self.update()
                    self.update_browser_info()
                    self.data["action"] = "add_beneficiary"
                    code = self.add_beneficiary()
                    self.update()
                    if not code:
                        raise Exception("Could not add beneficiary")
                    # BENEFICIARY_EXISTS=2 means it was already added — still success
                    if code == BENEFICIARY_EXISTS:
                        self.data["new"] = 0
                    self.info("Beneficiary successfully added")
                except Exception as e:
                    self.error(e)
                finally:
                    self.update_browser_info()
                    self.screenshot()
                    self.close_dialogs()
                    self.scrape_balance()
                    raise Exception("Could not add beneficiary, exiting")

            # PAYOUT
            elif self.operation == "payout":
                self.update()
                self.info("Starting payout")
                self.data["query"] = "payout"
                self.data["action"] = "login"
                self.data.update({"new": 1})
                if not self.init():
                    raise Exception("Could not initialize transaction manager")
                self.init_has_run = True
                self.update()
                try:
                    self.update_browser_info()
                    self.update()
                    if not self.login():
                        raise Exception("Could not login to transaction manager")
                    self.update_browser_info()
                    self.data["action"] = "payout"
                    code = self.payout()
                    self.update()
                    if not code:
                        raise Exception("Could not complete payout")
                    # ACCOUNT_BALANCE_TOO_LOW=3 — payout ran but balance insufficient
                    elif code == ACCOUNT_BALANCE_TOO_LOW:
                        self.data["new"] = 0
                    self.info("Finished payout")
                except Exception as e:
                    self.error(e)
                finally:
                    self.update_browser_info()
                    self.screenshot()
                    self.close_dialogs()
                    self.scrape_balance()
                    raise Exception("Could not complete payout, exiting")

            # QUICK TRANSFER (SBI-specific payout variant)
            elif self.operation == "quick_transfer_sbi":
                self.update()
                self.info("Starting quick transfer")
                self.data["query"] = "quick_transfer_sbi"
                self.data["action"] = "login"
                self.data.update({"new": 1})
                if not self.init():
                    raise Exception("Could not initialize transaction manager")
                self.init_has_run = True
                self.update()
                try:
                    self.update_browser_info()
                    self.update()
                    if not self.login():
                        raise Exception("Could not login to transaction manager")
                    self.update_browser_info()
                    self.data["action"] = "quick_transfer_sbi"
                    code = self.quick_transfer()
                    self.update()
                    if not code:
                        raise Exception("Could not complete quick transfer")
                    elif code == ACCOUNT_BALANCE_TOO_LOW:
                        self.data["new"] = 0
                    self.info("Finished quick transfer")
                except Exception as e:
                    self.error(e)
                finally:
                    self.update_browser_info()
                    self.screenshot()
                    self.close_dialogs()
                    self.scrape_balance()
                    raise Exception("Could not complete quick transfer, exiting")

            # SYNC
            elif self.operation == "sync":
                self.info("Starting sync")
                self.update()
                self.data["query"] = "sync"
                self.data["action"] = "login"
                if not self.init():
                    raise Exception("Could not initialize transaction manager")
                self.init_has_run = True
                self.update()
                self.update_browser_info()
                if not self.login():
                    raise Exception("Run failed, login check")
                self.update()
                self.update_browser_info()
                self.debug("Reporting login status success")
                self.report_status(True)
                self.data["action"] = "sync"
                self.debug("Starting sync")
                if not self.sync_transactions():
                    self.report_failed_sync()
                    raise Exception("Run failed, sync transactions")
                self.update_browser_info()

            else:
                raise Exception("Command not recognized: " + self.operation)

            self.shutdown_handler(True)
        except Exception as e:
            self.error(e)
            self.shutdown_handler(False)
            return False

    # ─── CLOSE ───────────────────────────────────────────────────────────────

    def close(self) -> bool:
        """
        Release the proxy and close the browser.
        Calls Browser.close() (via super()) to quit Selenium and stop AdsPower.

        V1: called close_adspower_driver() directly
        V2: delegates browser cleanup to Browser.close() via super()
        """
        try:
            self.debug("Closing transaction manager: " + self.bot_id)
            # Release the P2P proxy back to the pool
            if self.proxy:
                if not proxy_hangup(self.proxy):
                    self.warn("Error with proxy hangup: " + self.bot_id)
            # Browser.close() quits Selenium and stops the AdsPower process
            if not super().close():
                raise Exception("Error closing browser: " + self.bot_id)
            self.debug("Finished closing transaction manager: " + self.bot_id)
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── SHUTDOWN ────────────────────────────────────────────────────────────

    def shutdown_handler(self, status: bool) -> None:
        """
        Final cleanup called at the end of run() — success or failure.
        - On failure: takes screenshot before closing
        - Always: releases proxy, reports status to server, closes browser

        V1 location: TransactionManager.shutdown_handler()
        """
        try:
            self.debug("Starting shutdown flow")
            self.data["account_number"] = str(self.account_no)

            # Take screenshot on failure — helps diagnose what went wrong
            if not status and self.driver:
                self.debug("Error condition, taking screenshot")
                try:
                    self.screenshot()
                    self.debug("Screenshot URL: " + self.data["screenshot_url"])
                except Exception as e:
                    self.warn("Error taking screenshot")
                    self.warn(e)

            # Release proxy
            if self.proxy:
                try:
                    self.debug("Hanging up proxy")
                    proxy_hangup(self.proxy)
                except Exception as e:
                    self.warn("Exception thrown during proxy hangup")
                    self.warn(e)

            # Report final status to command server
            try:
                self.debug("Reporting parent status in shutdown flow")
                if not status and self.init_has_run:
                    # On failure after init, report as "logout" so server knows
                    # the bot reached the bank portal but failed mid-operation
                    self.data["action"] = "logout"
                self.report_status(status)
            except Exception as e:
                self.warn("Exception thrown in report parent status")
                self.warn(e)

            # Close the browser process
            if self.driver:
                try:
                    self.debug("Closing driver")
                    super().close()
                except Exception as e:
                    self.error("Error closing driver")
                    self.error(e)

            time.sleep(3)
            self.debug("Ending shutdown flow")
        except Exception as e:
            self.error("Error in shutdown handler")
            self.error(e)

    # ─── CAPTCHA ─────────────────────────────────────────────────────────────

    def solve(self, captcha_b64: str, is_int: bool = False) -> str:
        """
        Decode a base64-encoded captcha image and solve it.

        Flow:
        1. Decode base64 → save as .png temp file
        2. Try 2Captcha (TwoCaptchaClient.solve_image()) → returns dict
           Extract text via result['solution']['text']  (new 2Captcha API)
        3. If 2Captcha fails → fallback to AntiCaptcha (AntiCaptchaClient.solve_image())
           AntiCaptcha returns text directly (not a dict)
        4. Clean up temp file
        5. Return solved text, or "" if both services failed

        is_int — when True, validates that solved text is a numeric string
                 (used by banks that have numeric-only captchas)

        V1 location: TransactionManager.solve()
        V2 change: both solvers now return full dict — extract result['solution']['text']
        """
        try:
            self.debug("Solving captcha")
            self.update()

            # Save the base64 captcha image to a temp file for the solver API
            filename = get_random_file_name(".png")
            captcha_bytes = base64.b64decode(captcha_b64)
            with open(filename, "wb") as f:
                f.write(captcha_bytes)

            captcha_solved = False
            captcha_text = ""

            # ── ATTEMPT 1: 2Captcha ──────────────────────────────────────────
            self.debug("Sending captcha to 2Captcha")
            for _ in range(1):
                self.update()
                try:
                    # case_sensitive=True matches V1's regsense=1 — bank CAPTCHAs are case sensitive
                    result = self.twocaptcha.solve_image(filename, case_sensitive=True)
                    # New 2Captcha API: solved text is at result['solution']['text']
                    captcha_text = result.get("solution", {}).get("text", "")
                    if not captcha_text.strip():
                        self.warn("Empty captcha response from 2Captcha")
                        self.random_sleep(1.5, 4)
                        continue
                    if is_int:
                        try:
                            int(captcha_text)
                        except Exception:
                            self.warn("2Captcha result is not int: " + str(captcha_text))
                            self.random_sleep(1.5, 4)
                            continue
                    self.debug("2Captcha solved: " + str(captcha_text))
                    captcha_solved = True
                    break
                except Exception as ex:
                    self.warn("Error solving captcha with 2Captcha")
                    self.warn(ex)
                    self.random_sleep(1.5, 4)
                    continue

            # ── ATTEMPT 2: AntiCaptcha fallback ──────────────────────────────
            if not captcha_solved:
                self.warn("2Captcha failed — trying AntiCaptcha")
                for _ in range(1):
                    self.update()
                    try:
                        result = self.anticaptcha.solve_image(filename)
                        captcha_text = result.get("solution", {}).get("text", "")
                        if not captcha_text or not captcha_text.strip():
                            self.warn("Empty captcha response from AntiCaptcha")
                            self.random_sleep(1.5, 4)
                            continue
                        if is_int:
                            try:
                                int(captcha_text)
                            except Exception:
                                self.warn("AntiCaptcha result is not int: " + str(captcha_text))
                                self.random_sleep(1.5, 4)
                                continue
                        self.debug("AntiCaptcha solved: " + str(captcha_text))
                        captcha_solved = True
                        break
                    except Exception as ex:
                        self.warn("Error solving captcha with AntiCaptcha")
                        self.warn(ex)
                        self.random_sleep(1.5, 4)
                        continue

            if not captcha_solved:
                self.error("Unable to solve captcha with either service")

            # Remove temp file regardless of outcome
            try:
                os.remove(filename)
            except Exception as ex:
                self.warn(ex)

            return captcha_text
        except Exception as e:
            self.error(e)
            return ""

    # ─── SERVER REPORTING ────────────────────────────────────────────────────

    def report_status(self, success: bool) -> bool:
        """
        POST the current self.data dict to the parent report endpoint.
        Used for login_check, add_beneficiary, payout, quick_transfer results.

        Endpoint: _settings.PARENT_REPORT_URL
        (differs between payout and standard operations — see settings.py)

        V1 location: TransactionManager.report_status()
        """
        try:
            self.debug("Reporting command status")
            self.data["success"] = int(success)
            self.debug("Report data: " + str(self.data))
            resp = requests.post(
                url=_settings.PARENT_REPORT_URL,
                json=self.data,
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            if not resp.ok:
                raise Exception(
                    "Could not report command status: {}:{}".format(resp.status_code, resp.text)
                )
            self.debug("AFTER REPORT PARENT STATUS: ")
            self.debug("URL: " + resp.request.url)
            self.debug("DATA: " + str(resp.request.body))
            self.debug("HTTP STATUS/TEXT: " + str(resp.status_code) + "/" + resp.text)
            self.debug("Finished report command status")
            return True
        except Exception as e:
            self.error(e)
            return False

    def report_sync_status(self, success: bool) -> bool:
        """
        POST the current self.data dict to the sync report endpoint.
        Used for individual transaction sync results within sync operations.

        Endpoint: _settings.SYNC_REPORT_URL

        V1 location: TransactionManager.report_sync_status()
        """
        try:
            self.debug("Reporting sync status")
            self.data["success"] = int(success)
            resp = requests.post(
                url=_settings.SYNC_REPORT_URL,
                json=self.data,
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            if not resp.ok:
                raise Exception(
                    "Could not report sync status: {}:{}".format(resp.status_code, resp.text)
                )
            self.debug("Sync report: " + str(resp.text))
            return True
        except Exception as e:
            self.error(e)
            return False

    def report_no_transactions_found(self) -> bool:
        """
        Called when sync has found no new transactions to report.
        Fetches the current sync mode and reports "reached_tip" so the server
        knows the bot has caught up to the latest transaction.

        V1 location: TransactionManager.report_no_transactions_found()
        """
        try:
            self.debug("Reporting no new transactions found")
            self.update()
            if not self.fetch_sync_mode():
                raise Exception("Could not fetch sync mode")
            self.reached_tip = True
            previous_action = self.data["action"]
            latest_tx_hash = self.sync_mode["command"]["latest_tx_hash"]
            self.data["query"] = "sync"
            self.data["action"] = "reached_tip"
            self.data.update({"latest_tx_hash": latest_tx_hash})
            if not self.report_sync_status(True):
                self.warn("API error reporting no new transactions")
            self.reached_tip = False
            self.data["action"] = previous_action
            return True
        except Exception as e:
            self.error(e)
            return False

    def fetch_sync_mode(self) -> bool:
        """
        GET the current sync mode from the server for this instruction.
        Populates self.sync_mode with the server response dict.
        Used by report_no_transactions_found() to get the latest_tx_hash.

        Endpoint: _settings.SYNC_MODE_URL

        V1 location: TransactionManager.fetch_sync_mode()
        """
        params = {"instruction_id": self.instruction_id}
        self.debug("Fetching sync mode")
        try:
            resp = requests.get(
                url=_settings.SYNC_MODE_URL,
                params=params,
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            if not resp.ok:
                raise Exception(
                    "Could not fetch sync mode: {}:{}".format(resp.status_code, resp.text)
                )
            if resp.text.strip() == "UNAUTHORIZED":
                raise Exception("REQUEST UNAUTHORIZED")
            self.sync_mode = resp.json()
            return True
        except Exception as e:
            self.error(e)
            return False

    def report_failed_sync(self) -> bool:
        """
        Called when sync_transactions() fails — reports a "sleep" action so
        the server knows the sync loop stopped unexpectedly.

        V1 location: TransactionManager.report_failed_sync()
        """
        try:
            self.data["query"] = "sync"
            self.data["action"] = "sleep"
            self.debug("Report sync status in shutdown flow")
            self.report_sync_status(False)
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── BALANCE ─────────────────────────────────────────────────────────────

    def update_balance(self, balance: str) -> bool:
        """
        POST the current account balance to the sync balance endpoint.
        Called after login or sync to keep the server's balance figure up to date.

        Endpoint: _settings.SYNC_BALANCE_URL

        V1 location: TransactionManager.update_balance()
        """
        try:
            self.debug("Updating account balance")
            payload = {
                "total_available_balance": balance,
                "success": 1,
                "action": "sync_balance",
                "instruction_id": self.instruction_id,
            }
            resp = requests.post(
                url=_settings.SYNC_BALANCE_URL,
                json=payload,
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            if not resp.ok or resp.json().get("response") != "Ok":
                raise Exception(
                    "Error uploading balance: {}:{}".format(resp.status_code, resp.text)
                )
            self.debug("Account balance updated")
            return True
        except Exception as e:
            self.error(e)
            return False

    def update_lien(self, balance: str) -> bool:
        """
        POST the current lien amount to the sync balance endpoint.
        Some banks (e.g. Federal) report both available balance and lien separately.

        Endpoint: _settings.SYNC_BALANCE_URL

        V1 location: TransactionManager.update_lien()
        """
        try:
            self.debug("Updating account lien")
            payload = {
                "total_lien_amount": balance,
                "success": 1,
                "action": "sync_lien",
                "instruction_id": self.instruction_id,
            }
            resp = requests.post(
                url=_settings.SYNC_BALANCE_URL,
                json=payload,
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            if not resp.ok or resp.json().get("response") != "Ok":
                raise Exception(
                    "Error uploading lien: {}:{}".format(resp.status_code, resp.text)
                )
            self.debug("Account lien updated")
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── SCREENSHOT ──────────────────────────────────────────────────────────

    def screenshot(self) -> bool:
        """
        Capture the current browser screen as base64 and upload it to the
        screenshot server. Stores the returned URL in self.data['screenshot_url']
        so it gets included in the next report_status() call.

        Uses driver.get_screenshot_as_base64() (in-memory, no file save) then
        calls upload_screenshot() from utils/functions.py to POST to server.

        V1 location: TransactionManager.screenshot()
        """
        try:
            self.debug("Taking screenshot")
            if not self.driver:
                self.warn("Driver not initialized yet, no screenshot to take")
                return True
            b64_img = self.driver.get_screenshot_as_base64()
            screenshot_url = upload_screenshot(b64_img, self.instruction_id, self.operation)
            self.data["screenshot_url"] = screenshot_url
            if not screenshot_url:
                raise Exception("Cannot take screenshot")
            self.debug("Finished taking screenshot")
            return True
        except Exception as e:
            self.error("Error taking screenshot")
            self.error(e)
            return False

    # ─── DATABASE ────────────────────────────────────────────────────────────

    def update_browser_info(self) -> None:
        """
        UPSERT a row in the `browser_info` MySQL table with the current bot
        state. Called after login and at key points in run().

        Columns stored: trader_id, device_id, profile_id, username, institution,
        interface_type, last_command (base64 JSON), server_id, browser_id
        (AdsPower serial number), last_run_time.

        V1 location: TransactionManager.update_browser_info()
        V2 change: get_serial_number() now delegates to self.api.get_serial_number()
        """
        try:
            self.update()
            self.debug("Updating browser info")
            now = datetime.now()
            last_run_time = now.strftime("%Y/%m/%d %H:%M:%S")

            # AdsPower serial number — unique numeric ID for the profile row
            browser_id = self.get_serial_number()
            if not browser_id:
                raise Exception("Could not fetch serial number, exiting browser update")

            sql_query = (
                "REPLACE INTO browser_info "
                "(trader_id, device_id, profile_id, username, institution, "
                "interface_type, last_command, server_id, browser_id, last_run_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            )
            params = (
                self.trader_id,
                self.device_id,
                self.bot_id,
                self.username,
                self.institution_name,
                self.interface_type,
                base64.b64encode(json.dumps(self.command).encode()).decode(),
                _settings.SERVER_ID,
                browser_id,
                last_run_time,
            )
            ret = mysql_exec(sql_query, params)
            if not ret:
                raise Exception("Could not update database record for browser status")
            self.debug("Finished updating browser info")
        except Exception as e:
            self.error(e)

    def update_last_failed_reason(self) -> None:
        """
        Update the last_failed_reason, last_failed_time, and last_failed_screenshot
        columns in `browser_info` for this trader.

        self.last_failed_reason should be set by the bank subclass before calling
        this method.

        V1 location: TransactionManager.update_last_failed_reason()
        """
        try:
            self.debug("Updating last failed reason")
            now = datetime.now()
            last_failed_time = now.strftime("%Y/%m/%d %H:%M:%S")
            sql_query = """UPDATE browser_info
                           SET last_failed_reason=%s, last_failed_time=%s, last_failed_screenshot=%s
                           WHERE trader_id=%s"""
            params = (
                self.last_failed_reason,
                last_failed_time,
                self.data["screenshot_url"],
                self.trader_id,
            )
            if not mysql_exec(sql_query, params):
                raise Exception("Could not update database last failed reason")
            self.debug("Finished updating last failed reason")
        except Exception as e:
            self.error(e)

    # ─── ADSPOWER HELPERS ────────────────────────────────────────────────────

    def get_serial_number(self) -> int:
        """
        Return the AdsPower serial number (numeric display ID) for this profile.
        Cached in self.serial_no after first fetch.

        V1: made its own GET request to /api/v1/user/list
        V2: delegates to self.api.get_serial_number() in AdsPowerAPI

        V1 location: TransactionManager.get_serial_number()
        """
        try:
            self.debug("Fetching serial number")
            if self.serial_no:
                self.debug("Serial number exists: " + str(self.serial_no))
                return self.serial_no
            serial = self.api.get_serial_number(self.bot_id)
            if serial:
                self.serial_no = serial
                # Store on command dict so it flows through to browser_info logging
                self.command.update({"serial_no": self.serial_no})
                self.debug("Serial number fetched: " + str(self.serial_no))
            return self.serial_no
        except Exception as e:
            self.error(e)
            return 0

    def update_fingerprint(self) -> bool:
        """
        Randomise the browser fingerprint (Chrome version, UA string) for this
        profile via the AdsPower API.

        Called from init() when self.dynamic_fingerprint=True.
        Delegates to self.api.update_fingerprint() in AdsPowerAPI.

        V1 location: TransactionManager.update_fingerprint() (inline requests call)
        V2 change: delegates to AdsPowerAPI.update_fingerprint()
        """
        return self.api.update_fingerprint(self.bot_id)

    def update_profile(self) -> bool:
        """
        Replace this bot's AdsPower profile with a brand-new one.
        Called when a bank portal detects and blocks the current browser profile.

        Steps:
        1. Close the current browser session
        2. Delete the old AdsPower profile via API
        3. Create a new profile with the same name convention
        4. POST the new profile ID to the command server
        5. Fetch and apply a fresh proxy to the new profile
        6. Open the new browser

        V1 location: TransactionManager.update_profile()
        V2 change: uses self.api.delete_profile(), self.api.create_profile(),
                   self.open() instead of raw requests + get_ads_power_driver()
        """
        try:
            self.debug("Switching browser profile")
            self.update()
            if not self.close():
                self.warn("Error shutting down transaction manager in update profile")

            # Delete the blocked profile
            self.update()
            self.debug("Deleting old profile")
            self.random_sleep(5, 8)  # medium_sleep equivalent
            try:
                if not self.api.delete_profile(self.bot_id):
                    self.warn("Error deleting old profile in update_profile")
            except Exception as e:
                self.warn("Error deleting profile in update profile")
                self.warn(e)

            # Create a new profile with the same naming convention
            self.debug("Creating new profile")
            self.random_sleep(5, 8)
            profile_name = "{}_{}".format(self.trader_id, self.institution_name)

            # Extract domain from bank URL for the new profile's home page setting
            try:
                domain = urlparse(self.url).netloc.strip()
                if not domain:
                    domain = "www.google.com"
            except Exception as e:
                self.warn("Cannot parse domain from url: " + self.url)
                self.warn(e)
                domain = "www.google.com"

            profile_json = {
                "name": profile_name,
                "domain_name": domain,
                "group_id": "0",
                "fingerprint_config": {
                    "random_ua": {
                        "ua_browser": ["chrome"],
                        "ua_version": ["105"],
                        "ua_system_version": ["Windows 10"],
                    },
                    "browser_kernel_config": {"version": "latest", "type": "chrome"},
                },
                # Placeholder proxy — will be replaced by get_p2p_proxy() below
                "user_proxy_config": {
                    "proxy_soft": "other",
                    "proxy_type": "socks5",
                    "proxy_host": "0.0.0.0",
                    "proxy_port": "123",
                    "proxy_user": "abc",
                    "proxy_password": "xyz",
                },
            }

            new_bot_id = self.api.create_profile(profile_json)
            if not new_bot_id:
                raise Exception("Could not create new browser profile")
            self.debug("New profile ID: " + new_bot_id)

            # Report the new profile ID to the command server so it updates
            # the trader record and sends future commands to the new profile
            self.debug("Sending new browser profile ID to server")
            data = {
                "trader_id": self.trader_id,
                "old_bot_id": self.bot_id,
                "new_bot_id": new_bot_id,
                "success": 1,
            }
            self.bot_id = new_bot_id
            resp = requests.post(
                url=_settings.UPDATE_PROFILE_URL,
                json=data,
                timeout=_settings.REQUESTS_TIMEOUT,
            )
            if not resp.ok or resp.json().get("response") == "NotOk":
                raise Exception(
                    "API error when updating profile ID: " + str(resp.text)
                )

            # Fetch and apply a fresh proxy to the new profile
            self.debug("Fetching proxy for new profile")
            self.proxy = get_p2p_proxy(self.command)
            if not self.proxy:
                raise Exception("Could not fetch proxy")
            self.debug("Proxy fetched: " + str(self.proxy))
            if not self.api.update_proxy(self.bot_id, self.proxy):
                raise Exception("Could not update proxy on new profile")

            # Open the new browser session
            self.open(self.bot_id, clear_cache=self.clear_cache)

            self.info("Created new browser profile: " + self.bot_id)
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── HUMAN-LIKE INTERACTION ───────────────────────────────────────────────

    def human_type(self, text: str, element: WebElement) -> None:
        """
        Type text into an input field one character at a time with random
        delays (0.2–0.7s) to simulate human keystroke timing.
        Used for password fields and other sensitive inputs where uniform typing
        speed might trigger bot detection.

        V1 location: TransactionManager.human_type()
        """
        for c in text:
            time.sleep(random.uniform(0.2, 0.7))
            element.send_keys(c)

    def act_human(self) -> None:
        """
        Make random mouse movements to simulate human behaviour.
        Calls maximize_window() then random_movements().

        V1 location: TransactionManager.act_human()
        """
        self.maximize_window()
        self.random_movements()

    def maximize_window(self) -> bool:
        """
        Maximize the browser window with a small pause after.
        Safe to call even if driver is not connected — swallows exceptions.

        V1 location: TransactionManager.maximize_window()
        """
        try:
            try:
                self.driver.maximize_window()
            except Exception:
                pass
            self.random_sleep(1.5, 4)
            return True
        except Exception as e:
            error("Error maximizing window")
            error(e)
            return False

    def random_movements(self) -> None:
        """
        Move the mouse cursor to random positions using HumanClicker.
        Two random movements to two different screen positions with random
        duration each — makes automation less predictable to bot detectors.

        V1 location: TransactionManager.random_movements()
        """
        try:
            hc = HumanClicker()
            to_point = (random.randint(600, 800), random.randint(400, 500))
            duration = random.randint(1, 2)
            hc.move(toPoint=to_point, duration=duration)
            self.random_sleep(1.5, 4)
            to_point = (random.randint(600, 800), random.randint(400, 500))
            duration = random.randint(1, 2)
            hc.move(toPoint=to_point, duration=duration)
        except Exception as e:
            self.warn(e)
            self.warn("Error during random mouse movements")

    # ─── DOWNLOAD HELPER ─────────────────────────────────────────────────────

    def get_most_recent_download(self) -> str:
        """
        Read the Chrome downloads page to get the filename of the most recently
        downloaded file. Used by banks that download transaction statements
        (e.g. PDF or CSV exports) to verify the download completed.

        Opens chrome://downloads in a new tab, navigates the Shadow DOM to read
        the file-link element text, then closes the tab.

        V1 location: TransactionManager.get_most_recent_download()
        """
        from selenium.webdriver.common.by import By

        current_window = self.driver.current_window_handle
        try:
            self.debug("Getting most recent download")
            self.driver.switch_to.new_window()
            self.driver.get("chrome://downloads")
            shadow_host = self.driver.find_element(By.CSS_SELECTOR, "downloads-manager")
            script = "return arguments[0].shadowRoot"
            shadow_root = self.driver.execute_script(script, shadow_host)
            downloads_item = shadow_root.find_element(By.CSS_SELECTOR, "downloads-item")
            shadow_root = self.driver.execute_script(script, downloads_item)
            title = shadow_root.find_element(By.ID, "file-link").text.strip()
            self.debug("Most recent download: " + str(title))
            self.driver.close()
            self.driver.switch_to.window(current_window)
            return title
        except Exception as e:
            self.error(e)
            self.error("Cannot fetch recent download")
            self.driver.close()
            self.driver.switch_to.window(current_window)
            return ""

    # ─── INTERDAY STATEMENT ──────────────────────────────────────────────────

    def is_interday_statement_time(self) -> bool:
        """
        Return True if the current time is within the interday statement window
        (midnight to 00:30). Some banks switch to a different statement format
        at midnight — this check lets sync decide which scraper to use.

        V1 location: TransactionManager.is_interday_statement_time()
        """
        try:
            self.debug("Checking interday statement time")
            now = datetime.now()
            if now.hour == 0 and now.minute <= 30:
                self.debug("Inter-day statement time reached")
                return True
            return False
        except Exception as e:
            self.error(e)
            return False

    # ─── WATCHDOG SIGNAL ─────────────────────────────────────────────────────

    def update(self) -> None:
        """
        Put 'alive' into the child_status Queue so the parent watchdog in
        main.py knows this child process is still running.

        The 15-minute timeout in main.py resets each time it receives 'alive'
        from this queue. If nothing is received for 15 minutes, the parent
        kills and restarts the child process.

        V1 location: TransactionManager.update()
        """
        self.status.put("alive")

    # ─── LOGGING SHORTCUTS ───────────────────────────────────────────────────
    # Wrap module-level logger functions with the command context so every log
    # line from this TransactionManager includes the trader/instruction metadata

    def debug(self, msg) -> None:
        debug(msg=msg, command=self.command)

    def info(self, msg) -> None:
        info(msg=msg, command=self.command)

    def warn(self, msg) -> None:
        warn(msg=msg, command=self.command)

    def error(self, msg) -> None:
        error(msg=msg, command=self.command)
