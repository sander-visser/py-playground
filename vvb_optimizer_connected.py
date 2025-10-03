"""
Hot water system (HWS) scheduler to run resistive heating on cheap hours.
Runs on a Raspberry Pi PICO(2) W(H) with a SG90 servo connected to PWM GP0
Power takeout for servo possible from VBUS pin if USB powered.
Designed for WiFi use. Servo connected to theromostat of the HWS.
Upload to device from a PC using Thonny (as main.py).
Logs are made available via built in webserver that also allows override.

If USB only powered connect VBUS and VSYS for cleaner power (better WiFi),
WiFi range improves with ground connection and good placement.
First SG90 micro servo 9g lasted for 30 months before gears siezed.

Power reset unit to easily get 1h extra hot water.
vvb_optimizer_connected.png show price/target temperature behaviour.
12 months savings in Sweden SE3 ammount to approx 150 EUR.

Repo also contans a schedule based optimizer (that does not utilize WiFi)

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both ~500 SEK to shop gadgets with. Contact: github[a]visser.se or
check referral.link in this repo.
"""

# Install micropyton (hold BOOTSEL while connecting USB to get drive mounted),
# or execute "machine.bootloader()" to upgrade existing MicroPython board.
# Copy UF2 file to the USB drive:
# Pico W: https://micropython.org/download/RPI_PICO_W/RPI_PICO_W-latest.uf2
# Pico2 W: https://micropython.org/download/RPI_PICO2_W/RPI_PICO2_W-latest.uf2
# then (when WiFi connected, and before saving this script as main.py)
# import network
# wlan = network.WLAN(network.STA_IF)
# wlan.active(True)
# wlan.connect("WLAN_SSID", "WLAN_PASS")  # must not be pure WPA3
# import mip
# mip.install("requests")
# mip.install("datetime")


import asyncio
import sys
import time
import gc
import io
from datetime import date, timedelta
from machine import Pin, PWM
import rp2
import network
import ntptime
import requests


# https://www.raspberrypi.com/documentation/pico-sdk/networking.html#country-codes
PR2_COUNTRY = "SE"
WLAN_SSID = "your ssid"
WLAN_PASS = "your pass"  # must not be pure WPA3
NORDPOOL_AREA = "SE3"
NTP_HOST = "se.pool.ntp.org"
NTP_INTERVAL_H = 12
MAX_CLOCK_DRIFT_S = 10
SEC_PER_MIN = 60
EXTRA_HOT_DURATION_S = 60 * SEC_PER_MIN  # MIN_LEGIONELLA_TEMP duration after POR
OVERRIDE_UTC_UNIX_TIMESTAMP = None  # -3600 to Simulate script behaviour from 1h ago
MAX_NETWORK_ATTEMPTS = 10
UTC_OFFSET_IN_S = 3600
COP_FACTOR = 2.5  # Utilize leakage unless heatpump will be cheaper
HIGH_WATER_TAKEOUT_LIKELYHOOD = (
    0.2  # Percent chance that thermostat will heat at MIN_TEMP setting during max price
)
HEAT_LEAK_VALUE_THRESHOLD = 10
EXTREME_COLD_THRESHOLD = -8  # Heat leak always valuable
MAX_HOURS_NEEDED_TO_HEAT = (
    4  # Should exceed (MIN_DAILY_TEMP - MIN_TEMP) / DEGREES_PER_H
)
NORMAL_HOURS_NEEDED_TO_HEAT = MAX_HOURS_NEEDED_TO_HEAT - 1
NUM_MOST_EXPENSIVE_HOURS = 3  # Avoid heating
AMBIENT_TEMP = 20
DEGREES_PER_H = 9.4  # Nibe 300-CU ER56-CU 275L with 3kW
HEATER_KW = 3
HEAT_LOSS_PER_DAY_KWH = 2.8  # Leakage when not at home
DEGREES_LOST_PER_H = round(
    ((HEAT_LOSS_PER_DAY_KWH / HEATER_KW) * DEGREES_PER_H) / 24, 2
)
LAST_MORNING_HEATING_H = 6  # :59
LAST_WEEKEND_MORNING_HEATING_H = 8  # :59
FIRST_EVENING_HIGH_TAKEOUT_H = 20  # : 00 - by which time re-heating should have ran
DAILY_COMFORT_LAST_H = 21  # :59
NEW_PRICE_EXPECTED_HOUR = 12
NEW_PRICE_EXPECTED_MIN = 45
MAX_TEMP = 78
MIN_TEMP = 28
MIN_USABLE_TEMP = 35  # Good for hand washing, and one hour away from shower temp
MIN_DAILY_TEMP = 50
MIN_LEGIONELLA_TEMP = 65
LEGIONELLA_INTERVAL = 10  # In days
WEEKDAYS_WITH_EXTRA_TAKEOUT = [0, 2, 4, 6]  # 6 == Sunday
WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT = [0, 3]  # 0 == Monday
KWH_PER_MWH = 1000
BASE_COST = 0.747 / 11.0  # In EUR for tax and transfer costs (wo VAT)
HIGH_PRICE_THRESHOLD = 0.15  # In EUR (incl BASE_COST)
ACCEPTABLE_PRICING_ERROR = 0.003  # In EUR - how far from cheapest considder same
LOW_PRICE_VARIATION_PERCENT = 1.1  # Limit storage temp if just 10% cheaper
PRICE_API_URL = (
    "https://dataportal-api.nordpoolgroup.com/api/DayAheadPriceIndices?"
    + "market=DayAhead&currency=EUR&resolutionInMinutes=15&indexNames="
)
# TEMPERATURE_URL should return a number "x.y" for outside degrees C
TEMPERATURE_URL = (
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp"
)

