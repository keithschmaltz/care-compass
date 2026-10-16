-- CARE COMPASS, step 3: create the app on the warehouse runtime.
-- First upload care_compass_app.py to the CARE.APP_FILES stage
-- (Catalog > Database Explorer > DEFAULT_DATABASE > CARE > Stages > APP_FILES > + Files).
-- Run each statement on its own.

USE ROLE ATTENDEE_ROLE;
USE WAREHOUSE DEFAULT_WH;

LIST @DEFAULT_DATABASE.CARE.APP_FILES;

CREATE OR REPLACE STREAMLIT DEFAULT_DATABASE.CARE.CARE_COMPASS
  FROM '@DEFAULT_DATABASE.CARE.APP_FILES'
  MAIN_FILE = 'care_compass_app.py'
  QUERY_WAREHOUSE = DEFAULT_WH
  RUNTIME_NAME = 'SYSTEM$WAREHOUSE_RUNTIME'
  TITLE = 'Care Compass';

ALTER STREAMLIT DEFAULT_DATABASE.CARE.CARE_COMPASS ADD LIVE VERSION FROM LAST;

-- Then open it from Projects > Streamlit > Care Compass
