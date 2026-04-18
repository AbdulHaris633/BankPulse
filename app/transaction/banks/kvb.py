import csv
import re
import os
import json
import hashlib
import time
from datetime import datetime, timedelta
from multiprocessing import Queue, Value

from selenium.common.exceptions import WebDriverException

from app.transaction.base import TransactionManager
from app.core.settings import Settings
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
    get_otp,
)

_settings = Settings()


class KVBTransactionManager(TransactionManager):
    """
    Transaction manager for Karur Vysya Bank (KVB) netbanking portal.
    URL: https://www.kvb.co.in/ilogin/

    Handles KVB (current account) and KVBSAVINGS (savings account).
    account_no is read directly from login_details["account_number"] —
    not scraped from the portal after login.

    Operations supported:
    - login_check    : username + password + image CAPTCHA (#TuringImage),
                       30s wait for CAPTCHA to load, frame-based navigation,
                       account balance scraped from #graphtable after login
    - sync           : opens CSV statement (Last 'n' Days / Two Months),
                       parses with csv.reader, reports new transactions;
                       calls scrape_balance() at sync start
    - scrape_balance : navigates to account summary via frame_menu → RRASMlink,
                       reads balance from #graphtable
    - add_beneficiary: NEFT/IFB beneficiary creation with IFSC bank search
                       popup window; OTP confirmation
                       NOTE: incomplete in V1 — no success check or return value

    Statement parsing (CSV):
    - col[0]=date (DD-MM-YYYY HH:MM:SS), col[1]=value_date, col[2]=branch_code,
      col[3]=ref_no, col[4]=remarks, col[5]=debit, col[6]=credit, col[7]=balance
    - Rows without a valid date pattern in col[0] are skipped

    V1 location: kvb_transaction_manager.py
    V2 location: app/transaction/banks/kvb.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - download_dir → _settings.DOWNLOAD_DIR
    - Triple-quoted commented blocks converted to # comments
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = "https://www.kvb.co.in/ilogin/"
        self.institution_name: str = self.command["institution_name"]
        self.account_type: str = "CURRENT"
        if self.institution_name == "KVBSAVINGS":
            self.account_type = "SAVINGS"
        self.balance: float = 0
        # account_no read directly from login_details — not scraped after login
        self.account_no = self.command["login_details"]["account_number"]
        self.reached_tip: bool = False

    def login(self) -> bool:
        username = 'input[name="fldLoginUserId"]'
        password = 'input[type="password"]'
        try:
            for i in range(0, 5):
                try:
                    self.debug("Starting login")
                    self.update()
                    self.get(self.url)
                    self.wait_for_element_by_css('#ilogin', timeout=30)
                    self.random_sleep(2, 3)
                    login_uri = self.find_by_id('ilogin').get_attribute('href')
                    self.get(login_uri)
                    self.random_sleep(5, 8)
                    self.wait_for_element_by_css(username, timeout=30)
                    self.maximize_window()
                    self.random_sleep(5, 8)
                    self.debug("Sending account info")
                    self.send_keys(self.find_by_css(username), self.username)
                    self.random_sleep(2, 3)
                    self.send_keys(self.find_by_css(password), self.password)
                    time.sleep(30)
                    self.random_sleep(5, 8)
                    captcha_b64 = self.find_by_id('TuringImage').screenshot_as_base64
                    captcha_solved = False
                    self.update()
                    captcha_text = ""
                    for r in range(0, 5):
                        captcha_text = self.solve(captcha_b64)
                        self.update()
                        if captcha_text and not str(captcha_text) == '0':
                            captcha_solved = True
                            break
                        self.random_sleep(5, 8)
                        captcha_b64 = self.find_by_id('TuringImage').screenshot_as_base64
                        self.random_sleep(5, 8)
                    if not captcha_solved:
                        raise Exception("Could not solve captcha after 5 attempts")
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    self.send_keys(self.find_by_id('fldcaptcha'), captcha_text)
                    self.debug("Captcha solved")
                    break
                except WebDriverException as ex:
                    self.warn(ex)
                    self.warn("Webdriver exception during login")
            self.random_sleep(2, 3)
            self.click(self.find_by_css('div[onclick="return fLogon()"]'))
            self.random_sleep(2, 3)
            self.update()
            if len(self.driver.window_handles) > 1:
                self.debug("Switching windows")
                self.driver.close()
                self.random_sleep(2, 3)
                self.driver.switch_to.window(self.driver.window_handles.pop())
            self.random_sleep(2, 3)
            self.maximize_window()
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            if not self.wait_for_element_by_css('frame[name="frame_txn"]', timeout=30):
                if re.search("Invalid captcha", self.driver.page_source, re.I | re.M):
                    self.debug("Invalid captcha, retrying")
                    return self.login()
            self.random_sleep(5, 8)
            self.debug("Scraping account info")
            frame = self.find_by_css('frame[name="frame_txn"]')
            self.driver.switch_to.frame(frame)
            self.wait_for_element_by_css('#graphtable tr', timeout=30)
            self.random_sleep(2, 3)
            account_table_rows = self.find_by_css('#graphtable tr', multiple=True)
            account_found = False
            self.update()
            for row in account_table_rows:
                if not self.find_by_css('span[title]', multiple=True, parent=row):
                    continue
                account_data = self.find_by_tag('td', multiple=True, parent=row)
                account_desc = account_data[1].text.strip()
                account_no = account_desc.split('-')[0].strip()
                if not account_no:
                    raise Exception("Could not find account no.")
                balance = account_data[3].text.replace(',', '').strip()
                if not re.search(str(self.account_no), str(account_no)):
                    continue
                self.balance = balance
                account_found = True
                break
            if not account_found:
                raise Exception("Could not find account")
            self.debug("Account no. found: " + str(self.account_no))
            self.driver.switch_to.parent_frame()
            self.update()
            self.info("Login success")
            return True
        except Exception as e:
            self.error(e)
            return False

    def logout(self) -> bool:
        try:
            self.debug("Starting logout")
            self.update()
            self.maximize_window()
            self.driver.switch_to.parent_frame()
            frame = self.find_by_css('frame[name="frame_top"]')
            self.driver.switch_to.frame(frame)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('a[onclick="fireLogOut ()"]'))
            time.sleep(30)
            self.update()
            self.debug("Logout success")
            return True
        except Exception as e:
            self.error(e)
            return False

    def open_statement(self) -> bool:
        """
        Navigate to account statement via frame_menu → RRAAClink, select the
        account number, choose date range (Last 'n' Days or Two Months for full
        sync), and submit.

        Date range options:
        - Normal sync : "Last 'n' Days" with fldnooftxn=2
        - Full sync   : "Two Months"

        Alternate date range "Specify Period" (commented out — not currently used):
        # date_range_filter = "Specify Period"
        # Use date picker anchors (anchor / anchor1) to select yesterday→today.
        """
        try:
            self.debug("Opening statement")
            self.update()
            self.driver.switch_to.parent_frame()
            frame = self.find_by_css('frame[name="frame_menu"]')
            self.random_sleep(2, 3)
            self.driver.switch_to.frame(frame)
            self.maximize_window()
            self.random_sleep(2, 3)
            self.debug("Clicking nav link")
            self.click(self.find_by_css('#menuContainer2 #RRAAClink'))
            self.driver.switch_to.parent_frame()
            self.wait_for_element_by_css('frame[name="frame_txn"]', timeout=30)
            self.random_sleep(2, 3)
            frame = self.find_by_css('frame[name="frame_txn"]')
            self.driver.switch_to.frame(frame)
            self.wait_for_element_by_css('select[name="fldacctno"] option', timeout=30)
            self.maximize_window()
            self.random_sleep(2, 3)
            self.update()
            self.debug("Selecting account no.")
            account_options = self.find_by_css('select[name="fldacctno"] option', multiple=True)
            account_found = False
            for option in account_options:
                if re.search(self.account_no, option.text):
                    self.click(option)
                    self.random_sleep(2, 3)
                    account_found = True
                    break
            if not account_found:
                raise Exception("Could not find account")
            self.debug("Selecting date range")
            date_range_options = self.find_by_css('select[name="fldsearch"] option', multiple=True)
            # date_range_filter = "Specify Period"  # alternative: use date pickers
            date_range_filter = "Last 'n' Days"
            if self.do_full_sync:
                date_range_filter = "Two Months"
            self.maximize_window()
            for option in date_range_options:
                if re.search(date_range_filter, option.text, re.I):
                    self.click(option)
                    self.random_sleep(1.5, 4)
                    break

            # Select dates
            if not self.do_full_sync:
                self.send_keys(self.find_by_id('fldnooftxn'), 2)
                # Alternative: use "Specify Period" with date picker UI
                # today = datetime.today().day
                # day_delta = timedelta(days=1)
                # yesterday = (datetime.now() - day_delta).day
                # self.driver.find_element(By.ID, 'anchor').click()
                # small_sleep()
                # ui_dates = self.driver.find_elements(By.CSS_SELECTOR, 'a.ui-state-default')
                # for ui_date in ui_dates:
                #     if ui_date.text.strip() == str(yesterday):
                #         ui_date.click()
                #         small_sleep()
                #         break
                # self.driver.find_element(By.ID, 'anchor1').click()
                # small_sleep()
                # ui_dates = self.driver.find_elements(By.CSS_SELECTOR, 'a.ui-state-default')
                # for ui_date in ui_dates:
                #     if ui_date.text.strip() == str(today):
                #         ui_date.click()
                #         small_sleep()
                #         break
            else:
                self.do_full_sync = False

            # self.driver.find_element(By.ID, 'fldnooftxn').send_keys('200')
            self.maximize_window()
            self.random_sleep(2, 3)
            self.update()
            self.click(self.find_by_css('input[name="fldsubmit"]'))
            self.debug("Checking for transactions")
            if not self.wait_for_element_by_css('#fldsearchformat', timeout=30):
                self.warn("Could not find download button, checking for reason")
                no_txns_found_text = r'No records found, to search again please amend your inputs'
                if re.search(no_txns_found_text, self.driver.page_source, re.I | re.M):
                    self.debug("No transactions in the selected period")
                    self.report_no_transactions_found()
                else:
                    raise Exception("Could not determine reason")
            self.random_sleep(2, 3)
            self.update()
            self.debug("Statement opened")
            return True
        except Exception as e:
            self.error(e)
            return False

    def download_statement(self) -> bool:
        """
        Select CSV format (value="05") and click download.
        Waits up to 5 × medium_sleep for the file to appear.
        """
        try:
            self.debug("Downloading statement")
            self.random_sleep(2, 3)
            self.click(self.find_by_css('#fldsearchformat option[value="05"]'))
            self.random_sleep(2, 3)
            self.click(self.find_by_id('flddownload'))
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            for i in range(0, 5):
                self.update()
                if self.get_most_recent_download():
                    break
                self.random_sleep(5, 8)
            self.debug("Statement downloaded")
            return True
        except Exception as e:
            self.error(e)
            return False

    def parse_statement(self) -> list:
        """
        Parse the most recently downloaded CSV statement.
        Column layout: col[0]=date (DD-MM-YYYY HH:MM:SS), col[1]=value_date,
        col[2]=branch_code, col[3]=ref_no, col[4]=remarks,
        col[5]=debit, col[6]=credit, col[7]=balance.

        Validation notes:
        - Rows without a valid timestamp in col[0] are skipped (header rows)
        - value_date must match DD-Mon-YYYY pattern
        - branch_code must be digits only
        # Skipping rows where len(row) != 8 — disabled, col count can vary
        # Skipping rows where len(tx_remarks) < 5 — disabled
        """
        try:
            self.debug("Parsing transactions")
            self.update()
            statement_file_path = _settings.DOWNLOAD_DIR + os.sep + self.get_most_recent_download()
            transactions = []
            statement_fp = open(statement_file_path, 'r')
            reader = csv.reader(statement_fp)
            tx_date_regex = r'^\s*\d\d-\d\d-\d\d\d\d\s+\d\d:\d\d:\d\d\s*$'
            tx_value_date_regex = r'^\s*\d\d-[a-zA-Z]+-\d\d\d\d\s*$'
            tx_branch_code_regex = r'^\s*\d+\s*$'
            tx_ref_no_regex = r'^\s*\d+\s*$'
            for row in reader:
                try:
                    if not row:
                        continue
                    tx_date_col_data = str(row[0]).strip()
                    if not re.match(tx_date_regex, tx_date_col_data):
                        continue
                    # if not len(row) == 8:
                    #     self.warn("PARSE TX ERROR: length is not 8")
                    #     continue
                    tx_date = tx_date_col_data
                    if not re.match(tx_value_date_regex, row[1]):
                        self.warn("PARSE TX ERROR: tx_date does not match regex")
                        continue
                    tx_value_date = str(row[1]).strip()
                    if not re.match(tx_branch_code_regex, row[2]):
                        self.warn("PARSE TX ERROR: tx_value_date does not match regex")
                        continue
                    tx_branch_code = str(row[2]).strip()
                    tx_ref_no = str(row[3]).strip()
                    tx_remarks = str(row[4]).strip()
                    # if not len(tx_remarks) > 5:
                    #     self.warn("PARSE TX ERROR: tx_remarks length is less than 5")
                    #     continue
                    tx_debit = str(row[5]).replace(',', '').strip()
                    tx_credit = str(row[6]).replace(',', '').strip()
                    total_available_balance = str(row[7]).replace(',', '').strip()
                    if tx_debit:
                        credit_tx = 0
                        tx_amount = tx_debit
                    else:
                        credit_tx = 1
                        tx_amount = tx_credit
                    try:
                        float(tx_amount)
                        float(total_available_balance)
                    except Exception as e:
                        self.warn(e)
                        self.warn("Could not parse transaction: " + str(row))
                        continue
                    tx_data = {
                        "tx_date": tx_date,
                        "tx_value_date": tx_value_date,
                        "tx_branch_code": tx_branch_code,
                        "tx_ref_no": tx_ref_no,
                        "tx_remarks": tx_remarks,
                        "tx_debit": tx_debit,
                        "tx_credit": tx_credit,
                        "credit_tx": credit_tx,
                        "tx_amount": tx_amount,
                        "total_available_balance": total_available_balance,
                    }
                    transactions.append(tx_data)
                except Exception as e:
                    self.warn("Could not parse transaction: " + str(row))
                    self.warn(e)
            self.debug("No. of transactions found: " + str(len(transactions)))
            self.debug("Transactions parsed")
            return transactions
        except Exception as e:
            self.error(e)
            return []

    def download_and_parse_statement(self) -> list:
        try:
            if not self.download_statement():
                raise Exception("Could not download statement")
            return self.parse_statement()
        except Exception as e:
            self.error(e)
            return []

    def sync_transactions(self) -> bool:
        try:
            self.info("Starting transaction sync")
            # Scrape and report balance at sync start
            self.scrape_balance()
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
                            raise Exception("Could not logout of KVB, exiting sync")
                        if not report_sync_status(self.data, True):
                            raise Exception("Could not report sync status")
                        self.info("Sync finished successfully")
                        return True
                    previous_tx_hash = sync_mode_command["previous_hash"]
                    refresh_time = sync_mode_command["refresh_time"]
                    while not latest_tx_found:
                        self.update()
                        self.debug("Starting transaction search")
                        if not self.open_statement():
                            raise Exception("Could not open recent transaction list")

                        # Loop through all transactions
                        transactions = self.download_and_parse_statement()
                        for transaction in transactions:
                            try:
                                self.update()
                                tx_info = transaction
                                tx_info_hash_str = json.dumps(tx_info, separators=(',', ':'))
                                tx_hash = hashlib.sha256(tx_info_hash_str.encode()).hexdigest()
                                if tx_hash == sync_mode_command["latest_tx_hash"]:
                                    self.info("Latest tx_hash found: " + tx_hash)
                                    latest_tx_found = True
                                    self.reached_tip = True
                                    break
                                transaction_json = {
                                    "instruction_id": self.instruction_id,
                                    "query": "sync",
                                    "action": "sync_tx",
                                    "success": 0,
                                    "trader_id": self.trader_id,
                                    "bot_id": self.bot_id,
                                    "credit_tx": tx_info["credit_tx"],
                                    "tx_amount": tx_info["tx_amount"],
                                    "tx_desc": tx_info["tx_remarks"],
                                    "total_available_balance": tx_info["total_available_balance"],
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
                                self.info("Last transaction, exiting")
                                latest_tx_found = True
                                self.reached_tip = True
                                break

                    # Send new transactions to server with hash chaining
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
                    self.random_sleep(1.5, 4)
                    if not self.open_statement():
                        self.warn("Could not switch tabs")
                    self.random_sleep(1.5, 4)
        except Exception as e:
            self.error(e)
            return False

    def scrape_balance(self) -> None:
        try:
            self.debug("Scraping balance")
            self.random_sleep(2, 3)
            self.maximize_window()
            self.driver.switch_to.parent_frame()
            frame = self.find_by_css('frame[name="frame_menu"]')
            self.driver.switch_to.frame(frame)
            self.click(self.find_by_id('RRASMlink'))
            self.driver.switch_to.parent_frame()
            self.wait_for_element_by_css('frame[name="frame_txn"]', timeout=30)
            self.random_sleep(2, 3)
            self.debug("Scraping account info")
            frame = self.find_by_css('frame[name="frame_txn"]')
            self.driver.switch_to.frame(frame)
            self.wait_for_element_by_css('#graphtable tr', timeout=30)
            self.random_sleep(2, 3)
            account_table_rows = self.find_by_css('#graphtable tr', multiple=True)
            account_found = False
            self.update()
            for row in account_table_rows:
                if not self.find_by_css('span[title]', multiple=True, parent=row):
                    continue
                account_data = self.find_by_tag('td', multiple=True, parent=row)
                account_desc = account_data[1].text.strip()
                account_no = account_desc.split('-')[0].strip()
                if not account_no:
                    raise Exception("Could not find account no.")
                balance = account_data[3].text.replace(',', '').strip()
                if not re.search(str(self.account_no), str(account_no)):
                    continue
                self.balance = balance
                account_found = True
                break
            if not account_found:
                raise Exception("Could not find account")
            self.debug("Account no. found: " + str(self.account_no))
            self.driver.switch_to.parent_frame()
            self.update_balance(str(self.balance))
            self.debug("Finished scraping balance")
        except Exception as e:
            self.error(e)

    def add_beneficiary(self) -> bool:
        """
        Add a NEFT (other bank) or IFB (same bank) beneficiary.
        For other bank: opens IFSC lookup popup window, searches by IFSC code,
        selects the bank, then returns to main frame for OTP confirmation.

        NOTE: V1 implementation is incomplete — no success text check or
        explicit return value after submit. Migrated as-is.
        """
        self.info("Adding beneficiary")
        self.update()
        self.maximize_window()
        payee_details = self.command['beneficiary_details']
        nick_name = payee_details['name'].split(' ')[0]
        tx_password = self.login_details["qna"]["account_pin"]
        self.driver.switch_to.parent_frame()
        self.driver.switch_to.parent_frame()
        frame_top = self.find_by_css('frame[name="frame_top"]')
        self.driver.switch_to.frame(frame_top)
        self.click(self.find_by_css('#menu3 a[alt="Payments"]'))
        self.driver.switch_to.parent_frame()
        self.wait_for_element_by_css('frame[name="frame_menu"]', timeout=30)
        frame_menu = self.find_by_css('frame[name="frame_menu"]')
        self.driver.switch_to.frame(frame_menu)
        self.click(self.find_by_id('RRBTGlink'))
        self.driver.switch_to.parent_frame()
        self.wait_for_element_by_css('frame[name="frame_txn"]', timeout=30)
        self.random_sleep(2, 3)
        frame_txn = self.find_by_css('frame[name="frame_txn"]')
        self.driver.switch_to.frame(frame_txn)
        self.wait_for_element_by_css('select[name="fldtransfermode"]', timeout=30)
        self.update()
        self.maximize_window()
        self.random_sleep(2, 3)
        if payee_details['other_bank']:
            self.click(self.find_by_css('select[name="fldtransfermode"] option[value="NFB"]'))
        else:
            self.click(self.find_by_css('select[name="fldtransfermode"] option[value="IFB"]'))
        self.random_sleep(2, 3)
        self.click(self.find_by_css('input[value="Create Beneficiary"]'))
        self.wait_for_element_by_css('#fldbeneid', timeout=30)
        self.random_sleep(2, 3)
        self.send_keys(self.find_by_id('fldbeneid'), nick_name)
        self.random_sleep(2, 3)
        self.send_keys(self.find_by_id('fldname'), payee_details["name"])
        self.random_sleep(2, 3)
        self.send_keys(self.find_by_id('fldbeneaccountno'), payee_details["account_number"])
        self.random_sleep(2, 3)
        self.send_keys(self.find_by_id('fldconfbeneaccountno'), payee_details["account_number"])
        self.random_sleep(2, 3)
        self.click(self.find_by_css('option[value="03"]'))
        self.random_sleep(2, 3)
        self.click(self.find_by_id('fldbenenatnlclrcodanchor'))
        time.sleep(30)
        self.update()
        self.maximize_window()
        if not len(self.driver.window_handles) > 1:
            time.sleep(10)
        current_window = self.driver.current_window_handle
        window_handles = self.driver.window_handles
        for window_handle in window_handles:
            if window_handle == current_window:
                continue
            self.driver.switch_to.window(window_handle)
            if re.search(r"Bank List", self.driver.title, re.I | re.M):
                break
        self.maximize_window()
        self.random_sleep(2, 3)
        self.send_keys(self.find_by_css('input[name="fldbenenatnlclrcod"]'), payee_details["ifsc_code"])
        self.random_sleep(2, 3)
        self.click(self.find_by_css('input[value="Search"]'))
        self.wait_for_element_by_css('input[name="chkBank"]', timeout=30)
        self.random_sleep(2, 3)
        self.click(self.find_by_css('input[name="chkBank"]'))
        self.random_sleep(2, 3)
        self.click(self.find_by_css('input[value="Select Bank"]'))
        self.random_sleep(2, 3)
        self.driver.switch_to.window(current_window)
        self.update()
        self.random_sleep(2, 3)
        frame_txn = self.find_by_css('frame[name="frame_txn"]')
        self.driver.switch_to.frame(frame_txn)
        self.click(self.find_by_id('fldGo'))
        self.wait_for_element_by_css('#fldconfirm', timeout=30)
        self.random_sleep(2, 3)
        self.click(self.find_by_id('fldconfirm'))
        self.wait_for_element_by_css('#fldtxnpin1', timeout=30)
        self.random_sleep(2, 3)
        self.send_keys(self.find_by_id('fldtxnpin1'), tx_password)
        self.random_sleep(2, 3)

        # Fetch OTP
        self.debug("Acquiring OTP")
        self.update()
        otp_response = get_otp(self.command)
        if not otp_response or otp_response["response"] == "NotOk":
            self.warn("Could not fetch OTP, retrying")
            self.click(self.find_by_id('butgenpin'))
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            self.random_sleep(5, 8)
            try:
                self.driver.switch_to.alert.accept()
            except Exception as alert_ex:
                self.warn("Exception thrown accepting alert")
                self.warn(alert_ex)
            self.wait_for_element_by_css('#fldtxnpin1', timeout=30)
            self.random_sleep(5, 8)
            otp_response = get_otp(self.command)
            if not otp_response or otp_response["response"] == "NotOk":
                raise Exception("Could not fetch OTP")
        otp = str(otp_response["otp"])
        self.debug("OTP acquired: " + str(otp))
        self.send_keys(self.find_by_id('otptoken'), otp)
        self.random_sleep(2, 3)

        self.click(self.find_by_css('input[name="fldsubmit"]'))
        # NOTE: V1 ends here — no success check or return statement
