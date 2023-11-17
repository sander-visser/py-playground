"""
Hot water scheduler to move electricity usage to hours that are usually cheap
Runs on a Raspberry Pi PICO W(H) with a SG90 servo connected to PWM GP0
Power takeout for servo possible from VBUS pin
Designed for WiFi use. Servo connected to theromostat of electric water heater.
Upload to device using Thonny (as main.py).

Power reset unit to get 2h extra hot water

Repo also contans a schedule based optimizer (that does not utilize WiFi)

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both ~400 SEK to shop gadgets with. Contact: github[a]visser.se or
check referral.link in repo
"""

# Install micropyton (hold BOOTSEL while connecting USB to get drive mounted)
# Copy https://micropython.org/download/RPI_PICO_W/RPI_PICO_W-latest.uf2
# then (when WiFi connected, and before copying this script as main.py)
# import network
# wlan = network.WLAN(network.STA_IF)
# wlan.active(True)
# wlan.connect(WLAN_SSID, WLAN_PASS)
# import mip
# mip.install("urequests")
# mip.install("datetime")

import sys
import time
import gc
from datetime import date, timedelta
from machine import Pin, PWM
import rp2
import network
import ntptime
import urequests


# https://www.raspberrypi.com/documentation/pico-sdk/networking.html#CYW43_COUNTRY_
PR2_COUNTRY = "SE"
WLAN_SSID = "your ssid"
WLAN_PASS = "your pass"
NORDPOOL_REGION = "SE3"
OVERRIDE_TEST_HOUR = None  # Test script behaviour at different times today
UTC_OFFSET = 1  # 2 for manual CET summer time
COP_FACTOR = 3  # Utilize leakage unless heatpump will be cheaper
HEAT_LEAK_VALUE_THRESHOLD = 10
EXTREME_COLD_THRESHOLD = -8  # Heat leak always valuable
MAX_HOURS_NEEDED_TO_HEAT = 4
DEGREES_PER_HOUR = 8
LAST_MORNING_HEATING_HOUR = 6
DAILY_COMFORT_LAST_HOUR = 20
MIN_DAILY_TEMP = 50
MIN_LEGIONELLA_TEMP = 65
LEGIONELLA_INTERVAL = 10  # In days
SEC_PER_MIN = 60
EXTRA_HOT_DURATION_S = 120 * SEC_PER_MIN  # PWM_70_DEGREES duration after POR
HIGH_PRICE_THRESHOLD = 0.30  # In EUR
HOURLY_API_URL = "https://www.elprisetjustnu.se/api/v1/prices/"
# TEMPERATURE_URLshould return a number "x.y"
TEMPERATURE_URL = (
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp"
)

PWM_20_DEGREES = 1500  # Min rotation
PWM_70_DEGREES = 8100  # Max rotation
PWM_PER_DEGREE = (PWM_70_DEGREES - PWM_20_DEGREES) / 50
ROTATION_SECONDS = 2


class SimpleTemperatureProvider:
    def __init__(self):
        self.outdoor_temperature = 0

    def get_outdoor_temp(self):
        outdoor_temperature_req = urequests.get(TEMPERATURE_URL, timeout=10.0)
        if outdoor_temperature_req.status_code == 200:
            try:
                self.outdoor_temperature = float(outdoor_temperature_req.text)
            except ValueError:
                print(f"Ignored {outdoor_temperature_req.text} from {TEMPERATURE_URL}")
        return self.outdoor_temperature


class Thermostat:
    def __init__(self):
        self.pwm = PWM(Pin(0))
        self.pwm.freq(50)

    @staticmethod
    def get_pwm_degrees(degrees):
        pwm_degrees = PWM_20_DEGREES
        if degrees > 20:
            pwm_degrees += (degrees - 20) * PWM_PER_DEGREE
        return min(pwm_degrees, PWM_70_DEGREES)

    def set_thermosat(self, degrees):
        pwm_degrees = self.get_pwm_degrees(degrees)
        self.pwm.duty_u16(int(pwm_degrees))
        time.sleep(ROTATION_SECONDS)
        self.pwm.duty_u16(0)

    def nudge(self, degrees, nudge_degrees):
        pwm_degrees = self.get_pwm_degrees(degrees + nudge_degrees)
        self.pwm.duty_u16(int(pwm_degrees))
        time.sleep(1)
        self.pwm.duty_u16(0)

        time.sleep(5)

        pwm_degrees = self.get_pwm_degrees(degrees)
        self.pwm.duty_u16(int(pwm_degrees))
        time.sleep(1)
        self.pwm.duty_u16(0)

    def nudge_down(self, degrees):
        print("Nudging down")
        self.nudge(degrees, -5)

    def nudge_up(self, degrees):
        print("Nudging up")
        self.nudge(degrees, 5)


