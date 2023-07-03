#!/usr/bin/env python

"""
Cost summarizer for Easee EV charger (using nordpool spot prices)

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both 500 SEK to shop gadgets with. Contact: github[a]visser.se or
check referral.link in repo

Usage:
Install needed pip packages (see below pip module imports)
"""

import datetime
import sys
import requests

# "python3 -m pip install X" below python module(s)
from nordpool import elspot

NORDPOOL_PRICE_CODE = "SEK"
CHARGER_TIMEZONE_OFFSET = (
    1  # Do not adjust for daylight savings - use from/to Zulu adjust
)
HTTP_SUCCESS_CODE = 200
KWH_PER_MWH = 1000
VAT_SCALE = 1.25  # 25%
API_TIMEOUT = 10.0  # seconds
EASEE_API_BASE = "https://api.easee.com/api"
CHARGER_ID_URL = f"{EASEE_API_BASE}/chargers"
REFRESH_TOKEN_URL = f"{EASEE_API_BASE}/api/accounts/refresh_token"


def refresh_api_token(prev_api_access_token, api_refresh_token):
    refresh_payload = (
        f'{{"accessToken":"{prev_api_access_token}",'
        + f'"refreshToken":"{api_refresh_token}"}}'
    )
    refresh_headers = {
        "accept": "application/json",
        "content-type": "application/*+json",
    }

    response = requests.post(
        REFRESH_TOKEN_URL,
        data=refresh_payload,
        headers=refresh_headers,
        timeout=API_TIMEOUT,
    )
    next_token = response.json()
    print(
        f"Use this access + refresh token next time (within {next_token['expiresIn']} seconds):"
    )
    print(f"{next_token['accessToken']} {next_token['refreshToken']}")
    return next_token["accessToken"]


