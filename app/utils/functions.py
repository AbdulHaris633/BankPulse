import hashlib
import base64
import json
import os
import random
import sys
import re
import time
import requests
import mysql.connector
from datetime import datetime
from urllib.parse import urlparse

from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException

from app.core.settings import Settings
from app.core.proxy import get_proxy_manager
from app.services.logger import debug, info, warn, error
from app.orchestration.timer import Timer

# Load settings once at module level — all functions use this instance
_settings = Settings()


# STATUS CONSTANTS
# Returned by bank TransactionManager methods to signal specific outcomes.
# Used in TransactionManager.run() to set the correct report payload.
#
# BENEFICIARY_EXISTS    — beneficiary already added, no action needed
# ACCOUNT_BALANCE_TOO_LOW — payout failed due to insufficient balance
# WEBDRIVER_EXCEPTION   — Selenium error during operation
# DOWNLOAD_EXCEPTION    — bank statement download failed
BENEFICIARY_EXISTS = 2
ACCOUNT_BALANCE_TOO_LOW = 3
WEBDRIVER_EXCEPTION = 4
DOWNLOAD_EXCEPTION = 5


# CUSTOM EXCEPTIONS


class DownloadException(Exception):
    """
    Raised when a bank statement file fails to download.
    Caught in bank sync flows to trigger a failed sync report instead of
    a generic error — allows the server to retry the sync later.
    """
    pass


class UnrecoverableException(Exception):
    """
    Raised when an error occurs that cannot be recovered from within the
    current transaction (e.g. account locked, security question required).
    Triggers immediate shutdown of the transaction manager.
    """
    pass


# LEGACY ELEMENT HELPERS
# These pre-date V2's Browser.wait_for_element_by_css(). They are kept because
# some bank files still call them directly. New bank code should use
# self.wait_for_element_by_css() from Browser instead.

def element_exists(driver, css: str) -> bool:
    """
    Check if a CSS selector matches any element in the current page.
    Returns True if found, False if not.
    Used in wait_for_element() and directly in older bank login flows.
    """
    try:
        driver.find_element(By.CSS_SELECTOR, css)
    except NoSuchElementException:
        return False
    return True


def wait_for_element(driver, css: str) -> bool:
    """
    Poll for a CSS element to appear, checking every 2 seconds for up to 30s.
    Raises NoSuchElementException if element never appears.

    Legacy helper — kept because older bank files call it directly.
    New code should use self.wait_for_element_by_css() from Browser instead
    which has configurable timeout and does not raise on timeout.
    """
    for i in range(0, 15):
        if element_exists(driver, css):
            return True
        time.sleep(2)
    if not element_exists(driver, css):
        raise NoSuchElementException('Element not found: ' + css)


# SLEEP HELPERS
# Kept as standalone functions because bank files call them directly as
# small_sleep() / medium_sleep(). In TransactionManager subclasses you can
# also use self.random_sleep(1.5, 4) or self.random_sleep(5, 8) from Browser.

def small_sleep() -> None:
    time.sleep(random.randint(2, 3))


def medium_sleep() -> None:
    time.sleep(random.randint(5, 8))


# PROXY FUNCTIONS

def get_p2p_proxy(instruction: dict = None) -> dict:
    """
    Fetch a working proxy for this transaction via ProxyManager.

    ProxyManager rotates round-robin across all configured providers
    (Kookeey, iProxy, AstroProxy, BrightData, QuarkIP). Only providers
    with credentials set in .env participate in the rotation.

    Also measures and logs proxy speed (latency in seconds) so you can
    monitor which provider is fastest.

    Returns a proxy config dict in AdsPower format:
    {
        "proxy_soft": "other",
        "proxy_type": "socks5",
        "proxy_host": "1.2.3.4",
        "proxy_port": "8080",
        "proxy_user": "user",
        "proxy_password": "pass"
    }
    Internal _provider/_rotate_* fields are included for proxy_hangup() use.
    Returns False on failure.
    """
    try:
        proxy = get_proxy_manager().get_proxy()
        if not proxy:
            error("get_p2p_proxy: ProxyManager returned no proxy")
            return False
        debug("get_p2p_proxy: proxy ready — " + proxy.get("_provider", "unknown"))
        return proxy
    except Exception as e:
        error(e)
        return False


