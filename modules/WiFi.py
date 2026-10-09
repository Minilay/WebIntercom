import network
import time

# CYW43 power modes. Older firmware lacks the named constants.
_PM_NONE = getattr(network.WLAN, "PM_NONE", 0xA11140)
# PM2: the radio sleeps between beacons and wakes 200 ms after traffic. The access point
# buffers packets meanwhile, so a reply arrives at most a beacon interval (~0.1-0.3 s) late.
_PM_PERFORMANCE = getattr(network.WLAN, "PM_PERFORMANCE", 0xA11142)


class WiFi:
    def __init__(self, hostname=None, power_save=True):
        self.power_save = power_save
        if hostname:
            try:
                network.hostname(hostname)  # shows up in the router / Pi-hole
            except Exception:
                pass
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        self._apply_power_mode()

    def _apply_power_mode(self):
        # The radio is the biggest consumer on a Pico W. Power save lets it doze between
        # beacons; set power_save=False if your access point drops dozing clients.
        # Must be re-applied after every active(True).
        try:
            self.wlan.config(pm=_PM_PERFORMANCE if self.power_save else _PM_NONE)
        except Exception as e:
            print("Wi-Fi: could not set power mode:", e)

    def connect(self, ssid, password, timeout=20):
        """Connect if not already connected. Returns True/False, never raises."""
        if self.wlan.isconnected():
            return True

        print("Connecting to %s..." % ssid)
        try:
            self.wlan.active(True)
            self._apply_power_mode()
            self.wlan.connect(ssid, password)
        except Exception as e:
            print("Wi-Fi: connect() failed:", e)
            return False

        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < timeout * 1000:
            if self.wlan.isconnected():
                print("Connected! IP address:", self.ip())
                return True
            status = self.wlan.status()
            # negative = wrong password / AP not found / join failed: stop waiting
            if status < 0 and time.ticks_diff(time.ticks_ms(), start) > 1000:
                break
            time.sleep_ms(250)

        print("Wi-Fi: not connected (status %s)" % self.wlan.status())
        try:
            self.wlan.disconnect()  # abort the half-finished join; next try starts clean
        except Exception:
            pass
        return False

    def reset(self):
        """Bounce the interface: drops the DHCP lease and re-joins from scratch.
        Used when Wi-Fi claims to be connected but nothing gets through."""
        print("Wi-Fi: resetting interface")
        for step in (self.wlan.disconnect, lambda: self.wlan.active(False)):
            try:
                step()
            except Exception:
                pass
        time.sleep_ms(1000)
        try:
            self.wlan.active(True)
            self._apply_power_mode()
        except Exception as e:
            print("Wi-Fi: re-activate failed:", e)

    def disconnect(self):
        if self.wlan.isconnected():
            self.wlan.disconnect()
            print("Disconnected from Wi-Fi.")

    def is_connected(self):
        return self.wlan.isconnected()

    def ip(self):
        return self.wlan.ifconfig()[0] if self.is_connected() else None

    def rssi(self):
        try:
            return self.wlan.status("rssi")
        except Exception:
            return None

    def scan(self):
        nets = self.wlan.scan()
        return [(ssid.decode(), rssi) for ssid, bssid, ch, rssi, authmode, hidden in nets]

    def status(self):
        return {
            "connected": self.is_connected(),
            "ip": self.ip(),
            "ssid": self.wlan.config("ssid") if self.is_connected() else None,
            "rssi": self.rssi() if self.is_connected() else None,
        }
