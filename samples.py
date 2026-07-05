"""Discover DICOM sample studies for the simulated acquisition step.

Two ways to pick the images that stand in for scanner output:

  - The sample library: each immediate subfolder of SAMPLE_BASE_DIR is one
    ready-made sample study, offered as a quick pick.
  - Browsing: the exam page can walk the local filesystem and acquire any
    folder that holds readable DICOM. The app runs on the student's own
    machine and binds 127.0.0.1, so server-side browsing is local browsing;
    that is the whole point, not a security hole.

Modality is always read from Modality (0008,0060) in the files themselves,
never guessed from folder names. Originals are read-only as far as this
application is concerned.
"""

import logging
import os
import string
import subprocess
import sys
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

# While browsing, stop scanning a folder after this many files so wandering
# into something like C:\Windows cannot hang the page. The final scan at
# Start Exam runs uncapped on the folder the student actually chose.
BROWSE_SCAN_LIMIT = 1000


@dataclass
class SampleStudy:
    name: str                 # folder name shown in the UI
    path: Path
    modalities: list = field(default_factory=list)
    file_count: int = 0       # files readable as DICOM Part 10
    total_files: int = 0      # every regular file seen, DICOM or not
    series_count: int = 0
    patient_name: str = ""    # the sample's own (soon to be overwritten) identity
    patient_id: str = ""
    study_description: str = ""
    study_count: int = 0      # distinct StudyInstanceUIDs found in the folder
    truncated: bool = False   # scan stopped at the browse cap
    error: str = ""           # e.g. permission denied while walking

    @property
    def modality_label(self):
        return "/".join(self.modalities) if self.modalities else "?"

    def matches_modality(self, modality):
        return modality.upper() in (m.upper() for m in self.modalities)


def dicom_files(folder):
    """All readable DICOM Part 10 files under folder, sorted for stable
    ordering. Non-DICOM files (README.txt, DICOMDIR-less strays) are skipped.
    Used by the re-stamp step, which needs the complete, ordered list."""
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


def scan_folder(folder, limit=None):
    """Scan an arbitrary folder into a SampleStudy. A single header read per
    file both detects DICOM and collects the browser metadata. With `limit`,
    scanning stops after that many files (browsing preview); without it the
    scan is exhaustive (Start Exam)."""
    folder = Path(folder)
    sample = SampleStudy(name=folder.name or str(folder), path=folder)
    modalities = []
    series = set()
    studies = set()
    try:
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            if limit is not None and sample.total_files >= limit:
                sample.truncated = True
                break
            sample.total_files += 1
            try:
                ds = dcmread(path, stop_before_pixels=True, specific_tags=_SCAN_TAGS)
            except Exception:
                continue
            sample.file_count += 1
            modality = str(ds.get("Modality", "")).upper()
            if modality and modality not in modalities:
                modalities.append(modality)
            series.add(str(ds.get("SeriesInstanceUID", "")))
            studies.add(str(ds.get("StudyInstanceUID", "")))
            if not sample.patient_name:
                sample.patient_name = str(ds.get("PatientName", ""))
                sample.patient_id = str(ds.get("PatientID", ""))
                sample.study_description = str(ds.get("StudyDescription", ""))
    except (PermissionError, OSError) as exc:
        sample.error = str(exc)
        logger.warning("Could not fully scan %s: %s", folder, exc)
    sample.modalities = modalities
    sample.series_count = len(series - {""}) or (1 if sample.file_count else 0)
    sample.study_count = len(studies - {""}) or (1 if sample.file_count else 0)
    return sample


# ---------------------------------------------------------------------------
# Sample library (SAMPLE_BASE_DIR quick picks)
# ---------------------------------------------------------------------------

def base_dir_info():
    """Diagnostics for the sample library panel in the web UI: what path
    the app actually checks, whether it exists from this process's point of
    view, and the most common misconfigurations spelled out."""
    base = Path(config.SAMPLE_BASE_DIR)
    info = {
        "path": str(base),
        "exists": base.is_dir(),
        "loose_dicom": 0,   # DICOM files directly in the base dir (must be in a subfolder)
        "hint": "",
    }
    if not info["exists"]:
        if os.name != "nt" and ":" in str(base):
            # Windows drive path on a POSIX process: almost certainly WSL.
            drive = str(base)[0].lower()
            posix = "/mnt/" + drive + str(base)[2:].replace("\\", "/")
            info["hint"] = (
                "The app is running on a non-Windows system (WSL or Linux) but "
                f"SAMPLE_BASE_DIR is a Windows path. From WSL that folder is "
                f"{posix}; either run the app from Windows (start-modality.bat) "
                "or set SAMPLE_BASE_DIR to the WSL path."
            )
        else:
            info["hint"] = (
                "Create the folder (exact spelling), or point SAMPLE_BASE_DIR "
                "in config.py at where the sample studies actually live, then "
                "restart the app. You can also browse to any local folder "
                "during an exam instead."
            )
        logger.warning("SAMPLE_BASE_DIR does not exist: %s%s", base,
                       " | " + info["hint"] if info["hint"] else "")
        return info
    info["loose_dicom"] = sum(
        1 for p in sorted(base.iterdir()) if p.is_file() and _is_dicom(p)
    )
    if info["loose_dicom"]:
        logger.warning(
            "%d DICOM file(s) sit directly in %s; they are ignored. Each "
            "sample study must be in its own subfolder.",
            info["loose_dicom"], base,
        )
    return info


