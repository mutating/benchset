use benchset::{
    run, Benchmark, Cancellation, Options, ReferenceMachine, ReferenceRun, Status,
    PI_REFERENCE_MACHINE, PI_REFERENCE_RUN,
};
use std::time::Duration;

fn main() {
    let cancellation = Cancellation::default();
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::from_millis(100),
            measurement: Duration::from_millis(200),
        },
        &cancellation,
        |_| {},
    );
    let machine: &'static ReferenceMachine = result.reference_machine;
    let reference: &'static ReferenceRun = result.reference_run;
    assert!(std::ptr::eq(machine, &PI_REFERENCE_MACHINE));
    assert!(std::ptr::eq(reference, &PI_REFERENCE_RUN));
    println!("{result:#?}");
    assert_eq!(
        result.pi_equivalent,
        result
            .score
            .map(|score| score / Benchmark::SingleCore.reference_score()),
    );
    if result.status == Status::Completed {
        assert!(result.score.unwrap() > 0.0);
    } else {
        assert_eq!(result.status, Status::Failed);
        assert_eq!(
            result.reason,
            "measurement window was too short to complete any work"
        );
        assert!(result.iterations == 0 || result.elapsed_seconds == 0.0);
        assert!(result.score.is_none());
        assert!(result.pi_equivalent.is_none());
    }
    drop(result);
    drop(cancellation);
    println!(
        "Reference: {} ({}, {} {}), {} bytes, {} cores; warmup {} s",
        machine.name,
        machine.short_name,
        machine.os_name,
        machine.os_version,
        machine.memory_bytes,
        machine.cpu_cores,
        reference.warmup_seconds,
    );
}
