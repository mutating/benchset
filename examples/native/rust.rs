use benchset::{run, Benchmark, Cancellation, Options};
use std::time::Duration;

fn main() {
    let result = run(
        Benchmark::SingleCore,
        Options {
            warmup: Duration::from_millis(100),
            measurement: Duration::from_millis(200),
        },
        &Cancellation::default(),
        |_| {},
    );
    println!("{result:#?}");
}
