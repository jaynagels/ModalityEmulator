# Modality Emulator (DICOM Learning Lab)

The Acquisition Modality for the IHE Scheduled Workflow teaching lab. It runs
on the Windows workstation as a local web app and walks the student through a
complete SWF exam against the peers on the Linux box:

1. **Query worklist** - MWL C-FIND to the Order Entry System
2. **Select** an order and a ready-made sample study
3. **Start Exam** - MPPS N-CREATE (IN PROGRESS); the item leaves the worklist
4. **Acquire** - copy the sample and re-stamp it with the order's identity,
   with a before/after preview of every changed tag
5. **Send to PACS** - C-STORE to DCM4CHEE, per-instance results
6. **Complete** - MPPS N-SET (COMPLETED), or **Discontinue** at any point

The teaching point the tool makes visible: the patient and order information
flows out of the worklist and onto the images. Nobody types a patient name at
the modality.

## Project layout

```
modality-emulator/
  config.py           all tunables: IPs, ports, AE titles, paths
  main.py             entrypoint (logging + uvicorn)
  webapp.py           FastAPI routes + the exam state machine
  dicom_client.py     MWL C-FIND, MPPS N-CREATE/N-SET, C-STORE, C-ECHO SCUs
  restamp.py          the re-stamping rules (core teaching content)
  samples.py          sample study folder scanning
  weblog.py           in-memory log buffer feeding the browser activity panel
  templates/          server-rendered HTML (base, worklist, exam)
  start-modality.bat  double-click launcher for the Windows workstation
  deploy/nssm-service.md  optional auto-start Windows service via NSSM
```

## Install

On the Windows workstation:

1. Install Python 3.9+ from python.org (check "Add python.exe to PATH").
2. Copy this folder somewhere local, for example `C:\ModalityEmulator`.
3. Double-click `start-modality.bat`. The first run creates a `.venv` and
   installs the dependencies from `requirements.txt`, then the browser opens
   at `http://127.0.0.1:8080`.

The web server binds 127.0.0.1 and all DICOM connections are outbound, so no
inbound firewall rules are needed on Windows.

## Configure

Edit `config.py` and restart the app. The values that must match your lab:

| Setting | Meaning | Default |
| --- | --- | --- |
| `MOD_AE_TITLE` | This modality's AE title. Must equal the Order Entry System's `DEFAULT_SCHEDULED_STATION_AET` so ScheduledStationAETitle matching works. | `NAGELS_MOD` |
| `OF_HOST` / `OF_PORT` / `OF_AE_TITLE` | Order Entry System (MWL + MPPS SCP) on the Linux box | `10.0.0.50` / `4242` / `NAGELS_OF` |
| `PACS_HOST` / `PACS_PORT` / `PACS_AE_TITLE` | DCM4CHEE Arc (C-STORE target). Confirm the AE title in the DCM4CHEE UI. | `10.0.0.50` / `11112` / `DCM4CHEE` |
| `SAMPLE_BASE_DIR` | Folder of sample studies | `C:\LabSamples` |
| `WORK_DIR` | Where re-stamped copies are written | `C:\LabWork` |
| `UID_ROOT` | Prefix for generated UIDs. Teaching placeholder; replace with your own registered root outside the lab. | shared lab root |

Every setting also accepts a `MODEMU_*` environment variable override (see
`config.py`), which is how the automated tests point the app at localhost.

## Sample studies

- Create `C:\LabSamples` and put **one subfolder per sample study** in it,
  e.g. `C:\LabSamples\CT Chest Demo\...`. Nesting inside a sample folder is
  fine; every readable DICOM file under it belongs to that sample.
- The modality of a sample is read from `Modality (0008,0060)` in the files,
  never from the folder name. With `FILTER_SAMPLES_BY_MODALITY = True` only
  samples matching the selected order's scheduled modality are offered.
- Originals are never modified. Re-stamping writes copies to
  `WORK_DIR\<accession>_<timestamp>\`, so a sample is reusable across
  students and repeated runs.

## What re-stamping changes

Copied from the worklist item onto every instance: PatientName, PatientID,
PatientBirthDate, PatientSex, AccessionNumber, ReferringPhysicianName,
Modality, and the **StudyInstanceUID** (adopted from the Requested Procedure,
so the study ties to the order). StudyDate/StudyTime become acquisition time.
Each image also gets a Request Attributes Sequence (0040,0275) carrying the
Requested Procedure ID, Scheduled Procedure Step ID and description, and the
accession/study linkage, plus a PerformedProtocolCodeSequence mapped from the
order's ScheduledProtocolCodeSequence.

Freshly generated: one new SeriesInstanceUID per source series and a new
SOPInstanceUID per instance (file meta updated to match). Untouched: SOP
Class, pixel data, image geometry, and the transfer syntax (no transcoding).

## Run through (acceptance tests)

1. Start the app; the browser loads `http://127.0.0.1:8080`. On the Worklist
   page, run **C-ECHO** against both peers; both must report OK.
2. Schedule an order on the Order Entry System, then **Query Worklist**. The
   item appears; filters for modality, date range, name wildcard
   (`DOE^J*`), station AE, and accession all narrow correctly.
3. **Select** the item, pick a matching sample, press **Start Exam**. The
   Order Entry System shows the order IN PROGRESS and a worklist re-query no
   longer returns it.
4. Press **Acquire**. The preview shows worklist demographics overwriting the
   sample's values, the adopted StudyInstanceUID, fresh Series/SOP UIDs, and
   the populated Request Attributes Sequence.
5. Press **Send to PACS**. Every instance reports success; in the DCM4CHEE UI
   the study is under the order's patient and accession, not the sample's.
6. Press **Complete**. The order flips to COMPLETED on the Order Entry
   System. (Running a second exam and pressing **Discontinue** instead flips
   that order to DISCONTINUED.)
7. The files under `SAMPLE_BASE_DIR` are byte-for-byte untouched and the same
   sample works again for the next run.

## Troubleshooting

- **C-ECHO fails**: wrong IP/port/AE title in `config.py`, or the peer is
  down. The activity log at the bottom of every page shows the exact
  association attempt and rejection.
- **Worklist empty**: the Order Entry System only serves orders with status
  SCHEDULED. Check the station AE filter matches how the order was scheduled.
- **C-STORE rejected for some instances**: the archive refused that SOP Class
  or transfer syntax; the per-instance result table names the file. The app
  deliberately does not transcode.
- The activity log (also on stdout, or `modality.log` under NSSM) records
  every association, the C-FIND keys and result count, each MPPS transition,
  the re-stamp diff, and each C-STORE result.
