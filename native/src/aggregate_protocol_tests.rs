use super::*;
use std::slice;

#[derive(Debug, PartialEq)]
struct Call {
    index: u64,
    field: u32,
    benchmark: u32,
    operand_status: u32,
    operand_bits: u64,
    presence: u32,
    name: Vec<u8>,
}

struct ReaderState {
    ratios: [f64; 2],
    log_ratios: Option<[f64; 2]>,
    presence: [u32; 2],
    log_presence: [u32; 2],
    presence_reads: [u32; 2],
    numeric_reads: [u32; 2],
    host_names: bool,
    name_matches: [u32; 2],
    nonnegative: [u32; 2],
    zero: [u32; 2],
    logarithms: [f64; 2],
    exponential: f64,
    reject: Option<(u64, u32)>,
    reject_at: Option<usize>,
    calls: Vec<Call>,
    invalid_request: bool,
}

impl Default for ReaderState {
    fn default() -> Self {
        Self {
            ratios: [0.5, 2.0],
            log_ratios: None,
            presence: [u32::MAX; 2],
            log_presence: [7; 2],
            presence_reads: [0; 2],
            numeric_reads: [0; 2],
            host_names: false,
            name_matches: [7, u32::MAX],
            nonnegative: [7, u32::MAX],
            zero: [0, 0],
            logarithms: [1.0, 3.0],
            exponential: 42.5,
            reject: None,
            reject_at: None,
            calls: Vec::new(),
            invalid_request: false,
        }
    }
}

unsafe extern "C" fn read(
    index: u64,
    field: u32,
    request: *mut CAggregateRequest,
    user: *mut c_void,
) -> i32 {
    let state = &mut *user.cast::<ReaderState>();
    let Some(request) = request.as_mut() else {
        state.invalid_request = true;
        return 0;
    };
    if index >= 2 || field > 7 || request.name.is_null() || request.name_size > 32 {
        state.invalid_request = true;
        return 0;
    }
    // Copy borrowed UTF-8 during the callback; no request pointer escapes it.
    let name = slice::from_raw_parts(request.name, request.name_size as usize).to_vec();
    state.calls.push(Call {
        index,
        field,
        benchmark: request.value.benchmark,
        operand_status: request.value.status,
        operand_bits: request.value.pi_equivalent.to_bits(),
        presence: request.value.has_pi_equivalent,
        name,
    });
    if state.reject == Some((index, field)) || state.reject_at == Some(state.calls.len() - 1) {
        return 0;
    }
    let index = index as usize;
    match field {
        0 => request.value.status = 0,
        1 => {
            if state.host_names {
                request.value.has_pi_equivalent = if state.presence_reads[index] == 0 {
                    state.presence[index]
                } else {
                    state.log_presence[index]
                };
                state.presence_reads[index] += 1;
            } else {
                request.value.pi_equivalent = state.ratios[index];
                request.value.has_pi_equivalent = state.presence[index];
            }
        }
        2 => request.value.has_pi_equivalent = state.nonnegative[index],
        3 => request.value.has_pi_equivalent = state.zero[index],
        4 => request.value.pi_equivalent = state.logarithms[index],
        5 => request.value.has_pi_equivalent = state.name_matches[index],
        6 => {
            request.value.pi_equivalent = if state.numeric_reads[index] == 0 {
                state.ratios[index]
            } else {
                state.log_ratios.unwrap_or(state.ratios)[index]
            };
            state.numeric_reads[index] += 1;
        }
        7 => request.value.pi_equivalent = state.exponential,
        _ => return 0,
    }
    -7
}

fn execute(state: &mut ReaderState, count: u64, host_names: u32) -> CAggregatedScores {
    execute_ids(state, count, host_names, [0, u32::MAX, 0])
}

fn execute_ids(
    state: &mut ReaderState,
    count: u64,
    host_names: u32,
    ids: [u32; 3],
) -> CAggregatedScores {
    #[repr(C)]
    struct Guarded {
        before: u64,
        scores: CAggregatedScores,
        after: u64,
    }
    let mut guarded = Guarded {
        before: 0x123456789abcdef0,
        scores: CAggregatedScores {
            pi_equivalent: 123.0,
            has_pi_equivalent: 7,
        },
        after: 0xfedcba9876543210,
    };
    state.host_names = host_names != 0;
    // Host-name mode must not reconstruct identity from these unrelated IDs.
    let result = unsafe {
        benchset_aggregate_read(
            ids.as_ptr(),
            count,
            Some(read),
            (state as *mut ReaderState).cast(),
            &mut guarded.scores,
            host_names,
        )
    };
    assert_eq!(result, 1);
    assert_eq!(guarded.before, 0x123456789abcdef0);
    assert_eq!(guarded.after, 0xfedcba9876543210);
    assert!(!state.invalid_request);
    guarded.scores
}

