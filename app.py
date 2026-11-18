import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import streamlit as st

from backend import BackendError, Supabase
from imports import DAY_NAMES, ImportErrorDetail, parse_cardio, parse_routines


ROOT = Path(__file__).resolve().parent

st.set_page_config(page_title="Workout Log", page_icon="🏋", layout="centered", initial_sidebar_state="collapsed")
st.markdown("""
<style>
.block-container { max-width: 870px; padding-top: 3.3rem; padding-bottom: 3rem; }
[data-testid="stMetric"] { background: #edf2eb; border-radius: 12px; padding: .65rem .9rem; }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
[data-testid="stVerticalBlockBorderWrapper"] { border-radius: 15px; }
@media (max-width: 600px) { .block-container { padding-top: 2.4rem; } }
</style>
""", unsafe_allow_html=True)


def setting(name, default=""):
    if name in os.environ:
        return os.environ[name]
    try:
        return st.secrets.get(name, default)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return default


def instant(value):
    return datetime.fromisoformat(value)


def clock_text(seconds):
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}" if hours else f"{minutes:02}:{seconds:02}"


def local_stamp(value, timezone):
    return instant(value).astimezone(timezone).strftime("%d %b %Y · %I:%M %p")


def finish_notice(message):
    st.session_state["notice"] = message
    st.rerun()


def login(db):
    st.title("Workout Log")
    st.caption("Your private strength and cardio log")
    with st.form("sign-in", clear_on_submit=True, border=True):
        email = st.text_input("Email", autocomplete="email")
        password = st.text_input("Password", type="password", autocomplete="current-password")
        submit = st.form_submit_button("Sign in", type="primary", width="stretch")
    if submit:
        if not email.strip() or not password:
            st.error("Enter your email and password.")
            return
        try:
            db.sign_in(email.strip(), password)
        except BackendError as error:
            st.error(str(error))
            return
        st.rerun()


def plan_rows(plan):
    return [{"Exercise": row["exercise"], "Sets": row["sets"], "Reps": row["reps"],
             "Seconds": row["duration_seconds"], "Target kg": row["weight_kg"], "Notes": row["notes"]}
            for row in plan]


def plan_table(plan):
    frame = pd.DataFrame(plan_rows(plan)).dropna(axis=1, how="all")
    if not frame["Notes"].any():
        frame = frame.drop(columns="Notes")
    st.dataframe(frame, hide_index=True, width="stretch")


@st.fragment(run_every=1)
def live_clocks(started_at, latest_log, pending_since):
    now = datetime.now(UTC)
    main, detail = st.columns(2)
    main.metric("Workout time", clock_text((now - instant(started_at)).total_seconds()))
    if pending_since:
        detail.metric("Set time", clock_text((now - instant(pending_since)).total_seconds()))
    elif latest_log:
        detail.metric("Since last set", clock_text((now - instant(latest_log)).total_seconds()))


