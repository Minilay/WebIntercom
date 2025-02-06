import machine
import time
freq = 270000000
speed = str(round(machine.freq()/1000000,1))
print("The starting speed is",speed,"MHz")
print("Starting the test in five seconds")
time.sleep(5)
while True:
   machine.freq(freq)
   speed = str(round(machine.freq()/1000000,1))
   print("The current speed is",speed,"MHz")
   freq -= 10000000
   time.sleep(2)