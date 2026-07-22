# Use AMD-native thermal evidence for the local profile

The `local-7800x3d` profile requires a readable CPU temperature from a recognized Linux
hwmon driver and enforces both a maximum 80 °C temperature and a 3.99–4.41 GHz execution
frequency envelope. Critical-temperature alarms and per-core throttle counters are recorded
and enforced when the kernel exposes them, but their absence does not invalidate a Trial,
because the Ryzen 7 7800X3D `k10temp` driver does not expose Intel's per-core
`thermal_throttle` counter files. This keeps thermal validity runnable and reproducible on
the named AMD host without weakening the fixed temperature and frequency boundaries.
