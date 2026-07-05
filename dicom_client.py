"""DICOM SCU side of the Modality Emulator.

Implements everything this Acquisition Modality says on the wire:
  - C-ECHO         connectivity test to the Order Filler and to the PACS
  - MWL C-FIND     query the worklist on the Order Entry System
  - MPPS N-CREATE  procedure step started (IN PROGRESS)
  - MPPS N-SET     procedure step COMPLETED or DISCONTINUED
  - C-STORE        push the re-stamped instances to the PACS

Every association and its outcome is logged in plain language: students
watch this log side by side with Wireshark, so the log is a teaching
artifact, not debug output.
"""

import copy
import logging
from dataclasses import dataclass
from datetime import datetime

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE
from pynetdicom.sop_class import (
    ModalityPerformedProcedureStep,
    ModalityWorklistInformationFind,
    Verification,
)

import config

logger = logging.getLogger("modality.dicom")


class DicomClientError(Exception):
    """Raised when an association fails or a peer returns a failure status.
    The message is shown verbatim in the web UI."""


def _new_ae():
    ae = AE(ae_title=config.MOD_AE_TITLE)
    ae.connection_timeout = config.CONNECT_TIMEOUT
    ae.acse_timeout = config.ACSE_TIMEOUT
    ae.dimse_timeout = config.DIMSE_TIMEOUT
    return ae


def _associate(ae, host, port, called_aet, purpose):
    logger.info(
        "Association request: %s -> %s@%s:%s (%s)",
        config.MOD_AE_TITLE, called_aet, host, port, purpose,
    )
    assoc = ae.associate(host, port, ae_title=called_aet)
    if not assoc.is_established:
        logger.error(
            "Association REJECTED or failed: %s@%s:%s (check IP, port, AE title)",
            called_aet, host, port,
        )
        raise DicomClientError(
            f"Could not associate with {called_aet} at {host}:{port}. "
            "Check IP, port, and AE title, and that the peer is running."
        )
    logger.info("Association ESTABLISHED with %s", called_aet)
    return assoc


# ---------------------------------------------------------------------------
# C-ECHO
# ---------------------------------------------------------------------------

def c_echo(host, port, called_aet):
    """Verification SOP class ping. Returns a human-readable result string;
    raises DicomClientError on failure."""
    ae = _new_ae()
    ae.add_requested_context(Verification)
    assoc = _associate(ae, host, port, called_aet, "C-ECHO")
    try:
        status = assoc.send_c_echo()
        if status and status.Status == 0x0000:
            logger.info("C-ECHO to %s: SUCCESS", called_aet)
            return f"C-ECHO to {called_aet} at {host}:{port} succeeded"
        code = f"0x{status.Status:04X}" if status else "no response"
        logger.error("C-ECHO to %s FAILED: %s", called_aet, code)
        raise DicomClientError(f"C-ECHO to {called_aet} failed: {code}")
    finally:
        assoc.release()
        logger.info("Association RELEASED (%s)", called_aet)


# ---------------------------------------------------------------------------
# MWL C-FIND
# ---------------------------------------------------------------------------

def build_mwl_identifier(filters):
    """C-FIND identifier: matching keys from the student's filter form plus
    the full SWF return key set, so the response carries everything the
    re-stamp step needs (demographics, StudyInstanceUID, RP/SPS IDs,
    protocol codes)."""
    ds = Dataset()

    # Patient identification (return keys; PatientName may carry a wildcard)
    ds.PatientName = filters.get("patient_name") or "*"
    ds.PatientID = ""
    ds.PatientBirthDate = ""
    ds.PatientSex = ""

    # Imaging Service Request
    ds.AccessionNumber = filters.get("accession") or ""
    ds.ReferringPhysicianName = ""
    ds.RequestedProcedureDescription = ""

    # Requested Procedure
    ds.RequestedProcedureID = ""
    ds.StudyInstanceUID = ""
    ds.RequestedProcedureCodeSequence = []
    ds.RequestedProcedurePriority = ""

    # Scheduled Procedure Step: matching keys live inside the sequence item.
    sps = Dataset()
    sps.Modality = filters.get("modality") or ""
    sps.ScheduledStationAETitle = filters.get("station_aet") or ""
    sps.ScheduledProcedureStepStartDate = filters.get("date") or ""
    sps.ScheduledProcedureStepStartTime = ""
    sps.ScheduledPerformingPhysicianName = ""
    sps.ScheduledProcedureStepDescription = ""
    sps.ScheduledProtocolCodeSequence = []
    sps.ScheduledProcedureStepID = ""
    ds.ScheduledProcedureStepSequence = [sps]

    return ds