class EaseeCostAnalyzer:
    def __init__(self, api_access_token, region, verbose):
        self.api_header = {
            "accept": "application/json",
            "Authorization": "Bearer " + api_access_token,
        }
        self.region = region
        self.verbose = verbose
        self.spot_prices = elspot.Prices(NORDPOOL_PRICE_CODE)

    def get_chargers(self):
        chargers = []
        chargers_req = requests.get(
            CHARGER_ID_URL, headers=self.api_header, timeout=API_TIMEOUT
        )
        if chargers_req.status_code != 200:
            print(
                f"Error getting chargers {chargers_req.status_code}: {chargers_req.text}"
            )
            sys.exit(1)
        chargers_json = chargers_req.json()
        for charger_data in chargers_json:
            chargers.append((charger_data["id"], charger_data["name"]))
        return chargers

    def get_hourly_energy_json(self, charger_id, from_date, to_date):
        hourly_energy_url = (
            f"{EASEE_API_BASE}/chargers/lifetime-energy/{charger_id}/hourly?"
            + f"from={from_date}&to={to_date}"
        )
        hourly_energy = requests.get(
            hourly_energy_url, headers=self.api_header, timeout=API_TIMEOUT
        )
        if hourly_energy.status_code != HTTP_SUCCESS_CODE:
            print(f"Error: {hourly_energy.text}")
            sys.exit(1)
        return hourly_energy.json()

    def print_cost_report(self, charger_id, cost_settings, date_range):
        total_kwh = 0.0
        peak_kwh_per_hour = 0.0
        peak_contribution = None
        total_cost = 0.0
        looked_up_date = None
        day_spot_prices = None
        for hour_data in self.get_hourly_energy_json(
            charger_id, date_range[0], date_range[1]
        ):
            if hour_data["consumption"] != 0.0:
                if peak_kwh_per_hour < hour_data["consumption"]:
                    peak_kwh_per_hour = hour_data["consumption"]
                py36compat_date = hour_data["date"]
                if py36compat_date.rindex(":") > py36compat_date.rindex("+"):
                    py36compat_date = (
                        py36compat_date[0 : py36compat_date.rindex(":")]
                        + py36compat_date[py36compat_date.rindex(":") + 1 :]
                    )
                curr_zulu_date = datetime.datetime.strptime(
                    py36compat_date, "%Y-%m-%dT%H:%M:%S%z"
                )
                if (
                    cost_settings.pwr_fee_peak_hour is not None
                    and py36compat_date == cost_settings.pwr_fee_peak_hour
                ):
                    peak_contribution = hour_data["consumption"]
                curr_date = curr_zulu_date.astimezone(
                    datetime.timezone(datetime.timedelta(hours=CHARGER_TIMEZONE_OFFSET))
                )
                total_kwh += hour_data["consumption"]
                if looked_up_date is None or curr_date.date() != looked_up_date:
                    looked_up_date = curr_date.date()
                    day_spot_prices = None
                    try:
                        day_spot_prices = self.spot_prices.hourly(
                            end_date=looked_up_date, areas=[self.region]
                        )["areas"][self.region]["values"]
                    except KeyError:
                        print("retrying Nordpool price fetching...")
                    if day_spot_prices is None:
                        day_spot_prices = self.spot_prices.hourly(
                            end_date=looked_up_date, areas=[self.region]
                        )["areas"][self.region]["values"]

                    # print(f"Prices for {looked_up_date}: {day_spot_prices}")
                hour_cost = (
                    hour_data["consumption"]
                    * day_spot_prices[curr_date.hour]["value"]
                    / KWH_PER_MWH
                )
                if self.verbose:
                    print(
                        f"{hour_data['consumption']:.3f} kWh used at hour starting on {curr_date}."
                        + f" Cost was {hour_cost:.3f} {NORDPOOL_PRICE_CODE}"
                    )
                total_cost += hour_cost

        print(f"\nTotal consumption: {total_kwh:.3f} kWh")
        print(f"Peak kWh/h {peak_kwh_per_hour:.03f}")
        if peak_contribution is not None:
            print(f"Contribution to peak hour {peak_contribution:.03f} kWh/h")
        else:
            print(
                "No peak hour supplied / not charging at provided hour. Using 100% contributuion."
            )
            peak_contribution = peak_kwh_per_hour
        print(
            f"Total cost: {(total_cost ):.3f} {NORDPOOL_PRICE_CODE} (without VAT and fees)"
        )
        print(f"Average cost {(total_cost/total_kwh ):.3f} (without VAT and fees)")
        if cost_settings.fees_and_tax_excl_vat is not None:
            total_cost = (
                (cost_settings.fees_and_tax_excl_vat * total_kwh + total_cost)
                + (peak_contribution * cost_settings.pwr_fee_excl_vat)
            ) * VAT_SCALE
            print(
                f"Total cost incl all fees and VAT: {(total_cost ):.3f} {NORDPOOL_PRICE_CODE}"
            )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Easee nordpool cost summary")

    LOGIN_HELP = (
        "curl --request POST"
        + f"     --url {EASEE_API_BASE}/accounts/login"
        + "     --header 'accept: application/json'"
        + "     --header 'content-type: application/*+json'"
        + "     --data '"
        + "{"
        + '     "userName": "user",'
        + '     "password": "pass"'
        + "}"
        + "'"
    )
    parser.add_argument(
        "api_access_token", type=str, help="API Access token form " + LOGIN_HELP
    )
    parser.add_argument(
        "-rft",
        dest="api_refresh_token",
        type=str,
        help="API Refresh token form " + LOGIN_HELP,
        required=False,
    )
    parser.add_argument(
        "-f",
        dest="from_date",
        type=str,
        help="Zulu ISO_8601 date of earliest consumed energy to include (ex: 2022-12-30T23:00:00Z)",
        default="2022-12-30T23:00:00Z",
        required=False,
    )
    parser.add_argument(
        "-t",
        dest="to_date",
        type=str,
        help="Zulu ISO_8601 date of first consumed energy to exclude (ex: 2023-01-31T23:00:00Z)",
        default="2023-01-31T23:00:00Z",
        required=False,
    )
    parser.add_argument(
        "-r",
        dest="region",
        type=str,
        help="Nordpool region code",
        default="SE3",
        required=False,
    )
    parser.add_argument(
        "-power-fee",
        dest="pwr_fee_excl_vat",
        type=float,
        help="Cost for peak power use (per kWh/h excl VAT) in the analyzed period"
        + ". For instance 23.6 SEK/peak kW in Partille",
        default=None,
        required=False,
    )
    parser.add_argument(
        "-power-fee-peak-hour",
        dest="pwr_fee_peak_hour",
        type=str,
        help="Zulu start time for hour that was used as peak bill hour"
        + ". For instance 2023-01-23T01:00:00+0000",
        default=None,
        required=False,
    )
    parser.add_argument(
        "-fee",
        dest="fees_and_tax_excl_vat",
        type=float,
        help="Cost for fees and taxes per kWh (excl VAT)."
        + ' For instance "0.7209" for transmission, energytax, certificates etc.'
        + " (24.4 + 39.2 + 8.49 öre for Partille Energi with normal tax via Tibber in July 2023)",
        default=None,
        required=False,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        default=False,
        required=False,
        dest="verbose",
        help="increase output verbosity",
        action="store_true",
    )

    args = parser.parse_args()

    api_token = (
        refresh_api_token(args.api_access_token, args.api_refresh_token)
        if args.api_refresh_token is not None
        else args.api_access_token
    )
    cost_analyzer = EaseeCostAnalyzer(api_token, args.region, args.verbose)

    print(
        f"\nGenerating Nordpool cost report in {args.region}"
        + f" for period {args.from_date} - {args.to_date}"
    )

    for charger in cost_analyzer.get_chargers():
        print("\n======")
        print(f"Cost report for {charger[1]} ({charger[0]})")
        cost_analyzer.print_cost_report(
            charger[0],
            args,
            (args.from_date, args.to_date),
        )
        print("======\n")