fn trace(fields: &[(u64, u32)], ratios: [f64; 2]) -> Vec<Call> {
    fields
        .iter()
        .map(|&(index, field)| {
            let operand = if field == 4 {
                ratios[index as usize]
            } else {
                0.0
            };
            Call {
                index,
                field,
                benchmark: 0,
                operand_status: 0,
                operand_bits: operand.to_bits(),
                presence: 0,
                name: if field == 5 {
                    [b"single_core".as_slice(), b"multi_core".as_slice()][index as usize].to_vec()
                } else {
                    Vec::new()
                },
            }
        })
        .collect()
}

const COMPLETE: [(u64, u32); 16] = [
    (0, 5),
    (1, 5),
    (0, 0),
    (0, 1),
    (0, 6),
    (0, 2),
    (1, 0),
    (1, 1),
    (1, 6),
    (1, 2),
    (0, 3),
    (1, 3),
    (0, 1),
    (0, 6),
    (1, 1),
    (1, 6),
];

/// Host names use exact borrowed UTF-8 and canonical operands, ignore raw IDs,
/// accept noncanonical true flags, and compare the common prefix before length.
/// Counts above 32 bits still compare both names and cannot match a short suite
/// after narrowing on a 32-bit target.
#[test]
fn host_names_preserve_operands_prefix_and_nonzero_flags() {
    for flag in [1, 7, u32::MAX] {
        let mut state = ReaderState {
            name_matches: [flag, flag],
            nonnegative: [flag, flag],
            ..ReaderState::default()
        };
        let scores = execute(&mut state, 2, u32::MAX);
        assert_eq!(scores.has_pi_equivalent, 1);
        assert_eq!(scores.pi_equivalent, 1.0);
        assert_eq!(state.calls, trace(&COMPLETE, state.ratios));
    }
    for count in [0, 1, 3, 1 << 32, (1 << 32) + 1, (1 << 32) + 2, u64::MAX] {
        let mut state = ReaderState::default();
        let scores = execute(&mut state, count, 1);
        assert_eq!(scores.has_pi_equivalent, 0);
        assert_eq!(scores.pi_equivalent, 0.0);
        assert_eq!(
            state.calls,
            trace(&COMPLETE[..count.min(2) as usize], state.ratios)
        );
    }
    let mut state = ReaderState::default();
    let scores = execute(&mut state, 2, 0);
    assert_eq!(scores.has_pi_equivalent, 0);
    assert!(state.calls.is_empty());
}

/// A false comparison and an unavailable host comparison each stop at that name,
/// without inspecting later identities, status, ratios or numeric operators.
#[test]
fn host_name_mismatch_and_reader_failure_stop_before_values() {
    for index in 0..2 {
        for failed_reader in [false, true] {
            let mut state = ReaderState::default();
            if failed_reader {
                state.reject = Some((index, 5));
            } else {
                state.name_matches[index as usize] = 0;
            }
            let scores = execute(&mut state, 2, 1);
            assert_eq!(scores.has_pi_equivalent, 0);
            assert_eq!(scores.pi_equivalent, 0.0);
            assert_eq!(
                state.calls,
                trace(&COMPLETE[..index as usize + 1], state.ratios)
            );
        }
    }
}

/// Nonfinite primitives stop before host >=; a false host >= is authoritative
/// even for positive primitives. Either position must suppress every later read.
#[test]
fn host_numeric_validation_preserves_short_circuiting() {
    for index in 0..2 {
        for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let mut state = ReaderState::default();
            state.ratios[index as usize] = invalid;
            let scores = execute(&mut state, 2, 1);
            let stop = COMPLETE
                .iter()
                .position(|entry| *entry == (index, 6))
                .unwrap()
                + 1;
            assert_eq!(scores.has_pi_equivalent, 0);
            assert_eq!(scores.pi_equivalent, 0.0);
            assert_eq!(state.calls, trace(&COMPLETE[..stop], state.ratios));
        }
        let mut state = ReaderState::default();
        state.nonnegative[index as usize] = 0;
        let scores = execute(&mut state, 2, 1);
        let stop = COMPLETE
            .iter()
            .position(|entry| *entry == (index, 2))
            .unwrap()
            + 1;
        assert_eq!(scores.has_pi_equivalent, 0);
        assert_eq!(scores.pi_equivalent, 0.0);
        assert_eq!(state.calls, trace(&COMPLETE[..stop], state.ratios));
    }
}

