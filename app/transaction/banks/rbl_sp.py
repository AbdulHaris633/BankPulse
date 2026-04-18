import re
import os
import json
import hashlib
import time
from multiprocessing import Queue, Value

import pandas
from selenium.common.exceptions import WebDriverException

from app.transaction.banks.rbl_base import RBLBankTransactionManager
from app.core.settings import Settings
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
)

_settings = Settings()


class RBLSPTransactionManager(RBLBankTransactionManager):
    """
    Transaction manager for RBL Bank SP (Savings/Personal) netbanking portal.
    URL: https://online.rblbank.com/corp/AuthenticationController?...BANK_ID=176

    Inherits shared RBL utilities from RBLBankTransactionManager:
    - rbl_click(), select_date(), wait_for_loading_widget()
    - download_statement(), download_and_parse_statement()

    Operations supported:
    - login_check : username → #STU_VALIDATE_CREDENTIALS → confirm checkbox
                    → password → #VALIDATE_STU_CREDENTIALS_UX;
                    account_no scraped from span[id*="OPR_ACCOUNT_NUMBER"]
    - sync        : downloads XLS statement, parses with pandas, hash chaining
    - scrape_balance : navigates home, reads balance from td.amtRightAlign

    open_recent_transaction_list flow:
    1. Click home button (rbl_click)
    2. Click Account Statement nav link (rbl_click)
    3. Find account row matching self.account_no, click its 'a' link
    4. Click "Click to Expand/Collapse" advanced settings
    5. select_date() — uses 30-day offset on full sync, else today
    6. Click search (rbl_click)
    7. If "The transactions do not exist" → retry with start_offset=1 (yesterday);
       if still none → report_no_transactions_found() + do_full_sync=True

    parse_statement column layout (pandas, Excel):
    - row[1] = tx_date (DD/MM/YYYY — skip if not matching)
    - row[2] = tx_remarks
    - row[3] = tx_ref_no
    - row[5] = tx_value_date
    - row[6] = tx_withdrawal
    - row[7] = tx_deposit
    - row[8] = total_available_balance

    V1 location: rbl_sp_transaction_manager.py
    V2 location: app/transaction/banks/rbl_sp.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - Inherits from RBLBankTransactionManager (app/transaction/banks/rbl_base.py)
    - download_dir → _settings.DOWNLOAD_DIR
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = (
            "https://online.rblbank.com/corp/AuthenticationController"
            "?FORMSGROUP_ID__=AuthenticationFG&__START_TRAN_FLAG__=Y"
            "&__FG_BUTTONS__=LOAD&ACTION.LOAD=Y&AuthenticationFG.LOGIN_FLAG=1"
            "&BANK_ID=176"
        )
        self.institution_name: str = "RBLSP"
        self.reached_tip: bool = False

    def login(self) -> bool:
        try:
            username_input = 'input[id="AuthenticationFG.CUSTOM_USER_PRINCIPAL"]'
            confirm_checkbox = 'input[id="AuthenticationFG.TARGET_CHECKBOX"] ~ span'
            password_input = 'input[id="AuthenticationFG.ACCESS_CODE"]'
            account_no_selector = 'span[id*="OPR_ACCOUNT_NUMBER"]'
            self.debug("Starting login")
            self.update()
            self.get(self.url)
            self.wait_for_element_by_css(username_input, timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Sending username")
            self.send_keys(self.find_by_css(username_input), self.username)
            self.random_sleep(1.5, 4)
            self.update()
            self.click(self.find_by_id('STU_VALIDATE_CREDENTIALS'))
            self.wait_for_element_by_css(confirm_checkbox, timeout=30)
            self.update()
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(confirm_checkbox))
            self.random_sleep(1.5, 4)
            self.debug("Sending password")
            self.send_keys(self.find_by_css(password_input), self.password)
            self.random_sleep(1.5, 4)
            self.update()
            self.click(self.find_by_id('VALIDATE_STU_CREDENTIALS_UX'))
            self.wait_for_element_by_css(account_no_selector, timeout=30)
            self.random_sleep(1.5, 4)
            account_no = self.find_by_css(account_no_selector).text.strip()
            if not account_no:
                raise Exception("Could not scrape account no.")
            self.account_no = account_no
            self.debug("Account no. scraped: " + self.account_no)
            self.info("Login success")
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    def logout(self) -> bool:
        try:
            logout_link = 'span.logoutLink a[title="Log out"]'
            self.debug("Starting logout")
            self.update()
            self.random_sleep(1.5, 4)
            self.click(self.find_by_css(logout_link))
            self.wait_for_element_by_css('#LOG_OUT', timeout=30)
            self.random_sleep(1.5, 4)
            self.update()
            self.click(self.find_by_id('LOG_OUT'))
            self.wait_for_element_by_css('#captionGoToLogin', timeout=30)
            self.random_sleep(1.5, 4)
            self.debug("Logout success")
            self.update()
            return True
        except Exception as e:
            self.error(e)
            return False

    def open_recent_transaction_list(self) -> bool:
        """
        Navigate to Account Statement, select the matching account, expand
        advanced filters, set date range, and click Search.
        Retries up to 5 WebDriverExceptions before raising.

        On "no transactions" after today's search, retries with yesterday
        (start_offset=1). If still none, reports no_transactions and sets
        do_full_sync=True for next cycle.
        """
        account_statement_nav_link = 'div[id*="RetailUser"] a[title="Account Statement"]'
        more_info_btn = 'a[title="Click to Expand/Collapse"]'
        search_btn = 'input[name="Action.SEARCH"]'
        tx_date_checkbox = 'input[title="Select Transaction Date"] ~ span'
        home_btn = 'div.menubar a[name="HREF_Home"]'
        webdriver_errors = 0
        try:
            while True:
                try:
                    self.debug("Opening recent transactions list")
                    self.random_sleep(1.5, 4)
                    self.debug("Clicking home button")
                    self.maximize_window()
                    self.random_sleep(1.5, 4)
                    self.rbl_click(home_btn, account_statement_nav_link)
                    self.random_sleep(1.5, 4)
                    self.update()
                    self.maximize_window()
                    self.debug("Clicking nav link")
                    self.rbl_click(account_statement_nav_link, 'tbody tr')
                    self.random_sleep(1.5, 4)
                    account_no_clicked = False
                    self.debug("Selecting account no.")
                    account_rows = self.find_by_css('tbody tr', multiple=True)
                    for account_row in account_rows:
                        if re.search(self.account_no, account_row.text, re.M):
                            self.rbl_click('a', more_info_btn, account_row)
                            account_no_clicked = True
                            break
                    if not account_no_clicked:
                        raise Exception("Could not click account no.")
                    self.wait_for_element_by_css(more_info_btn, timeout=30)
                    self.random_sleep(1.5, 4)
                    self.random_sleep(1.5, 4)
                    self.maximize_window()
                    self.random_sleep(1.5, 4)
                    self.debug("Clicking advanced settings")
                    self.rbl_click(more_info_btn, tx_date_checkbox)
                    self.wait_for_element_by_css(tx_date_checkbox, timeout=30)
                    self.random_sleep(1.5, 4)
                    self.update()
                    if self.do_full_sync:
                        self.select_date(start_offset=30)
                        self.do_full_sync = False
                    else:
                        self.select_date()

                    # Continue
                    self.debug("Clicking continue")
                    self.maximize_window()
                    self.rbl_click(search_btn, more_info_btn)
                    self.update()
                    self.random_sleep(5, 8)
                    self.wait_for_loading_widget()
                    no_transactions_text = "The transactions do not exist"
                    if re.search(no_transactions_text, self.driver.page_source, re.I | re.M):
                        # Search previous day
                        self.debug("No transactions found, search yesterday")
                        self.rbl_click(more_info_btn, tx_date_checkbox)
                        self.select_date(start_offset=1)
                        self.debug("Clicking continue")
                        self.maximize_window()
                        self.rbl_click(search_btn, more_info_btn)
                        self.update()
                        self.random_sleep(5, 8)
                        self.wait_for_loading_widget()
                        self.random_sleep(5, 8)
                        if re.search(no_transactions_text, self.driver.page_source, re.I | re.M):
                            self.warn("No transactions found")
                            self.report_no_transactions_found()
                            self.do_full_sync = True
                    else:
                        self.debug("Transactions found")
                    break
                except WebDriverException as e:
                    self.update()
                    self.warn(e)
                    self.warn("Webdriver error opening recent transactions list")
                    webdriver_errors += 1
                    if webdriver_errors > 5:
                        raise Exception("Could not open recent transaction list")
                    self.random_sleep(1.5, 4)
            self.debug("Recent transactions list opened")
            return True
        except Exception as e:
            self.error(e)
            return False

    def parse_statement(self) -> list:
        """
        Parse the most recently downloaded XLS statement using pandas.
        Column layout: row[1]=tx_date (DD/MM/YYYY), row[2]=remarks,
        row[3]=ref_no, row[5]=value_date, row[6]=withdrawal,
        row[7]=deposit, row[8]=balance.
        Rows where col[1] is not a string matching DD/MM/YYYY are skipped.
        """
        try:
            self.update()
            self.debug("Parsing statement")
            statement_file_path = _settings.DOWNLOAD_DIR + os.sep + self.get_most_recent_download()
            self.debug("Statement file: " + statement_file_path)
            df = pandas.read_excel(statement_file_path)
            transactions = []
            for row in df.values:
                tx_date_item = row[1]
                if not (tx_date_item and type(tx_date_item) == str):
                    continue
                if not re.match(r'^\d+/\d+/\d+$', tx_date_item):
                    continue
                tx_date = str(row[1]).strip()
                tx_remarks = str(row[2]).strip()
                tx_ref_no = str(row[3]).strip()
                if tx_ref_no == "nan":
                    tx_ref_no = ""
                tx_value_date = str(row[5]).strip()
                tx_withdrawal = str(row[6]).strip()
                if tx_withdrawal == "nan":
                    tx_withdrawal = ""
                tx_deposit = str(row[7]).strip()
                if tx_deposit == "nan":
                    tx_deposit = ""
                total_available_balance = str(row[8]).strip()
                transaction = {
                    "tx_date": tx_date,
                    "tx_remarks": tx_remarks,
                    "tx_ref_no": tx_ref_no,
                    "tx_value_date": tx_value_date,
                    "tx_withdrawal": tx_withdrawal,
                    "tx_deposit": tx_deposit,
                    "total_available_balance": total_available_balance,
                }
                transactions.append(transaction)
            self.update()
            self.debug("Number of transactions: " + str(len(transactions)))
            self.debug("Statement file parsed")
            return transactions
        except Exception as e:
            self.error(e)
            return []

    def sync_transactions(self) -> bool:
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
                        if not self.open_recent_transaction_list():
                            raise WebDriverException("Could not open recent transaction list")

                        # Loop through all transactions
                        transactions = self.download_and_parse_statement()
                        for transaction in transactions:
                            try:
                                self.update()
                                tx_date = transaction["tx_date"].strip()
                                tx_remarks = transaction["tx_remarks"].strip()
                                tx_ref_no = transaction["tx_ref_no"].strip()
                                tx_withdrawal = transaction["tx_withdrawal"].strip()
                                tx_deposit = transaction["tx_deposit"].strip()
                                total_available_balance = transaction["total_available_balance"].strip()
                                if tx_deposit:
                                    credit_tx = 1
                                    tx_amount = tx_deposit
                                else:
                                    credit_tx = 0
                                    tx_amount = tx_withdrawal
                                tx_info = {
                                    "tx_date": tx_date,
                                    "tx_remarks": tx_remarks,
                                    "tx_ref_no": tx_ref_no,
                                    "tx_withdrawal": tx_withdrawal,
                                    "tx_deposit": tx_deposit,
                                    "credit_tx": credit_tx,
                                    "total_available_balance": total_available_balance,
                                }
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
                                    "credit_tx": credit_tx,
                                    "tx_amount": tx_amount,
                                    "tx_desc": tx_remarks,
                                    "total_available_balance": total_available_balance,
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
                            # Check end condition (last transaction)
                            if transaction == transactions[len(transactions) - 1]:
                                self.info("Latest transaction reached")
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
                    if not self.open_recent_transaction_list():
                        self.warn("Could not switch tabs")
                    self.random_sleep(1.5, 4)
        except Exception as e:
            self.error(e)
            return False

    def scrape_balance(self) -> None:
        try:
            self.update()
            self.debug("Scraping balance")
            self.random_sleep(1.5, 4)
            home_btn = 'div.menubar a[name="HREF_Home"]'
            self.click(self.find_by_css(home_btn))
            self.wait_for_element_by_css('tbody tr', timeout=30)
            self.random_sleep(1.5, 4)
            account_rows = self.find_by_css('tbody tr', multiple=True)
            account_balance_found = False
            balance = ""
            self.update()
            for account_row in account_rows:
                if re.search(self.account_no, account_row.text, re.M):
                    ab_text = self.find_by_css('td.amtRightAlign', parent=account_row).text.strip()
                    balance = ab_text.replace('₹', '').replace(',', '').strip()
                    if not balance:
                        raise Exception("Could not find account balance")
                    account_balance_found = True
                    break
            if not account_balance_found:
                raise Exception("Could not find account balance")
            self.debug("Balance found: " + str(balance))
            self.update()
            self.update_balance(balance)
            self.debug("Balance updated")
        except Exception as e:
            self.error(e)
