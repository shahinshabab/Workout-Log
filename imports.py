"""Validate routine and wearable files before sending records to Supabase."""

import csv
import hashlib
import io
import math
import re
import zipfile
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import PurePosixPath
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree as xml
from defusedxml.common import DefusedXmlException


DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
CARDIO_COLUMNS = ("distance_km", "calories_kcal", "avg_hr_bpm", "max_hr_bpm", "steps")
MAX_EXPANDED_BYTES = 150_000_000
MAX_ACTIVITIES = 5_000


class ImportErrorDetail(ValueError):
    pass


def csv_rows(contents, required):
    try:
        reader = csv.DictReader(io.StringIO(contents.decode("utf-8-sig")), strict=True)
        if not reader.fieldnames:
            raise ImportErrorDetail("The CSV needs header rows and data.")
        reader.fieldnames = [re.sub(r"\W+", "_", name.strip().lower()).strip("_") for name in reader.fieldnames]
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ImportErrorDetail("The CSV has duplicate column names.")
        missing = set(required) - set(reader.fieldnames)
        if missing:
            raise ImportErrorDetail("Missing CSV columns: " + ", ".join(sorted(missing)) + ".")
        for row in reader:
            if None in row:
                raise ImportErrorDetail(f"Line {reader.line_num} has more cells than the header.")
            clean = {key: (value or "").strip() for key, value in row.items()}
            if any(clean.values()):
                yield reader.line_num, clean
    except UnicodeDecodeError:
        raise ImportErrorDetail("Save the CSV as UTF-8, then upload it again.") from None
    except csv.Error as error:
        raise ImportErrorDetail(f"The CSV could not be read: {error}.") from None


def number(value, label, *, minimum=0, ceiling=1_000_000, integer=False, required=False):
    if value is None or value == "":
        if required:
            raise ImportErrorDetail(f"{label} is required.")
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ImportErrorDetail(f"{label} must be a number.") from None
    if not math.isfinite(result) or not minimum <= result <= ceiling or (integer and not result.is_integer()):
        raise ImportErrorDetail(f"{label} must be {'a whole number' if integer else 'a number'} from {minimum} to {ceiling}.")
    return int(result) if integer else result


def parse_routines(contents):
    result = {}
    days = {day.lower(): i for i, day in enumerate(DAY_NAMES, 1)}
    for line, row in csv_rows(contents, ("routine", "weekday", "exercise", "sets")):
        try:
            name, exercise = row["routine"], row["exercise"]
            if not 1 <= len(name) <= 100 or not 1 <= len(exercise) <= 160:
                raise ImportErrorDetail("Use a routine name and exercise name under 100 and 160 characters, respectively.")
            weekday = days.get(row["weekday"].casefold())
            if weekday is None:
                raise ImportErrorDetail("Weekday must be a full day name, such as Monday.")
            specs = {
                "exercise": exercise,
                "sets": number(row["sets"], "Sets", minimum=1, ceiling=99, integer=True, required=True),
                "reps": number(row.get("reps"), "Reps", ceiling=1_000, integer=True),
                "duration_seconds": number(row.get("duration_seconds"), "Duration", minimum=1, ceiling=86_400, integer=True),
                "weight_kg": number(row.get("weight_kg"), "Weight (kg)", ceiling=5_000),
                "notes": row.get("notes", ""),
            }
            if len(specs["notes"]) > 500:
                raise ImportErrorDetail("Keep exercise notes to 500 characters.")
            key = name.casefold()
            routine = result.setdefault(key, {"routine_key": key, "name": name, "weekday": weekday, "exercise_plan": []})
            if routine["weekday"] != weekday:
                raise ImportErrorDetail(f"{name} is assigned to more than one day. Use a different name for each schedule slot.")
            if len(routine["exercise_plan"]) >= 60 or len(result) > 60:
                raise ImportErrorDetail("Import up to 60 routines, with up to 60 exercise rows per routine.")
            routine["exercise_plan"].append(specs)
        except ImportErrorDetail as error:
            raise ImportErrorDetail(f"Line {line}: {error}") from None
    if not result:
        raise ImportErrorDetail("The CSV has no routine rows.")
    return list(result.values())


def timestamp(value, timezone):
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ImportErrorDetail("Use a date and time such as 2026-11-19T07:10:00+05:30.") from None
    if not re.search(r"[T ]\d{2}:\d{2}", value):
        raise ImportErrorDetail("An activity needs its start time as well as its date.")
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone)
    return stamp.astimezone(UTC)


