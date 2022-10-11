#!/usr/bin/env python3

"""
Optimizer balancing comfort vs price for an IR remote controlled heatpump
- pre-heats the home in an optimal way when occupants are away or sleeping
- offers extra heat when the price is affordable

Usage:
Install needed pip packages
Run this script on a internet connected machine configured with relevant timezone
Script tested with a Sensibo Sky placed 20cm above floor level
Adapt constants as needed for your home
"""

from datetime import datetime, timedelta
from time import sleep
import sys
import copy

# "python3 -m pip install X" below python modules
import requests
import pause
import holidays
from nordpool import elspot
import pytz
import sensibo_client  # https://github.com/Sensibo/sensibo-python-sdk with py3 print fix

# should return a number "x.y"
TEMPERATURE_URL = (
    "https://www.temperatur.nu/termo/gettemp.php?stadname=partille&what=temp"
)
REGION = "SE3"
REGION_HOLIDAYS = holidays.country_holidays("SE")
TIME_ZONE = "CET"
# Expecting about 10% of heat energy to be leaked per hour
ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY = 1.10
WORKDAY_MORNING = {
    "comfort_by_hour": 6,
    "comfort_until_hour": 7,
    "comfort_until_minute": 30,
    "idle_monitor_from_hour": 8,
}
DAYOFF_MORNING = {
    "comfort_by_hour": 8,
    "eat_until_hour": 9,
}
WORKDAY_MORNING_COMFORT_UNTIL_HOUR = 8
EARLIEST_AFTERNOON_PREHEAT_HOUR = 11  # Must be a pause since morning hour
BEGIN_AFTERNOON_HEATING_BY_HOUR = 14
WORKDAY_AFTERNOON_COMFORT_BY_HOUR = 16
WORKDAY_COMFORT_UNTIL_HOUR = 22
WEEKEND_COMFORT_UNTIL_HOUR = 23
DEGREES_PER_HOUR_DURING_RAMPUP = 1
SECONDS_BETWEEN_COMMANDS = 1.5
AT_HOME_DAYS = [5, 6, 7]
TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY = 40.0
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE = 750.0
RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST = 600.0
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_CHEAP = 300.0
ABSOLUTE_SEK_PER_MWH_BEYOND_WHICH_TO_REDUCE_COMFORT = 7000.0
MAX_HOURS_OF_REDUCED_COMFORT_PER_DAY = 3
MIN_OUTDOOR_TEMP_TO_REDUCE_COMFORT_AT = (
    2.0  # Pure electric heaters should be off abouve
)
MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE = 22.5
MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE = 19.0
MIN_FLOOR_SENSOR_IDLE_TEMPERATURE = 17.0

IDLE_SETTINGS = {
    "on": True,
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 17,
}

MAX_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "high",
    "targetTemperature": 23,
}

COMFORT_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}

COMFORT_PLUS_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 22,
}

COMFORT_REDUCED_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium",
    "targetTemperature": 18,
}

HEAT_DISTRIBUTION_SETTINGS = {
    "targetTemperature": 16,  # Ignored,b ut needed during restore
    "mode": "fan",
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium",
}

COMFORT_EATING_HEAT_SETTINGS = {
    "mode": "heat",
    "horizontalSwing": "fixedCenterRight",
    "swing": "fixedMiddle",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}


