"""Web UI: worklist query page and the exam workflow page.

Server-rendered Jinja2 templates with a little vanilla JS (activity log
polling only). One student, one browser, one exam at a time: the current
exam lives in process memory, which keeps the SWF state machine visible
and easy to reason about. Restarting the app clears it.
"""

import logging
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

import config
import dicom_client
import restamp
import samples
import weblog
from dicom_client import DicomClientError, wl_sps

logger = logging.getLogger("modality.web")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="DICOM Lab Modality Emulator")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# In-memory state (single student, single exam)
# ---------------------------------------------------------------------------

# Exam status values, in SWF order. STORED means at least one attempt ran;
# store_ok says whether every instance made it.
EXAM_STATUSES = (
    "SELECTED",       # order + sample picked, no MPPS yet
    "IN PROGRESS",    # MPPS N-CREATE accepted
    "ACQUIRED",       # copies re-stamped in WORK_DIR
    "STORED",         # C-STORE attempted
    "COMPLETED",      # MPPS N-SET COMPLETED accepted
    "DISCONTINUED",   # MPPS N-SET DISCONTINUED accepted
)


class Exam:
    def __init__(self, wl_item, sample):
        self.wl_item = wl_item          # the selected worklist Dataset
        self.sample = sample            # SampleStudy
        self.status = "SELECTED"
        self.mpps_uid = None
        self.restamp_result = None      # RestampResult
        self.store_results = None       # list[StoreResult]
        self.error = None               # last step error shown in a banner

    @property
    def store_ok(self):
        return bool(self.store_results) and all(r.success for r in self.store_results)

    @property
    def is_final(self):
        return self.status in ("COMPLETED", "DISCONTINUED")


class AppState:
    def __init__(self):
        self.worklist = []      # datasets from the last MWL C-FIND
        self.filters = {}       # the filter values that produced them
        self.query_ran = False
        self.echo = {}          # "of"/"pacs" -> result message
        self.exam = None


state = AppState()


def _wl_row(idx, ds):
    """Flatten one worklist dataset for the results table."""
    sps = wl_sps(ds)
    return {
        "idx": idx,
        "patient_name": str(ds.get("PatientName", "")),
        "mrn": str(ds.get("PatientID", "")),
        "birth_date": str(ds.get("PatientBirthDate", "")),
        "sex": str(ds.get("PatientSex", "")),
        "accession": str(ds.get("AccessionNumber", "")),
        "procedure": str(ds.get("RequestedProcedureDescription", "")),
        "modality": str(sps.get("Modality", "")),
        "sps_start": (
            f"{sps.get('ScheduledProcedureStepStartDate', '')} "
            f"{str(sps.get('ScheduledProcedureStepStartTime', ''))[:4]}"
        ).strip(),
        "station_aet": str(sps.get("ScheduledStationAETitle", "")),
        "study_uid": str(ds.get("StudyInstanceUID", "")),
        "referring": str(ds.get("ReferringPhysicianName", "")),
        "rp_id": str(ds.get("RequestedProcedureID", "")),
        "sps_id": str(sps.get("ScheduledProcedureStepID", "")),
    }


def _config_view():
    return {
        "mod_aet": config.MOD_AE_TITLE,
        "of": f"{config.OF_AE_TITLE} @ {config.OF_HOST}:{config.OF_PORT}",
        "pacs": f"{config.PACS_AE_TITLE} @ {config.PACS_HOST}:{config.PACS_PORT}",
        "sample_dir": config.SAMPLE_BASE_DIR,
        "work_dir": config.WORK_DIR,
    }


# ---------------------------------------------------------------------------
# Worklist page
# ---------------------------------------------------------------------------

@app.get("/")
def worklist_page(request: Request):
    rows = [_wl_row(i, ds) for i, ds in enumerate(state.worklist)]
    return templates.TemplateResponse(request, "worklist.html", {
        "cfg": _config_view(),
        "rows": rows,
        "filters": state.filters,
        "query_ran": state.query_ran,
        "echo": state.echo,
        "exam": state.exam,
        "today": date.today().strftime("%Y%m%d"),
    })


