"""Named host profile enforcement and execution evidence."""

from __future__ import annotations

import os
import platform
import re
from pathlib import Path
from typing import Any, Sequence


LOCAL_RESOURCE_PROFILE: dict[str, Any] = {
    "id": "local-7800x3d",
    "cpuModel": "AMD Ryzen 7 7800X3D",
    "contender": {
        "cpusetCpus": "0-3",
        "memoryBytes": 8 * 1024**3,
        "memorySwapBytes": 8 * 1024**3,
    },
    "postgres": {
        "cpusetCpus": "4-5",
        "memoryBytes": 8 * 1024**3,
        "memorySwapBytes": 8 * 1024**3,
    },
    "k6": {
        "cpusetCpus": "6-7",
        "memoryBytes": 4 * 1024**3,
        "memorySwapBytes": 4 * 1024**3,
    },
    "hostTolerances": {
        "maximumLoad1": 1.0,
        "minimumFrequencyKHz": 3_990_000,
        "maximumFrequencyKHz": 4_410_000,
        "maximumTemperatureMilliCelsius": 80_000,
    },
}

_ROLE_NAMES = ("contender", "postgres", "k6")
_CPU_HWMON_DRIVERS = {"coretemp", "k10temp", "zenpower", "cpu_thermal"}


def host_identity() -> dict[str, Any]:
    """Return stable host fields shared by fingerprints and resume validation."""
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "memoryBytes": os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"),
    }


def _parse_cpu_set(value: str) -> list[int]:
    cpus: list[int] = []
    for part in value.split(","):
        bounds = part.strip().split("-", 1)
        if len(bounds) == 1:
            cpus.append(int(bounds[0]))
        else:
            cpus.extend(range(int(bounds[0]), int(bounds[1]) + 1))
    return cpus


def profile_cpus(profile: dict[str, Any]) -> list[int]:
    """Return every logical CPU assigned by a resource profile."""
    return [
        cpu
        for role in _ROLE_NAMES
        for cpu in _parse_cpu_set(str(profile[role]["cpusetCpus"]))
    ]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _read_int(path: Path) -> int | None:
    value = _read_text(path)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _cpu_model(cpuinfo: Path) -> str | None:
    contents = _read_text(cpuinfo)
    if contents is None:
        return None
    match = re.search(r"^model name\s*:\s*(.+)$", contents, re.MULTILINE)
    return match.group(1).strip() if match else None


def _temperature_evidence(hwmon_root: Path) -> tuple[int | None, bool]:
    temperatures: list[int] = []
    alarms: list[int] = []
    try:
        devices = sorted(hwmon_root.glob("hwmon*"))
    except OSError:
        devices = []
    for device in devices:
        driver = _read_text(device / "name")
        if driver not in _CPU_HWMON_DRIVERS:
            continue
        for path in sorted(device.glob("temp*_input")):
            value = _read_int(path)
            if value is not None:
                temperatures.append(value)
        for path in sorted(device.glob("temp*_crit_alarm")):
            value = _read_int(path)
            if value is not None:
                alarms.append(value)
    return (
        max(temperatures) if temperatures else None,
        any(value != 0 for value in alarms) if alarms else False,
    )


