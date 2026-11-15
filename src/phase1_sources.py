"""Phase 1 reference sources: acquire, verify and normalise authentic observations."""

import csv
import hashlib
import json
import re
import shutil
import ssl
import socket
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TRIP_MONTHS = [f"2024-{month:02d}" for month in range(1, 13)] + ["2025-01"]
SOURCE_MONTHS = ["2023-12", *TRIP_MONTHS, "2025-02"]
STATIONS = {"central_park": "72505394728", "laguardia": "72503014732", "jfk": "74486094789"}
TLC_URL = "https://d37ci6vzurychx.cloudfront.net"
OPM_URL = "https://www.opm.gov/policy-data-oversight/pay-leave/federal-holidays/"
LCD_DOCUMENTATION = "https://www.ncei.noaa.gov/pub/data/cdo/documentation/LCD_documentation.pdf"
UTC = timezone.utc
EST = timezone(timedelta(hours=-5))
NEIGHBOUR_TOLERANCE_METRES = 1.0
MIN_SHARED_BOUNDARY_METRES = 10.0


def hash_file(path):
    """Hash a file in bounded reads for provenance and checkpoint validation."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json_new(path, value):
    """Write a new JSON artifact; never overwrite an existing checkpoint."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as destination:
        json.dump(value, destination, indent=2, default=str)


def expected_sources(root):
    """Return the exact required file list, including explicitly versioned NOAA sources."""
    records = []
    for month in SOURCE_MONTHS:
        name = f"fhvhv_tripdata_{month}.parquet"
        records.append({"source_id": f"trips_{month}", "kind": "trips", "source_month": month,
                        "product": "NYC TLC HVFHV", "role": "analysis" if month in TRIP_MONTHS else "boundary_guard",
                        "time_convention": "Offset-free Eastern wall time; America/New_York civil interpretation, with unresolved DST times kept explicitly",
                        "time_evidence_url": "https://www.nyc.gov/assets/tlc/downloads/pdf/csv_instruction_manual.pdf",
                        "units": {"trip_miles": "miles", "trip_time": "seconds", "monetary_fields": "USD"},
                        "url": f"{TLC_URL}/trip-data/{name}",
                        "path": str(Path(root) / "data/raw/trips" / name)})
    for name in ["taxi_zone_lookup.csv", "taxi_zones.zip"]:
        records.append({"source_id": name, "kind": "zones", "product": "TLC zones",
                        "url": f"{TLC_URL}/misc/{name}",
                        "path": str(Path(root) / "data/raw/zones" / name)})
    for year in [2024, 2025]:
        for label, station in STATIONS.items():
            records.append({"source_id": f"lcdv1_{label}_{year}", "kind": "weather",
                            "product": "NOAA LCDv1 historical CSV", "station_id": station,
                            "time_convention": "Must be verified directly from CSV DATE versus raw METAR UTC before use",
                            "units": "LCDv1 original Fahrenheit/inches/mph/miles; not LCDv2 SI values",
                            "year": year, "url": f"https://www.ncei.noaa.gov/data/local-climatological-data/access/{year}/{station}.csv",
                            "path": str(Path(root) / f"data/raw/weather/lcdv1/{year}/{station}.csv")})
    records.append({"source_id": "opm_holidays", "kind": "calendar", "product": "OPM federal holidays",
                    "url": OPM_URL, "path": str(Path(root) / "data/raw/calendar/opm_federal_holidays.html")})
    return records


def verify_source_file(record):
    """Check structure and record size/schema; do not equate readability with complete coverage."""
    path = Path(record["path"])
    if record["kind"] == "trips":
        import pyarrow.parquet as parquet
        source = parquet.ParquetFile(path)
        if source.metadata.num_rows == 0:
            raise ValueError(f"Empty trip input: {path}")
        details = {"rows": source.metadata.num_rows, "schema": str(source.schema_arrow)}
    elif record["url"].endswith(".zip"):
        import zipfile
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError(f"Failed archive CRC: {path}")
        details = {"format": "ZIP; CRC checked"}
    elif record["kind"] == "weather" or record["url"].endswith(".csv"):
        with path.open(newline="", encoding="utf-8-sig") as stream:
            names = next(csv.reader(stream))
        required = {"STATION", "DATE"} if record["kind"] == "weather" else {"LocationID", "Borough", "Zone"}
        has_raw_report = record["kind"] != "weather" or bool({"REM", "RAW_REPORT"} & set(names))
        if not required <= set(names) or not has_raw_report:
            raise ValueError(f"Wrong CSV schema for {record['source_id']}: missing {sorted(required - set(names))}; raw REM/RAW_REPORT present={has_raw_report}")
        details = {"columns": names}
    else:
        text = path.read_text()
        if "2024 Holiday Schedule" not in text and "BEGIN:VCALENDAR" not in text:
            raise ValueError("OPM snapshot does not contain the requested holiday evidence")
        details = {"format": "official OPM snapshot"}
    return {**record, **details, "bytes": path.stat().st_size,
            "sha256": hash_file(path), "checked_at_utc": datetime.now(UTC).isoformat(),
            "status": "checked", "check_scope": "file identity/readability; temporal coverage and integration verified separately"}


