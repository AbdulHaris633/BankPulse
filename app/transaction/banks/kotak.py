import re
import json
import hashlib
import time
from multiprocessing import Queue, Value

from selenium.common.exceptions import WebDriverException

from app.transaction.base import TransactionManager
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
    get_otp,
)


class KotakTransactionManager(TransactionManager):
    """
    Transaction manager for Kotak Mahindra Bank netbanking portal.
    URL: https://netbanking.kotak.com/

    Operations supported:
    - login_check  : username + image CAPTCHA + password + OTP (optional)
    - sync         : scrapes recent transactions page, hashes each tx,
                     reports new ones to server until latest_tx_hash matched
    - payout       : navigates to transfer section, submits OTP to confirm

    Login flow:
    1. Navigate to netbanking URL
    2. Enter username + solve image CAPTCHA (up to 10 attempts)
    3. Enter password
    4. Enter OTP if prompted (optional — not always shown)
    5. Dismiss onboarding modal if present
    6. Wait for home dashboard, scrape account number

    V1 location: kotak_transaction_manager.py
    V2 location: app/transaction/banks/kotak.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.institution_name: str = "KOTAK"
        self.url: str = "https://netbanking.kotak.com/"
        # clear_cache=True (default) — ensures no stale Kotak session cookies
        # from a previous run interfere with the fresh login

    # ─── CAPTCHA ─────────────────────────────────────────────────────────────

    def solve_captcha(self) -> str:
        """
        Grab the inline base64 CAPTCHA image from the login form and solve it.
        Kotak renders the CAPTCHA as an inline data URI on the #crnForm.
        Uses TransactionManager.solve() which tries 2Captcha then AntiCaptcha.

        Returns solved text string, or False on failure.

        V1 location: KotakTransactionManager.solve_captcha()
        """
        try:
            captcha_b64 = self.find_by_css('#crnForm img[src^="data"]').screenshot_as_base64
            captcha_text = self.solve(captcha_b64)
            return captcha_text
        except Exception as e:
            self.error(e)
            return False

    # ─── LOGIN ───────────────────────────────────────────────────────────────

    def login(self) -> bool:
        """
        Full login flow for Kotak netbanking:
        1. Navigate to portal URL
        2. Wait for username field to appear
        3. Loop up to 10 times: enter username + solve CAPTCHA
           — if CAPTCHA wrong, #userName field reappears — retry
        4. Enter password and submit
        5. If OTP page appears, fetch OTP from server and submit
        6. Dismiss onboarding modal if shown
        7. Wait for home dashboard and scrape account number

        V1 location: KotakTransactionManager.login()
        """
        try:
            self.info("Starting login")
            self.get(self.url)
            self.wait_for_element_by_css("#userName", timeout=30)
            self.random_sleep(2, 3)

            # ── CAPTCHA loop (up to 10 attempts) ─────────────────────────────
            # Kotak shows an image CAPTCHA on the username screen.
            # If the CAPTCHA is wrong, #userName stays on screen — detect that
            # and retry with a fresh CAPTCHA.
            captcha_solved = False
            for i in range(0, 10):
                self.debug("Sending username")
                try:
                    self.maximize_window()
                except Exception:
                    pass
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id("userName"), self.username)
                self.debug("Solving CAPTCHA")
                captcha_code = self.solve_captcha()
                if not captcha_code:
                    raise Exception("Could not solve CAPTCHA")
                self.debug("Sending CAPTCHA code")
                self.send_keys(self.find_by_css('input[placeholder="Enter Captcha"]'), captcha_code)
                self.random_sleep(2, 3)
                # Submit username + CAPTCHA form
                self.click(self.find_by_css("div.cardFooter > button"))
                self.random_sleep(5, 8)
                self.random_sleep(5, 8)
                # If #userName is still present, the CAPTCHA was wrong — retry
                if not self.find_by_css("#userName"):
                    captcha_solved = True
                    break
                self.debug("CAPTCHA code was incorrect, retrying")

            if not captcha_solved:
                raise Exception("Could not solve CAPTCHA after 10 attempts")
            self.debug("CAPTCHA solved")

            # ── Password page ─────────────────────────────────────────────────
            self.debug("Sending password")
            self.wait_for_element_by_css('input[type="password"]', timeout=30)
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[type="password"]'), self.password)
            self.random_sleep(2, 3)
            self.click(self.find_by_css("div.cardFooter > button"))

            # ── OTP page (optional) ───────────────────────────────────────────
            # OTP page is not always shown — depends on device trust status.
            # If #otpMobile appears, fetch OTP from server and submit it.
            try:
                self.debug("Checking for OTP page")
                self.wait_for_element_by_css("#otpMobile", timeout=30)
                self.random_sleep(2, 3)
                self.debug("Fetching OTP")
                otp_code = get_otp(self.command)
                if not otp_code:
                    raise Exception("Could not get OTP")
                self.debug("OTP fetched: " + str(otp_code))
                self.send_keys(self.find_by_id("otpMobile"), otp_code)
                self.random_sleep(2, 3)
                self.click(self.find_by_css("div.cardFooter > button"))
                self.random_sleep(5, 8)
                self.random_sleep(5, 8)
            except Exception as e:
                self.warn(e)
                self.debug("Could not complete OTP, trying regular login")

            # ── Onboarding modal (optional) ───────────────────────────────────
            # Kotak sometimes shows an onboarding/feature tour modal after login.
            # Dismiss it by clicking "Not Now" or the Next button.
            if self.find_by_css("div.form-content-onboarding"):
                self.debug("Onboarding form detected")
                not_now_btn_selector = "//a[contains(text(), 'Not Now')]"
                not_now_btn = self.find_by_xpath(not_now_btn_selector)
                if not_now_btn:
                    self.debug("Clicking Not Now button")
                    self.click(not_now_btn)
                self.random_sleep(2, 3)
                if self.find_by_css("div.form-content-onboarding button"):
                    self.debug("Clicking Next button")
                    self.click(self.find_by_css("div.form-content-onboarding button"))

            # ── Wait for dashboard ────────────────────────────────────────────
            self.wait_for_element_by_css("div.home-menu", timeout=30)
            self.random_sleep(2, 3)

            # Scrape account number from the recent transactions heading
            header_selector = "app-recent-transaction-desc-tab div.heading-wrapper"
            try:
                header_text = self.find_by_css(header_selector).text.strip()
                self.account_no = re.findall(r"\d+", header_text)[0]
                self.debug("Account no. found: " + self.account_no)
            except Exception as e:
                self.error(e)
                raise Exception("Could not find account number")

            self.info("Login successful")
            return True
        except Exception as e:
            self.error(e)
            self.error("Could not login")
            return False

    # ─── LOGOUT ──────────────────────────────────────────────────────────────

    def logout(self) -> bool:
        """
        Logout from Kotak netbanking:
        1. Click the last item in the header right nav (profile/settings)
        2. Click the logout button in the profile dropdown

        V1 location: KotakTransactionManager.logout()
        """
        try:
            self.debug("Starting logout")
            self.random_sleep(2, 3)
            header_btns_selector = "div.header > div.sub-header.header-right li"
            # Last li in the header nav is the profile/logout button
            self.click(self.find_by_css(header_btns_selector, multiple=True).pop())
            self.random_sleep(2, 3)
            self.click(self.find_by_css("div.profile-logout"))
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            self.debug("Finished logout")
            return True
        except Exception as e:
            self.error(e)
            self.error("Error during logout")
            return False

    # ─── SYNC HELPERS ────────────────────────────────────────────────────────

    def open_recent_transactions(self) -> bool:
        """
        Navigate to the recent transactions list inside the Kotak portal.

        Steps:
        1. Click the main content area to trigger the nav menu
        2. Wait for the transaction tab to appear
        3. Click the 3rd nav item (Accounts section)
        4. Click the 1st sub-nav item (Recent Transactions)
        5. Wait for transaction list items to load

        Called at the start of each sync loop iteration and on WebDriverException
        recovery to re-open the transaction list after a page error.

        V1 location: KotakTransactionManager.open_recent_transactions()
        """
        try:
            self.debug("Opening recent transactions list")
            self.click(self.find_by_css("div.clr ~ div.home-menu"))
            self.wait_for_element_by_css("app-recent-transaction-desc-tab", timeout=30)
            self.random_sleep(2, 3)
            nav_items_selector = "div.nav-left-section div.nav-item-section > div"
            sub_nav_items_selector = "app-sub-navigation-item > ul > li"
            # Index 2 = Accounts section in the left nav
            self.click(self.find_by_css(nav_items_selector, multiple=True)[2])
            self.random_sleep(2, 3)
            # Index 0 = Recent Transactions sub-nav item
            self.click(self.find_by_css(sub_nav_items_selector, multiple=True)[0])
            self.wait_for_element_by_css("li[apppopanimation]", timeout=30)
            self.random_sleep(2, 3)
            self.debug("Finished opening transactions list")
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── SYNC ────────────────────────────────────────────────────────────────

    def sync_transactions(self) -> bool:
        """
        Continuous sync loop — polls the server for sync mode, scrapes
        transactions from the Kotak recent transactions page, and reports
        new ones to the server until the latest_tx_hash is matched.

        Loop flow:
        1. Fetch sync mode from server (operation + latest_tx_hash + refresh_time)
        2. If operation == "sleep" → logout and return True (server wants bot to stop)
        3. Open recent transactions page
        4. Parse total pages from pagination element
        5. For each page, loop through li[apppopanimation] items:
           - Extract date, description, ref no, amount, balance
           - Hash the tx_info dict with SHA-256
           - If hash matches latest_tx_hash → stop (already synced up to here)
           - Otherwise add to new_transactions list
        6. If new transactions found:
           - Reverse list (oldest first)
           - Chain hashes: new_state_hash = SHA-256(previous_hash::tx_hash)
           - Report each transaction to server via report_sync_status()
        7. If no new transactions → report "reached_tip" to server
        8. Sleep for refresh_time seconds, then repeat

        WebDriverException recovery: up to 5 errors tolerated before giving up.
        On each error, tries to re-open the transaction list and continue.

        V1 location: KotakTransactionManager.sync_transactions()
        """
        try:
            self.info("Starting transaction sync")
            webdriver_errors = 0

            while True:
                try:
                    # Fetch current sync mode from server
                    sync_mode = fetch_sync_mode(self.instruction_id)
                    self.debug("Sync mode: " + str(sync_mode))
                    sync_mode_command = sync_mode["command"]
                    operation = sync_mode_command["operation"]
                    latest_tx_found = False
                    new_transactions = []

                    # Server signals bot to stop syncing and logout
                    if operation == "sleep":
                        self.info("Sync mode changed to sleep, logging out")
                        self.data["query"] = "sync"
                        self.data["action"] = "sleep"
                        if not self.logout():
                            if not report_sync_status(self.data, False):
                                raise Exception("Could not report sync status")
                            raise Exception("Could not logout, exiting sync")
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                        self.info("Sync finished successfully")
                        return True

                    previous_tx_hash = sync_mode_command["previous_hash"]
                    refresh_time = sync_mode_command["refresh_time"]

                    # ── Transaction scraping loop ─────────────────────────────
                    while not latest_tx_found:
                        self.debug("Starting transaction search")
                        if not self.open_recent_transactions():
                            raise WebDriverException("Could not open transaction list")
                        self.random_sleep(2, 3)

                        # Parse total number of pages from pagination element
                        # Format: "1-10 of N" — extract N, calculate pages
                        self.debug("Parsing total number of pages")
                        try:
                            pages_text = self.find_by_css("ul.paddinationNav li").text.strip()
                            total_transactions = int(
                                re.match(r"^1-10 of (\d+)$", pages_text).group(1)
                            )
                            pages = int(total_transactions / 10)
                            if total_transactions % 10:
                                pages += 1
                        except Exception as e:
                            self.warn("Could not get total number of pages")
                            self.warn(e)
                            pages = 1
                        self.debug("Total transaction pages: " + str(pages))

                        # Loop through each page of transactions
                        for i in range(1, pages):
                            transactions = self.find_by_css("li[apppopanimation]", multiple=True)
                            for transaction in transactions:
                                try:
                                    # Extract transaction fields from DOM
                                    tx_heading = self.find_by_css("div.list-heading", parent=transaction)
                                    tx_desc = self.find_by_css("div.heading", parent=tx_heading).text.strip()
                                    tx_date = self.find_by_css("div.heading-desc", parent=tx_heading).text.strip()
                                    tx_data = self.find_by_css("div.list-desc", parent=transaction)
                                    amount_cols = self.find_by_css("div.amount-column", multiple=True, parent=tx_data)
                                    tx_ref_no = amount_cols[0].text.strip()
                                    tx_amount = amount_cols[1].text.strip()
                                    # Clean amount — remove ₹, commas, spaces
                                    tx_amount = tx_amount.replace("₹", "").replace(",", "").replace(" ", "")
                                    total_balance = self.find_by_css("div.item-view-balance-value", parent=tx_data).text.strip()
                                    total_balance = total_balance.replace("₹", "").replace(",", "").replace(" ", "")

                                    # Positive amount = credit transaction
                                    credit_tx = 1 if float(tx_amount) > 0 else 0

                                    # Build tx_info dict and hash it — this hash
                                    # is used to deduplicate transactions across syncs
                                    tx_info = {
                                        "tx_date": tx_date,
                                        "remarks": tx_desc,
                                        "ref_no": tx_ref_no,
                                        "tx_value_date": tx_date,
                                        "tx_amount": tx_amount,
                                        "total_available_balance": total_balance,
                                    }
                                    tx_info_hash_str = json.dumps(tx_info, separators=(",", ":"))
                                    tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()

                                    # If this tx hash matches the server's latest_tx_hash,
                                    # we've reached the point we synced up to last time — stop
                                    if tx_hash == sync_mode_command["latest_tx_hash"]:
                                        self.info("Latest tx_hash found: " + tx_hash)
                                        latest_tx_found = True
                                        break

                                    transaction_json = {
                                        "instruction_id": self.instruction_id,
                                        "query": "sync",
                                        "action": "sync_tx",
                                        "success": 0,
                                        "trader_id": self.trader_id,
                                        "bot_id": self.bot_id,
                                        "credit_tx": credit_tx,
                                        "tx_amount": tx_amount,
                                        "tx_desc": tx_desc,
                                        "total_available_balance": total_balance,
                                        "previous_hash": "",
                                        "tx_info": tx_info,
                                        "tx_hash": tx_hash,
                                        "new_state_hash": "",
                                    }
                                    self.info("New transaction: " + str(transaction_json))
                                    new_transactions.append(transaction_json)

                                    # If this is the last tx on the last page,
                                    # we've gone through everything — stop
                                    if (
                                        transaction == transactions[len(transactions) - 1]
                                        and i == pages - 1
                                    ):
                                        self.info("Last transaction in the list, exiting")
                                        latest_tx_found = True

                                except Exception as e:
                                    self.warn("Could not parse transaction")
                                    self.warn(e)

                            if latest_tx_found:
                                break

                            # Navigate to next page of transactions
                            self.debug("Clicking next page button")
                            self.click(self.find_by_css("span.icon-pagination_arrow_right"))
                            self.wait_for_element_by_css("li[apppopanimation]", timeout=30)
                            self.random_sleep(2, 3)

                    # ── Report new transactions ───────────────────────────────
                    if new_transactions:
                        self.info("New transactions found: " + str(len(new_transactions)))
                        # Reverse so we send oldest first — server chains hashes in order
                        new_transactions.reverse()
                        for transaction in new_transactions:
                            latest_tx_hash = transaction["tx_hash"]
                            # Chain hash: SHA-256(previous_hash::this_tx_hash)
                            # This creates a tamper-evident chain of transactions
                            previous_hash_str = "{}::{}".format(previous_tx_hash, latest_tx_hash)
                            new_state_hash = hashlib.sha256(previous_hash_str.encode()).hexdigest()
                            transaction["new_state_hash"] = new_state_hash
                            transaction["previous_hash"] = previous_tx_hash
                            previous_tx_hash = new_state_hash
                            self.info("Sending new transaction: " + str(transaction))
                            if not report_sync_status(transaction, True):
                                raise Exception("Could not report sync status")
                    else:
                        # No new transactions — already at the tip
                        self.info("No new transactions found")
                        latest_tx_hash = sync_mode["command"]["latest_tx_hash"]
                        self.data["query"] = "sync"
                        self.data["action"] = "reached_tip"
                        self.data.update({"latest_tx_hash": latest_tx_hash})
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")

                    # Wait before next sync iteration
                    self.debug("Sleeping for: " + str(refresh_time))
                    time.sleep(refresh_time)

                except WebDriverException as e:
                    # Selenium errors during sync — tolerate up to 5 before giving up
                    self.warn("WebDriver error during sync")
                    self.warn(e)
                    self.random_sleep(5, 8)
                    if webdriver_errors == 5:
                        self.error("WebDriver errors exceeded 5, exiting sync")
                        raise Exception(str(e))
                    webdriver_errors += 1
                    self.random_sleep(2, 3)
                    # Try to recover by re-opening the transaction list
                    if not self.open_recent_transactions():
                        self.warn("Could not reopen recent transactions after error")
                    self.random_sleep(2, 3)

        except Exception as e:
            self.error(e)
            return False

    # ─── PAYOUT ──────────────────────────────────────────────────────────────

    def payout(self) -> bool:
        """
        Complete a payout (fund transfer) on Kotak netbanking.

        Flow:
        1. Click the 2nd nav item (Payments/Transfer section)
        2. Wait for OTP input (#otpMobile)
        3. Fetch OTP from server
        4. Submit OTP to confirm the transfer

        Note: The beneficiary is assumed to already be added and the transfer
        amount pre-set by a prior add_beneficiary + payout setup flow on the
        server side. This method only handles the OTP confirmation step.

        V1 location: KotakTransactionManager.payout()
        """
        try:
            self.random_sleep(2, 3)
            nav_items_selector = "app-left-side-nav-bar div.nav-item-section > div"
            # Index 1 = Payments/Transfer section in the left nav
            self.click(self.find_by_css(nav_items_selector, multiple=True)[1])
            self.wait_for_element_by_css("#otpMobile", timeout=30)
            self.random_sleep(2, 3)
            otp_response = get_otp(self.command)
            if not otp_response:
                raise Exception("Could not fetch OTP, exiting")
            otp = str(otp_response["otp"])
            self.send_keys(self.find_by_id("otpMobile"), otp)
            self.random_sleep(2, 3)
            self.click(self.find_by_css("div.modal-footer-dashboard button"))
            return True
        except Exception as e:
            self.error(e)
            self.error("Could not complete payout")
            return False
