from machine import Pin, reset, reset_cause, freq, WDT_RESET
from neopixel import NeoPixel
from time import sleep_ms, ticks_ms, ticks_diff, ticks_add, time
from credentials import SSID, PASSWORD, BOT_TOKEN, CHAT_ID
import gc
import os
import sys

from modules import Buzzer, TelegramBot, WiFi, Watchdog

# --- CONFIGURATION ---
PIN_BUZZER     = 2
PIN_INPUT      = 14
PIN_RELAY      = 15
PIN_LED_WS2812 = 16
PIN_INTERNAL_LED = "LED"

# Settings
LOUDNESS       = 1
OPEN_DURATION  = 3000   # ms
DEBOUNCE_MS    = 500    # ms

# Power
CPU_FREQ_MHZ    = 80    # 125 = stock. Lower draws less; TLS handshakes get a bit slower
WIFI_POWER_SAVE = True  # radio dozes between beacons. Set False if Wi-Fi becomes flaky
LONG_POLL_S     = 25    # Telegram holds each getUpdates open this long (max 50)
LOOP_WAIT_MS    = 250   # main loop tick = longest delay before a ring is handled
HEARTBEAT_S     = 5     # onboard LED blink period, 0 = off

# Access
ALLOW_ANYONE    = True  # anyone who finds the bot can use it. False = only CHAT_ID
NOTIFY_OWNER    = True  # tell CHAT_ID whenever someone else opens the door
OPEN_COOLDOWN_S = 10    # repeat /open within this window is ignored (double taps, spam)

# Reliability
ENABLE_WATCHDOG    = True  # set False while developing if auto-reboots get in the way
LOOP_STALL_S       = 90    # main loop silent this long -> hardware reset
WIFI_RESET_AFTER_S = 120   # no reply from Telegram this long -> bounce the Wi-Fi interface
REBOOT_AFTER_S     = 600   # no reply from Telegram this long -> full reboot
WIFI_RETRY_S       = 10    # pause between Wi-Fi connection attempts
MAX_COMMAND_AGE_S  = 60    # /open older than this was sent while offline -> ignored
HTTP_TIMEOUT_S     = 8
REASON_FILE        = "last_reset.txt"

# Status colours on the WS2812 (driven by the RP2040 itself, so it still works
# when the Wi-Fi chip -- which drives the onboard LED on a Pico W -- is wedged)
LED_OK      = (0, 0, 0)
LED_BOOT    = (0, 0, 10)
LED_TROUBLE = (10, 0, 0)

# --- HARDWARE INITIALIZATION ---
# Clock first: PWM dividers are computed from the clock at the time they are set.
if CPU_FREQ_MHZ:
    try:
        freq(CPU_FREQ_MHZ * 1000000)
    except ValueError as e:
        print("Could not set CPU clock:", e)

buzzer = Buzzer(PIN_BUZZER)
rgb_led = NeoPixel(Pin(PIN_LED_WS2812), 1)
onboard_led = Pin(PIN_INTERNAL_LED, Pin.OUT)
relay = Pin(PIN_RELAY, Pin.OUT, value=0)  # Ensure relay starts OFF
intercom_input = Pin(PIN_INPUT, Pin.IN, Pin.PULL_DOWN)

status_color = None


def set_status(color):
    global status_color
    if color != status_color:
        status_color = color
        rgb_led[0] = color
        rgb_led.write()


set_status(LED_BOOT)

# Armed before anything network-related, so even a hang during boot recovers.
watchdog = Watchdog(LOOP_STALL_S, enabled=ENABLE_WATCHDOG)

wifi = WiFi(hostname="webintercom", power_save=WIFI_POWER_SAVE)
tg = TelegramBot(BOT_TOKEN, CHAT_ID, timeout_s=HTTP_TIMEOUT_S)

# --- STATE MANAGEMENT ---
# Using a flag to move work from ISR to the main loop
ring_detected = False
last_trigger = ticks_add(ticks_ms(), -DEBOUNCE_MS - 1)
missed_rings = 0
next_wifi_try = ticks_ms()
last_intruder_alert = None
last_open = None
boot_time = time()


