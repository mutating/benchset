//! Immutable calibration of the unchanged scalar kernel, measured on a Pi 5.

/// Installed hardware capacity; memory is bytes, not Linux's usable MemTotal.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ReferenceMachine {
    pub name: &'static str,
    pub short_name: &'static str,
    pub os_name: &'static str,
    pub os_version: &'static str,
    pub cpu_architecture: &'static str,
    pub memory_bytes: u64,
    pub cpu_cores: u32,
}

/// One benchmark's sustained iterations/second, pooled across three repeats.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ReferenceBenchmarkResult {
    pub benchmark: &'static str,
    pub score: f64,
}

/// The six final calibration runs, from the first warmup to the last measurement.
/// Timestamp: UTC Unix seconds recorded on the Pi. Temperatures: Celsius.
/// Scores pool iterations / actual seconds across three repeats per benchmark.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ReferenceRun {
    /// UTC Unix seconds at the start of the series' first warmup.
    pub started_at_unix_seconds: f64,
    /// CPU temperature in Celsius at the start of the whole series.
    pub start_temperature_c: f64,
    /// CPU temperature in Celsius after the series' final measurement.
    pub end_temperature_c: f64,
    /// Seconds before each reference measurement; does not change runtime defaults.
    pub warmup_seconds: f64,
    /// One pooled score per benchmark, rather than one entry per repeat.
    pub results: &'static [ReferenceBenchmarkResult],
}

/// Fixed identity and installed hardware of the calibrated Raspberry Pi 5.
pub static PI_REFERENCE_MACHINE: ReferenceMachine = ReferenceMachine {
    name: "Raspberry Pi 5 Model B Rev 1.0",
    short_name: "pi",
    os_name: "Ubuntu Server",
    os_version: "26.04 LTS",
    cpu_architecture: "aarch64",
    memory_bytes: 8 * 1024 * 1024 * 1024,
    cpu_cores: 4,
};

/// Recorded final calibration series; scores are the Pi-equivalent denominators.
pub static PI_REFERENCE_RUN: ReferenceRun = ReferenceRun {
    started_at_unix_seconds: 1792767590.517391,
    start_temperature_c: 41.35,
    end_temperature_c: 66.65,
    warmup_seconds: 1248.0,
    results: &[
        ReferenceBenchmarkResult {
            benchmark: "single_core",
            score: 47231.227029007096,
        },
        ReferenceBenchmarkResult {
            benchmark: "multi_core",
            score: 181953.17419139884,
        },
    ],
};

impl crate::Benchmark {
    /// Sustained iterations/second on the reference machine for this workload.
    pub fn reference_score(self) -> f64 {
        PI_REFERENCE_RUN.results[match self {
            Self::SingleCore => 0,
            Self::MultiCore => 1,
        }]
        .score
    }
}
