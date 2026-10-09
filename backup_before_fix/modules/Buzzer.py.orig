from machine import PWM
from math import e
from time import sleep

def volume2duty(volume):
    return (volume ** e / 2)

class Buzzer(PWM):
    def __init__(self, pin):
        super().__init__(pin)
        self._volume = 0.5
        self.freq(560)

    def duty(self, duty_cycle:float|None = None):
        """Set duty_cycle of PWM object in range from 0 to 1. """
        if duty_cycle == None:
            return self._duty
        if duty_cycle < 0 or duty_cycle > 1: 
            raise ValueError("duty_cycle must be in range from 0 to 1")
        self._duty = duty_cycle
        self._u16_duty = int(self._duty * 65535)
        self.duty_u16(self._u16_duty)

    def volume(self, volume=None):
        if volume is None:
            return self._volume
        if not 0 <= volume <= 1.0:
            raise ValueError("Volume must be between 0 and 1.0")
        self._volume = volume
        duty = volume ** e
        self.duty(duty / 2)

    def make_sound(self, freq, duration, volume = None):
        vol = volume if volume else self._volume
        self.freq(freq)
        self.duty(volume2duty(vol))
        sleep(duration)
        self.duty(0)

    def beep(self):
        self.make_sound(1000, 0.1, self._volume)
    
    def boop(self):
        self.make_sound(500, 0.1, self._volume)
        
    def on(self):
        self.volume(self._volume)

    def off(self):
        self.duty(0)

        
    def bell_like(self, freq):
        steps = 10
        for i in range(steps):
            self.make_sound(freq, 0.018, self._volume * (1-i/steps))

    def play_alert_sound(self):
        self.bell_like(660)
        self.bell_like(550)
        self.bell_like(440)
        self.bell_like(660)
