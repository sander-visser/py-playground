"""
Hot water scheduler to move electricity usage to hours that are usually cheap
Runs on a Raspberry Pi PICO W(H) with a SG90 servo connected to PWM GP0
Power takeout for servo possible from VBUS pin if USB powered.
If USB only powered connect VBUS and VSYS for cleaner power (better WiFi)
WiFi range improves with ground connection and good placement.
Designed for WiFi use. Servo connected to theromostat of electric water heater.
Upload to device using Thonny (as main.py).

Power reset unit to get 1h extra hot water.
vvb_optimizer_connected.png show price/target temperature behaviour.

Repo also contans a schedule based optimizer (that does not utilize WiFi)

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both ~500 SEK to shop gadgets with. Contact: github[a]visser.se or
check referral.link in repo
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
# wlan.connect("WLAN_SSID", "WLAN_PASS")
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


# https://www.raspberrypi.com/documentation/pico-sdk/networking.html#CYW43_COUNTRY_
PR2_COUNTRY = "SE"
WLAN_SSID = "your ssid"
WLAN_PASS = "your pass"
NORDPOOL_REGION = "SE3"
NTP_HOST = "se.pool.ntp.org"
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
LAST_MORNING_HEATING_H = 6
FIRST_EVENING_HIGH_TAKEOUT_H = 20  # : 00 - by which time re-heating should have ran
DAILY_COMFORT_LAST_H = 21  # :59
NEW_PRICE_EXPECTED_HOUR = 12
NEW_PRICE_EXPECTED_MIN = 45
MAX_TEMP = 78
MIN_TEMP = 25
MIN_NUDGABLE_TEMP = 28.6  # Setting it any lower will just make it MIN stuck
MIN_USABLE_TEMP = 35  # Good for hand washing, and one hour away from shower temp
MIN_DAILY_TEMP = 50
MIN_LEGIONELLA_TEMP = 65
LEGIONELLA_INTERVAL = 10  # In days
WEEKDAYS_WITH_EXTRA_TAKEOUT = [0, 2, 4, 6]  # 6 == Sunday
WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT = [0, 3]  # 0 == Monday
KWN_PER_MWH = 1000
OVERHEAD_BASE_PRICE = 0.0691656  # In EUR for tax, purchase and transfer costs (wo VAT)
HIGH_PRICE_THRESHOLD = 0.15  # In EUR (incl OVERHEAD_BASE_PRICE)
ACCEPTABLE_PRICING_ERROR = 0.003  # In EUR - how far from cheapest considder same
LOW_PRICE_VARIATION_PERCENT = 1.1  # Limit storage temp if just 10% cheaper
PRICE_API_URL = (
    "https://dataportal-api.nordpoolgroup.com/api/DayAheadPriceIndices?"
    + "market=DayAhead&currency=EUR&resolutionInMinutes=15&indexNames="
)
# TEMPERATURE_URL should return a number "x.y" for degrees C
TEMPERATURE_URL = (
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp"
)

PWM_25_DEGREES = 1172  # Min rotation (@MIN_TEMP)
PWM_78_DEGREES = 8300  # Max rotation (@MAX_TEMP)
PWM_PER_DEGREE = (PWM_78_DEGREES - PWM_25_DEGREES) / 53
ROTATION_SECONDS = 2


def log_print(*args, **kwargs):
    global last_log

    log_str = io.StringIO()
    print("".join(map(str, args)), file=log_str, **kwargs)
    last_log.append(log_str.getvalue())
    if len(last_log) > 125:
        last_log.pop(0)
        last_log.pop(0)
        last_log.pop(0)
        last_log.pop(0)
        last_log.pop(0)
        gc.collect()
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
                    gc.collect()
                except ValueError:
                    log_print(
                        f"Ignored {outdoor_temperature_req.text} from {TEMPERATURE_URL}"
                    )
        except OSError as req_err:
            if req_err.args[0] == 110:  # ETIMEDOUT
                log_print("Ignoring temperature read timeout")
            else:
                raise req_err
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
            if (time.time() - self.last_sync_time) > (12 * 3600):
                self.sync_utc_time()  # Attempt sync time twice per day
                self.last_sync_time = time.time()

    def get_utc_unix_timestamp(self):
        return time.time() if self.current_utc_time is None else self.current_utc_time

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
        pwm_degrees = PWM_25_DEGREES
        if degrees > MIN_TEMP:
            pwm_degrees += (degrees - MIN_TEMP) * PWM_PER_DEGREE
        return min(pwm_degrees, PWM_78_DEGREES)

    def set_thermosat(self, degrees, override=False):
        self.overridden = override
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
        # log_print("Nudging down")
        if self.prev_degrees is not None and self.prev_degrees >= MIN_NUDGABLE_TEMP:
            self.nudge(-5)

    def nudge_up(self):
        # log_print("Nudging up")
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
        log_print("waiting for connection...")
        time.sleep(1)

    if wlan.status() != 3:  # Handle connection error
        for wlans in wlan.scan():
            log_print(f"Seeing SSID {wlans[0]} with rssi {wlans[3]}")
        raise RuntimeError("network connection failed")

    log_print(f"Connected with rssi {wlan.status('rssi')} and IP {wlan.ifconfig()[0]}")


async def get_cost(end_date):
    if not isinstance(end_date, date):
        raise RuntimeError("Error not a date")
    price_api_url = PRICE_API_URL + (
        f"{NORDPOOL_REGION}&date={end_date.year}-{end_date.month}-{end_date.day}"
    )
    gc.collect()
    result = requests.get(price_api_url, timeout=10.0)
    if result.status_code != 200:
        return (None, None)

    the_json_result = result.json()
    gc.collect()

    cost_array = []
    hourly_price = {'avg': 0.0, 'quartely': []}
    for row in the_json_result["multiIndexEntries"]:
        curr_price = (
            row["entryPerArea"][NORDPOOL_REGION] / KWN_PER_MWH + OVERHEAD_BASE_PRICE
        )
        hourly_price['avg'] += curr_price / 4
        hourly_price['quartely'].append(curr_price)
        if len(hourly_price['quartely']) == 4:
            cost_array.append(hourly_price)
            hourly_price = {'avg': 0.0, 'quartely': []}
    if len(the_json_result["areaStates"]) == 0 or len(cost_array) == 0:
        return (None, None)
    if len(cost_array) == 23:
        cost_array.append(cost_array[0])  # DST hack - off by one in adjust days
    return (the_json_result["areaStates"][0]["state"] == "Final", cost_array)


def heat_leakage_loading_desired(local_hour, today_cost, tomorrow_cost, outdoor_temp):
    now_price = today_cost[local_hour]['avg']
    max_price = now_price
    min_price = now_price
    while local_hour < 23:
        local_hour += 1
        max_price = max(max_price, today_cost[local_hour]['avg'])
        min_price = min(min_price, today_cost[local_hour]['avg'])
    if tomorrow_cost is not None:
        for tomorrow_hour_price in tomorrow_cost:
            max_price = max(max_price, tomorrow_hour_price['avg'])
    if (outdoor_temp <= EXTREME_COLD_THRESHOLD) or max_price > (min_price * COP_FACTOR):
        log_print(f"Extra heating due to COP? now: {now_price}. min: {min_price}")
        return now_price <= (min_price + ACCEPTABLE_PRICING_ERROR)
    return False


def now_is_cheap_in_forecast(now_hour, today_cost, tomorrow_cost):
    """
    Scan 16h ahead and check if now is the best time to buffer some comfort
    """
    scan_hours_remaining = 16
    hours_til_cheaper = 0
    max_price_ahead = today_cost[now_hour]['avg']
    max_price_til_next_cheap = max_price_ahead
    min_price_ahead = max_price_ahead
    for scan_hour in range(now_hour, min(24, now_hour + scan_hours_remaining)):
        scan_hours_remaining -= 1
        max_price_ahead = max(max_price_ahead, today_cost[scan_hour]['avg'])
        min_price_ahead = min(min_price_ahead, today_cost[scan_hour]['avg'])
        if hours_til_cheaper == 0 and cheap_later_test(
            today_cost, now_hour, scan_hour, now_hour
        ):
            hours_til_cheaper = 15 - scan_hours_remaining
            max_price_til_next_cheap = max_price_ahead

    if tomorrow_cost is not None:
        for scan_hour in range(0, scan_hours_remaining):
            max_price_ahead = max(max_price_ahead, tomorrow_cost[scan_hour]['avg'])
            if tomorrow_cost[scan_hour]['avg'] <= min_price_ahead:
                min_price_ahead = tomorrow_cost[scan_hour]['avg']
                if hours_til_cheaper == 0:
                    hours_til_cheaper = (24 - now_hour) + scan_hour
                    max_price_til_next_cheap = max_price_ahead
        scan_hours_remaining = 0

    if (min_price_ahead + ACCEPTABLE_PRICING_ERROR) >= (today_cost[now_hour]['avg']):
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
        cost_sum += hour['avg']
    return cost_sum


def get_cheap_score_until(now_hour, until_hour, today_cost, verbose):
    """
    Give the cheapest MAX_HOURS_NEEDED_TO_HEAT a decreasing score
    that can be used to calculate heating curve.
    Scoring considders ramping vs aggressive heating to cheapest, as well as
    moving completion hour if needed and heating for total of MAX_HOURS_NEEDED_TO_HEAT
    """
    now_price = today_cost[now_hour]['avg']
    cheap_hours = today_cost[0:NORMAL_HOURS_NEEDED_TO_HEAT]
    heat_end_hour = NORMAL_HOURS_NEEDED_TO_HEAT
    cheapest_price_sum = summarize_cost(cheap_hours)
    score = MAX_HOURS_NEEDED_TO_HEAT  # Assume now_hour is cheapest
    delay_msg = None
    for scan_hour in range(0, until_hour + 1):
        if today_cost[scan_hour]['avg'] < now_price:
            score -= 1
        if scan_hour > NORMAL_HOURS_NEEDED_TO_HEAT:
            scan_price_sum = summarize_cost(
                today_cost[(scan_hour - NORMAL_HOURS_NEEDED_TO_HEAT) : scan_hour]
            )
            delay_saving = (
                cheapest_price_sum + ACCEPTABLE_PRICING_ERROR - scan_price_sum
            )
            if delay_saving >= 0:
                delay_msg = (
                    f"Delaying heatup end to {scan_hour}:00 saves {delay_saving} EUR"
                )
                cheapest_price_sum = scan_price_sum
                cheap_hours = today_cost[
                    (scan_hour - NORMAL_HOURS_NEEDED_TO_HEAT) : scan_hour
                ]
                heat_end_hour = scan_hour

    if (now_price + ACCEPTABLE_PRICING_ERROR) <= sorted(
        cheap_hours, key=lambda h: h['avg']
    )[NORMAL_HOURS_NEEDED_TO_HEAT - 1]['avg']:
        if heat_end_hour - now_hour > NORMAL_HOURS_NEEDED_TO_HEAT:
            # If late cheap hour preheat somewhat now
            score = max(score, 1)
        else:
            # If delayed still heat aggressive now
            score = MAX_HOURS_NEEDED_TO_HEAT

    if (
        now_price
        <= sorted(cheap_hours, key=lambda h: h['avg'])[
            NORMAL_HOURS_NEEDED_TO_HEAT - 1
        ]['avg']
    ):
        if verbose and delay_msg is not None:
            log_print(delay_msg)
        # Secure correct score inside boost period (with late peak favored)
        min_score = 1
        for cheap_price_route in cheap_hours:
            if (now_price + ACCEPTABLE_PRICING_ERROR) <= cheap_price_route['avg']:
                min_score += 1
        if (
            now_price
            <= (
                sorted(cheap_hours, key=lambda h: h['avg'])[0]['avg']
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
    for cheap_future_cost in sorted(future_cost, key=lambda h: h['avg'])[
        0:MAX_HOURS_NEEDED_TO_HEAT
    ]:
        if this_hour_cost < cheap_future_cost['avg']:
            score += 1
    return score


def cheap_later_test(today_cost, scan_from, scan_to, test_hour):
    extra_kwh_loss_per_hour_of_pre_heat = (
        (DEGREES_PER_H + MIN_DAILY_TEMP - AMBIENT_TEMP)
        / (MIN_DAILY_TEMP - AMBIENT_TEMP)
        - 1
    ) * (HEAT_LOSS_PER_DAY_KWH / 24)
    min_compensated_cost = today_cost[scan_from]['avg'] * (
        HEATER_KW + (test_hour - scan_from) * extra_kwh_loss_per_hour_of_pre_heat
    )

    for i in range(scan_from + 1, scan_to):
        if i <= test_hour:
            compensated_cost = today_cost[i]['avg'] * (
                HEATER_KW + (test_hour - i) * extra_kwh_loss_per_hour_of_pre_heat
            )
            min_compensated_cost = min(compensated_cost, min_compensated_cost)
        else:
            compensated_cost = today_cost[i]['avg'] * (
                HEATER_KW - (i - test_hour) * extra_kwh_loss_per_hour_of_pre_heat
            )  # Less energy is used when load is heated later than test_hour
            if compensated_cost <= min_compensated_cost:
                log_print(
                    f"Analyzed {scan_from}-{scan_to}: Delaying beyond {test_hour} worth while"
                    + f" (delay til {i})"
                )
                return True  # Found cost to be cheaper if heating later
    return False


def is_now_cheapest_remaining_during_comfort(today_cost, local_hour):
    return not cheap_later_test(
        today_cost, local_hour, DAILY_COMFORT_LAST_H, local_hour
    )


def hours_to_next_lower_price(today_cost, scan_from):
    min_price = today_cost[scan_from]['avg']
    for i in range(scan_from + 1, DAILY_COMFORT_LAST_H):
        if today_cost[i]['avg'] <= min_price:
            return i - scan_from
    return 0


def next_night_is_cheaper(today_cost):
    return cheap_later_test(today_cost, 0, 24, DAILY_COMFORT_LAST_H)


def is_the_cheapest_hour_during_daytime(today_cost):
    return cheap_later_test(today_cost, 0, DAILY_COMFORT_LAST_H, LAST_MORNING_HEATING_H)


def is_now_significantly_cheaper(now_hour, today_cost, tomorrow_cost):
    """
    Scan 16h ahead and check if now is significantly cheaper than max price ahead
    """
    scan_hours_remaining = 16
    max_price_ahead = today_cost[now_hour]['avg']
    for scan_hour in range(now_hour, min(24, now_hour + scan_hours_remaining)):
        scan_hours_remaining -= 1
        max_price_ahead = max(max_price_ahead, today_cost[scan_hour]['avg'])

    if tomorrow_cost is not None:
        for scan_hour in range(0, scan_hours_remaining):
            max_price_ahead = max(max_price_ahead, tomorrow_cost[scan_hour]['avg'])

    return today_cost[now_hour]['avg'] * LOW_PRICE_VARIATION_PERCENT < max_price_ahead


def add_scorebased_wanted_temperature(
    local_hour, weekday, today_cost, tomorrow_cost, outside_temp, wanted_temp, verbose
):
    score_based_heating = 0
    max_temp_limit = MAX_TEMP
    if local_hour <= LAST_MORNING_HEATING_H:
        score_based_heating = get_cheap_score_until(
            local_hour, LAST_MORNING_HEATING_H, today_cost, verbose
        )
        if is_the_cheapest_hour_during_daytime(today_cost):
            # limit morning heating much if daytime heating is cheap
            max_temp_limit = MIN_DAILY_TEMP + (LAST_MORNING_HEATING_H - local_hour)
        elif next_night_is_cheaper(today_cost):
            max_temp_limit = MIN_DAILY_TEMP + DEGREES_PER_H

    if local_hour <= DAILY_COMFORT_LAST_H:
        score_based_heating = max(
            score_based_heating,
            get_cheap_score_until(
                local_hour, DAILY_COMFORT_LAST_H, today_cost, verbose
            ),
        )

    if not is_now_significantly_cheaper(local_hour, today_cost, tomorrow_cost):
        max_temp_limit = MIN_DAILY_TEMP  # Restrict heating if only slightly cheaper

    if tomorrow_cost is not None:
        preload_score = get_cheap_score_relative_future(
            today_cost[local_hour]['avg'],
            today_cost[local_hour:23] + tomorrow_cost[0:LAST_MORNING_HEATING_H],
        )
        if preload_score > 0:
            score_based_heating = max(score_based_heating, preload_score)
            if cheap_later_test(
                today_cost, local_hour, 24, FIRST_EVENING_HIGH_TAKEOUT_H
            ):
                max_temp_limit = MIN_DAILY_TEMP + DEGREES_PER_H
        else:
            max_temp_limit = (
                MIN_DAILY_TEMP + DEGREES_PER_H
            )  # Long cheap period is ahead
        if weekday not in WEEKDAYS_WITH_EXTRA_TAKEOUT:
            max_temp_limit -= DEGREES_PER_H  # Avoid storing long time if low takeout

    max_score = MAX_HOURS_NEEDED_TO_HEAT
    if outside_temp < HEAT_LEAK_VALUE_THRESHOLD and heat_leakage_loading_desired(
        local_hour, today_cost, tomorrow_cost, outside_temp
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


def get_wanted_temp_boost(local_hour, weekday, today_cost):
    wanted_temp_boost = 0
    if weekday in WEEKDAYS_WITH_EXTRA_TAKEOUT and local_hour < DAILY_COMFORT_LAST_H:
        wanted_temp_boost += 5

    if (
        weekday in WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT
        and local_hour <= LAST_MORNING_HEATING_H
    ):
        wanted_temp_boost += 5

    if MAX_HOURS_NEEDED_TO_HEAT <= local_hour < DAILY_COMFORT_LAST_H:
        if today_cost[local_hour]['avg'] < HIGH_PRICE_THRESHOLD:
            wanted_temp_boost += 5  # Slightly raise hot water takeout capacity
        if (
            local_hour < 23
            and today_cost[local_hour]['avg'] < today_cost[local_hour + 1]['avg']
        ):
            hours_to_bridge = hours_to_next_lower_price(today_cost, local_hour)
            if is_now_cheapest_remaining_during_comfort(today_cost, local_hour):
                hours_to_bridge = 1 + (DAILY_COMFORT_LAST_H - local_hour)
            # Better heat now rather than later
            wanted_temp_boost += DEGREES_LOST_PER_H * hours_to_bridge

    return wanted_temp_boost


def get_wanted_temp(
    local_hour,
    weekday,
    today_cost,
    tomorrow_cost,
    outside_temp,
    alarm_fully_armed,
    verbose,
):
    wanted_temp = MIN_TEMP

    if not alarm_fully_armed:
        wanted_temp += get_wanted_temp_boost(local_hour, weekday, today_cost)
    elif MAX_HOURS_NEEDED_TO_HEAT <= local_hour < DAILY_COMFORT_LAST_H:
        if is_now_cheapest_remaining_during_comfort(today_cost, local_hour):
            wanted_temp += (
                1 + (DAILY_COMFORT_LAST_H - local_hour)
            ) * DEGREES_LOST_PER_H
    gc.collect()  # Avoid fragmentation after alarm API use

    wanted_temp = add_scorebased_wanted_temperature(
        local_hour,
        weekday,
        today_cost,
        tomorrow_cost,
        outside_temp,
        wanted_temp,
        verbose,
    )

    if MAX_HOURS_NEEDED_TO_HEAT < local_hour <= LAST_MORNING_HEATING_H:
        if not cheap_later_test(
            today_cost, local_hour, FIRST_EVENING_HIGH_TAKEOUT_H, local_hour
        ):
            wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)  # Maintain morning heating
    if DAILY_COMFORT_LAST_H > local_hour > LAST_MORNING_HEATING_H and (
        MAX_HOURS_NEEDED_TO_HEAT - 1
    ) <= get_cheap_score_relative_future(
        today_cost[local_hour]['avg'],
        today_cost[LAST_MORNING_HEATING_H:DAILY_COMFORT_LAST_H],
    ):
        wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)  # Restore comfort once per day

    if (
        today_cost[local_hour]['avg']
        >= sorted(today_cost, key=lambda h: h['avg'])[24 - NUM_MOST_EXPENSIVE_HOURS]['avg']
    ):
        wanted_temp = MIN_NUDGABLE_TEMP  # Min temp during most expensive hours in day

    if now_is_cheap_in_forecast(local_hour, today_cost, tomorrow_cost):
        wanted_temp = max(wanted_temp, MIN_DAILY_TEMP)
        if local_hour <= LAST_MORNING_HEATING_H:
            wanted_temp = max(wanted_temp, MIN_DAILY_TEMP + DEGREES_PER_H)

    if alarm_fully_armed:
        wanted_temp = min(wanted_temp, MIN_LEGIONELLA_TEMP)
    return wanted_temp


def get_local_date_and_hour(utc_unix_timestamp):
    local_unix_timestamp = utc_unix_timestamp + UTC_OFFSET_IN_S
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

    return (adjusted_day, now[3])


async def delay_minor_temp_increase(wanted_temp, thermostat, local_hour):
    diff_temp = wanted_temp - thermostat.prev_degrees
    if 0 < diff_temp < DEGREES_PER_H and time.localtime()[4] < 45:
        raise_delay = (45 * SEC_PER_MIN) - (
            (diff_temp / DEGREES_PER_H) * 45 * SEC_PER_MIN
        )
        raise_delay_min = int(raise_delay/60)
        pretty_min = f"{'0' if (raise_delay_min<=9) else ''}{raise_delay_min}"
        log_print(f"Delaying temp increase to {local_hour}:{pretty_min}")
        if OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            await asyncio.sleep(raise_delay)


async def run_hotwater_optimization(thermostat, alarm_status, boost_req):
    time_provider = TimeProvider()
    time_provider.sync_utc_time()

    today = None
    today_cost = None
    tomorrow_cost = None
    tomorrow_final = False
    days_since_legionella = 0
    peak_temp_today = 0
    pending_legionella_reset = False
    temperature_provider = SimpleTemperatureProvider()

    if boost_req:
        log_print("Boosting...")
        thermostat.set_thermosat(MIN_LEGIONELLA_TEMP)
        await asyncio.sleep(EXTRA_HOT_DURATION_S)

    while True:
        await asyncio.sleep(0.1)
        new_today, local_hour = get_local_date_and_hour(
            time_provider.get_utc_unix_timestamp()
        )
        if today_cost is None or new_today != today:
            peak_temp_today = 0
            today = new_today
            if pending_legionella_reset:
                days_since_legionella = 0
                pending_legionella_reset = False
            days_since_legionella += 1
            today_cost = tomorrow_cost
            if today_cost is None:
                today_final, today_cost = await get_cost(today)
                if today_final is None:
                    raise RuntimeError("Optimization not possible")
            tomorrow_final, tomorrow_cost = (False, None)

        current_minute = time.localtime()[4]
        if not tomorrow_final and (
            (local_hour > NEW_PRICE_EXPECTED_HOUR)
            or (
                local_hour == NEW_PRICE_EXPECTED_HOUR
                and current_minute >= NEW_PRICE_EXPECTED_MIN
            )
        ):
            tomorrow_final, tomorrow_cost = await get_cost(today + timedelta(days=1))
            gc.collect()

        log_print(
            f"Cost optimizing for {today.day} / {today.month} {today.year} {local_hour}:00 @"
            + f" {today_cost[local_hour]['avg']} EUR / kWh"
        )
        outside_temp = temperature_provider.get_outdoor_temp()
        wanted_temp = get_wanted_temp(
            local_hour,
            today.weekday(),
            today_cost,
            tomorrow_cost,
            outside_temp,
            alarm_status is not None and alarm_status.is_fully_armed(),
            True,
        )
        if days_since_legionella > LEGIONELLA_INTERVAL and (
            (LAST_MORNING_HEATING_H - 2) <= local_hour <= LAST_MORNING_HEATING_H
        ):  # Secure legionella temperature gets reached
            wanted_temp = max(wanted_temp, MIN_LEGIONELLA_TEMP)
        if wanted_temp >= MIN_LEGIONELLA_TEMP:
            pending_legionella_reset = True

        peak_temp_today = max(peak_temp_today, wanted_temp)
        if (
            today.weekday() in WEEKDAYS_WITH_EXTRA_MORNING_TAKEOUT
            and local_hour == (LAST_MORNING_HEATING_H - 1)
            and get_cheap_score_until(
                local_hour, FIRST_EVENING_HIGH_TAKEOUT_H, today_cost, False
            )
            > 0
        ):
            wanted_temp = min(MAX_TEMP, peak_temp_today + DEGREES_PER_H / 4)

        pretty_time = f"{local_hour}:{'0' if (current_minute<=9) else ''}{current_minute}"
        log_print(
            f"-- {pretty_time} thermostat @ {wanted_temp}. Outside is {outside_temp}."
            + f" Tomorrow {tomorrow_cost is not None}"
        )

        pre_delay_override = thermostat.overridden
        if local_hour <= NEW_PRICE_EXPECTED_HOUR or tomorrow_cost is not None:
            await delay_minor_temp_increase(wanted_temp, thermostat, local_hour)

        if pre_delay_override == thermostat.overridden:
            thermostat.set_thermosat(wanted_temp)
        else:
            log_print("Skipping thermostat setting due to override during delay")
        curr_min = time.localtime()[4]
        if curr_min <= 50 and OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            if local_hour == NEW_PRICE_EXPECTED_HOUR and tomorrow_cost is None:
                if curr_min < NEW_PRICE_EXPECTED_MIN:
                    await asyncio.sleep(
                        (NEW_PRICE_EXPECTED_MIN - curr_min) * SEC_PER_MIN
                    )
                else:
                    await asyncio.sleep(1 * SEC_PER_MIN)  # Retry price fetching
                continue
            await asyncio.sleep(
                (50 - curr_min) * SEC_PER_MIN
            )  # Sleep slightly before next hour
        if local_hour < 23 and OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            next_hour_wanted_temp = get_wanted_temp(
                local_hour + 1,
                today.weekday(),
                today_cost,
                tomorrow_cost,
                temperature_provider.get_outdoor_temp(),
                alarm_status is not None and alarm_status.is_fully_armed(),
                False,
            )
            if (
                next_hour_wanted_temp >= wanted_temp
                and today_cost[local_hour + 1]['avg'] <= today_cost[local_hour]['avg']
            ):
                thermostat.nudge_down()
            if (
                not thermostat.overridden
                and next_hour_wanted_temp <= wanted_temp
                and today_cost[local_hour + 1]['avg'] > today_cost[local_hour]['avg']
            ):
                thermostat.nudge_up()

        time_provider.hourly_timekeeping()
        if OVERRIDE_UTC_UNIX_TIMESTAMP is None:
            await asyncio.sleep(12 * SEC_PER_MIN)  # Sleep slightly into next hour


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
    log_print("Request: ", request)

    try:
        if request == "/reduceload":
            shared_thermostat.set_thermosat(
                shared_thermostat.prev_degrees - DEGREES_PER_H, True
            )
            log_print(
                f"-- {time.localtime()} Lowering thermostat until next schedule point"
                + f" {shared_thermostat.prev_degrees}"
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
            override_temp = float(request[1:])
            log_print(
                f"-- {time.localtime()} Overriding thermostat to {override_temp} "
                + "until next scheduling point"
            )
            shared_thermostat.set_thermosat(override_temp, True)
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
    if " W " not in sys.implementation._machine:
        log_print("Unsupported board?")
    thermostat = shared_thermostat
    thermostat.set_thermosat(MIN_NUDGABLE_TEMP)
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
            log_print(
                f"Delaying due to exception... {attemts_remaing_before_reset}"
            )
            log_print(e)
            wlan = network.WLAN(network.STA_IF)
            log_print(f"rssi = {wlan.status('rssi')}")
            tasks.pop(1)
            log_print("Starting fresh optimization")
            await asyncio.sleep(30)
            setup_wifi()
            attemts_remaing_before_reset -= 1
    log_print("Resetting to recover")
    await asyncio.sleep(10)
    machine.reset()


# Globals
last_log = []
shared_thermostat = Thermostat()


if __name__ == "__main__":
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