PWM_28_DEGREES = 1575  # Min rotation (@MIN_TEMP)
PWM_78_DEGREES = 9000  # Max rotation (@MAX_TEMP)
PWM_PER_DEGREE = (PWM_78_DEGREES - PWM_28_DEGREES) / 50
ROTATION_SECONDS = 1.5


def log_print(*args, **kwargs):
    global last_log

    if len(last_log) > 125:
        last_log.del(0:5)
        gc.collect()

    log_str = io.StringIO()
    print("".join(map(str, args)), file=log_str, **kwargs)
    last_log.append(log_str.getvalue())
    print(f"   {log_str.getvalue().rstrip()}")


class SimpleTemperatureProvider:
    def __init__(self):
        self.outdoor_temperature = 0
        self.last_update = None

    def get_outdoor_temp(self):
        if self.last_update is not None and (self.last_update + 600) > time.time():
            return self.outdoor_temperature
        try:
            outdoor_temperature_req = requests.get(TEMPERATURE_URL, timeout=10.0)
            if outdoor_temperature_req.status_code == 200:
                try:
                    self.outdoor_temperature = float(outdoor_temperature_req.text)
                    self.last_update = time.time()
                except ValueError:
                    log_print(
                        f"Ignored {outdoor_temperature_req.text} from {TEMPERATURE_URL}"
                    )
        except OSError as req_err:
            if req_err.args[0] == 110:  # ETIMEDOUT
                log_print("Ignoring temperature read timeout")
            else:
                raise req_err
        gc.collect()
        return self.outdoor_temperature


class TimeProvider:
    def __init__(self):
        ntptime.host = NTP_HOST
        self.last_sync_time = time.time()
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
            if (time.time() - self.last_sync_time) > (NTP_INTERVAL_H * 3600):
                self.sync_utc_time()  # Attempt sync time twice per day
                self.last_sync_time = time.time()

    def get_local_date_and_time(self):
        local_unix_timestamp = UTC_OFFSET_IN_S + (
            time.time() if self.current_utc_time is None else self.current_utc_time
        )
        now = time.gmtime(local_unix_timestamp)
        year = now[0]
        dst_start = time.mktime(
            (year, 3, (31 - (int(5 * year / 4 + 4)) % 7), 1, 0, 0, 0, 0, 0)
        )
        dst_end = time.mktime(
            (year, 10, (31 - (int(5 * year / 4 + 1)) % 7), 1, 0, 0, 0, 0, 0)
        )
        if dst_start < local_unix_timestamp < dst_end:
            now = time.gmtime(local_unix_timestamp + 3600)
        adjusted_day = date(now[0], now[1], now[2])

        return (adjusted_day, now[3], now[4])

    @staticmethod
    def sync_utc_time():
        max_wait = MAX_NETWORK_ATTEMPTS
        while max_wait > 0:
            try:
                log_print(f"Local time before NTP sync：{time.localtime()}")
                ntptime.settime()
                log_print(f"UTC   time after  NTP sync：{time.localtime()}")
                break
            except Exception as excep:
                log_print(f"Time sync error: {excep}")
                time.sleep(1)
                max_wait -= 1


class Thermostat:
    def __init__(self):
        self.pwm = PWM(Pin(0))
        self.pwm.freq(50)
        self.prev_degrees = None
        self.overridden = False

    @staticmethod
    def get_pwm_degrees(degrees):
        pwm_degrees = PWM_28_DEGREES
        if degrees > MIN_TEMP:
            pwm_degrees += (degrees - MIN_TEMP) * PWM_PER_DEGREE
        return min(pwm_degrees, PWM_78_DEGREES)

    def set_thermostat(self, degrees, override=False):
        self.overridden = override
        degrees = max(degrees, MIN_TEMP)
        degrees = min(degrees, MAX_TEMP)
        if self.prev_degrees != degrees:
            self.prev_degrees = degrees
            self.pwm.duty_u16(int(self.get_pwm_degrees(degrees)))
            time.sleep(ROTATION_SECONDS)
            self.pwm.duty_u16(0)