def _describe_filters(filters):
    parts = [f"{k}={v}" for k, v in filters.items() if v]
    return ", ".join(parts) if parts else "(no filters: universal match)"


def mwl_query(filters):
    """Run the MWL C-FIND. Returns the list of worklist item Datasets."""
    identifier = build_mwl_identifier(filters)
    logger.info("MWL C-FIND query keys: %s", _describe_filters(filters))

    ae = _new_ae()
    ae.add_requested_context(ModalityWorklistInformationFind)
    assoc = _associate(
        ae, config.OF_HOST, config.OF_PORT, config.OF_AE_TITLE, "MWL C-FIND"
    )
    results = []
    try:
        for status, item in assoc.send_c_find(
            identifier, ModalityWorklistInformationFind
        ):
            if status is None:
                raise DicomClientError("Connection lost during C-FIND")
            if status.Status in (0xFF00, 0xFF01):  # pending: one match
                results.append(item)
                sps = wl_sps(item)
                logger.info(
                    "  worklist item %d: %s / MRN %s / acc %s / %s %s @ %s %s",
                    len(results),
                    item.get("PatientName", "?"), item.get("PatientID", "?"),
                    item.get("AccessionNumber", "?"),
                    sps.get("Modality", "?"),
                    item.get("RequestedProcedureDescription", ""),
                    sps.get("ScheduledProcedureStepStartDate", "?"),
                    sps.get("ScheduledProcedureStepStartTime", ""),
                )
            elif status.Status == 0x0000:
                logger.info("MWL C-FIND complete: %d item(s)", len(results))
            else:
                raise DicomClientError(
                    f"C-FIND returned failure status 0x{status.Status:04X}"
                )
    finally:
        assoc.release()
        logger.info("Association RELEASED (%s)", config.OF_AE_TITLE)
    return results


def wl_sps(wl_item):
    """The (single) Scheduled Procedure Step item of a worklist dataset."""
    seq = wl_item.get("ScheduledProcedureStepSequence")
    return seq[0] if seq else Dataset()


# ---------------------------------------------------------------------------
# MPPS N-CREATE / N-SET
# ---------------------------------------------------------------------------

def build_mpps_create(wl_item, mpps_uid, pps_id, now):
    """MPPS N-CREATE attribute list, fully populated from the worklist item.

    The ScheduledStepAttributesSequence is what the Order Filler matches on
    (StudyInstanceUID first, then SPS ID, then Accession Number), so it
    carries every identifier the worklist gave us. This is the SWF link
    between the performed step and the order; nothing here is hand-typed.
    """
    sps = wl_sps(wl_item)
    ds = Dataset()
    ds.SOPClassUID = ModalityPerformedProcedureStep
    ds.SOPInstanceUID = mpps_uid

    # Link back to the scheduled step (Scheduled Step Attributes Sequence)
    ref = Dataset()
    ref.StudyInstanceUID = str(wl_item.get("StudyInstanceUID", ""))
    ref.ReferencedStudySequence = []
    ref.AccessionNumber = str(wl_item.get("AccessionNumber", ""))
    ref.RequestedProcedureID = str(wl_item.get("RequestedProcedureID", ""))
    ref.RequestedProcedureDescription = str(
        wl_item.get("RequestedProcedureDescription", "")
    )
    ref.ScheduledProcedureStepID = str(sps.get("ScheduledProcedureStepID", ""))
    ref.ScheduledProcedureStepDescription = str(
        sps.get("ScheduledProcedureStepDescription", "")
    )
    ref.ScheduledProtocolCodeSequence = copy.deepcopy(
        list(sps.get("ScheduledProtocolCodeSequence", []))
    )
    ds.ScheduledStepAttributesSequence = [ref]

    # Patient identity, straight from the worklist (never typed at the console)
    ds.PatientName = str(wl_item.get("PatientName", ""))
    ds.PatientID = str(wl_item.get("PatientID", ""))
    ds.PatientBirthDate = str(wl_item.get("PatientBirthDate", ""))
    ds.PatientSex = str(wl_item.get("PatientSex", ""))

    # Performed Procedure Step
    ds.PerformedProcedureStepID = pps_id
    ds.PerformedStationAETitle = config.MOD_AE_TITLE
    ds.PerformedStationName = ""
    ds.PerformedLocation = ""
    ds.PerformedProcedureStepStartDate = now.strftime("%Y%m%d")
    ds.PerformedProcedureStepStartTime = now.strftime("%H%M%S")
    ds.PerformedProcedureStepStatus = "IN PROGRESS"
    ds.PerformedProcedureStepDescription = str(
        sps.get("ScheduledProcedureStepDescription", "")
    )
    ds.PerformedProcedureTypeDescription = ""
    ds.PerformedProtocolCodeSequence = copy.deepcopy(
        list(sps.get("ScheduledProtocolCodeSequence", []))
    )
    # End date/time and series info are unknown at creation; sent zero-length
    # now and filled in by the final N-SET (PS3.4 F.7.2.1).
    ds.PerformedProcedureStepEndDate = ""
    ds.PerformedProcedureStepEndTime = ""
    ds.Modality = str(sps.get("Modality", ""))
    ds.StudyID = str(wl_item.get("RequestedProcedureID", ""))
    ds.PerformedSeriesSequence = []
    return ds


