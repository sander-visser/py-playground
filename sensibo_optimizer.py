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


REGION = "SE3"
REGION_HOLIDAYS = holidays.country_holidays("SE")
TIME_ZONE = "CET"
ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY = 1.05
COMFORT_WORKDAY_MORNING_HEATING_BY_HOUR = 6
COMFORT_DAYOFF_MORNING_HEATING_BY_HOUR = 7
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
MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE = 22.5
MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE = 19.0

IDLE_SETTINGS = {
    "on": True,
    "mode": "heat",
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 17,
}

MAX_HEAT_SETTINGS = {
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "high",
    "targetTemperature": 23,
}

COMFORT_HEAT_SETTINGS = {
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}

COMFORT_PLUS_HEAT_SETTINGS = {
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 22,
}

COMFORT_ALT_HEAT_SETTINGS = {
    "horizontalSwing": "fixedLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}

COMFORT_EATING_HEAT_SETTINGS = {
    "horizontalSwing": "fixedCenterRight",
    "swing": "fixedMiddle",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}


class SensiboOptimizer:
    def __init__(self):
        self.client = None
        self._uid = None
        self._cheap_afternoon_hour = None
        self._reasonably_priced_hours = None
        self._pre_heat_favorable_hours = None
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

    @staticmethod
    def find_warmup_hours(region, lookup_date, morning_comfort_by_hour):
        spot_prices = elspot.Prices("SEK")

        print(f"Getting prices for {lookup_date} to find cheap hours...")
        day_spot_prices = spot_prices.hourly(end_date=lookup_date, areas=[region])[
            "areas"
        ][region]["values"]

        price_period_price = None
        morning_heat_period_start_hour = None
        afternoon_heat_period_start_hour = None
        local_tz = pytz.timezone(TIME_ZONE)
        lowest_price = None
        reasonably_priced_hours = []
        pre_heat_favorable_hours = []
        for hour_price in day_spot_prices:
            if lowest_price is None or hour_price["value"] < lowest_price:
                lowest_price = hour_price["value"]

        curr_hour_idx = 0
        for hour_price in day_spot_prices:
            price_period_start_hour = hour_price["start"].astimezone(local_tz).hour
            print(
                f"{hour_price['start'].astimezone(local_tz)} @ {hour_price['value']} SEK/MWh"
            )
            if curr_hour_idx > 0 and hour_price["value"] > (
                (
                    day_spot_prices[curr_hour_idx - 1]["value"]
                    * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
                )
                + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
            ):
                pre_heat_favorable_hours.append(price_period_start_hour - 1)
            if (
                hour_price["value"]
                <= (
                    lowest_price
                    + RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST
                )
            ) or hour_price["value"] <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE:
                if (
                    (curr_hour_idx + 1) < len(day_spot_prices)
                    and (
                        hour_price["value"]
                        <= day_spot_prices[curr_hour_idx + 1]["value"]
                        or (price_period_start_hour - 1) not in reasonably_priced_hours
                    )
                ) or hour_price["value"] <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_CHEAP:
                    reasonably_priced_hours.append(price_period_start_hour)

            if price_period_start_hour < morning_comfort_by_hour:
                if (
                    price_period_price is None
                    or hour_price["value"] <= price_period_price
                ):
                    price_period_price = hour_price["value"]
                    morning_heat_period_start_hour = price_period_start_hour
                price_period_price = (
                    price_period_price * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
                ) + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
            elif (
                EARLIEST_AFTERNOON_PREHEAT_HOUR
                <= price_period_start_hour
                <= BEGIN_AFTERNOON_HEATING_BY_HOUR
            ):
                if (
                    price_period_price is None
                    or hour_price["value"] <= price_period_price
                ):
                    price_period_price = hour_price["value"]
                    afternoon_heat_period_start_hour = price_period_start_hour
                price_period_price = (
                    price_period_price * ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
                ) + TRANSFER_AND_TAX_COST_PER_MWH_TO_PREHEAT_EARLY
            else:
                price_period_price = None
            curr_hour_idx += 1

        return (
            morning_heat_period_start_hour,
            afternoon_heat_period_start_hour,
            reasonably_priced_hours,
            pre_heat_favorable_hours,
        )

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

    def run_boost_rampup_to_comfort(
        self, boost_hour_start, short_boost, comfort_hour_start
    ):
        if short_boost:
            self.wait_for_hour(boost_hour_start - 1)
            current_floor_sensor_value = MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE
            for sample_minute in range(9, 60, 10):
                try:
                    current_floor_sensor_value = self.client.pod_measurement(self._uid)[
                        0
                    ]["temperature"]
                except requests.exceptions.ConnectionError:
                    print(
                        f"Ignoring temperature read error - using {current_floor_sensor_value}"
                    )

                sleep(SECONDS_BETWEEN_COMMANDS)
                if current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
                    self.apply_multi_settings(MAX_HEAT_SETTINGS)
                else:
                    self.apply_multi_settings(COMFORT_ALT_HEAT_SETTINGS)
                pause.until(
                    self._prev_midnight
                    + timedelta(hours=boost_hour_start - 1, minutes=sample_minute)
                )
        self.wait_for_hour(boost_hour_start)
        self.apply_multi_settings(MAX_HEAT_SETTINGS)
        pause_setting = copy.deepcopy(COMFORT_HEAT_SETTINGS)
        for pause_hour in range(boost_hour_start + 1, comfort_hour_start):
            self.wait_for_hour(pause_hour)
            pause_setting["targetTemperature"] = int(
                COMFORT_HEAT_SETTINGS["targetTemperature"]
                - (comfort_hour_start - pause_hour) * DEGREES_PER_HOUR_DURING_RAMPUP
            )
            if pause_hour in self._pre_heat_favorable_hours:
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
            + timedelta(hours=WORKDAY_MORNING_COMFORT_UNTIL_HOUR - 1, minutes=30)
        )
        self.apply_multi_settings(COMFORT_HEAT_SETTINGS)

        self.wait_for_hour(WORKDAY_MORNING_COMFORT_UNTIL_HOUR)
        self.apply_multi_settings(IDLE_SETTINGS)

        self.run_boost_rampup_to_comfort(
            self._cheap_afternoon_hour,
            self._cheap_afternoon_hour == BEGIN_AFTERNOON_HEATING_BY_HOUR,
            WORKDAY_AFTERNOON_COMFORT_BY_HOUR,
        )

        self.manage_comfort_hours([WORKDAY_AFTERNOON_COMFORT_BY_HOUR])

        self.wait_for_hour(17)
        self.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

        self.manage_comfort_hours(range(18, WORKDAY_COMFORT_UNTIL_HOUR))

    def manage_comfort_hours(self, comfort_range):
        current_floor_sensor_value = MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
        for comfort_hour in comfort_range:
            self.wait_for_hour(comfort_hour)
            for sample_minute in range(9, 60, 10):
                try:
                    current_floor_sensor_value = self.client.pod_measurement(self._uid)[
                        0
                    ]["temperature"]
                except requests.exceptions.ConnectionError:
                    print(
                        f"Ignoring temperature read error - using {current_floor_sensor_value}"
                    )

                sleep(SECONDS_BETWEEN_COMMANDS)
                if current_floor_sensor_value < MIN_FLOOR_SENSOR_COMFORT_TEMPERATURE:
                    self.apply_multi_settings(MAX_HEAT_SETTINGS)
                elif (
                    sample_minute == 59  # boost 49-59 if price will rise
                    and comfort_hour in self._pre_heat_favorable_hours
                    and current_floor_sensor_value
                    <= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                elif (
                    current_floor_sensor_value
                    <= MAX_FLOOR_SENSOR_COMFORT_PLUS_TEMPERATURE
                ):
                    if comfort_hour in self._reasonably_priced_hours:
                        self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                    else:
                        self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                else:
                    self.apply_multi_settings(COMFORT_ALT_HEAT_SETTINGS)
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

        while True:
            optimizing_a_workday = (
                self._prev_midnight.date().isoweekday() not in AT_HOME_DAYS
            ) and self._prev_midnight.date() not in REGION_HOLIDAYS
            comfort_heating_by_hour = (
                COMFORT_WORKDAY_MORNING_HEATING_BY_HOUR
                if optimizing_a_workday
                else COMFORT_DAYOFF_MORNING_HEATING_BY_HOUR
            )
            (
                cheap_morning_hour,
                self._cheap_afternoon_hour,
                self._reasonably_priced_hours,
                self._pre_heat_favorable_hours,
            ) = self.find_warmup_hours(
                REGION, self._prev_midnight.date(), comfort_heating_by_hour
            )

            self.apply_multi_settings(IDLE_SETTINGS, True)

            self.run_boost_rampup_to_comfort(
                cheap_morning_hour,
                cheap_morning_hour == (comfort_heating_by_hour - 1),
                comfort_heating_by_hour,
            )

            self.manage_comfort_hours([comfort_heating_by_hour])

            self.wait_for_hour(comfort_heating_by_hour + 1)
            self.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

            keep_comfort_until_hour = WORKDAY_COMFORT_UNTIL_HOUR
            if optimizing_a_workday:
                self.run_workday_8_to_22_schedule()
            else:
                keep_comfort_until_hour = WEEKEND_COMFORT_UNTIL_HOUR
                self.manage_comfort_hours(range(8, WEEKEND_COMFORT_UNTIL_HOUR))

            pause.until(
                self._prev_midnight
                + timedelta(hours=keep_comfort_until_hour - 1, minutes=30)
            )
            self.apply_multi_settings(COMFORT_HEAT_SETTINGS)

            self.wait_for_hour(22)
            self.apply_multi_settings(IDLE_SETTINGS)

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
