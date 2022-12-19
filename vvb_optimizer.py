"""
Hot water scheduler to move electricity usage to hours that are usually cheap
Runs on a Raspberry Pi PICO with a SG90 servo connected to PWM GP0
Designed to run without WiFi. Upload to device using Thonny.
Servo connected to theromostat.

Power reset unit at 20:00 to get 2h extra hot water and resync schedule

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both 500 SEK to shop gadets with. Contact: github[a]visser.se
"""

from time import sleep
from machine import Pin, PWM

led = Pin(25, Pin.OUT)

pwm = PWM(Pin(0))
pwm.freq(50)

PWM_20_DEGREES = 1500
PWM_45_DEGREES = 4900
PWM_70_DEGREES = 8200
PWM_PER_DEGREE = (PWM_70_DEGREES - PWM_45_DEGREES) / 25  # Whole number
SECONDS_PER_HOUR = 3600.0  # 3600 in prod, 8.0 in test with schedule diff of 15 min
ROTATION_SECONDS = 2

SCHEDULE = [  # Hour, Minute, Desired PWM setting at the provided time
    [0, 15, PWM_20_DEGREES + 5 * PWM_PER_DEGREE],  # First sched time 00:15 or later
    [0, 30, PWM_20_DEGREES + 10 * PWM_PER_DEGREE],
    [0, 45, PWM_20_DEGREES + 15 * PWM_PER_DEGREE],
    [1, 0, PWM_20_DEGREES + 20 * PWM_PER_DEGREE],
    [1, 30, PWM_45_DEGREES],
    [2, 0, PWM_45_DEGREES + 5 * PWM_PER_DEGREE],
    [2, 40, PWM_45_DEGREES + 10 * PWM_PER_DEGREE],
    [3, 0, PWM_45_DEGREES + 15 * PWM_PER_DEGREE],
    [3, 30, PWM_45_DEGREES + 20 * PWM_PER_DEGREE],
    [4, 0, PWM_70_DEGREES],
    [5, 0, PWM_70_DEGREES - 5 * PWM_PER_DEGREE],
    [5, 30, PWM_20_DEGREES],
    [13, 30, PWM_20_DEGREES + 5 * PWM_PER_DEGREE],
    [14, 0, PWM_20_DEGREES + 10 * PWM_PER_DEGREE],
    [14, 30, PWM_20_DEGREES + 15 * PWM_PER_DEGREE],
    [15, 0, PWM_20_DEGREES + 20 * PWM_PER_DEGREE],
    [15, 30, PWM_20_DEGREES],
]


def apply_pwm(pwm_degrees, wait_hours, wait_minutes):
    """Send PWM request for limited amount of time."""
    wait_time = (
        (wait_hours + (wait_minutes / 60.0)) * SECONDS_PER_HOUR
    ) - ROTATION_SECONDS
    if wait_time < 0:
        print("Error in schedule !!!")
    else:
        sleep(wait_time)

    pwm.duty_u16(int(pwm_degrees))
    sleep(ROTATION_SECONDS)
    pwm.duty_u16(0)


def run_schedule():
    """Loops the schedule."""
    prev_hour = 0
    prev_minute = 0
    print("time is 00:00")
    for schedpoint in SCHEDULE:
        apply_pwm(schedpoint[2], schedpoint[0] - prev_hour, schedpoint[1] - prev_minute)
        prev_minute = schedpoint[1]
        prev_hour = schedpoint[0]
        print(f"At {prev_hour}:{prev_minute}")
    midnight_wait_time = (
        (24 - prev_hour) + ((0 - prev_minute) / 60.0)
    ) * SECONDS_PER_HOUR
    sleep(midnight_wait_time)


if __name__ == "__main__":
    print("70 at 20:00")
    pwm.duty_u16(PWM_70_DEGREES)
    sleep(ROTATION_SECONDS)
    pwm.duty_u16(0)

    print("switching to off at 22:00...")
    apply_pwm(PWM_20_DEGREES, 2, 0)

    print("At 22:00 - waiting for midnight...")
    sleep((2 * SECONDS_PER_HOUR) - ROTATION_SECONDS)

    while True:
        run_schedule()
