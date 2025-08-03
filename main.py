"""
Main application for the Rent‑to‑Serviced‑Accommodation (SA) profitability API.

This simple Flask application exposes a single `/calculate` endpoint which accepts a
JSON payload describing a rental property (address, price and number of
bedrooms), derives an appropriate nightly rate based on the postcode prefix and
bedroom count, and computes monthly profit projections at three different
occupancy levels.

The API returns a structured JSON response containing the nightly rate, the
calculated profits per month for 50 %, 70 % and 100 % occupancy, and a
pre‑formatted WhatsApp message summarising the results. To run this
application locally you can execute `python main.py` and open
`http://localhost:5000` in your browser. For production deployments the
application is designed to be run behind a WSGI server such as Gunicorn (see
the accompanying `nixpacks.toml` for Railway configuration).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

import os
from flask import Flask, jsonify, request


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create the Flask application instance
app = Flask(__name__)

# Mapping of postcode prefix and bedroom count to nightly rate
NIGHTLY_RATE_MAP: Dict[str, int] = {
    "L4-3": 130,
    "L4-4": 160,
    "L5-3": 130,
    "L5-4": 160,
    "L6-3": 120,
    "L6-4": 150,
}


def parse_rent(price_str: str) -> float:
    """
    Extract the numeric monthly rent from a human‑readable price string.

    The input is expected to contain a currency symbol and optional commas and
    textual suffixes (e.g. "£1,200 pcm"). The function returns the numeric
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
    (e.g. "L4" or "L5"). This function uses a regular expression to find
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

    The occupancy levels considered are 50 %, 70 % and 100 %. Profit per
    month is calculated as:

        profit = nightly_rate * 30 * occupancy - rent - 600

    Where 600 is an assumed fixed cost for bills. Profits are rounded to
    two decimal places for clarity.

    :param nightly_rate: The nightly rate in pounds.
    :param rent: The monthly rent in pounds.
    :return: Dictionary mapping occupancy percentage strings to profit values.
    """
    profits: Dict[str, float] = {}
    for occupancy in [0.5, 0.7, 1.0]:
        profit = nightly_rate * 30 * occupancy - rent - 600
        # Round to two decimal places
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
        f"📍 {address}, {bedrooms} Bed\n\n"
        f"🏡 Rent + Bills = £{rent + 600:.2f}\n\n"
        "| Conservative Figures |\n"
        f"💎 £{profits['50']:.2f} PPM @ 50%\n"
        f"💎 £{profits['70']:.2f} PPM @ 70%\n"
        f"💎 £{profits['100']:.2f} PPM @ 100%"
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

    Returns a JSON response containing the nightly rate, profit projections and
    a formatted WhatsApp message.

    :return: Flask response with JSON data.
    """
    data: Dict[str, Any] = request.get_json(silent=True, force=True) or {}
    logger.info("Received request data: %s", data)

    address = data.get("address")
    price_str = data.get("price")
    bedrooms = data.get("bedrooms")

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

    # Extract prefix and look up nightly rate
    prefix = extract_prefix(address)
    if not prefix:
        return jsonify({"error": "Could not extract postcode prefix from address."}), 400
    lookup_key = f"{prefix}-{bedrooms_int}"
    nightly_rate = NIGHTLY_RATE_MAP.get(lookup_key)
    if nightly_rate is None:
        return (
            jsonify({
                "error": f"No nightly rate configured for prefix-bedrooms combination '{lookup_key}'."
            }),
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
    return jsonify(response_data)


if __name__ == "__main__":
    # Allow running the app directly for local development
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)