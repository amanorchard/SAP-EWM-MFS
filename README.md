# PLC·SIM — SAP EWM MFS Device Simulator

> A desktop GUI application that simulates a PLC/conveyor device communicating with **SAP EWM** over real TCP/IP using the MFS telegram protocol.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![customtkinter](https://img.shields.io/badge/UI-customtkinter-teal)](https://github.com/TomSchimansky/CustomTkinter)

---

## Features

- **Real TCP/IP** connection to SAP EWM MFS gateway
- **Fixed 128-byte ASCII telegram protocol** — matching MFS spec
- **4 telegram types**: LIFE (heartbeat), MOVE ORDER, CONFIRMATION, ERROR
- **Auto-LIFE**: sends heartbeat pings at a configurable interval
- **Auto-CONFIRM**: automatically sends `CF DONE` for every `MO` received
- **Rate-limited PONG**: responds to SAP LIFE pings at max 1 per second
- **Telegram log**: colour-coded by type, full hex dump in detail panel
- **Manual compose**: send any telegram type with custom data fields
- **Export**: saves the session log as a sanitised TSV file
- **Elegant dark UI**: built with customtkinter

- <img width="960" height="504" alt="ShareX_HHgE84vHOz" src="https://github.com/user-attachments/assets/d1fdd389-cf59-467c-8d65-383bf5849d56" />


---
Download the .exe file and run. Viola! 
Pre-requisites:
Details on connectivity to EWM mentioned in further steps.

OR

## Quick Start

```bash
# 1. Clone
git clone https://github.com/<your-username>/plc-sim.git
cd plc-sim

# 2. Install dependency
pip install customtkinter

# 3. Run
python plc_sim.py
```

> **Optional fonts** for the intended aesthetic:
> [JetBrains Mono](https://www.jetbrains.com/legalnotice/fonts/) and [Syne](https://fonts.google.com/specimen/Syne).
> The app falls back to system monospace fonts automatically.

---

## Telegram Format

All telegrams are **128-byte fixed-length ASCII**, space-padded:

```
Offset  Length  Field
──────  ──────  ──────────────────────────────────
  0       2     Type        LI | MO | CF | ER
  2       2     SubType     00-99
  4       8     Source      device/system name
 12       8     Destination
 20       6     Sequence    zero-padded integer
 26     102     Data        type-specific payload
```

| Code | Name         | Direction     | Data Layout |
|------|--------------|---------------|-------------|
| `LI` | LIFE         | RX / TX       | `PING` or `PONG` |
| `MO` | Move Order   | RX (from SAP) | `TU[20] SRC_BIN[20] DST_BIN[20] PRIORITY[2]` |
| `CF` | Confirmation | TX (to SAP)   | `TU[20] BIN[20] STATUS[4] TIMESTAMP[14]` |
| `ER` | Error        | RX / TX       | `ERRCODE[4] ERRMSG[98]` |

---

## Connecting to SAP EWM

1. Open **MFS Monitor** (`/SCWM/MON`) in SAP EWM.
2. Configure your MFS subsystem with Protocol: TCP/IP, the host IP of the machine running PLC·SIM, and the port (default `5000`).
3. Start the MFS subsystem.

| PLC·SIM Field | Value                               |
|---------------|-------------------------------------|
| Host          | SAP application server IP           |
| Port          | TCP port configured in SAP MFS      |
| Device ID     | Your PLC station name (max 8 chars) |
| SAP ID        | SAP MFS system name (max 8 chars)   |

---

## Security Audit

13 vulnerabilities identified and patched — see [SECURITY.md](SECURITY.md) for the full report.

---

## Requirements

- Python 3.10+
- `customtkinter >= 5.2.0`

---

## License

MIT © 2025 — see [LICENSE](LICENSE) for details.
