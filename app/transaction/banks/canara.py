import re
import json
import hashlib
import time
import os
from multiprocessing import Queue, Value
from datetime import datetime, timedelta

from selenium.webdriver import ActionChains, Keys
from selenium.common.exceptions import WebDriverException

from app.transaction.base import TransactionManager
from app.core.settings import Settings
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)

# Used for download_dir path in parse_statement()
_settings = Settings()


class CanaraTransactionManager(TransactionManager):
    """
    Transaction manager for Canara Bank netbanking portal.
    URL: https://online.canarabank.in/?module=login

    Operations supported:
    - login_check    : username + password + image CAPTCHA (up to 3 attempts)
    - sync           : downloads CSV statement, parses it, reports new txs
    - add_beneficiary: adds a domestic or intra-bank beneficiary

    Login flow:
    1. Navigate to login URL
    2. Dismiss popup if present
    3. Enter username + password + solve CAPTCHA
    4. Submit login — if "Invalid Captcha" appears, retry up to 3 times
    5. Dismiss alert modals (transaction password, QR code prompts)
    6. Click Accounts nav and scrape account number from the accounts table

    Sync flow:
    1. Downloads CSV bank statement (date range or last N transactions)
    2. Parses each CSV row into a transaction dict
    3. Hashes each transaction and reports new ones to the server
    4. Uses download_and_parse_statement() → parse_statement() pipeline

    V1 location: canara_transaction_manager.py
    V2 location: app/transaction/banks/canara.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - download_dir from config.py → _settings.DOWNLOAD_DIR
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = "https://online.canarabank.in/?module=login"
        self.institution_name: str = "CANARABANKSAVINGS"
        # Tracks the last downloaded statement filename for change detection
        self.last_download: str = ""
        self.reached_tip: bool = False
        # Set to True when open_statement() fails — triggers sync abort
        self.open_statement_error: bool = False
        # When True uses Date Range filter; False uses Last N Transactions
        self.use_date_range: bool = True

    # ─── LOGIN ───────────────────────────────────────────────────────────────

    def login(self) -> bool:
        """
        Login to Canara Bank netbanking.

        Outer loop (3 attempts): retries the full login if CAPTCHA is wrong
        Inner loop (3 attempts): retries on WebDriverException during form fill

        Steps:
        1. Navigate to login URL
        2. Dismiss popup modal if visible
        3. Fill username + password + solve CAPTCHA
        4. Submit login
        5. If "Invalid Captcha Input" found in page source → retry outer loop
        6. After successful login, dismiss alert modals
        7. Navigate to Accounts section, scrape account number from table

        V1 location: CanaraTransactionManager.login()
        """
        username = 'input[name="username"]'
        password = 'input[name="password"]'
        login_btn = 'oj-button[id="login-button"]'
        accounts = 'a[alt="Accounts & Services"]'
        account = 'span[data-bind*="displayValue"]'
        captcha_solved = False
        try:
            # ── Outer CAPTCHA retry loop ──────────────────────────────────────
            for f in range(0, 3):
                if captcha_solved:
                    break
                # ── Inner WebDriverException retry loop ───────────────────────
                for i in range(0, 3):
                    try:
                        self.update()
                        self.debug("Starting login")
                        self.get(self.url)
                        self.wait_for_element_by_css(username, timeout=30)
                        self.maximize_window()
                        self.random_sleep(2, 3)

                        # Dismiss popup if it appears on login page
                        popup = self.find_by_id("popup")
                        if popup:
                            self.debug("Popup window found")
                            if popup.is_displayed():
                                try:
                                    self.click(self.find_by_css("div.close-btn", parent=popup))
                                except Exception as ex:
                                    self.warn("Error closing popup")
                                    self.warn(ex)

                        self.debug("Sending username")
                        self.send_keys(self.find_by_css(username), self.username)
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_css(password), self.password)
                        self.random_sleep(2, 3)
                        self.update()
                        self.maximize_window()
                        self.solve_captcha()
                        break
                    except WebDriverException as e:
                        self.warn("WebDriver exception in login")
                        self.warn(e)

                # Submit the login form
                self.maximize_window()
                self.click(self.find_by_css(login_btn))
                if self.wait_for_element_by_css(accounts, timeout=30):
                    captcha_solved = True
                else:
                    self.warn("Account not found, checking for CAPTCHA invalid text")
                    if re.search(r"Invalid Captcha Input", self.driver.page_source, re.I | re.M):
                        self.debug("Invalid CAPTCHA detected, retrying")
                    else:
                        raise Exception("Invalid CAPTCHA text not found, exiting")

            if not captcha_solved:
                raise Exception("Could not solve CAPTCHA after 3 attempts")
            self.random_sleep(5, 8)

            # Dismiss alert modals that Canara shows after login
            # (transaction password setup, QR code setup, login password prompts)
            self.clear_alert_modals()

            # ── Navigate to accounts and scrape account number ────────────────
            self.update()
            for i in range(0, 3):
                try:
                    self.debug("Clicking accounts nav button")
                    self.maximize_window()
                    self.click_displayed(accounts)
                    self.wait_for_element_by_css("tbody tr", timeout=30)
                    self.random_sleep(2, 3)
                    self.debug("Searching for account no.")
                    account_rows = self.find_by_css("tbody tr", multiple=True)
                    self.update()
                    self.maximize_window()
                    # Last row in the accounts table contains the primary account
                    last_row = account_rows.pop()
                    account_no = self.find_by_css(account, parent=last_row).text.strip()
                    self.account_no = account_no
                    if not account_no:
                        raise Exception("Could not find account no.")
                    self.debug("Account no. found: " + str(account_no))
                    break
                except WebDriverException as e:
                    self.clear_alert_modals()
                    self.warn("WebDriver exception in login")
                    self.warn(e)

            self.update()
            self.info("Login success")
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── ALERT MODALS ────────────────────────────────────────────────────────

    def clear_alert_modals(self) -> None:
        """
        Dismiss Canara Bank alert/prompt modals that appear after login.
        Canara shows modals asking users to set up transaction password,
        QR code, or change login password — these block navigation if not closed.

        Checks for each known modal text and clicks the close button if found.

        V1 location: CanaraTransactionManager.clear_alert_modals()
        """
        alert_modal_css = 'div[role="alert"]'
        modal_close_btn = "a.modal-header__close"
        # Known modal types that appear after Canara login
        alert_modal_texts = ["transaction password", "qr code", "login password"]
        try:
            for alert_modal_text in alert_modal_texts:
                if self.find_by_css(alert_modal_css):
                    alert_modals = self.find_by_css(alert_modal_css, multiple=True)
                    for alert_modal in alert_modals:
                        if alert_modal.is_displayed():
                            if re.search(alert_modal_text, alert_modal.text, re.I | re.M):
                                if self.find_by_css(modal_close_btn, parent=alert_modal):
                                    try:
                                        self.click(self.find_by_css(modal_close_btn, parent=alert_modal))
                                        self.random_sleep(2, 3)
                                    except Exception as modal_click_exception:
                                        self.error("Error clicking modal close button")
                                        self.error(modal_click_exception)
        except Exception as e:
            self.error(e)

    # ─── CAPTCHA ─────────────────────────────────────────────────────────────

    def solve_captcha(self) -> bool:
        """
        Grab the Canara login CAPTCHA image and solve it.
        Canara uses a custom CAPTCHA rendered as img.customCaptcha.
        Uses TransactionManager.solve() which tries 2Captcha then AntiCaptcha.

        V1 location: CanaraTransactionManager.solve_captcha()
        """
        try:
            self.update()
            captcha_selector = "img.customCaptcha"
            captcha_input = 'input[placeholder="Captcha"]'
            captcha_b64 = self.find_by_css(captcha_selector).screenshot_as_base64
            captcha_text = self.solve(captcha_b64)
            if not captcha_text:
                raise Exception("Could not send CAPTCHA to anticaptcha")
            self.send_keys(self.find_by_css(captcha_input), captcha_text)
            self.random_sleep(2, 3)
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── LOGOUT ──────────────────────────────────────────────────────────────

    def logout(self) -> bool:
        """
        Logout from Canara netbanking by clicking the logout link.

        V1 location: CanaraTransactionManager.logout()
        """
        logout = 'a[data-bind*="logout"] span'
        try:
            self.update()
            self.debug("Starting logout")
            self.random_sleep(2, 3)
            self.click(self.find_by_css(logout))
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            self.debug("Logout success")
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── STATEMENT ───────────────────────────────────────────────────────────

    def open_statement(self) -> bool:
        """
        Navigate to the Canara account statement page and apply filters.

        Steps (up to 5 retry attempts):
        1. Click Accounts & Services nav
        2. Click Statements sub-menu item
        3. Select the account number from the dropdown
        4. Select date range type:
           - do_full_sync=True  → "Last N Transactions" (10000)
           - do_full_sync=False → "Date Range" (yesterday to today)
        5. Apply filter and wait for results table
        6. Set page size to 10000 via JS to load all transactions at once

        V1 location: CanaraTransactionManager.open_statement()
        """
        left_menu_btns_css = 'div[data-bind*="left-menu"] ul.mainMenu > li'
        sub_menu_btns_css = 'div[data-bind*="left-menu"] ul.subMenu> li'
        download_type = 'div.account-statement-details-balances__dropdown div.oj-select'
        listbox_results_css = 'ul.oj-listbox-results > li'
        listbox_results_sub_css = 'ul.oj-listbox-result-sub'
        accounts = 'a.oj-navigationlist-item-no-icon[alt="Accounts & Services"]'
        account_no_input_css = 'obdx-component.account-input-container oj-select-one'
        date_type_input = 'div.account-statement-left__selectPeriod oj-select-one'
        apply_filter_btn = 'oj-button[on-click="[[searchTransaction]]"]'
        no_txns_text = r'No Activity found for the specified period.'
        statement_opened = False
        try:
            for i in range(0, 5):
                try:
                    self.debug("Opening statement")
                    self.update()
                    self.maximize_window()
                    self.debug("Clicking accounts nav button")
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    # Scroll to top before clicking nav to avoid element-not-visible errors
                    ActionChains(self.driver).key_down(Keys.PAGE_UP).key_up(Keys.PAGE_UP).perform()
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css(accounts))
                    self.random_sleep(2, 3)
                    self.wait_for_element_by_css("tbody tr", timeout=30)
                    self.random_sleep(2, 3)

                    # Navigate to Statements sub-menu
                    self.debug("Clicking statements nav button")
                    left_menu_btns = self.find_by_css(left_menu_btns_css, multiple=True)
                    self.click(left_menu_btns[2])  # Index 2 = Account Statements section
                    self.random_sleep(2, 3)
                    sub_menu_btns = self.find_by_css(sub_menu_btns_css, multiple=True)
                    self.click(sub_menu_btns[0])   # Index 0 = Account Statement sub-item
                    self.maximize_window()
                    self.wait_for_element_by_css(account_no_input_css, timeout=30)
                    self.random_sleep(5, 8)
                    self.random_sleep(5, 8)

                    # ── Select account number from dropdown ───────────────────
                    self.update()
                    self.debug("Selecting account no.")
                    self.click(self.find_by_css(account_no_input_css))
                    self.random_sleep(2, 3)
                    account_no_found = False

                    # Try sub-results first (grouped dropdown), then main results
                    self.debug("Checking submenu list")
                    listbox_results_sub = self.find_by_css(listbox_results_sub_css, multiple=True)
                    for listbox_results_sub_item in listbox_results_sub:
                        if listbox_results_sub_item.is_displayed():
                            if re.search(str(self.account_no), listbox_results_sub_item.text.strip()):
                                listbox_results_sub_item.click()
                                self.debug("Account no. selected")
                                account_no_found = True
                                self.random_sleep(2, 3)
                                break

                    if not account_no_found:
                        self.debug("Checking main menu list")
                        listbox_results = self.find_by_css(listbox_results_css, multiple=True)
                        for listbox_result in listbox_results:
                            if listbox_result.is_displayed():
                                if re.search(str(self.account_no), listbox_result.text.strip()):
                                    self.click(listbox_result)
                                    self.debug("Account no. selected")
                                    account_no_found = True
                                    self.random_sleep(2, 3)
                                    break

                    if not account_no_found:
                        raise Exception("Could not find account no. in statements page")

                    # ── Select date range type ────────────────────────────────
                    self.update()
                    self.debug("Selecting date range type")
                    self.maximize_window()
                    self.random_sleep(2, 3)

                    # Full sync uses last N transactions; normal sync uses date range
                    if self.do_full_sync:
                        date_range_filter = "Last N Transactions"
                    else:
                        date_range_filter = "Date Range"

                    self.click(self.find_by_css(date_type_input))
                    self.random_sleep(2, 3)
                    date_range_type_selected = False
                    listbox_results = self.find_by_css(listbox_results_css, multiple=True)
                    for listbox_result in listbox_results:
                        if listbox_result.is_displayed():
                            if re.search(date_range_filter, listbox_result.text.strip()):
                                self.click(listbox_result)
                                self.debug("Date range type selected")
                                self.random_sleep(2, 3)
                                date_range_type_selected = True
                                break

                    if not date_range_type_selected:
                        raise Exception("Could not select date range type")

                    if self.do_full_sync:
                        # Request 10000 transactions for full sync
                        self.send_keys(self.find_by_css("label[data-bind] ~ oj-input-text"), 10000)
                        self.do_full_sync = False
                    else:
                        if date_range_filter == "Date Range":
                            # Set date range to yesterday–today
                            date_filters_css = "input.oj-inputdatetime-input"
                            self.wait_for_element_by_css(date_filters_css, timeout=30)
                            self.random_sleep(2, 3)
                            date_elements = self.find_by_css(date_filters_css, multiple=True)
                            from_date_input = date_elements[0]
                            to_date_input = date_elements[1]
                            day_delta = timedelta(days=1)
                            today = datetime.now().strftime("%d %b %Y")
                            yesterday = (datetime.now() - day_delta).strftime("%d %b %Y")
                            self.send_keys(from_date_input, yesterday)
                            self.random_sleep(2, 3)
                            self.click(self.find_by_tag("body"))
                            self.random_sleep(2, 3)
                            self.send_keys(to_date_input, today)
                            self.random_sleep(2, 3)
                            self.click(self.find_by_tag("body"))

                    self.random_sleep(2, 3)
                    self.click(self.find_by_css(apply_filter_btn))

                    if not self.wait_for_element_by_css(download_type, timeout=30):
                        self.warn("Could not find transactions, checking for reason")
                        if re.search(no_txns_text, self.driver.page_source, re.I | re.M):
                            self.warn("No transactions exist in selected period")
                            self.warn("Trying full sync")
                            self.do_full_sync = True
                            continue

                    self.random_sleep(5, 8)
                    # Set page size to 10000 via JS so all transactions load at once
                    script = "document.querySelector('oj-paging-control').setAttribute('page-size', 10000);"
                    self.driver.execute_script(script)
                    self.random_sleep(5, 8)
                    self.debug("Statement opened")
                    statement_opened = True
                    break

                except Exception as e:
                    self.error("Error opening statement")
                    self.error(e)

            if not statement_opened:
                raise Exception("Could not open statement after 5 attempts")
            return True
        except Exception as e:
            self.error(e)
            return False

    def download_statement(self) -> bool:
        """
        Open the statement page and download it as a CSV file.

        Steps (up to 2 attempts):
        1. Call open_statement() to navigate and apply filters
        2. Select "CSV" from the download type dropdown
        3. Click the download button
        4. Poll chrome://downloads (via get_most_recent_download()) to detect
           when the new file appears — compares against self.last_download

        V1 location: CanaraTransactionManager.download_statement()
        """
        download_type = 'div.account-statement-details-balances__dropdown div.oj-select'
        listbox_results_css = 'ul.oj-listbox-results > li'
        download_btn = 'oj-button[on-click*="submit"]'
        statement_has_downloaded = False
        try:
            for i in range(0, 2):
                try:
                    if not self.open_statement():
                        raise Exception("Could not open statement")

                    # Select CSV as download format
                    self.update()
                    self.maximize_window()
                    self.debug("Selecting download type")
                    self.random_sleep(2, 3)
                    download_type_selected = False
                    self.click(self.find_by_css(download_type))
                    self.random_sleep(2, 3)
                    listbox_results = self.find_by_css(listbox_results_css, multiple=True)
                    for listbox_result in listbox_results:
                        if listbox_result.is_displayed():
                            if listbox_result.text.strip() == "CSV":
                                self.click(listbox_result)
                                self.debug("Download type selected")
                                self.random_sleep(2, 3)
                                download_type_selected = True
                                break
                    if not download_type_selected:
                        raise Exception("Could not select download type")

                    # Click download and wait for file to appear
                    self.update()
                    self.maximize_window()
                    self.debug("Downloading statement")
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css(download_btn))
                    statement_downloaded = False
                    for o in range(0, 3):
                        self.random_sleep(5, 8)
                        self.update()
                        # New file = different from last download filename
                        if not self.get_most_recent_download() == self.last_download:
                            statement_downloaded = True
                            break
                    if not statement_downloaded:
                        raise Exception("Statement download timeout")
                    self.last_download = self.get_most_recent_download()
                    self.update()
                    statement_has_downloaded = True
                    break
                except Exception as e:
                    self.error("Error downloading statement")
                    self.error(e)

            if not statement_has_downloaded:
                self.open_statement_error = True
                raise Exception("Could not download statement")
            return True
        except Exception as e:
            self.error(e)
            return False

    def parse_statement(self) -> list:
        """
        Parse a downloaded Canara CSV statement file into a list of transaction dicts.

        CSV format uses quoted fields with = prefix for formula-injection prevention:
            ="DD-MM-YYYY",="DD-MM-YYYY",="ref_no",="remarks",...,amount,balance

        Each parsed transaction dict contains:
            tx_date, tx_value_date, tx_ref_no, tx_remarks,
            tx_debit, tx_credit, credit_tx, total_available_balance

        V1 location: CanaraTransactionManager.parse_statement()
        V2 change: download_dir from config.py → _settings.DOWNLOAD_DIR
        """
        try:
            self.update()
            self.debug("Starting parse transactions")
            # V2: uses _settings.DOWNLOAD_DIR instead of bare download_dir from config
            statement_filename = _settings.DOWNLOAD_DIR + os.sep + self.last_download
            self.debug("Transaction file: " + statement_filename)
            statement_file = open(statement_filename, "r").read()

            # Match Canara CSV transaction rows (quoted fields with = prefix)
            tx_regex = r'^\s*=".+",=".+",.+$'
            tx_matches = re.findall(tx_regex, statement_file, re.M)
            self.debug("tx_matches: " + str(len(tx_matches)))

            transactions = []
            for tx_match in tx_matches:
                try:
                    tx_data = tx_match.split('"')

                    # Parse and validate each field
                    tx_date = tx_data[1].strip()
                    if not re.search(r"\d\d-\d\d-\d\d\d\d", tx_date):
                        raise Exception("Could not parse transaction, tx_date regex")

                    tx_value_date = tx_data[3].strip()
                    if not re.search(r"\d\d\d\d", tx_value_date):
                        raise Exception("Could not parse transaction, tx_value_date regex")

                    try:
                        tx_ref_no = tx_data[5].strip()
                        if not re.match(r"^\d+$", tx_ref_no):
                            raise Exception("Could not parse transaction, tx_ref_no regex")
                    except Exception:
                        tx_ref_no = ""

                    tx_remarks = tx_data[7]
                    if not len(tx_remarks) > 5:
                        raise Exception("Could not parse transaction, tx_remarks length")

                    # Determine debit vs credit from CSV column structure
                    # Double comma (,,) in position 10 indicates credit entry
                    if re.search(r",,", tx_data[10]):
                        tx_debit = ""
                        tx_credit = tx_data[11].replace(",", "").strip()
                        try:
                            float(tx_credit)
                        except ValueError:
                            raise Exception("Could not parse transaction, tx_credit value error")
                        credit_tx = 1
                    else:
                        tx_debit = tx_data[11].replace(",", "").strip()
                        try:
                            float(tx_debit)
                        except ValueError:
                            raise Exception("Could not parse transaction, tx_debit value error")
                        tx_credit = ""
                        credit_tx = 0

                    try:
                        total_available_balance = tx_data[13].replace(",", "").strip()
                        try:
                            float(total_available_balance)
                        except ValueError:
                            raise Exception(
                                "Could not parse transaction, total_available_balance value error"
                            )
                    except Exception:
                        total_available_balance = ""

                    tx = {
                        "tx_date": tx_date,
                        "tx_value_date": tx_value_date,
                        "tx_ref_no": tx_ref_no,
                        "tx_remarks": tx_remarks,
                        "tx_debit": tx_debit,
                        "tx_credit": tx_credit,
                        "credit_tx": credit_tx,
                        "total_available_balance": total_available_balance,
                    }
                    transactions.append(tx)
                except Exception as e:
                    self.warn("TX PARSING ERROR: " + str(tx_match))
                    self.warn(e)

            if not len(tx_matches) == len(transactions):
                self.warn("Some transactions could not be parsed")
            self.debug("Finished parsing")
            self.debug("Transactions parsed: " + str(len(transactions)))
            return transactions
        except Exception as e:
            self.error(e)
            return []

    def download_and_parse_statement(self) -> list:
        """
        Download the CSV statement and parse it in one call.
        Returns list of parsed transaction dicts, or [] on failure.

        V1 location: CanaraTransactionManager.download_and_parse_statement()
        """
        try:
            if not self.download_statement():
                return []
            return self.parse_statement()
        except Exception as e:
            self.error(e)
            return []

    # ─── SYNC ────────────────────────────────────────────────────────────────

    def scrape_statement(self) -> bool:
        """
        Alternative DOM-scraping sync loop — scrapes tbody rows directly
        instead of downloading a CSV. Not used by default (sync_transactions
        uses the CSV approach). Kept as incomplete V1 alternative.

        NOTE: V1 bugs preserved as-is:
        - Error message says "Could not logout of HDFC" (copy-paste from HDFC TM)
        - tx_info = transaction assigns a WebElement, causing json.dumps to fail

        V1 location: CanaraTransactionManager.scrape_statement()
        V2 changes: Browser methods applied (find_by_css, find_by_tag, random_sleep)
        """
        try:
            self.info("Starting transaction sync")
            webdriver_errors = 0
            while True:
                try:
                    self.update()
                    sync_mode = fetch_sync_mode(self.instruction_id)
                    self.debug("Sync mode: " + str(sync_mode))
                    sync_mode_command = sync_mode["command"]
                    operation = sync_mode_command["operation"]
                    latest_tx_found = False
                    new_transactions = []
                    if operation == "sleep":
                        self.info("Sync mode changed, starting logout")
                        self.data["query"] = "sync"
                        self.data["action"] = "sleep"
                        if not self.logout():
                            if not report_sync_status(self.data, False):
                                raise Exception("Could not report sync status")
                            raise Exception("Could not logout of HDFC, exiting sync")  # V1 bug: should be Canara
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                        self.info("Sync finished successfully")
                        return True
                    previous_tx_hash = sync_mode_command["previous_hash"]
                    refresh_time = sync_mode_command["refresh_time"]
                    while not latest_tx_found:
                        self.update()
                        self.debug("Starting transaction search")
                        self.open_statement()
                        if self.open_statement_error:
                            raise Exception("Could not open statement, exiting sync")

                        # Loop through all transactions
                        transactions = self.find_by_css('tbody tr', multiple=True)
                        for transaction in transactions:
                            tx_data = self.find_by_tag('td', multiple=True, parent=transaction)
                            tx_date = tx_data[1].text.strip()
                            tx_value_date = tx_data[2].text.strip()
                            tx_ref_no = tx_data[3].text.strip()
                            tx_remarks = tx_data[4].text.strip()
                            tx_amount_match = re.match(r'^₹\s+([0-9.,]+)\s+(.+)$', tx_data[6].text.strip())
                            tx_amount = tx_amount_match.group(1)
                            tx_type = tx_amount_match.group(2)
                            try:
                                self.update()
                                tx_info = transaction  # V1 bug: WebElement, json.dumps will fail
                                tx_info_hash_str = json.dumps(tx_info, separators=(',', ':'))
                                tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()
                                if tx_hash == sync_mode_command["latest_tx_hash"]:
                                    self.info("Latest tx_hash found: " + tx_hash)
                                    latest_tx_found = True
                                    self.reached_tip = True
                                    break
                                if transaction["credit_tx"]:  # V1 bug: WebElement not subscriptable
                                    tx_amount = transaction["tx_credit"]
                                else:
                                    tx_amount = transaction["tx_debit"]
                                transaction_json = {
                                    "instruction_id": self.instruction_id,
                                    "query": "sync",
                                    "action": "sync_tx",
                                    "success": 0,
                                    "trader_id": self.trader_id,
                                    "bot_id": self.bot_id,
                                    "credit_tx": transaction["credit_tx"],
                                    "tx_amount": tx_amount,
                                    "tx_desc": transaction["tx_remarks"],
                                    "total_available_balance": transaction["total_available_balance"],
                                    "previous_hash": "",
                                    "tx_info": tx_info,
                                    "tx_hash": tx_hash,
                                    "new_state_hash": "",
                                }
                                self.info("New transaction: " + str(transaction_json))
                                new_transactions.append(transaction_json)
                            except Exception as e:
                                self.warn("Error parsing transaction")
                                self.warn(e)
                            if transaction == transactions[len(transactions) - 1]:
                                self.info("Latest transaction reached")
                                latest_tx_found = True
                                self.reached_tip = True
                                break

                    if new_transactions:
                        self.info("New transactions found")
                        new_transactions.reverse()
                        for transaction in new_transactions:
                            self.update()
                            latest_tx_hash = transaction["tx_hash"]
                            previous_hash_str = "{}::{}".format(previous_tx_hash, latest_tx_hash)
                            new_state_hash = hashlib.sha256(previous_hash_str.encode()).hexdigest()
                            transaction["new_state_hash"] = new_state_hash
                            transaction["previous_hash"] = previous_tx_hash
                            previous_tx_hash = new_state_hash
                            self.info("Sending new transaction: " + str(transaction))
                            if not report_sync_status(transaction, True):
                                raise Exception("Could not report sync status")
                    else:
                        self.info("No new transactions found")
                        self.update()
                        self.reached_tip = True
                        latest_tx_hash = sync_mode["command"]["latest_tx_hash"]
                        self.data["query"] = "sync"
                        self.data["action"] = "reached_tip"
                        self.data.update({"latest_tx_hash": latest_tx_hash})
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                    self.debug("Sleeping for: " + str(refresh_time))
                    time.sleep(refresh_time)
                except WebDriverException as e:
                    self.warn("Webdriver error detected during sync")
                    self.warn(e)
                    self.random_sleep(5, 8)
                    if webdriver_errors == 3:
                        self.error("Number of webdriver errors exceeds 3, exiting")
                        raise Exception(str(e))
                    webdriver_errors += 1
                    self.random_sleep(2, 3)
        except Exception as e:
            self.error(e)
            return False

    def sync_transactions(self) -> bool:
        """
        Continuous sync loop — downloads CSV statement each iteration,
        parses transactions, and reports new ones to the server.

        Loop flow:
        1. Fetch sync mode from server
        2. If operation == "sleep" → logout and return True
        3. Call download_and_parse_statement() to get parsed tx list
        4. If open_statement_error → abort sync
        5. For each transaction: hash it, compare to latest_tx_hash
           - Match → stop (already synced up to here)
           - No match → add to new_transactions
        6. Report new transactions with chained hashes
        7. If no new transactions → report "reached_tip"
        8. Sleep refresh_time seconds, repeat

        WebDriverException recovery: up to 3 errors before giving up.

        V1 location: CanaraTransactionManager.sync_transactions()
        """
        try:
            self.info("Starting transaction sync")
            webdriver_errors = 0
            while True:
                try:
                    self.update()
                    sync_mode = fetch_sync_mode(self.instruction_id)
                    self.debug("Sync mode: " + str(sync_mode))
                    sync_mode_command = sync_mode["command"]
                    operation = sync_mode_command["operation"]
                    latest_tx_found = False
                    new_transactions = []

                    # Server signals bot to stop syncing
                    if operation == "sleep":
                        self.info("Sync mode changed, starting logout")
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

                    while not latest_tx_found:
                        self.update()
                        self.debug("Starting transaction search")

                        # Download and parse the CSV statement
                        transactions = self.download_and_parse_statement()
                        if self.open_statement_error:
                            raise Exception("Could not open statement, exiting sync")

                        # Statement comes newest-first; reverse to process oldest-first
                        transactions.reverse()

                        for transaction in transactions:
                            try:
                                self.update()
                                tx_info = transaction
                                tx_info_hash_str = json.dumps(tx_info, separators=(",", ":"))
                                tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()

                                # Stop if we've reached the last synced transaction
                                if tx_hash == sync_mode_command["latest_tx_hash"]:
                                    self.info("Latest tx_hash found: " + tx_hash)
                                    latest_tx_found = True
                                    self.reached_tip = True
                                    break

                                # Determine amount field based on debit/credit type
                                if transaction["credit_tx"]:
                                    tx_amount = transaction["tx_credit"]
                                else:
                                    tx_amount = transaction["tx_debit"]

                                transaction_json = {
                                    "instruction_id": self.instruction_id,
                                    "query": "sync",
                                    "action": "sync_tx",
                                    "success": 0,
                                    "trader_id": self.trader_id,
                                    "bot_id": self.bot_id,
                                    "credit_tx": transaction["credit_tx"],
                                    "tx_amount": tx_amount,
                                    "tx_desc": transaction["tx_remarks"],
                                    "total_available_balance": transaction["total_available_balance"],
                                    "previous_hash": "",
                                    "tx_info": tx_info,
                                    "tx_hash": tx_hash,
                                    "new_state_hash": "",
                                }
                                self.info("New transaction: " + str(transaction_json))
                                new_transactions.append(transaction_json)

                            except Exception as e:
                                self.warn("Error parsing transaction")
                                self.warn(e)

                            # Stop after processing all transactions
                            if transaction == transactions[len(transactions) - 1]:
                                self.info("Latest transaction reached")
                                latest_tx_found = True
                                self.reached_tip = True
                                break

                    # ── Report new transactions ───────────────────────────────
                    if new_transactions:
                        self.info("New transactions found")
                        new_transactions.reverse()
                        for transaction in new_transactions:
                            self.update()
                            latest_tx_hash = transaction["tx_hash"]
                            previous_hash_str = "{}::{}".format(previous_tx_hash, latest_tx_hash)
                            new_state_hash = hashlib.sha256(previous_hash_str.encode()).hexdigest()
                            transaction["new_state_hash"] = new_state_hash
                            transaction["previous_hash"] = previous_tx_hash
                            previous_tx_hash = new_state_hash
                            self.info("Sending new transaction: " + str(transaction))
                            if not report_sync_status(transaction, True):
                                raise Exception("Could not report sync status")
                    else:
                        self.info("No new transactions found")
                        self.update()
                        self.reached_tip = True
                        latest_tx_hash = sync_mode["command"]["latest_tx_hash"]
                        self.data["query"] = "sync"
                        self.data["action"] = "reached_tip"
                        self.data.update({"latest_tx_hash": latest_tx_hash})
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")

                    self.debug("Sleeping for: " + str(refresh_time))
                    time.sleep(refresh_time)

                except WebDriverException as e:
                    self.warn("WebDriver error detected during sync")
                    self.warn(e)
                    self.random_sleep(5, 8)
                    if webdriver_errors == 3:
                        self.error("Number of WebDriver errors exceeds 3, exiting")
                        raise Exception(str(e))
                    webdriver_errors += 1
                    self.random_sleep(2, 3)

        except Exception as e:
            self.error(e)
            return False

    # ─── ADD BENEFICIARY ─────────────────────────────────────────────────────

    def add_beneficiary(self) -> bool:
        """
        Add a beneficiary to Canara Bank netbanking.

        Handles two cases:
        - other_bank=True  → Domestic transfer: fills name, nick name, account no,
                              account type, IFSC code
        - other_bank=False → Intra-bank transfer: fills account no and nick name only

        Transaction password is read from login_details["qna"]["account_pin"].
        Nick name is derived from the first 9 chars of the first word of payee name.

        NOTE: This method is incomplete in V1 (no return statement or OTP handling
        at end of method) — migrated as-is from V1.

        V1 location: CanaraTransactionManager.add_beneficiary()
        """
        self.info("Adding beneficiary")
        self.update()
        self.maximize_window()
        payee_details = self.command["beneficiary_details"]
        nick_name = payee_details["name"].split(" ")[0][0:9]
        tx_password = self.login_details["qna"]["account_pin"]

        # Navigate to Beneficiary Maintenance
        self.click_displayed('a[alt="Pay & Transfer"].oj-navigationlist-item-no-icon')
        self.wait_for_element_by_css('a[alt="Beneficiary Maintenance"]', timeout=30)
        self.random_sleep(2, 3)
        self.click_displayed('a[alt="Beneficiary Maintenance"]')
        self.click_displayed('a[alt="Add Beneficiary"]')

        if payee_details["other_bank"]:
            # ── Domestic (other bank) beneficiary ────────────────────────────
            self.wait_for_element_by_css("#DOMESTIC", timeout=30)
            self.click(self.find_by_id("DOMESTIC"))
            self.wait_for_element_by_css('input[id="payeename|input"]', timeout=30)
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id="payeename|input"]'), payee_details["name"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id="nickName|input"]'), nick_name)
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id="accNumber|input"]'), payee_details["account_number"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id="confirmAccNumber|input"]'), payee_details["account_number"])
            self.random_sleep(2, 3)
            self.click(self.find_by_id("oj-select-choice-accountType"))
            self.random_sleep(2, 3)
            btns = self.find_by_css("li.oj-listbox-results-depth-0 > div", multiple=True)
            self.click(btns[1])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id="domSwiftCode|input"]'), payee_details["ifsc_code"])
            self.random_sleep(2, 3)
            self.click(self.find_by_css('oj-button[data-id="addPayee"]'))
        else:
            # ── Intra-bank (same bank) beneficiary ───────────────────────────
            self.wait_for_element_by_css("input.oj-inputpassword-input", timeout=30)
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css("input.oj-inputpassword-input"), payee_details["account_number"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id*="confirm"]'), payee_details["account_number"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[id="nickName|input"]'), nick_name)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('oj-button[data-id="addPayee"]'))