def download_missing_files(records, downloads=None):
    """Reuse checked files, copy valid browser downloads, or resume one HTTP acquisition."""
    results = []
    network_blocker = None
    system_ca = Path("/etc/ssl/certs/ca-certificates.crt")
    context = ssl.create_default_context(cafile=str(system_ca) if system_ca.is_file() else None)
    for record in records:
        path = Path(record["path"])
        try:
            if path.exists():
                results.append(verify_source_file(record))
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            browser = Path(downloads) / path.name if downloads else None
            if browser is not None and browser.is_file():
                verify_source_file({**record, "path": str(browser)})
                # Exclusive destination creation preserves any concurrently created file.
                with browser.open("rb") as source, path.open("xb") as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                results.append(verify_source_file(record))
                print("Reused browser file:", record["source_id"], flush=True)
                continue
            if network_blocker:
                raise OSError(f"Network attempt skipped after shared TLS/DNS blocker: {network_blocker}")
            request = urllib.request.Request(record["url"], method="HEAD")
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                expected_bytes = int(response.headers.get("Content-Length", "0"))
            if expected_bytes and shutil.disk_usage(path.parent).free < expected_bytes * 2:
                raise OSError("Insufficient free space for download plus processing headroom")
            partial = path.with_suffix(path.suffix + ".part")
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            with urllib.request.urlopen(urllib.request.Request(record["url"], headers=headers), timeout=60, context=context) as response:
                append = offset > 0 and response.status == 206
                if append and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("Unexpected resume range")
                with partial.open("ab" if append else "wb") as destination:
                    shutil.copyfileobj(response, destination, 1024 * 1024)
            if expected_bytes and partial.stat().st_size != expected_bytes:
                raise ValueError("Incomplete HTTP download; partial file retained")
            verify_source_file({**record, "path": str(partial)})
            # A hard link fails if a target appears; it never replaces raw data.
            import os
            os.link(partial, path)
            partial.unlink()
            checked = verify_source_file(record)
            results.append({**checked, "retrieved_at_utc": datetime.now(UTC).isoformat(),
                            "remote_content_length": expected_bytes or None})
            print("Acquired:", record["source_id"], flush=True)
        except (OSError, ValueError) as error:
            if isinstance(error, ssl.SSLError) or isinstance(getattr(error, "reason", None), (ssl.SSLError, socket.gaierror)):
                network_blocker = str(error)
            results.append({**record, "status": "blocked", "issue": str(error)})
            print("Blocked:", record["source_id"], str(error), flush=True)
    return results


def inventory_sources(records):
    """Inspect existing source files without a network attempt."""
    result = []
    for record in records:
        if not Path(record["path"]).is_file():
            result.append({**record, "status": "missing"})
            continue
        try:
            result.append(verify_source_file(record))
        except (OSError, ValueError) as error:
            result.append({**record, "status": "invalid", "issue": str(error)})
    return result


def build_zone_metadata(lookup_path, shape_path, expected_mapped_zones=263):
    """Return authentic zone attributes and transparent geometry derivatives in explicit units."""
    import shapefile
    from pyproj import CRS, Transformer
    from shapely.geometry import shape

    with Path(lookup_path).open(newline="") as stream:
        lookup = {int(row["LocationID"]): row for row in csv.DictReader(stream)}
    crs = CRS.from_wkt(Path(shape_path).with_suffix(".prj").read_text())
    if not crs.is_projected:
        raise ValueError("Expected the verified projected boundary CRS")
    metres_per_unit = crs.axis_info[0].unit_conversion_factor
    transform = Transformer.from_crs(crs, 4326, always_xy=True)
    zones, geometries = [], {}
    with shapefile.Reader(str(shape_path)) as reader:
        for item in reader.iterShapeRecords():
            zone_id = int(item.record.as_dict()["LocationID"])
            geometry = shape(item.shape.__geo_interface__)
            if zone_id in geometries or zone_id not in lookup or not geometry.is_valid:
                raise ValueError(f"Invalid/duplicate/unrecognised geometry: {zone_id}; no repair attempted")
            geometries[zone_id] = geometry
            x, y = geometry.centroid.x, geometry.centroid.y
            longitude, latitude = transform.transform(x, y)
            row = lookup[zone_id]
            zones.append({"zone_id": zone_id, "zone_name": row["Zone"], "borough": row["Borough"],
                          "service_zone": row["service_zone"], "is_airport": zone_id in (1, 132, 138),
                          "area_square_metres": geometry.area * metres_per_unit ** 2,
                          "centroid_x_metres": x * metres_per_unit,
                          "centroid_y_metres": y * metres_per_unit,
                          "centroid_longitude": longitude, "centroid_latitude": latitude,
                          "source_crs": crs.to_string(), "geometry_wkb": bytes(geometry.wkb)})
    if len(zones) != expected_mapped_zones:
        raise ValueError("Expected the verified 263 mapped zones")
    return zones, find_zone_neighbours(geometries, metres_per_unit)