class PriceAnalyzer:
    def __init__(self):
        self._day_spot_prices = None
        self.cheap_morning_hour = None
        self.cheap_afternoon_hour = None
        self._reasonably_priced_hours = None
        self._reduced_comfort_hours = None
        self._pre_heat_favorable_hours = None
        self.significantly_more_expensive_after_midnight = False

    def is_hour_with_reduced_comfort(self, hour):
        return hour in self._reduced_comfort_hours

    def is_hour_reasonably_priced(self, hour):
        return hour in self._reasonably_priced_hours

    def is_hour_preheat_favorable(self, hour):
        return hour in self._pre_heat_favorable_hours

    def prepare_next_day(self, lookup_date):
        spot_prices = elspot.Prices("SEK")

        print(f"Getting prices for {lookup_date} to find cheap hours...")
        day_spot_prices = spot_prices.hourly(end_date=lookup_date, areas=[REGION])[
            "areas"
        ][REGION]["values"]

        if self._day_spot_prices is not None:
            self.significantly_more_expensive_after_midnight = (
                (
                    day_spot_prices[0]["value"]
                    * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
                )
                + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
            ) < self._day_spot_prices[23]["value"]
        self._day_spot_prices = day_spot_prices

    def process_preheat_favourable_hour(
        self, previous_hour_price, current_hour_price, previous_price_period_start_hour
    ):
        if previous_hour_price is not None and current_hour_price > (
            (previous_hour_price * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY)
            + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
        ):
            self._pre_heat_favorable_hours.append(previous_price_period_start_hour)

    def find_warmup_hours(self, first_comfort_range, second_comfort_range):
        price_period_price = None
        self.cheap_morning_hour = None
        self.cheap_afternoon_hour = None
        local_tz = pytz.timezone(TIME_ZONE)
        lowest_price = None
        previous_hour_price = None
        self._reasonably_priced_hours = []
        self._pre_heat_favorable_hours = []
        for hour_price in self._day_spot_prices:
            if lowest_price is None or hour_price["value"] < lowest_price:
                lowest_price = hour_price["value"]

        curr_hour_idx = 0
        comfort_hours = {}
        for hour_price in self._day_spot_prices:
            price_period_start_hour = hour_price["start"].astimezone(local_tz).hour
            print(
                f"{hour_price['start'].astimezone(local_tz)} @ {hour_price['value']} SEK/MWh"
            )
            if (
                price_period_start_hour in first_comfort_range
                or price_period_start_hour == first_comfort_range.stop
            ):
                comfort_hours[
                    hour_price["value"] + 0.000001 * len(comfort_hours)
                ] = price_period_start_hour
            if second_comfort_range is not None and (
                price_period_start_hour in second_comfort_range
                or price_period_start_hour == second_comfort_range.stop
            ):
                comfort_hours[
                    hour_price["value"] + 0.000001 * len(comfort_hours)
                ] = price_period_start_hour

            self.process_preheat_favourable_hour(
                previous_hour_price, hour_price["value"], price_period_start_hour - 1
            )

            if (
                hour_price["value"]
                <= (
                    lowest_price
                    + RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST
                )
            ) or hour_price["value"] <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE:
                if (
                    (curr_hour_idx + 1) < len(self._day_spot_prices)
                    and (
                        hour_price["value"]
                        <= self._day_spot_prices[curr_hour_idx + 1]["value"]
                        or (price_period_start_hour - 1)
                        not in self._reasonably_priced_hours
                    )
                ) or hour_price["value"] <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_CHEAP:
                    self._reasonably_priced_hours.append(price_period_start_hour)

            if price_period_start_hour < first_comfort_range.start:
                if (
                    price_period_price is None
                    or hour_price["value"] <= price_period_price
                ):
                    price_period_price = hour_price["value"]
                    self.cheap_morning_hour = price_period_start_hour
                price_period_price = (
                    price_period_price * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
                ) + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
            elif second_comfort_range is not None and (
                EARLIEST_AFTERNOON_PREHEAT_HOUR
                <= price_period_start_hour
                <= second_comfort_range.start
            ):
                if (
                    price_period_price is None
                    or hour_price["value"] <= price_period_price
                ):
                    price_period_price = hour_price["value"]
                    self.cheap_afternoon_hour = price_period_start_hour
                price_period_price = (
                    price_period_price * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
                ) + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
            else:
                price_period_price = None
            curr_hour_idx += 1
            previous_hour_price = hour_price["value"]
        self.calculate_reduced_comfort_hours(comfort_hours)

    def calculate_reduced_comfort_hours(self, comfort_hours):
        self._reduced_comfort_hours = []
        for comfort_hour_price, comfort_hour_start in sorted(
            comfort_hours.items(), reverse=True
        ):
            if comfort_hour_price > ABSOLUTE_SEK_PER_MWH_BEYOND_WHICH_TO_REDUCE_COMFORT:
                self._reduced_comfort_hours.append(comfort_hour_start)
                if (
                    len(self._reduced_comfort_hours)
                    >= MAX_HOURS_OF_REDUCED_COMFORT_PER_DAY
                ):
                    break