def setup_wifi():
    rp2.country(PR2_COUNTRY)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WLAN_SSID, WLAN_PASS)

    # Wait for connect or fail
    max_wait = 10
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        print("waiting for connection...")
        time.sleep(1)

    # Handle connection error
    if wlan.status() != 3:
        for wlans in wlan.scan():
            print(f"Seeing SSID {wlans[0]} with rssi {wlans[3]}")
        raise RuntimeError("network connection failed")

    print("connected")
    status = wlan.ifconfig()
    print(f"ip = {status[0]}")
    print(f"rssi = {wlan.status('rssi')}")


def sync_utc_time():
    # if needed, overwrite default time server
    # ntptime.host = "1.europe.pool.ntp.org"

    max_wait = 10
    while max_wait > 0:
        try:
            print(f"Local time before NTP sync：{time.localtime()}")
            ntptime.settime()
            print(f"UTC   time after  NTP sync：{time.localtime()}")
            break
        except Exception as excep:
            print("Error syncing time")
            print(excep)
            time.sleep(1)


def get_cost(end_date):
    if not isinstance(end_date, date):
        raise RuntimeError("Error not a date")
    hourly_api_url = HOURLY_API_URL + (
        f"{end_date.year}/{end_date.month}-{end_date.day}_{NORDPOOL_REGION}.json"
    )
    result = urequests.get(hourly_api_url, timeout=10.0)
    if result.status_code != 200:
        return None

    gc.collect()
    the_json_result = result.json()
    gc.collect()

    cost_array = []
    for row in the_json_result:
        cost_array.append(row["EUR_per_kWh"])
    return cost_array


def heat_leakage_loading_desired(local_hour, today_cost, tomorrow_cost, outdoor_temp):
    max_price = today_cost[local_hour]
    min_price = today_cost[local_hour]
    now_price = today_cost[local_hour]
    while local_hour < 23:
        local_hour += 1
        if max_price < today_cost[local_hour]:
            max_price = today_cost[local_hour]
        if min_price > today_cost[local_hour]:
            min_price = today_cost[local_hour]
    local_hour = 0
    if tomorrow_cost is not None:
        while local_hour < 23:
            local_hour += 1
            if max_price < tomorrow_cost[local_hour]:
                max_price = tomorrow_cost[local_hour]
    if (outdoor_temp <= EXTREME_COLD_THRESHOLD) or (
        max_price > (min_price * COP_FACTOR) and max_price > HIGH_PRICE_THRESHOLD
    ):
        return (
            now_price == min_price
        )  # This is the cheapest hour before significantly higher
    return False


def get_cheap_score_until(now_hour, until_hour, today_cost):
    """
    Give the cheapest MAX_HOURS_NEEDED_TO_HEAT a decreasing score
    that can be used to calculate heating curve
    """
    now_price = today_cost[now_hour]
    cheapest_hour = 0
    cheapest_price = today_cost[0]
    score = MAX_HOURS_NEEDED_TO_HEAT  # Assume now_hour is cheapest
    for scan_hour in range(0, until_hour + 1):
        if today_cost[scan_hour] < now_price:
            score -= 1
        if today_cost[scan_hour] < cheapest_price:
            cheapest_price = today_cost[scan_hour]
            cheapest_hour = scan_hour
    if now_hour < cheapest_hour:
        score = max(score, MAX_HOURS_NEEDED_TO_HEAT - (cheapest_hour - now_hour))
    print(f"Score given: {score}")
    return max(score, 0)


def next_night_is_cheaper(today_cost):
    min_price = today_cost[0]
    for i in range(1, 24):
        if today_cost[i] <= min_price:
            min_price = today_cost[i]
            if i > DAILY_COMFORT_LAST_HOUR:
                return True  # Found price to be cheaper next night
    return False


def is_the_cheapest_hour_during_daytime(today_cost):
    min_price = today_cost[0]
    for i in range(1, DAILY_COMFORT_LAST_HOUR):
        if today_cost[i] <= min_price:
            min_price = today_cost[i]
            if i > LAST_MORNING_HEATING_HOUR:
                return True  # Found price to be cheaper during daytime
    return False