class CostProvider:
    def __init__(self):
        self.today = None
        self.tomorrow = None
        self.tomorrow_final = None

    def is_tomorrow_final(self):
        return self.tomorrow_final is not None and self.tomorrow_final

    def transition_day(self):
        self.today = self.tomorrow
        self.tomorrow = None
        self.tomorrow_final = None

    async def get_cost(self, end_date, as_today):
        price_api_url = PRICE_API_URL + (
            f"{NORDPOOL_AREA}&date={end_date.year}-{end_date.month}-{end_date.day}"
        )
        gc.collect()
        try:
            result = requests.get(price_api_url, timeout=10.0)
            if result.status_code != 200:
                await asyncio.sleep(1 * SEC_PER_MIN)  # Delay retry
                return None
            json_result = result.json()
        except OSError:
            await asyncio.sleep(1 * SEC_PER_MIN)  # Delay retry
            return None
        gc.collect()
        cost_array = []
        hourly_price = {"avg": 0.0, "quartely": []}
        for row in json_result["multiIndexEntries"]:
            curr_price = row["entryPerArea"][NORDPOOL_AREA] / KWH_PER_MWH + BASE_COST
            hourly_price["avg"] += curr_price / 4
            hourly_price["quartely"].append(curr_price)
            if len(hourly_price["quartely"]) == 4:
                cost_array.append(hourly_price)
                hourly_price = {"avg": 0.0, "quartely": []}
        if len(json_result["areaStates"]) == 0 or len(cost_array) == 0:
            if OVERRIDE_UTC_UNIX_TIMESTAMP is None:
                await asyncio.sleep(1 * SEC_PER_MIN)  # Delay retry
            return None
        if len(cost_array) == 23:
            cost_array.append(cost_array[0])  # DST hack - off by one in adjust days
        price_is_final = json_result["areaStates"][0]["state"] == "Final"
        if as_today:
            self.today = cost_array
        else:
            self.tomorrow = cost_array
        json_result = None
        gc.collect()
        return price_is_final


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
        log_print("waiting for connection...")
        time.sleep(1)

    if wlan.status() != 3:  # Handle connection error
        for wlans in wlan.scan():
            log_print(f"Seeing SSID {wlans[0]} with rssi {wlans[3]}")
        raise RuntimeError("network connection failed")

    log_print(f"Connected with rssi {wlan.status('rssi')} and IP {wlan.ifconfig()[0]}")


def heat_leakage_loading_desired(local_hour, cost, outdoor_temp):
    now_price = cost.today[local_hour]["avg"]
    max_price = now_price
    min_price = now_price
    while local_hour < 23:
        local_hour += 1
        max_price = max(max_price, cost.today[local_hour]["avg"])
        min_price = min(min_price, cost.today[local_hour]["avg"])
    if cost.tomorrow is not None:
        for tomorrow_hour_price in cost.tomorrow:
            max_price = max(max_price, tomorrow_hour_price["avg"])
    if (outdoor_temp <= EXTREME_COLD_THRESHOLD) or max_price > (min_price * COP_FACTOR):
        log_print(f"Extra heating due to COP? now: {now_price}. min: {min_price}")
        return now_price <= (min_price + ACCEPTABLE_PRICING_ERROR)
    return False


def now_is_cheap_in_forecast(now_hour, cost):
    """
    Scan 16h ahead and check if now is the best time to buffer some comfort
    """
    scan_hours_remaining = 16
    hours_til_cheaper = 0
    max_price_ahead = cost.today[now_hour]["avg"]
    max_price_til_next_cheap = max_price_ahead
    min_price_ahead = max_price_ahead
    for scan_hour in range(now_hour, min(24, now_hour + scan_hours_remaining)):
        scan_hours_remaining -= 1
        max_price_ahead = max(max_price_ahead, cost.today[scan_hour]["avg"])
        min_price_ahead = min(min_price_ahead, cost.today[scan_hour]["avg"])
        if hours_til_cheaper == 0 and cheap_later_test(
            cost, now_hour, scan_hour, now_hour
        ):
            hours_til_cheaper = 15 - scan_hours_remaining
            max_price_til_next_cheap = max_price_ahead

    if cost.tomorrow is not None:
        for scan_hour in range(0, scan_hours_remaining):
            max_price_ahead = max(max_price_ahead, cost.tomorrow[scan_hour]["avg"])
            if cost.tomorrow[scan_hour]["avg"] <= min_price_ahead:
                min_price_ahead = cost.tomorrow[scan_hour]["avg"]
                if hours_til_cheaper == 0:
                    hours_til_cheaper = (24 - now_hour) + scan_hour
                    max_price_til_next_cheap = max_price_ahead
        scan_hours_remaining = 0

    if (min_price_ahead + ACCEPTABLE_PRICING_ERROR) >= (cost.today[now_hour]["avg"]):
        if scan_hours_remaining == 0 and hours_til_cheaper == 0:
            return True  # This very cheapest time to heat
        if 1 <= hours_til_cheaper <= 2:
            return False  # Can wait two more hours for cheaper price
        # Check if long time til next cheap period, or if high price spikes pending
        return (
            scan_hours_remaining == 0
            and (DEGREES_LOST_PER_H * hours_til_cheaper)
            > DEGREES_PER_H * HIGH_WATER_TAKEOUT_LIKELYHOOD
        ) or min_price_ahead <= (
            max_price_til_next_cheap * HIGH_WATER_TAKEOUT_LIKELYHOOD
        )
    return False


