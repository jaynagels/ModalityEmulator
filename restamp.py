"""Re-stamp a sample study with the identity of a selected worklist item.

This is the core teaching content of the lab: the patient and order
information flows from the worklist onto the images. Nobody types a patient
name at the modality.

For every instance in the copied study we:
  - overwrite patient demographics, accession, referring physician, and
    Modality with the worklist values
  - adopt the worklist's StudyInstanceUID so the study ties to the order
  - set StudyDate/StudyTime to acquisition time (now)
  - add a Request Attributes Sequence linking each image back to the order
  - map ScheduledProtocolCodeSequence to PerformedProtocolCodeSequence
  - generate a fresh SeriesInstanceUID per source series and a fresh
    SOPInstanceUID per instance (file meta updated to match)

Everything else - SOP Class, pixel data, image geometry, transfer syntax -
is left exactly as it was. Originals under SAMPLE_BASE_DIR are never
touched; all writes go to a new folder under WORK_DIR.
"""

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid

import config
import samples
from dicom_client import wl_sps

logger = logging.getLogger("modality.restamp")

# Tags copied 1:1 from the worklist item onto every instance, shown in the
# before/after preview in this order. (keyword, tag string, worklist source)
# where source is "item" (top level) or "sps" (Scheduled Procedure Step).
IDENTITY_TAGS = [
    ("PatientName", "(0010,0010)", "item"),
    ("PatientID", "(0010,0020)", "item"),
    ("PatientBirthDate", "(0010,0030)", "item"),
    ("PatientSex", "(0010,0040)", "item"),
    ("AccessionNumber", "(0008,0050)", "item"),
    ("ReferringPhysicianName", "(0008,0090)", "item"),
    ("StudyInstanceUID", "(0020,000D)", "item"),
    ("Modality", "(0008,0060)", "sps"),
]


@dataclass
class TagChange:
    keyword: str
    tag: str
    old: str
    new: str

    @property
    def changed(self):
        return self.old != self.new


@dataclass
class InstanceRecord:
    rel_path: str
    sop_class_uid: str
    sop_class_name: str
    old_sop_uid: str
    new_sop_uid: str
    new_series_uid: str
    series_description: str
    transfer_syntax: str
    work_path: Path


@dataclass
class RestampResult:
    work_dir: Path
    tag_changes: list = field(default_factory=list)     # TagChange, study level
    series_map: dict = field(default_factory=dict)      # old series UID -> new
    instances: list = field(default_factory=list)       # InstanceRecord
    request_attributes: list = field(default_factory=list)  # (label, value) preview
    study_date: str = ""
    study_time: str = ""

    @property
    def file_paths(self):
        return [rec.work_path for rec in self.instances]

    def performed_series(self, stored_uids=None):
        """Shape the result for the MPPS Performed Series Sequence. When
        stored_uids is given, only those SOP Instance UIDs are referenced,
        so the MPPS reports exactly what actually reached the PACS."""
        by_series = {}
        for rec in self.instances:
            if stored_uids is not None and rec.new_sop_uid not in stored_uids:
                continue
            by_series.setdefault(rec.new_series_uid, []).append(rec)
        out = []
        for series_uid, recs in by_series.items():
            out.append({
                "series_uid": series_uid,
                "series_description": recs[0].series_description,
                "instances": [(rec.sop_class_uid, rec.new_sop_uid) for rec in recs],
            })
        return out


def _save(ds, path):
    """Write a full Part 10 file. pydicom 3 renamed the keyword argument, so
    support both the lab's pinned pydicom 2.4 and newer installs."""
    try:
        ds.save_as(path, enforce_file_format=True)
    except TypeError:
        ds.save_as(path, write_like_original=False)