@app.post("/query")
def run_query(
    patient_name: str = Form(""),
    modality: str = Form(""),
    date_from: str = Form(""),
    date_to: str = Form(""),
    station_aet: str = Form(""),
    accession: str = Form(""),
):
    # DA matching: single value, or an open/closed range "lo-hi".
    date_from, date_to = date_from.strip(), date_to.strip()
    if date_from and date_to:
        date_key = date_from if date_from == date_to else f"{date_from}-{date_to}"
    elif date_from:
        date_key = date_from          # single-date exact match
    elif date_to:
        date_key = f"-{date_to}"      # open-ended: everything up to date_to
    else:
        date_key = ""

    filters = {
        "patient_name": patient_name.strip(),
        "modality": modality.strip().upper(),
        "date": date_key,
        "station_aet": station_aet.strip().upper(),
        "accession": accession.strip(),
    }
    state.filters = {**filters, "date_from": date_from, "date_to": date_to}
    state.echo.pop("query_error", None)
    try:
        state.worklist = dicom_client.mwl_query(filters)
        state.query_ran = True
    except DicomClientError as exc:
        state.worklist = []
        state.query_ran = False
        state.echo["query_error"] = str(exc)
    return RedirectResponse("/", status_code=303)


@app.post("/echo/{target}")
def run_echo(target: str):
    if target == "of":
        host, port, aet = config.OF_HOST, config.OF_PORT, config.OF_AE_TITLE
    else:
        target = "pacs"
        host, port, aet = config.PACS_HOST, config.PACS_PORT, config.PACS_AE_TITLE
    try:
        state.echo[target] = "OK: " + dicom_client.c_echo(host, port, aet)
    except DicomClientError as exc:
        state.echo[target] = f"FAILED: {exc}"
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Exam workflow
# ---------------------------------------------------------------------------

@app.get("/exam/select")
def select_order(item: int):
    """Student picked a worklist row. Refuse to drop an exam that has an
    open MPPS: it must be completed or discontinued first, exactly like a
    real modality console."""
    if state.exam and not state.exam.is_final and state.exam.status != "SELECTED":
        state.exam.error = (
            "This exam has an open MPPS. Complete or discontinue it before "
            "starting another one."
        )
        return RedirectResponse("/exam", status_code=303)
    if item < 0 or item >= len(state.worklist):
        return RedirectResponse("/", status_code=303)
    wl_item = state.worklist[item]
    state.exam = Exam(wl_item, sample=None)
    logger.info(
        "Order selected: %s / acc %s",
        wl_item.get("PatientName", "?"), wl_item.get("AccessionNumber", "?"),
    )
    return RedirectResponse("/exam", status_code=303)


@app.get("/exam")
def exam_page(request: Request):
    exam = state.exam
    if exam is None:
        return RedirectResponse("/", status_code=303)
    error, exam.error = exam.error, None  # show each error once

    row = _wl_row(0, exam.wl_item)
    sample_list = []
    if exam.status == "SELECTED":
        sample_list = samples.scan_samples(
            modality=row["modality"] if config.FILTER_SAMPLES_BY_MODALITY else None
        )
    return templates.TemplateResponse(request, "exam.html", {
        "cfg": _config_view(),
        "exam": exam,
        "order": row,
        "sample_list": sample_list,
        "filter_by_modality": config.FILTER_SAMPLES_BY_MODALITY,
        "error": error,
    })


@app.post("/exam/start")
def start_exam(sample_name: str = Form(...)):
    """Start Exam: bind the sample study, then MPPS N-CREATE (IN PROGRESS).
    This is the message that makes the order leave the worklist."""
    exam = state.exam
    if exam is None or exam.status != "SELECTED":
        return RedirectResponse("/exam", status_code=303)

    sample = samples.get_sample(sample_name)
    if sample is None:
        exam.error = f"Sample study {sample_name!r} was not found. Rescan and retry."
        return RedirectResponse("/exam", status_code=303)
    modality = str(wl_sps(exam.wl_item).get("Modality", ""))
    if config.FILTER_SAMPLES_BY_MODALITY and not sample.matches_modality(modality):
        exam.error = (
            f"Sample '{sample.name}' is {sample.modality_label} but the order "
            f"is scheduled for {modality}."
        )
        return RedirectResponse("/exam", status_code=303)
    exam.sample = sample

    try:
        exam.mpps_uid = dicom_client.mpps_create(exam.wl_item)
        exam.status = "IN PROGRESS"
    except DicomClientError as exc:
        exam.error = f"MPPS N-CREATE failed: {exc}"
    return RedirectResponse("/exam", status_code=303)


