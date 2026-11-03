use super::*;
use crate::{Benchmark, Status};
use std::ptr;

fn input(benchmark: Benchmark, ratio: f64) -> CAggregateInput {
    CAggregateInput {
        benchmark: benchmark as u32,
        status: Status::Completed as u32,
        pi_equivalent: ratio,
        has_pi_equivalent: 1,
    }
}

struct ReaderState {
    values: [CAggregateInput; 2],
    calls: Vec<(u64, u32)>,
    reject: Option<(u64, u32)>,
    accepted: i32,
}

unsafe extern "C" fn read(
    index: u64,
    field: u32,
    out: *mut CAggregateRequest,
    user: *mut c_void,
) -> i32 {
    let state = &mut *user.cast::<ReaderState>();
    state.calls.push((index, field));
    if state.reject == Some((index, field)) || index >= 2 || field > 3 {
        return 0;
    }
    let mut value = state.values[index as usize];
    if field >= 2 {
        let operand = f64::from((*out).value.status);
        let matches = if field == 2 {
            value.pi_equivalent >= operand
        } else {
            value.pi_equivalent == operand
        };
        value.has_pi_equivalent = if matches { value.has_pi_equivalent } else { 0 };
    }
    (*out).value = value;
    state.accepted
}

/// The core requests status/ratio/operators in order, stops before unused host fields,
/// and treats failed readers like unavailable values without overriding identities.
/// All nonzero reader returns and presence flags, including negative returns, are accepted.
#[test]
fn aggregate_reader_preserves_lazy_order_and_failures() {
    unsafe {
        let names = [Benchmark::SingleCore as u32, Benchmark::MultiCore as u32];
        let expected = [
            (0, 0),
            (0, 1),
            (0, 2),
            (1, 0),
            (1, 1),
            (1, 2),
            (0, 3),
            (1, 3),
        ];
        for rejected in 0..=expected.len() {
            let mut state = ReaderState {
                values: [
                    input(Benchmark::MultiCore, 0.5),
                    input(Benchmark::SingleCore, 2.0),
                ],
                calls: Vec::new(),
                reject: expected.get(rejected).copied(),
                accepted: -1,
            };
            state.values[0].has_pi_equivalent = 7;
            state.values[1].has_pi_equivalent = u32::MAX;
            let mut out = CAggregatedScores {
                pi_equivalent: 123.0,
                has_pi_equivalent: 7,
            };
            assert_eq!(
                benchset_aggregate_read(
                    names.as_ptr(),
                    2,
                    Some(read),
                    (&mut state as *mut ReaderState).cast(),
                    &mut out,
                    0
                ),
                1
            );
            assert_eq!(state.calls, expected[..(rejected + 1).min(expected.len())]);
            assert_eq!(out.has_pi_equivalent, u32::from(rejected == expected.len()));
            assert_eq!(
                out.pi_equivalent,
                if rejected == expected.len() { 1.0 } else { 0.0 }
            );
        }
        for field in 0..=1 {
            for index in 0..2 {
                let mut state = ReaderState {
                    values: [
                        input(Benchmark::SingleCore, 0.5),
                        input(Benchmark::MultiCore, 2.0),
                    ],
                    calls: Vec::new(),
                    reject: None,
                    accepted: 7,
                };
                if field == 0 {
                    state.values[index].status = Status::Cancelled as u32;
                } else {
                    state.values[index].has_pi_equivalent = 0;
                }
                let mut out = CAggregatedScores {
                    pi_equivalent: 123.0,
                    has_pi_equivalent: 7,
                };
                assert_eq!(
                    benchset_aggregate_read(
                        names.as_ptr(),
                        2,
                        Some(read),
                        (&mut state as *mut ReaderState).cast(),
                        &mut out,
                        0
                    ),
                    1
                );
                assert_eq!(out.has_pi_equivalent, 0);
                let stop = expected
                    .iter()
                    .position(|entry| *entry == (index as u64, field))
                    .unwrap()
                    + 1;
                assert_eq!(state.calls, expected[..stop]);
            }
        }
    }
}