def active_workout(db, workout):
    session_id = workout["id"]
    sets = list(db.all_rows("workout_sets", session_id=f"eq.{session_id}", order="logged_at.desc,id.desc"))
    count = Counter(row["exercise_index"] for row in sets)
    plan = workout["exercise_plan"]
    pending = workout["pending_since"]
    st.subheader(workout["routine_name"])
    live_clocks(workout["started_at"], sets[0]["logged_at"] if sets else None, pending)
    if st.button("Stop workout", type="primary", width="stretch", key=f"stop-{session_id}"):
        db.rpc("workout_stop", p_session=session_id)
        finish_notice(f'{workout["routine_name"]} saved to History.')

    st.divider()
    default = next((i for i, row in enumerate(plan) if count[i] < row["sets"]), len(plan) - 1)
    selected = st.selectbox("Exercise", range(len(plan)), index=workout["pending_exercise"] if pending else default,
                            format_func=lambda index: f'{plan[index]["exercise"]} · {count[index]}/{plan[index]["sets"]} sets',
                            disabled=bool(pending), key=f"exercise-{session_id}-{len(sets)}-{pending or 'ready'}")
    exercise = plan[selected]
    next_set = count[selected] + 1
    if exercise["notes"]:
        st.caption(exercise["notes"])
    if next_set > 99:
        st.info("This exercise has reached 99 sets. Choose another exercise or stop the workout.")
        return

    if pending:
        if st.button("Clear set timer", key="clear-set"):
            db.rpc("workout_clear_set", p_session=session_id, p_since=pending)
            st.rerun()
    else:
        if st.button("Begin set", help="Optional. Tap before lifting to measure the rest since your last set."):
            db.rpc("workout_begin_set", p_session=session_id, p_exercise=selected, p_set=next_set)
            st.rerun()

    exercise_filter = "in.(" + json.dumps(exercise["exercise"], ensure_ascii=False) + ")"
    previous = db.rows("workout_sets", exercise_name=exercise_filter,
                       select="weight_kg,reps", order="logged_at.desc,id.desc", limit=1)
    previous_weight = previous[0]["weight_kg"] if previous else None
    weight = previous_weight if previous_weight is not None else exercise["weight_kg"]
    previous_reps = previous[0]["reps"] if previous else None
    form_id = f"set-{session_id}-{selected}-{next_set}"
    if st.session_state.get("request_scope") != form_id:
        st.session_state["request_scope"] = form_id
        st.session_state["set_request_id"] = str(uuid4())
    with st.form(form_id, border=True):
        st.markdown(f"**Set {next_set}**")
        if previous and (previous_weight is not None or previous_reps is not None):
            previous_values = [f"{previous_weight:g} kg"] if previous_weight is not None else []
            if previous_reps is not None:
                previous_values.append(f"{previous_reps} reps")
            st.caption("Last logged · " + " × ".join(previous_values))
        a, b = st.columns(2)
        weight_kg = a.number_input("Weight (kg)", min_value=0.0, max_value=5000.0, step=0.5,
                                   value=float(weight) if weight is not None else None, placeholder="Optional")
        reps = b.number_input("Reps", min_value=0, max_value=1000, step=1, value=exercise["reps"], placeholder="Optional for timed sets")
        duration = None
        if exercise["duration_seconds"] is not None:
            duration = st.number_input("Set duration (seconds)", min_value=1, max_value=86400,
                                       step=1, value=exercise["duration_seconds"])
        submitted = st.form_submit_button("Log set", type="primary", width="stretch")
    if submitted:
        if reps is None and duration is None:
            st.warning("Enter your reps before logging this set.")
        else:
            db.rpc("workout_log_set", p_session=session_id, p_exercise=selected, p_set=next_set,
                   p_request=st.session_state["set_request_id"], p_weight=weight_kg, p_reps=reps, p_duration=duration)
            st.rerun()
    if sets:
        with st.expander(f"Logged sets · {len(sets)}"):
            set_table(sets)


def today(db, timezone):
    workout = db.rows("workout_sessions", finished_at="is.null", order="started_at.desc", limit=1)
    if workout:
        active_workout(db, workout[0])
        return
    now = datetime.now(timezone)
    st.caption(now.strftime("%A, %d %B"))
    routines = db.rows("workout_routines", order="weekday.asc,name.asc")
    if not routines:
        st.subheader("Add your first routine")
        st.write("Open **Import** and add the included A/B schedule or upload your own routine CSV.")
        return
    by_id = {row["id"]: row for row in routines}
    scheduled = [row["id"] for row in routines if row["weekday"] == now.isoweekday()]
    if not scheduled:
        st.subheader("Rest day")
    first = list(by_id).index(scheduled[0]) if scheduled else None
    selected = st.selectbox("Routine", list(by_id), index=first,
                            format_func=lambda key: f'{by_id[key]["name"]} · {DAY_NAMES[by_id[key]["weekday"] - 1]}',
                            placeholder="Choose a routine")
    if selected:
        if st.button("Start workout", type="primary", width="stretch"):
            db.rpc("workout_start", p_routine=selected)
            st.rerun()
        plan_table(by_id[selected]["exercise_plan"])
    with st.expander("Weekly schedule"):
        names = defaultdict(list)
        for row in routines:
            names[row["weekday"]].append(row["name"])
        st.table([{"Day": day, "Routine": ", ".join(names[i]) or "Rest"} for i, day in enumerate(DAY_NAMES, 1)])


def set_table(sets):
    st.dataframe([{"Exercise": row["exercise_name"], "Set": row["set_number"], "kg": row["weight_kg"],
                   "Reps": row["reps"], "Set duration (s)": row["duration_seconds"],
                   "Rest (s)": row["rest_seconds"], "Between logs (s)": row["gap_seconds"]}
                  for row in sets], hide_index=True, width="stretch")


