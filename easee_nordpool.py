#!/usr/bin/env python

"""
Cost summarizer for Easee EV charger (using nordpool spot prices)

MIT license (as the rest of the repo)

If you plan to migrate to Tibber electricity broker I can provide a referral
giving us both 500 SEK to shop gadgets with. Contact: github[a]visser.se or
check referral.link in repo

Usage:
python3.10 or later needed for current nordpool API
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
CHARGE_SESSION_DURATION_THRES = 1.0


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
                f"Error getting chargers. Error: {chargers_req.status_code}; {chargers_req.text}"
            )
            if chargers_req.status_code == 401:
                print("Check API key is not expired...")
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

    def get_day_spot_prices(self, looked_up_date):
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
        return day_spot_prices

    @staticmethod
    def print_fees_report(cost_settings, total_kwh, peak_contribution, nordpool_cost):
        total_fee = 0.0
        if cost_settings.fees_and_tax_excl_vat is not None:
            for fee in cost_settings.fees_and_tax_excl_vat.split(","):
                this_fee = float(fee)
                total_fee += this_fee
                print(
                    f"Fee w/o VAT {(total_kwh * this_fee):.03f} {NORDPOOL_PRICE_CODE}"
                    + f" @ {this_fee} {NORDPOOL_PRICE_CODE} / kWh"
                )
        total_cost = (
            (total_fee * total_kwh + nordpool_cost)
            + (peak_contribution * cost_settings.pwr_fee_excl_vat)
        ) * VAT_SCALE
        print(
            f"Total cost incl all fees and VAT: {(total_cost ):.3f} {NORDPOOL_PRICE_CODE}"
        )

    def print_cost_report(self, charger_id, cost_settings, date_range):
        total_kwh = 0.0
        peak_kwh_per_hour = 0.0
        peak_contribution = None
        total_cost = 0.0
        one_kw_diff_price = 0.0
        looked_up_date = None
        day_spot_prices = None
        charged_last_hour = False
        hour_cost_before_charge_start = None
        hour_cost_first_charge_hour = None
        session_duration_hours = 0.0
        slower_cost = 0.0
        faster_savings = 0.0
        for hour_data in self.get_hourly_energy_json(
            charger_id, date_range[0], date_range[1]
        ):
            curr_zulu_date = datetime.datetime.strptime(
                hour_data["date"], "%Y-%m-%dT%H:%M:%S%z"
            )
            curr_date = curr_zulu_date.astimezone(
                datetime.timezone(datetime.timedelta(hours=CHARGER_TIMEZONE_OFFSET))
            )
            if hour_data["consumption"] == 0.0:
                if charged_last_hour and self.verbose:
                    print(
                        f"Summing up charge session that lasted {session_duration_hours} hours"
                    )
                    if session_duration_hours <= CHARGE_SESSION_DURATION_THRES:
                        print ("Short charge session...\n")
                    
                if charged_last_hour and session_duration_hours > CHARGE_SESSION_DURATION_THRES:
                    prolonged_hour_cost = hour_cost_before_charge_start
                    if hour_cost_after_charge_end < hour_cost_before_charge_start:
                        prolonged_hour_cost = hour_cost_after_charge_end
                    slower_contribution = (
                        prolonged_hour_cost * session_duration_hours
                    ) - one_kw_diff_price
                    if slower_contribution > 0.0:
                        slower_cost += slower_contribution
                    most_expensive_charge_hour_cost = cost_of_last_charge_hour
                    if hour_cost_first_charge_hour > cost_of_last_charge_hour:
                        most_expensive_charge_hour_cost = hour_cost_first_charge_hour
                    faster_contribution = (
                        most_expensive_charge_hour_cost
                        + (
                            (most_expensive_charge_hour_cost)
                            * (session_duration_hours - 1)
                        )
                    ) - one_kw_diff_price
                    if faster_contribution > 0.0:
                        faster_savings += faster_contribution
                    if self.verbose:
                        print(
                            f"Slower charging could be done during hour with cost {prolonged_hour_cost:.3f}"
                        )
                        print(
                            f"Faster charging would avoid charging during hour with cost {most_expensive_charge_hour_cost:.3f}"
                        )
                        print(
                            f"Session rate contribution; Faster {faster_contribution:.3f}. Slower {slower_contribution:.3f}\n"
                        )
                charged_last_hour = False
                session_duration_hours = 0.0

            else:
                if peak_kwh_per_hour < hour_data["consumption"]:
                    peak_kwh_per_hour = hour_data["consumption"]
                if (
                    cost_settings.pwr_fee_peak_hour is not None
                    and curr_date == cost_settings.pwr_fee_peak_hour
                ):
                    peak_contribution = hour_data["consumption"]

                total_kwh += hour_data["consumption"]
                hour_cost = None
                if args.region is not None:
                    if looked_up_date is None or curr_date.date() != looked_up_date:
                        looked_up_date = curr_date.date()
                        day_spot_prices = self.get_day_spot_prices(looked_up_date)
                    curr_hour_price = (
                        day_spot_prices[curr_date.hour]["value"] / KWH_PER_MWH
                    )
                    if not charged_last_hour and hour_data["consumption"] > 1.0:
                        charged_last_hour = True
                        one_kw_diff_price = 0.0
                        hour_cost_before_charge_start = (
                            day_spot_prices[max(0, curr_date.hour - 1)]["value"]
                            / KWH_PER_MWH
                        )
                        hour_cost_first_charge_hour = curr_hour_price
                    session_duration_hours += 1
                    one_kw_diff_price += curr_hour_price
                    hour_cost = hour_data["consumption"] * curr_hour_price
                    total_cost += hour_cost
                    # somewhat inexact if ending during last hour of the day
                    hour_after_charge = curr_date.hour  + 1 if curr_date.hour != 23 else 0
                    if hour_data["consumption"] > 1.0:
                        hour_cost_after_charge_end = (
                            day_spot_prices[hour_after_charge]["value"] / KWH_PER_MWH
                        )
                        cost_of_last_charge_hour = (
                            day_spot_prices[curr_date.hour]["value"] / KWH_PER_MWH
                        )

                if hour_cost is not None and self.verbose:
                    print(
                        f"{hour_data['consumption']:.3f} kWh used at hour starting on {curr_date}."
                        + f" Cost was {hour_cost:.3f} @ {curr_hour_price:.3f} {NORDPOOL_PRICE_CODE}"
                    )
                    if not charged_last_hour:
                        print ("Tiny charge not considdered part of a charge session...\n")

        print(f"\nPeak kWh/h {peak_kwh_per_hour:.03f}")
        if peak_contribution is not None:
            print(f"Contribution to peak hour {peak_contribution:.03f} kWh/h")
        else:
            print(
                "No peak hour supplied / not charging at provided hour. Using 100% contributuion."
            )
            peak_contribution = peak_kwh_per_hour
        if cost_settings.pwr_fee_excl_vat > 0.0:
            print(
                f"Total powerfee is {(peak_contribution*cost_settings.pwr_fee_excl_vat):.03f} "
                + f"{NORDPOOL_PRICE_CODE} (without VAT and fees)"
            )

        if slower_cost != 0.0:
            print(
                f" - By charging 1 kW slower energy cost would rise by approx {slower_cost:.3f} {NORDPOOL_PRICE_CODE}"
            )
        if faster_savings != 0.0:
            print(
                f" - By charging 1 kW faster energy cost would drop by approx {faster_savings:.3f} {NORDPOOL_PRICE_CODE}"
            )

        print(f"\nTotal consumption: {total_kwh:.3f} kWh")
        if self.region is not None and total_kwh > 0.0:
            print(f"Energy cost in {self.region} (without VAT and fees)")
            print(f" - Summarized cost: {(total_cost ):.3f} {NORDPOOL_PRICE_CODE}")
            print(
                f" - Average cost in {self.region} {(total_cost/total_kwh ):.3f}"
                + f" {NORDPOOL_PRICE_CODE} / kWh"
            )
        self.print_fees_report(cost_settings, total_kwh, peak_contribution, total_cost)


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
        "api_access_token",
        type=str,
        help="API Access token from " + LOGIN_HELP + ". Note: expires in an hour unless refreshed",
    )
    parser.add_argument(
        "-rft",
        dest="api_refresh_token",
        type=str,
        help="API Refresh token from " + LOGIN_HELP,
        required=False,
    )
    parser.add_argument(
        "-f",
        dest="from_date",
        type=str,
        help="Zulu ISO_8601 date of earliest consumed energy to include (ex: 2024-12-30T23:00:00Z)."
        + " Note that nordpool does not supply older price data than 3 months back.",
        default="2024-12-30T23:00:00Z",
        required=False,
    )
    parser.add_argument(
        "-t",
        dest="to_date",
        type=str,
        help="Zulu ISO_8601 date of first consumed energy to exclude (ex: 2025-01-31T23:00:00Z)",
        default="2025-01-31T23:00:00Z",
        required=False,
    )
    parser.add_argument(
        "-r",
        dest="region",
        type=str,
        help="Nordpool region code. SE3 for instance."
        + " Note that nordpool does not supply older price data than 3 months back.",
        default=None,
        required=False,
    )
    parser.add_argument(
        "-power-fee",
        dest="pwr_fee_excl_vat",
        type=float,
        help="Cost for peak power use (per kWh/h excl VAT) in the analyzed period"
        + ". For instance 26 SEK/peak kW in Partille",
        default=0.0,
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
        "-fees",
        dest="fees_and_tax_excl_vat",
        type=str,
        help="Cost for fees and taxes per kWh (excl VAT). Comma separated"
        + ' For instance "0.244,0.439,0.06904" for transmission, energytax, certificates etc.'
        + " (Example is for Partille Energi with normal tax via Tibber in Jan 2025)",
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
        f"\nGenerating Nordpool cost report in Nordpool region: {args.region}"
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
