# Smart Temperature-Controlled Fan V6 - Proteus MicroPython / Raspberry Pi Pico
# Four-pin I2C-16X2 display at 0x3F, standard PCF8574 backpack mapping
from machine import Pin, ADC, PWM, I2C
from time import sleep_ms, ticks_ms, ticks_diff

LCD_ADDRESS = 0x3F
ADC_REF_VOLTS = 3.3
TEMP_OFFSET_C = 0.0       # Calibrate only after comparing with a trusted reference
SENSOR_PERIOD_MS = 50
DISPLAY_PERIOD_MS = 150
BUTTON_DEBOUNCE_MS = 40
ALARM_ON_C = 50.0
ALARM_OFF_C = 49.0       # Hysteresis prevents alarm chatter

class LCD1602:
    RS = 0x01
    EN = 0x04
    BL = 0x08

    def __init__(self, i2c, address):
        self.i2c = i2c
        self.addr = address
        self.lines = [None, None]
        sleep_ms(50)
        self._nibble(0x30)
        sleep_ms(5)
        self._nibble(0x30)
        sleep_ms(2)
        self._nibble(0x30)
        sleep_ms(2)
        self._nibble(0x20)
        sleep_ms(2)
        self.command(0x28)   # 4-bit, two lines
        self.command(0x08)   # Display off
        self.command(0x01)   # Clear display
        sleep_ms(3)         # LCD clear command is slower
        self.command(0x06)   # Entry mode
        self.command(0x0C)   # Display on, cursor off

    def _nibble(self, upper, rs=0):
        b = (upper & 0xF0) | self.BL | rs
        self.i2c.writeto(self.addr, bytes([b | self.EN, b]))

    def command(self, value):
        self._send(value, 0)

    def _send(self, value, rs):
        h = (value & 0xF0) | self.BL | rs
        l = ((value << 4) & 0xF0) | self.BL | rs
        self.i2c.writeto(self.addr, bytes([
            h | self.EN, h, l | self.EN, l
        ]))

    def line(self, row, message):
        text = str(message)[:16]
        while len(text) < 16:
            text += ' '
        if self.lines[row] == text:
            return
        self.command(0x80 if row == 0 else 0xC0)
        # Send whole row in one I2C operation instead of 32 separate ones.
        packet = bytearray(16 * 4)
        pos = 0
        for char in text:
            value = ord(char)
            h = (value & 0xF0) | self.BL | self.RS
            l = ((value << 4) & 0xF0) | self.BL | self.RS
            packet[pos] = h | self.EN
            packet[pos + 1] = h
            packet[pos + 2] = l | self.EN
            packet[pos + 3] = l
            pos += 4
        self.i2c.writeto(self.addr, packet)
        self.lines[row] = text

class Button:
    def __init__(self, pin):
        self.pin = Pin(pin, Pin.IN, Pin.PULL_UP)
        self.stable = self.pin.value()
        self.previous_raw = self.stable
        self.changed_at = ticks_ms()

    def pressed(self, now):
        raw = self.pin.value()
        if raw != self.previous_raw:
            self.previous_raw = raw
            self.changed_at = now
        if raw != self.stable and ticks_diff(now, self.changed_at) >= BUTTON_DEBOUNCE_MS:
            self.stable = raw
            return raw == 0
        return False

# Outputs OFF at start.
buzzer = Pin(16, Pin.OUT)
red_led = Pin(17, Pin.OUT)
buzzer.value(0)
red_led.value(0)
fan = PWM(Pin(15))
fan.freq(1000)
fan.duty_u16(0)

sensor = ADC(Pin(26))
mode_button = Button(14)
plus_button = Button(13)
minus_button = Button(12)

bus = I2C(0, sda=Pin(0), scl=Pin(1), freq=100000)
print('I2C addresses:', [hex(a) for a in bus.scan()])
lcd = None
if LCD_ADDRESS in bus.scan():
    try:
        lcd = LCD1602(bus, LCD_ADDRESS)
        lcd.line(0, 'SMART FAN V6')
        lcd.line(1, 'STARTING...')
    except Exception as exc:
        print('LCD initialization failed:', exc)
        lcd = None
else:
    print('LCD 0x3F not found; fan control continues.')


def read_temperature():
    # Fast four-sample ADC average; no artificial sleep between samples.
    total = 0
    for _ in range(4):
        total += sensor.read_u16()
    raw = total / 4
    voltage = raw * ADC_REF_VOLTS / 65535.0
    return voltage * 100.0 + TEMP_OFFSET_C


def auto_speed(t):
    if t < 30:
        return 0
    if t < 35:
        return 30
    if t < 40:
        return 50
    if t < 45:
        return 75
    return 100


def temp_string(t):
    # Explicit one-decimal rounding; avoids unsupported format features.
    tenths = int(t * 10 + 0.5)
    return str(tenths // 10) + '.' + str(tenths % 10)

manual_mode = False
manual_speed = 50
speed = -1
alarm = False
sensor_fault = True
last_sensor = ticks_ms() - SENSOR_PERIOD_MS
last_display = ticks_ms() - DISPLAY_PERIOD_MS
temperature = 0.0

while True:
    now = ticks_ms()
    if mode_button.pressed(now):
        manual_mode = not manual_mode
    if plus_button.pressed(now) and manual_mode:
        manual_speed = min(100, manual_speed + 10)
    if minus_button.pressed(now) and manual_mode:
        manual_speed = max(0, manual_speed - 10)

    if ticks_diff(now, last_sensor) >= SENSOR_PERIOD_MS:
        last_sensor = now
        try:
            temperature = read_temperature()
            # Out-of-range detection cannot identify all disconnected sensor faults.
            sensor_fault = (temperature < 1.0 or temperature > 120.0)
        except Exception as exc:
            print('ADC error:', exc)
            sensor_fault = True

        if sensor_fault or temperature >= ALARM_ON_C:
            alarm = True
        elif temperature <= ALARM_OFF_C:
            alarm = False

        if alarm:
            requested_speed = 100
        elif manual_mode:
            requested_speed = manual_speed
        else:
            requested_speed = auto_speed(temperature)

        if requested_speed != speed:
            speed = requested_speed
            fan.duty_u16(int(speed * 65535 / 100))

        buzzer.value(1 if alarm else 0)
        red_led.value(1 if alarm else 0)

    if ticks_diff(now, last_display) >= DISPLAY_PERIOD_MS:
        last_display = now
        if sensor_fault:
            first = 'TEMP: SENSOR ERR'
        else:
            first = 'TEMP: ' + temp_string(temperature) + 'C'
        if alarm:
            second = 'FAN:100% ALARM'
        else:
            second = 'FAN:' + str(speed) + '% ' + ('MAN' if manual_mode else 'AUTO')
        if lcd is not None:
            try:
                lcd.line(0, first)
                lcd.line(1, second)
            except Exception as exc:
                print('LCD update error:', exc)
                lcd = None
        print(first, '|', second)

    sleep_ms(5)