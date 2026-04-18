from traceback import extract_stack
from datetime import datetime
import platform
import os
import time
import requests
import multiprocessing
from colorama import init, Fore

from app.core.settings import Settings

# Load settings once at module level
_settings = Settings()


class Logger:
    """
    Builds structured log messages and puts them onto a multiprocessing Queue.
    The Queue is consumed by LogWriter (running in a separate process) which
    prints to console and ships logs to Datadog in batches.

    This class is NOT used directly — instead use the module-level functions:
        debug(), info(), warn(), error()

    The Logger instance is shared across the parent process and all child
    transaction processes via init_logger() / init_logger_worker().
    """

    def __init__(self, log_queue):
        # Capture machine info once at startup — included in every log entry
        # so logs from multiple machines can be distinguished in Datadog
        self.system = platform.system()
        self.release = platform.release()
        self.version = platform.version()
        self.architecture = platform.machine()
        self.computer_name = platform.node()
        self.login = "vm"  # os.getlogin() avoided as it can fail in some envs
        self.log_queue = log_queue
        # How deep in the call stack to look for the caller's file/function/line.
        # 3 means: skip log() itself, skip the debug/info/warn/error wrapper,
        # and capture the actual caller site.
        self.stack_limit = 3

    def scrub_passwords(self, command: dict) -> dict:
        """
        Remove sensitive login credentials from the command dict before logging.
        The full login_details field is blanked out to prevent passwords,
        transaction PINs, and security Q&A from appearing in Datadog logs.
        Returns a shallow copy — does not mutate the original command dict.
        """
        try:
            if not command:
                return command
            if "login_details" not in command.keys():
                return command
            # Shallow copy so the original command used by the bot is unaffected
            new_dict = command.copy()
            new_dict["login_details"] = ""
            return new_dict
        except Exception as e:
            print(e)

    def log(self, log_level: str, msg, screenshot_url: str = None, command: dict = None) -> bool:
        """
        Build a structured log entry and push it onto the queue.
        LogWriter (separate process) picks it up and ships it to Datadog.

        The log entry contains:
        - Timestamp, level, app name
        - Machine info (OS, hostname, architecture)
        - Process ID (to trace which child process logged this)
        - Caller location (file, function, line number)
        - The message
        - Scrubbed command dict (trader context without passwords)
        - Optional screenshot URL (attached on error conditions)
        """
        try:
            timestamp = datetime.strftime(datetime.now(), '%Y-%m-%d %I:%M:%S:%f %p')
            pid = str(os.getpid())

            # Walk up the call stack to find the actual caller's location.
            # This gives us the real file/function/line, not this log() method.
            frame = extract_stack(limit=self.stack_limit)[0]
            file_name = os.path.basename(frame.filename)
            function = frame.name
            line_no = frame.lineno

            message_str = str(msg)

            # Build the human-readable log line for console output
            if screenshot_url:
                log_msg = "{0} - {1} - {2}:{3}:{4} - {5}:{6}:{7}:{8} - {9} : {10}:{11}:{12} - {13} - {14}"
                log_msg = log_msg.format(
                    timestamp, log_level,
                    _settings.APP_NAME, self.computer_name, self.login,
                    self.system, self.release, self.version, self.architecture,
                    pid, file_name, function, line_no,
                    message_str, screenshot_url
                )
            else:
                log_msg = "{0} - {1} - {2}:{3}:{4} - {5}:{6}:{7}:{8} - {9} : {10}:{11}:{12} - {13}"
                log_msg = log_msg.format(
                    timestamp, log_level,
                    _settings.APP_NAME, self.computer_name, self.login,
                    self.system, self.release, self.version, self.architecture,
                    pid, file_name, function, line_no,
                    message_str
                )

            # Build the structured JSON payload sent to Datadog
            log_json = {
                "timestamp": timestamp,
                "level": log_level,
                "app_name": _settings.APP_NAME,
                "computer_name": self.computer_name,
                "server_id": _settings.SERVER_ID,
                "login": self.login,
                "system": self.system,
                "release": self.release,
                "version": self.version,
                "architecture": self.architecture,
                "pid": pid,
                "file_name": file_name,
                "function": function,
                "line_no": line_no,
                "screenshot_url": screenshot_url or "",
                "msg": message_str,
                # Command is scrubbed to remove passwords before logging
                "data": self.scrub_passwords(command)
            }

            # Put onto the queue — LogWriter process consumes this asynchronously
            log = {'level': log_level, 'msg': str(log_msg), 'json': log_json}
            self.log_queue.put(log)
            return True
        except Exception as e:
            print(e)