/// Malformed pointers/readers leave output intact; incomplete or unordered IDs
/// never invoke host code, including maximum counts and a valid empty input.
#[test]
fn aggregate_reader_pointer_count_and_identity_contract() {
    unsafe {
        let names = [Benchmark::MultiCore as u32, Benchmark::SingleCore as u32];
        let mut out = CAggregatedScores {
            pi_equivalent: 123.0,
            has_pi_equivalent: 7,
        };
        for count in [0, 1, 2, 3, u64::MAX] {
            assert_eq!(
                benchset_aggregate_read(names.as_ptr(), count, None, ptr::null_mut(), &mut out, 0),
                0
            );
            assert_eq!(
                benchset_aggregate_read(
                    names.as_ptr(),
                    count,
                    Some(read),
                    ptr::null_mut(),
                    ptr::null_mut(),
                    0
                ),
                0
            );
            assert_eq!(out.pi_equivalent, 123.0);
            assert_eq!(out.has_pi_equivalent, 7);
        }
        for count in [1, 2, 3, u64::MAX] {
            assert_eq!(
                benchset_aggregate_read(
                    ptr::null(),
                    count,
                    Some(read),
                    ptr::null_mut(),
                    &mut out,
                    0
                ),
                0
            );
            assert_eq!(out.pi_equivalent, 123.0);
        }
        for count in [0, 1, 2, 3, u64::MAX] {
            assert_eq!(
                benchset_aggregate_read(
                    names.as_ptr(),
                    count,
                    Some(read),
                    ptr::null_mut(),
                    &mut out,
                    0
                ),
                1
            );
            assert_eq!(out.has_pi_equivalent, 0);
        }
        assert_eq!(
            benchset_aggregate_read(ptr::null(), 0, Some(read), ptr::null_mut(), &mut out, 0),
            1
        );
    }
}

/// Keeps empty/incomplete counts safe, bounds raw input reads and leaves output intact on invalid pointers.
#[test]
fn aggregate_pointer_and_count_contract() {
    unsafe {
        #[repr(C)]
        struct Guarded {
            before: u64,
            out: CAggregatedScores,
            after: u64,
        }
        let mut guarded = Guarded {
            before: 0x123456789abcdef0,
            out: CAggregatedScores {
                pi_equivalent: 123.0,
                has_pi_equivalent: 7,
            },
            after: 0xfedcba9876543210,
        };
        let out = &mut guarded.out;
        for count in [1, 2, 3, u64::MAX] {
            assert_eq!(benchset_aggregate(ptr::null(), count, out), 0);
            assert_eq!(out.pi_equivalent, 123.0);
            assert_eq!(out.has_pi_equivalent, 7);
        }
        let one = input(Benchmark::SingleCore, 0.5);
        for count in [0, 1, 2, 3, u64::MAX] {
            assert_eq!(benchset_aggregate(&one, count, ptr::null_mut()), 0);
        }
        for count in [0, 1, 3, u64::MAX] {
            assert_eq!(benchset_aggregate(&one, count, out), 1);
            assert_eq!(out.has_pi_equivalent, 0);
        }
        assert_eq!(benchset_aggregate(ptr::null(), 0, out), 1);
        assert_eq!(out.has_pi_equivalent, 0);
        assert_eq!(guarded.before, 0x123456789abcdef0);
        assert_eq!(guarded.after, 0xfedcba9876543210);
    }
}

/// Preserves every nonzero presence flag and rejects raw unknown IDs/statuses or missing ratios at either position.
/// Shared core tests cover numerical boundaries; this matrix guards the C-to-Rust field interpretation.
#[test]
fn aggregate_ffi_preserves_raw_fields_and_presence() {
    unsafe {
        let original = [
            input(Benchmark::SingleCore, 0.5),
            input(Benchmark::MultiCore, 8.0),
        ];
        let mut out = CAggregatedScores {
            pi_equivalent: 0.0,
            has_pi_equivalent: 0,
        };
        for flag in [1, 7, u32::MAX] {
            let inputs = original.map(|mut value| {
                value.has_pi_equivalent = flag;
                value
            });
            assert_eq!(benchset_aggregate(inputs.as_ptr(), 2, &mut out), 1);
            assert_eq!(out.has_pi_equivalent, 1);
            assert!((out.pi_equivalent - 2.0).abs() < 1e-15);
        }
        for position in 0..2 {
            for invalid in 0..4 {
                let mut inputs = original;
                match invalid {
                    0 => inputs[position].benchmark = 0,
                    1 => inputs[position].benchmark = u32::MAX,
                    2 => inputs[position].status = u32::MAX,
                    _ => inputs[position].has_pi_equivalent = 0,
                }
                assert_eq!(benchset_aggregate(inputs.as_ptr(), 2, &mut out), 1);
                assert_eq!(out.has_pi_equivalent, 0);
            }
        }
    }
}