def _is_dicom(path):
    try:
        dcmread(path, stop_before_pixels=True, specific_tags=["SOPClassUID"])
        return True
    except Exception:
        return False


def scan_all():
    """Every immediate subfolder of SAMPLE_BASE_DIR, including ones that hold
    no readable DICOM, so the UI can show WHY a folder is not offered.

    Rescans the disk on every call: sample folders are small and the student
    may drop new ones in while the app is running.
    """
    base = Path(config.SAMPLE_BASE_DIR)
    if not base.is_dir():
        return []
    return [scan_folder(folder, limit=BROWSE_SCAN_LIMIT)
            for folder in sorted(p for p in base.iterdir() if p.is_dir())]


def scan_samples(modality=None):
    """Usable library sample studies, optionally restricted to one modality."""
    result = []
    for sample in scan_all():
        if sample.file_count == 0:
            logger.warning(
                "Sample folder %s has %d file(s) but none readable as DICOM "
                "Part 10; not offered.", sample.path, sample.total_files,
            )
            continue
        if (
            modality
            and config.FILTER_SAMPLES_BY_MODALITY
            and not sample.matches_modality(modality)
        ):
            continue
        result.append(sample)
    return result


# ---------------------------------------------------------------------------
# Folder browsing (pick any local folder as the acquisition source)
# ---------------------------------------------------------------------------

def list_roots():
    """Filesystem roots for the browser: drive letters on Windows, / elsewhere."""
    if hasattr(os, "listdrives"):          # Python 3.12+
        return list(os.listdrives())
    if os.name == "nt":
        return [f"{c}:\\" for c in string.ascii_uppercase
                if os.path.exists(f"{c}:\\")]
    return ["/"]


def browse(path_str=None):
    """One level of the folder browser: where we are, how to go up, what
    subfolders exist, and a capped DICOM scan of the current folder."""
    base = Path(config.SAMPLE_BASE_DIR)
    if path_str:
        current = Path(path_str)
    elif base.is_dir():
        current = base
    else:
        current = Path.home()
    try:
        current = current.expanduser().resolve()
    except OSError:
        current = Path.home()
    error = ""
    if not current.is_dir():
        error = f"{current} is not a folder; showing the default location instead."
        current = base if base.is_dir() else Path.home()

    subfolders = []
    try:
        subfolders = [(p.name, str(p))
                      for p in sorted(current.iterdir())
                      if p.is_dir() and not p.name.startswith(".")]
    except (PermissionError, OSError) as exc:
        error = f"Cannot list {current}: {exc}"

    # Breadcrumbs: every ancestor is clickable.
    crumbs = []
    accumulated = None
    for part in current.parts:
        accumulated = Path(part) if accumulated is None else accumulated / part
        crumbs.append((part, str(accumulated)))

    return {
        "current": str(current),
        "parent": str(current.parent) if current.parent != current else None,
        "roots": list_roots(),
        "crumbs": crumbs,
        "subfolders": subfolders,
        "error": error,
        "scan": scan_folder(current, limit=BROWSE_SCAN_LIMIT),
    }


# The native OS "Select Folder" dialog. A browser page cannot open it and
# hand the path to the server, but this server runs on the student's own
# machine, so the server opens the dialog itself. It runs in a short-lived
# helper process: tkinter is not thread-safe, and uvicorn handles requests
# on worker threads.
_PICKER_SCRIPT = """\
import sys
import tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(
    initialdir=sys.argv[1], title="Select the study folder to acquire", mustexist=True
)
root.destroy()
sys.stdout.write(path or "")
"""


def pick_folder_native(initial=None):
    """Open the OS folder picker. Returns (path or None, error message).
    None with no error means the student cancelled the dialog."""
    if initial is None:
        base = Path(config.SAMPLE_BASE_DIR)
        initial = str(base) if base.is_dir() else str(Path.home())
    # CREATE_NO_WINDOW: no console flash on Windows for the helper process.
    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PICKER_SCRIPT, initial],
            capture_output=True, text=True, timeout=300,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        return None, "The folder dialog was left open too long and timed out."
    except OSError as exc:
        return None, f"Could not start the folder dialog: {exc}"
    if proc.returncode != 0:
        logger.warning("Native folder dialog unavailable: %s",
                       proc.stderr.strip().splitlines()[-1] if proc.stderr else "?")
        return None, (
            "The native folder dialog is not available here (no desktop "
            "session, or Python was installed without tkinter). Use the "
            "in-page browser below instead."
        )
    path = proc.stdout.strip()
    return (path or None), ""


def get_sample(path_str):
    """Resolve a chosen folder (quick pick or browsed) into a fully scanned
    SampleStudy. Uncapped scan: this is what actually gets acquired."""
    try:
        folder = Path(path_str).expanduser().resolve()
    except (OSError, ValueError):
        return None
    if not folder.is_dir():
        return None
    sample = scan_folder(folder)
    return sample if sample.file_count else None
