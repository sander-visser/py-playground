#!/usr/bin/env python

"""
Use the Tibber App interface to adjust power control level to maximize grid rewards.
"""

import datetime
import time

import requests

LOGIN_HEADERS = {"Content-Type": "application/x-www-form-urlencoded"}
REQUEST_BODY = {"email": "user@example.com", "password": "very_secret"}
FIRST_QS_BOOST = 0.25  # 50% extra first quarter and 25% extra second
PWR_BUDGET = 7.5


QUERY = """
mutation SetPulseSettings($homeId: String!, $deviceId: String!, $settings: [SettingsItemInput!]!) { me { home(id: $homeId) { pulse(id: $deviceId) { setSettings(settings: $settings) { __typename ...PulseItem } } } } } fragment callToActionItem on CallToAction { text url redirectUrlStartsWith link action }  fragment pulseErrorItem on PulseError { title description callToAction { __typename ...callToActionItem } }  fragment setting on Setting { key value valueType valueIsArray isReadOnly inputOptions { type title description unitText rangeOptions { max min step defaultValue displayText displayTextPlural } pickerOptions { values postFix } selectOptions { value title description imgUrl iconName isRecommendedOption } timeOptions { doNotSetATimeText } textFieldOptions { format placeholder imgUrl } } }  fragment settingLayoutFields on SettingLayoutItem { uid type title description valueText imgUrl iconName isUpdated isEnabled settingKey settingKeyForIsHidden callToAction { __typename ...callToActionItem } }  fragment settingsLayout on SettingLayoutItem { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields } } }  fragment PulseItem on Pulse { id name type shortName isAlive error { __typename ...pulseErrorItem } hasAccessToNewPulseScreen hasAccessToPeakControl hasAccessToConsumptionHistory hasPhaseCurrents peakControlStatus estimatedMeasurementsPerMinute settings2 { __typename ...setting } settingsLayout { __typename ...settingsLayout } mainScreen { tabConsumptionText tabPhaseText } energyDealAlert { title message cancelText callToAction { __typename ...callToActionItem } } }
"""

# TODO get the home id: "{\"operationName\":\"GetHomes\",\"variables\":{},\"query\":\"query GetHomes { me { homes { __typename ...HomeItem } } }  fragment CurrentMeterItem on CurrentMeter { id meterNo isUserRead }  fragment AstronomyItem on Astronomy { sunIsUp sunrise sunset }  fragment DateRangeItem on DateRange { from to }  fragment AddressItem on Address { addressText city postalCode country latitude longitude astronomy { __typename ...AstronomyItem } dayNightTimes { night { __typename ...DateRangeItem } sunrise { __typename ...DateRangeItem } sunset { __typename ...DateRangeItem } daytime { __typename ...DateRangeItem } } }  fragment callToActionItem on CallToAction { text url redirectUrlStartsWith link action }  fragment MessageItem on Message { id title description style iconSrc iconName callToAction { __typename ...callToActionItem } dismissButtonText }  fragment setting on Setting { key value valueType valueIsArray isReadOnly inputOptions { type title description unitText rangeOptions { max min step defaultValue displayText displayTextPlural } pickerOptions { values postFix } selectOptions { value title description imgUrl iconName isRecommendedOption } timeOptions { doNotSetATimeText } textFieldOptions { format placeholder imgUrl } } }  fragment settingLayoutFields on SettingLayoutItem { uid type title description valueText imgUrl iconName isUpdated isEnabled settingKey settingKeyForIsHidden callToAction { __typename ...callToActionItem } }  fragment settingsLayout on SettingLayoutItem { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields } } }  fragment HomeItem on Home { id timeZone hasSmartMeterCapabilities hasSignedEnergyDeal hasConsumption showMeterNo showMeteringPointId currentMeter { __typename ...CurrentMeterItem } meteringPointIdFormatted address { __typename ...AddressItem } mainPromotion { __typename ...MessageItem } settings { __typename ...setting } settingsLayout { __typename ...settingsLayout } imsOrders { hasOrder } avatar title type }\"}"


# TODO get device id from home id "{\"operationName\":\"GetHomeGizmos\",\"variables\":{\"homeId\":\"76dabd8e-017f-4b61-a5a0-3d6731c0f3b3\"},\"query\":\"query GetHomeGizmos($homeId: String!) { me { home(id: $homeId) { gizmos { __typename ... on Gizmo { __typename ...GizmoItem } ... on GizmoGroup { id title gizmos { __typename ...GizmoItem } } } } } }  fragment QueryArgument on QueryArguments { key value }  fragment GizmoItem on Gizmo { id title type isHidden isAlwaysVisible isFixed context { __typename ...QueryArgument } }\"}"


HOME_ID = "76dabd8e-017f-4b61-a5a0-3d6731c0f3b3"
DEVICE_ID = "f934fed9-68ec-4d01-ae4a-cabd6a825ff5"


def maximize_gr():
    auth_headers = None
    variables = {
        "homeId": HOME_ID,
        "deviceId": DEVICE_ID,
        "settings": [{"key": "hourlyConsumptionLimit", "value": "1.0"}],
    }
    next_q = datetime.datetime.now()
    next_q = next_q + ((next_q.min - next_q) % datetime.timedelta(minutes=15))
    # Begin with current quarter - set rules one minute in advance
    next_q -= datetime.timedelta(minutes=16)
    while True:
        time.sleep(59)  # dont miss a minute
        now_time = datetime.datetime.now()

        if now_time >= next_q:
            if auth_headers is None or next_q.minute == 44:
                auth_response = requests.post(  # TODO use refreshtoken instead
                    "https://app.tibber.com/login.credentials",
                    data=REQUEST_BODY,
                    headers=LOGIN_HEADERS,
                    timeout=10.0,
                )

                if auth_response.status_code != 200:
                    print("Login failed - check credentials..")
                auth_headers = {  # Valid for 3 hours - refresh every hour
                    "Content-Type": "application/json",
                    "authorization": f"{auth_response.json()['token']}",
                }
            budget = PWR_BUDGET
            if next_q.minute == 59:  # boost the first quarter to maximize GR
                budget *= 1 + FIRST_QS_BOOST * 2
            elif next_q.minute == 14:  # boost the second quarter to maximize GR
                budget *= 1 + FIRST_QS_BOOST
            variables["settings"][0]["value"] = f"{budget:.2f}"
            next_q += datetime.timedelta(minutes=15)
            print(f"setting {variables}")
            response = requests.post(
                "https://app.tibber.com/v4/gql",
                json={"query": QUERY, "variables": variables},
                headers=auth_headers,
                timeout=10.0,
            )
            if response.status_code == 200:
                print("Updated the hourlyConsumptionLimit ok")


if __name__ == "__main__":
    while True:
        try:
            maximize_gr()
        except Exception as e:
            print(f"failed: {e}")
        time.sleep(60)