def restamp_study(sample, wl_item):
    """Copy the sample study into a fresh WORK_DIR subfolder and re-stamp
    every instance from the worklist item. Returns a RestampResult carrying
    the before/after diff for the preview page."""
    sps = wl_sps(wl_item)
    now = datetime.now()
    accession = str(wl_item.get("AccessionNumber", "NOACC")) or "NOACC"
    work_dir = Path(config.WORK_DIR) / f"{accession}_{now:%Y%m%d_%H%M%S}"
    work_dir.mkdir(parents=True, exist_ok=True)

    result = RestampResult(
        work_dir=work_dir,
        study_date=now.strftime("%Y%m%d"),
        study_time=now.strftime("%H%M%S"),
    )

    # New values common to every instance
    new_values = {}
    for keyword, tag, source in IDENTITY_TAGS:
        src = wl_item if source == "item" else sps
        new_values[keyword] = str(src.get(keyword, ""))

    # One Request Attributes Sequence item, identical on every image: the
    # SWF back-link from image to order.
    ras = Dataset()
    ras.AccessionNumber = new_values["AccessionNumber"]
    ras.StudyInstanceUID = new_values["StudyInstanceUID"]
    ras.RequestedProcedureID = str(wl_item.get("RequestedProcedureID", ""))
    ras.RequestedProcedureDescription = str(
        wl_item.get("RequestedProcedureDescription", "")
    )
    ras.ScheduledProcedureStepID = str(sps.get("ScheduledProcedureStepID", ""))
    ras.ScheduledProcedureStepDescription = str(
        sps.get("ScheduledProcedureStepDescription", "")
    )
    ras.ScheduledProtocolCodeSequence = copy.deepcopy(
        list(sps.get("ScheduledProtocolCodeSequence", []))
    )
    result.request_attributes = [
        ("Requested Procedure ID (0040,1001)", str(ras.RequestedProcedureID)),
        ("Scheduled Procedure Step ID (0040,0009)", str(ras.ScheduledProcedureStepID)),
        ("SPS Description (0040,0007)", str(ras.ScheduledProcedureStepDescription)),
        ("Accession Number (0008,0050)", str(ras.AccessionNumber)),
        ("Study Instance UID (0020,000D)", str(ras.StudyInstanceUID)),
    ]

    performed_protocol = copy.deepcopy(
        list(sps.get("ScheduledProtocolCodeSequence", []))
    )

    files = samples.dicom_files(sample.path)
    logger.info(
        "Re-stamping sample '%s' (%d file(s)) as %s / acc %s into %s",
        sample.name, len(files), new_values["PatientName"], accession, work_dir,
    )

    series_map = {}     # old SeriesInstanceUID -> new
    old_values = {}     # keyword -> set of old values seen (for the diff)

    for src_path in files:
        ds = dcmread(src_path)  # full read: pixel data comes along untouched

        # Record old values for the preview before overwriting anything
        for keyword, tag, _ in IDENTITY_TAGS:
            old_values.setdefault(keyword, set()).add(str(ds.get(keyword, "")))
        for keyword in ("StudyDate", "StudyTime"):
            old_values.setdefault(keyword, set()).add(str(ds.get(keyword, "")))

        # Fresh Series UID, one per distinct source series
        old_series = str(ds.get("SeriesInstanceUID", ""))
        if old_series not in series_map:
            series_map[old_series] = generate_uid(prefix=config.UID_ROOT)
        new_series_uid = series_map[old_series]

        # Fresh SOP Instance UID per instance; file meta must match (PS3.10)
        old_sop_uid = str(ds.get("SOPInstanceUID", ""))
        new_sop_uid = generate_uid(prefix=config.UID_ROOT)

        # --- identity from the worklist ---
        for keyword, tag, _ in IDENTITY_TAGS:
            setattr(ds, keyword, new_values[keyword])

        # --- acquisition context ---
        ds.StudyDate = result.study_date
        ds.StudyTime = result.study_time
        ds.SeriesInstanceUID = new_series_uid
        ds.SOPInstanceUID = new_sop_uid
        ds.file_meta.MediaStorageSOPInstanceUID = new_sop_uid

        # --- order back-links ---
        ds.RequestAttributesSequence = [copy.deepcopy(ras)]
        ds.PerformedProtocolCodeSequence = copy.deepcopy(performed_protocol)

        # Write the copy, mirroring the sample's own folder layout so the
        # student can diff the two trees side by side.
        rel = src_path.relative_to(sample.path)
        dest = work_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _save(ds, dest)

        result.instances.append(InstanceRecord(
            rel_path=str(rel),
            sop_class_uid=str(ds.SOPClassUID),
            sop_class_name=getattr(ds.SOPClassUID, "name", str(ds.SOPClassUID)),
            old_sop_uid=old_sop_uid,
            new_sop_uid=new_sop_uid,
            new_series_uid=new_series_uid,
            series_description=str(ds.get("SeriesDescription", "")),
            transfer_syntax=getattr(
                ds.file_meta.TransferSyntaxUID, "name",
                str(ds.file_meta.TransferSyntaxUID),
            ),
            work_path=dest,
        ))

    result.series_map = series_map

    # Study-level before/after diff for the preview page
    def _old_display(keyword):
        olds = old_values.get(keyword, {""})
        return olds.pop() if len(olds) == 1 else "(multiple values)"

    for keyword, tag, _ in IDENTITY_TAGS:
        result.tag_changes.append(
            TagChange(keyword=keyword, tag=tag, old=_old_display(keyword),
                      new=new_values[keyword])
        )
    result.tag_changes.append(
        TagChange("StudyDate", "(0008,0020)", _old_display("StudyDate"),
                  result.study_date)
    )
    result.tag_changes.append(
        TagChange("StudyTime", "(0008,0030)", _old_display("StudyTime"),
                  result.study_time)
    )

    for change in result.tag_changes:
        logger.info(
            "  re-stamp %s %s: %r -> %r",
            change.tag, change.keyword, change.old, change.new,
        )
    logger.info(
        "  %d series re-mapped, %d instance(s) written; originals untouched",
        len(series_map), len(result.instances),
    )
    return result
