#!/usr/bin/env python3

"""
usage:
python3 -m pip install nordpool
python3 ellevio_hourly_costs.py
tested only with winter time exports
"""

from datetime import date
import csv
from nordpool import elspot

CET_CEST_00_TO_01 = 0
MW_TO_KW = 1000
SEK_TO_ORE = 100
REGION = "SE3"
ELLEVIO_HOURLY_DATA = "Förbrukning.csv"
spot_prices = elspot.Prices("SEK")


def analyze_ellevio_hourly_costs(csv_file_name, region):
    """
    Parses all rows in an Ellevio hourly consuption data export looks up the raw costs from Nordpool
    """
    print(
        "Kostnader i SEK utan certifikat, moms, påslag, skatter och elnät vid timmätt debitering"
    )

    with open(csv_file_name, mode="r", newline="", encoding="UTF-8") as csvfile:
        datareader = csv.reader(csvfile, delimiter=",")
        prev_day = None
        first_day = None
        most_expensive_hour = None
        most_expensive_hour_sek_cost = 0
        day_cost = 0
        total_cost = 0
        this_hour = None
        day_spot_prices = {}
        for row in datareader:
            century = row[0][:2]
            if century != "20":
                # Skip labels
                continue

            this_day = date.fromisoformat(row[0].split()[0])
            this_hour_kw = float(row[1])
            if prev_day != this_day:
                if prev_day is not None:
                    most_expensive_hour_cost = int(
                        most_expensive_hour_sek_cost * SEK_TO_ORE
                    )
                    print(
                        f"\n{prev_day} kostade {round(day_cost, 2)}kr:"
                        + f"\nDyraste timmen började {most_expensive_hour}:00 "
                        + f"och kostade {most_expensive_hour_cost}öre"
                    )
                else:
                    first_day = this_day
                this_hour = CET_CEST_00_TO_01
                most_expensive_hour = this_hour
                day_spot_prices = spot_prices.hourly(end_date=this_day, areas=[region])[
                    "areas"
                ][region]["values"]
                most_expensive_hour_sek_cost = (
                    this_hour_kw * float(day_spot_prices[this_hour]["value"]) / MW_TO_KW
                )
                total_cost = total_cost + day_cost
                day_cost = most_expensive_hour_sek_cost
                prev_day = this_day
            else:
                this_hour = this_hour + 1
                this_hour_cost = (
                    this_hour_kw * float(day_spot_prices[this_hour]["value"]) / MW_TO_KW
                )
                day_cost = day_cost + this_hour_cost
                if this_hour_cost > most_expensive_hour_sek_cost:
                    most_expensive_hour_sek_cost = this_hour_cost
                    most_expensive_hour = this_hour

        most_expensive_hour_cost = int(most_expensive_hour_sek_cost * SEK_TO_ORE)
        print(
            f"\n{prev_day} kostade {round(day_cost, 2)}kr:"
            + f"\nDyraste timmen började {most_expensive_hour}:00 "
            + f"och kostade {most_expensive_hour_cost}öre"
        )
        total_cost = total_cost + day_cost
        print(
            f"\n\nTotal kostnad för perioden {first_day} tom {this_day}: {total_cost}kr"
        )


if __name__ == "__main__":
    analyze_ellevio_hourly_costs(ELLEVIO_HOURLY_DATA, REGION)
