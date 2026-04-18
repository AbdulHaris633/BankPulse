import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import time
import requests
from multiprocessing import Queue, Value
from unittest.mock import patch

from app.transaction.banks.factory import get_transaction_manager
from app.core.settings import Settings

_settings = Settings()

# Fake proxy returned instead of hitting the real proxy server
MOCK_PROXY = {
    "proxy_soft": "other",
    "proxy_type": "socks5",
    "proxy_host": "127.0.0.1",
    "proxy_port": "1080",
    "proxy_user": "test",
    "proxy_password": "test",
    "_provider": "mock",
    "_rotate_url": "",
    "_rotate_mode": "url",
}


def get_first_profile_id():
    """Fetches the first available profile ID from AdsPower."""
    headers = {"Authorization": f"Bearer {_settings.ADSPOWER_API_KEY}"}
    endpoint = f"http://local.adspower.net:{_settings.ADSPOWER_PORT}/api/v1/user/list"
    try:
        resp = requests.get(endpoint, headers=headers, timeout=10)
        data = resp.json()
        if data.get("code") == 0:
            profiles = data.get("data", {}).get("list", [])
            if profiles:
                return profiles[0]["user_id"]
    except Exception as e:
        print(f"Error fetching profile: {e}")
    return None


def run_dry_test(bank_name, operation, account_id, account_pass):
    print(f"--- Starting Dry Run for {bank_name} ({operation}) ---")

    profile_id = get_first_profile_id()
    if not profile_id:
        print("ERROR: No AdsPower profiles found. Please create one first.")
        return

    # Construct dummy instruction
    instruction = {
        "instruction_id": str(int(time.time())),
        "institution_name": bank_name,
        "bot_id": profile_id,
        "device_id": "DRY-RUN-DEVICE",
        "trader_id": "999",
        "login_details": {
            "username": account_id,
            "password": account_pass
        },
        "security_answer": "DUMMY_ANSWER",
        "beneficiary_account": "123456789",
        "beneficiary_name": "TEST USER",
        "amount": "10.00",
        "operation": operation
    }

    print(f"Instruction ID: {instruction['instruction_id']}")
    print(f"Using Profile ID: {profile_id}")

    try:
        child_status = Queue()
        update_flag = Value('i', 0)

        obj = get_transaction_manager(instruction, child_status, update_flag)

        if not obj:
            print(f"FAILED: Could not find manager for bank: {bank_name}")
            return

        print(f"Manager {type(obj).__name__} initialized. Running {operation}...")

        # Mock proxy fetch and server reporting so we can step through
        # the actual bank logic (login, sync) without needing live servers
        with patch("app.utils.functions.get_p2p_proxy", return_value=MOCK_PROXY), \
             patch("app.utils.functions.proxy_hangup", return_value=True), \
             patch("app.utils.functions.report_child_status", return_value=True), \
             patch("app.utils.functions.report_sync_status", return_value=True), \
             patch("app.utils.functions.upload_screenshot", return_value=""), \
             patch("app.utils.functions.mysql_exec", return_value=True):
            obj.run()

        print(f"\n--- Dry Run Completed for {bank_name} ---")

    except Exception as e:
        print(f"CRITICAL ERROR during dry run: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dry run a specific bank automation.")
    parser.add_argument("--bank", default="KOTAK", help="Bank name (e.g., KOTAK, FEDERAL, RBL, RBL_SP, RBL_CORPORATE, KVB, KARNATAKA, CANARA, INDIAN_BANK, FEDERAL_MERCHANT)")
    parser.add_argument("--op", default="login_check", help="Operation (login_check, sync, etc.)")
    parser.add_argument("--id", default="DUMMY_ID", help="Bank account ID")
    parser.add_argument("--pw", default="DUMMY_PW", help="Bank account password")

    args = parser.parse_args()

    run_dry_test(args.bank, args.op, args.id, args.pw)
