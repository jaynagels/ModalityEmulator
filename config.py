"""Central configuration for the DICOM Learning Lab Modality Emulator.

Every tunable value lives here. Edit this file, restart the app, done.
Nothing elsewhere in the project hard-codes IPs, ports, AE titles, or paths.

Each value can also be overridden with a MODEMU_* environment variable
(shown next to each setting). That is convenient for testing; in the lab
you normally just edit this file.
"""

import os


def _env(name, default):
    return os.environ.get(name, default)


def _env_int(name, default):
    return int(os.environ.get(name, default))


# ---------------------------------------------------------------------------
# Web UI (local browser only)
# ---------------------------------------------------------------------------
WEB_HOST = _env("MODEMU_WEB_HOST", "127.0.0.1")   # loopback: no inbound firewall rule needed
WEB_PORT = _env_int("MODEMU_WEB_PORT", "8080")

# ---------------------------------------------------------------------------
# This modality (Acquisition Modality in IHE SWF terms)
# ---------------------------------------------------------------------------
# Must match the Order Entry System's DEFAULT_SCHEDULED_STATION_AET so that
# worklist matching on ScheduledStationAETitle finds our scheduled steps.
MOD_AE_TITLE = _env("MODEMU_AE_TITLE", "NAGELS_MOD")

# ---------------------------------------------------------------------------
# Order Entry System (MWL SCP + MPPS SCP on the Linux box)
# ---------------------------------------------------------------------------
OF_HOST = _env("MODEMU_OF_HOST", "10.0.0.50")      # replace with the Linux box IP
OF_PORT = _env_int("MODEMU_OF_PORT", "4242")
OF_AE_TITLE = _env("MODEMU_OF_AET", "NAGELS_OF")

# ---------------------------------------------------------------------------
# PACS (DCM4CHEE Arc, C-STORE destination on the Linux box)
# ---------------------------------------------------------------------------
PACS_HOST = _env("MODEMU_PACS_HOST", "10.0.0.50")  # replace with the Linux box IP
PACS_PORT = _env_int("MODEMU_PACS_PORT", "11112")
PACS_AE_TITLE = _env("MODEMU_PACS_AET", "DCM4CHEE")  # confirm against the archive AE title

# ---------------------------------------------------------------------------
# Sample data (student-supplied ready-made DICOM studies)
# ---------------------------------------------------------------------------
# Each immediate subfolder of SAMPLE_BASE_DIR is one sample study.
# Originals are never modified; re-stamped copies go to WORK_DIR.
SAMPLE_BASE_DIR = _env("MODEMU_SAMPLE_DIR", "C:\\LabSamples")
WORK_DIR = _env("MODEMU_WORK_DIR", "C:\\LabWork")

# Only offer sample studies whose Modality (0008,0060) matches the selected
# worklist item's scheduled modality.
FILTER_SAMPLES_BY_MODALITY = True

# ---------------------------------------------------------------------------
# UID generation (new Series / SOP Instance UIDs, MPPS SOP Instance UIDs)
# ---------------------------------------------------------------------------
# TEACHING PLACEHOLDER: replace with a UID root registered to your own
# organization before using this outside the lab. Same teaching root as the
# Order Entry System; generate_uid() adds enough entropy that the two
# applications cannot collide.
UID_ROOT = _env("MODEMU_UID_ROOT", "1.2.826.0.1.3680043.10.9999.")

# ---------------------------------------------------------------------------
# DICOM association behavior
# ---------------------------------------------------------------------------
# Short timeouts so a wrong IP or a down peer fails fast in the browser
# instead of hanging the page.
CONNECT_TIMEOUT = 5    # seconds to establish the TCP connection
ACSE_TIMEOUT = 10      # seconds for association negotiation
DIMSE_TIMEOUT = 30     # seconds for each DIMSE message (C-FIND, C-STORE, ...)

# ---------------------------------------------------------------------------
# Activity log shown in the web UI
# ---------------------------------------------------------------------------
LOG_BUFFER_LINES = 500  # how many recent log lines the browser panel keeps
