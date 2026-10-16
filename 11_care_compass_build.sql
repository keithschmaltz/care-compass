-- CARE COMPASS, step 1: build the app's data layer. Run with Run All (Ctrl+Shift+Enter / Cmd+Shift+Enter).
-- Reads HEALTHCARE_DW through secondary roles; writes tables owned by ATTENDEE_ROLE in DEFAULT_DATABASE.CARE.
-- Everything is a table (no views), so the Streamlit app can read it with ATTENDEE_ROLE's own rights.

USE ROLE ATTENDEE_ROLE;
USE SECONDARY ROLES ALL;
USE WAREHOUSE DEFAULT_WH;
USE DATABASE DEFAULT_DATABASE;
CREATE SCHEMA IF NOT EXISTS DEFAULT_DATABASE.CARE;

-- 1. Candidate patients for the persona: older, several chronic conditions, rich records.
--    The app's patient picker lists these; the top one is the demo patient.
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.PATIENTS AS
WITH dx AS (
    SELECT patient_id,
           COUNT(DISTINCT IFF(is_chronic, icd10_code, NULL)) AS chronic_conditions,
           COUNT(DISTINCT icd10_code)                        AS conditions
    FROM HEALTHCARE_DW.CLINICAL.DIAGNOSES GROUP BY patient_id
),
enc AS (
    SELECT patient_id, COUNT(*) AS encounters, MAX(encounter_date) AS last_visit
    FROM HEALTHCARE_DW.CLINICAL.ENCOUNTERS GROUP BY patient_id
),
rx AS (
    SELECT patient_id, COUNT(DISTINCT medication_id) AS medications
    FROM HEALTHCARE_DW.PHARMACY.PRESCRIPTIONS GROUP BY patient_id
),
docs AS (
    SELECT patient_id, COUNT(*) AS documents
    FROM HEALTHCARE_DW.CORE.MEDICAL_DOCUMENTS GROUP BY patient_id
)
SELECT
    p.patient_id, p.medical_record_number, p.first_name, p.last_name,
    p.first_name || ' ' || p.last_name                 AS full_name,
    DATEDIFF(year, p.date_of_birth, CURRENT_DATE())    AS age,
    p.date_of_birth, p.gender, p.state_code, p.primary_insurance, p.blood_type, p.status,
    dx.chronic_conditions, dx.conditions,
    enc.encounters, enc.last_visit,
    COALESCE(rx.medications, 0) AS medications,
    COALESCE(docs.documents, 0) AS documents,
    dx.chronic_conditions * 3 + LEAST(COALESCE(rx.medications, 0), 6) * 2
      + LEAST(enc.encounters, 12) + LEAST(COALESCE(docs.documents, 0), 3) * 3 AS richness
FROM HEALTHCARE_DW.CORE.PATIENTS p
JOIN dx  ON p.patient_id = dx.patient_id
JOIN enc ON p.patient_id = enc.patient_id
LEFT JOIN rx   ON p.patient_id = rx.patient_id
LEFT JOIN docs ON p.patient_id = docs.patient_id
WHERE DATEDIFF(year, p.date_of_birth, CURRENT_DATE()) BETWEEN 65 AND 85
  AND dx.chronic_conditions >= 2
QUALIFY ROW_NUMBER() OVER (ORDER BY richness DESC, p.patient_id) <= 200;

-- 2. Visits, with the doctor and the facility named
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.VISITS AS
SELECT
    e.encounter_id, e.patient_id, e.encounter_date, e.encounter_type, e.length_of_stay_days,
    e.status AS visit_status, e.chief_complaint, e.clinical_notes, e.vitals,
    pr.first_name || ' ' || pr.last_name || COALESCE(', ' || pr.credential, '') AS provider_name,
    pr.specialty,
    o.org_name AS facility_name
