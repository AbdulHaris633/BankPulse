import time


class Timer:
    """
    A simple elapsed time tracker used across the bot to measure how long
    operations take — primarily used in get_otp() in functions.py to keep
    polling for an OTP for up to 180 seconds before giving up.
    
    """

    def __init__(self):
        # Holds the perf_counter value at the moment start() was called.
        # None means the timer has not been started yet.
        self._start_time = None

    def start(self):
        """
        Start the timer. If already running, does nothing (no restart).
        Call restart() if you need to reset and begin again.
        """
        if self._start_time is not None:
            return
        self._start_time = time.perf_counter()

    def stop(self):
        """
        Stop the timer and clear the start time.
        After calling stop(), elapsed() will raise an error until start()
        is called again.
        """
        if self._start_time is None:
            return
        self._start_time = None

    def elapsed(self):
        """
        Return total elapsed seconds as an integer since start() was called.
        Used in OTP polling loops to enforce a maximum wait time.
        """
        elapsed_time = time.perf_counter() - self._start_time
        return int(elapsed_time)

    def elapsed_minutes(self):
        """
        Return total elapsed time in whole minutes.
        Used in main.py to check if a bot process has been running too long
        and needs to be forcefully killed (15-minute timeout).
        """
        return int(self.elapsed() / 60)

    def restart(self):
        """
        Reset the timer and start counting from zero immediately.
        Unlike stop() + start(), this has no gap — the counter resets
        to the current moment right away.
        """
        self._start_time = time.perf_counter()
