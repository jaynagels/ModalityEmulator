# Running the Modality Emulator as a Windows service (optional)

For the baked AMI you can register the emulator as an auto-starting Windows
service with NSSM (the Non-Sucking Service Manager), so it is already running
when the student logs in. For a normal lab session, double-clicking
`start-modality.bat` is enough and this page can be ignored.

## One-time setup

1. Run `start-modality.bat` once so the `.venv` folder exists and the
   dependencies are installed.
2. Download NSSM from https://nssm.cc and put `nssm.exe` somewhere on the
   PATH (for example `C:\Windows\System32`).
3. In an elevated (Administrator) command prompt, with `C:\ModalityEmulator`
   standing in for wherever this folder lives:

```
nssm install ModalityEmulator "C:\ModalityEmulator\.venv\Scripts\python.exe" "C:\ModalityEmulator\main.py"
nssm set ModalityEmulator AppDirectory "C:\ModalityEmulator"
nssm set ModalityEmulator DisplayName "DICOM Lab Modality Emulator"
nssm set ModalityEmulator Description "Acquisition Modality emulator (MWL, MPPS, C-STORE) for the DICOM teaching lab"
nssm set ModalityEmulator Start SERVICE_AUTO_START
nssm set ModalityEmulator AppStdout "C:\ModalityEmulator\modality.log"
nssm set ModalityEmulator AppStderr "C:\ModalityEmulator\modality.log"
nssm start ModalityEmulator
```

The same timestamped activity log that appears in the web UI is then also in
`modality.log`.

## Everyday commands

```
nssm status  ModalityEmulator
nssm restart ModalityEmulator     (after editing config.py)
nssm stop    ModalityEmulator
nssm remove  ModalityEmulator confirm
```

## Notes

- The web server binds 127.0.0.1 only, so the service is reachable just from
  a browser on this workstation; no inbound firewall rule is needed.
- All DICOM traffic is outbound from this box to the Linux box, so no DICOM
  listener or inbound port is involved either.