def capture_host_observation(
    profile: dict[str, Any] = LOCAL_RESOURCE_PROFILE,
    *,
    cpu_root: Path = Path("/sys/devices/system/cpu"),
    hwmon_root: Path = Path("/sys/class/hwmon"),
    cpuinfo: Path = Path("/proc/cpuinfo"),
) -> dict[str, Any]:
    """Capture one auditable observation from Linux host authorities."""
    cpus = profile_cpus(profile)
    governors: dict[str, str] = {}
    siblings: dict[str, list[int]] = {}
    frequencies: dict[str, int] = {}
    throttle_counts: dict[str, int] = {}
    for cpu in cpus:
        cpu_path = cpu_root / f"cpu{cpu}"
        governor = _read_text(cpu_path / "cpufreq" / "scaling_governor")
        if governor is not None:
            governors[str(cpu)] = governor
        sibling_list = _read_text(cpu_path / "topology" / "thread_siblings_list")
        if sibling_list is not None:
            try:
                siblings[str(cpu)] = _parse_cpu_set(sibling_list)
            except ValueError:
                pass
        frequency = _read_int(cpu_path / "cpufreq" / "scaling_cur_freq")
        if frequency is not None:
            frequencies[str(cpu)] = frequency
        throttle_paths = sorted((cpu_path / "thermal_throttle").glob("*_throttle_count"))
        values = [value for path in throttle_paths if (value := _read_int(path)) is not None]
        if values:
            throttle_counts[str(cpu)] = sum(values)

    boost_value = None
    for boost_path in (
        cpu_root / "cpufreq" / "boost",
        cpu_root / "cpu0" / "cpufreq" / "boost",
    ):
        boost_value = _read_int(boost_path)
        if boost_value is not None:
            break
    temperature, thermal_alarm = _temperature_evidence(hwmon_root)
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None
    return {
        "cpuModel": _cpu_model(cpuinfo),
        "logicalCpuCount": os.cpu_count(),
        "load1": load1,
        "boost": {
            "observed": boost_value is not None,
            "enabled": bool(boost_value) if boost_value is not None else None,
        },
        "governors": governors,
        "threadSiblings": siblings,
        "frequenciesKHz": frequencies,
        "temperatureMilliCelsius": temperature,
        "temperatureObserved": temperature is not None,
        "thermalThrottlingActive": thermal_alarm,
        "thermalThrottlingObserved": len(throttle_counts) == len(cpus),
        "thermalThrottleCounts": throttle_counts,
    }


def _physical_core_isolation(profile: dict[str, Any], observation: dict[str, Any]) -> bool:
    cpus = profile_cpus(profile)
    if len(cpus) != len(set(cpus)):
        return False
    siblings = observation.get("threadSiblings", {})
    groups: list[frozenset[int]] = []
    for cpu in cpus:
        try:
            group = frozenset(int(value) for value in siblings[str(cpu)])
        except (KeyError, TypeError, ValueError):
            return False
        if not group or cpu not in group or group in groups:
            return False
        groups.append(group)
    return True