/// Host operators can admit nonpositive primitives. Forward successful host logs
/// in order, propagate either reader failure, and skip all logs after host zero.
#[test]
fn host_logs_forward_values_failures_and_short_circuit_order() {
    for failure in [None, Some(0), Some(1)] {
        let mut state = ReaderState {
            ratios: [0.0, -2.0],
            reject: failure.map(|index| (index, 4)),
            ..ReaderState::default()
        };
        let scores = execute(&mut state, 2, 1);
        let mut expected = COMPLETE[..14].to_vec();
        expected.push((0, 4));
        if failure != Some(0) {
            expected.extend_from_slice(&COMPLETE[14..]);
            expected.push((1, 4));
        }
        assert_eq!(state.calls, trace(&expected, state.ratios));
        assert_eq!(scores.has_pi_equivalent, u32::from(failure.is_none()));
        assert_eq!(
            scores.pi_equivalent,
            if failure.is_none() {
                2.0_f64.exp()
            } else {
                0.0
            }
        );
    }
    let mut state = ReaderState {
        ratios: [0.0, -2.0],
        zero: [u32::MAX, 0],
        ..ReaderState::default()
    };
    let scores = execute(&mut state, 2, 1);
    assert_eq!(scores.has_pi_equivalent, 1);
    assert_eq!(scores.pi_equivalent.to_bits(), 0.0_f64.to_bits());
    assert_eq!(state.calls, trace(&COMPLETE[..11], state.ratios));
}

/// Every requested host primitive is fallible, including the separate numeric
/// reads and repeated presence guards used only while computing logarithms.
#[test]
fn host_reader_failures_stop_at_each_requested_primitive() {
    for rejected in 0..=COMPLETE.len() {
        let mut state = ReaderState {
            reject_at: (rejected < COMPLETE.len()).then_some(rejected),
            ..ReaderState::default()
        };
        let scores = execute(&mut state, 2, 1);
        assert_eq!(
            state.calls,
            trace(
                &COMPLETE[..(rejected + 1).min(COMPLETE.len())],
                state.ratios
            )
        );
        assert_eq!(
            scores.has_pi_equivalent,
            u32::from(rejected == COMPLETE.len())
        );
        assert_eq!(
            scores.pi_equivalent,
            if rejected == COMPLETE.len() { 1.0 } else { 0.0 }
        );
    }
}

/// Presence checks do not convert numbers. A missing validation value stops
/// before its numeric getter; a missing log guard skips just that logarithm.
#[test]
fn host_presence_guards_skip_only_their_unused_numeric_reads() {
    for index in 0..2 {
        let mut state = ReaderState::default();
        state.presence[index] = 0;
        let scores = execute(&mut state, 2, 1);
        let stop = COMPLETE
            .iter()
            .position(|entry| *entry == (index as u64, 1))
            .unwrap()
            + 1;
        assert_eq!(scores.has_pi_equivalent, 0);
        assert_eq!(scores.pi_equivalent, 0.0);
        assert_eq!(state.calls, trace(&COMPLETE[..stop], state.ratios));

        let mut state = ReaderState::default();
        state.log_presence[index] = 0;
        let scores = execute(&mut state, 2, 1);
        let omitted = (index as u64, 6);
        let mut expected = COMPLETE[..12].to_vec();
        expected.extend(
            COMPLETE[12..]
                .iter()
                .copied()
                .filter(|entry| *entry != omitted),
        );
        assert_eq!(state.calls, trace(&expected, state.ratios));
        assert_eq!(scores.has_pi_equivalent, 1);
        assert_eq!(
            scores.pi_equivalent,
            (state.ratios[1 - index].ln() / 2.0).exp()
        );
    }
}

/// The logarithm consumes a fresh getter value after its guard, rather than the
/// finite primitive captured during validation, including fresh domain errors.
#[test]
fn host_logs_use_fresh_values_after_their_presence_guards() {
    let mut state = ReaderState {
        log_ratios: Some([2.0, 8.0]),
        ..ReaderState::default()
    };
    let scores = execute(&mut state, 2, 1);
    assert_eq!(state.calls, trace(&COMPLETE, state.ratios));
    assert_eq!(state.numeric_reads, [2, 2]);
    assert_eq!(state.presence_reads, [2, 2]);
    assert_eq!(scores.has_pi_equivalent, 1);
    assert_eq!(scores.pi_equivalent, 4.0);

    for index in 0..2 {
        let mut state = ReaderState {
            log_ratios: Some([0.0, -2.0]),
            reject: Some((index, 4)),
            ..ReaderState::default()
        };
        let scores = execute(&mut state, 2, 1);
        let mut expected = COMPLETE[..14].to_vec();
        expected.push((0, 4));
        if index == 1 {
            expected.extend_from_slice(&COMPLETE[14..]);
            expected.push((1, 4));
        }
        let mut expected_calls = trace(&expected, state.ratios);
        for call in &mut expected_calls {
            if call.field == 4 {
                let operand = state.log_ratios.unwrap()[call.index as usize];
                call.operand_status = 0;
                call.operand_bits = operand.to_bits();
            }
        }
        assert_eq!(state.calls, expected_calls);
        assert_eq!(scores.has_pi_equivalent, 0);
        assert_eq!(scores.pi_equivalent, 0.0);
    }
}

