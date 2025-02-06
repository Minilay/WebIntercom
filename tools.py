from machine import Pin, I2C, reset
from time import sleep
from ssd1306 import SSD1306_I2C
i2c = I2C(0, sda=Pin(0), scl=Pin(1))
oled = SSD1306_I2C(128, 32, i2c)

def display_message(message, x=0, y=0, clear=True):
    if clear: 
        oled.fill(0)
    oled.text(message, x, y)
    oled.show()


def error_handler(e):
    print(e)
    oled.fill(0)
    oled.text("Error", 0, 0)
    oled.text(str(e), 0, 10)
    oled.show()
    sleep(5)
    reset()


def flash_screen():
    for i in range(10):
        oled.fill(i%2==0)
        oled.show()
        sleep(0.05)

def blink(led):
    led.on()
    sleep(0.1)
    led.off()
    