def cardio_record(activity, source, started, duration, details=None, **metrics):
    if not activity or len(activity) > 160 or not source or len(source) > 160:
        raise ImportErrorDetail("Supply a short activity and source name.")
    start_utc = started.astimezone(UTC).isoformat()
    identity = "|".join((source.casefold(), activity.casefold(), start_utc))
    return {
        "fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
        "activity": activity,
        "source": source,
        "started_at": start_utc,
        "duration_seconds": number(duration, "Duration in seconds", minimum=1, ceiling=604_800, integer=True, required=True),
        "distance_km": number(metrics.get("distance_km"), "Distance (km)", ceiling=2000),
        "calories_kcal": number(metrics.get("calories_kcal"), "Calories", ceiling=100_000),
        "avg_hr_bpm": number(metrics.get("avg_hr_bpm"), "Average heart rate", minimum=1, ceiling=350),
        "max_hr_bpm": number(metrics.get("max_hr_bpm"), "Maximum heart rate", minimum=1, ceiling=350),
        "steps": number(metrics.get("steps"), "Steps", ceiling=500_000, integer=True),
        "details": details or {},
    }


def parse_cardio_csv(contents, timezone):
    for line, row in csv_rows(contents, ("activity", "started_at", "duration_seconds")):
        try:
            yield cardio_record(row["activity"], row.get("source") or "Pebble Halo",
                                timestamp(row["started_at"], timezone), row["duration_seconds"],
                                {"csv_fields": {key: value[:500] for key, value in row.items()
                                                if key not in (*CARDIO_COLUMNS, "activity", "started_at", "duration_seconds", "source") and value}},
                                **{key: row.get(key) for key in CARDIO_COLUMNS})
        except ImportErrorDetail as error:
            raise ImportErrorDetail(f"Line {line}: {error}") from None


def metric_value(raw):
    try:
        value = float(raw)
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None


def unit_distance(raw, unit):
    value = metric_value(raw)
    factors = {"km": 1, "m": 0.001, "mi": 1.609344, "yd": 0.0009144}
    return value * factors[unit] if value is not None and unit in factors else None


def unit_energy(raw, unit):
    value = metric_value(raw)
    if value is not None and unit in ("kcal", "kJ", "J"):
        return value / {"kcal": 1, "kJ": 4.184, "J": 4184}[unit]
    return None


def series_stats(samples):
    if not samples:
        return None, None
    heart_rates = [sample["bpm"] for sample in samples]
    return round(sum(heart_rates) / len(heart_rates), 1), max(heart_rates)


def parse_tcx(contents, timezone):
    root = xml.fromstring(contents)
    for activity in root.findall(".//{*}Activities/{*}Activity"):
        laps = activity.findall("{*}Lap")
        started = activity.findtext("{*}Id") or (laps[0].get("StartTime") if laps else None)
        if not started or not laps:
            continue
        durations = [metric_value(lap.findtext("{*}TotalTimeSeconds")) or 0 for lap in laps]
        duration = round(sum(durations))
        if duration <= 0:
            continue
        distances = [value for lap in laps if (value := metric_value(lap.findtext("{*}DistanceMeters"))) is not None]
        calories = [value for lap in laps if (value := metric_value(lap.findtext("{*}Calories"))) is not None]
        points = []
        for point in activity.findall(".//{*}Trackpoint"):
            taken_at = point.findtext("{*}Time")
            bpm = metric_value(point.findtext("{*}HeartRateBpm/{*}Value"))
            if taken_at and bpm is not None and 1 <= bpm <= 350:
                points.append({"at": timestamp(taken_at, timezone).isoformat(), "bpm": bpm})
        weighted_laps = [(bpm, seconds) for lap, seconds in zip(laps, durations)
                         if (bpm := metric_value(lap.findtext("{*}AverageHeartRateBpm/{*}Value"))) and seconds]
        peaks = [bpm for lap in laps if (bpm := metric_value(lap.findtext("{*}MaximumHeartRateBpm/{*}Value")))]
        average, maximum = series_stats(points)
        if weighted_laps:
            average = round(sum(bpm * seconds for bpm, seconds in weighted_laps) / sum(seconds for _, seconds in weighted_laps), 1)
        if peaks:
            maximum = max(peaks + ([maximum] if maximum else []))
        yield cardio_record(activity.get("Sport") or "Workout", "TCX / Google Fit", timestamp(started, timezone), duration,
                            {"heart_rate_samples": points}, distance_km=sum(distances) / 1000 if distances else None,
                            calories_kcal=sum(calories) if calories else None, avg_hr_bpm=average, max_hr_bpm=maximum)


