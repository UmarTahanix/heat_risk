#!/usr/bin/env python3
"""Heat Risk CLI — current conditions and 3-day forecast via Open-Meteo."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HEAT_INDEX_WARNING_C = 38.0
SUPPORTED_OUTPUT_FORMATS = ("txt", "csv", "json")


def heat_index_celsius(temp_c: float, relative_humidity: float) -> float:
    """Compute NWS heat index (°C) from air temperature (°C) and RH (%)."""
    temp_f = temp_c * 9.0 / 5.0 + 32.0
    rh = relative_humidity

    # Steadman / NWS simple approximation
    heat_index_f = 0.5 * (
        temp_f + 61.0 + ((temp_f - 68.0) * 1.2) + (rh * 0.094)
    )
    heat_index_f = (heat_index_f + temp_f) / 2.0

    if heat_index_f >= 80.0:
        # Rothfusz regression
        heat_index_f = (
            -42.379
            + 2.04901523 * temp_f
            + 10.14333127 * rh
            - 0.22475541 * temp_f * rh
            - 0.00683783 * temp_f**2
            - 0.05481717 * rh**2
            + 0.00122874 * temp_f**2 * rh
            + 0.00085282 * temp_f * rh**2
            - 0.00000199 * temp_f**2 * rh**2
        )

        if rh < 13.0 and 80.0 <= temp_f <= 112.0:
            adjustment = ((13.0 - rh) / 4.0) * math.sqrt(
                (17.0 - abs(temp_f - 95.0)) / 17.0
            )
            heat_index_f -= adjustment
        elif rh > 85.0 and 80.0 <= temp_f <= 87.0:
            adjustment = ((rh - 85.0) / 10.0) * ((87.0 - temp_f) / 5.0)
            heat_index_f += adjustment

    return (heat_index_f - 32.0) * 5.0 / 9.0


def http_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
    query = urllib.parse.urlencode(params, doseq=True)
    full_url = f"{url}?{query}"
    request = urllib.request.Request(
        full_url,
        headers={"User-Agent": "heat-risk-cli/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} from Open-Meteo: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error contacting Open-Meteo: {exc.reason}") from exc


def location_score(candidate: dict[str, Any], query: str) -> tuple:
    """Rank geocoding hits: prefer exact names, cities/admin areas, population."""
    name = str(candidate.get("name", ""))
    feature = str(candidate.get("feature_code", ""))
    population = int(candidate.get("population") or 0)
    query_norm = query.casefold()
    name_norm = name.casefold()

    exact_match = 0 if name_norm == query_norm else 1
    starts_with = 0 if name_norm.startswith(query_norm) else 1

    if feature.startswith("PPL") or feature.startswith("ADM"):
        place_kind = 0
    elif feature in {"PCL", "PCLI"}:
        place_kind = 1
    else:
        place_kind = 2

    # Lower tuple sorts first: exact city/admin with largest population wins.
    return (exact_match, starts_with, place_kind, -population, name_norm)


def resolve_city(city_name: str) -> dict[str, Any]:
    data = http_get_json(
        GEOCODING_URL,
        {"name": city_name, "count": 10, "language": "en", "format": "json"},
    )
    results = data.get("results") or []
    if not results:
        raise RuntimeError(f"No location found for '{city_name}'.")
    return min(results, key=lambda item: location_score(item, city_name))


def fetch_forecast(latitude: float, longitude: float) -> dict[str, Any]:
    return http_get_json(
        FORECAST_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": "auto",
            "forecast_days": 3,
            "temperature_unit": "celsius",
            "current": "temperature_2m,relative_humidity_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
        },
    )


def format_place(location: dict[str, Any]) -> str:
    parts = [location["name"]]
    admin1 = location.get("admin1")
    country = location.get("country")
    if admin1 and admin1 != location["name"]:
        parts.append(admin1)
    if country:
        parts.append(country)
    return ", ".join(parts)


def format_date(iso_date: str) -> str:
    return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%a %d %b")


def build_report_data(
    location: dict[str, Any],
    forecast: dict[str, Any],
) -> dict[str, Any]:
    current = forecast["current"]
    daily = forecast["daily"]
    temp_c = float(current["temperature_2m"])
    humidity = float(current["relative_humidity_2m"])
    heat_index = heat_index_celsius(temp_c, humidity)
    days = [
        {
            "date_iso": day,
            "date_label": format_date(day),
            "high_c": float(high),
            "low_c": float(low),
        }
        for day, high, low in zip(
            daily["time"],
            daily["temperature_2m_max"],
            daily["temperature_2m_min"],
            strict=True,
        )
    ]
    return {
        "place": format_place(location),
        "observed_at": current.get("time", "n/a"),
        "timezone": forecast.get("timezone", "local"),
        "latitude": float(location["latitude"]),
        "longitude": float(location["longitude"]),
        "temperature_c": temp_c,
        "humidity_pct": humidity,
        "heat_index_c": heat_index,
        "heat_warning": heat_index > HEAT_INDEX_WARNING_C,
        "forecast_days": days,
    }


def format_txt_report(report: dict[str, Any]) -> str:
    width = 48
    lines = [
        "=" * width,
        f"  Heat Risk Report — {report['place']}",
        "=" * width,
        f"  Observed:     {report['observed_at']} ({report['timezone']})",
        f"  Coordinates:  {report['latitude']:.4f}, {report['longitude']:.4f}",
        "-" * width,
        "  Current conditions",
        f"    Temperature:   {report['temperature_c']:5.1f} °C",
        f"    Humidity:      {report['humidity_pct']:5.0f} %",
        f"    Heat index:    {report['heat_index_c']:5.1f} °C",
        "-" * width,
        "  3-day forecast",
        f"  {'Date':<12} {'High':>10} {'Low':>10}",
        f"  {'-' * 12} {'-' * 10} {'-' * 10}",
    ]
    for day in report["forecast_days"]:
        lines.append(
            f"  {day['date_label']:<12} "
            f"{day['high_c']:7.1f} °C "
            f"{day['low_c']:7.1f} °C"
        )
    lines.append("=" * width)

    if report["heat_warning"]:
        lines.extend(
            [
                "",
                f"  WARNING: Heat index is {report['heat_index_c']:.1f} °C "
                f"(above {HEAT_INDEX_WARNING_C:.0f} °C). "
                "Limit outdoor exposure and stay hydrated.",
                "",
            ]
        )

    return "\n".join(lines) + "\n"


def format_csv_report(report: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "record_type",
            "city",
            "observed_at",
            "timezone",
            "latitude",
            "longitude",
            "temperature_c",
            "humidity_pct",
            "heat_index_c",
            "forecast_date",
            "high_c",
            "low_c",
            "heat_warning",
        ]
    )
    writer.writerow(
        [
            "current",
            report["place"],
            report["observed_at"],
            report["timezone"],
            f"{report['latitude']:.4f}",
            f"{report['longitude']:.4f}",
            f"{report['temperature_c']:.1f}",
            f"{report['humidity_pct']:.0f}",
            f"{report['heat_index_c']:.1f}",
            "",
            "",
            "",
            str(report["heat_warning"]).lower(),
        ]
    )
    for day in report["forecast_days"]:
        writer.writerow(
            [
                "forecast",
                report["place"],
                report["observed_at"],
                report["timezone"],
                f"{report['latitude']:.4f}",
                f"{report['longitude']:.4f}",
                "",
                "",
                "",
                day["date_iso"],
                f"{day['high_c']:.1f}",
                f"{day['low_c']:.1f}",
                "",
            ]
        )
    return buffer.getvalue()


def format_json_report(report: dict[str, Any]) -> str:
    payload = {
        "city": report["place"],
        "observed_at": report["observed_at"],
        "timezone": report["timezone"],
        "coordinates": {
            "latitude": round(report["latitude"], 4),
            "longitude": round(report["longitude"], 4),
        },
        "current": {
            "temperature_c": round(report["temperature_c"], 1),
            "humidity_pct": round(report["humidity_pct"]),
            "heat_index_c": round(report["heat_index_c"], 1),
            "heat_warning": report["heat_warning"],
        },
        "forecast_3_day": [
            {
                "date": day["date_iso"],
                "high_c": round(day["high_c"], 1),
                "low_c": round(day["low_c"], 1),
            }
            for day in report["forecast_days"]
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def default_report_path(city_name: str, output_format: str) -> Path:
    """Build `<name>_report_<ddMMyyyy>.<format>` from the city query."""
    safe_name = "_".join(city_name.split())
    stamp = datetime.now().strftime("%d%m%Y")
    return Path(f"{safe_name}_report_{stamp}.{output_format}")


def render_report(report: dict[str, Any], output_format: str) -> str:
    if output_format == "csv":
        return format_csv_report(report)
    if output_format == "json":
        return format_json_report(report)
    return format_txt_report(report)


def write_report(content: str, output_path: Path) -> None:
    output_path.write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote report to {output_path}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="heat_risk.py",
        description=(
            "Show current temperature, humidity, heat index, and a 3-day "
            "high/low forecast for a city (Open-Meteo)."
        ),
    )
    parser.add_argument(
        "--city",
        nargs="+",
        required=True,
        help="City name (one or more words), e.g. --city New York",
    )
    parser.add_argument(
        "--output",
        "-o",
        choices=SUPPORTED_OUTPUT_FORMATS,
        default="txt",
        help="Output format: txt (default), csv, or json. "
        "Writes <city>_report_<ddMMyyyy>.<format>.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    city_name = " ".join(args.city).strip()
    if len(city_name) < 2:
        print("Error: city name must be at least 2 characters.", file=sys.stderr)
        return 1

    output_format = args.output
    output_path = default_report_path(city_name, output_format)

    try:
        location = resolve_city(city_name)
        forecast = fetch_forecast(location["latitude"], location["longitude"])
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    report = build_report_data(location, forecast)
    content = render_report(report, output_format)

    try:
        write_report(content, output_path)
    except OSError as exc:
        print(f"Error: could not write output: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
