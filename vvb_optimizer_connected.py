"""
Hot water scheduler to move electricity usage to hours that are usually cheap
Runs on a Raspberry Pi PICO W(H) with a SG90 servo connected to PWM GP0
Power takeout for servo possible from VBUS pin if USB powered.
If USB only powered connect VBUS and VSYS for cleaner power (better WiFi)
WiFi range improves with ground connection and good placement.
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
NTP_HOST = "se.pool.ntp.org"
SEC_PER_MIN = 60
EXTRA_HOT_DURATION_S = 120 * SEC_PER_MIN  # MIN_LEGIONELLA_TEMP duration after POR
OVERRIDE_UTC_UNIX_TIMESTAMP = None  # Simulate script behaviour from (0==auto)
MAX_NETWORK_ATTEMPTS = 10
UTC_OFFSET_IN_S = 3600
COP_FACTOR = 2.5  # Utilize leakage unless heatpump will be cheaper
HIGH_WATER_TAKEOUT_LIKELYHOOD = (
    0.5  # Percent chance that thermostat will heat at MIN_TEMP setting during max price
)
HEAT_LEAK_VALUE_THRESHOLD = 10
EXTREME_COLD_THRESHOLD = -8  # Heat leak always valuable
MAX_HOURS_NEEDED_TO_HEAT = (
    4  # Should exceed (MIN_DAILY_TEMP - MIN_TEMP) / DEGREES_PER_H
)
NORMAL_HOURS_NEEDED_TO_HEAT = MAX_HOURS_NEEDED_TO_HEAT - 1
NUM_MOST_EXPENSIVE_HOURS = 3  # Avoid heating
DEGREES_PER_H = 9.4  # Nibe 300-CU ER56-CU 275L with 3kW
DEGREES_LOST_PER_H = 0.75
LAST_MORNING_HEATING_H = 6
DAILY_COMFORT_LAST_H = 21
MIN_TEMP = 25
MIN_NUDGABLE_TEMP = 28.6  # Setting it any lower will just make it MIN stuck
MIN_DAILY_TEMP = 50
MIN_LEGIONELLA_TEMP = 65
LEGIONELLA_INTERVAL = 10  # In days
WEEKDAYS_WITH_EXTRA_TAKEOUT = [6]  # 6 == Sunday
WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT = [0, 4]  # 0 == Monday
OVERHEAD_BASE_PRICE = 0.075  # In EUR for tax, purchase and transfer costs (wo VAT)
HIGH_PRICE_THRESHOLD = 0.15  # In EUR (incl OVERHEAD_BASE_PRICE)
HOURLY_API_URL = "https://www.elprisetjustnu.se/api/v1/prices/"
# TEMPERATURE_URLshould return a number "x.y"
TEMPERATURE_URL = (
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp"
)

PWM_25_DEGREES = 1172  # Min rotation (@MIN_TEMP)
PWM_78_DEGREES = 8300  # Max rotation
PWM_PER_DEGREE = (PWM_78_DEGREES - PWM_25_DEGREES) / 53
ROTATION_SECONDS = 2


class SimpleTemperatureProvider:
    def __init__(self):
        self.outdoor_temperature = 0

    def get_outdoor_temp(self):
        try:
            outdoor_temperature_req = urequests.get(TEMPERATURE_URL, timeout=10.0)
            if outdoor_temperature_req.status_code == 200:
                try:
                    self.outdoor_temperature = float(outdoor_temperature_req.text)
                except ValueError:
                    print(
                        f"Ignored {outdoor_temperature_req.text} from {TEMPERATURE_URL}"
                    )
        except OSError as ureq_err:
            if ureq_err.args[0] == 110:  # ETIMEDOUT
                print("Ignoring temperature read timeout")
            else:
                raise ureq_err
        return self.outdoor_temperature


class TimeProvider:
    def __init__(self):
        ntptime.host = NTP_HOST
        self.current_utc_time = (
            time.time() + OVERRIDE_UTC_UNIX_TIMESTAMP
            if (OVERRIDE_UTC_UNIX_TIMESTAMP is not None)
            and (OVERRIDE_UTC_UNIX_TIMESTAMP <= 0)
            else OVERRIDE_UTC_UNIX_TIMESTAMP
        )

    def hourly_timekeeping(self):
        if OVERRIDE_UTC_UNIX_TIMESTAMP is not None:
            self.current_utc_time += 3600
        else:
            self.sync_utc_time()  # Sync time once per hour

    def get_utc_unix_timestamp(self):
        return time.time() if self.current_utc_time is None else self.current_utc_time

    @staticmethod
    def sync_utc_time():
        max_wait = MAX_NETWORK_ATTEMPTS
        while max_wait > 0:
            try:
                print(f"Local time before NTP sync：{time.localtime()}")
                ntptime.settime()
                print(f"UTC   time after  NTP sync：{time.localtime()}")
                break
            except Exception as excep:
                print(f"Time sync error: {excep}")
                time.sleep(1)
                max_wait -= 1


class Thermostat:
    def __init__(self):
        self.pwm = PWM(Pin(0))
        self.pwm.freq(50)
        self.prev_degrees = None

    @staticmethod
    def get_pwm_degrees(degrees):
        pwm_degrees = PWM_25_DEGREES
        if degrees > MIN_TEMP:
            pwm_degrees += (degrees - MIN_TEMP) * PWM_PER_DEGREE
        return min(pwm_degrees, PWM_78_DEGREES)

    def set_thermosat(self, degrees):
        if self.prev_degrees != degrees:
            pwm_degrees = self.get_pwm_degrees(degrees)
            self.pwm.duty_u16(int(pwm_degrees))
            time.sleep(ROTATION_SECONDS)
            self.pwm.duty_u16(0)
            self.prev_degrees = degrees

    def nudge(self, nudge_degrees):
        if self.prev_degrees is not None:
            pwm_degrees = self.get_pwm_degrees(self.prev_degrees + nudge_degrees)
            self.pwm.duty_u16(int(pwm_degrees))

            time.sleep(1)

            pwm_degrees = self.get_pwm_degrees(self.prev_degrees)
            self.pwm.duty_u16(int(pwm_degrees))
            time.sleep(
                (2 * ROTATION_SECONDS)
                if (self.prev_degrees == MIN_NUDGABLE_TEMP)
                else 1
            )
            self.pwm.duty_u16(0)

    def nudge_down(self):
        # print("Nudging down")
        self.nudge(-5)

    def nudge_up(self):
        # print("Nudging up")
        self.nudge(5)


def setup_wifi():
    rp2.country(PR2_COUNTRY)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WLAN_SSID, WLAN_PASS)

    max_wait = MAX_NETWORK_ATTEMPTS
    while max_wait > 0:
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        max_wait -= 1
        print("waiting for connection...")
        time.sleep(1)

    if wlan.status() != 3:  # Handle connection error
        for wlans in wlan.scan():
            print(f"Seeing SSID {wlans[0]} with rssi {wlans[3]}")
        raise RuntimeError("network connection failed")

    print(f"Connected with rssi {wlan.status('rssi')} and IP {wlan.ifconfig()[0]}")


def get_cost(end_date):
    if not isinstance(end_date, date):
        raise RuntimeError("Error not a date")
    two_digit_month = f"{end_date.month}"
    if len(two_digit_month) == 1:
        two_digit_month = f"0{two_digit_month}"
    two_digit_day = f"{end_date.day}"
    if len(two_digit_day) == 1:
        two_digit_day = f"0{two_digit_day}"
    hourly_api_url = HOURLY_API_URL + (
        f"{end_date.year}/{two_digit_month}-{two_digit_day}_{NORDPOOL_REGION}.json"
    )
    gc.collect()
    result = urequests.get(hourly_api_url, timeout=10.0)
    if result.status_code != 200:
        return None

    the_json_result = result.json()
    gc.collect()

    cost_array = []
    for row in the_json_result:
        cost_array.append(row["EUR_per_kWh"] + OVERHEAD_BASE_PRICE)
    if len(cost_array) == 23:
        cost_array.append(OVERHEAD_BASE_PRICE)  # DST hack - off by one in adjust days
    return cost_array


def heat_leakage_loading_desired(local_hour, today_cost, tomorrow_cost, outdoor_temp):
    now_price = today_cost[local_hour]
    max_price = now_price
    min_price = now_price
    while local_hour < 23:
        local_hour += 1
        if max_price < today_cost[local_hour]:
            max_price = today_cost[local_hour]
        if min_price > today_cost[local_hour]:
            min_price = today_cost[local_hour]
    if tomorrow_cost is not None:
        for tomorrow_hour_price in tomorrow_cost:
            max_price = max(max_price, tomorrow_hour_price)
    if (outdoor_temp <= EXTREME_COLD_THRESHOLD) or max_price > (min_price * COP_FACTOR):
        print(f"Extra heating due to COP? {now_price} == {min_price}")
        return now_price == min_price
    return False


def refill_heating_worth_while(now_hour, today_cost, tomorrow_cost):
    """
    Scan 16h ahead and check if now is the best time to buffer some comfort
    """
    scan_hours_remaining = 16
    max_price_ahead = today_cost[now_hour]
    min_price_ahead = max_price_ahead
    for scan_hour in range(now_hour, min(24, now_hour + scan_hours_remaining)):
        scan_hours_remaining -= 1
        if today_cost[scan_hour] > max_price_ahead:
            max_price_ahead = today_cost[scan_hour]
        if today_cost[scan_hour] < min_price_ahead:
            min_price_ahead = today_cost[scan_hour]

    if tomorrow_cost is not None:
        for scan_hour in range(0, scan_hours_remaining):
            if tomorrow_cost[scan_hour] > max_price_ahead:
                max_price_ahead = tomorrow_cost[scan_hour]
            if tomorrow_cost[scan_hour] < min_price_ahead:
                min_price_ahead = tomorrow_cost[scan_hour]
        scan_hours_remaining = 0

    if min_price_ahead == today_cost[now_hour]:
        return scan_hours_remaining == 0 or min_price_ahead <= (
            max_price_ahead * HIGH_WATER_TAKEOUT_LIKELYHOOD
        )
    return False


def get_cheap_score_until(now_hour, until_hour, today_cost):
    """
    Give the cheapest MAX_HOURS_NEEDED_TO_HEAT a decreasing score
    that can be used to calculate heating curve.
    Scoring considders ramping vs aggressive heating to cheapest, as well as
    moving completion hour if needed and heating for total of MAX_HOURS_NEEDED_TO_HEAT
    """
    now_price = today_cost[now_hour]
    cheapest_hour = NORMAL_HOURS_NEEDED_TO_HEAT  # Secure sufficient rampup time
    cheapest_price_sum = sum(today_cost[0:NORMAL_HOURS_NEEDED_TO_HEAT])
    score = MAX_HOURS_NEEDED_TO_HEAT  # Assume now_hour is cheapest
    if now_hour > until_hour:
        score = 0
    delay_msg = None
    for scan_hour in range(0, until_hour + 1):
        if today_cost[scan_hour] < now_price:
            score -= 1
        if scan_hour > NORMAL_HOURS_NEEDED_TO_HEAT:
            scan_price_sum = sum(
                today_cost[(scan_hour - NORMAL_HOURS_NEEDED_TO_HEAT) : scan_hour]
            )
            delay_saving = cheapest_price_sum - scan_price_sum
            if delay_saving >= 0:
                delay_msg = (
                    f"Delaying heatup to {scan_hour}:00 saves {delay_saving} EUR"
                )
                cheapest_price_sum = scan_price_sum
                cheapest_hour = scan_hour

    if now_price < today_cost[cheapest_hour]:
        score = MAX_HOURS_NEEDED_TO_HEAT  # If delayed still heat aggressive now

    if now_hour <= cheapest_hour:
        if delay_msg is not None:
            print(delay_msg)
        # Secure rampup before boost end
        score = max(score, MAX_HOURS_NEEDED_TO_HEAT - (cheapest_hour - now_hour))
    print(f"Score given: {score}")
    return max(score, 0)


def get_cheap_score_relative_tomorrow(this_hour_cost, tomorrow_morning_cost):
    score = 0
    for cheap_tomorrow_cost in sorted(tomorrow_morning_cost)[
        0:MAX_HOURS_NEEDED_TO_HEAT
    ]:
        if this_hour_cost < cheap_tomorrow_cost:
            score += 1
    return score


def cheap_later_test(today_cost, scan_from, scan_to, test_hour):
    min_price = today_cost[scan_from]
    for i in range(scan_from + 1, scan_to):
        if today_cost[i] <= min_price:
            min_price = today_cost[i]
            if i > test_hour:
                return True  # Found price to be cheaper later
    return False


def is_now_chepeast_remaining_during_comfort(today_cost, local_hour):
    return not cheap_later_test(
        today_cost, local_hour, DAILY_COMFORT_LAST_H, local_hour
    )


def hours_to_next_lower_price(today_cost, scan_from):
    min_price = today_cost[scan_from]
    for i in range(scan_from + 1, DAILY_COMFORT_LAST_H):
        if today_cost[i] <= min_price:
            return i - scan_from
    return 0


def next_night_is_cheaper(today_cost):
    return cheap_later_test(today_cost, 0, 24, DAILY_COMFORT_LAST_H)


def is_the_cheapest_hour_during_daytime(today_cost):
    return cheap_later_test(today_cost, 0, DAILY_COMFORT_LAST_H, LAST_MORNING_HEATING_H)


def get_optimized_temp(local_hour, today_cost, tomorrow_cost, outside_temp):
    wanted_temp = MIN_NUDGABLE_TEMP if local_hour <= DAILY_COMFORT_LAST_H else MIN_TEMP
    print(f"{local_hour}:00 the hour cost is {today_cost[local_hour]} EUR / kWh")
    if MAX_HOURS_NEEDED_TO_HEAT <= local_hour <= DAILY_COMFORT_LAST_H:
        if today_cost[local_hour] < HIGH_PRICE_THRESHOLD:
            wanted_temp += 5  # Slightly raise hot water takeout capacity
        if local_hour < 23 and today_cost[local_hour] < today_cost[local_hour + 1]:
            if is_now_chepeast_remaining_during_comfort(today_cost, local_hour):
                wanted_temp += DEGREES_LOST_PER_H * (
                    DAILY_COMFORT_LAST_H
                    - local_hour  # Better trigger further low temp heating now compared to later
                )
            else:
                # Compensate for later heat leaks
                wanted_temp += hours_to_next_lower_price(today_cost, local_hour)
    if tomorrow_cost is not None:
        if local_hour > DAILY_COMFORT_LAST_H:
            wanted_temp += DEGREES_PER_H * get_cheap_score_relative_tomorrow(
                today_cost[local_hour], tomorrow_cost[0:LAST_MORNING_HEATING_H]
            )
    if is_the_cheapest_hour_during_daytime(today_cost):
        wanted_temp += DEGREES_PER_H * get_cheap_score_until(
            local_hour, DAILY_COMFORT_LAST_H, today_cost
        )
    if local_hour <= LAST_MORNING_HEATING_H:
        wanted_temp += DEGREES_PER_H * get_cheap_score_until(
            local_hour, LAST_MORNING_HEATING_H, today_cost
        )
        if is_the_cheapest_hour_during_daytime(today_cost):
            wanted_temp = min(
                wanted_temp, MIN_DAILY_TEMP - DEGREES_PER_H
            )  # limit morning heating much if daytime heating is cheap
        elif next_night_is_cheaper(today_cost):
            wanted_temp = min(MIN_DAILY_TEMP, wanted_temp)  # limit morning heating

    if outside_temp < HEAT_LEAK_VALUE_THRESHOLD and heat_leakage_loading_desired(
        local_hour, today_cost, tomorrow_cost, outside_temp
    ):
        wanted_temp += DEGREES_PER_H  # Extra boost since heat leakage is valuable

    return wanted_temp


def get_wanted_temp(local_hour, weekday, today_cost, tomorrow_cost, outside_temp):
    wanted_temp = get_optimized_temp(
        local_hour, today_cost, tomorrow_cost, outside_temp
    )

    if weekday in WEEKDAYS_WITH_EXTRA_TAKEOUT and local_hour <= DAILY_COMFORT_LAST_H:
        wanted_temp += 5

    if (
        weekday in WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT
        and local_hour <= LAST_MORNING_HEATING_H
    ):
        wanted_temp += 5

    if (
        refill_heating_worth_while(local_hour, today_cost, tomorrow_cost)
        or MAX_HOURS_NEEDED_TO_HEAT <= local_hour <= LAST_MORNING_HEATING_H
    ):
        wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)

    if today_cost[local_hour] >= sorted(today_cost)[24 - NUM_MOST_EXPENSIVE_HOURS]:
        wanted_temp = MIN_NUDGABLE_TEMP  # Min temp during most expensive hours in day

    if local_hour > (LAST_MORNING_HEATING_H - NORMAL_HOURS_NEEDED_TO_HEAT):
        wanted_temp = min(wanted_temp, MIN_DAILY_TEMP)  # Limit heating last hours

    return wanted_temp


def get_local_date_and_hour(utc_unix_timestamp):
    local_unix_timestamp = utc_unix_timestamp + UTC_OFFSET_IN_S
    now = time.gmtime(local_unix_timestamp)
    year = now[0]
    dst_start = time.mktime(
        (year, 3, (31 - (int(5 * year / 4 + 1)) % 7), 1, 0, 0, 0, 0, 0)
    )
    dst_end = time.mktime(
        (year, 10, (31 - (int(5 * year / 4 + 1)) % 7), 1, 0, 0, 0, 0, 0)
    )
    if dst_start < local_unix_timestamp < dst_end:
        now = time.gmtime(local_unix_timestamp + 3600)
    adjusted_day = date(now[0], now[1], now[2])

    return (adjusted_day, now[3])


def run_hotwater_optimization(thermostat):
    setup_wifi()
    time_provider = TimeProvider()
    time_provider.sync_utc_time()

    today = None
    today_cost = None
    tomorrow_cost = None
    days_since_legionella = 0
    pending_legionella_reset = False
    temperature_provider = SimpleTemperatureProvider()

    while True:
        new_today, local_hour = get_local_date_and_hour(
            time_provider.get_utc_unix_timestamp()
        )
        if today_cost is None or new_today != today:
            today = new_today
            if pending_legionella_reset:
                days_since_legionella = 0
                pending_legionella_reset = False
            days_since_legionella += 1
            today_cost = get_cost(today)
            if today_cost is None:
                raise RuntimeError("Optimization not possible")
            tomorrow_cost = None
        if tomorrow_cost is None:
            tomorrow_cost = get_cost(today + timedelta(days=1))

        print(
            f"Cost optimizing for {today.day} / {today.month} {today.year} {local_hour}:00"
        )
        outside_temp = temperature_provider.get_outdoor_temp()
        wanted_temp = get_wanted_temp(
            local_hour,
            today.weekday(),
            today_cost,
            tomorrow_cost,
            outside_temp,
        )
        if days_since_legionella > LEGIONELLA_INTERVAL and (
            (LAST_MORNING_HEATING_H - 2) <= local_hour <= LAST_MORNING_HEATING_H
        ):  # Secure legionella temperature gets reached
            wanted_temp = max(wanted_temp, MIN_LEGIONELLA_TEMP)
        print(f"{local_hour}:00 thermostat @ {wanted_temp}. Outside is {outside_temp}")
        if wanted_temp >= MIN_LEGIONELLA_TEMP:
            pending_legionella_reset = True

        thermostat.set_thermosat(wanted_temp)
        curr_min = time.localtime()[4]
        if curr_min < 50 and OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            time.sleep((50 - curr_min) * SEC_PER_MIN)  # Sleep slightly before next hour
        if local_hour < 23 and today_cost is not None:
            next_hour_wanted_temp = get_wanted_temp(
                local_hour + 1,
                today.weekday(),
                today_cost,
                tomorrow_cost,
                outside_temp,
            )
            if (
                next_hour_wanted_temp >= wanted_temp
                and today_cost[local_hour + 1] < today_cost[local_hour]
            ):
                thermostat.nudge_down()
            if (
                next_hour_wanted_temp <= wanted_temp
                and today_cost[local_hour + 1] > today_cost[local_hour]
            ):
                thermostat.nudge_up()

        time_provider.hourly_timekeeping()
        if OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            time.sleep(12 * SEC_PER_MIN)  # Sleep slightly into next hour


if __name__ == "__main__":
    if "Pico W" in sys.implementation._machine:
        THERMOSTAT = Thermostat()
        if machine.reset_cause() == machine.PWRON_RESET:
            print("Boosting...")
            THERMOSTAT.set_thermosat(MIN_LEGIONELLA_TEMP)
            time.sleep(EXTRA_HOT_DURATION_S)
        ATTEMTS_REMAING_BEFORE_RESET = MAX_NETWORK_ATTEMPTS
        while ATTEMTS_REMAING_BEFORE_RESET > 0:
            try:
                run_hotwater_optimization(THERMOSTAT)
            except Exception as EXCEPT:
                print("Delaying due to exception...")
                print(EXCEPT)
                WLAN = network.WLAN(network.STA_IF)
                print(f"rssi = {WLAN.status('rssi')}")
                time.sleep(300)
                ATTEMTS_REMAING_BEFORE_RESET -= 1
        machine.reset()