fn overflowing_log_trace(exponent: f64) -> Vec<Call> {
    let mut fields = COMPLETE[..14].to_vec();
    fields.push((0, 4));
    fields.extend_from_slice(&COMPLETE[14..]);
    fields.push((1, 4));
    let mut calls = trace(&fields, [0.0, -2.0]);
    calls.push(Call {
        index: 0,
        field: 7,
        benchmark: 0,
        operand_status: 0,
        operand_bits: exponent.to_bits(),
        presence: 0,
        name: Vec::new(),
    });
    calls
}

/// A finite overflowing exponent requests one host primitive after every log,
/// carrying the actual mean and preserving the host's returned double exactly.
#[test]
fn host_exponential_overflow_requests_its_operand_and_forwards_the_primitive() {
    for returned in [0.0, -0.0, 42.5, f64::INFINITY, f64::NAN] {
        let mut state = ReaderState {
            ratios: [0.0, -2.0],
            logarithms: [1200.0, 1800.0],
            exponential: returned,
            ..ReaderState::default()
        };
        let scores = execute(&mut state, 2, 1);
        assert_eq!(state.calls, overflowing_log_trace(1500.0));
        assert_eq!(scores.has_pi_equivalent, 1);
        assert_eq!(scores.pi_equivalent.to_bits(), returned.to_bits());
    }
}

/// An unavailable overflow primitive must suppress the score without extra host
/// calls, so a Python exception survives until its own callback bridge rethrows it.
#[test]
fn host_exponential_reader_failure_keeps_the_aggregate_unavailable() {
    let mut state = ReaderState {
        ratios: [0.0, -2.0],
        logarithms: [1200.0, 1800.0],
        reject: Some((0, 7)),
        ..ReaderState::default()
    };
    let scores = execute(&mut state, 2, 1);
    assert_eq!(state.calls, overflowing_log_trace(1500.0));
    assert_eq!(scores.has_pi_equivalent, 0);
    assert_eq!(scores.pi_equivalent, 0.0);
}

/// Nonfinite exponents, finite underflow and raw-reader mode retain Rust exp
/// semantics; none may ask for Python's finite-overflow exception primitive.
#[test]
fn exponential_translation_is_only_for_finite_host_overflow() {
    for fresh in [f64::INFINITY, f64::NAN] {
        let mut state = ReaderState {
            log_ratios: Some([fresh, 1.0]),
            ..ReaderState::default()
        };
        let scores = execute(&mut state, 2, 1);
        assert_eq!(state.calls, trace(&COMPLETE, state.ratios));
        assert_eq!(scores.has_pi_equivalent, 1);
        if fresh.is_nan() {
            assert!(scores.pi_equivalent.is_nan());
        } else {
            assert_eq!(scores.pi_equivalent, f64::INFINITY);
        }
    }
    let raw_fields = [
        (0, 0),
        (0, 1),
        (0, 2),
        (1, 0),
        (1, 1),
        (1, 2),
        (0, 3),
        (1, 3),
        (0, 4),
        (1, 4),
    ];
    for host in [0, 1] {
        for logarithm in [-1500.0, 1500.0] {
            if host != 0 && logarithm > 0.0 {
                continue;
            }
            let mut state = ReaderState {
                ratios: [0.0, -2.0],
                logarithms: [logarithm; 2],
                ..ReaderState::default()
            };
            let scores = execute_ids(&mut state, 2, host, [1, 2, 0]);
            let expected_calls = if host == 0 {
                trace(&raw_fields, state.ratios)
            } else {
                let mut calls = overflowing_log_trace(logarithm);
                calls.pop();
                calls
            };
            assert_eq!(state.calls, expected_calls);
            assert_eq!(scores.has_pi_equivalent, 1);
            assert_eq!(scores.pi_equivalent, logarithm.exp());
        }
    }
}