def summarize_cost(hours_to_summarize):
    cost_sum = 0.0
    for hour in hours_to_summarize:
        cost_sum += hour["avg"]
    return cost_sum


def get_delay_score(until_hour, cost, now_price):
    cheap_hours = cost.today[0:NORMAL_HOURS_NEEDED_TO_HEAT]
    heat_end_hour = NORMAL_HOURS_NEEDED_TO_HEAT
    cheapest_price_sum = summarize_cost(cheap_hours)
    score = MAX_HOURS_NEEDED_TO_HEAT  # Assume now_hour is cheapest
    delay_msg = None
    for scan_hour in range(0, until_hour + 1):
        if cost.today[scan_hour]["avg"] < now_price:
            score -= 1
        if scan_hour > NORMAL_HOURS_NEEDED_TO_HEAT:
            scan_price_sum = summarize_cost(
                cost.today[(scan_hour - NORMAL_HOURS_NEEDED_TO_HEAT) : scan_hour]
            )
            delay_saving = (
                cheapest_price_sum + ACCEPTABLE_PRICING_ERROR - scan_price_sum
            )
            if delay_saving >= 0:
                delay_msg = f"Delaying heatup end to {scan_hour}:00 saves {delay_saving:.6f} EUR"
                cheapest_price_sum = scan_price_sum
                cheap_hours = cost.today[
                    (scan_hour - NORMAL_HOURS_NEEDED_TO_HEAT) : scan_hour
                ]
                heat_end_hour = scan_hour
    return (score, delay_msg, cheap_hours, heat_end_hour)


def get_cheap_score_until(now_hour, until_hour, cost, verbose):
    """
    Give the cheapest MAX_HOURS_NEEDED_TO_HEAT a decreasing score
    that can be used to calculate heating curve.
    Scoring considders ramping vs aggressive heating to cheapest, as well as
    moving completion hour if needed and heating for total of MAX_HOURS_NEEDED_TO_HEAT
    """
    now_price = cost.today[now_hour]["avg"]
    score, delay_msg, cheap_hours, heat_end_hour = get_delay_score(
        until_hour, cost, now_price
    )

    if (now_price + ACCEPTABLE_PRICING_ERROR) <= sorted(
        cheap_hours, key=lambda h: h["avg"]
    )[NORMAL_HOURS_NEEDED_TO_HEAT - 1]["avg"]:
        if heat_end_hour - now_hour > NORMAL_HOURS_NEEDED_TO_HEAT:
            # If late cheap hour preheat somewhat now
            score = max(score, 1)
        else:
            # If delayed still heat aggressive now
            score = MAX_HOURS_NEEDED_TO_HEAT

    if (
        now_price
        <= sorted(cheap_hours, key=lambda h: h["avg"])[NORMAL_HOURS_NEEDED_TO_HEAT - 1][
            "avg"
        ]
    ):
        if verbose and delay_msg is not None and until_hour >= now_hour:
            log_print(delay_msg)
        # Secure correct score inside boost period (with late peak favored)
        min_score = 1
        for cheap_price_route in cheap_hours:
            if (now_price + ACCEPTABLE_PRICING_ERROR) <= cheap_price_route["avg"]:
                min_score += 1
        if (
            now_price
            <= (
                sorted(cheap_hours, key=lambda h: h["avg"])[0]["avg"]
                + ACCEPTABLE_PRICING_ERROR
            )
        ) and heat_end_hour == (now_hour + 1):
            min_score = MAX_HOURS_NEEDED_TO_HEAT
        # Secure rampup before boost end
        score = max(score, min_score)
        if (now_hour < heat_end_hour) and (
            heat_end_hour - now_hour
        ) < MAX_HOURS_NEEDED_TO_HEAT:
            score = max(score, MAX_HOURS_NEEDED_TO_HEAT - (heat_end_hour - now_hour))

    if now_hour > until_hour:
        score = 0

    if verbose:
        log_print(
            f"Score for {now_hour}-{now_hour+1} is {score} (until {until_hour}:59)"
        )
    return max(score, 0)


def get_cheap_score_relative_future(this_hour_cost, future_cost):
    score = 0
    for cheap_future_cost in sorted(future_cost, key=lambda h: h["avg"])[
        0:MAX_HOURS_NEEDED_TO_HEAT
    ]:
        if this_hour_cost < cheap_future_cost["avg"]:
            score += 1
    return score


