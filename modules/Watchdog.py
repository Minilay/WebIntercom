"""
Hardware watchdog with a long, software-defined timeout.

The RP2040 watchdog can only wait 8.3 s, which is shorter than a slow TLS handshake
plus a Wi-Fi reconnect. So a 1 s soft timer feeds the hardware watchdog, but only
while the main loop has called kick() within the last `stall_s` seconds.

If the main loop hangs anywhere -- a blocking socket, a wedged Wi-Fi driver, a
deadlock -- feeding stops and the RP2040 hardware-resets about 8 s later. If the
whole VM is frozen, the timer stops too and the result is the same.
"""
from machine import WDT, Timer
from time import ticks_ms, ticks_diff, ticks_add

HW_TIMEOUT_MS = 8000  # RP2040 maximum is 8388 ms


class Watchdog:
    def __init__(self, stall_s=90, enabled=True):
        self.enabled = enabled
        self._stall_ms = stall_s * 1000
        self._last = ticks_ms()
        self._pause_until = None
        self._wdt = None
        self._timer = None
        if enabled:
            self._wdt = WDT(timeout=HW_TIMEOUT_MS)
            self._timer = Timer(period=1000, mode=Timer.PERIODIC, callback=self._service)

    def kick(self):
        """Call from the main loop: 'I am still alive'."""
        self._last = ticks_ms()

    def pause(self, seconds):
        """Keep feeding unconditionally for a while (used after Ctrl-C so the REPL
        stays usable). Once armed, the RP2040 watchdog cannot be stopped, and a soft
        reset kills this timer, so the board reboots ~8 s after a soft reset."""
        self._pause_until = ticks_add(ticks_ms(), seconds * 1000)

    def _service(self, _timer):
        now = ticks_ms()
        if ticks_diff(now, self._last) < self._stall_ms:
            self._wdt.feed()
        elif self._pause_until is not None and ticks_diff(self._pause_until, now) > 0:
            self._wdt.feed()