FROM HEALTHCARE_DW.CLINICAL.ENCOUNTERS e
JOIN DEFAULT_DATABASE.CARE.PATIENTS cp ON e.patient_id = cp.patient_id
LEFT JOIN HEALTHCARE_DW.CORE.PROVIDERS pr    ON e.provider_id = pr.provider_id
LEFT JOIN HEALTHCARE_DW.CORE.ORGANIZATIONS o ON e.facility_org_id = o.org_id;

-- 3. Conditions (diagnoses)
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.CONDITIONS AS
SELECT d.diagnosis_id, d.encounter_id, d.patient_id, d.icd10_code, d.diagnosis_description,
       d.category, d.is_chronic, d.diagnosis_date, d.diagnosis_rank
FROM HEALTHCARE_DW.CLINICAL.DIAGNOSES d
JOIN DEFAULT_DATABASE.CARE.PATIENTS cp ON d.patient_id = cp.patient_id;

-- 4. Medications, with what each one is for and when the supply runs out
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.MEDICATIONS AS
SELECT
    rx.prescription_id, rx.patient_id, rx.encounter_id, rx.medication_id,
    rx.medication_name, m.generic_name, m.brand_name, m.drug_class, m.treats_icd10, m.route, m.controlled,
    rx.dosage, rx.frequency, rx.prescribed_date, rx.days_supply, rx.refills_remaining,
    DATEADD(day, rx.days_supply, rx.prescribed_date) AS supply_ends,
    rx.cost, rx.status AS rx_status, rx.pharmacist_notes,
    pr.first_name || ' ' || pr.last_name AS prescriber_name, pr.specialty AS prescriber_specialty
FROM HEALTHCARE_DW.PHARMACY.PRESCRIPTIONS rx
JOIN DEFAULT_DATABASE.CARE.PATIENTS cp ON rx.patient_id = cp.patient_id
LEFT JOIN HEALTHCARE_DW.PHARMACY.MEDICATIONS m ON rx.medication_id = m.medication_id
LEFT JOIN HEALTHCARE_DW.CORE.PROVIDERS pr      ON rx.provider_id = pr.provider_id;

-- 5. What a visit usually costs, from ALL patients' claims (by visit type and procedure code)
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.BILL_BENCHMARKS AS
SELECT
    c.encounter_type, c.primary_cpt_code,
    COUNT(*)                                                    AS claims,
    MEDIAN(c.billed_amount)                                     AS median_billed,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY c.billed_amount) AS p90_billed,
    MEDIAN(p.patient_responsibility)                            AS median_patient_owes
FROM HEALTHCARE_DW.BILLING.CLAIMS c
LEFT JOIN HEALTHCARE_DW.BILLING.PAYMENTS p ON c.claim_id = p.claim_id
GROUP BY ALL;

-- 6. Bills: charged, paid by insurance, owed, compared with what that kind of visit usually costs
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.BILLS AS
WITH type_level AS (   -- fallback benchmark when a procedure code is rare
    SELECT c.encounter_type,
           MEDIAN(c.billed_amount)                                      AS median_billed,
           PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY c.billed_amount) AS p90_billed
    FROM HEALTHCARE_DW.BILLING.CLAIMS c GROUP BY c.encounter_type
)
SELECT
    c.claim_id, c.claim_number, c.encounter_id, c.patient_id, c.service_date, c.submitted_date,
    c.encounter_type, c.primary_cpt_code, c.payer_name, c.status AS claim_status,
    c.billed_amount, p.allowed_amount, p.insurance_paid, p.patient_responsibility,
    p.payment_date, p.payment_method, p.claim_status AS payment_status,
    pr.first_name || ' ' || pr.last_name AS provider_name, o.org_name AS facility_name,
    IFF(b.claims >= 30, b.median_billed, t.median_billed) AS typical_billed,
    IFF(b.claims >= 30, b.p90_billed,    t.p90_billed)    AS high_billed_threshold,
    ROUND(c.billed_amount / NULLIF(IFF(b.claims >= 30, b.median_billed, t.median_billed), 0), 2) AS vs_typical,
    c.billed_amount > IFF(b.claims >= 30, b.p90_billed, t.p90_billed) AS is_unusually_high
