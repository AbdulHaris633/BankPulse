import random
import requests
import time
import yaml

from app.core.settings import Settings
from app.core.adspower import AdsPowerAPI
from app.services.logger import Logger, debug, info, error, init_logger_worker
from app.transaction.banks.factory import get_transaction_manager
from app.utils.functions import (
    get_p2p_proxy,
    proxy_hangup,
    handle_webdriver_error,
    BENEFICIARY_EXISTS,
    ACCOUNT_BALANCE_TOO_LOW,
)

# from sbi_transaction_manager import SBITransactionManager  # not yet migrated

_settings = Settings()


# ─── SERVER COMMUNICATION ────────────────────────────────────────────────────
# All URLs are computed from _settings — they switch automatically when
# PAYOUT_SERVER=True (uses payout-specific endpoints on the command server).

def confirm_parent_instruction(instruction_id) -> bool:
    try:
        url = _settings.CONFIRM_INSTRUCTION_URL.format(instruction_id)
        resp = requests.get(url=url, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok:
            error('Could not confirm parent instruction: ' + str(resp.status_code))
            error(resp.text)
            return False
        if resp.json()['response'].lower().strip() == 'ok':
            return True
        return False
    except Exception as e:
        error(e)
        return False


def get_parent_instructions() -> list:
    try:
        resp = requests.get(url=_settings.PARENT_INSTRUCTIONS_URL, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok:
            error('Could not fetch parent instructions: ' + str(resp.status_code))
            error(resp.text)
            return False
        instructions = []
        for this_instruction in resp.json():
            instructions.append(this_instruction)
        return instructions
    except Exception as e:
        error(e)
        return False


def report_parent_status(data: dict, success: bool) -> bool:
    try:
        if success:
            data['success'] = 1
        else:
            data['success'] = 0
        resp = requests.post(url=_settings.PARENT_REPORT_URL, json=data, timeout=_settings.REQUESTS_TIMEOUT)
        print(resp.text)
        if not resp.ok:
            error('Could not report parent status: {}:{}'.format(str(resp.status_code), resp.text))
            return False
        return True
    except Exception as e:
        error(e)
        return False


def report_child_status(data: dict, success: bool) -> bool:
    try:
        if success:
            data['success'] = 1
        else:
            data['success'] = 0
        resp = requests.post(url=_settings.CHILD_REPORT_URL, json=data, timeout=_settings.REQUESTS_TIMEOUT)
        if not resp.ok:
            debug('Could not report child status: ' + str(resp.status_code))
            debug(resp.text)
            return False
        return True
    except Exception as e:
        error(e)
        return False


# ─── PROFILE MANAGEMENT ──────────────────────────────────────────────────────

def create_trader(command: dict) -> bool:
    """
    Create a new AdsPower browser profile for an imported trader account.
    Reports the new profile ID (bot_id) back to the command server.

    GoLogin support:
    - Enabled when _settings.ENABLE_GOLOGIN=True
    - GoLoginManager is not yet implemented in V2. If ENABLE_GOLOGIN=True,
      this function will raise NotImplementedError.
    - When implemented, both AdsPower and GoLogin profile IDs are reported
      as bot_ids: {"adspower": profile_id, "gologin": gl_profile_id}

    V1 location: parent_functions.py create_trader()
    V2 changes:
    - Direct AdsPower HTTP calls replaced with AdsPowerAPI.create_profile()
    - URL constants replaced with _settings properties
    - GoLogin guard added (was live in V1 via enable_gologin config flag)
    """
    data = False
    try:
        debug('Starting create trader')
        api = AdsPowerAPI()
        data = {
            "instruction_id": command['instruction_id'],
            "query": "import",
            "trader_id": command['trader_id'],
            "bot_ids": "",
            "device_id": command['device_id'],
            "success": 0,
        }
        debug('Data: ' + str(data))
        profile_name = "{}_{}".format(command["trader_id"], command["institution_name"])
        bank_name = command["institution_name"]
        if bank_name == "HDFC":
            domain = "netbanking.hdfcbank.com"
        elif bank_name == "FEDERAL":
            domain = "fednetbank.com"
        elif bank_name == "AXIS":
            domain = "omni.axisbank.co.in"
        elif bank_name == "INDUSIND":
            domain = "www.indusind.com"
        else:
            domain = "www.google.com"
        fingerprint_config = {
            "language": ["en-US", "en"],
            "language_switch": 0,
        }
        user_proxy_config = {
            "proxy_soft": "other",
            "proxy_type": "socks5",
            "proxy_host": "0.0.0.0",
            "proxy_port": "123",
            "proxy_user": "abc",
            "proxy_password": "xyz",
        }
        profile_json = {
            "name": profile_name,
            "domain_name": domain,
            "group_id": "0",
            "fingerprint_config": fingerprint_config,
            "user_proxy_config": user_proxy_config,
        }
        debug('Profile: ' + str(profile_json))
        profile_id = api.create_profile(profile_json)
        debug('New profile ID: ' + str(profile_id))
        if _settings.ENABLE_GOLOGIN:
            # GoLoginManager is not yet implemented in V2.
            # When implemented, create a GoLogin profile and report both IDs:
            # gl_manager = GoLoginManager()
            # gl_profile_id = gl_manager.create(profile_name)
            # bot_ids = {"adspower": profile_id, "gologin": gl_profile_id}
            # data = {... "bot_ids": bot_ids, "success": 1}
            raise NotImplementedError("GoLoginManager is not yet implemented in V2")
        else:
            data = {
                "instruction_id": command['instruction_id'],
                "query": "import",
                "action": "sync",
                "trader_id": command['trader_id'],
                "bot_id": profile_id,
                "device_id": command['device_id'],
                "success": 1,
            }
        debug("Data after profile creation: ")
        debug(str(data))
        report_parent_status(data, True)
        debug('Finished create trader')
        return True
    except Exception as e:
        if data:
            report_parent_status(data, False)
        error(e)
        return False


def delete_trader(command: dict) -> bool:
    """
    Stop and permanently delete an AdsPower profile for a trader being removed.
    Reports result back to the command server.

    V1 location: parent_functions.py delete_trader()
    V2 changes:
    - Direct AdsPower HTTP calls replaced with AdsPowerAPI.delete_profile()
      which handles stop + poll until closed + delete internally
    - URL constants replaced with _settings properties

    NOTE: V1 had a bug — at the end it passed the delete payload dict
    (data = {'user_ids': [profile_id]}) to report_parent_status instead of
    the status dict. Migrated as-is for compatibility.
    """
    data = False
    try:
        debug('Starting delete trader')
        api = AdsPowerAPI()
        data = {
            "instruction_id": command['instruction_id'],
            "query": "delete",
            "trader_ID": command['trader_id'],
            "bot_id": command['bot_id'],
            "device_id": command['device_id'],
            "success": 1,
        }
        debug('Data: ' + str(data))
        profile_id = command['bot_id']
        # AdsPowerAPI.delete_profile() handles: stop browser → poll until closed → delete
        api.delete_profile(profile_id)
        report_parent_status(data, True)
        return True
    except Exception as e:
        if data:
            report_parent_status(data, False)
        error(e)
        return False


# ─── SHUTDOWN FLOW ───────────────────────────────────────────────────────────

# def shutdown_flow(proxy: dict, data: dict, driver, profile_id: str, status: bool) -> bool:
#     """
#     Utility cleanup: release proxy, report status, stop browser.
#     Used when a TransactionManager is NOT available (e.g. create_trader,
#     delete_trader, or early failures before tm is created).
#
#     In all TransactionManager operations (trader_login, trader_sync, etc.),
#     this is replaced by tm.shutdown_handler(status) which handles the same
#     steps with full context (screenshot on error, account_no in report, etc.).
#
#     V1 location: parent_functions.py shutdown_flow()
#     V2 changes:
#     - close_adspower_driver(profile_id) replaced with AdsPowerAPI().stop_browser()
#     """
#     debug('Starting shutdown flow: ' + str(profile_id))
#     exception_raised = False
#     api = AdsPowerAPI()
#     if proxy:
#         try:
#             if not proxy_hangup(proxy):
#                 error_msg = 'Error with proxy hangup in shutdown flow: ' + profile_id
#                 raise Exception(error_msg)
#         except Exception as proxy_hangup_exception:
#             exception_raised = True
#             error(proxy_hangup_exception)
#     if data:
#         try:
#             debug("Report parent status in shutdown flow")
#             if not report_parent_status(data, status):
#                 error_msg = 'Error with report parent status in shutdown flow: ' + profile_id
#                 raise Exception(error_msg)
#         except Exception as report_parent_status_exception:
#             exception_raised = True
#             error(report_parent_status_exception)
#     if driver and profile_id:
#         try:
#             if not api.stop_browser(profile_id):
#                 error_msg = 'Error closing driver in shutdown flow: ' + profile_id
#                 raise Exception(error_msg)
#         except Exception as adspower_exception:
#             exception_raised = True
#             error(adspower_exception)
#     time.sleep(3)
#     debug('Ending shutdown flow: ' + profile_id)
#     if exception_raised:
#         return False
#     return True


# ─── TRANSACTION OPERATIONS ──────────────────────────────────────────────────
# Each function runs in a child process, does one bank operation, then shuts
# down via tm.shutdown_handler(). The child_logger is passed from the parent
# process so logs from child processes arrive on the same queue.

def trader_login(command: dict) -> bool:
    """
    Perform a login check for a trader account.
    Verifies credentials are still valid and scrapes the current account_no.

    V1 location: parent_functions.py trader_login()
    V2 changes:
    - shutdown_flow() replaced with tm.shutdown_handler()
    - V1 bug fixed: error branch was `if not tm:` (unreachable) → `if tm:`
    """
    tm = None
    try:
        debug('Starting trader login check')
        tm = get_transaction_manager(command)
        if not tm:
            raise Exception("Could not get transaction manager, exiting")
        tm.data["query"] = "login_check"
        tm.data["action"] = "login"
        if not tm.init():
            raise Exception("Could not initialize transaction manager")
        if not tm.login():
            raise Exception("Could not login with transaction manager")
        tm.data['account_number'] = tm.account_no
        info('Login successful')
        tm.shutdown_handler(True)
        return True
    except Exception as e:
        error(e)
        if tm:
            tm.shutdown_handler(False)
        return False


def trader_add_beneficiary(command: dict, child_logger: Logger) -> bool:
    """
    Add a new beneficiary (payee) for a trader account.

    V1 location: parent_functions.py trader_add_beneficiary()
    V2 changes:
    - shutdown_flow() replaced with tm.shutdown_handler()
    - Commented screenshot lines preserved (replaced by shutdown_handler's built-in screenshot)
    """
    tm = None
    try:
        init_logger_worker(child_logger)
        debug('Starting add beneficiary')
        tm = get_transaction_manager(command)
        if not tm:
            raise Exception("Could not get transaction manager, exiting")
        tm.data["query"] = "add_beneficiary"
        tm.data["action"] = "login"
        tm.data.update({"new": 1})
        if not tm.init():
            raise Exception("Could not initialize transaction manager")
        if not tm.login():
            raise Exception("Could not login to transaction manager")
        tm.data["action"] = "add_beneficiary"
        code = tm.add_beneficiary()
        if not code:
            raise Exception('Could not add beneficiary')
        if code == BENEFICIARY_EXISTS:
            tm.data['new'] = 0
        info('Finished add beneficiary')
        tm.screenshot()
        tm.close_dialogs()
        info('Beneficiary successfully added')
        tm.scrape_balance()
        tm.shutdown_handler(True)
        return True
    except Exception as e:
        if tm:
            tm.screenshot()
            tm.close_dialogs()
            tm.scrape_balance()
            # screenshot_url = handle_webdriver_error(driver=tm.driver, command=command, exception=e)
            # tm.data['screenshot_url'] = screenshot_url
            tm.shutdown_handler(False)
        error(e)
        return False


def trader_payout(command: dict, child_logger: Logger) -> bool:
    """
    Execute a payout (fund transfer) for a trader account.

    V1 location: parent_functions.py trader_payout()
    V2 changes:
    - shutdown_flow() replaced with tm.shutdown_handler()
    - Commented screenshot lines preserved (replaced by shutdown_handler's built-in screenshot)
    """
    tm = None
    try:
        init_logger_worker(child_logger)
        debug('Starting payout: ' + str(command))
        tm = get_transaction_manager(command)
        if not tm:
            raise Exception("Could not get transaction manager, exiting payout")
        tm.data["query"] = "payout"
        tm.data.update({"new": 1})
        if not tm.init():
            raise Exception("Could not initialize transaction manager")
        if not tm.login():
            raise Exception("Could not login to transaction manager")
        code = tm.payout()
        if not code:
            raise Exception('Could not complete payout')
        elif code == ACCOUNT_BALANCE_TOO_LOW:
            tm.data["new"] = 0
        info('Finished payout')
        tm.screenshot()
        tm.close_dialogs()
        tm.scrape_balance()
        tm.shutdown_handler(True)
        return True
    except Exception as e:
        if tm:
            tm.screenshot()
            tm.close_dialogs()
            tm.scrape_balance()
            # screenshot_url = handle_webdriver_error(driver=tm.driver, command=command, exception=e)
            # tm.data['screenshot_url'] = screenshot_url
            tm.shutdown_handler(False)
        error(e)
        return False


def trader_sync(command: dict, child_logger: Logger) -> bool:
    """
    Run the continuous transaction sync loop for a trader account.
    Reports login success to the server before entering the sync loop,
    so the server knows the bot is online even before the first transaction.

    V1 location: parent_functions.py trader_sync()
    V2 changes:
    - shutdown_flow() replaced with tm.shutdown_handler()
    - report_parent_status(tm.data, True) replaced with tm.report_status(True)
      (same endpoint, same payload — just uses the TM's built-in method)
    """
    tm = None
    try:
        init_logger_worker(child_logger)
        info('Starting trader sync')
        tm = get_transaction_manager(command)
        if not tm:
            raise Exception("Could not get transaction manager, exiting sync")
        if not tm.init():
            raise Exception("Could not initialize transaction manager")
        if not tm.login():
            raise Exception("Could not login with transaction manager")
        debug("Reporting login status success")
        if not tm.report_status(True):
            raise Exception("Cannot report sync status after login")
        tm.data["action"] = "sync"
        debug("Starting sync")
        if not tm.sync_transactions():
            raise Exception('Cannot complete sync')
        time.sleep(random.randint(5, 8))
        tm.shutdown_handler(True)
        return True
    except Exception as e:
        error(e)
        if tm:
            tm.shutdown_handler(False)
        return False


# ─── INSTRUCTION HELPERS ─────────────────────────────────────────────────────

def confirm_instruction(instruction: dict) -> bool:
    instruction_id = instruction["instruction_id"]
    if not confirm_parent_instruction(instruction_id):
        debug('Instruction not confirmed, continuing: ' + str(instruction_id))
        time.sleep(3)
        return False
    return True


def confirm_with_proxy(instruction: dict):
    """
    Fetch a P2P proxy for this instruction, then confirm the instruction
    with the server. Returns the proxy dict on success, False on failure.
    Releases proxy if confirmation fails.
    """
    proxy = get_p2p_proxy(instruction)
    instruction_id = instruction["instruction_id"]
    if not proxy:
        error('Could not fetch proxy: ' + str(instruction))
        time.sleep(3)
        return False
    if not confirm_parent_instruction(instruction_id):
        debug('Instruction not confirmed, continuing: ' + str(instruction))
        proxy_hangup(proxy)
        time.sleep(3)
        return False
    return proxy


# ─── UPDATE FLAG ─────────────────────────────────────────────────────────────

def is_update_time() -> bool:
    """
    Read config.yaml to check if a bot update has been scheduled.
    The parent process polls this before starting each new instruction cycle —
    if True, it waits for running children to finish then restarts itself.
    """
    try:
        stream = open("config.yaml", "r")
        config = yaml.safe_load(stream)
        ret = config["update"]
        stream.close()
        return bool(ret)
    except Exception as e:
        error(e)