def duration_of(workout):
    return max(0, int((instant(workout["finished_at"]) - instant(workout["started_at"])).total_seconds()))


def activity_end(activity):
    details = activity.get("details") or {}
    return instant(details["ended_at"]) if details.get("ended_at") else instant(activity["started_at"]) + timedelta(seconds=activity["duration_seconds"])


def cardio_matches(workout, activities):
    start, end = instant(workout["started_at"]), instant(workout["finished_at"])
    return [activity for activity in activities if instant(activity["started_at"]) < end and activity_end(activity) > start]


def cardio_table(activities, timezone):
    st.dataframe([{"Activity": row["activity"], "Started": local_stamp(row["started_at"], timezone),
                   "Time": clock_text(row["duration_seconds"]), "km": row["distance_km"],
                   "kcal": row["calories_kcal"], "Avg HR": row["avg_hr_bpm"], "Max HR": row["max_hr_bpm"],
                   "Steps": row["steps"], "Source": row["source"]} for row in activities],
                 hide_index=True, width="stretch")


def history(db, timezone):
    workouts = list(db.all_rows("workout_sessions", finished_at="not.is.null", order="started_at.desc,id.desc"))
    cardio = list(db.all_rows("workout_cardio", order="started_at.desc,id.desc"))
    if not workouts and not cardio:
        st.subheader("No activity yet")
        st.write("Completed workouts and imported cardio will appear here.")
        return
    daily = defaultdict(lambda: {"Strength": 0, "Cardio": 0})
    for workout in workouts:
        daily[instant(workout["started_at"]).astimezone(timezone).date()]["Strength"] += duration_of(workout) / 60
    for activity in cardio:
        daily[instant(activity["started_at"]).astimezone(timezone).date()]["Cardio"] += activity["duration_seconds"] / 60
    combined = pd.DataFrame.from_dict(daily, orient="index").sort_index().tail(30)
    combined.index = pd.to_datetime(combined.index)
    st.subheader("Training time")
    st.caption("Minutes logged by date. A wearable activity can overlap a strength session.")
    st.bar_chart(combined, stack=False, color=["#1b6b54", "#4478a6"])

    if workouts:
        st.subheader("Strength sessions")
        lookup = {row["id"]: row for row in workouts}
        selected = st.selectbox("Workout", lookup, format_func=lambda key: f'{lookup[key]["routine_name"]} · {local_stamp(lookup[key]["started_at"], timezone)}')
        workout = lookup[selected]
        sets = list(db.all_rows("workout_sets", session_id=f"eq.{selected}", order="logged_at.asc,id.asc"))
        measured = [row["rest_seconds"] for row in sets if row["rest_seconds"] is not None]
        a, b = st.columns(2)
        a.metric("Duration", clock_text(duration_of(workout)))
        b.metric("Sets logged", len(sets))
        if measured:
            st.caption(f"Average measured rest · {clock_text(sum(measured) / len(measured))} across {len(measured)} intervals")
        if sets:
            set_table(sets)
        matches = cardio_matches(workout, cardio)
        if matches:
            st.markdown("**Wearable activities during this session**")
            cardio_table(matches, timezone)

    if cardio:
        st.subheader("Wearable activities")
        cardio_table(cardio, timezone)
        sampled = {row["id"]: row for row in cardio if (row.get("details") or {}).get("heart_rate_samples")}
        if sampled:
            selected = st.selectbox("Heart-rate trace", sampled, format_func=lambda key: f'{sampled[key]["activity"]} · {local_stamp(sampled[key]["started_at"], timezone)}')
            series = pd.DataFrame(sampled[selected]["details"]["heart_rate_samples"])
            series["at"] = pd.to_datetime(series["at"], utc=True, format="ISO8601").dt.tz_convert(timezone)
            st.line_chart(series, x="at", y="bpm", x_label="Time", y_label="Heart rate (bpm)", color="#1b6b54")
    with st.expander("Export for analysis"):
        if st.button("Prepare data export"):
            payload = {
                "exported_at": datetime.now(UTC).isoformat(),
                "routines": db.rows("workout_routines", order="weekday.asc,name.asc"),
                "sessions": list(db.all_rows("workout_sessions", order="started_at.asc,id.asc")),
                "sets": list(db.all_rows("workout_sets", order="logged_at.asc,id.asc")),
                "cardio": cardio,
            }
            st.download_button("Download workout data", json.dumps(payload, indent=2), "workout-data.json",
                               "application/json", on_click="ignore")