def cheap_later_test(cost, scan_from, scan_to, test_hour):
    extra_kwh_loss_per_hour_of_pre_heat = (
        (DEGREES_PER_H + MIN_DAILY_TEMP - AMBIENT_TEMP)
        / (MIN_DAILY_TEMP - AMBIENT_TEMP)
        - 1
    ) * (HEAT_LOSS_PER_DAY_KWH / 24)
    min_compensated_cost = cost.today[scan_from]["avg"] * (
        HEATER_KW + (test_hour - scan_from) * extra_kwh_loss_per_hour_of_pre_heat
    )

    for i in range(scan_from + 1, scan_to):
        if i <= test_hour:
            compensated_cost = cost.today[i]["avg"] * (
                HEATER_KW + (test_hour - i) * extra_kwh_loss_per_hour_of_pre_heat
            )
            min_compensated_cost = min(compensated_cost, min_compensated_cost)
        else:
            compensated_cost = cost.today[i]["avg"] * (
                HEATER_KW - (i - test_hour) * extra_kwh_loss_per_hour_of_pre_heat
            )  # Less energy is used when load is heated later than test_hour
            if compensated_cost <= min_compensated_cost:
                log_print(
                    f"Within {scan_from}-{scan_to}: Wait worth while {test_hour} -> {i}"
                )
                return True  # Found cost to be cheaper if heating later
    return False


def is_now_cheapest_remaining_during_comfort(cost, local_hour):
    return not cheap_later_test(cost, local_hour, DAILY_COMFORT_LAST_H, local_hour)


def hours_to_next_lower_price(cost, scan_from):
    min_price = cost.today[scan_from]["avg"]
    for i in range(scan_from + 1, DAILY_COMFORT_LAST_H):
        if cost.today[i]["avg"] <= min_price:
            return i - scan_from
    return 0


def is_the_cheapest_hour_during_daytime(cost, last_morning_heating_h):
    return cheap_later_test(cost, 0, DAILY_COMFORT_LAST_H, last_morning_heating_h)


def is_now_significantly_cheaper(now_hour, cost):
    """
    Scan 16h ahead and check if now is significantly cheaper than max price ahead
    """
    scan_hours_remaining = 16
    max_price_ahead = cost.today[now_hour]["avg"]
    for scan_hour in range(now_hour, min(24, now_hour + scan_hours_remaining)):
        scan_hours_remaining -= 1
        max_price_ahead = max(max_price_ahead, cost.today[scan_hour]["avg"])

    if cost.tomorrow is not None:
        for scan_hour in range(0, scan_hours_remaining):
            max_price_ahead = max(max_price_ahead, cost.tomorrow[scan_hour]["avg"])

    return cost.today[now_hour]["avg"] * LOW_PRICE_VARIATION_PERCENT < max_price_ahead


def get_last_morning_heating_h(weekday):
    if weekday in (5, 6):
        return LAST_WEEKEND_MORNING_HEATING_H
    return LAST_MORNING_HEATING_H


def add_scorebased_wanted_temperature(
    optimization_date, cost, outside_temp, wanted_temp, verbose
):
    local_hour = optimization_date["hour"]
    weekday = optimization_date["weekday"]
    last_morning_heating_h = get_last_morning_heating_h(weekday)
    score_based_heating = 0
    max_temp_limit = MAX_TEMP
    if local_hour <= last_morning_heating_h:
        score_based_heating = get_cheap_score_until(
            local_hour, last_morning_heating_h, cost, verbose
        )
        if is_the_cheapest_hour_during_daytime(cost, last_morning_heating_h):
            # Limit morning heating much if daytime heating is cheap
            max_temp_limit = MIN_DAILY_TEMP + (last_morning_heating_h - local_hour)
        elif cheap_later_test(cost, 0, 24, DAILY_COMFORT_LAST_H):
            max_temp_limit = MIN_DAILY_TEMP + DEGREES_PER_H  # Next night is cheap

    if local_hour <= DAILY_COMFORT_LAST_H:
        score_based_heating = max(
            score_based_heating,
            get_cheap_score_until(local_hour, DAILY_COMFORT_LAST_H, cost, verbose),
        )

    if not is_now_significantly_cheaper(local_hour, cost):
        max_temp_limit = MIN_DAILY_TEMP  # Restrict heating if only slightly cheaper

    if cost.tomorrow is not None:
        preload_score = get_cheap_score_relative_future(
            cost.today[local_hour]["avg"],
            cost.today[local_hour:23] + cost.tomorrow[0:last_morning_heating_h],
        )
        if preload_score > 0:
            score_based_heating = max(score_based_heating, preload_score)
            if cheap_later_test(cost, local_hour, 24, FIRST_EVENING_HIGH_TAKEOUT_H):
                max_temp_limit = MIN_DAILY_TEMP + DEGREES_PER_H
        else:
            # Long cheap period is ahead
            max_temp_limit = MIN_DAILY_TEMP + DEGREES_PER_H
        if weekday not in WEEKDAYS_WITH_EXTRA_TAKEOUT:
            max_temp_limit -= DEGREES_PER_H  # Avoid storing long time if low takeout

    max_score = MAX_HOURS_NEEDED_TO_HEAT
    if outside_temp < HEAT_LEAK_VALUE_THRESHOLD and heat_leakage_loading_desired(
        local_hour, cost, outside_temp
    ):
        score_based_heating += 1  # Extra boost since heat leakage is valuable
        max_score += 1
        max_temp_limit = MAX_TEMP

    overshoot_offset = wanted_temp + (DEGREES_PER_H * max_score) - max_temp_limit
    wanted_temp += score_based_heating * DEGREES_PER_H
    if wanted_temp > MIN_USABLE_TEMP and overshoot_offset > 0:
        wanted_temp -= overshoot_offset
        wanted_temp = max(MIN_USABLE_TEMP, wanted_temp)

    return wanted_temp


