#!/usr/bin/env python

import sys
import requests

login_headers = {"Content-Type": "application/x-www-form-urlencoded"}
request_body = {"email": "user@example.com", "password": "very_secret"}
response = requests.post(
    "https://app.tibber.com/login.credentials", data=request_body, headers=login_headers
)

if response.status_code != 200:
    print("Login failed - check credentials..")
    sys.exit()


query = """
mutation SetPulseSettings($homeId: String!, $deviceId: String!, $settings: [SettingsItemInput!]!) { me { home(id: $homeId) { pulse(id: $deviceId) { setSettings(settings: $settings) { __typename ...PulseItem } } } } } fragment callToActionItem on CallToAction { text url redirectUrlStartsWith link action }  fragment pulseErrorItem on PulseError { title description callToAction { __typename ...callToActionItem } }  fragment setting on Setting { key value valueType valueIsArray isReadOnly inputOptions { type title description unitText rangeOptions { max min step defaultValue displayText displayTextPlural } pickerOptions { values postFix } selectOptions { value title description imgUrl iconName isRecommendedOption } timeOptions { doNotSetATimeText } textFieldOptions { format placeholder imgUrl } } }  fragment settingLayoutFields on SettingLayoutItem { uid type title description valueText imgUrl iconName isUpdated isEnabled settingKey settingKeyForIsHidden callToAction { __typename ...callToActionItem } }  fragment settingsLayout on SettingLayoutItem { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields } } }  fragment PulseItem on Pulse { id name type shortName isAlive error { __typename ...pulseErrorItem } hasAccessToNewPulseScreen hasAccessToPeakControl hasAccessToConsumptionHistory hasPhaseCurrents peakControlStatus estimatedMeasurementsPerMinute settings2 { __typename ...setting } settingsLayout { __typename ...settingsLayout } mainScreen { tabConsumptionText tabPhaseText } energyDealAlert { title message cancelText callToAction { __typename ...callToActionItem } } }
"""

# TODO get the home id: "{\"operationName\":\"GetHomes\",\"variables\":{},\"query\":\"query GetHomes { me { homes { __typename ...HomeItem } } }  fragment CurrentMeterItem on CurrentMeter { id meterNo isUserRead }  fragment AstronomyItem on Astronomy { sunIsUp sunrise sunset }  fragment DateRangeItem on DateRange { from to }  fragment AddressItem on Address { addressText city postalCode country latitude longitude astronomy { __typename ...AstronomyItem } dayNightTimes { night { __typename ...DateRangeItem } sunrise { __typename ...DateRangeItem } sunset { __typename ...DateRangeItem } daytime { __typename ...DateRangeItem } } }  fragment callToActionItem on CallToAction { text url redirectUrlStartsWith link action }  fragment MessageItem on Message { id title description style iconSrc iconName callToAction { __typename ...callToActionItem } dismissButtonText }  fragment setting on Setting { key value valueType valueIsArray isReadOnly inputOptions { type title description unitText rangeOptions { max min step defaultValue displayText displayTextPlural } pickerOptions { values postFix } selectOptions { value title description imgUrl iconName isRecommendedOption } timeOptions { doNotSetATimeText } textFieldOptions { format placeholder imgUrl } } }  fragment settingLayoutFields on SettingLayoutItem { uid type title description valueText imgUrl iconName isUpdated isEnabled settingKey settingKeyForIsHidden callToAction { __typename ...callToActionItem } }  fragment settingsLayout on SettingLayoutItem { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields childItems { __typename ...settingLayoutFields } } }  fragment HomeItem on Home { id timeZone hasSmartMeterCapabilities hasSignedEnergyDeal hasConsumption showMeterNo showMeteringPointId currentMeter { __typename ...CurrentMeterItem } meteringPointIdFormatted address { __typename ...AddressItem } mainPromotion { __typename ...MessageItem } settings { __typename ...setting } settingsLayout { __typename ...settingsLayout } imsOrders { hasOrder } avatar title type }\"}"


# TODO get device id from home id "{\"operationName\":\"GetHomeGizmos\",\"variables\":{\"homeId\":\"76dabd8e-017f-4b61-a5a0-3d6731c0f3b3\"},\"query\":\"query GetHomeGizmos($homeId: String!) { me { home(id: $homeId) { gizmos { __typename ... on Gizmo { __typename ...GizmoItem } ... on GizmoGroup { id title gizmos { __typename ...GizmoItem } } } } } }  fragment QueryArgument on QueryArguments { key value }  fragment GizmoItem on Gizmo { id title type isHidden isAlwaysVisible isFixed context { __typename ...QueryArgument } }\"}"


variables = {
    "homeId": "76dabd8e-017f-4b61-a5a0-3d6731c0f3b3",
    "deviceId": "f934fed9-68ec-4d01-ae4a-cabd6a825ff5",
    "settings": [{"key": "hourlyConsumptionLimit", "value": "5.5"}],
}
auth_headers = {
    "Content-Type": "application/json",
    "authorization": f"{response.json()['token']}",
}

response = requests.post(
    "https://app.tibber.com/v4/gql",
    json={"query": query, "variables": variables},
    headers=auth_headers,
)
if response.status_code == 200:
    print("Updated the hourlyConsumptionLimit ok")
