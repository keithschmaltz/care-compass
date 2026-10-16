"""
Care Compass: helps a family caregiver understand a parent's health records and bills.
Streamlit in Snowflake, warehouse runtime. Reads DEFAULT_DATABASE.CARE (built by 11_care_compass_build.sql).
Care Compass explains records; it never gives medical advice.
"""
import html
import json
import re
from datetime import timedelta

import pandas as pd
import streamlit as st
from snowflake.snowpark.context import get_active_session

session = get_active_session()

DB = "DEFAULT_DATABASE.CARE"
DEMO_PATIENT = 30026  # Joseph Thomas, 78: 8 ongoing conditions, 8 medications, a high bill, a document
LLM_OPTIONS = ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-4-5", "llama3.1-70b"]

st.set_page_config(page_title="Care Compass", layout="wide")


# ---------------------------------------------------------------- compatibility (older Streamlit)
def show_table(df: pd.DataFrame):
    try:
        st.dataframe(df, hide_index=True, use_container_width=True)
    except TypeError:
        st.dataframe(df.reset_index(drop=True), use_container_width=True)


def rerun():
    (getattr(st, "rerun", None) or st.experimental_rerun)()


# ---------------------------------------------------------------- styling
CSS = """
<style>
:root { --teal:#0F766E; --amber:#B45309; --red:#B91C1C; }
.hero {border-radius:14px; padding:1.3rem 1.5rem; margin:.2rem 0 1rem 0;
       background:linear-gradient(135deg, rgba(15,118,110,.14), rgba(15,118,110,.04));
       border:1px solid rgba(15,118,110,.35);}
.hero-kicker {text-transform:uppercase; letter-spacing:.08em; font-size:.75rem; opacity:.7;}
.hero-name {font-size:2rem; font-weight:700; line-height:1.15; margin:.15rem 0 .25rem 0;}
.hero-sub {opacity:.8;}
.note {border-radius:10px; padding:.7rem 1rem; margin:.3rem 0 1rem 0; font-size:.88rem;
       background:rgba(128,128,128,.10); border:1px solid rgba(128,128,128,.25);}
.todo {display:flex; gap:.7rem; align-items:flex-start; padding:.65rem .9rem; margin:.4rem 0;
       border-radius:10px; border:1px solid rgba(128,128,128,.25);}
.dot {flex:0 0 .7rem; height:.7rem; border-radius:50%; margin-top:.35rem;}
.dot-red {background:var(--red);} .dot-amber {background:var(--amber);} .dot-teal {background:var(--teal);}
.todo-title {font-weight:700;}
.todo-body {font-size:.9rem; opacity:.85;}
.card {border-radius:12px; padding:1rem 1.1rem; margin:.4rem 0 .8rem 0; border:1px solid rgba(128,128,128,.28);}
.card-title {font-size:1.1rem; font-weight:700;}
.card-sub {font-size:.85rem; opacity:.7; margin-bottom:.35rem;}
.pill {display:inline-block; padding:.1rem .55rem; border-radius:999px; font-size:.75rem; font-weight:700;
       margin-right:.35rem;}
.pill-red {background:rgba(185,28,28,.14); color:var(--red);}
.pill-amber {background:rgba(180,83,9,.14); color:var(--amber);}
.pill-teal {background:rgba(15,118,110,.14); color:var(--teal);}
.ai-head {font-weight:700; color:var(--teal); margin:.8rem 0 .2rem 0;}
</style>
"""

e = html.escape


def html_block(s: str):
    st.markdown(s, unsafe_allow_html=True)


def pill(text: str, kind: str = "teal") -> str:
    return f'<span class="pill pill-{kind}">{e(text)}</span>'


def money(x) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "—"


def times(x) -> str:
    try:
        return f"{float(x):.1f}×"
    except (TypeError, ValueError):
        return "—"


def as_int(x) -> int:
    try:
        return int(x) if pd.notna(x) else 0
    except (TypeError, ValueError):
        return 0


def txt(x) -> str:
    """Text or empty string; database NULLs arrive as None or NaN."""
    return "" if x is None or (isinstance(x, float) and pd.isna(x)) else str(x)


