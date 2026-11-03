//! Export immutable package data from the same Rust definitions as the runtime.
#![allow(dead_code)]

#[path = "src/catalog.rs"]
mod catalog;
#[path = "src/reference.rs"]
mod reference;
pub use catalog::Benchmark;

use std::{env, fmt::Write, fs, path::PathBuf};

fn quoted(value: &str) -> String {
    let mut output = String::from("\"");
    for character in value.chars() {
        match character {
            '"' => output.push_str("\\\""),
            '\\' => output.push_str("\\\\"),
            '\u{0}'..='\u{1f}' => write!(output, "\\u{:04x}", character as u32).unwrap(),
            _ => output.push(character),
        }
    }
    output.push('"');
    output
}

fn main() {
    let machine = &reference::PI_REFERENCE_MACHINE;
    let run = &reference::PI_REFERENCE_RUN;
    let benchmarks = catalog::BENCHMARKS
        .map(|benchmark| {
            format!(
                "{{\"id\":{},\"name\":{}}}",
                benchmark as u32,
                quoted(benchmark.name())
            )
        })
        .join(",");
    let results = run
        .results
        .iter()
        .map(|result| {
            format!(
                "{{\"benchmark\":{},\"score\":{:?}}}",
                quoted(result.benchmark),
                result.score
            )
        })
        .collect::<Vec<_>>()
        .join(",");
    let snapshot = format!(concat!(
        "{{\"benchmarks\":[{}],\"default_warmup_seconds\":{:?},\"default_measurement_seconds\":{:?},",
        "\"reference_machine\":{{\"name\":{},\"short_name\":{},\"os_name\":{},\"os_version\":{},",
        "\"cpu_architecture\":{},\"memory_bytes\":{},\"cpu_cores\":{}}},",
        "\"reference_run\":{{\"started_at_unix_seconds\":{:?},\"start_temperature_c\":{:?},",
        "\"end_temperature_c\":{:?},\"warmup_seconds\":{:?},\"results\":[{}]}}}}\n"
    ), benchmarks, catalog::DEFAULT_WARMUP_SECONDS, catalog::DEFAULT_MEASUREMENT_SECONDS,
       quoted(machine.name), quoted(machine.short_name), quoted(machine.os_name), quoted(machine.os_version),
       quoted(machine.cpu_architecture), machine.memory_bytes, machine.cpu_cores,
       run.started_at_unix_seconds, run.start_temperature_c, run.end_temperature_c, run.warmup_seconds, results);
    let path = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("benchset-metadata.json");
    fs::write(&path, snapshot).unwrap();
    let mut encoded_path = String::new();
    for byte in path.as_os_str().as_encoded_bytes() {
        write!(encoded_path, "{byte:02x}").unwrap();
    }
    println!("cargo:rustc-env=BENCHSET_METADATA_PATH_HEX={encoded_path}");
    println!("cargo:rerun-if-changed=native/src/catalog.rs");
    println!("cargo:rerun-if-changed=native/src/reference.rs");
}
