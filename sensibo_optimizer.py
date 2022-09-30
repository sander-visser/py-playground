#!/usr/bin/env python3

"""
Usage:
Install needed pip packages
Run this script on a internet connected machine configured with relevant timezone
Script tested with a Sensibo Sky
Adapt constants as needed for your home
"""

from datetime import datetime, timedelta
from time import sleep
import sys

# "python3 -m pip install X" below python modules
import requests
import pause
from nordpool import elspot
import pytz
import sensibo_client  # https://github.com/Sensibo/sensibo-python-sdk with py3 print fix


REGION = "SE3"
TIME_ZONE = "CET"
ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY = 1.05
BEGIN_MORNING_HEATING_BY_HOUR = 5
BEGIN_AFTERNOON_HEATING_BY_HOUR = 14
SECONDS_BETWEEN_COMMANDS = 1.5
AT_HOME_DAYS = [5, 6, 7]
ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE = 750.0
RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST = 600.0

IDLE_SETTINGS = {
    "on": True,
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 17,
}

MAX_HEAT_SETTINGS = {
    "on": True,
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "high",
    "targetTemperature": 23,
}

COMFORT_HEAT_SETTINGS = {
    "on": True,
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 21,
}

COMFORT_PLUS_HEAT_SETTINGS = {
    "on": True,
    "horizontalSwing": "fixedCenterLeft",
    "swing": "fixedTop",
    "fanLevel": "medium_high",
    "targetTemperature": 22,
}

COMFORT_EATING_HEAT_SETTINGS = {
    "on": True,
    "horizontalSwing": "fixedCenterRight",
    "swing": "fixedMiddle",
    "fanLevel": "medium_high",
    "targetTemperature": 20,
}


