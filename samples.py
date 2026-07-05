"""Scan SAMPLE_BASE_DIR for ready-made DICOM sample studies.

Each immediate subfolder of SAMPLE_BASE_DIR is one sample study; it may hold
one or more series worth of instances, nested however the student dropped
them in. Modality is read from Modality (0008,0060) in the files themselves,
never guessed from folder names. Originals are read-only as far as this
application is concerned.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from pydicom import dcmread

import config

logger = logging.getLogger("modality.samples")

# Header-only read of just the tags the sample browser needs; pixel data is
# never loaded during scanning.
_SCAN_TAGS = [
    "SOPClassUID",
    "SOPInstanceUID",
    "Modality",
    "PatientName",
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "StudyDescription",
    "SeriesDescription",
]


@dataclass
class SampleStudy:
    name: str                 # folder name, used as the selection key
    path: Path
    modalities: list = field(default_factory=list)
    file_count: int = 0
    series_count: int = 0
    patient_name: str = ""    # the sample's own (soon to be overwritten) identity
    patient_id: str = ""
    study_description: str = ""

    @property
    def modality_label(self):
        return "/".join(self.modalities) if self.modalities else "?"

    def matches_modality(self, modality):
        return modality.upper() in (m.upper() for m in self.modalities)


def dicom_files(folder):
    """All readable DICOM Part 10 files under folder, sorted for stable
    ordering. Non-DICOM files (README.txt, DICOMDIR-less strays) are skipped."""
    found = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        try:
            dcmread(path, stop_before_pixels=True, specific_tags=["SOPClassUID"])
        except Exception:
            # Not DICOM (or unreadable): fine, sample folders may hold notes etc.
            continue
        found.append(path)
    return found


def _scan_one(folder):
    sample = SampleStudy(name=folder.name, path=folder)
    modalities = []
    series = set()
    for path in dicom_files(folder):
        try:
            ds = dcmread(path, stop_before_pixels=True, specific_tags=_SCAN_TAGS)
        except Exception as exc:
            logger.warning("Skipping unreadable file %s: %s", path, exc)
            continue
        sample.file_count += 1
        modality = str(ds.get("Modality", "")).upper()
        if modality and modality not in modalities:
            modalities.append(modality)
        series.add(str(ds.get("SeriesInstanceUID", "")))
        if not sample.patient_name:
            sample.patient_name = str(ds.get("PatientName", ""))
            sample.patient_id = str(ds.get("PatientID", ""))
            sample.study_description = str(ds.get("StudyDescription", ""))
    sample.modalities = modalities
    sample.series_count = len(series - {""}) or (1 if sample.file_count else 0)
    return sample


def scan_samples(modality=None):
    """List sample studies, optionally restricted to one modality.

    Rescans the disk on every call: sample folders are small and the student
    may drop new ones in while the app is running.
    """
    base = Path(config.SAMPLE_BASE_DIR)
    if not base.is_dir():
        logger.warning("SAMPLE_BASE_DIR does not exist: %s", base)
        return []
    result = []
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        sample = _scan_one(folder)
        if sample.file_count == 0:
            continue
        if (
            modality
            and config.FILTER_SAMPLES_BY_MODALITY
            and not sample.matches_modality(modality)
        ):
            continue
        result.append(sample)
    return result


def get_sample(name):
    """Look one sample up by folder name (form round-trip)."""
    base = Path(config.SAMPLE_BASE_DIR)
    folder = base / name
    # Guard against path escape through a doctored form value.
    if not folder.is_dir() or folder.parent.resolve() != base.resolve():
        return None
    sample = _scan_one(folder)
    return sample if sample.file_count else None