def proxy_hangup(proxy: dict) -> bool:
    """
    Rotate the IP after a transaction completes so the next transaction
    gets a fresh IP from the same provider.

    Called in TransactionManager.shutdown_handler() and close() regardless
    of whether the transaction succeeded or failed.

    Rotation behaviour per provider:
    - url mode    : GET the provider's rotation URL
    - session mode: no-op (Bright Data rotates via new session ID on next fetch)
    """
    try:
        return get_proxy_manager().rotate_ip(proxy)
    except Exception as e:
        error(e)
        return False


# OTP FUNCTIONS

def get_otp(instruction: dict):
    """
    Poll the OTP server until an SMS OTP code is received for this transaction.

    How it works:
    1. Make initial request to the OTP server with device_id + instruction_id
    2. Poll every 1 second for up to 180 seconds (3 minutes)
    3. When response["response"] == "ok", return the full JSON (contains OTP)
    4. If 180 seconds pass without a code, mark as expired and return anyway
       (caller handles the expired case)

    The OTP server receives the SMS via Twilio and holds it until the bot
    polls for it. The 180-second window matches typical bank OTP validity.

    Returns the JSON response dict on success or False on connection failure.

    V1 location: functions.py get_otp()
    """
    try:
        debug('Requesting OTP: ' + str(instruction["instruction_id"]))
        session = requests.session()
        params = {
            "device_id": instruction["device_id"],
            "instruction_id": instruction["instruction_id"]
        }
        debug('Request OTP params: ' + str(params))

        resp = session.get(url=_settings.GET_OTP_URL, params=params, timeout=_settings.REQUESTS_TIMEOUT)

        # Retry up to 3 times if server is temporarily unreachable
        if not resp.ok:
            for i in range(0, 3):
                time.sleep(3)
                resp = session.get(url=_settings.GET_OTP_URL, params=params, timeout=_settings.REQUESTS_TIMEOUT)
                if resp.ok:
                    break

        if not resp.ok:
            error_msg = "Could not fetch OTP, instruction_id: {}, {}:{}"
            error_msg = error_msg.format(instruction["instruction_id"], str(resp.status_code), resp.text)
            raise Exception(error_msg)

        debug('OTP Request URL: ' + resp.request.url)

        # Start 180-second polling window
        timer = Timer()
        timer.start()
        json_resp = resp.json()

        while timer.elapsed() < 180:
            json_resp = resp.json()
            otp_response = json_resp["response"].lower().strip()
            if otp_response == "ok":
                info("OTP received successfully: " + str(json_resp))
                return json_resp
            debug("OTP not yet received, polling: " + str(json_resp))
            resp = session.get(url=_settings.GET_OTP_URL, params=params, timeout=_settings.REQUESTS_TIMEOUT)
            time.sleep(1)

        # 180s passed — mark as expired on server and return whatever we have
        session.get(url=_settings.GET_OTP_URL, params={"expired": 1}, timeout=_settings.REQUESTS_TIMEOUT)
        info("OTP polling timed out: " + str(json_resp))
        return json_resp
    except Exception as exception:
        error(exception)
        return False


# SCREENSHOT FUNCTIONS

def get_random_file_name(extension: str) -> str:
    """
    Generate a random filename using MD5 hash of a random number.
    Used to create temporary captcha image files and screenshot files
    that won't collide between concurrent child processes.

    V1 location: functions.py get_random_file_name()
    """
    hashable_str = str(random.randint(99999999999, 1000000000000)).encode()
    hash_str = hashlib.md5(hashable_str).hexdigest()
    return hash_str + extension