class SensiboOptimizer:
    def __init__(self, api_key, device_name):
        self._api_key = api_key
        self._client = None
        self._uid = None
        self._device_name = device_name
        self._program_start_time = datetime.today()
        self._current_settings = {}
        self._prev_midnight = datetime(
            self._program_start_time.year,
            self._program_start_time.month,
            self._program_start_time.day,
            0,
            0,
        )

    def wait_for_hour(self, hour):
        pause.until(
            self._prev_midnight + timedelta(hours=hour)
        )  # Direct return if in the past...
        print(f"At {hour}:00")

    @staticmethod
    def find_warmup_hours(region):
        spot_prices = elspot.Prices("SEK")

        print("getting day prices to find cheap hours...")
        day_spot_prices = spot_prices.hourly(
            end_date=datetime.today().date(), areas=[region]
        )["areas"][region]["values"]

        nightly_price = None
        morning_heat_period_start_hour = 0
        afternoon_heat_period_start_hour = 10
        afternoon_price = None
        local_tz = pytz.timezone(TIME_ZONE)
        lowest_price = None
        reasonalby_priced_hours = []
        for hour_price in day_spot_prices:
            if lowest_price is None or hour_price["value"] < lowest_price:
                lowest_price = hour_price["value"]

        for hour_price in day_spot_prices:
            print(
                f"Analyzing pricing for: {hour_price['start'].astimezone(local_tz)}: "
                + f"{hour_price['value']}"
            )
            price_period_start_hour = hour_price["start"].astimezone(local_tz).hour
            if (
                hour_price["value"]
                <= (
                    lowest_price
                    + RELATIVE_SEK_PER_MWH_TO_CONSIDER_REASONABLE_WHEN_COMPARED_TO_CHEAPEST
                )
            ) or hour_price["value"] <= ABSOLUTE_SEK_PER_MWH_TO_CONSIDER_REASONABLE:
                reasonalby_priced_hours.append(price_period_start_hour)

            if price_period_start_hour < BEGIN_MORNING_HEATING_BY_HOUR:
                if nightly_price is None or hour_price["value"] <= nightly_price:
                    nightly_price = hour_price["value"]
                    morning_heat_period_start_hour = price_period_start_hour
                nightly_price *= ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY
            if (
                afternoon_heat_period_start_hour
                < price_period_start_hour
                < BEGIN_AFTERNOON_HEATING_BY_HOUR
            ):
                if afternoon_price is None or hour_price["value"] <= afternoon_price:
                    afternoon_price = hour_price["value"]
                    afternoon_heat_period_start_hour = price_period_start_hour
                afternoon_price *= ACCEPTABLE_PRICE_INCREASE_FOR_ONE_HOUR_DELAY

        return (
            morning_heat_period_start_hour,
            afternoon_heat_period_start_hour,
            reasonalby_priced_hours,
        )

    def apply_multi_settings(self, settings, force=False):
        if force:
            self._current_settings = {}
        first_setting = True
        for setting in settings:
            if (
                setting not in self._current_settings
                or settings[setting] != self._current_settings[setting]
            ):
                self._current_settings[setting] = settings[setting]
                self._client.pod_change_ac_state(
                    self._uid, None, setting, settings[setting]
                )
                if not first_setting:
                    sleep(SECONDS_BETWEEN_COMMANDS)
                first_setting = False

    def run_workday_8_to_22_schedule(
        self, cheap_afternoon_hour, reasonably_priced_hours
    ):
        self.wait_for_hour(8)
        self.apply_multi_settings(IDLE_SETTINGS)

        self.wait_for_hour(cheap_afternoon_hour)
        self.apply_multi_settings(MAX_HEAT_SETTINGS)

        for pause_hour in range(cheap_afternoon_hour + 1, 16):
            self.wait_for_hour(pause_hour)
            if pause_hour in reasonably_priced_hours:
                self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
            else:
                self.apply_multi_settings(IDLE_SETTINGS)

        self.wait_for_hour(16)
        self.apply_multi_settings(COMFORT_HEAT_SETTINGS)

        self.wait_for_hour(17)
        self.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

        for pause_hour in range(18, 22):
            self.wait_for_hour(pause_hour)
            if pause_hour in reasonably_priced_hours:
                self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
            else:
                self.apply_multi_settings(COMFORT_HEAT_SETTINGS)

    def run(self):
        self._client = sensibo_client.SensiboClientAPI(self._api_key)
        devices = self._client.devices()
        print("-" * 10, "devices", "-" * 10)
        print(devices)

        if len(devices) == 0:
            print("No devices present in account associated with API key...")
            sys.exit(0)

        if self._device_name is None:
            print("No device selected for optimization - exiting")
            sys.exit(0)

        self._uid = devices[self._device_name]
        print("-" * 10, f"AC State of {self._device_name}", "_" * 10)
        try:
            print(self._client.pod_measurement(self._uid))
            ac_state = self._client.pod_ac_state(
                self._uid
            )  # If no result then stop/start in the Sensibo App
            print(ac_state)
        except IndexError:
            print(
                "Warning: Server does not know current state - try to stop/start in the Sensibo App"
            )

        while True:
            (
                cheap_morning_hour,
                cheap_afternoon_hour,
                reasonably_priced_hours,
            ) = self.find_warmup_hours(REGION)

            self.apply_multi_settings(IDLE_SETTINGS, True)

            self.wait_for_hour(cheap_morning_hour)
            self.apply_multi_settings(MAX_HEAT_SETTINGS)

            for pause_hour in range(cheap_morning_hour + 1, 6):
                self.wait_for_hour(pause_hour)
                if pause_hour in reasonably_priced_hours:
                    self.apply_multi_settings(COMFORT_HEAT_SETTINGS)
                else:
                    self.apply_multi_settings(IDLE_SETTINGS)

            self.wait_for_hour(6)
            self.apply_multi_settings(COMFORT_HEAT_SETTINGS)

            self.wait_for_hour(7)
            self.apply_multi_settings(COMFORT_EATING_HEAT_SETTINGS)

            if self._prev_midnight.date().isoweekday() not in AT_HOME_DAYS:
                self.run_workday_8_to_22_schedule(
                    cheap_afternoon_hour, reasonably_priced_hours
                )
            else:
                for pause_hour in range(8, 22):
                    self.wait_for_hour(pause_hour)
                    if pause_hour in reasonably_priced_hours:
                        self.apply_multi_settings(COMFORT_PLUS_HEAT_SETTINGS)
                    else:
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

    optimizer = SensiboOptimizer(args.apikey, args.deviceName)

    while True:
        try:
            optimizer.run()
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