def mpps_create(wl_item):
    """Send MPPS N-CREATE (IN PROGRESS). Returns the MPPS SOP Instance UID
    that the later N-SET must reference."""
    now = datetime.now()
    mpps_uid = generate_uid(prefix=config.UID_ROOT)
    pps_id = f"PPS{now:%Y%m%d%H%M%S}"
    ds = build_mpps_create(wl_item, mpps_uid, pps_id, now)

    logger.info(
        "MPPS N-CREATE (IN PROGRESS): patient %s, acc %s, StudyInstanceUID %s, "
        "PPS ID %s, MPPS SOP Instance UID %s",
        ds.PatientName, wl_item.get("AccessionNumber", "?"),
        wl_item.get("StudyInstanceUID", "?"), pps_id, mpps_uid,
    )

    ae = _new_ae()
    ae.add_requested_context(ModalityPerformedProcedureStep)
    assoc = _associate(
        ae, config.OF_HOST, config.OF_PORT, config.OF_AE_TITLE, "MPPS N-CREATE"
    )
    try:
        status, attr_list = assoc.send_n_create(
            ds, ModalityPerformedProcedureStep, mpps_uid
        )
        if status is None or status.Status != 0x0000:
            code = f"0x{status.Status:04X}" if status else "no response"
            logger.error("MPPS N-CREATE FAILED: %s", code)
            raise DicomClientError(f"MPPS N-CREATE failed: status {code}")
        # The SCP may assign its own SOP Instance UID (PS3.7 10.1.5).
        assigned = getattr(attr_list, "SOPInstanceUID", None) if attr_list else None
        final_uid = str(assigned) if assigned else mpps_uid
        logger.info(
            "MPPS N-CREATE accepted; the order is now IN PROGRESS on the "
            "Order Filler and off the worklist. MPPS UID %s", final_uid,
        )
        return final_uid
    finally:
        assoc.release()
        logger.info("Association RELEASED (%s)", config.OF_AE_TITLE)


def _mpps_set(mpps_uid, mod_list, label):
    ae = _new_ae()
    ae.add_requested_context(ModalityPerformedProcedureStep)
    assoc = _associate(
        ae, config.OF_HOST, config.OF_PORT, config.OF_AE_TITLE, f"MPPS N-SET {label}"
    )
    try:
        status, _ = assoc.send_n_set(
            mod_list, ModalityPerformedProcedureStep, mpps_uid
        )
        if status is None or status.Status != 0x0000:
            code = f"0x{status.Status:04X}" if status else "no response"
            logger.error("MPPS N-SET (%s) FAILED: %s", label, code)
            raise DicomClientError(f"MPPS N-SET {label} failed: status {code}")
        logger.info("MPPS N-SET accepted: procedure step is now %s", label)
    finally:
        assoc.release()
        logger.info("Association RELEASED (%s)", config.OF_AE_TITLE)


