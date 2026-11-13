# Teacher Portal

All-in-one web app for lecturers: dashboard, calendar, student booking links,
attendance tracker, AI-sorted student inquiries (Gemini), to-do list.

## Run it
    pip install flask
    python app.py
Open http://127.0.0.1:5000 and log in with:
    lecturer@apu.edu.my / password123

## Turn on Gemini AI
Option 1: Settings tab in the app -> paste key -> Save -> Test connection
Option 2: set an environment variable before running
    Windows:   set GEMINI_API_KEY=your_key
    Mac/Linux: export GEMINI_API_KEY=your_key
Get a free key at https://aistudio.google.com
Without a key, the app still works using simple keyword rules.

## Student links
- Booking Links tab -> Generate link -> students pick a free slot -> appears in your calendar
- Inquiries tab -> copy your inquiry link -> students send messages -> Gemini summarises them

To let students on other devices open the links, run with
    flask --app app run --host 0.0.0.0
and share your laptop's IP address (same Wi-Fi), or deploy to Render / PythonAnywhere.

## Reset demo data
Delete portal.db and run app.py again.

## Files
    app.py                 Flask backend + SQLite database + Gemini calls
    templates/portal.html  ONE file for every page (lecturer portal, login/register,
                           student booking + inquiry pages), with CSS, icons and logo inside
