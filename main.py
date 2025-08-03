"""
Main application for the Rent‑to‑Serviced‑Accommodation (SA) profitability API.

This Flask application exposes a `/calculate` endpoint which accepts a JSON
payload describing a rental property (address, price, property type and number
of bedrooms).  The API derives an estimated nightly rate using a simple
heuristic based on the property type, bedroom count and location.  Monthly
profit projections at three different occupancy levels are then computed and
returned alongside a pre‑formatted WhatsApp message summarising the results.

The nightly rate estimation logic has been deliberately separated into its own
function (`fetch_average_nightly_rate`) to facilitate future integration with
external data sources (e.g. Property Market Intel).  When a suitable
scraper or API wrapper becomes available the body of this function can be
replaced with live data retrieval logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional

import requests  # For optional webhook notification and future integrations
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create the Flask application instance
app = Flask(__name__)

# -----------------------------------------------------------------------------
# Nightly rate estimation
#
# Base nightly rates by property type and bedroom count.  These values are
# rough estimates and can be tuned over time as more data becomes available.
BASE_RATE_MAP: Dict[str, Dict[int, int]] = {
    "house": {1: 60, 2: 85, 3: 110, 4: 140, 5: 170},
    "apartment": {1: 55, 2: 75, 3: 100, 4: 125, 5: 150},
    "bungalow": {1: 50, 2: 70, 3: 95, 4: 120, 5: 145},
    "studio": {1: 45, 2: 65, 3: 85, 4: 105, 5: 125},
}

# Multiplier adjustments based on major UK cities.  If the provided address
# contains one of these keys (case insensitive) the base nightly rate will be
# scaled by the associated multiplier.  A default multiplier of 1.0 is used
# when no city is matched.
CITY_MULTIPLIERS: Dict[str, float] = {
    "LONDON": 1.6,
    "MANCHESTER": 1.3,
    "LIVERPOOL": 1.2,
    "BIRMINGHAM": 1.2,
    "LEEDS": 1.1,
    "GLASGOW": 1.1,
    "EDINBURGH": 1.3,
    "BRISTOL": 1.2,
    "CARDIFF": 1.1,
    "SHEFFIELD": 1.0,
}
DEFAULT_MULTIPLIER: float = 1.0

# Legacy static mapping of postcode prefix and bedroom count to nightly rate.
# This mapping remains for backward compatibility when the heuristic estimator
# cannot determine a rate.  Entries can be added or adjusted manually.
LEGACY_NIGHTLY_RATE_MAP: Dict[str, int] = {
    "L4-3": 130,
    "L4-4": 160,
    "L5-3": 130,
    "L5-4": 160,
    "L6-3": 120,
    "L6-4": 150,
}


def fetch_average_nightly_rate(address: str, property_type: str, bedrooms: int) -> Optional[int]:
    """
    Estimate the average nightly rate for a given property.

    This function first attempts to normalise the input parameters and look
    up a base rate from the in‑memory ``BASE_RATE_MAP``.  A location factor is
    then applied if the address contains the name of a major city defined in
    ``CITY_MULTIPLIERS``.  If the property type or bedroom count are not
    recognised the function returns ``None`` to signal that no estimate could
    be computed.

    A hook has been left in place for future integration with external data
    sources (e.g. Property Market Intel, Airbnb or Booking.com).  Should a
    live scraping or API integration be implemented, its logic can be added
    within this function before falling back to the heuristic provided.

    :param address: Full address string including city/town and postcode.
    :param property_type: String describing the type of property (e.g.
        ``"house"``, ``"apartment"``).  Case insensitive.
    :param bedrooms: Number of bedrooms.
    :return: Estimated nightly rate as an integer, or ``None`` if unknown.
    """
    logger.debug(
        "Estimating nightly rate for address=%s, property_type=%s, bedrooms=%d",
        address,
        property_type,
        bedrooms,
    )

    # Normalise property type and bedroom count
    p_type = (property_type or "").strip().lower()
    # Look up base rates for the given property type
    base_for_type = BASE_RATE_MAP.get(p_type)
    if not base_for_type:
        logger.warning("Unrecognised property type '%s' for nightly rate estimate", p_type)
        return None
    # Determine base rate using the nearest lower bedroom key when exact match
    # is unavailable
    if bedrooms in base_for_type:
        base_rate = base_for_type[bedrooms]
    else:
        sorted_keys = sorted(base_for_type.keys())
        lower_keys = [k for k in sorted_keys if k <= bedrooms]
        if lower_keys:
            base_rate = base_for_type[max(lower_keys)]
        else:
            base_rate = base_for_type[sorted_keys[0]]
    logger.debug("Base rate from BASE_RATE_MAP: %s", base_rate)

    # Determine location multiplier
    multiplier = DEFAULT_MULTIPLIER
    upper_address = address.upper()
    for city, factor in CITY_MULTIPLIERS.items():
        if city in upper_address:
            multiplier = factor
            logger.debug(
                "Applied city multiplier %.2f for matched city '%s' in address",
                multiplier,
                city,
            )
            break
    estimated_rate = int(round(base_rate * multiplier))
    logger.debug("Estimated nightly rate after multiplier: %s", estimated_rate)
    return estimated_rate


def parse_rent(price_str: str) -> float:
    """
    Extract the numeric monthly rent from a human‑readable price string.

    The input is expected to contain a currency symbol and optional commas
    and textual suffixes (e.g. "£1,200 pcm").  The function returns the numeric
    value as a float.

    :param price_str: Price string such as "£1,200 pcm".
    :return: Numeric rent value.
    :raises ValueError: If no digits can be extracted from the string.
    """
    logger.debug("Parsing rent from price string: %s", price_str)
    # Remove any currency symbols and non‑digit characters except decimal point
    cleaned = re.sub(r"[^0-9.]+", "", price_str)
    if not cleaned:
        raise ValueError(f"Could not parse rent from price string '{price_str}'")
    try:
        rent_value = float(cleaned)
        logger.debug("Parsed rent value: %s", rent_value)
        return rent_value
    except ValueError as exc:
        raise ValueError(f"Invalid numeric value in price string '{price_str}'") from exc


def extract_prefix(address: str) -> Optional[str]:
    """
    Extract the postcode prefix from an address string.

    The prefix is defined as the initial letters and digits of the postcode
    (e.g. "L4" or "L5").  This function uses a regular expression to find
    a pattern consisting of one or two letters followed by one or two digits.

    :param address: Full address string containing a postcode.
    :return: The postcode prefix, or None if not found.
    """
    logger.debug("Extracting postcode prefix from address: %s", address)
    # UK postcode area format: letters followed by digits
    match = re.search(r"([A-Za-z]{1,2}\d{1,2})", address)
    prefix = match.group(1).upper() if match else None
    logger.debug("Extracted prefix: %s", prefix)
    return prefix


def calculate_profits(nightly_rate: int, rent: float) -> Dict[str, float]:
    """
    Compute the monthly profits at predefined occupancy levels.

    The occupancy levels considered are 50 %, 70 % and 100 %.  Profit per
    month is calculated as:

        profit = nightly_rate * 30 * occupancy - rent - 600

    Where 600 is an assumed fixed cost for bills.  Profits are rounded to
    two decimal places for clarity.

    :param nightly_rate: The nightly rate in pounds.
    :param rent: The monthly rent in pounds.
    :return: Dictionary mapping occupancy percentage strings to profit values.
    """
    profits: Dict[str, float] = {}
    for occupancy in [0.5, 0.7, 1.0]:
        profit = nightly_rate * 30 * occupancy - rent - 600
        profits[str(int(occupancy * 100))] = round(profit, 2)
        logger.debug(
            "Calculated profit for occupancy %s%%: %.2f", int(occupancy * 100), profit
        )
    return profits


def format_whatsapp_message(address: str, bedrooms: int, rent: float, profits: Dict[str, float]) -> str:
    """
    Build a WhatsApp‑friendly message summarising the profitability figures.

    :param address: Property address.
    :param bedrooms: Number of bedrooms.
    :param rent: Monthly rent amount.
    :param profits: Dictionary of profits keyed by occupancy percentage.
    :return: A formatted multi‑line string suitable for WhatsApp.
    """
    return (
        f"{address}, {bedrooms} Bed\n\n"
        f"Rent + Bills = £{rent + 600:.2f}\n\n"
        "| Conservative Figures |\n"
        f"£{profits['50']:.2f} PPM @ 50%\n"
        f"£{profits['70']:.2f} PPM @ 70%\n"
        f"£{profits['100']:.2f} PPM @ 100%"
    )


@app.route("/", methods=["GET"])
def root() -> Dict[str, str]:
    """
    A simple health check endpoint to confirm the server is running.

    :return: JSON message with a greeting.
    """
    return {"message": "Rent‑to‑SA profitability API is running."}


@app.route("/calculate", methods=["POST"])
def calculate_endpoint() -> Any:
    """
    Calculate the nightly rate and monthly profits for a rent‑to‑SA deal.

    Expects a JSON payload with keys:
      - address: The full address including postcode (string).
      - price: The monthly rent string (e.g. "£1,200 pcm").
      - bedrooms: Number of bedrooms (integer).
      - property_type (optional): The type of property (e.g. "house" or "apartment").

    Returns a JSON response containing the nightly rate, profit projections and
    a formatted WhatsApp message.  When the estimated nightly rate cannot be
    determined an informative error is returned instead of a numeric result.

    :return: Flask response with JSON data.
    """
    data: Dict[str, Any] = request.get_json(silent=True, force=True) or {}
    logger.info("Received request data: %s", data)

    address = data.get("address")
    price_str = data.get("price")
    bedrooms = data.get("bedrooms")
    property_type = data.get("property_type", "house")  # default to house if omitted

    # Validate input
    if not address or not isinstance(address, str):
        return jsonify({"error": "Invalid or missing 'address'."}), 400
    if not price_str or not isinstance(price_str, str):
        return jsonify({"error": "Invalid or missing 'price'."}), 400
    if bedrooms is None:
        return jsonify({"error": "Missing 'bedrooms'."}), 400
    try:
        bedrooms_int = int(bedrooms)
    except (ValueError, TypeError):
        return jsonify({"error": "'bedrooms' must be an integer."}), 400

    # Parse rent
    try:
        rent = parse_rent(price_str)
    except ValueError as err:
        return jsonify({"error": str(err)}), 400

    # Attempt to estimate nightly rate using heuristic
    nightly_rate = fetch_average_nightly_rate(address, property_type, bedrooms_int)

    # Fallback to legacy mapping if heuristic fails
    if nightly_rate is None:
        prefix = extract_prefix(address)
        if prefix:
            lookup_key = f"{prefix}-{bedrooms_int}"
            nightly_rate = LEGACY_NIGHTLY_RATE_MAP.get(lookup_key)

    if nightly_rate is None:
        return (
            jsonify(
                {
                    "error": (
                        "Unable to determine nightly rate for the provided inputs."
                        " Please ensure the property type is one of: "
                        f"{', '.join(sorted(BASE_RATE_MAP.keys()))}."
                    )
                }
            ),
            404,
        )

    # Calculate profits
    profits = calculate_profits(nightly_rate, rent)
    message = format_whatsapp_message(address, bedrooms_int, rent, profits)

    response_data = {
        "nightly_rate": nightly_rate,
        "profits": profits,
        "message": message,
    }
    logger.info("Calculated response: %s", response_data)

    # Optionally notify an external webhook (e.g. Make.com) with the results
    webhook_url = os.environ.get("MAKE_WEBHOOK_URL")
    if webhook_url:
        try:
            headers = {"Content-Type": "application/json"}
            requests.post(webhook_url, data=json.dumps(response_data), headers=headers, timeout=5)
            logger.debug("Successfully posted results to Make.com webhook")
        except Exception as exc:  # broad catch to avoid failing the request
            logger.warning("Failed to post results to webhook: %s", exc)

    return jsonify(response_data)


if __name__ == "__main__":
    # Allow running the app directly for local development
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