def input_handler(pin):
    """
    ISR: Keep this extremely minimal.
    Just set a flag and get out.
    """
    global ring_detected, last_trigger
    now = ticks_ms()
    if ticks_diff(now, last_trigger) > DEBOUNCE_MS:
        ring_detected = True
        last_trigger = now


# Attach Interrupt
intercom_input.irq(trigger=Pin.IRQ_RISING, handler=input_handler)

# --- CORE FUNCTIONS ---


def flash_led(color):
    for i in range(11):
        rgb_led[0] = color if i % 2 else (0, 0, 0)
        rgb_led.write()
        sleep_ms(50)
    rgb_led[0] = status_color
    rgb_led.write()


def read_boot_reason():
    reason = None
    try:
        with open(REASON_FILE) as f:
            reason = f.read().strip()
        os.remove(REASON_FILE)
    except OSError:
        pass
    if reason:
        return "self-recovery: " + reason
    if reset_cause() == WDT_RESET:
        # machine.reset() also reports WDT_RESET on the RP2040
        return "watchdog reset (main loop hung) or manual reset"
    return "power-on"


def reboot(reason):
    print("Rebooting:", reason)
    try:
        with open(REASON_FILE, "w") as f:
            f.write(reason)
    except OSError:
        pass
    tg.close()
    sleep_ms(200)
    reset()


def ensure_wifi():
    """True if connected. Otherwise retries every WIFI_RETRY_S; never raises."""
    global next_wifi_try
    if wifi.is_connected():
        return True
    if ticks_diff(ticks_ms(), next_wifi_try) < 0:
        return False
    set_status(LED_TROUBLE)
    tg.close()  # any open socket died with the link
    ok = wifi.connect(SSID, PASSWORD, timeout=20)
    next_wifi_try = ticks_add(ticks_ms(), WIFI_RETRY_S * 1000)
    return ok


def open_door(chat_id=None):
    """Triggers the relay with safety checks"""
    global last_open
    print("Action: Opening Door")
    relay.on()
    try:
        sleep_ms(OPEN_DURATION)
    finally:
        relay.off()  # never leave the relay energised, whatever happens
        last_open = time()
    tg.send_message("Vio-la", chat_id=chat_id)


def handle_ring():
    global ring_detected, missed_rings
    print("Intercom Triggered!")
    buzzer.play_alert_sound()
    flash_led((255, 100, 0))
    if not (wifi.is_connected() and tg.send_message("Knock-Knock!")):
        missed_rings += 1
    ring_detected = False  # Reset flag