def routine_import(db):
    st.subheader("Routines")
    workout_csv = (ROOT / "routines.csv").read_bytes()
    st.download_button("Download A/B routine CSV", workout_csv, "routines.csv", "text/csv")
    upload = st.file_uploader("Routine CSV", type="csv", key="routine-upload", max_upload_size=2)
    if upload:
        routines = parse_routines(upload.getvalue())
        st.table([{"Routine": row["name"], "Day": DAY_NAMES[row["weekday"] - 1], "Exercises": len(row["exercise_plan"])}
                  for row in routines])
        st.caption("A matching routine name updates its schedule and exercises. Workout history keeps the original routine used.")
        if st.button("Save routines", type="primary"):
            db.upsert("workout_routines", routines, "user_id,routine_key")
            finish_notice(f"Saved {len(routines)} routines.")
    elif st.button("Add the included A/B schedule", type="primary"):
        routines = parse_routines(workout_csv)
        db.upsert("workout_routines", routines, "user_id,routine_key")
        finish_notice(f"Saved {len(routines)} routines.")


def cardio_import(db, timezone):
    st.subheader("Pebble Halo cardio")
    st.caption("Import timed activities from Apple Health, Google Fit TCX, or the cardio CSV template.")
    st.download_button("Download cardio CSV template", (ROOT / "cardio-template.csv").read_bytes(),
                       "cardio-template.csv", "text/csv")
    upload = st.file_uploader("Cardio export", type=["csv", "tcx", "xml", "zip"], key="cardio-upload")
    if not upload:
        st.session_state.pop("cardio_preview", None)
        return
    contents = upload.getvalue()
    digest = hashlib.sha256(contents).hexdigest(), upload.name.lower()
    previous = st.session_state.get("cardio_preview")
    if previous is None or previous[0] != digest:
        previous = digest, parse_cardio(upload.name, contents, timezone)
        st.session_state["cardio_preview"] = previous
    sources = sorted({row["source"] for row in previous[1]})
    halo = [source for source in sources if "pebble" in source.lower() or "halo" in source.lower()]
    chosen = st.multiselect("Activity sources", sources, default=halo or (sources if len(sources) == 1 else []),
                            key=f"sources-{digest[0]}")
    rows = [row for row in previous[1] if row["source"] in chosen]
    if rows:
        st.caption(f"{len(rows)} activities selected")
        cardio_table(rows[:50], timezone)
    if st.button("Import cardio", type="primary", disabled=not rows):
        for offset in range(0, len(rows), 10):
            db.upsert("workout_cardio", rows[offset:offset + 10], "user_id,fingerprint")
        finish_notice(f"Saved {len(rows)} cardio activities. Re-imports update matching activities.")


def main():
    url, key = setting("SUPABASE_URL"), setting("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key or "REPLACE_ME" in key:
        st.title("Workout Log")
        st.info("Connect your Supabase project to start logging. Open the included README for setup.")
        return
    try:
        timezone = ZoneInfo(setting("WORKOUT_TIMEZONE", "Asia/Kolkata"))
    except ZoneInfoNotFoundError:
        st.error("Set WORKOUT_TIMEZONE to a valid timezone such as Asia/Kolkata.")
        return
    try:
        db = Supabase(url, key, st.session_state)
        if "auth" not in st.session_state:
            login(db)
            return
        st.title("Workout Log")
        notice = st.session_state.pop("notice", None)
        if notice:
            st.toast(notice)
        with st.sidebar:
            if st.button("Sign out"):
                try:
                    db.sign_out()
                finally:
                    st.session_state.clear()
                    st.rerun()
        page = st.segmented_control("Page", ["Today", "History", "Import"], default="Today", label_visibility="collapsed") or "Today"
        if page == "Today":
            today(db, timezone)
        elif page == "History":
            history(db, timezone)
        else:
            try:
                routine_import(db)
            except ImportErrorDetail as error:
                st.error(str(error))
            st.divider()
            cardio_import(db, timezone)
    except ImportErrorDetail as error:
        st.error(str(error))
    except BackendError as error:
        if error.status == 401:
            st.session_state.pop("auth", None)
            st.rerun()
        st.error(str(error))


main()