def assess_host_preflight(
    profile: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    """Evaluate whether the named host is safe to begin official measurement."""
    cpus = profile_cpus(profile)
    tolerance = profile["hostTolerances"]
    frequencies = observation.get("frequenciesKHz", {})
    temperature = observation.get("temperatureMilliCelsius")
    boost = observation.get("boost", {})
    checks = {
        "cpuModel": profile["cpuModel"] in str(observation.get("cpuModel") or ""),
        "logicalCpuAvailability": (
            isinstance(observation.get("logicalCpuCount"), int)
            and observation["logicalCpuCount"] > max(cpus)
        ),
        "physicalCoreIsolation": _physical_core_isolation(profile, observation),
        "quiescent": (
            isinstance(observation.get("load1"), (int, float))
            and observation["load1"] <= tolerance["maximumLoad1"]
        ),
        "performanceGovernor": all(
            observation.get("governors", {}).get(str(cpu)) == "performance" for cpu in cpus
        ),
        "dynamicBoostDisabled": boost.get("observed") is True and boost.get("enabled") is False,
        "frequency": all(
            isinstance(frequencies.get(str(cpu)), int)
            and tolerance["minimumFrequencyKHz"]
            <= frequencies[str(cpu)]
            <= tolerance["maximumFrequencyKHz"]
            for cpu in cpus
        ),
        "temperatureSignal": observation.get("temperatureObserved") is True,
        "temperature": (
            observation.get("temperatureObserved") is not True
            or (
                isinstance(temperature, int)
                and temperature <= tolerance["maximumTemperatureMilliCelsius"]
            )
        ),
        "noThermalThrottling": observation.get("thermalThrottlingActive") is False,
    }
    reason_by_check = {
        "cpuModel": "host_cpu_model_mismatch",
        "logicalCpuAvailability": "profile_cpu_unavailable",
        "physicalCoreIsolation": "cpu_profile_uses_smt_siblings",
        "quiescent": "host_not_quiescent",
        "performanceGovernor": "performance_governor_required",
        "dynamicBoostDisabled": "dynamic_boost_enabled",
        "frequency": "cpu_frequency_out_of_tolerance",
        "temperatureSignal": "cpu_temperature_observation_missing",
        "temperature": "cpu_temperature_out_of_tolerance",
        "noThermalThrottling": "thermal_throttling_active",
    }
    reasons = [reason_by_check[name] for name, passed in checks.items() if not passed]
    return {
        "profile": profile["id"],
        "valid": not reasons,
        "reasons": reasons,
        "checks": checks,
        "observation": observation,
    }


def summarize_host_execution(
    profile: dict[str, Any], observations: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Summarize frequency, temperature, and throttling evidence for one Trial."""
    cpus = profile_cpus(profile)
    tolerance = profile["hostTolerances"]
    frequencies = [
        sample.get("frequenciesKHz", {}).get(str(cpu))
        for sample in observations
        for cpu in cpus
    ]
    temperatures = [sample.get("temperatureMilliCelsius") for sample in observations]
    complete = (
        bool(observations)
        and all(isinstance(value, int) for value in frequencies)
        and all(isinstance(value, int) for value in temperatures)
    )
    numeric_frequencies = [int(value) for value in frequencies if isinstance(value, int)]
    numeric_temperatures = [int(value) for value in temperatures if isinstance(value, int)]
    first_counts = observations[0].get("thermalThrottleCounts", {}) if observations else {}
    last_counts = observations[-1].get("thermalThrottleCounts", {}) if observations else {}
    increments = {
        str(cpu): int(last_counts[str(cpu)]) - int(first_counts[str(cpu)])
        for cpu in cpus
        if str(cpu) in first_counts
        and str(cpu) in last_counts
        and int(last_counts[str(cpu)]) > int(first_counts[str(cpu)])
    }
    throttle_counters_observed = all(
        sample.get("thermalThrottlingObserved") is True for sample in observations
    )
    reasons: list[str] = []
    if not complete:
        reasons.append("host_execution_evidence_missing")
    if numeric_frequencies and (
        min(numeric_frequencies) < tolerance["minimumFrequencyKHz"]
        or max(numeric_frequencies) > tolerance["maximumFrequencyKHz"]
    ):
        reasons.append("cpu_frequency_out_of_tolerance")
    if numeric_temperatures and max(numeric_temperatures) > tolerance["maximumTemperatureMilliCelsius"]:
        reasons.append("cpu_temperature_out_of_tolerance")
    if increments or any(sample.get("thermalThrottlingActive") is True for sample in observations):
        reasons.append("thermal_throttling_observed")
    return {
        "profile": profile["id"],
        "observed": complete,
        "sampleCount": len(observations),
        "valid": not reasons,
        "reasons": reasons,
        "thermalThrottleCountersObserved": throttle_counters_observed,
        "frequencyKHz": {
            "average": (
                sum(numeric_frequencies) / len(numeric_frequencies)
                if numeric_frequencies
                else None
            ),
            "minimum": min(numeric_frequencies) if numeric_frequencies else None,
            "maximum": max(numeric_frequencies) if numeric_frequencies else None,
        },
        "temperatureMilliCelsius": {
            "average": (
                sum(numeric_temperatures) / len(numeric_temperatures)
                if numeric_temperatures
                else None
            ),
            "peak": max(numeric_temperatures) if numeric_temperatures else None,
        },
        "thermalThrottleIncrements": increments,
    }