def find_zone_neighbours(geometries, metres_per_unit):
    """Take zone shapes by id and return directed neighbour edges, in both directions.

    The TLC shapes were drawn with tiny gaps and overlaps along shared borders, so an exact
    "touches" test misses many real neighbours. Two zones are neighbours when they are
    within NEIGHBOUR_TOLERANCE_METRES of each other. The shared border length is how much of
    the second zone's outline lies within that tolerance of the first zone. A shared length
    of MIN_SHARED_BOUNDARY_METRES or more is a shared border; anything shorter is only a
    corner contact. The length is approximate: it can be up to about twice the tolerance
    too long per border, so use it to rank borders, not as a surveyed distance.
    """
    tolerance_units = NEIGHBOUR_TOLERANCE_METRES / metres_per_unit
    edges = []
    # 34,453 comparisons for 263 zones is small; a plain loop is easy to explain.
    for zone_id in sorted(geometries):
        for neighbour_id in sorted(geometries):
            if neighbour_id <= zone_id:
                continue
            first, second = geometries[zone_id], geometries[neighbour_id]
            if first.distance(second) > tolerance_units:
                continue
            nearby_outline = second.boundary.intersection(first.buffer(tolerance_units))
            boundary_length = nearby_outline.length * metres_per_unit
            has_shared_border = boundary_length >= MIN_SHARED_BOUNDARY_METRES
            for start, end in [(zone_id, neighbour_id), (neighbour_id, zone_id)]:
                edges.append({"zone_id": start, "neighbour_zone_id": end,
                              "shares_boundary": has_shared_border,
                              "touches_at_point": not has_shared_border,
                              "shared_boundary_metres": boundary_length})
    return edges


def parse_weather_number(value, conversion=1.0, offset=0.0):
    """Return a measured number and quality label; never turn missing/suspect/trace into zero."""
    text = (value or "").strip()
    if not text:
        return None, "missing"
    if text == "T":
        return None, "trace"
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?", text):
        return None, "qualified_or_invalid"
    return (float(text) + offset) * conversion, "numeric"