class LogWriter:
    """
    Runs in its own dedicated process (spawned by init_logger()).
    Continuously drains the log queue, prints coloured output to console,
    and batches log events to Datadog's HTTP intake API.

    Runs as a separate process so that logging never blocks the main bot
    or any child transaction process — they just put onto the queue and move on.
    """

    def __init__(self, log_queue):
        self.log_queue = log_queue
        # Datadog HTTP intake — logs are sent as a JSON array (batched)
        self.dd_headers = {"DD-API-KEY": _settings.DD_API_KEY}
        self.dd_ingest_url = "https://http-intake.logs.ap1.datadoghq.com/api/v2/logs"
        # How often the writer checks the queue when it is empty (seconds)
        self.buffer_sleep_time = 0.1
        # Enable colorama auto-reset so colour codes don't bleed into next line
        init(autoreset=True)

    def run(self):
        """
        Infinite loop — drain the queue, print to console, batch-send to Datadog.
        Runs until the process is killed.
        """
        while True:
            log_events = []

            # Drain everything currently in the queue into a local batch
            while not self.log_queue.empty():
                try:
                    log = self.log_queue.get()
                    log_level = log['level']
                    log_msg = log['msg']
                    log_json = log['json']

                    # Colour-code console output by log level for readability
                    if log_level == 'ERROR':
                        stdio_msg = Fore.RED + log_msg
                    elif log_level == 'WARN':
                        stdio_msg = Fore.YELLOW + log_msg
                    elif log_level == 'INFO':
                        stdio_msg = Fore.CYAN + log_msg
                    else:
                        stdio_msg = log_msg

                    print(stdio_msg)

                    # Prepare Datadog payload for this log event
                    dd_log_data = {
                        "ddsource": log_json["app_name"],
                        "hostname": log_json["computer_name"],
                        "message": log_json 
                    }
                    log_events.append(dd_log_data)
                except Exception as e:
                    print(e)

            # Send the batch to Datadog if there are any events
            if log_events:
                try:
                    requests.post(
                        url=self.dd_ingest_url,
                        headers=self.dd_headers,
                        json=log_events,
                        timeout=_settings.REQUESTS_TIMEOUT
                    )
                except Exception as e:
                    print(e)

            # Sleep briefly before checking the queue again
            time.sleep(self.buffer_sleep_time)


def run_log_writer(log_queue):
    """
    Entry point for the LogWriter subprocess.
    Called by multiprocessing.Process(target=run_log_writer, args=(log_queue,))
    in init_logger().
    """
    log_writer = LogWriter(log_queue)
    log_writer.run()


def init_logger():
    """
    Called once in main.py at startup to initialise the logging system.
    Spawns the LogWriter as a background process and returns a Logger instance
    that the parent process (and all child processes via init_logger_worker)
    will use for all logging.
    """
    global logger
    log_queue = multiprocessing.Queue()
    # LogWriter runs in its own process so logging is fully non-blocking
    multiprocessing.Process(target=run_log_writer, args=(log_queue,)).start()
    logger = Logger(log_queue)
    return logger


def init_logger_worker(this_logger):
    """
    Called at the start of each child transaction process to inject the
    shared Logger instance created by the parent via init_logger().
    This ensures all child processes log to the same queue and therefore
    the same Datadog stream.
    """
    global logger
    logger = this_logger


# -----------------------------------------------------------------------------
# Module-level log functions
# These are the functions used everywhere in the codebase:
#   from app.services.logger import debug, info, warn, error
#
# When DEBUG_APP=True  → prints directly to console (no Datadog, no queue)
# When DEBUG_APP=False → sends to Logger queue → LogWriter → Datadog
# -----------------------------------------------------------------------------

def error(msg, command: dict = None, screenshot_url: str = None) -> None:
    """
    Log an ERROR level message.
    Used when something has failed and the operation cannot continue.
    A screenshot URL can be attached to give visual context in Datadog.
    """
    if _settings.DEBUG_APP:
        print(msg)
    else:
        global logger
        logger.log(log_level='ERROR', msg=msg, screenshot_url=screenshot_url, command=command)


def warn(msg, command: dict = None, screenshot_url: str = None) -> None:
    """
    Log a WARN level message.
    Used for recoverable issues — something went wrong but the bot will retry
    or continue (e.g. captcha failed once, proxy took too long, etc.)
    """
    if _settings.DEBUG_APP:
        print(msg)
    else:
        global logger
        logger.log(log_level='WARN', msg=msg, screenshot_url=screenshot_url, command=command)


def info(msg, command: dict = None, screenshot_url: str = None) -> None:
    """
    Log an INFO level message.
    Used for key milestones: login successful, payout complete, sync done, etc.
    """
    if _settings.DEBUG_APP:
        print(msg)
    else:
        global logger
        logger.log(log_level='INFO', msg=msg, screenshot_url=screenshot_url, command=command)


def debug(msg, command: dict = None, screenshot_url: str = None) -> None:
    """
    Log a DEBUG level message.
    Used for detailed step-by-step tracing inside operations.
    Only logged when VERBOSE=True in settings — can be turned off in production
    to reduce log volume without losing ERROR/WARN/INFO events.
    """
    if _settings.DEBUG_APP:
        print(msg)
    else:
        global logger
        if _settings.VERBOSE:
            logger.log(log_level='DEBUG', msg=msg, screenshot_url=screenshot_url, command=command)