def get_wanted_temp_boost(local_hour, weekday, cost):
    wanted_temp_boost = 0
    if weekday in WEEKDAYS_WITH_EXTRA_TAKEOUT and local_hour < DAILY_COMFORT_LAST_H:
        wanted_temp_boost += 5

    if (
        weekday in WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT
        and local_hour <= get_last_morning_heating_h(weekday)
    ):
        wanted_temp_boost += 5

    if MAX_HOURS_NEEDED_TO_HEAT <= local_hour < DAILY_COMFORT_LAST_H:
        if cost.today[local_hour]["avg"] < HIGH_PRICE_THRESHOLD:
            wanted_temp_boost += 5  # Slightly raise hot water takeout capacity
        if (
            local_hour < 23
            and cost.today[local_hour]["avg"] < cost.today[local_hour + 1]["avg"]
        ):
            hours_to_bridge = hours_to_next_lower_price(cost, local_hour)
            if is_now_cheapest_remaining_during_comfort(cost, local_hour):
                hours_to_bridge = 1 + (DAILY_COMFORT_LAST_H - local_hour)
            # Better heat now rather than later
            wanted_temp_boost += DEGREES_LOST_PER_H * hours_to_bridge

    return wanted_temp_boost


def get_wanted_temp(
    optimization_date,
    cost,
    outside_temp,
    alarm_fully_armed,
    verbose,
):
    local_hour = optimization_date["hour"]
    weekday = optimization_date["weekday"]
    wanted_temp = MIN_TEMP

    if not alarm_fully_armed:
        wanted_temp += get_wanted_temp_boost(local_hour, weekday, cost)
    elif MAX_HOURS_NEEDED_TO_HEAT <= local_hour < DAILY_COMFORT_LAST_H:
        if is_now_cheapest_remaining_during_comfort(cost, local_hour):
            wanted_temp += (
                1 + (DAILY_COMFORT_LAST_H - local_hour)
            ) * DEGREES_LOST_PER_H
    gc.collect()  # Avoid fragmentation after alarm API use

    wanted_temp = add_scorebased_wanted_temperature(
        optimization_date,
        cost,
        outside_temp,
        wanted_temp,
        verbose,
    )

    last_morning_heating_h = get_last_morning_heating_h(weekday)

    if MAX_HOURS_NEEDED_TO_HEAT < local_hour <= last_morning_heating_h:
        if not cheap_later_test(
            cost, local_hour, FIRST_EVENING_HIGH_TAKEOUT_H, local_hour
        ):
            wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)  # Maintain morning heating
    if DAILY_COMFORT_LAST_H > local_hour > last_morning_heating_h and (
        MAX_HOURS_NEEDED_TO_HEAT - 1
    ) <= get_cheap_score_relative_future(
        cost.today[local_hour]["avg"],
        cost.today[last_morning_heating_h:DAILY_COMFORT_LAST_H],
    ):
        wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)  # Restore comfort once per day

    if (
        cost.today[local_hour]["avg"]
        >= sorted(cost.today, key=lambda h: h["avg"])[24 - NUM_MOST_EXPENSIVE_HOURS][
            "avg"
        ]
    ):
        wanted_temp = MIN_TEMP  # Min temp during most expensive hours in day

    if now_is_cheap_in_forecast(local_hour, cost):
        wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)
        if local_hour <= last_morning_heating_h:
            wanted_temp = max(wanted_temp, MIN_DAILY_TEMP + DEGREES_PER_H)

    return wanted_temp


