# Care Compass

Helps family caregivers understand a parent's health records and bills, in plain language and in the way that suits them. Built in one day for the Snowflake "AI at Work: Hack the Everyday" hackathon (Hack the Experience track) at Data in the D, Detroit.

All data is synthetic, from the event's `HEALTHCARE_DW` dataset.

## What's here

| File | What it does |
| --- | --- |
| `11_care_compass_build.sql` | Builds the app's data layer in `DEFAULT_DATABASE.CARE`: patients, visits, conditions, medications, bills with a typical-charge benchmark, documents and trials |
| `care_compass_app.py` | The Streamlit in Snowflake app |
| `13_care_compass_create_app.sql` | Creates the app on the warehouse runtime from an internal stage |

## How to run it

1. In a Snowflake account with the event's `HEALTHCARE_DW` database, run `11_care_compass_build.sql` with Run All.
2. Upload `care_compass_app.py` to the `DEFAULT_DATABASE.CARE.APP_FILES` stage.
3. Run the statements in `13_care_compass_create_app.sql` one at a time.
4. Open the app from Projects → Streamlit → Care Compass.

Requires access to Snowflake Cortex (`SNOWFLAKE.CORTEX.COMPLETE`) with the Claude models.

## Snowflake features used

SQL data layer with window and percentile functions, semi-structured JSON (VARIANT), role-based access with secondary roles, an internal stage, Streamlit in Snowflake on the warehouse runtime, and Cortex AI (COMPLETE with Claude Sonnet 4.6).

## Limitations

It's a working prototype: no login, so anyone with the app can pick any patient; "today" is the latest date in the records; current medications are inferred from prescriptions; trial matching is by condition category, not eligibility rules. Care Compass explains records and is not medical advice.
