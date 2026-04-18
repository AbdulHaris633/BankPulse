import re
import json
import hashlib
import time
from multiprocessing import Queue, Value

from selenium.webdriver.common.keys import Keys

from app.transaction.base import TransactionManager
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)


class FederalTransactionManager(TransactionManager):
    """
    Transaction manager for Federal Bank netbanking portal (FedNet).
    URL: https://fednetbank.com/

    Handles both FEDERAL (current account) and FEDERALSAVINGS (savings account)
    — account_type is set based on institution_name from the command.

    Operations supported:
    - login_check  : username (MIS) + password + mobile + image CAPTCHA
    - sync         : scrapes transaction history table, paginates through pages,
                     reports new transactions to server
    - add_beneficiary : federal_add_beneficiary() — uses driver/command params
    - payout          : federal_cashout() — uses driver/command params

    Login credentials:
    - username → login_details["mis_username"]  (overrides base class default)
    - password → login_details["mis_pwd"]        (overrides base class default)
    - mobile   → login_details["mobile_number"]
    - device_id → login_details["device_id"]     (stored as panel_device_id)

    Note on federal_add_beneficiary / federal_cashout:
    These are defined as class methods but written in standalone style
    (take driver and command explicitly, use module-level debug/info/error).
    Migrated as-is from V1.

    V1 location: federal_transaction_manager.py
    V2 location: app/transaction/banks/federal.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = "https://fednetbank.com/"
        # institution_name comes from command — can be "FEDERAL" or "FEDERALSAVINGS"
        self.institution_name: str = self.command["institution_name"]
        # Account type differs between current and savings account variants
        self.account_type: str = "CURRENT"
        if self.institution_name == "FEDERALSAVINGS":
            self.account_type = "SAVINGS"
        self.captcha_image: str = "federal_captcha.png"
        # Federal uses MIS credentials — different field names from other banks
        self.panel_device_id: str = self.login_details["device_id"]
        self.username: str = self.login_details["mis_username"]
        self.password: str = self.login_details["mis_pwd"]

    # ─── CAPTCHA ─────────────────────────────────────────────────────────────

    def solve_captcha(self) -> str:
        """
        Grab the Federal login CAPTCHA image (element ID: IMAGECAPTCHA)
        and solve it via TransactionManager.solve() (2Captcha → AntiCaptcha).
        Returns solved text string, or "" on failure.

        V1 location: FederalTransactionManager.solve_captcha()
        """
        try:
            self.update()
            captcha_b64 = self.find_by_id("IMAGECAPTCHA").screenshot_as_base64
            captcha_text = self.solve(captcha_b64)
            self.update()
            return captcha_text
        except Exception as e:
            self.error(e)
            return ""

    # ─── LOGIN ───────────────────────────────────────────────────────────────

    def login(self) -> bool:
        """
        Login to Federal Bank FedNet portal.

        Steps:
        1. Navigate to portal URL
        2. Click "Personal" tab link
        3. Click the Authentication link for corporate/personal banking
        4. Fill username (MIS), password, mobile number
        5. Solve image CAPTCHA
        6. Submit login form
        7. Wait for profile dropdown to confirm successful login
        8. Scrape account number from the summary table

        V1 location: FederalTransactionManager.login()
        """
        try:
            self.update()
            self.debug("Starting Federal login")
            mobile = self.login_details["mobile_number"]
            self.get(self.url)
            self.random_sleep(5, 8)

            # Click "Personal" tab on the Federal homepage
            self.wait_for_element_by_css('a[href="#personal"]', timeout=30)
            self.click(self.find_by_css('a[href="#personal"]'))
            self.random_sleep(2, 3)

            # Click the authentication link to open the login form
            auth_element = '#personal a[href^="https://www.fednetbank.com/corp/Authentication"]'
            self.wait_for_element_by_css(auth_element, timeout=30)
            self.click(self.find_by_css(auth_element))
            self.random_sleep(2, 3)

            # Fill login form fields
            self.wait_for_element_by_css('input[name="AuthenticationFG.USER_PRINCIPAL"]', timeout=30)
            self.debug("Sending username: " + self.username)
            username_element = 'input[name="AuthenticationFG.USER_PRINCIPAL"]'
            self.send_keys(self.find_by_css(username_element), self.username)
            self.random_sleep(2, 3)

            self.update()
            self.debug("Sending password")
            access_code_element = 'input[name="AuthenticationFG.ACCESS_CODE"]'
            self.send_keys(self.find_by_css(access_code_element), self.password)
            self.random_sleep(2, 3)

            self.debug("Sending mobile: " + mobile)
            mobile_element = 'input[name="AuthenticationFG.MOBILE_NUMBER"]'
            self.send_keys(self.find_by_css(mobile_element), mobile)

            # Solve and submit CAPTCHA
            self.debug("Acquiring CAPTCHA")
            captcha_code = self.solve_captcha()
            self.debug("CAPTCHA code: " + captcha_code)
            verification_code_element = 'input[name="AuthenticationFG.VERIFICATION_CODE"]'
            self.send_keys(self.find_by_css(verification_code_element), captcha_code)
            self.random_sleep(2, 3)

            self.debug("Submitting login form")
            self.update()
            self.click(self.find_by_css('button[type="submit"]'))
            self.random_sleep(5, 8)

            # Wait for profile dropdown — confirms successful login
            self.wait_for_element_by_css("span.profile-dropdown-details", timeout=30)

            # Scrape account number from the summary table
            account_no_element = "tbody.summary-table-tbody > tr > td"
            account_no = self.find_by_css(account_no_element).text.strip()
            self.update()
            if not account_no:
                raise Exception("Account No. not found")
            self.account_no = account_no
            self.debug("Account No.: " + self.account_no)
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── LOGOUT ──────────────────────────────────────────────────────────────

    def logout(self) -> bool:
        """
        Logout from Federal Bank FedNet:
        1. Click the profile dropdown
        2. Click the Logout link (ID: HREF_Logout)

        V1 location: FederalTransactionManager.logout()
        """
        try:
            self.debug("Starting Federal logout")
            self.update()
            self.random_sleep(2, 3)
            self.click(self.find_by_css("li.dropdown.profile-dropdown"))
            self.random_sleep(2, 3)
            self.click(self.find_by_id("HREF_Logout"))
            self.random_sleep(5, 8)
            self.debug("Federal successful log out")
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── SYNC HELPERS ────────────────────────────────────────────────────────

    def open_recent_transactions(self) -> bool:
        """
        Navigate to the Federal transaction history page.

        Steps:
        1. Click Accounts menu item
        2. Click Operative Accounts sub-menu
        3. Click Transaction History button
        4. Click "Last Two Months" to load recent transactions

        Called once before the sync loop starts, and the sync loop refreshes
        the page each iteration by clicking "Last Two Months" again.

        V1 location: FederalTransactionManager.open_recent_transactions()
        """
        try:
            self.update()
            self.debug("Opening Federal recent transaction list")
            self.click(self.find_by_id("Accounts"))
            self.random_sleep(2, 3)
            self.click(self.find_by_id("Accounts-Info_Operative-Accounts"))
            self.wait_for_element_by_css("#txnHistoryBtn", timeout=30)
            self.random_sleep(2, 3)
            self.debug("Clicking transaction history button")
            self.click(self.find_by_id("txnHistoryBtn"))
            self.wait_for_element_by_css('button[name="Action.SEARCH"]', timeout=30)
            self.random_sleep(2, 3)
            self.debug("Click last two months button")
            last_months_selector = 'a[title="Click here for Last Two Months Transactions"]'
            self.click(self.find_by_css(last_months_selector))
            self.wait_for_element_by_css("#TransactionsMade", timeout=30)
            self.random_sleep(2, 3)
            self.debug("Successfully opened Federal transactions")
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── SYNC ────────────────────────────────────────────────────────────────

    def sync_transactions(self) -> bool:
        """
        Continuous sync loop for Federal Bank — scrapes paginated transaction
        history table and reports new transactions to the server.

        Differences from Kotak/Canara:
        - Opens transaction list ONCE before the loop (not each iteration)
        - Each iteration refreshes by clicking "Last Two Months" after sleeping
        - Paginates using ul.pagination — reads total pages from "Page X of N" text
        - Extracts 10 fields per transaction row (date, id, desc, value_date,
          tran_type, cheq_no, type, amount, balance)
        - Dr. = debit (credit_tx=0), everything else = credit (credit_tx=1)

        V1 location: FederalTransactionManager.sync_transactions()
        """
        try:
            self.update()
            self.debug("Starting transaction sync")
            # Open transaction list once — refreshed each loop iteration below
            if not self.open_recent_transactions():
                raise Exception("Could not open transaction list")

            while True:
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

                # Parse total page count from "Page X of N" pagination text
                self.wait_for_element_by_css("ul.pagination", timeout=30)
                self.random_sleep(2, 3)
                self.debug("Getting page number")
                pagination_text = self.find_by_css("ul.pagination").text.strip()
                page_number = int(
                    re.match(r"^Page \d of (\d+)$", pagination_text).group(1)
                )
                self.debug("Page number: " + str(page_number))

                # ── Paginated transaction scraping ────────────────────────────
                for i in range(0, page_number):
                    if latest_tx_found:
                        break
                    new_transactions = []
                    # Selects all transaction rows excluding the header row
                    tx_regex = 'tr[class^="list"]:not(.table-header)'
                    transactions = self.find_by_css(tx_regex, multiple=True)
                    if transactions:
                        self.debug("Transactions list found")

                    for transaction in transactions:
                        self.update()
                        transaction_data = self.find_by_css("td", multiple=True, parent=transaction)
                        tx_date = transaction_data[1].text.strip()
                        tx_id = transaction_data[2].text.strip()
                        tx_desc = transaction_data[3].text.strip()
                        tx_value_date = transaction_data[4].text.strip()
                        tx_tran_type = transaction_data[5].text.strip()
                        tx_cheq_no = transaction_data[6].text.strip()
                        tx_type = transaction_data[7].text.strip()
                        tx_amount = str(
                            float(transaction_data[8].text.replace(",", "").strip())
                        )
                        total_available_balance = str(
                            float(transaction_data[9].text.replace(",", "").strip())
                        )
                        # "Dr." = debit transaction; anything else = credit
                        credit_tx = 0 if tx_type == "Dr." else 1

                        tx_info = {
                            "tx_date": tx_date,
                            "tx_id": tx_id,
                            "tx_desc": tx_desc,
                            "tx_value_date": tx_value_date,
                            "tx_tran_type": tx_tran_type,
                            "tx_cheq_no": tx_cheq_no,
                            "tx_type": tx_type,
                            "tx_amount": tx_amount,
                            "total_available_balance": total_available_balance,
                        }
                        tx_info_hash_str = json.dumps(tx_info, separators=(",", ":"))
                        tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()

                        # Stop if we've reached the previously synced transaction
                        if tx_hash == sync_mode["command"]["latest_tx_hash"]:
                            self.debug("Latest tx_hash found: " + tx_hash)
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
                            "total_available_balance": total_available_balance,
                            "previous_hash": "",
                            "tx_info": tx_info,
                            "tx_hash": tx_hash,
                            "new_state_hash": "",
                        }
                        self.debug("New transaction: " + str(transaction_json))
                        new_transactions.append(transaction_json)

                        # Last transaction on last page — stop pagination
                        if transaction == transactions[len(transactions) - 1]:
                            self.debug("Last transaction in the list, exiting")
                            latest_tx_found = True

                    # Navigate to next page (not on last page)
                    if not i == (page_number - 1):
                        self.debug("Click next page button")
                        self.random_sleep(2, 3)
                        next_btn_selector = "ul.pagination > li:nth-of-type(3) > span"
                        next_page_btn = self.find_by_css(next_btn_selector)
                        # Scroll into view before clicking to avoid interception
                        self.driver.execute_script("arguments[0].scrollIntoView();", next_page_btn)
                        self.random_sleep(2, 3)
                        self.click(next_page_btn)
                        self.wait_for_element_by_css("#TransactionsMade", timeout=30)
                        self.random_sleep(2, 3)
                        self.debug("Finished clicking next page")

                # ── Report new transactions ───────────────────────────────────
                if new_transactions:
                    self.update()
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
                    latest_tx_hash = sync_mode["command"]["latest_tx_hash"]
                    self.data["query"] = "sync"
                    self.data["action"] = "reached_tip"
                    self.data.update({"latest_tx_hash": latest_tx_hash})
                    if not report_sync_status(self.data, True):
                        raise Exception("Could not report sync status")

                # Sleep then refresh by clicking "Last Two Months" again
                self.debug("Sleeping for: " + str(refresh_time))
                time.sleep(refresh_time)
                self.debug("Refreshing")
                last_months_selector = 'a[title="Click here for Last Two Months Transactions"]'
                self.click(self.find_by_css(last_months_selector))
                self.wait_for_element_by_css("#TransactionsMade", timeout=30)
                self.random_sleep(2, 3)
                self.debug("Finished refresh")

        except Exception as e:
            self.error(e)
            return False

    # ─── ADD BENEFICIARY ─────────────────────────────────────────────────────

    def federal_add_beneficiary(self) -> bool:
        """
        Add a beneficiary to Federal Bank FedNet.

        Handles two cases:
        - other_bank=True  → IMPS/NEFT: fills name, account no, account type,
                              IFSC code, then submits with transaction password
        - other_bank=False → Intra-bank: skips IFSC/account type fields

        After submission, checks for failure message on the confirmation page.

        V1 location: FederalTransactionManager.federal_add_beneficiary()
        V2 changes:
        - Converted from standalone (driver, command) to class method (self)
        - driver.find_element → self.find_by_css / self.find_by_id
        - wait_for_element(driver, css) → self.wait_for_element_by_css(css, timeout=30)
        - module-level debug/info/error → self.debug/self.info/self.error
        - command → self.command
        """
        try:
            self.debug("Starting Federal add beneficiary")
            payee_details = self.command["beneficiary_details"]
            self.click(self.find_by_id("Beneficiaries"))
            self.random_sleep(2, 3)
            self.click(self.find_by_css("#child-Manage-Beneficiary > div > div", multiple=True)[0])
            self.random_sleep(5, 8)
            self.wait_for_element_by_css('input[title="Branch"] ~ label', timeout=30)
            self.random_sleep(2, 3)

            self.debug("Entering name and account number")
            self.send_keys(self.find_by_css('input[title="Name"]'), payee_details["name"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[title="Nickname"]'), payee_details["name"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[title="Account Number"]'), payee_details["account_number"])
            self.random_sleep(2, 3)
            self.send_keys(self.find_by_css('input[title="Confirm Account Number"]'), payee_details["account_number"])
            self.random_sleep(2, 3)

            if payee_details["other_bank"]:
                self.debug("Starting other bank flow")
                self.click(self.find_by_css('input[title="Branch"] ~ label'))
                self.click(self.find_by_id("InputFormCollapsible.Re13.C2"))
                self.random_sleep(2, 3)
                search_input = self.find_by_css("#select2-drop div.select2-search > input")
                self.send_keys(search_input, "Current")
                self.random_sleep(2, 3)
                self.send_keys(search_input, Keys.ENTER)
                self.random_sleep(2, 3)
                self.debug("Entering IFSC code: " + str(payee_details["ifsc_code"]))
                self.send_keys(self.find_by_css('input[title="Bank Identifier"]'), payee_details["ifsc_code"])
                self.random_sleep(2, 3)
            else:
                self.debug("Same bank flow")

            self.debug("Clicking submit")
            self.click(self.find_by_css('input[type="Submit"]'))
            self.random_sleep(5, 8)
            self.wait_for_element_by_css('input[title="Transaction Password"]', timeout=30)
            transaction_pass = self.login_details["transaction_pass"]
            self.debug("Entering transaction pass: " + transaction_pass)
            self.send_keys(self.find_by_css('input[title="Transaction Password"]'), transaction_pass)
            self.random_sleep(2, 3)
            self.debug("Clicking submit")
            self.click(self.find_by_css("#SUBMIT"))
            self.random_sleep(5, 8)
            self.wait_for_element_by_css("#DispForm", timeout=30)

            # Check for failure message on confirmation page
            failure_msg = r"unsuccessful attempt\(s\) for transaction"
            if re.findall(failure_msg, self.driver.page_source, re.I | re.M):
                raise Exception("Federal beneficiary could not be added")

            self.info("Federal beneficiary successfully added")
            return True
        except Exception as e:
            self.error(e)
            return False

    # ─── PAYOUT ──────────────────────────────────────────────────────────────

    def federal_cashout(self) -> bool:
        """
        Complete a fund transfer (cashout/payout) on Federal Bank FedNet.

        Flow:
        1. Click Transfer Funds menu
        2. Click Other Bank Accounts → IMPS using Account Number + IFSC
        3. Select beneficiary from dropdown by matching name
        4. Enter amount, select debit account, enter remarks
        5. Submit the transfer form

        V1 location: FederalTransactionManager.federal_cashout()
        V2 changes:
        - Converted from standalone (driver, command) to class method (self)
        - driver.find_element → self.find_by_css / self.find_by_id
        - wait_for_element(driver, css) → self.wait_for_element_by_css(css, timeout=30)
        - command → self.command
        - print(e) → self.error(e)
        """
        try:
            payout_details = self.command["JSON_PAYOUT_DETAILS"]
            self.click(self.find_by_id("Transfer_Funds"))
            self.random_sleep(2, 3)
            self.click(self.find_by_id("Other-Bank-Accounts"))
            self.random_sleep(2, 3)
            self.click(self.find_by_id("Other-Bank-Accounts_IMPS-using-Account-Number--IFSC-247"))
            self.random_sleep(5, 8)
            self.wait_for_element_by_css('input[title="paymentAmount"]', timeout=30)

            # Select beneficiary from dropdown by exact name match
            beneficiary_name_input = self.find_by_id("InputForm.Ra1.C2")
            self.click(self.find_by_css("span.select2-chosen", parent=beneficiary_name_input))
            self.random_sleep(2, 3)
            beneficiary_name_options = self.find_by_css("#select2-drop li", multiple=True)
            beneficiary_name = self.command["beneficiary_name"].lower().strip()
            beneficiary_found = 0
            for beneficiary_name_option in beneficiary_name_options:
                if beneficiary_name_option.text.lower().strip() == beneficiary_name:
                    self.click(beneficiary_name_option)
                    beneficiary_found = 1
                    self.random_sleep(2, 3)
            if not beneficiary_found:
                raise Exception("Beneficiary not found: " + beneficiary_name)

            self.send_keys(self.find_by_css('input[title="paymentAmount"]'), payout_details["amount"])
            self.random_sleep(2, 3)

            # Select debit account (skip "Select" placeholder option)
            debit_account_input = self.find_by_id("InputForm.Ra3.C2")
            self.click(self.find_by_css("span.select2-chosen", parent=debit_account_input))
            self.random_sleep(2, 3)
            debit_account_options = self.find_by_css("#select2-drop li", multiple=True)
            if len(debit_account_options) > 2:
                raise Exception("Number of debit accounts invalid")
            for debit_account_option in debit_account_options:
                if debit_account_option.text.lower().strip() != "select":
                    self.click(debit_account_option)
                    self.random_sleep(2, 3)

            self.send_keys(self.find_by_css('input[title="remarks"]'), payout_details["remarks"])
            self.random_sleep(2, 3)
            self.click(self.find_by_css('input[type="submit"]'))
            return True
        except Exception as e:
            self.error(e)
            return False
