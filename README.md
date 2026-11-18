# Workout Log

Private Streamlit app for the six-day Push, Pull, Legs A/B rotation. Use it in your phone browser to log your workouts and review strength and wearable activity together.

## Set up Supabase once

1. Create a Supabase project. In **Authentication → Users**, create your own email/password user and mark the email as confirmed. Copy its **User UID**. In your authentication settings, disable new user sign-ups and keep email sign-in enabled.
2. In the project's **SQL Editor**, run `schema.sql`. Then run the following with your own User UID in place of the example. This assigns the app to one account at the database level.

   ```sql
   insert into workout_private.owner (user_id)
   values ('00000000-0000-0000-0000-000000000000');
   ```

3. Copy `.streamlit/secrets.example.toml` to `.streamlit/secrets.toml`. Enter the project URL and **publishable key** shown by Supabase. Use the publishable key or the legacy anon key. Keep the private service role and secret keys out of the app. `WORKOUT_TIMEZONE` defaults to `Asia/Kolkata`.

## Run the app

With Python 3.11 or newer, open a terminal in this folder and run:

```text
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

For phone access away from home, deploy `app.py` from a repository using [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app). Put the three values from your local `secrets.toml` into the app's Secrets setting. Keep the secrets file out of the repository. The web app requires a connection to log and sync.

## Follow your routine

In **Import**, choose **Add the included A/B schedule** or upload a routine CSV. The included `routines.csv` contains the six sessions from the earlier draft. Sunday is a rest day.

| Monday | Tuesday | Wednesday | Thursday | Friday | Saturday |
| --- | --- | --- | --- | --- | --- |
| Push A | Pull A | Leg A | Push B | Pull B | Leg B |

**Today** selects the scheduled routine. You can also choose a different one. Tap **Start workout**, then **Stop workout** when you finish. The app shows one session control at a time. If the page closes, sign in again and the active session and completed sets will still be there.

Tap **Log set** after each set. The app automatically records the time since the previous set was logged. That interval can include part of the next set. To measure rest precisely, tap **Begin set** immediately before the next set. Rest is then measured from the previous **Log set** to this **Begin set**. Each interval is labeled in History.

Routine CSVs use one row per exercise in the order it should appear. Required columns are `routine`, `weekday`, `exercise`, and `sets`. Use full weekdays and UTF-8 CSV. Optional columns are `reps`, `duration_seconds`, `weight_kg`, and `notes`. Importing a matching routine name updates the routine while keeping earlier workout records intact.

## Import Pebble Halo activities

Start a timed activity on the ring if your Halo app and device support it, then sync the ring with the phone. The logger imports files from your phone. It does not pair with the ring or pull live data over Bluetooth.

- **iPhone:** If Pebble Halo syncs your workouts to Apple Health, open Health → your profile → **Export All Health Data**. Upload the ZIP or its `export.xml` under **Import → Pebble Halo cardio**. Choose the Halo source in the preview when it is present. [Apple Health export](https://support.apple.com/guide/iphone/iph5ede58c3d/ios)
- **Android:** If a Google Fit export contains timed TCX activities from Halo, upload the `.tcx` files or the archive that contains them. If your companion app sends workouts only to Health Connect, use a **workout export in TCX or the included cardio CSV format**. Android's Health Connect device-transfer backup is a separate format. [Pebble Halo on Google Play](https://play.google.com/store/apps/details?id=com.pebble.ring.app) · [Google Takeout](https://support.google.com/accounts/answer/3024190)
- **CSV:** When your phone shows a workout summary, you can copy it into `cardio-template.csv`. Each row needs `activity`, `started_at`, and `duration_seconds`. Include the UTC offset when known, for example `2026-11-19T07:10:00+05:30`. Distance, calories, average and maximum heart rate, steps, and source are optional. Blank fields remain blank.

Only timed workout records in a supported format are imported. When available, the app records heart-rate samples within the activity, duration, distance, calories, and steps. Individual fields depend on what Halo exported. History shows wearable activities alongside overlapping strength sessions, and **Export for analysis** downloads the routines, sessions, set timings and imported data as JSON.