async def quarterly_optimization(
    today,
    local_hour,
    wanted_temp,
    last_h_wanted_temp,
    cost,
    temperature_provider,
    alarm_status,
    thermostat,
):
    q_temps = []
    for q in range(0, 4):
        q_holdoff = 0
        for scan_q in range(q, 4):
            if (
                cost.today[local_hour]["quartely"][q]
                > cost.today[local_hour]["quartely"][scan_q]
            ):
                q_holdoff += 1
            if q != scan_q and (
                cost.today[local_hour]["quartely"][q]
                == cost.today[local_hour]["quartely"][scan_q]
            ):
                q_holdoff += 0.5
        q_temp = wanted_temp - (q_holdoff / 4) * DEGREES_PER_H
        if (
            local_hour > 0
            and min(cost.today[local_hour - 1]["quartely"])
            >= cost.today[local_hour]["quartely"][q]
        ):
            q_temp = max(q_temp, last_h_wanted_temp)
        q_temps.append(max(q_temp, MIN_TEMP))

    curr_min = time.localtime()[4]
    log_print(
        f"{curr_min}: Quarterly temps {q_temps} C @ "
        + f"{cost.today[local_hour]['quartely']} EUR"
    )
    for q in range(int(curr_min / 15), 4):  # loop the quarters and sub optimize
        curr_min = max(curr_min, time.localtime()[4])
        if q == 3 and local_hour < 23 and OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            next_hour_wanted_temp = get_wanted_temp(
                {"hour": local_hour + 1, "weekday": today.weekday()},
                cost,
                temperature_provider.get_outdoor_temp(),
                alarm_status is not None and alarm_status.is_fully_armed(),
                False,
            )
            if (
                next_hour_wanted_temp >= wanted_temp
                and cost.today[local_hour + 1]["quartely"][0]
                <= cost.today[local_hour]["quartely"][3]
            ):
                log_print("Lowering due to next is cheap")
                q_temps[q] = wanted_temp - DEGREES_PER_H / 4
            if (
                next_hour_wanted_temp <= wanted_temp
                and cost.today[local_hour + 1]["quartely"][0]
                > cost.today[local_hour]["quartely"][3]
            ):
                log_print("Boosting due to next is expensive")
                q_temps[q] = wanted_temp + DEGREES_PER_H / 4
        if q == 0 or not thermostat.overridden:
            thermostat.set_thermostat(q_temps[q])
        if OVERRIDE_UTC_UNIX_TIMESTAMP is None and q != 3:
            await asyncio.sleep(((15 * (1 + q)) - curr_min) * SEC_PER_MIN)


async def run_hotwater_optimization(thermostat, alarm_status, boost_req):
    cost = CostProvider()
    temperature_provider = SimpleTemperatureProvider()
    time_provider = TimeProvider()
    time_provider.sync_utc_time()
    today, local_hour, current_minute = time_provider.get_local_date_and_time()

    days_since_legionella = 0
    days_with_alarm_armed = 0
    peak_temp_today = 0
    last_h_wanted_temp = 0
    pending_legionella_reset = False
    today_final = await cost.get_cost(today, True)
    if today_final is None or not today_final:
        raise RuntimeError("Optimization not possible")

    if boost_req:
        log_print("Boosting...")
        thermostat.set_thermostat(MIN_LEGIONELLA_TEMP)
        await asyncio.sleep(EXTRA_HOT_DURATION_S)

    while True:
        await asyncio.sleep(0.1)
        alarm_fully_armed = alarm_status is not None and alarm_status.is_fully_armed()
        new_today, local_hour, current_minute = time_provider.get_local_date_and_time()
        if new_today != today:
            peak_temp_today = 0
            today = new_today
            if pending_legionella_reset:
                days_since_legionella = 0
                pending_legionella_reset = False
            days_since_legionella += 1
            days_with_alarm_armed += 1
            cost.transition_day()
        if not alarm_fully_armed:
            days_with_alarm_armed = 0

        if not cost.is_tomorrow_final() and (
            (local_hour > NEW_PRICE_EXPECTED_HOUR)
            or (
                local_hour == NEW_PRICE_EXPECTED_HOUR
                and current_minute >= NEW_PRICE_EXPECTED_MIN
            )
        ):
            cost.tomorrow_final = await cost.get_cost(today + timedelta(days=1), False)

        outside_temp = temperature_provider.get_outdoor_temp()
        log_print(
            f"Cost optimizing {today.day} / {today.month} {local_hour}:00 @"
            + f" {cost.today[local_hour]['avg']:.6f} EUR / kWh. Outside is {outside_temp} C."
        )
        wanted_temp = get_wanted_temp(
            {"hour": local_hour, "weekday": today.weekday()},
            cost,
            outside_temp,
            alarm_fully_armed,
            True,
        )
        last_morning_heating_h = get_last_morning_heating_h(today.weekday())
        if alarm_fully_armed:
            if days_with_alarm_armed > 3:
                wanted_temp = min(wanted_temp, MIN_DAILY_TEMP)
            wanted_temp = min(wanted_temp, MIN_LEGIONELLA_TEMP)
        if days_since_legionella > LEGIONELLA_INTERVAL and (
            (last_morning_heating_h - 2) <= local_hour <= last_morning_heating_h
        ):  # Secure legionella temperature gets reached
            wanted_temp = max(wanted_temp, MIN_LEGIONELLA_TEMP + 1.0)
        if wanted_temp > MIN_LEGIONELLA_TEMP and peak_temp_today > MIN_LEGIONELLA_TEMP:
            pending_legionella_reset = True  # At least two hours above legionella temp

        peak_temp_today = max(peak_temp_today, wanted_temp)
        if (
            today.weekday() in WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT
            and local_hour == (last_morning_heating_h - 1)
            and get_cheap_score_until(
                local_hour, FIRST_EVENING_HIGH_TAKEOUT_H, cost, False
            )
            > 0
        ):
            wanted_temp = min(MAX_TEMP, peak_temp_today + DEGREES_PER_H / 4)

        log_print(
            f"-- {local_hour}:{'0' if (current_minute<=9) else ''}{current_minute}"
            + f" thermostat @ {wanted_temp}. Tomorrow cost final: {cost.tomorrow_final}."
        )

        curr_min = time.localtime()[4]
        if curr_min <= 58 and OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            if local_hour == NEW_PRICE_EXPECTED_HOUR and cost.tomorrow is None:
                if curr_min >= NEW_PRICE_EXPECTED_MIN:
                    continue  # Retry price fetching
        await quarterly_optimization(
            today,
            local_hour,
            wanted_temp,
            last_h_wanted_temp,
            cost,
            temperature_provider,
            alarm_status,
            thermostat,
        )
        if OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            if local_hour == NEW_PRICE_EXPECTED_HOUR and cost.tomorrow is None:
                continue  # Retry price fetching
            curr_min = max(curr_min, time.localtime()[4])
            # Sleep slightly into next hour
            await asyncio.sleep(((60 - curr_min) * SEC_PER_MIN) + MAX_CLOCK_DRIFT_S)
        last_h_wanted_temp = wanted_temp
        time_provider.hourly_timekeeping()