FROM HEALTHCARE_DW.BILLING.CLAIMS c
JOIN DEFAULT_DATABASE.CARE.PATIENTS cp ON c.patient_id = cp.patient_id
LEFT JOIN HEALTHCARE_DW.BILLING.PAYMENTS p      ON c.claim_id = p.claim_id
LEFT JOIN HEALTHCARE_DW.CORE.PROVIDERS pr       ON c.provider_id = pr.provider_id
LEFT JOIN HEALTHCARE_DW.CORE.ORGANIZATIONS o    ON c.facility_org_id = o.org_id
LEFT JOIN DEFAULT_DATABASE.CARE.BILL_BENCHMARKS b
       ON c.encounter_type = b.encounter_type AND c.primary_cpt_code = b.primary_cpt_code
LEFT JOIN type_level t ON c.encounter_type = t.encounter_type;

-- 7. Scanned documents (lab reports, discharge summaries, ...) with their OCR text
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.DOCUMENTS AS
SELECT d.document_id, d.patient_id, d.document_type, d.file_name, d.page_count,
       d.uploaded_date, d.ocr_extracted_text
FROM HEALTHCARE_DW.CORE.MEDICAL_DOCUMENTS d
JOIN DEFAULT_DATABASE.CARE.PATIENTS cp ON d.patient_id = cp.patient_id;

-- 8. All clinical trials with the site named (matching to conditions comes next, once we see the values)
CREATE OR REPLACE TABLE DEFAULT_DATABASE.CARE.TRIALS AS
SELECT t.trial_id, t.nct_number, t.trial_name, t.phase, t.status AS trial_status, t.therapeutic_area,
       t.start_date, t.end_date, t.target_enrollment, t.protocol_metadata,
       site.org_name AS site_name, site.hq_city AS site_city, site.hq_state AS site_state,
       sponsor.org_name AS sponsor_name,
       (SELECT COUNT(*) FROM HEALTHCARE_DW.RESEARCH.TRIAL_ENROLLMENTS e WHERE e.trial_id = t.trial_id) AS enrolled
FROM HEALTHCARE_DW.RESEARCH.CLINICAL_TRIALS t
LEFT JOIN HEALTHCARE_DW.CORE.ORGANIZATIONS site    ON t.site_org_id = site.org_id
LEFT JOIN HEALTHCARE_DW.CORE.ORGANIZATIONS sponsor ON t.sponsor_org_id = sponsor.org_id;

-- 9. Stage for the app's files
CREATE STAGE IF NOT EXISTS DEFAULT_DATABASE.CARE.APP_FILES DIRECTORY = (ENABLE = TRUE);

-- Last statement: row counts (this is the result you'll see after Run All)
SELECT 'PATIENTS' AS t, COUNT(*) AS n FROM DEFAULT_DATABASE.CARE.PATIENTS
UNION ALL SELECT 'VISITS',      COUNT(*) FROM DEFAULT_DATABASE.CARE.VISITS
UNION ALL SELECT 'CONDITIONS',  COUNT(*) FROM DEFAULT_DATABASE.CARE.CONDITIONS
UNION ALL SELECT 'MEDICATIONS', COUNT(*) FROM DEFAULT_DATABASE.CARE.MEDICATIONS
UNION ALL SELECT 'BILLS',       COUNT(*) FROM DEFAULT_DATABASE.CARE.BILLS
UNION ALL SELECT 'BILLS_HIGH',  COUNT_IF(is_unusually_high) FROM DEFAULT_DATABASE.CARE.BILLS
UNION ALL SELECT 'DOCUMENTS',   COUNT(*) FROM DEFAULT_DATABASE.CARE.DOCUMENTS
UNION ALL SELECT 'TRIALS',      COUNT(*) FROM DEFAULT_DATABASE.CARE.TRIALS;
