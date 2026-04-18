import datetime
from datetime import datetime, timedelta
import psutil
import multiprocessing
from multiprocessing import Process, Manager, Queue, Value
import random
import time
import re

from app.core.adspower import AdsPowerAPI
from app.services.logger import Logger, init_logger, init_logger_worker, debug, info, warn, error
from app.orchestration.parent import (
    get_parent_instructions,
    confirm_instruction,
    create_trader,
    delete_trader,
    is_update_time,
)
from app.transaction.banks.factory import get_transaction_manager


def run_transaction_manager(command: dict, child_logger: Logger, child_status: Queue, update_flag: Value) -> None:
    """
    Entry point for each child process.

    V1 changes:
    - AdsPowerManager() creation removed — TransactionManager creates AdsPowerAPI internally
    - driver_manager parameter removed from get_transaction_manager() call
    """
    try:
        init_logger_worker(child_logger)
        # driver_manager = AdsPowerManager()  # V1: removed in V2, TM handles driver internally
        tm = get_transaction_manager(command, child_status, update_flag)
        if not tm:
            raise Exception("Could not initialize transaction manager")
        tm.run()
    except Exception as exception:
        error(msg=exception, command=command)


def get_serial_number(this_profile_id: str) -> int:
    """
    Delegate to AdsPowerAPI.get_serial_number() — already implemented in V2.
    V1 had this as raw requests calls; V2 centralises all AdsPower HTTP in AdsPowerAPI.
    """
    return AdsPowerAPI().get_serial_number(this_profile_id)


def cleanup_bot(bot_data: dict):
    try:
        bot_serial_no = bot_data["serial_no"]
        bot_process: Process = bot_data["process"]
        bot_process.kill()
        children = psutil.Process(bot_process.pid).children(recursive=True)
        for child in children:
            child.kill()
        for this_process in psutil.process_iter():
            try:
                cmdline = this_process.cmdline()
                exe = cmdline[0]
                if re.match(r'^.+\\SunBrowser.exe$', exe):
                    cmd_arg1 = "acc_id=" + str(bot_serial_no)
                    cmd_arg2 = "--protected-userid=" + str(bot_serial_no)
                    if cmd_arg1 or cmd_arg2 in cmdline:
                        this_process.kill()
            except Exception as ex:
                error(ex)
                pass
    except Exception as e:
        error("Exception in cleanup_bot: ")
        error(e)


def check_and_cleanup(bot_data: dict) -> bool:
    try:
        # debug("Checking bot status: " + str(bot_data))
        bot_trader_id = bot_data["trader_id"]
        bot_serial_no = bot_data["serial_no"]
        bot_process: Process = bot_data["process"]
        elapsed_delta = timedelta(minutes=bot_status_elapsed_minutes)
        bot_last_update_time = bot["last_update_time"]
        bot_expired_time = (bot_last_update_time + elapsed_delta)
        now = datetime.now()
        if now > bot_expired_time:
            error("Bot process hanged, forceful exit for trader ID: " + str(bot_trader_id))
            bot_process.kill()
            children = psutil.Process(bot_process.pid).children(recursive=True)
            for child in children:
                child.kill()
            for this_process in psutil.process_iter():
                try:
                    cmdline = this_process.cmdline()
                    exe = cmdline[0]
                    if re.match(r'^.+\\SunBrowser.exe$', exe):
                        cmd_arg1 = "acc_id=" + str(bot_serial_no)
                        cmd_arg2 = "--protected-userid=" + str(bot_serial_no)
                        if cmd_arg1 or cmd_arg2 in cmdline:
                            this_process.kill()
                except Exception as ex:
                    print(ex)
                    pass
        return True
    except Exception as ex:
        error(ex)
        return False