class SensiboOptimizer:
    def __init__(self):
        self.client = None
        self._uid = None
        self._price_analyzer = PriceAnalyzer()
        self._step_1_overtemperature_distribution_active = False
        program_start_time = datetime.today()
        self._current_settings = {}
        self._prev_midnight = datetime(
            program_start_time.year,
            program_start_time.month,
            program_start_time.day,
            0,
            0,
        )

    def wait_for_hour(self, hour):
        pause.until(
            self._prev_midnight + timedelta(hours=hour)
        )  # Direct return if in the past...
        print(f"At {hour}:00")

    def apply_multi_settings(self, settings, force=False):
        # print(f"Applying: {settings}")
        if force:
            self._current_settings = {}
        first_setting = True
        for setting in settings:
            if (
                setting not in self._current_settings
                or settings[setting] != self._current_settings[setting]
            ):
                self._current_settings[setting] = settings[setting]
                self.client.pod_change_ac_state(
                    self._uid, None, setting, settings[setting]
                )
                if not first_setting:
                    sleep(SECONDS_BETWEEN_COMMANDS)
                first_setting = False

    def get_current_floor_temp(self, na_temp_val):
        current_floor_sensor_value = na_temp_val
        try:
            current_floor_sensor_value = self.client.pod_measurement(self._uid)[0][
                "temperature"
            ]
        except requests.exceptions.ConnectionError:
            print(
                f"Ignoring temperature read error - using {current_floor_sensor_value}"
            )
        return current_floor_sensor_value

    def manage_over_temperature(self):
        if self._step_1_overtemperature_distribution_active:
            self.apply_multi_settings(HEAT_DISTRIBUTION_SETTINGS)
        else:
            self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
            self._step_1_overtemperature_distribution_active = True

    def run_boost_rampup_to_comfort(
        self, idle_hour_start, boost_hour_start, short_boost, comfort_hour_start
    ):
        self.wait_for_hour(idle_hour_start)
        current_floor_sensor_value = MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE
        for pause_hour in range(idle_hour_start, boost_hour_start - 1):
            for sample_minute in range(9, 60, 10):
                current_floor_sensor_value = self.get_current_floor_temp(
                    current_floor_sensor_value
                )
                if current_floor_sensor_value < MIN_FLOOR_SENSOR_IDLE_TEMPERATURE:
                    self._step_1_overtemperature_distribution_active = False
                    self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                elif (
                    current_floor_sensor_value
                    >= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self.manage_over_temperature()
                else:
                    self._step_1_overtemperature_distribution_active = False
                    self.apply_multi_settings(IDLE_SETTINGS)
                pause.until(
                    self._prev_midnight
                    + timedelta(hours=pause_hour, minutes=sample_minute)
                )

        if short_boost:
            self.wait_for_hour(boost_hour_start - 1)
            for sample_minute in range(9, 60, 10):
                current_floor_sensor_value = self.get_current_floor_temp(
                    current_floor_sensor_value
                )

                sleep(SECONDS_BETWEEN_COMMANDS)
                if current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
                    self.apply_multi_settings(MAX_HEAT_SETTINGS)
                elif (
                    current_floor_sensor_value
                    >= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                else:
                    self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                pause.until(
                    self._prev_midnight
                    + timedelta(hours=boost_hour_start - 1, minutes=sample_minute)
                )
        self.wait_for_hour(boost_hour_start)
        self.apply_multi_settings(MAX_HEAT_SETTINGS)

        self.handle_post_boost(boost_hour_start + 1, comfort_hour_start)

    def handle_post_boost(self, post_boost_hour_start, comfort_hour_start):
        pause_setting = copy.deepcopy(COMFORT_HEAT_SETTINGS)
        for pause_hour in range(post_boost_hour_start, comfort_hour_start):
            self.wait_for_hour(pause_hour)
            pause_setting["targetTemperature"] = int(
                COMFORT_HEAT_SETTINGS["targetTemperature"]
                - (comfort_hour_start - pause_hour) * DEGREES_PER_HOUR_DURING_RAMPUP
            )
            if self._price_analyzer.is_hour_preheat_favorable(pause_hour):
                self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
            elif (
                pause_setting["targetTemperature"] < IDLE_SETTINGS["targetTemperature"]
            ):
                self.apply_multi_settings(IDLE_SETTINGS)
            else:
                self.apply_multi_settings(pause_setting)

    def run_workday_8_to_22_schedule(self):
        pause.until(
            self._prev_midnight
            + timedelta(
                hours=WORKDAY_MORNING["comfort_until_hour"],
                minutes=WORKDAY_MORNING["comfort_until_minute"],
            )
        )

        self.apply_multi_settings(IDLE_SETTINGS)

        self.run_boost_rampup_to_comfort(
            WORKDAY_MORNING["idle_monitor_from_hour"],
            self._price_analyzer.cheap_afternoon_hour,
            self._price_analyzer.cheap_afternoon_hour
            == BEGIN_AFTERNOON_HEATING_BY_HOUR,
            WORKDAY_AFTERNOON_COMFORT_BY_HOUR,
        )

        self.manage_comfort_hours([WORKDAY_AFTERNOON_COMFORT_BY_HOUR])

        self.wait_for_hour(17)
        self.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

        self.manage_comfort_hours(range(18, WORKDAY_COMFORT_UNTIL_HOUR))

    def manage_comfort_hours(self, comfort_range):
        current_floor_sensor_value = MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
        current_outdoor_temperature = MIN_OUTDOOR_TEMP_TO_REDUCE_COMFORT_AT
        for comfort_hour in comfort_range:
            self.wait_for_hour(comfort_hour)
            for sample_minute in range(9, 60, 10):
                current_floor_sensor_value = self.get_current_floor_temp(
                    current_floor_sensor_value
                )
                outdoor_temperature_req = requests.get(TEMPERATURE_URL, timeout=10.0)
                if outdoor_temperature_req.status_code == 200:
                    current_outdoor_temperature = float(outdoor_temperature_req.text)

                if (
                    current_floor_sensor_value
                    <= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self._step_1_overtemperature_distribution_active = False

                if (
                    current_outdoor_temperature > MIN_OUTDOOR_TEMP_TO_REDUCE_COMFORT_AT
                    and self._price_analyzer.is_hour_with_reduced_comfort(comfort_hour)
                ):
                    self.apply_multi_settings(COMFORT_REDUCED_HEAT_SETTINGS)
                elif current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
                    self.apply_multi_settings(MAX_HEAT_SETTINGS)
                elif (
                    current_outdoor_temperature >= MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE
                ):
                    self.apply_multi_settings(IDLE_SETTINGS)
                elif (
                    sample_minute == 59  # boost 49-59 if price will rise
                    and self._price_analyzer.is_hour_preheat_favorable(comfort_hour)
                    and current_floor_sensor_value
                    <= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                elif (
                    current_floor_sensor_value
                    < MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    if self._price_analyzer.is_hour_reasonably_priced(comfort_hour):
                        self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                    else:
                        self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                else:
                    self.manage_over_temperature()
                pause.until(
                    self._prev_midnight
                    + timedelta(hours=comfort_hour, minutes=sample_minute)
                )

    def run(self, device_name):
        devices = self.client.devices()
        print("-" * 10, "devices", "-" * 10)
        print(devices)

        if len(devices) == 0:
            print("No devices present in account associated with API key...")
            sys.exit(0)

        if device_name is None:
            print("No device selected for optimization - exiting")
            sys.exit(0)

        self._uid = devices[device_name]
        print("-" * 10, f"AC State of {device_name}", "_" * 10)
        try:
            print(self.client.pod_measurement(self._uid))
            ac_state = self.client.pod_ac_state(
                self._uid
            )  # If no result then stop/start in the Sensibo App
            print(ac_state)
        except IndexError:
            print(
                "Warning: Server does not know current state - try to stop/start in the Sensibo App"
            )

        self._price_analyzer.prepare_next_day(self._prev_midnight.date())
        while True:
            optimizing_a_workday = (
                self._prev_midnight.date().isoweekday() not in AT_HOME_DAYS
            ) and self._prev_midnight.date() not in REGION_HOLIDAYS
            comfort_heating_first_range = (
                range(
                    WORKDAY_MORNING["comfort_by_hour"],
                    WORKDAY_MORNING_COMFORT_UNTIL_HOUR,
                )
                if optimizing_a_workday
                else range(
                    DAYOFF_MORNING["comfort_by_hour"], WEEKEND_COMFORT_UNTIL_HOUR
                )
            )
            comfort_heating_second_range = (
                range(WORKDAY_AFTERNOON_COMFORT_BY_HOUR, WORKDAY_COMFORT_UNTIL_HOUR)
                if optimizing_a_workday
                else None
            )
            self._price_analyzer.find_warmup_hours(
                comfort_heating_first_range, comfort_heating_second_range
            )
            cheap_morning_hour = self._price_analyzer.cheap_morning_hour

            self.apply_multi_settings(IDLE_SETTINGS, True)

            self.run_boost_rampup_to_comfort(
                0,
                cheap_morning_hour,
                cheap_morning_hour == (comfort_heating_first_range.start - 1),
                comfort_heating_first_range.start,
            )

            self.manage_comfort_hours([comfort_heating_first_range.start])

            self.wait_for_hour(comfort_heating_first_range.start + 1)
            self.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

            keep_comfort_until_hour = WORKDAY_COMFORT_UNTIL_HOUR
            if optimizing_a_workday:
                self.run_workday_8_to_22_schedule()
            else:
                keep_comfort_until_hour = WEEKEND_COMFORT_UNTIL_HOUR
                self.manage_comfort_hours(
                    range(DAYOFF_MORNING["eat_until_hour"], WEEKEND_COMFORT_UNTIL_HOUR)
                )

            pause.until(
                self._prev_midnight
                + timedelta(hours=keep_comfort_until_hour - 1, minutes=30)
            )
            self.apply_multi_settings(COMFORT_HEAT_SETTINGS)

            self.wait_for_hour(22)
            self.apply_multi_settings(IDLE_SETTINGS)

            self._price_analyzer.prepare_next_day(
                self._prev_midnight.date() + timedelta(days=1)
            )

            if self._price_analyzer.significantly_more_expensive_after_midnight:
                self.wait_for_hour(23)
                self.apply_multi_settings(MAX_HEAT_SETTINGS)
                self.wait_for_hour(24)

            self._prev_midnight += timedelta(days=1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sensibo nordpool heating optimizer")
    parser.add_argument(
        "apikey", type=str, help="API KEY from https://home.sensibo.com/me/api"
    )
    parser.add_argument(
        "-d", "--device", type=str, default=None, required=False, dest="deviceName"
    )
    args = parser.parse_args()

    optimizer = SensiboOptimizer()

    while True:
        try:
            optimizer.client = sensibo_client.SensiboClientAPI(args.apikey)
            optimizer.run(args.deviceName)
        except requests.exceptions.ReadTimeout:
            print("Resetting optimizer due to error 2")
            sleep(300)
        except requests.exceptions.ConnectTimeout:
            print("Resetting optimizer due to error 3")
            sleep(300)
        except requests.exceptions.Timeout:
            print("Resetting optimizer due to error 1")
            sleep(300)
        except requests.exceptions.ConnectionError:
            print("Resetting optimizer due to error 5")
            sleep(300)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                print("403: check the API key")
                sys.exit(1)
            print("Resetting optimizer due to error 6")
            sleep(300)
        except requests.exceptions.RequestException:
            print("Resetting optimizer due to error 4")
            sleep(300)
