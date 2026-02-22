# Security Audit Report — PLC·SIM

Full audit performed on `plc_sim.py`. All 13 vulnerabilities identified have been patched.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 1     |
| High     | 4     |
| Medium   | 6     |
| Low      | 2     |
| **Total**| **13**|

---

## Findings & Fixes

### [CRITICAL] FIX 1 — `assert` Stripped by Optimiser
**Location**: `build_telegram()`, telegram encoder  
**Issue**: `assert len(body) == TELEGRAM_LEN` is silently removed when Python runs with the `-O` (optimise) flag. A malformed 128-byte telegram could be transmitted to SAP with no error raised.  
**Fix**: Replaced with `raise RuntimeError(...)` which is never stripped.

---

### [HIGH] FIX 2 — Bare `except` Swallows Critical Signals
**Location**: `TCPConnection.stop()`  
**Issue**: `except: pass` catches *everything*, including `KeyboardInterrupt` and `SystemExit`, making the process impossible to kill cleanly.  
**Fix**: Changed to `except Exception: pass`.

---

### [HIGH] FIX 3 — Unbounded Receive Buffer (OOM Risk)
**Location**: `TCPConnection.run()`, receive loop  
**Issue**: `buf += chunk` with no upper bound. A misbehaving or malicious SAP system sending a continuous stream of bytes without completing a 128-byte telegram would fill all available RAM until an OOM kill.  
**Fix**: Buffer capped at `TELEGRAM_LEN × 256` (~32 KB). Overflow bytes are discarded and a warning event is emitted.

---

### [HIGH] FIX 4 — Unbounded Message Log (OOM Risk)
**Location**: `_on_recv()` / `_on_sent()`  
**Issue**: `self.messages` grows without limit. Long sessions or a flooding SAP system can exhaust memory.  
**Fix**: Log capped at 5,000 entries. Oldest entries are dropped when the cap is reached.

---

### [HIGH] FIX 5 — LIFE Pong Storm
**Location**: `_on_recv()`, LIFE handling  
**Issue**: Every incoming LIFE telegram scheduled `self.after(200, pong)` unconditionally. A SAP burst of 100 LIFE telegrams would schedule 100 PONGs, flooding the TCP connection.  
**Fix**: Rate-limited to at most one PONG per second using `time.monotonic()`.

---

### [MEDIUM] FIX 6 — Port Not Range-Validated
**Location**: `_connect()`  
**Issue**: Port was only checked for `ValueError` (non-integer). Values like `0` or `99999` were silently accepted, causing confusing socket errors at connect time.  
**Fix**: Explicit range check enforcing `1 <= port <= 65535`.

---

### [MEDIUM] FIX 7 — LIFE Interval of 0 Causes Tight Loop
**Location**: `_schedule_autolife()`  
**Issue**: A user-entered interval of `0` results in `self.after(0, _schedule_autolife)`, scheduling the callback as fast as Tkinter allows — hammering CPU and flooding SAP with LIFE telegrams.  
**Fix**: `max(1, interval)` enforces a minimum of 1 second.

---

### [MEDIUM] FIX 8 — Empty Host Accepted
**Location**: `_connect()`  
**Issue**: An empty or whitespace-only host string was passed directly to `socket.connect()`, producing an unhelpful OS-level error.  
**Fix**: Explicit non-empty validation with a clear UI error message before any socket call.

---

### [MEDIUM] FIX 9 — `UnicodeEncodeError` on Non-ASCII Input
**Location**: `build_telegram()`  
**Issue**: `body.encode("ascii")` raises `UnicodeEncodeError` if the user types non-ASCII characters in Device ID, SAP ID, or Data fields (e.g. accented characters, emoji).  
**Fix**: `encode("ascii", errors="replace")` — non-ASCII bytes are replaced with `?`, preserving the fixed-length frame.

---

### [MEDIUM] FIX 10 — Socket File Descriptor Leak
**Location**: `TCPConnection.run()`  
**Issue**: The socket was only closed inside `stop()`. When the receive loop exits naturally (e.g. SAP closes the connection), the socket was never explicitly closed, leaking a file descriptor for every session.  
**Fix**: Wrapped in a `finally` block that always calls `sock.close()`.

---

### [MEDIUM] FIX 11 — Duplicate Threads on Rapid Reconnect
**Location**: `_connect()`  
**Issue**: Clicking Connect while a session was still tearing down started a second `TCPConnection` thread. Both threads shared `event_q` and `send_q`, causing interleaved events and duplicate messages.  
**Fix**: The old thread is `stop()`ed and `join()`ed (up to 3 s) before starting a new one. Both queues are also drained to prevent stale events carrying over.

---

### [LOW] FIX 12 — TSV Injection via SAP Telegram Data
**Location**: `_export_log()`  
**Issue**: SAP telegram fields containing tab (`\t`) or newline (`\n`) characters would corrupt the exported TSV file, injecting extra rows or splitting columns. A crafted SAP payload could produce a misleading export.  
**Fix**: All field values pass through `_safe()`, which strips `\t`, `\n`, and `\r` before writing.

---

### [LOW] FIX 13 — Silent Parse Failures
**Location**: `parse_telegram()`  
**Issue**: `except Exception: return None` silently discarded every malformed telegram with no indication of what went wrong, making integration debugging very difficult.  
**Fix**: The exception and a hex preview of the raw bytes are printed to stdout, making failures visible during development and integration testing.