def save_screenshot(driver: WebDriver) -> str:
    """
    Save a screenshot from a Selenium driver to a local temp file.
    Returns the file path on success, False on failure.

    Used in handle_webdriver_error() before uploading to the server.

    V1 location: functions.py save_screenshot()
    """
    try:
        hash_str = str(random.randint(99999999, 1000000000))
        img_file_path = hashlib.md5(hash_str.encode()).hexdigest() + '.png'
        driver.save_screenshot(img_file_path)
        return img_file_path
    except Exception as e:
        error(e)
        return False


def upload_screenshot(img_b64: str, instruction_id: str, command_type: str = "error") -> str:
    """
    Upload a base64-encoded screenshot to the screenshot server.
    Returns the public URL of the uploaded screenshot on success, False on failure.

    Called from TransactionManager.screenshot() on every error condition so
    that the Datadog log entry and the server report both have a screenshot URL
    for debugging failed transactions.

    img_b64        — base64 string from driver.get_screenshot_as_base64()
    instruction_id — attached to the screenshot for traceability
    command_type   — "error", "login", "payout" etc. used for server-side filing

    V1 location: functions.py upload_screenshot()
    """
    try:
        post_data = {
            "instruction_id": instruction_id,
            "command_type": command_type,
            "img_base64": img_b64
        }
        resp = requests.post(url=_settings.UPLOAD_SCREENSHOT_URL, json=post_data, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok or resp.json()["response"] == "NotOk":
            error_msg = "Could not upload screenshot: {}:{}".format(str(resp.status_code), resp.text)
            error(error_msg)
            return False
        return resp.json()["screenshot_url"]
    except Exception as e:
        error(e)
        return False


def handle_webdriver_error(driver: WebDriver, command: dict, exception: Exception) -> str:
    """
    On a Selenium/webdriver error: save screenshot, upload it, and log the error.
    Returns the screenshot URL so the caller can attach it to the server report.

    Called in parent_functions.py trader_sync() and trader_login() when an
    exception occurs during a transaction to ensure there is always a visual
    record of the failure state.

    V1 location: functions.py handle_webdriver_error()
    """
    try:
        img_b64 = driver.get_screenshot_as_base64()
        screenshot_url = upload_screenshot(img_b64=img_b64, instruction_id=command["instruction_id"])
        if not screenshot_url:
            error("Could not upload screenshot in webdriver error handler")
        error(msg=exception, screenshot_url=screenshot_url, command=command)
        return screenshot_url
    except Exception as e:
        error(e)
        return False


# SERVER REPORTING HELPERS (standalone versions)
# These are module-level versions used in parent_functions.py.
# TransactionManager has its own instance methods that do the same thing.
# Both sets are kept to avoid circular dependencies.

def report_child_status(data: dict, success: bool) -> bool:
    """
    Report the result of a child process operation back to the command server.
    V1 location: functions.py report_child_status()
    """
    try:
        data['success'] = 1 if success else 0
        resp = requests.post(url=_settings.CHILD_REPORT_URL, json=data, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok:
            return False
        return True
    except Exception as e:
        print(e)
        return False


def fetch_sync_mode(instruction_id: str):
    """
    Fetch the current sync mode config from the server for a given instruction.
    Returns the JSON response dict, or False on failure.

    Used in TransactionManager.fetch_sync_mode() (instance method) and
    directly in parent_functions.py for standalone sync operations.

    V1 location: functions.py fetch_sync_mode()
    """
    params = {"instruction_id": instruction_id}
    try:
        resp = requests.get(url=_settings.SYNC_MODE_URL, params=params, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok:
            error_msg = "Could not fetch sync mode: {}:{}".format(str(resp.status_code), resp.text)
            raise Exception(error_msg)
        if resp.text.strip() == 'UNAUTHORIZED':
            raise Exception("REQUEST UNAUTHORIZED")
        return resp.json()
    except Exception as e:
        error(e)
        return False


def report_sync_status(data: dict, success: bool, command: dict = None) -> bool:
    """
    Report the result of a sync operation to the sync report endpoint.
    V1 location: functions.py report_sync_status()
    """
    try:
        data['success'] = 1 if success else 0
        resp = requests.post(url=_settings.SYNC_REPORT_URL, json=data, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok:
            error('Could not report sync status: {}:{}'.format(str(resp.status_code), resp.text))
            return False
        debug("Sync report response: " + str(resp.text))
        return True
    except Exception as e:
        error(e)
        return False


# MYSQL DATABASE HELPERS
# Used to log browser info and save command history for auditing.
# All bank TransactionManagers call mysql_exec() / mysql_query() via
# update_browser_info() and update_last_failed_reason() in base.py.

def get_mysql_conn():
    """
    Open and return a MySQL connection using credentials from settings.
    Returns False on connection failure.
    Every database function opens its own connection and closes it after
    use — no persistent connection pool.

    V1 location: functions.py get_mysql_conn()
    """
    try:
        conn = mysql.connector.connect(
            host=_settings.MYSQL_HOST,
            port=_settings.MYSQL_PORT,
            user=_settings.MYSQL_USER,
            password=_settings.MYSQL_PASS,
            database=_settings.MYSQL_DB_NAME
        )
        return conn
    except Exception as e:
        print(e)
        return False


def mysql_query(sql: str, params=None):
    """
    Execute a SELECT query and return the results.
    Returns a single dict if one row, a list of dicts if multiple rows,
    or False if no results or on error.

    V1 location: functions.py mysql_query()
    """
    try:
        conn = get_mysql_conn()
        cursor = conn.cursor(dictionary=True)
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        records = cursor.fetchall()
        conn.commit()
        conn.close()
        if not records:
            return False
        if len(records) == 1:
            return records[0]
        return records
    except Exception as e:
        print(e)
        return False


def mysql_exec(sql: str, params=None) -> bool:
    """
    Execute an INSERT/UPDATE/REPLACE/DELETE statement.
    Returns True on success, False on error.

    V1 location: functions.py mysql_exec()
    """
    try:
        conn = get_mysql_conn()
        cursor = conn.cursor()
        if params:
            cursor.execute(sql, params)
        else:
            cursor.execute(sql)
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(e)
        return False


def save_commands(commands: list) -> bool:
    """
    Save a batch of server instructions to the MySQL commands table for auditing.
    The full command JSON is base64-encoded before storage to handle special chars.
    Called optionally in main.py after fetching instructions (currently commented out).

    V1 location: functions.py save_commands()
    """
    try:
        sql_query = '''INSERT INTO bp2pb_commands
            (instruction_id, trader_id, device_id, profile_id,
             institution, operation, command, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)'''
        for command in commands:
            try:
                now = datetime.now()
                date_time = now.strftime("%Y/%m/%d %H:%M:%S")
                params = (
                    command['instruction_id'],
                    command['trader_id'],
                    command['device_id'],
                    command['bot_id'],
                    command['institution_name'],
                    command['operation'],
                    base64.b64encode(json.dumps(command).encode()).decode(),
                    date_time
                )
                mysql_exec(sql_query, params)
            except Exception:
                pass
        return True
    except Exception as e:
        print(e)
        return False


def get_latest_command(instruction_id="", trader_id="", device_id="", bot_id="", institution_name=""):
    """
    Fetch the most recent command record from the MySQL commands table.
    Used for debugging — look up the last command run for a specific trader/profile.

    V1 location: functions.py get_latest_command()
    """
    try:
        table_name = 'u144687827_bp2pb.bp2pb_commands'
        query = 'SELECT * FROM {} WHERE {}=%s ORDER BY `timestamp` DESC LIMIT 1'
        if instruction_id:
            column_name = 'instruction_id'
            params = (instruction_id,)
        elif trader_id:
            column_name = 'trader_id'
            params = (trader_id,)
        elif device_id:
            column_name = 'device_id'
            params = (device_id,)
        elif bot_id:
            column_name = 'bot_id'
            params = (bot_id,)
        else:
            column_name = 'institution_name'
            params = (institution_name,)
        sql_query = query.format(table_name, column_name)
        return mysql_query(sql_query, params)
    except Exception as e:
        print(e)
        return False


# LEGACY COMMAND SYSTEM
# These functions are from an older version of the bot that used a direct IP
# command server (206.189.0.87). They are kept because some bank files may
# still reference them. They are NOT part of the active command flow which
# now uses parent_functions.py / parent.py.

# Legacy server endpoints (older API version)
_LEGACY_COMMAND_URL = 'http://206.189.0.87/command.php'
_LEGACY_REPORT_URL = 'http://206.189.0.87/report.php'
_COMMAND_ID_FILE = 'last_command_id'


def update_command_id(command_id: str) -> bool:
    """
    Track the last processed command ID in a local file to avoid reprocessing.
    V1 location: functions.py update_command_id()
    """
    try:
        last_command_id = get_last_command_id()
        if not last_command_id or int(command_id) > int(last_command_id):
            with open(_COMMAND_ID_FILE, 'w') as f:
                f.write(str(command_id).strip())
            return True
        return False
    except Exception as e:
        print(e)
        sys.exit(1)


def get_last_command_id():
    """
    Read the last processed command ID from the local tracking file.
    Returns False if the file does not exist yet.
    V1 location: functions.py get_last_command_id()
    """
    try:
        if os.path.exists(_COMMAND_ID_FILE):
            with open(_COMMAND_ID_FILE, 'r') as f:
                return int(f.read().strip())
        return False
    except Exception as e:
        print(e)
        sys.exit(1)


def post_command_status(command: dict, status: bool) -> bool:
    """
    Post command result to the legacy command server.
    V1 location: functions.py post_command_status()
    """
    try:
        status_str = str(command['id']) + ';'
        if command['command'] == 'ADD_BENEFECIARY':
            status_str += 'ADD_BENEFICIARY_SUCCESS;' if status else 'ADD_BENEFICIARY_FAILURE;'
            status_str += '"' + command['username'] + '";'
            status_str += '"' + command['beneficiary_name'] + '";'
            status_str += command['account_no'] + ';'
            status_str += '"' + command['ifsc_code'] + '"'
        if command['command'] == 'CASHOUT':
            status_str += 'CASHOUT_SUCCESS;'
            status_str += '"' + command['username'] + '";'
            status_str += command['amount'] + ';'
            status_str += command['remarks']
        resp = requests.post(url=_LEGACY_REPORT_URL, data=status_str, timeout=_settings.REQUESTS_TIMEOUT)
        while not resp.ok:
            resp = requests.post(url=_LEGACY_REPORT_URL, data=status_str, timeout=_settings.REQUESTS_TIMEOUT)
            time.sleep(2)
        return True
    except Exception as e:
        print(e)
        return False


def get_commands(commands_queue) -> None:
    """
    Poll the legacy command server for instructions and push them onto a queue.
    V1 location: functions.py get_commands()
    """
    while True:
        try:
            resp = requests.get(_LEGACY_COMMAND_URL)
            while not resp.ok:
                resp = requests.get(_LEGACY_COMMAND_URL)
                time.sleep(2)
            command_text = resp.text.strip()
            if command_text:
                command_lines = command_text.split("\n")
                for line in command_lines:
                    command_parts = line.split(';')
                    if command_parts:
                        command_name = command_parts[1]
                        command = None
                        if command_name == 'ADD_BENEFECIARY':
                            command = {
                                'id': command_parts[0],
                                'command': command_parts[1],
                                'username': command_parts[2].replace('"', '').strip(),
                                'beneficiary_name': command_parts[3].replace('"', '').strip(),
                                'account_no': command_parts[4],
                                'ifsc_code': command_parts[5].replace('"', '').strip()
                            }
                        if command_name == 'CASHOUT':
                            command = {
                                'id': command_parts[0],
                                'command': command_parts[1],
                                'username': command_parts[2].replace('"', '').strip(),
                                'amount': command_parts[3],
                                'remarks': command_parts[4].replace('"', '').strip()
                            }
                        if command:
                            if update_command_id(command['id']):
                                commands_queue.put(command)
        except Exception as e:
            print(e)
        time.sleep(5)