def prepare_weather(path, expected_station):
    """Verify the CSV clock against raw UTC station reports, then normalise LCDv1 observations."""
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        records = list(csv.DictReader(stream))
    raw_report_column = "REM" if records and "REM" in records[0] else "RAW_REPORT"
    evidence = Counter()
    verified_rows, clock_exceptions = set(), []
    for record_number, row in enumerate(records, start=1):
        if row.get("REPORT_TYPE", "").strip() != "FM-15":
            continue
        match = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", row.get(raw_report_column, ""))
        if not match:
            clock_exceptions.append({"csv_record_number": record_number, "date": row["DATE"], "issue": "no_parseable_raw_utc_report"})
            continue
        wall = datetime.fromisoformat(row["DATE"])
        if wall.tzinfo is not None:
            raise ValueError("Unexpected offset-aware LCDv1 DATE; inspect the product")
        actual_utc = wall.replace(tzinfo=EST).astimezone(UTC)
        day, hour, minute = map(int, match.groups())
        if (actual_utc.day, actual_utc.hour, actual_utc.minute) != (day, hour, minute):
            clock_exceptions.append({"csv_record_number": record_number, "date": row["DATE"],
                                     "raw_utc_day_hour_minute": match.group(0), "issue": "csv_vs_raw_utc_clock_conflict"})
            continue
        evidence[wall.month] += 1
        verified_rows.add(record_number)
    if not evidence:
        if clock_exceptions:
            raise ValueError("CSV clock conflicts with or lacks raw METAR UTC evidence; no conversion performed")
        raise ValueError("CSV time convention unverified: no matching raw UTC report evidence")
    observations, counts = [], Counter()
    conversions = {
        "temperature_c": ("HourlyDryBulbTemperature", 5 / 9, -32),
        "precipitation_previous_hour_mm": ("HourlyPrecipitation", 25.4, 0),
        "wind_metres_per_second": ("HourlyWindSpeed", 0.44704, 0),
        "visibility_metres": ("HourlyVisibility", 1609.344, 0),
        "relative_humidity_percent": ("HourlyRelativeHumidity", 1, 0),
        "dew_point_c": ("HourlyDewPointTemperature", 5 / 9, -32),
    }
    for record_number, row in enumerate(records, start=1):
        counts["source_rows"] += 1
        if row.get("STATION", "").strip() != expected_station:
            raise ValueError("Weather station ID does not match its manifest")
        if row.get("REPORT_TYPE", "").strip() != "FM-15":
            counts["non_routine_rows_retained_in_raw_only"] += 1
            continue
        if record_number not in verified_rows:
            counts["routine_clock_unresolved_rows_retained_in_raw_only"] += 1
            continue
        wall = datetime.fromisoformat(row["DATE"])
        utc = wall.replace(tzinfo=EST).astimezone(UTC)
        observed = {"station_id": expected_station, "observed_at_utc": utc,
                    "hour_start_utc": utc.replace(minute=0, second=0, microsecond=0),
                    "report_type": "FM-15", "source_code": row.get("SOURCE", ""),
                    "source_time_convention": "LCDv1 CSV fixed EST; verified against raw METAR UTC",
                    "product_version": "LCDv1", "has_verified_publication_time": False}
        for name, (raw_name, conversion, offset) in conversions.items():
            value, quality = parse_weather_number(row.get(raw_name), conversion, offset)
            if value is not None and ((name in ["precipitation_previous_hour_mm", "wind_metres_per_second", "visibility_metres"] and value < 0)
                                      or (name == "relative_humidity_percent" and not 0 <= value <= 100)):
                value, quality = None, "out_of_range"
            observed[name], observed[name + "_quality"] = value, quality
            observed[name + "_raw"] = row.get(raw_name, "")
            counts[f"{name}:{quality}"] += 1
        codes = row.get("HourlyPresentWeatherType", "")
        observed["present_weather_codes"] = codes
        observed["has_observed_rain"] = bool(re.search(r"\b(?:RA|DZ)\b", codes)) if codes else None
        observed["has_observed_snow"] = bool(re.search(r"\b(?:SN|SG|IC|PL)\b", codes)) if codes else None
        observed["source_row_sha256"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        observations.append(observed)
    return observations, {"counts": dict(counts), "utc_clock_matches_by_month": dict(evidence),
                          "raw_utc_report_column": raw_report_column,
                          "clock_exceptions": clock_exceptions,
                          "source_sha256": hash_file(path), "documentation": LCD_DOCUMENTATION,
                          "units": "F/inch/mph/miles in source, explicitly converted to SI",
                          "publication_latency": "unknown; observations are retrospective descriptive data"}


def prepare_calendar(snapshot_path, start=date(2024, 1, 1), end=date(2025, 2, 1)):
    """Transcribe only dates corroborated by the saved official OPM HTML snapshot."""
    import html
    text = html.unescape(re.sub(r"<[^>]+>", " ", Path(snapshot_path).read_text())).replace("’", "'")
    text = re.sub(r"\s+", " ", text)
    # This is a documented transcription, not a holiday-generating algorithm.
    dates = {
        "2024-01-01": "New Year's Day", "2024-01-15": "Birthday of Martin Luther King, Jr.",
        "2024-02-19": "Washington's Birthday", "2024-05-27": "Memorial Day",
        "2024-06-19": "Juneteenth National Independence Day", "2024-07-04": "Independence Day",
        "2024-09-02": "Labor Day", "2024-10-14": "Columbus Day",
        "2024-11-11": "Veterans Day", "2024-11-28": "Thanksgiving Day",
        "2024-12-25": "Christmas Day", "2025-01-01": "New Year's Day",
        "2025-01-20": "Birthday of Martin Luther King, Jr.",
    }
    for year in range(start.year, (end - timedelta(days=1)).year + 1):
        section = re.search(rf"{year} Holiday Schedule(.*?)(?=20\d{{2}} Holiday Schedule|$)", text)
        if section is None:
            raise ValueError(f"OPM snapshot has no {year} schedule; holiday coverage unverified")
        for date_text, name in dates.items():
            day = date.fromisoformat(date_text)
            if day.year != year:
                continue
            label = day.strftime("%B %d")
            if not re.search(re.escape(label) + r".{0,80}" + re.escape(name), section.group(1)):
                raise ValueError(f"Holiday transcription not corroborated by OPM snapshot: {date_text}")
    result = []
    current = start
    while current < end:
        holiday_name = dates.get(current.isoformat())
        result.append({"event_date_nyc": current, "month_nyc": current.month,
                       "weekday_nyc": current.isoweekday(), "is_weekend": current.isoweekday() >= 6,
                       "is_federal_holiday": holiday_name is not None, "holiday_name": holiday_name,
                       "holiday_observed_date": current if holiday_name else None,
                       "holiday_statutory_date": current if holiday_name else None,
                       "holiday_source": OPM_URL})
        current += timedelta(days=1)
    return result