@app.post("/exam/acquire")
def acquire(request: Request):
    """Acquire: copy the sample into WORK_DIR and re-stamp the copies with
    the worklist identity. No network traffic; this is the modality's own
    image generation step."""
    exam = state.exam
    if exam is None or exam.status != "IN PROGRESS":
        return RedirectResponse("/exam", status_code=303)
    try:
        exam.restamp_result = restamp.restamp_study(exam.sample, exam.wl_item)
        exam.status = "ACQUIRED"
    except Exception as exc:
        logger.exception("Re-stamp failed")
        exam.error = f"Re-stamp failed: {exc}"
    return RedirectResponse("/exam", status_code=303)


@app.post("/exam/store")
def store(request: Request):
    """Send the re-stamped instances to the PACS. Can be retried if some
    instances fail (a retry re-sends everything; DCM4CHEE treats duplicate
    SOP Instance UIDs as replacements or ignores them)."""
    exam = state.exam
    if exam is None or exam.status not in ("ACQUIRED", "STORED"):
        return RedirectResponse("/exam", status_code=303)
    try:
        exam.store_results = dicom_client.store_files(
            exam.restamp_result.file_paths
        )
        exam.status = "STORED"
        if not exam.store_ok:
            failed = sum(1 for r in exam.store_results if not r.success)
            exam.error = (
                f"{failed} instance(s) failed to store. You can retry the send "
                "or discontinue the procedure step."
            )
    except DicomClientError as exc:
        exam.error = f"C-STORE failed: {exc}"
    return RedirectResponse("/exam", status_code=303)


@app.post("/exam/complete")
def complete(request: Request):
    """MPPS N-SET COMPLETED, referencing exactly the instances that stored
    successfully. The Order Filler flips the order to COMPLETED."""
    exam = state.exam
    if exam is None or exam.status != "STORED" or not exam.store_ok:
        return RedirectResponse("/exam", status_code=303)
    stored_uids = {r.sop_instance_uid for r in exam.store_results if r.success}
    try:
        dicom_client.mpps_complete(
            exam.mpps_uid, exam.restamp_result.performed_series(stored_uids)
        )
        exam.status = "COMPLETED"
    except DicomClientError as exc:
        exam.error = f"MPPS N-SET COMPLETED failed: {exc}"
    return RedirectResponse("/exam", status_code=303)


@app.post("/exam/discontinue")
def discontinue(request: Request):
    """MPPS N-SET DISCONTINUED: abandon the step at any point after Start.
    The Order Filler maps the order back to DISCONTINUED."""
    exam = state.exam
    if exam is None or exam.status not in ("IN PROGRESS", "ACQUIRED", "STORED"):
        return RedirectResponse("/exam", status_code=303)
    try:
        dicom_client.mpps_discontinue(exam.mpps_uid)
        exam.status = "DISCONTINUED"
    except DicomClientError as exc:
        exam.error = f"MPPS N-SET DISCONTINUED failed: {exc}"
    return RedirectResponse("/exam", status_code=303)


@app.post("/exam/discard")
def discard(request: Request):
    """Local cleanup only: forget the exam without sending any DICOM. Only
    allowed when no MPPS is open (SELECTED or a final state)."""
    exam = state.exam
    if exam and (exam.is_final or exam.status == "SELECTED"):
        logger.info("Exam discarded (status was %s)", exam.status)
        state.exam = None
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/exam", status_code=303)


# ---------------------------------------------------------------------------
# Activity log polling
# ---------------------------------------------------------------------------

@app.get("/api/log")
def api_log(after: int = 0):
    return weblog.get_lines(after_seq=after)