def label(x) -> str:
    """OFFICE_VISIT -> Office visit, ER -> ER."""
    t = txt(x).replace("_", " ").strip()
    return t.upper() if len(t) <= 3 else t.capitalize()


GENDER = {"M": "Male", "F": "Female", "X": "Nonbinary"}
HE = {"Dad", "Grandpa", "Husband"}


def fmt_date(d) -> str:
    try:
        return pd.to_datetime(d).strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return str(d)


# ---------------------------------------------------------------- data
def q(sql: str, params=None) -> pd.DataFrame:
    df = session.sql(sql, params=params).to_pandas()
    df.columns = [c.lower() for c in df.columns]
    return df


@st.cache_data(ttl=3600, show_spinner=False)
def load_patients() -> pd.DataFrame:
    return q(f"""
        SELECT patient_id, full_name, first_name, age, gender, primary_insurance, state_code,
               chronic_conditions, encounters, medications, documents
        FROM {DB}.PATIENTS ORDER BY richness DESC, patient_id""")


@st.cache_data(ttl=3600, show_spinner=False)
def load_as_of():
    """The records' 'today': the latest visit in the data (synthetic data has no real now)."""
    return pd.to_datetime(q(f"SELECT MAX(encounter_date) AS d FROM {DB}.VISITS")["d"].iloc[0])


@st.cache_data(ttl=3600, show_spinner="Opening the record…")
def load_record(pid: int) -> dict:
    p = [pid]
    rec = dict(
        conditions=q(f"""
            SELECT icd10_code, diagnosis_description, category, is_chronic,
                   MIN(diagnosis_date) AS first_noted, MAX(diagnosis_date) AS last_noted,
                   COUNT(*) AS times_recorded
            FROM {DB}.CONDITIONS WHERE patient_id = ?
            GROUP BY ALL ORDER BY is_chronic DESC, last_noted DESC""", p),
        meds=q(f"""
            SELECT * FROM {DB}.MEDICATIONS WHERE patient_id = ?
            QUALIFY ROW_NUMBER() OVER (PARTITION BY medication_id ORDER BY prescribed_date DESC) = 1
            ORDER BY prescribed_date DESC""", p),
        visits=q(f"""
            SELECT encounter_id, encounter_date, encounter_type, length_of_stay_days, visit_status,
                   chief_complaint, clinical_notes, provider_name, specialty, facility_name
            FROM {DB}.VISITS WHERE patient_id = ? ORDER BY encounter_date DESC""", p),
        bills=q(f"SELECT * FROM {DB}.BILLS WHERE patient_id = ? ORDER BY service_date DESC", p),
        vitals=q(f"""
            SELECT encounter_date,
                   vitals:bp_systolic::FLOAT    AS systolic,
                   vitals:bp_diastolic::FLOAT   AS diastolic,
                   vitals:heart_rate::FLOAT     AS heart_rate,
                   vitals:weight_lbs::FLOAT     AS weight_lbs,
                   vitals:o2_saturation::FLOAT  AS oxygen,
                   vitals:temp_f::FLOAT         AS temp_f
            FROM {DB}.VISITS WHERE patient_id = ? AND vitals IS NOT NULL
            ORDER BY encounter_date""", p),
        docs=q(f"""
            SELECT document_id, document_type, file_name, page_count, uploaded_date, ocr_extracted_text
            FROM {DB}.DOCUMENTS WHERE patient_id = ? ORDER BY uploaded_date DESC""", p),
    )
    for name, cols in [("meds", ["prescribed_date", "supply_ends"]), ("visits", ["encounter_date"]),
                       ("bills", ["service_date"]), ("docs", ["uploaded_date"]), ("vitals", ["encounter_date"]),
                       ("conditions", ["first_noted", "last_noted"])]:
        for col in cols:
            rec[name][col] = pd.to_datetime(rec[name][col], errors="coerce")
    return rec


@st.cache_data(ttl=3600, show_spinner=False)
def load_trials() -> pd.DataFrame:
    return q(f"""
        SELECT trial_id, nct_number, trial_name, phase, trial_status, therapeutic_area,
               start_date, end_date, target_enrollment, enrolled, site_name, site_city, site_state,
               sponsor_name, protocol_metadata::STRING AS protocol
        FROM {DB}.TRIALS""")


