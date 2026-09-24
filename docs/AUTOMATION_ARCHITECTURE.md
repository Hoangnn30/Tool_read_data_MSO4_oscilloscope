# Automation Test Architecture

## Goal

Keep test scripts stable when the DUT/IC changes.

The software is split into four layers:

1. **Transport** - VISA TCPIP, TCP Socket, Serial COM, GPIB/VISA.
2. **Instrument driver** - MSO44B, MSO24B, PWS4323, Keithley 2100/2000, AWG2000.
3. **Test sequence** - reusable test flow and instrument actions.
4. **IC profile** - DUT-specific voltages, channel mapping, timing and limits.

When the IC changes, prefer changing the profile instead of the driver or sequence.

## Example

A generic sequence can remain:

1. Configure power rail
2. Turn output on
3. Configure AWG
4. Wait for DUT settle
5. Read DMM
6. Capture scope waveform
7. Evaluate limits
8. Save raw data and report
9. Turn output off

For a different IC, only change values such as:

- VDD / VIO
- DMM range
- Scope channel mapping
- Scope V/div and Time/div
- AWG frequency/amplitude
- Measurement limits
- Delays/timeouts

## Device naming

Automation should use logical fixture names rather than IP addresses:

- `PWS-1`
- `DMM-1`
- `AWG-1`
- `SCOPE-1`

Connection Manager maps these names to the current physical resources.

This allows a test sequence to move to another bench without changing the sequence.

## Result pipeline

Each run should create a session directory:

```text
results/
  2026-09-24_001_SN123/
    metadata.json
    result.csv
    result.json
    raw/
      scope_CH1.csv
      scope_CH2.csv
      dmm.csv
    screenshots/
```

Raw measurements should be retained separately from calculated PASS/FAIL results.
