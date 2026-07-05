"""Entrypoint for the Modality Emulator.

Pure SCU application: all DICOM connections are outbound (MWL C-FIND and
MPPS to the Order Entry System, C-STORE to the PACS), so there is no DICOM
listener here, just uvicorn serving the local web UI on 127.0.0.1.
The start-modality.bat launcher and the optional NSSM service both run
exactly this file.
"""

import logging
import sys
from pathlib import Path

import uvicorn
from pydicom.uid import generate_uid

import config
import weblog
from webapp import app

logger = logging.getLogger("modality")


def setup_logging():
    formatter_spec = dict(
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, **formatter_spec)
    # Mirror everything into the ring buffer that feeds the browser log panel.
    weblog.install(logging.Formatter(formatter_spec["format"],
                                     formatter_spec["datefmt"]))
    # pynetdicom's own DEBUG/INFO output is very chatty; dicom_client.py logs
    # the teaching-relevant events in plain language instead.
    logging.getLogger("pynetdicom").setLevel(logging.WARNING)


def main():
    setup_logging()

    # Fail fast if the UID root placeholder was mangled during editing.
    try:
        generate_uid(prefix=config.UID_ROOT)
    except ValueError as exc:
        logger.error("config.UID_ROOT is not a valid UID prefix: %s", exc)
        sys.exit(1)

    if not Path(config.SAMPLE_BASE_DIR).is_dir():
        logger.warning(
            "SAMPLE_BASE_DIR %s does not exist yet. Create it and drop one "
            "folder per sample study inside.", config.SAMPLE_BASE_DIR,
        )
    Path(config.WORK_DIR).mkdir(parents=True, exist_ok=True)

    logger.info(
        "Modality Emulator '%s' | worklist/MPPS: %s@%s:%s | PACS: %s@%s:%s",
        config.MOD_AE_TITLE,
        config.OF_AE_TITLE, config.OF_HOST, config.OF_PORT,
        config.PACS_AE_TITLE, config.PACS_HOST, config.PACS_PORT,
    )
    logger.info("Web UI on http://%s:%s", config.WEB_HOST, config.WEB_PORT)
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT,
                log_level="warning")


if __name__ == "__main__":
    main()