# Condition category (CLINICAL.DIAGNOSES) -> clinical-trial therapeutic areas (RESEARCH.CLINICAL_TRIALS)
TRIAL_AREAS = {
    "Cardiovascular": ["Cardiology"],
    "Endocrine": ["Endocrinology"],
    "Metabolic": ["Endocrinology"],
    "Neurological": ["Neurology"],
    "Respiratory": ["Pulmonology"],
    "Musculoskeletal": ["Surgery"],
}


# ---------------------------------------------------------------- AI
GUARDRAILS = """You help a family caregiver understand their parent's health records and bills.
Rules: write plainly and warmly, without gushing, at the detail level described below. Use ONLY the record
provided; if something isn't in it, say so. Never diagnose, never say whether a treatment is right,
and never suggest starting, stopping or changing a medication; for anything like that, suggest asking
the care team. Refer to the patient by name or as the caregiver's parent, as instructed."""


# Set from the sidebar: how the caregiver wants things explained. Every AI answer follows it.
STYLE = ""
LEVELS = {
    "New to all of this": "The caregiver has no medical background: avoid jargon entirely and explain every "
                          "medical word in everyday terms.",
    "Some medical background": "The caregiver has some medical background (for example a nurse or aide): you may use "
                               "standard medical terms and give a little more clinical detail.",
}
LANGUAGES = ["English", "Spanish", "Arabic", "Polish", "Chinese (Simplified)"]


def complete(prompt: str, model: str) -> str:
    # COMPLETE needs the model name as a literal; it only ever comes from the fixed list.
    if model not in LLM_OPTIONS:
        raise ValueError(f"Unknown model: {model}")
    return session.sql(
        f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{model}', ?) AS r", params=[prompt]
    ).collect()[0]["R"]


def ai(key: str, prompt: str, label: str, button_text: str, model: str, kind: str = "markdown"):
    """A button that runs Cortex once and keeps the answer on screen until the patient changes."""
    store = st.session_state.setdefault("ai", {})
    key = f"{key}|{STYLE}"  # a new answer when the language or detail level changes
    if st.button(button_text, key=f"btn-{key}"):
        with st.spinner("Reading the record…"):
            store[key] = complete(f"{GUARDRAILS}\n{STYLE}\n\n{prompt}", model)
    if key in store:
        html_block(f'<div class="ai-head">{e(label)}</div>')
        if kind == "letter":
            st.text_area("Edit before sending", store[key], height=320, key=f"txt-{key}")
        else:
            st.markdown(store[key])


# ---------------------------------------------------------------- record helpers
def condition_names(conds: pd.DataFrame) -> dict:
    return {r.icd10_code: r.diagnosis_description for r in conds.itertuples()}


def med_purpose(row, conds: pd.DataFrame) -> str:
    """What this medication is for, matched to the patient's own conditions where possible."""
    treats = [t for t in re.split(r"[\s,;|/]+", str(row.treats_icd10 or "")) if t]
    for code, desc in condition_names(conds).items():
        if any(str(code).upper().startswith(t.upper()[:3]) for t in treats):
            return desc
    return str(row.drug_class or "See prescriber")


def current_meds(meds: pd.DataFrame, as_of) -> pd.DataFrame:
    """Most recent prescription per medication whose supply ended no more than 30 days before 'today'."""
    live = meds[meds.rx_status.astype(str).str.upper() != "CANCELLED"]
    return live[live.supply_ends >= as_of - timedelta(days=30)]


def med_flag(row, as_of):
    days = (row.supply_ends - as_of).days
    refills = as_int(row.refills_remaining)
    if days < 0 and refills == 0:
        return "red", "out of supply, no refills left"
    if days <= 14 and refills == 0:
        return "red", f"runs out in {days} days, no refills left"
    if days < 0:
        return "amber", f"supply ended {-days} days ago; {refills} refill(s) left"
    if days <= 14:
        return "amber", f"refill due in {days} days"
    return "teal", f"{refills} refill(s) left"