def mpps_complete(mpps_uid, performed_series):
    """N-SET COMPLETED with end time and the Performed Series Sequence
    referencing exactly what was stored to the PACS.

    performed_series: list of dicts with keys series_uid, series_description,
    instances = list of (sop_class_uid, sop_instance_uid).
    """
    now = datetime.now()
    ds = Dataset()
    ds.PerformedProcedureStepStatus = "COMPLETED"
    ds.PerformedProcedureStepEndDate = now.strftime("%Y%m%d")
    ds.PerformedProcedureStepEndTime = now.strftime("%H%M%S")

    seq = []
    for series in performed_series:
        item = Dataset()
        item.PerformingPhysicianName = ""
        item.ProtocolName = series.get("protocol_name", "")
        item.OperatorsName = ""
        item.SeriesInstanceUID = series["series_uid"]
        item.SeriesDescription = series.get("series_description", "")
        item.RetrieveAETitle = ""
        refs = []
        for sop_class_uid, sop_instance_uid in series["instances"]:
            ref = Dataset()
            ref.ReferencedSOPClassUID = sop_class_uid
            ref.ReferencedSOPInstanceUID = sop_instance_uid
            refs.append(ref)
        item.ReferencedImageSequence = refs
        item.ReferencedNonImageCompositeSOPInstanceSequence = []
        seq.append(item)
    ds.PerformedSeriesSequence = seq

    n_instances = sum(len(s["instances"]) for s in performed_series)
    logger.info(
        "MPPS N-SET (COMPLETED) for %s: %d series, %d instance(s) referenced",
        mpps_uid, len(performed_series), n_instances,
    )
    _mpps_set(mpps_uid, ds, "COMPLETED")


def mpps_discontinue(mpps_uid):
    """N-SET DISCONTINUED: the step was abandoned before completion."""
    now = datetime.now()
    ds = Dataset()
    ds.PerformedProcedureStepStatus = "DISCONTINUED"
    ds.PerformedProcedureStepEndDate = now.strftime("%Y%m%d")
    ds.PerformedProcedureStepEndTime = now.strftime("%H%M%S")
    ds.PerformedSeriesSequence = []
    logger.info("MPPS N-SET (DISCONTINUED) for %s", mpps_uid)
    _mpps_set(mpps_uid, ds, "DISCONTINUED")


# ---------------------------------------------------------------------------
# C-STORE
# ---------------------------------------------------------------------------

@dataclass
class StoreResult:
    filename: str
    sop_instance_uid: str
    success: bool
    detail: str


def store_files(paths):
    """C-STORE each re-stamped file to the PACS. Returns per-instance
    StoreResults; association-level failure raises DicomClientError."""
    datasets = []
    for path in paths:
        datasets.append((path, dcmread(path)))

    ae = _new_ae()
    # One presentation context per (SOP Class, Transfer Syntax) pair found in
    # the files. We propose only the file's own transfer syntax so nothing is
    # transcoded on our side; the archive sees exactly the original encoding.
    seen = set()
    for _, ds in datasets:
        pair = (ds.SOPClassUID, ds.file_meta.TransferSyntaxUID)
        if pair not in seen:
            ae.add_requested_context(pair[0], transfer_syntax=[pair[1]])
            seen.add(pair)

    logger.info(
        "C-STORE: sending %d instance(s) to %s@%s:%s",
        len(datasets), config.PACS_AE_TITLE, config.PACS_HOST, config.PACS_PORT,
    )
    assoc = _associate(
        ae, config.PACS_HOST, config.PACS_PORT, config.PACS_AE_TITLE, "C-STORE"
    )
    results = []
    try:
        for idx, (path, ds) in enumerate(datasets, start=1):
            name = path.name
            uid = str(ds.SOPInstanceUID)
            try:
                status = assoc.send_c_store(ds)
                if status and status.Status == 0x0000:
                    logger.info("  C-STORE %d/%d %s: SUCCESS", idx, len(datasets), name)
                    results.append(StoreResult(name, uid, True, "Success (0x0000)"))
                else:
                    code = f"0x{status.Status:04X}" if status else "no response"
                    logger.error(
                        "  C-STORE %d/%d %s: FAILED (%s)", idx, len(datasets), name, code
                    )
                    results.append(StoreResult(name, uid, False, f"Failed ({code})"))
            except Exception as exc:
                logger.error("  C-STORE %d/%d %s: %s", idx, len(datasets), name, exc)
                results.append(StoreResult(name, uid, False, str(exc)))
    finally:
        assoc.release()
        logger.info("Association RELEASED (%s)", config.PACS_AE_TITLE)

    ok = sum(1 for r in results if r.success)
    logger.info("C-STORE done: %d/%d instance(s) stored", ok, len(results))
    return results