def parse_apple(contents, timezone):
    activities = []
    heart_rate = defaultdict(list)
    root = None
    depth = 0
    for event, element in xml.iterparse(io.BytesIO(contents), events=("start", "end")):
        if event == "start":
            if root is None:
                root = element
                if root.tag != "HealthData":
                    raise ImportErrorDetail("The XML is not an Apple Health export.")
            depth += 1
            continue
        if depth == 2:
            a = element.attrib
            source = a.get("sourceName") or "Apple Health"
            if element.tag == "Workout" and a.get("startDate") and a.get("endDate"):
                stats = {stat.get("type", "").removeprefix("HKQuantityTypeIdentifier"): dict(stat.attrib)
                         for stat in element.findall("WorkoutStatistics")}
                activities.append((dict(a), source, stats))
                if len(activities) > MAX_ACTIVITIES:
                    raise ImportErrorDetail("This file has over 5,000 activities. Upload a smaller export.")
            if element.tag == "Record" and a.get("type") == "HKQuantityTypeIdentifierHeartRate" and a.get("startDate"):
                bpm = metric_value(a.get("value"))
                if bpm is not None and 1 <= bpm <= 350:
                    heart_rate[source].append((timestamp(a["startDate"], timezone), bpm))
            root.clear()
        depth -= 1
    for points in heart_rate.values():
        points.sort()
    for a, source, stats in activities:
        start, end = timestamp(a["startDate"], timezone), timestamp(a["endDate"], timezone)
        duration = metric_value(a.get("duration"))
        factor = {"s": 1, "sec": 1, "min": 60, "hr": 3600, "h": 3600}.get(a.get("durationUnit"))
        elapsed = round(duration * factor) if duration is not None and factor else round((end - start).total_seconds())
        if elapsed <= 0:
            continue
        all_points = heart_rate.get(source, [])
        lo = bisect_left(all_points, (start, 0))
        hi = bisect_right(all_points, (end, 351))
        points = [{"at": at.isoformat(), "bpm": bpm} for at, bpm in all_points[lo:hi]]
        average, maximum = series_stats(points)
        hr = stats.get("HeartRate", {})
        average = metric_value(hr.get("average")) or average
        maximum = metric_value(hr.get("maximum")) or maximum
        distances = [value for key, stat in stats.items() if key.startswith("Distance")
                     and (value := unit_distance(stat.get("sum"), stat.get("unit"))) is not None]
        distance = max(distances) if distances else unit_distance(a.get("totalDistance"), a.get("totalDistanceUnit"))
        energy = stats.get("ActiveEnergyBurned", {})
        calories = unit_energy(energy.get("sum"), energy.get("unit"))
        if calories is None:
            calories = unit_energy(a.get("totalEnergyBurned"), a.get("totalEnergyBurnedUnit"))
        activity_type = a.get("workoutActivityType", "Workout").removeprefix("HKWorkoutActivityType")
        activity = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", activity_type)
        yield cardio_record(activity, f"Apple Health / {source}", start, elapsed,
                            {"statistics": stats, "heart_rate_samples": points, "ended_at": end.isoformat()},
                            distance_km=distance, calories_kcal=calories, avg_hr_bpm=average, max_hr_bpm=maximum,
                            steps=stats.get("StepCount", {}).get("sum"))


def parse_cardio(filename, contents, timezone):
    def entries():
        extension = PurePosixPath(filename).suffix.lower()
        if extension == ".csv":
            yield from parse_cardio_csv(contents, timezone)
        elif extension == ".tcx":
            yield from parse_tcx(contents, timezone)
        elif extension == ".xml":
            yield from parse_apple(contents, timezone)
        elif extension == ".zip":
            with zipfile.ZipFile(io.BytesIO(contents)) as archive:
                files = [file for file in archive.infolist() if not file.is_dir()
                         and (PurePosixPath(file.filename).name.lower() == "export.xml" or file.filename.lower().endswith(".tcx"))]
                if sum(file.file_size for file in files) > MAX_EXPANDED_BYTES:
                    raise ImportErrorDetail("This export exceeds 150 MB after opening. Upload a smaller export.")
                for file in files:
                    data = archive.read(file)
                    if file.filename.lower().endswith(".tcx"):
                        yield from parse_tcx(data, timezone)
                    else:
                        yield from parse_apple(data, timezone)
        else:
            raise ImportErrorDetail("Choose a cardio CSV, TCX, Apple Health XML, or a supported ZIP export.")

    seen = {}
    try:
        for entry in entries():
            seen[entry["fingerprint"]] = entry
            if len(seen) > MAX_ACTIVITIES:
                raise ImportErrorDetail("This file has over 5,000 activities. Upload a smaller export.")
    except (DefusedXmlException, ParseError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise ImportErrorDetail("This activity export is damaged or in an unsupported format.") from None
    if not seen:
        raise ImportErrorDetail("No timed workouts were found. Upload a workout export or use the cardio CSV template.")
    return sorted(seen.values(), key=lambda entry: entry["started_at"], reverse=True)
