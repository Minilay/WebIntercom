from machine import Pin
import time
import network
import urequests as requests
from machine import Pin, reset
from credentials import *
from tools import *
import rp2


relay = Pin(7, Pin.OUT)
input = Pin(16, Pin.IN, Pin.PULL_DOWN)
led = Pin("LED", Pin.OUT)
led.on()
is_calling = False
def call_handler(pin):
    global is_calling
    is_calling = True

input.irq(trigger=Pin.IRQ_RISING, handler=call_handler)

display_message("@WebIntercomBot", 4, 16)
time.sleep(2)
wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.connect(ssid, password)
display_message("Connecting to", 4, 0)
display_message(f"{ssid}", 4, 14, False)

cnt = 0
while not wlan.isconnected() and wlan.status() >= 0:
    cnt += 1
    print('Waiting to connect...')
    oled.fill_rect(100, 14, 128, 18, 0)
    oled.text(f"{cnt}", 100, 14)
    oled.show()
    time.sleep(1)

    if cnt > 20:
        display_message("Connection failed")
        time.sleep(1)
        display_message("Resetting")
        time.sleep(1)
        reset()

# Handle connection error
if wlan.isconnected() :
    print('Connected')
    display_message(f"Connected to", 4, 10)
    display_message(f"{ssid}", 4, 20, False)
    status = wlan.ifconfig()
    print( 'ip = ' + status[0] )
else:
    display_message("Connection failed")
    time.sleep(1)
    reset()


def request_data():
    global OFFSET
    
    request = requests.get(requestURL + "?offset=" + str(OFFSET)).json()
    data = request['result']
    if data == []:
        return False
    data=data[0]
    OFFSET = data['update_id'] + 1
    chat_id = data['message']['chat']['id']
    text = data['message']['text']
    return (chat_id, text)

def send_message (chatId, message):
    rp2.PIO(0).remove_program()
    requests.post(sendURL + "?chat_id=" + str(chatId) + "&text=" + message)
    print("Message sent")

def open_door():
    relay.value(1)
    time.sleep(3)
    relay.value(0)

default_timeout = 60
last_open_request_time = 0
left_open_time = 0
left_open_timeout = 0

def open_door_handler(chat_id):
    global last_open_request_time, is_calling
    if is_calling:
        open_door()
        is_calling = False
    else:
        send_message(chat_id, f"Door will open automatically next {default_timeout} seconds")
        last_open_request_time = time.time()

def leave_open(chat_id, timeout):
    global left_open_time, left_open_timeout
    if not timeout.isdigit():
        return
    left_open_time = time.time()
    left_open_timeout = int(timeout) * 60
    send_message(chat_id, f"Door will be left open for the next {timeout} minutes.")

def close(chat_id):
    global left_open_time, left_open_timeout
    left_open_time = 0
    left_open_timeout = 0
    send_message(chat_id, "Door is closed")

def update ():
    global last_open_request_time, is_calling, left_open_time, left_open_timeout
    if is_calling:
        if time.time() - last_open_request_time <= default_timeout:
            last_open_request_time = 0
            open_door()
        elif time.time() - left_open_time <= left_open_timeout:
            open_door()
            send_message(my_chat_id, "Door was left open, someone came in")
        else:
            send_message(my_chat_id, "Knock-knock")
        flash_screen()
        is_calling = False
        time.sleep(1)
    
    if left_open_timeout != 0:
        time_left = (left_open_timeout - (time.time() - left_open_time))
        if time_left <= 0:
            left_open_time = 0
            left_open_timeout = 0
            oled.fill(0)
        display_message(f"Left open:{time_left // 60}:{time_left%60:02d}")
    else:
        oled.fill(0)
    data = request_data()
    if data == False:
        return
    
    chat_id, text = data
    
    print(text)
    if text == "/open":
        open_door_handler(chat_id)
    elif text.startswith("/open "):
        leave_open(chat_id, text[6::])
    elif text == "/force":
        open_door()
    elif text == "/close":
        close(chat_id) 
    else:
        display_message(text)

    

def main():
    try:
        update()
    except Exception as e:
        error_handler(e)
    finally:
        blink(led)
    
while True: 
    main()
    # time.sleep(0.3)