def get_wanted_temp(
    local_hour, today_cost, tomorrow_cost, days_since_legionella, outside_temp
):
    wanted_temp = 20

    if today_cost:
        print(f"Current hour cost is {today_cost[local_hour]} EUR / kWh")
        if today_cost[local_hour] < HIGH_PRICE_THRESHOLD:
            wanted_temp += 5  # Slightly raise hot water takeout capacity
        if local_hour < 23 and today_cost[local_hour] < today_cost[local_hour + 1]:
            wanted_temp += 3  # Better trigger low temp heating now than later
            if (
                local_hour < 22
                and today_cost[local_hour + 1] < today_cost[local_hour + 2]
            ):
                wanted_temp += (
                    2  # Better trigger further low temp heating now than later
                )
        if tomorrow_cost is not None:
            if local_hour == 23 and (
                today_cost[23] < tomorrow_cost[0] or today_cost[23] < tomorrow_cost[1]
            ):
                wanted_temp += DEGREES_PER_HOUR  # Start pre-heating before midnight
        if is_the_cheapest_hour_during_daytime(today_cost):
            wanted_temp += DEGREES_PER_HOUR * get_cheap_score_until(
                local_hour, DAILY_COMFORT_LAST_HOUR, today_cost
            )
        if local_hour <= LAST_MORNING_HEATING_HOUR:
            if next_night_is_cheaper(today_cost):
                wanted_temp = MIN_DAILY_TEMP - (
                    DEGREES_PER_HOUR * MAX_HOURS_NEEDED_TO_HEAT
                )  # limit morning heating
            if is_the_cheapest_hour_during_daytime(today_cost):
                wanted_temp = MIN_DAILY_TEMP - (
                    DEGREES_PER_HOUR * (MAX_HOURS_NEEDED_TO_HEAT + 1)
                )  # limit morning heating more if day heating can be used

            wanted_temp += DEGREES_PER_HOUR * get_cheap_score_until(
                local_hour, LAST_MORNING_HEATING_HOUR, today_cost
            )
            if days_since_legionella > LEGIONELLA_INTERVAL and local_hour >= (
                LAST_MORNING_HEATING_HOUR - 2
            ):
                wanted_temp = MIN_LEGIONELLA_TEMP  # Secure legionella temp gets reached
        if outside_temp < HEAT_LEAK_VALUE_THRESHOLD and heat_leakage_loading_desired(
            local_hour, today_cost, tomorrow_cost, outside_temp
        ):
            wanted_temp += (
                DEGREES_PER_HOUR  # Extra boost since heat leakage is valuable
            )
    return wanted_temp


def run_hotwater_optimization(thermostat):
    setup_wifi()
    sync_utc_time()
    today = date.today()
    today_cost = None
    tomorrow_cost = None
    days_since_legionella = 0
    temperature_provider = SimpleTemperatureProvider()
    prev_wanted_temp = None
    while True:
        if today_cost is None or date.today() != today:
            today = date.today()
            days_since_legionella += 1
            today_cost = get_cost(today)
            tomorrow_cost = None
        if tomorrow_cost is None:
            tomorrow_cost = get_cost(today + timedelta(days=1))
        local_hour = time.localtime()[3] + UTC_OFFSET
        if OVERRIDE_TEST_HOUR is not None:
            print("Using override hour !!!")
            local_hour = OVERRIDE_TEST_HOUR
        if local_hour >= 24:
            local_hour -= 24
            today_cost = tomorrow_cost
            tomorrow_cost = None
            today += timedelta(days=1)

        print(
            f"Cost optimizing for {today.day} / {today.month} {today.year} {local_hour}:00"
        )
        outside_temp = temperature_provider.get_outdoor_temp()
        wanted_temp = get_wanted_temp(
            local_hour, today_cost, tomorrow_cost, days_since_legionella, outside_temp
        )
        print(f"Wanted temperature is {wanted_temp}. Outside is {outside_temp}")
        if wanted_temp >= MIN_LEGIONELLA_TEMP:
            days_since_legionella = 0

        if wanted_temp != prev_wanted_temp:
            thermostat.set_thermosat(wanted_temp)
            prev_wanted_temp = wanted_temp
        curr_min = time.localtime()[4]
        if curr_min < 50:
            time.sleep((50 - curr_min) * SEC_PER_MIN)  # Sleep slightly before next hour
        if local_hour < 23:
            next_hour_wanted_temp = get_wanted_temp(
                local_hour + 1,
                today_cost,
                tomorrow_cost,
                days_since_legionella,
                outside_temp,
            )
            if next_hour_wanted_temp > wanted_temp:
                thermostat.nudge_down(wanted_temp)
            if next_hour_wanted_temp < wanted_temp:
                thermostat.nudge_up(wanted_temp)
        time.sleep(12 * SEC_PER_MIN)  # Sleep slightly into next hour

        sync_utc_time()  # Sync time once per hour


if __name__ == "__main__":
    if "Pico W" in sys.implementation._machine:
        THERMOSTAT = Thermostat()
        print("Boosting...")
        THERMOSTAT.set_thermosat(MIN_LEGIONELLA_TEMP)
        time.sleep(EXTRA_HOT_DURATION_S)
        while True:
            try:
                run_hotwater_optimization(THERMOSTAT)
            except:
                time.sleep(300)

