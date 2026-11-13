LEANWATTS TEST PROGRAMS - 1.1.11

Choose 04-ocv-current-fixed-cutoff.json. Existing station files are preserved.
Earlier 01/02/03 procedures cannot override this release's fixed sequence;
they remain visible with an explanatory message. JSON files are data only,
never executable code. Each run freezes its program and SHA-256 hash.

CALIBRATION AND BASELINE
The full-cycle program measures the baseline, updates parameters if needed,
and calibrates current and voltage first. That sequence is unchanged.
Baseline engages CV after its initial voltage snapshot and keeps input ON
through the current baseline. Shutdown occurs after baseline completion,
before parameter programming and calibration.

TESTS: THREE RESULTS, IN THIS ORDER
1. Open-circuit voltage: prepare CV 49 while input stays OFF. After an observed
   fresh low-to-high output rise, wait for voltage build and an in-range
   plateau: at least 2 s startup observation and 3 fresh samples spanning 1 s,
   low spread and low slope. Intermediate low plateaus do not engage the load
   or fail early. If no acceptable plateau appears within 15 s, record why and
   stop before loading. Operator restart window is separate (default 120 s).
2. Full current: enable CV 49, observe at least 18 s, then require a fresh 3 s
   plateau with low spread and slope. Rising current never fails early just
   because it has not reached the target. Bounded ramp window defaults 60 s.
   Record sustained plateau, peak, required range and failure reason.
3. Cutoff: transition the still-enabled load to CC 5 A. A fresh actual 5 A
   conducting reading permits the first 4.9 A demand immediately. Continue
   fixed 0.1 A steps at nominal 1 s cadence, waiting for fresh measured current
   to follow each demand. There is no separate CC voltage test, control hold
   or adaptive stepping. Stop decreasing at 0.5 A; observe that floor for 3 s
   before failing if no cutoff occurs. Overall cutoff bound is 180 s.

Both actual voltage and current must remain low for 10 s to confirm cutoff.
A restart/relay chatter fails; no automatic charger restart is attempted.
Report the observed bracket between last actual conducting current and the
first-off demand, not zero after shutdown. Cadence and firmware delay limit
threshold resolution; firmware delay/repeatability are not certified.

Load input stays ON between loaded test items and switches OFF only after
completion, Stop or failure. Tests send no CAN Start/Stop, parameters, trim,
calibration entry or save commands. Controller ACK remains enabled while all
application CAN transmission is blocked. Switch AC OFF before disconnecting.

SUPPORTED SCHEMA 1 FIELDS
schema:1; name; description
workflow:full_cycle, calibration_only, or test_only
baseline:
  engage_voltage:null uses the variant baseline engagement voltage
  minimum_ramp_seconds:18 to 60, default 18
  ramp_timeout_seconds:15 to 180, default 60; must exceed minimum + settle
  settle_seconds:1 to 10, default 3
testing:
  start_method:external_restart
  procedure:ocv_current_fixed_cutoff_v1
  restart_timeout_seconds:30 to 300, default 120

CV 49 V, CC 5 A,0.1 A steps,0.5 A floor and their cadence are fixed test behavior;
they do not alter the saved variant or calibration parameters. Variant and
station electrical limits still apply. Reports include explicit failure
reasons and never relabel historical loaded-voltage tests as open-circuit.
