// Shared public benchmark identities, suite order and measurement defaults.

pub const DEFAULT_WARMUP_SECONDS: f64 = 120.0;
pub const DEFAULT_MEASUREMENT_SECONDS: f64 = 60.0;
pub const BENCHMARKS: [Benchmark; 2] = [Benchmark::SingleCore, Benchmark::MultiCore];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u32)]
pub enum Benchmark {
    SingleCore = 1,
    MultiCore = 2,
}

impl Benchmark {
    pub fn name(self) -> &'static str {
        match self {
            Self::SingleCore => "single_core",
            Self::MultiCore => "multi_core",
        }
    }

    pub(crate) fn from_id(id: u32) -> Option<Self> {
        BENCHMARKS
            .into_iter()
            .find(|benchmark| *benchmark as u32 == id)
    }

    pub(crate) fn from_name(name: &str) -> Option<Self> {
        BENCHMARKS
            .into_iter()
            .find(|benchmark| benchmark.name() == name)
    }
}