async def handle_client(reader, writer):
    global last_log

    request_line = await reader.readline()

    # Skip HTTP request headers
    while await reader.readline() != b"\r\n":
        pass

    request = str(request_line, "utf-8").split()[1]
    if request in ["/favicon.ico", "/shelly"]:
        writer.write("HTTP/1.0 404 Not Found\r\n")
        await writer.drain()
        await writer.wait_closed()
        return

    if request == "/reduceload":
        reduced_temp = shared_thermostat.prev_degrees - DEGREES_PER_H
        shared_thermostat.set_thermostat(reduced_temp, True)
        log_print(
            f"-- {time.localtime()} Lowering thermostat until next hour {reduced_temp}"
        )
        writer.write("HTTP/1.0 200 OK\r\n")
        await writer.drain()
        await writer.wait_closed()
        return
    if request == "/postponedload":
        writer.write("HTTP/1.0 200 OK\r\nContent-type: text/html\r\n\r\n")
        writer.write(
            "True" if shared_thermostat.prev_degrees < MIN_USABLE_TEMP else "False"
        )
        await writer.drain()
        await writer.wait_closed()
        return
    if request != "/log":
        try:
            override_temp = float(request[1:])
            log_print(
                f"-- {time.localtime()} Overriding thermostat to {override_temp} "
                + "until next scheduling point"
            )
            shared_thermostat.set_thermostat(override_temp, True)
        except ValueError:
            log_print("Failed to parse target temp req as float")

    writer.write("HTTP/1.0 200 OK\r\nContent-type: text/html\r\n\r\n")
    log_cpy = last_log.copy()
    for log_row in log_cpy:
        writer.write(log_row)
        writer.write("<br>")
    await writer.drain()
    await writer.wait_closed()


async def main():
    thermostat = shared_thermostat
    thermostat.set_thermostat(MIN_TEMP)
    attemts_remaing_before_reset = MAX_NETWORK_ATTEMPTS
    while attemts_remaing_before_reset > 0:
        try:
            log_print("Setting up wifi")
            setup_wifi()
            break
        except Exception as setup_e:
            log_print("Delaying due to exception...")
            log_print(setup_e)
            time.sleep(30)
            attemts_remaing_before_reset -= 1

    alarm_status = None
    try:
        import usector_alarm_status
    except ImportError:
        pass
    else:
        alarm_status = usector_alarm_status.AlarmStatusProvider()

    server = asyncio.start_server(handle_client, "0.0.0.0", 80)
    tasks = [server]
    boost_req = machine.reset_cause() == machine.PWRON_RESET

    while attemts_remaing_before_reset > 0:
        gc.collect()
        tasks.append(run_hotwater_optimization(thermostat, alarm_status, boost_req))
        boost_req = False
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
            log_print("Unexpected success termination...")
            break
        except Exception as e:
            sio = io.StringIO()
            sys.print_exception(e, sio)
            traceback_str = sio.getvalue()
            log_print(f"Delaying due to exception... {attemts_remaing_before_reset}")
            log_print(e)
            log_print(traceback_str)
            wlan = network.WLAN(network.STA_IF)
            log_print(f"rssi = {wlan.status('rssi')}")
            tasks.pop(1)
            log_print("Starting fresh optimization")
            await asyncio.sleep(30)
            try:
                setup_wifi()
            except Exception as wifi_e:
                log_print(wifi_e)
            attemts_remaing_before_reset -= 1
    log_print("Resetting to recover")
    machine.reset()


# Globals
last_log = []
shared_thermostat = Thermostat()


if __name__ == "__main__":
    if " W " not in sys.implementation._machine:
        log_print("Unsupported board? WiFi missing?")
    # Create an Event Loop
    ev_loop = asyncio.get_event_loop()
    # Create a task to run the main function
    ev_loop.create_task(main())

    try:
        ev_loop.run_forever()
    except Exception as e:
        log_print("Error occured: ", e)
    except KeyboardInterrupt:
        log_print("Program Interrupted by the user")
