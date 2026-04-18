import re
import os
import json
import hashlib
import time
from datetime import datetime, timedelta
from multiprocessing import Queue, Value
from xml.dom import minidom

from selenium.common.exceptions import (
    WebDriverException,
    TimeoutException,
    UnexpectedAlertPresentException,
)

from app.transaction.base import TransactionManager
from app.core.settings import Settings
from app.utils.functions import (
    fetch_sync_mode,
    report_sync_status,
    get_otp,
)

_settings = Settings()


class IndianBankTransactionManager(TransactionManager):
    """
    Transaction manager for Indian Bank netbanking portal.
    URL: https://www.indianbank.net.in/jsp/startIB.jsp

    Handles INDIANBANKCURRENT and INDIANBANKSAVINGS institution types.
    account_no is scraped from the portal after login and used throughout
    for account selection in statements, payout, and balance pages.

    Operations supported:
    - login_check    : username (#uid) + image CAPTCHA + password (#pwd)
                       with iframe switching and account number scraping
    - sync           : downloads XML statement (today / last 30 days for
                       full sync), parses with minidom, reports new txns
    - add_beneficiary: same bank and other bank (NEFT/IMPS) flows, OTP
    - payout         : other bank (IMPS247) or same bank fund transfer, OTP
    - instant_payout : InstaPay flow, OTP
    - scrape_balance : navigates to Account Summary, scrapes current balance

    V1 location: indian_bank_transaction_manager.py
    V2 location: app/transaction/banks/indian_bank.py

    Changes from V1:
    - driver_manager parameter removed from __init__
    - super().__init__() updated — no driver_manager
    - download_dir → _settings.DOWNLOAD_DIR
    - All bank logic unchanged
    """

    def __init__(self, command: dict, child_status: Queue = None, update_flag: Value = None):
        # driver_manager removed — TransactionManager creates AdsPowerAPI internally
        super().__init__(command, child_status, update_flag)
        self.url: str = "https://www.indianbank.net.in/jsp/startIB.jsp"
        self.institution_name: str = command["institution_name"]
        self.reached_tip: bool = False

    def login(self) -> bool:
        try:
            password_input_displayed = False
            for i in range(0, 3):
                try:
                    self.update()
                    self.debug("Starting login")
                    self.get(self.url)
                    self.maximize_window()
                    self.wait_for_element_by_css('#uid', timeout=30)
                    self.random_sleep(2, 3)
                    self.debug("Sending username")
                    self.send_keys(self.find_by_id('uid'), self.username)
                    self.random_sleep(2, 3)
                    self.update()
                    if not self.solve_captcha():
                        raise Exception("Could not solve CAPTCHA")
                    self.debug("Clicking proceed")
                    self.click(self.find_by_css('a.l_login_button'))
                    self.wait_for_element_by_css('#pwd', timeout=30)
                    self.random_sleep(5, 8)
                    pwd_input = self.find_by_id('pwd')
                    if not pwd_input.is_displayed():
                        self.warn("Password element is not displayed, retrying")
                        self.maximize_window()
                        self.random_sleep(2, 3)
                        self.click(self.find_by_id('uid'))
                        self.random_sleep(2, 3)
                        self.click(self.find_by_css('a.l_login_button'))
                        self.wait_for_element_by_css('#pwd', timeout=30)
                        self.random_sleep(5, 8)
                        self.random_sleep(5, 8)
                    if pwd_input.is_displayed():
                        self.debug("Password input is displayed, continuing")
                        password_input_displayed = True
                        break
                except Exception as login_ex:
                    self.warn("Exception in login")
                    self.warn(login_ex)
                    self.warn("Retrying")
            if not password_input_displayed:
                raise Exception("Could not send password")
            self.debug("Sending password")
            self.send_keys(self.find_by_id('pwd'), self.password)
            self.random_sleep(2, 3)
            self.update()
            self.click(self.find_by_css('a.pass_button'))
            try:
                self.wait_for_element_by_css('iframe[name="main_content"]', timeout=30)
            except UnexpectedAlertPresentException as alert:
                self.warn("Login failure, unexpected alert")
                try:
                    self.warn("Alert found: " + alert.alert_text)
                    if re.search(r"Please enter password", alert.alert_text):
                        self.maximize_window()
                        self.random_sleep(2, 3)
                        self.warn("Could not send password, retrying")
                        self.send_keys(self.find_by_id('pwd'), self.password)
                        self.random_sleep(2, 3)
                        self.update()
                        self.click(self.find_by_css('a.pass_button'))
                    else:
                        raise Exception("Unrecognized alert text")
                    self.wait_for_element_by_css('iframe[name="main_content"]', timeout=30)
                except TimeoutException:
                    raise Exception("No alert found, could not login")
            self.random_sleep(2, 3)
            self.debug("Switching iframes")
            main_content_frame = self.find_by_css('iframe[name="main_content"]')
            self.driver.switch_to.frame(main_content_frame)
            self.wait_for_element_by_css('td.type_account', timeout=30)
            self.random_sleep(2, 3)
            self.update()

            # Scrape account no.
            self.debug("Scraping account no.")
            account_no = ''
            if self.institution_name in ['INDIANBANKCURRENT', 'INDIANBANKSAVINGS']:
                self.debug("Selecting account no. tab")
                self.maximize_window()
                self.random_sleep(2, 3)
                tabs = self.find_by_css('label[class*="tab"]', multiple=True)
                if self.institution_name == 'INDIANBANKCURRENT':
                    account_type_identifier = 'CA'
                else:
                    account_type_identifier = 'SB'
                if len(tabs) > 1:
                    if re.search(r'SAVINGS', tabs[0].text.strip()):
                        savings_tab = tabs[0]
                        current_account_tab = tabs[1]
                    else:
                        savings_tab = tabs[1]
                        current_account_tab = tabs[0]
                    if self.institution_name == 'INDIANBANKCURRENT':
                        current_account_tab.click()
                    else:
                        savings_tab.click()
                self.debug("Scraping account row")
                account_rows = self.find_by_css('div.content_table tbody tr', multiple=True)
                for account_row in account_rows:
                    if account_row.is_displayed():
                        account_type = self.find_by_css('td.type_data', parent=account_row).text.strip()
                        if account_type == account_type_identifier:
                            account_no = self.find_by_css('td.type_account', parent=account_row).text.strip()
                            self.debug("Account row scraped")
            else:
                account_no = self.find_by_css('td.type_account').text.strip()
            if not account_no:
                raise Exception("Could not find account no.")
            self.account_no = account_no
            self.debug("Account no.: " + str(self.account_no))
            self.update()
            self.info("Login successful")
            return True
        except Exception as e:
            self.error(e)
            return False

    def logout(self) -> bool:
        try:
            self.random_sleep(2, 3)
            self.driver.switch_to.parent_frame()
            self.driver.switch_to.parent_frame()
            self.click(self.find_by_css('div.cont_logout'))
            return True
        except Exception as e:
            self.error(e)
            return False

    def solve_captcha(self) -> bool:
        """
        Screenshot #img_captcha as base64, solve via self.solve(), send to #captcha.
        Returns True on success, False on failure.
        """
        try:
            self.update()
            self.debug("Solving CAPTCHA")
            captcha_text_returned = False
            for i in range(0, 3):
                self.update()
                if captcha_text_returned:
                    break
                captcha_b64 = self.find_by_id('img_captcha').screenshot_as_base64
                captcha_text = self.solve(captcha_b64)
                captcha_text_returned = True
                self.debug("CAPTCHA text returned: " + captcha_text)
                self.send_keys(self.find_by_id('captcha'), captcha_text)
                self.random_sleep(2, 3)
            self.update()
            self.debug("CAPTCHA text sent")
            return True
        except Exception as e:
            self.error(e)
            return False

    def open_recent_transaction_list(self, scheduled_download: bool = False) -> bool:
        """
        Navigate to Statement of Accounts, select the scraped account number,
        set the date range (today, or last 30 days on full sync), download
        the XML statement, and wait for the file to appear in the download dir.
        On session expiry (LogOff in URL), re-logs in and restarts sync.
        """
        try:
            statements_link = 'a[onclick*="Statement of Accounts\')"]'
            main_frame_css = 'iframe[name="main_content"]'
            from_date = 'input[name="fromdate"]'
            to_date = 'input[name="todate"]'
            statement_opened = False

            # Open statements tab
            for i in range(0, 10):
                try:
                    self.debug("Opening statement")
                    self.update()
                    self.maximize_window()
                    self.driver.switch_to.parent_frame()
                    self.driver.switch_to.parent_frame()
                    self.random_sleep(2, 3)
                    self.debug("Clicking statements link")
                    self.click(self.find_by_css(statements_link))
                    self.wait_for_element_by_css(main_frame_css, timeout=30)
                    self.random_sleep(2, 3)
                    main_frame = self.find_by_css(main_frame_css)
                    self.driver.switch_to.frame(main_frame)
                    self.wait_for_element_by_css('#soa_acctype option', timeout=30)
                    self.random_sleep(2, 3)

                    # Select account no.
                    account_no_selected = False
                    self.debug("Selecting account no.")
                    self.maximize_window()
                    try:
                        account_options = self.find_by_css('#soa_acctype option', multiple=True)
                        for account_option in account_options:
                            if re.search(self.account_no, account_option.text, re.I | re.M):
                                self.debug("Account no. found")
                                account_option.click()
                                self.random_sleep(2, 3)
                                account_no_selected = True
                                break
                    except Exception as e:
                        self.warn("Error selecting account no.")
                        self.warn(e)
                    if not account_no_selected:
                        self.warn("Could not select account no.")

                    # Select dates
                    self.debug("Selecting dates")
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    from_date_input = self.find_by_css(from_date)
                    to_date_input = self.find_by_css(to_date)
                    now = datetime.now()
                    today = now.strftime('%d/%m/%Y')
                    if self.do_full_sync:
                        last_month = (now - timedelta(days=30)).strftime('%d/%m/%Y')
                        from_date_script = """arguments[0].value = '{}';""".format(last_month)
                        self.do_full_sync = False
                    else:
                        from_date_script = """arguments[0].value = '{}';""".format(today)
                    to_date_script = """arguments[0].value = '{}';""".format(today)
                    self.update()
                    self.driver.execute_script(from_date_script, from_date_input)
                    self.random_sleep(2, 3)
                    self.driver.execute_script(to_date_script, to_date_input)
                    self.random_sleep(2, 3)

                    # Download statement as XML
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    self.debug("Downloading statement")
                    self.click(self.find_by_id('xml'))
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css('a.d_button'))
                    self.update()
                    no_txns_text = "NO RECORD FOUND"
                    no_txns_found = False
                    for w in range(0, 3):
                        self.update()
                        if re.search(no_txns_text, self.driver.page_source):
                            self.debug("No transactions found, opening full statement")
                            self.do_full_sync = True
                            no_txns_found = True
                            break
                        self.random_sleep(5, 8)
                        if self.get_most_recent_download():
                            break
                    if no_txns_found:
                        continue
                    self.update()
                    self.driver.switch_to.parent_frame()
                    self.driver.switch_to.parent_frame()
                    self.debug("Statement download finished")
                    statement_opened = True
                    break
                except WebDriverException as ex:
                    self.warn("Webdriver exception opening statement")
                    self.warn(ex)
                    if re.search(r'LogOff', self.driver.current_url):
                        self.debug("Session expiry, logging in")
                        self.login()
                        return self.sync_transactions()
            if not statement_opened:
                raise Exception("Could not open statement")
            return True
        except Exception as e:
            self.error(e)
            if re.search(r'LogOff', self.driver.current_url):
                self.debug("Session expiry, logging in")
                self.login()
                return self.sync_transactions()
            return False

    def parse_statement_file(self) -> list:
        """
        Parse the most recently downloaded XML statement file using minidom.
        Returns a list of transaction dicts with tx_date, tx_credit, tx_debit,
        tx_amount, credit_tx, total_available_balance, tx_remarks.
        """
        try:
            statement_file_path = _settings.DOWNLOAD_DIR + os.sep + self.get_most_recent_download()
            transactions = []
            statement_file = minidom.parse(statement_file_path)
            txns = statement_file.getElementsByTagName('rows')
            for row in txns:
                try:
                    if not row.getElementsByTagName("tranDate")[0].firstChild:
                        continue
                    tx_date = row.getElementsByTagName("tranDate")[0].firstChild.data
                    tx_remarks = row.getElementsByTagName("tranParticulars")[0].firstChild.data
                    tx_type = row.getElementsByTagName("tranTypeIndicator")[0].firstChild.data
                    tx_amount = row.getElementsByTagName("tranAmount")[0].firstChild.data
                    total_available_balance = row.getElementsByTagName("balanceAfterTran")[0].firstChild.data
                    if tx_type == "C":
                        tx_credit = tx_amount
                        tx_debit = "0"
                        credit_tx = 1
                    else:
                        tx_credit = "0"
                        tx_debit = tx_amount
                        credit_tx = 0
                    transaction = {
                        "tx_date": tx_date,
                        "tx_credit": tx_credit,
                        "credit_tx": credit_tx,
                        "tx_debit": tx_debit,
                        "tx_amount": tx_amount,
                        "total_available_balance": total_available_balance,
                        "tx_remarks": tx_remarks,
                    }
                    transactions.append(transaction)
                except Exception as e:
                    self.warn("Cannot parse transaction: " + str(row))
                    self.warn(e)
            if not transactions:
                self.warn("No transactions were found")
                self.report_no_transactions_found()
                self.do_full_sync = True
            self.debug("Statement parsing finished")
            self.debug("Number of transactions: " + str(len(transactions)))
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
                            raise Exception("Could not logout of Indian Bank, exiting sync")
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

                        # Loop through all transactions (oldest first after reverse)
                        transactions = self.parse_statement_file()
                        transactions.reverse()
                        for transaction in transactions:
                            try:
                                self.update()
                                tx_date = transaction["tx_date"]
                                tx_remarks = transaction["tx_remarks"]
                                tx_debit = transaction["tx_debit"]
                                tx_credit = transaction["tx_credit"]
                                credit_tx = transaction["credit_tx"]
                                tx_amount = transaction["tx_amount"]
                                total_available_balance = transaction["total_available_balance"]
                                tx_info = {
                                    "tx_date": tx_date,
                                    "tx_remarks": tx_remarks,
                                    "tx_withdrawal": tx_debit,
                                    "tx_deposit": tx_credit,
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
                    self.random_sleep(2, 3)
                    if not self.open_recent_transaction_list():
                        self.warn("Could not switch tabs")
                    self.random_sleep(2, 3)
        except Exception as e:
            self.error(e)
            return False

    def add_beneficiary(self) -> bool:
        try:
            for i in range(0, 3):
                try:
                    self.info("Adding beneficiary")
                    self.update()
                    self.maximize_window()
                    payee_details = self.command['beneficiary_details']
                    self.driver.switch_to.parent_frame()
                    self.driver.switch_to.parent_frame()
                    self.debug("Click nav button")
                    self.wait_for_element_by_css('#fund_transfer', timeout=30)
                    self.random_sleep(2, 3)
                    self.click(self.find_by_id('fund_transfer'))
                    self.wait_for_element_by_css('#ManageBeneficiary', timeout=30)
                    self.random_sleep(2, 3)
                    self.click(self.find_by_id('ManageBeneficiary'))
                    self.random_sleep(2, 3)
                    self.debug("Clicking add beneficiary")
                    add_payee_btns = self.find_by_css('#AddBeneficiary', multiple=True)
                    for add_payee_btn in add_payee_btns:
                        if add_payee_btn.is_displayed():
                            add_payee_btn.click()
                            break
                    self.wait_for_element_by_css('iframe[name="main_content"]', timeout=30)
                    self.random_sleep(2, 3)
                    frame = self.find_by_css('iframe[name="main_content"]')
                    self.driver.switch_to.frame(frame)
                    self.wait_for_element_by_css('#ibbenfname', timeout=30)
                    self.random_sleep(2, 3)
                    self.maximize_window()
                    self.update()
                    nick_name = payee_details['name'].split(' ')[0]
                    self.debug("Sending beneficiary info")

                    # Other bank flow
                    if payee_details['other_bank']:
                        self.debug("Starting other bank flow")
                        self.click(self.find_by_css('#bank_select > option[value="ab_other_bank"]'))
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('obifsccode'), payee_details['ifsc_code'])
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('obbenfname'), payee_details['name'])
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('obbenfNName'), nick_name)
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('obbenAccNos'), payee_details['account_number'])
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('obrebenAccNos'), payee_details['account_number'])
                        self.random_sleep(2, 3)
                        self.click(self.find_by_css('#bank_select > option[value="11-CURRENT ACCOUNT"]'))
                        self.random_sleep(2, 3)
                        self.debug("Clicking submit")
                        self.click(self.find_by_css('div.neft_acc ~ a.ab_indian_button2'))
                    # Same bank flow
                    else:
                        self.debug("Starting same bank flow")
                        self.send_keys(self.find_by_id('ibbenfname'), payee_details['name'])
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('ibbenfNName'), nick_name)
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('ibbenAccNos'), payee_details['account_number'])
                        self.random_sleep(2, 3)
                        self.debug("Clicking submit")
                        self.click(self.find_by_id('ib_ab_indian_button2'))
                    self.update()

                    # Fetch OTP
                    self.wait_for_element_by_css('#otp', timeout=30)
                    self.random_sleep(2, 3)
                    self.debug("Acquiring OTP")
                    self.update()
                    otp_response = get_otp(self.command)
                    if not otp_response or otp_response["response"] == "NotOk":
                        self.warn("Could not fetch OTP, retrying")
                        self.click(self.find_by_id('resend'))
                        self.wait_for_element_by_css('#otp', timeout=30)
                        self.random_sleep(5, 8)
                        otp_response = get_otp(self.command)
                        if not otp_response or otp_response["response"] == "NotOk":
                            raise Exception("Could not fetch OTP")
                    otp = str(otp_response["otp"])
                    self.debug("OTP acquired: " + str(otp))
                    self.send_keys(self.find_by_id('otp'), otp)
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css('a.mmid_ack_button2'))

                    # Check success text
                    self.debug("Checking for success text")
                    self.wait_for_element_by_css('div.vc_body_head', timeout=30)
                    self.random_sleep(2, 3)
                    success_text = "successfully and will be activated after 4 hours"
                    if not re.search(success_text, self.driver.page_source, re.I | re.M):
                        raise Exception("Could not find success text")
                    self.info("Beneficiary added successfully")
                    return True
                except Exception as ex:
                    self.error("Error in add beneficiary, retrying")
                    self.error(ex)
            raise Exception("Could not add beneficiary after 3 attempts")
        except Exception as e:
            self.error("Error adding beneficiary")
            self.error(e)
            return False

    def payout(self) -> bool:
        try:
            self.update()
            self.maximize_window()
            payout_details = self.command['payout_details']
            beneficiary_details = self.command['beneficiary_details']
            tx_password = self.login_details["qna"]["tx_password"]
            self.driver.switch_to.parent_frame()
            self.driver.switch_to.parent_frame()
            self.debug("Click nav button")
            self.wait_for_element_by_css('#fund_transfer', timeout=30)
            self.random_sleep(2, 3)
            self.click(self.find_by_id('fund_transfer'))
            self.wait_for_element_by_css('#ManageBeneficiary', timeout=30)
            self.maximize_window()
            self.random_sleep(2, 3)
            if beneficiary_details["other_bank"]:
                self.click(self.find_by_id('IMPS247OtherBankFundTransfer'))
                self.random_sleep(2, 3)
                self.click(self.find_by_css('ul.menu_list #FundTransferToACCOUNT'))
                self.wait_for_element_by_css('iframe[name="main_content"]', timeout=30)
                self.random_sleep(2, 3)
                frame = self.find_by_css('iframe[name="main_content"]')
                self.driver.switch_to.frame(frame)
                self.wait_for_element_by_css('#fromAcc > option', timeout=30)
                self.random_sleep(2, 3)
                self.maximize_window()
                account_no_options = self.find_by_css('#fromAcc > option', multiple=True)
                for account_no_option in account_no_options:
                    if re.search(self.account_no, account_no_option.text, re.M):
                        account_no_option.click()
                        break
                self.maximize_window()
                self.random_sleep(2, 3)
                self.click(self.find_by_css('a[title="Show All Items"]'))
                self.random_sleep(2, 3)
                menu_items = self.find_by_css('li.ui-menu-item', multiple=True)
                for menu_item in menu_items:
                    if re.search(beneficiary_details["account_number"], menu_item.get_property('innerText'), re.M):
                        self.driver.execute_script("arguments[0].click();", menu_item)
                        break
                self.maximize_window()
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id('amount'), payout_details["amount"])
                self.random_sleep(2, 3)
                self.click(self.find_by_css('#remarks option[value="others"]'))
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id('RemarksOthers'), payout_details["remarks"])
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id('transactionPwd'), tx_password)
            else:
                self.maximize_window()
                self.update()
                self.click(self.find_by_id('OtherIBAccounts'))
                self.wait_for_element_by_css('iframe[name="main_content"]', timeout=30)
                self.random_sleep(2, 3)
                frame = self.find_by_css('iframe[name="main_content"]')
                self.driver.switch_to.frame(frame)
                self.wait_for_element_by_css('#fromAcc > option', timeout=30)
                self.random_sleep(2, 3)
                account_no_options = self.find_by_css('#accno1 > option', multiple=True)
                for account_no_option in account_no_options:
                    if re.search(self.account_no, account_no_option.text, re.M):
                        account_no_option.click()
                        break
                self.maximize_window()
                self.random_sleep(2, 3)
                self.click(self.find_by_css('a[title="Show All Items"]'))
                self.random_sleep(2, 3)
                menu_items = self.find_by_css('li.ui-menu-item', multiple=True)
                for menu_item in menu_items:
                    if re.search(beneficiary_details["account_number"], menu_item.get_property('innerText'), re.M):
                        self.driver.execute_script("arguments[0].click();", menu_item)
                        break
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id('amount'), payout_details["amount"])
                self.random_sleep(2, 3)
                self.click(self.find_by_css('#remarks option[value="others"]'))
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id('RemarksOthers'), payout_details["remarks"])
                self.maximize_window()
                self.random_sleep(2, 3)
                self.send_keys(self.find_by_id('password'), tx_password)

            # Fetch OTP
            self.maximize_window()
            self.update()
            self.wait_for_element_by_css('#otp', timeout=30)
            self.random_sleep(2, 3)
            self.debug("Acquiring OTP")
            self.update()
            otp_response = get_otp(self.command)
            if not otp_response or otp_response["response"] == "NotOk":
                self.warn("Could not fetch OTP, retrying")
                resend_btn = self.find_by_id('resend')
                self.driver.execute_script("arguments[0].click();", resend_btn)
                self.wait_for_element_by_css('#otp', timeout=30)
                self.random_sleep(5, 8)
                otp_response = get_otp(self.command)
                if not otp_response or otp_response["response"] == "NotOk":
                    raise Exception("Could not fetch OTP")
            otp = str(otp_response["otp"])
            self.debug("OTP acquired: " + str(otp))
            self.send_keys(self.find_by_id('otp'), otp)
            self.random_sleep(2, 3)

            self.click(self.find_by_css('a.vc_dep_button2'))
            self.wait_for_element_by_css('a.pop_button2', timeout=30)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('a.pop_button2'))
            time.sleep(30)

            # Check success text
            self.update()
            self.debug("Checking for success text")
            success_text = "is processed successfully"
            if not re.search(success_text, self.driver.page_source, re.I | re.M):
                raise Exception("Could not find success text")
            self.info("Instant payout completed successfully")
            return True
        except Exception as e:
            self.error("Error during payout")
            self.error(e)
            return False

    def instant_payout(self) -> bool:
        try:
            for i in range(0, 3):
                try:
                    self.info("Starting instant payout")
                    self.update()
                    self.maximize_window()
                    payout_details = self.command['payout_details']
                    beneficiary_details = self.command['beneficiary_details']
                    tx_password = self.login_details["qna"]["tx_password"]
                    self.driver.switch_to.parent_frame()
                    self.driver.switch_to.parent_frame()
                    self.debug("Click nav button")
                    self.wait_for_element_by_css('#fund_transfer', timeout=30)
                    self.random_sleep(2, 3)
                    self.click(self.find_by_id('fund_transfer'))
                    self.wait_for_element_by_css('#ManageBeneficiary', timeout=30)
                    self.random_sleep(2, 3)
                    self.click(self.find_by_css('a[onclick="SelectedTab(this,\'INSTAPAY\')"]'))
                    self.wait_for_element_by_css('iframe[name="main_content"]', timeout=30)
                    self.random_sleep(2, 3)
                    frame = self.find_by_css('iframe[name="main_content"]')
                    self.driver.switch_to.frame(frame)
                    self.wait_for_element_by_css('#bank_select > option[value="ab_other_bank"]', timeout=30)
                    self.update()
                    self.maximize_window()
                    self.random_sleep(2, 3)
                    self.debug("Sending payout info")
                    if beneficiary_details["other_bank"]:
                        self.click(self.find_by_css('#bank_select > option[value="ab_other_bank"]'))
                    self.random_sleep(2, 3)
                    account_options = self.find_by_css('#fromAcc > option', multiple=True)
                    for account_option in account_options:
                        if re.search(self.account_no, account_option.text, re.M):
                            account_option.click()
                            break
                    self.random_sleep(2, 3)
                    self.send_keys(self.find_by_id('toaccount'), beneficiary_details["account_number"])
                    self.random_sleep(2, 3)
                    if beneficiary_details["other_bank"]:
                        self.send_keys(self.find_by_id('verifytoaccount'), beneficiary_details["account_number"])
                        self.random_sleep(2, 3)
                    self.send_keys(self.find_by_id('amount'), payout_details["amount"])
                    self.random_sleep(2, 3)
                    if beneficiary_details["other_bank"]:
                        self.send_keys(self.find_by_id('beneficiaryname'), beneficiary_details["name"])
                        self.random_sleep(2, 3)
                        self.send_keys(self.find_by_id('tomobile'), beneficiary_details["ifsc_code"])
                        self.random_sleep(2, 3)
                    self.click(self.find_by_id('others'))
                    self.random_sleep(2, 3)
                    self.send_keys(self.find_by_id('RemarksOthers'), payout_details["remarks"])
                    self.random_sleep(2, 3)
                    self.send_keys(self.find_by_id('password'), tx_password)
                    break
                except Exception as ex:
                    self.error(ex)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('a.vc_dep_button2'))

            # Fetch OTP
            self.wait_for_element_by_css('#otp', timeout=30)
            self.random_sleep(2, 3)
            self.debug("Acquiring OTP")
            self.update()
            otp_response = get_otp(self.command)
            if not otp_response or otp_response["response"] == "NotOk":
                self.warn("Could not fetch OTP, retrying")
                resend_btn = self.find_by_id('resend')
                self.driver.execute_script("arguments[0].click();", resend_btn)
                self.wait_for_element_by_css('#otp', timeout=30)
                self.random_sleep(5, 8)
                otp_response = get_otp(self.command)
                if not otp_response or otp_response["response"] == "NotOk":
                    raise Exception("Could not fetch OTP")
            otp = str(otp_response["otp"])
            self.update()
            self.maximize_window()
            self.debug("OTP acquired: " + str(otp))
            self.send_keys(self.find_by_id('otp'), otp)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('a.vc_dep_button2'))

            self.wait_for_element_by_css('a.pop_button2', timeout=30)
            self.random_sleep(2, 3)
            self.click(self.find_by_css('a.pop_button2'))
            time.sleep(30)

            # Check success text
            self.debug("Checking for success text")
            success_text = "is processed successfully"
            if not re.search(success_text, self.driver.page_source, re.I | re.M):
                raise Exception("Could not find success text")
            self.info("Instant payout completed successfully")
            return True
        except Exception as e:
            self.error("Error in instant payout")
            self.error(e)
            return False

    def scrape_balance(self) -> None:
        try:
            self.update()
            self.debug("Scraping account balance")
            self.random_sleep(2, 3)
            summary_link = 'ul.menu_list a[onclick*="Account Summary"]'
            self.driver.switch_to.parent_frame()
            self.driver.switch_to.parent_frame()
            self.click(self.find_by_css(summary_link))
            main_frame = 'iframe[name="main_content"]'
            self.wait_for_element_by_css(main_frame, timeout=30)
            self.update()
            self.random_sleep(2, 3)
            self.driver.switch_to.frame(self.find_by_css(main_frame))
            self.wait_for_element_by_css('tr[acctype]', timeout=30)
            self.random_sleep(2, 3)
            account_rows = self.find_by_css('tr[acctype]', multiple=True)
            balance = ""
            self.update()
            for account_row in account_rows:
                if re.search(self.account_no, account_row.text, re.M):
                    balance_text = self.find_by_css('td.type_bb', parent=account_row).text.strip()
                    balance_match = re.match(r'^\s*([0-9,.]+).+\s*$', balance_text)
                    if not balance_match:
                        raise Exception("Could not match balance")
                    balance = balance_match.group(1).replace(',', '').strip()
                    if not balance:
                        raise Exception("Could not match balance")
                    break
            self.debug("Balance scraped: " + str(balance))
            self.update_balance(balance)
            self.debug("Balance updated")
            self.update()
        except Exception as e:
            self.error(e)