def status_text():
    up = int(time() - boot_time)
    return "\n".join((
        "System Online",
        "Uptime: %dd %02dh %02dm" % (up // 86400, up % 86400 // 3600, up % 3600 // 60),
        "Last boot: " + boot_reason,
        "Wi-Fi RSSI: %s dBm, power save %s" % (wifi.rssi(), "on" if WIFI_POWER_SAVE else "off"),
        "CPU: %d MHz" % (freq() // 1000000),
        "Free RAM: %d KB" % (gc.mem_free() // 1024),
        "TLS connections: %d, requests: %d" % (tg.connections, tg.requests),
        "Last error: %s" % tg.last_error,
    ))


def process_telegram_command(msg):
    """Handles one incoming bot message"""
    global last_intruder_alert
    if msg is None:
        return
    text = msg.text.strip()
    if not text.startswith("/"):
        return

    is_owner = msg.chat_id == tg.chat_id
    if not is_owner and not ALLOW_ANYONE:
        print("Ignored %r from unauthorized chat %s (%s)" % (text, msg.chat_id, msg.sender))
        now = time()
        if last_intruder_alert is None or now - last_intruder_alert > 600:  # max 1 alert / 10 min
            last_intruder_alert = now
            tg.send_message("Ignored %s from unauthorized user %s (chat %s)"
                            % (text[:32], msg.sender, msg.chat_id))
        return

    # "/Open@MyBot now" -> "open"
    cmd = text.split()[0].split("@")[0].lstrip("/").lower()

    reply_to = msg.chat_id  # answer whoever asked

    if cmd == "open" or cmd == "force":
        if msg.age_s is not None and msg.age_s > MAX_COMMAND_AGE_S:
            tg.send_message("Ignored /%s sent %d s ago (I was offline). Send it again if "
                            "you still need the door." % (cmd, msg.age_s), chat_id=reply_to)
            return
        if last_open is not None and time() - last_open < OPEN_COOLDOWN_S:
            tg.send_message("The door was just opened.", chat_id=reply_to)
            return
        tg.save_offset()  # persisted first: this command can never replay after a reboot
        print("Opened by %s (chat %s)" % (msg.sender, msg.chat_id))
        open_door(reply_to)
        if NOTIFY_OWNER and not is_owner:
            tg.send_message("Door opened by %s (chat %s)" % (msg.sender, msg.chat_id),
                            silent=True)
    elif cmd == "status":
        tg.send_message(status_text(), chat_id=reply_to)
    elif cmd == "start" or cmd == "help":
        tg.send_message("Send /open to open the door.", chat_id=reply_to)


boot_reason = "unknown"


def main():
    global ring_detected, last_trigger, missed_rings, next_wifi_try, boot_reason
    boot_reason = read_boot_reason()
    print("Boot:", boot_reason)

    flash_led((0, 20, 0))
    buzzer.volume(LOUDNESS)
    buzzer.boop()
    buzzer.beep()

    print("System Booted. Monitoring intercom...")
    announced = False
    wifi_bounced = False
    last_beat = last_gc = ticks_ms()

    while True:
        try:
            watchdog.kick()
            now = ticks_ms()

            # Keep the debounce reference recent: ticks_diff() is only valid for
            # gaps < ~6.2 days, after which rings would be silently ignored.
            if ticks_diff(now, last_trigger) > 60000:
                last_trigger = ticks_add(now, -60000)

            # 1. Check if the Intercom rang (Flag set by ISR)
            if ring_detected:
                handle_ring()

            # 2. Network work, only when Wi-Fi is up
            waited = False
            if ensure_wifi():
                if not announced:
                    announced = tg.send_message("WebIntercom Online (%s)" % boot_reason,
                                                silent=True)
                if missed_rings and tg.failures == 0:
                    if tg.send_message("%d ring(s) while I was offline" % missed_rings):
                        missed_rings = 0

                tg.begin_poll(LONG_POLL_S)  # no-op while one is already waiting
                if tg.polling:
                    # The CPU sleeps in here until Telegram answers or LOOP_WAIT_MS passes.
                    waited = True
                    if tg.poll_ready(LOOP_WAIT_MS):
                        process_telegram_command(tg.finish_poll())

            # 3. Escalating recovery, based on the last *successful* Telegram reply
            silent_s = ticks_diff(ticks_ms(), tg.last_ok) // 1000
            if silent_s > REBOOT_AFTER_S:
                reboot("no reply from Telegram for %d s, last error: %s"
                       % (silent_s, tg.last_error))
            elif silent_s > WIFI_RESET_AFTER_S:
                if not wifi_bounced:
                    wifi_bounced = True
                    tg.close()
                    wifi.reset()
                    next_wifi_try = ticks_ms()
            else:
                wifi_bounced = False

            set_status(LED_OK if tg.failures == 0 and wifi.is_connected() else LED_TROUBLE)

            # 4. Short heartbeat blink every few seconds (goes through the Wi-Fi chip)
            if HEARTBEAT_S and ticks_diff(now, last_beat) >= HEARTBEAT_S * 1000:
                last_beat = now
                onboard_led.on()
                sleep_ms(30)
                onboard_led.off()

            # 5. Periodic garbage collection to prevent memory fragmentation
            if ticks_diff(now, last_gc) >= 10000:
                last_gc = now
                gc.collect()

            # 6. Idle until the next tick (already done above while a long poll waits)
            if not waited:
                sleep_ms(LOOP_WAIT_MS)

        except KeyboardInterrupt:
            relay.off()
            buzzer.off()
            tg.close()
            watchdog.pause(30 * 60)
            print("Stopped. Watchdog paused for 30 min; a soft reset (Ctrl-D) reboots "
                  "the board ~8 s later. Set ENABLE_WATCHDOG = False for long REPL work.")
            raise
        except Exception as e:
            print("Main loop error:")
            sys.print_exception(e)
            sleep_ms(1000)  # recovery is handled by the escalation above + watchdog


if __name__ == "__main__":
    # Start main application
    main()
