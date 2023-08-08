"""
Hot water scheduler to move electricity usage to hours that are usually cheap
Runs on a Raspberry Pi PICO with a SG90 servo connected to PWM GP0
Designed to run without WiFi. Upload to device using Thonny.
Servo connected to theromostat of electric water heater.

Power reset unit at 20:00 to get 2h extra hot water and resync schedule

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both 500 SEK to shop gadgets with. Contact: github[a]visser.se or
check referral.link in repo
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
USE_SUMMER_SCHEDULE = True
LEGIONELLA_HOUR = 5
LEGIONELLA_INTERVALL_DAYS = 7
WINTER_SCHEDULE = [  # Hour, Minute, Desired PWM setting at the provided time
    [0, 15, PWM_20_DEGREES + 10 * PWM_PER_DEGREE],  # First sched time 00:15 or later
    [0, 30, PWM_20_DEGREES + 15 * PWM_PER_DEGREE],
    [0, 45, PWM_20_DEGREES + 20 * PWM_PER_DEGREE],
    [1, 0, PWM_45_DEGREES],
    [1, 30, PWM_45_DEGREES + 4 * PWM_PER_DEGREE],
    [2, 0, PWM_45_DEGREES + 8 * PWM_PER_DEGREE],
    [2, 30, PWM_45_DEGREES + 12 * PWM_PER_DEGREE],
    [3, 0, PWM_45_DEGREES + 16 * PWM_PER_DEGREE],
    [3, 30, PWM_45_DEGREES + 20 * PWM_PER_DEGREE],
    [4, 0, PWM_45_DEGREES + 23 * PWM_PER_DEGREE],
    [4, 30, PWM_70_DEGREES],
    [LEGIONELLA_HOUR, 0, PWM_70_DEGREES - 5 * PWM_PER_DEGREE],  # required LEGIONELLA_HOUR match
    [6, 0, PWM_20_DEGREES],
    [13, 0, PWM_20_DEGREES + 7 * PWM_PER_DEGREE],
    [13, 30, PWM_20_DEGREES + 13 * PWM_PER_DEGREE],
    [14, 0, PWM_20_DEGREES + 18 * PWM_PER_DEGREE],
    [14, 30, PWM_20_DEGREES + 22 * PWM_PER_DEGREE],
    [15, 0, PWM_20_DEGREES],
]
# During the summer energy cost is low during high noon and ~3kWh dissipation/day @60* useless
SUMMER_SCHEDULE = [  # Hour, Minute, Desired PWM setting at the provided time
    [0, 15, PWM_20_DEGREES + 8 * PWM_PER_DEGREE],  # First sched time 00:15 or later
    [0, 30, PWM_20_DEGREES + 12 * PWM_PER_DEGREE],
    [0, 45, PWM_20_DEGREES + 16 * PWM_PER_DEGREE],
    [1, 0, PWM_20_DEGREES + 21 * PWM_PER_DEGREE],
    [1, 30, PWM_45_DEGREES],
    [2, 0, PWM_45_DEGREES + 3 * PWM_PER_DEGREE],
    [2, 30, PWM_45_DEGREES + 6 * PWM_PER_DEGREE],
    [3, 0, PWM_45_DEGREES + 9 * PWM_PER_DEGREE],
    [3, 30, PWM_45_DEGREES + 12 * PWM_PER_DEGREE],
    [4, 0, PWM_45_DEGREES + 15 * PWM_PER_DEGREE],
    [4, 30, PWM_45_DEGREES + 12 * PWM_PER_DEGREE],
    [LEGIONELLA_HOUR, 0, PWM_45_DEGREES + 10 * PWM_PER_DEGREE],  # required LEGIONELLA_HOUR match
    [6, 0, PWM_20_DEGREES],
    [11, 0, PWM_20_DEGREES + 5 * PWM_PER_DEGREE],
    [12, 0, PWM_20_DEGREES + 10 * PWM_PER_DEGREE],
    [13, 0, PWM_20_DEGREES + 15 * PWM_PER_DEGREE],
    [13, 30, PWM_20_DEGREES + 20 * PWM_PER_DEGREE],
    [14, 0, PWM_20_DEGREES + 15 * PWM_PER_DEGREE],
    [14, 20, PWM_20_DEGREES + 20 * PWM_PER_DEGREE],
    [14, 40, PWM_45_DEGREES],
    [15, 30, PWM_45_DEGREES + 5 * PWM_PER_DEGREE],
    [16, 0, PWM_20_DEGREES],
    [23, 0, PWM_20_DEGREES + 4 * PWM_PER_DEGREE],
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


def run_schedule(is_legionella_day=False):
    """Loops the schedule."""
    prev_hour = 0
    prev_minute = 0
    print("time is 00:00")
    for schedpoint in SUMMER_SCHEDULE if USE_SUMMER_SCHEDULE else WINTER_SCHEDULE:
        curr_pwm = schedpoint[2]
        if is_legionella_day and schedpoint[0] == LEGIONELLA_HOUR:
            curr_pwm = PWM_70_DEGREES
        apply_pwm(curr_pwm, schedpoint[0] - prev_hour, schedpoint[1] - prev_minute)
        prev_minute = schedpoint[1]
        prev_hour = schedpoint[0]
        print(f"At {prev_hour}:{prev_minute}. pwm {curr_pwm}")
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

    LEGIONELLA_DAY = 0
    while True:
        LEGIONELLA_DAY += 1
        run_schedule((LEGIONELLA_DAY % LEGIONELLA_INTERVALL_DAYS) == 0)
