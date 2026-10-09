import network, time

class WiFi:
    def __init__(self):
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)


    def connect(self, ssid: str, password: str, timeout: int = 10):
        if self.wlan.isconnected():
            print("Already connected.")
            print(f"IP address: {self.wlan.ifconfig()[0]}")
            return self.wlan.ifconfig()

        print(f"Connecting to {ssid}...")
        self.wlan.active(True)
        self.wlan.connect(ssid, password)

        start = time.ticks_ms()
        while not self.wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), start) > (timeout * 1000):
                raise RuntimeError("Wi-Fi connection timed out.")
            time.sleep(0.5)

        print("Connected!")
        print("IP address:", self.wlan.ifconfig()[0])
        return self.wlan.ifconfig()

    def disconnect(self):
        if self.wlan.isconnected():
            self.wlan.disconnect()
            print("Disconnected from Wi-Fi.")

    def is_connected(self):
        return self.wlan.isconnected()

    def ip(self):
        return self.wlan.ifconfig()[0] if self.is_connected() else None
    
    def scan(self):
        nets = self.wlan.scan()
        return [(ssid.decode(), rssi) for ssid, bssid, ch, rssi, authmode, hidden in nets]

    def status(self):
        return {
            "connected": self.is_connected(),
            "ip": self.ip(),
            "ssid": self.wlan.config('ssid') if self.is_connected() else None,
            "rssi": self.wlan.status('rssi') if self.is_connected() else None,
        }
   