if __name__ == '__main__':  # REMEMBER TO ENABLE/DISABLE DEBUG APP
    logger = init_logger()
    bots = []
    bot_status_elapsed_minutes = 15
    debug("Starting BP2PB")
    shared_memory = Manager()
    update_flag = shared_memory.Value('update', 0)
    while True:
        debug("Fetching commands")
        instructions = get_parent_instructions()

        # Check all bots run status
        # debug("Checking run status")
        bot_runtime_changes = False
        for bot in bots:
            try:
                process: Process = bot["process"]
                pid = bot["pid"]
                trader_id = bot["trader_id"]
                if not process.is_alive():
                    debug("Bot process exited: " + str(bot))
                    bots.remove(bot)
                    bot_runtime_changes = True
                    continue
                status: Queue = bot["status"]
                if not status.empty():
                    while not status.empty():
                        status.get()
                    bot["last_update_time"] = datetime.now()
                else:
                    if not check_and_cleanup(bot):
                        error("Error during check and cleanup: " + str(bot))
                    bot_runtime_changes = True
            except Exception as e:
                error("Error checking bot run status: " + str(bot))
                error(e)
            if not bot_runtime_changes:
                pass
                # debug("No bot status changes")

        # Check update status
        """if is_update_time():
            info("Update started, shutting down bots")
            update_flag.value = 1
            for bot in bots:
                if bot["process"].is_alive():
                    bot["process"].join()
            info("All bots have been shut down, exiting main")
            sys.exit()"""

        sleep_time = 1 + len(bots)
        time.sleep(sleep_time)
        if not instructions:
            continue
        else:
            # debug("Saving commands")
            # save_commands(instructions)
            random.shuffle(instructions)
        for instruction in instructions:
            try:
                operation = instruction['operation']
                debug('Processing instruction: ' + str(instruction["instruction_id"]))
                instruction_id = instruction['instruction_id']
                if operation == 'import_account':
                    if not confirm_instruction(instruction):
                        continue
                    info('Creating account' + str(instruction["instruction_id"]))
                    create_trader(instruction)
                    info('Finished create account' + str(instruction["instruction_id"]))
                    time.sleep(sleep_time)
                if operation == 'delete':
                    if not confirm_instruction(instruction):
                        continue
                    info('Deleting account' + str(instruction["instruction_id"]))
                    delete_trader(instruction)
                    info('Finished delete account' + str(instruction["instruction_id"]))
                    time.sleep(sleep_time)
                if operation == 'close':
                    try:
                        info('98723: Closing driver: ' + str(instruction["instruction_id"]))
                        close_trader_id = int(instruction["trader_id"])
                        # V1: close_adspower_driver(instruction["bot_id"])
                        # V2: AdsPowerAPI().stop_browser() handles stop internally
                        if AdsPowerAPI().stop_browser(instruction["bot_id"]):
                            debug("987113: Confirming browser is closed: " + str(instruction["instruction_id"]))
                            confirm_instruction(instruction)
                            for bot in bots:
                                if bot["trader_id"] == close_trader_id:
                                    cleanup_bot(bot)
                                    bots.remove(bot)
                                    break
                            info("987423: Finished closing driver: " + str(instruction["instruction_id"]))
                    except Exception as e:
                        error("98623: Exception in close operation")
                        error(e)
                    time.sleep(sleep_time)

                # Run TM commands
                if operation in ["login_check", "add_beneficiary", "payout", "sync", "quick_transfer_sbi"]:
                    debug("Running TM command: " + str(instruction["instruction_id"]))
                    if not confirm_instruction(instruction):
                        continue
                    status_queue = Queue()
                    p = multiprocessing.Process(target=run_transaction_manager,
                                                args=(instruction, logger, status_queue, update_flag))
                    p.start()
                    last_update_time = datetime.now()
                    profile_id = instruction["bot_id"]
                    trader_id = instruction["trader_id"]
                    serial_no = get_serial_number(profile_id)
                    if not serial_no:
                        warn("Could not fetch serial no. for trader ID: " + str(trader_id))
                    bot = {"process": p,
                           "pid": p.pid,
                           "status": status_queue,
                           "last_update_time": last_update_time,
                           "trader_id": trader_id,
                           "profile_id": profile_id,
                           "serial_no": serial_no}
                    bots.append(bot)
                    time.sleep(sleep_time)

            except Exception as e:
                error('Failed instruction: ' + str(instruction["trader_id"]))
                error(e)