def with_insurance(status) -> bool:
    return txt(status).upper() in ("SUBMITTED", "PENDING")


def is_denied(status) -> bool:
    return "DENI" in str(status).upper() or "REJECT" in str(status).upper()


def to_dos(rec: dict, as_of, rel: str) -> list:
    items = []
    for row in current_meds(rec["meds"], as_of).itertuples():
        kind, text = med_flag(row, as_of)
        if kind != "teal":
            advice = ("Ask the pharmacy to refill it." if as_int(row.refills_remaining) > 0
                      else f"Call {row.prescriber_name}'s office for a new prescription.")
            items.append((kind, f"{row.medication_name}: {text}", advice))
    recent_rx = rec["meds"][rec["meds"].prescribed_date >= as_of - timedelta(days=45)]
    for row in recent_rx.itertuples():
        note = txt(row.pharmacist_notes)
        if re.search(r"partial fill|pickup|pick up|substitut|prior auth|call|contact", note, re.I):
            items.append(("amber", f"Pharmacy note about {row.medication_name}", note))
    for row in rec["bills"][rec["bills"].is_unusually_high == True].head(5).itertuples():  # noqa: E712
        items.append(("amber", f"A {money(row.billed_amount)} bill looks high",
                      f"{label(row.encounter_type) or 'Visit'} on "
                      f"{fmt_date(row.service_date)} at {row.facility_name}: {times(row.vs_typical)} the usual "
                      f"charge of {money(row.typical_billed)}. See the Bills tab."))
    for row in rec["bills"][rec["bills"].claim_status.map(is_denied)].head(3).itertuples():
        items.append(("red", f"Insurance denied a {money(row.billed_amount)} claim",
                      f"From {fmt_date(row.service_date)}. Denials can often be appealed; see the Bills tab."))
    waiting = rec["bills"][rec["bills"].claim_status.map(with_insurance)]
    if not waiting.empty:
        items.append(("teal", f"{len(waiting)} bill(s) are still with insurance",
                      f"{money(waiting.billed_amount.sum())} charged, not yet processed. "
                      "Hold off paying these until the insurer's statement arrives."))
    recent_docs = rec["docs"][rec["docs"].uploaded_date >= as_of - timedelta(days=60)]
    for row in recent_docs.head(3).itertuples():
        items.append(("teal", f"New document: {label(row.document_type)}",
                      f"Added {fmt_date(row.uploaded_date)}. See the Documents tab for a simple summary."))
    return items


def record_context(pat, rec: dict, as_of, rel: str, max_visits: int = 8) -> str:
    """A compact, plain-text version of the record for Cortex."""
    conds = rec["conditions"]
    meds = current_meds(rec["meds"], as_of)
    visits = rec["visits"].head(max_visits)
    bills = rec["bills"]
    lines = [
        f"PATIENT: {pat.full_name}, age {pat.age}, insurance {pat.primary_insurance}. "
        f"The caregiver calls this parent '{rel}'; refer to them as {rel} and use "
        f"{'he/him' if rel in HE else 'she/her'}. Records current as of {fmt_date(as_of)}.",
        "CONDITIONS:",
        *[f"- {r.diagnosis_description} ({r.icd10_code}), {'ongoing' if r.is_chronic else 'short-term'}, "
          f"last noted {fmt_date(r.last_noted)}" for r in conds.itertuples()],
        "CURRENT MEDICATIONS:",
        *[f"- {r.medication_name} {r.dosage} {r.frequency}, for {med_purpose(r, conds)}, prescribed by "
          f"{r.prescriber_name}; supply ends {fmt_date(r.supply_ends)}; refills left {as_int(r.refills_remaining)}"
          + (f"; pharmacist note: {txt(r.pharmacist_notes)[:160]}" if txt(r.pharmacist_notes) else "")
          for r in meds.itertuples()],
        f"RECENT VISITS (latest {len(visits)}):",
        *[f"- {fmt_date(r.encounter_date)} {label(r.encounter_type)} with {r.provider_name} ({r.specialty}) at "
          f"{r.facility_name}. Reason: {r.chief_complaint}. Notes: {str(r.clinical_notes)[:220]}"
          for r in visits.itertuples()],
        "RECENT VITALS: " + "; ".join(
            f"{fmt_date(r.encounter_date)}: BP {as_int(r.systolic)}/{as_int(r.diastolic)}, HR {as_int(r.heart_rate)}, "
            f"weight {as_int(r.weight_lbs)} lb, oxygen {as_int(r.oxygen)}%, temp {r.temp_f}F"
            for r in rec["vitals"].tail(3).itertuples()),
        f"BILLS: {len(bills)} bills, {money(bills.billed_amount.sum())} charged, "
        f"{money(bills.insurance_paid.sum())} paid by insurance, {money(bills.patient_responsibility.sum())} "
        f"owed by the patient. {int((bills.is_unusually_high == True).sum())} flagged as unusually high.",  # noqa: E712
        "DOCUMENTS: " + "; ".join(f"{label(r.document_type)} ({fmt_date(r.uploaded_date)})" for r in rec["docs"].itertuples()),
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- page
html_block(CSS)
patients = load_patients()
as_of = load_as_of()

with st.sidebar:
    st.header("Care Compass")
    caregiver = st.text_input("Your name", "Maria")
    rel = st.selectbox("They are your…", ["Dad", "Mom", "Grandpa", "Grandma", "Husband", "Wife"])
    ids = patients.patient_id.tolist()
    pid = st.selectbox(
        "Whose care are you managing?", ids, index=ids.index(DEMO_PATIENT) if DEMO_PATIENT in ids else 0,
        format_func=lambda i: f"{patients.set_index('patient_id').loc[i, 'full_name']}, "
                              f"{patients.set_index('patient_id').loc[i, 'age']}")
    st.markdown("**How should Care Compass explain things?**")
    level = st.radio("Your background", list(LEVELS))
    language = st.selectbox("Language for explanations", LANGUAGES)
    llm = st.selectbox("AI model", LLM_OPTIONS)
    st.caption("All data is synthetic, from the hackathon's healthcare dataset.")
    st.caption(f"Streamlit {st.__version__}")

STYLE = (f"{LEVELS[level]} Write every explanation in {language}"
         + ("." if language == "English" else ", but keep medication names, doctor names and codes as written.")
         )

PLAIN = "plain English" if language == "English" else language  # used in button labels and headings

# Answers belong to one patient: clear them when the patient changes.
if st.session_state.get("pid") != pid:
    st.session_state.pid = pid
    st.session_state.ai = {}
    st.session_state.chat = []

pat = patients.set_index("patient_id").loc[pid]
rec = load_record(int(pid))
meds_now = current_meds(rec["meds"], as_of)
ongoing = rec["conditions"][rec["conditions"].is_chronic == True]  # noqa: E712
last_visit = rec["visits"].encounter_date.max()
year_bills = rec["bills"][rec["bills"].service_date >= as_of - timedelta(days=365)]
owed = year_bills.patient_responsibility.sum()

html_block(
    f'<div class="hero"><div class="hero-kicker">Hi {e(caregiver)}, here is how {e(rel)} is doing</div>'
    f'<div class="hero-name">{e(pat.full_name)}</div>'
    f'<div class="hero-sub">{int(pat.age)} · {e(GENDER.get(txt(pat.gender), txt(pat.gender)))} · {e(str(pat.primary_insurance))} · '
    f'records as of {fmt_date(as_of)}</div></div>')

m1, m2, m3, m4 = st.columns(4)
m1.metric("Ongoing conditions", len(ongoing))
m2.metric("Current medications", len(meds_now))
m3.metric("Last visit", fmt_date(last_visit) if pd.notna(last_visit) else "—")
m4.metric(f"{rel}'s share of bills, last 12 months", money(owed))

html_block(f'<div class="note">Care Compass explains records in {e(PLAIN)}. It is not medical advice: '
           f'check with {e(rel)}\'s care team before changing anything.</div>')

ctx = record_context(pat, rec, as_of, rel)
tabs = st.tabs(["Overview", "Medications", "Visits", "Bills", "Documents", "Clinical trials", f"Ask about {rel}"])

# ---------------- Overview
with tabs[0]:
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Needs your attention")
        items = to_dos(rec, as_of, rel)
        if not items:
            st.write("Nothing urgent right now.")
        for kind, title, body in items:
            html_block(f'<div class="todo"><div class="dot dot-{kind}"></div><div>'
                       f'<div class="todo-title">{e(title)}</div><div class="todo-body">{e(body)}</div></div></div>')

        v = rec["vitals"]
        if not v.empty:
            st.subheader("Blood pressure and weight over time")
            last = v.iloc[-1]
            v1, v2, v3 = st.columns(3)
            v1.metric("Latest blood pressure", f"{as_int(last.systolic)}/{as_int(last.diastolic)}")
            v2.metric("Latest weight", f"{as_int(last.weight_lbs)} lb",
                      f"{as_int(last.weight_lbs) - as_int(v.iloc[0].weight_lbs):+d} lb since {fmt_date(v.iloc[0].encounter_date)}",
                      delta_color="off")
            v3.metric("Latest oxygen level", f"{as_int(last.oxygen)}%")
            chart = v.set_index("encounter_date")
            st.line_chart(chart[["systolic", "diastolic"]].rename(
                columns={"systolic": "Top number (systolic)", "diastolic": "Bottom number (diastolic)"}))
            st.line_chart(chart[["weight_lbs"]].rename(columns={"weight_lbs": "Weight (lb)"}))
    with right:
        st.subheader(f"{rel}'s health, explained")
        ai("summary", f"""{ctx}

Write a short explanation for {caregiver} of how {rel} is doing, in markdown:
- One opening sentence that sums it up.
- Each ongoing condition: its everyday name, then one sentence on what it means day to day.
- "What to keep an eye on": two or three practical things drawn from the record.
Under 220 words.""", f"{rel}'s health in {PLAIN}", f"Explain it in {PLAIN}", llm)

        ai("prep", f"""{ctx}

{caregiver} is going with {rel} to the next appointment. Write 5 to 7 specific questions to ask, in markdown,
grouped under short headings (for example Medications, Recent visits, Bills). Each question must refer to
something specific in the record. End with one line suggesting she bring the medication list.""",
           "Questions for the next appointment", "Prepare for the next appointment", llm)

# ---------------- Medications
with tabs[1]:
    st.subheader("Current medications")
    st.caption("Based on the most recent prescription for each medication.")
    if meds_now.empty:
        st.write("No current prescriptions in the record.")
    for row in meds_now.itertuples():
        kind, text = med_flag(row, as_of)
        brand = f" ({txt(row.brand_name)})" if txt(row.brand_name) and txt(row.brand_name) != txt(row.medication_name) else ""
        note = (f'<div class="card-sub">Pharmacist note: {e(txt(row.pharmacist_notes))}</div>'
                if txt(row.pharmacist_notes) else "")
        html_block(
            f'<div class="card"><div class="card-title">{e(str(row.medication_name))}{e(brand)}</div>'
            f'<div class="card-sub">For {e(med_purpose(row, rec["conditions"]))} · {e(str(row.drug_class))}</div>'
            f'<div>{e(str(row.dosage))}, {e(str(row.frequency))} · prescribed by {e(str(row.prescriber_name))} '
            f'({e(str(row.prescriber_specialty))})</div>'
            f'<div style="margin-top:.4rem">{pill(text[:1].upper() + text[1:], kind)}{pill("Supply ends " + fmt_date(row.supply_ends), "teal")}'
            f'{pill("Waiting at the pharmacy", "amber") if txt(row.rx_status).upper() == "PENDING" else ""}</div>'
            f'{note}</div>')
    past = rec["meds"][~rec["meds"].prescription_id.isin(meds_now.prescription_id)]
    if not past.empty:
        with st.expander(f"Past medications ({len(past)})"):
            show_table(past[["medication_name", "dosage", "frequency", "prescribed_date", "prescriber_name"]]
                       .assign(prescribed_date=past.prescribed_date.map(fmt_date)))

# ---------------- Visits
with tabs[2]:
    st.subheader("Visits, newest first")
    for row in rec["visits"].head(15).itertuples():
        title = f"{fmt_date(row.encounter_date)} · {label(row.encounter_type)} · {txt(row.provider_name)}"
        with st.expander(title):
            st.markdown(f"**Why:** {row.chief_complaint}  \n**Where:** {row.facility_name}  \n"
                        f"**Doctor:** {row.provider_name}, {row.specialty}")
            if txt(row.clinical_notes):
                st.markdown(f"**Doctor's notes:** {row.clinical_notes}")
            ai(f"visit-{row.encounter_id}", f"""Visit record:
Date {fmt_date(row.encounter_date)}; type {row.encounter_type}; doctor {row.provider_name} ({row.specialty});
reason: {row.chief_complaint}; notes: {row.clinical_notes}

Patient's conditions: {", ".join(rec["conditions"].diagnosis_description.unique()[:10])}

Explain this visit to {caregiver} in 3 or 4 plain sentences: why {rel} went, what the notes say in everyday
words, and any follow-up the notes mention. Define any medical term you use.""",
               "This visit, explained", "Explain this visit", llm)

# ---------------- Bills
with tabs[3]:
    bills = rec["bills"]
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Charged", money(bills.billed_amount.sum()))
    b2.metric("Insurance paid", money(bills.insurance_paid.sum()))
    b3.metric(f"{rel}'s share", money(bills.patient_responsibility.sum()))
    b4.metric("Bills that look high", int((bills.is_unusually_high == True).sum()))  # noqa: E712

    flagged = bills[(bills.is_unusually_high == True) | bills.claim_status.map(is_denied)]  # noqa: E712
    st.subheader("Worth a closer look")
    if flagged.empty:
        st.write("No bills stand out.")
    for row in flagged.head(8).itertuples():
        reasons = []
        if row.is_unusually_high:
            reasons.append(pill(f"{times(row.vs_typical)} the usual charge", "amber"))
        if is_denied(row.claim_status):
            reasons.append(pill("Denied by insurance", "red"))
        if with_insurance(row.claim_status):
            reasons.append(pill("Still with insurance", "teal"))
        html_block(
            f'<div class="card"><div class="card-title">{money(row.billed_amount)} · '
            f'{e(label(row.encounter_type))} on {fmt_date(row.service_date)}</div>'
            f'<div class="card-sub">{e(str(row.facility_name))} · {e(str(row.provider_name))} · '
            f'procedure code {e(str(row.primary_cpt_code))} · {e(str(row.payer_name))}</div>'
            f'<div>{"".join(reasons)}</div>'
            f'<div style="margin-top:.4rem">Usual charge for this kind of visit: {money(row.typical_billed)}. '
            f'Insurance paid {money(row.insurance_paid)}; {e(rel)}\'s share {money(row.patient_responsibility)}.</div></div>')
        ai(f"bill-{row.claim_id}", f"""Bill details: claim {row.claim_number}; service date {fmt_date(row.service_date)};
{row.encounter_type} visit at {row.facility_name} with {row.provider_name}; procedure code {row.primary_cpt_code};
payer {row.payer_name}; charged {money(row.billed_amount)}; usual charge for this visit type and code
{money(row.typical_billed)} ({times(row.vs_typical)}); allowed {money(row.allowed_amount)}; insurance paid
{money(row.insurance_paid)}; patient owes {money(row.patient_responsibility)}; claim status {row.claim_status}.

Write a short, polite letter from {caregiver}, on behalf of {pat.full_name}, to the billing office of
{row.facility_name}. Ask for an itemized bill, ask them to confirm the procedure code, and ask them to
review the charge against the usual amount{" and explain the denial and how to appeal it" if is_denied(row.claim_status) else ""}.
Include the claim number and service date. Leave [brackets] for the address and phone number. Plain text, no markdown.
Write the letter itself in English, since it goes to a US billing office, whatever language is set above.""",
           "Draft letter", "Draft a letter about this bill", llm, kind="letter")

    with st.expander(f"All bills ({len(bills)})"):
        show_table(bills.assign(service_date=bills.service_date.map(fmt_date),
                                billed=bills.billed_amount.map(money),
                                insurance=bills.insurance_paid.map(money),
                                owed=bills.patient_responsibility.map(money))
                   [["service_date", "encounter_type", "facility_name", "billed", "insurance", "owed", "claim_status"]])

# ---------------- Documents
with tabs[4]:
    docs = rec["docs"]
    st.subheader("Documents")
    if docs.empty:
        st.write("No scanned documents in the record.")
    for row in docs.itertuples():
        with st.expander(f"{label(row.document_type)} · {fmt_date(row.uploaded_date)} · {as_int(row.page_count)} page(s)"):
            ai(f"doc-{row.document_id}", f"""Scanned document ({row.document_type}), text extracted by OCR:
{str(row.ocr_extracted_text)[:6000]}

Summarize this for {caregiver} in plain language, in markdown: what kind of document it is, the 3 most important
points, any numbers that are flagged as out of range (explain what the number measures), and any follow-up
it mentions. Under 180 words.""", "This document, explained", f"Summarize in {PLAIN}", llm)
            st.caption("Original text")
            st.text(str(row.ocr_extracted_text)[:3000])

# ---------------- Clinical trials
with tabs[5]:
    st.subheader("Clinical trials that relate to these conditions")
    st.caption("A starting point for a conversation with the care team, not an eligibility decision.")
    trials = load_trials()
    cats = rec["conditions"].category.dropna().unique().tolist()
    areas = sorted({a for c in cats for a in TRIAL_AREAS.get(c, [])})
    matches = trials[trials.therapeutic_area.isin(areas)]
    shown = matches[matches.trial_status.astype(str).str.upper() == "RECRUITING"]
    if areas:
        st.caption("Matched on: " + ", ".join(f"{c} → {', '.join(TRIAL_AREAS[c])}" for c in cats if c in TRIAL_AREAS))
    if shown.empty:
        st.write("No recruiting trials in the database relate to these conditions.")
    else:
        st.write(f"{len(shown)} recruiting trial(s) relate to {rel}'s conditions. Showing up to 6.")
    for row in shown.head(6).itertuples():
        html_block(
            f'<div class="card"><div class="card-title">{e(str(row.trial_name))}</div>'
            f'<div class="card-sub">{e(str(row.therapeutic_area))} · {e(str(row.phase))} · {e(str(row.nct_number))}</div>'
            f'<div>{pill(str(row.trial_status).title(), "teal")} At {e(str(row.site_name))}, '
            f'{e(str(row.site_city))}, {e(str(row.site_state))} · sponsored by {e(str(row.sponsor_name))}</div></div>')
        ai(f"trial-{row.trial_id}", f"""{ctx}

Clinical trial: {row.trial_name} ({row.nct_number}), {row.phase}, {row.therapeutic_area}, status {row.trial_status},
site {row.site_name} in {row.site_city}, {row.site_state}. Protocol details (JSON): {txt(row.protocol)[:2500]}

In markdown, for {caregiver}: in two sentences, what this trial is testing in everyday words; then which parts
of {rel}'s record seem relevant to it; then three questions to ask the care team before considering it.
Do not say whether {rel} qualifies. Under 170 words.""",
           "What this trial would mean", f"What would this mean for {rel}?", llm)

# ---------------- Ask
with tabs[6]:
    st.subheader(f"Ask about {rel}'s care")
    st.caption(f"Answers come only from the record above. Try: “What is each of {'his' if rel in HE else 'her'} medications for?”")
    chat = st.session_state.setdefault("chat", [])
    for who, text in chat:
        st.markdown(f"**{e(caregiver) if who == 'user' else 'Care Compass'}:** {text}")
    with st.form("ask", clear_on_submit=True):
        question = st.text_input("Your question")
        sent = st.form_submit_button("Ask")
    if sent and question:
        history = "\n".join(f"{'Caregiver' if w == 'user' else 'You'}: {t}" for w, t in chat[-6:])
        with st.spinner("Reading the record…"):
            answer = complete(f"""{GUARDRAILS}
{STYLE}

{ctx}

Conversation so far:
{history}

Caregiver ({caregiver}) asks: {question}
Answer in under 150 words, in markdown.""", llm)
        chat.extend([("user", question), ("assistant", answer)])
        rerun()
    if chat and st.button("Clear conversation"):
        st.session_state.chat = []
        rerun()
