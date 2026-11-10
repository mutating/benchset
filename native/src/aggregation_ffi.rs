use super::suite::CAggregatedScores;
use crate::{aggregate, AggregateOperator, AggregateValue, BENCHMARKS};
use std::ffi::c_void;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct CAggregateInput {
    pub benchmark: u32,
    pub status: u32,
    pub pi_equivalent: f64,
    pub has_pi_equivalent: u32,
}

/// Aggregate caller-owned snapshots using the same rules as Rust SuiteResult.
#[no_mangle]
pub unsafe extern "C" fn benchset_aggregate(
    inputs: *const CAggregateInput,
    count: u64,
    out: *mut CAggregatedScores,
) -> i32 {
    if out.is_null() || (inputs.is_null() && count != 0) {
        return 0;
    }
    // Only a complete suite needs reading; this also bounds the raw slice.
    let equivalent = if count == BENCHMARKS.len() as u64 {
        aggregate(
            count as usize,
            |index, expected| (unsafe { (*inputs.add(index)).benchmark }) == expected as u32,
            |index| unsafe { (*inputs.add(index)).status },
            |index| {
                let value = unsafe { &*inputs.add(index) };
                (value.has_pi_equivalent != 0).then_some(value.pi_equivalent)
            },
        )
    } else {
        None
    };
    unsafe { write_scores(out, equivalent) }
}

unsafe fn write_scores(out: *mut CAggregatedScores, equivalent: Option<f64>) -> i32 {
    let scores = CAggregatedScores {
        pi_equivalent: equivalent.unwrap_or(0.0),
        has_pi_equivalent: u32::from(equivalent.is_some()),
    };
    unsafe { out.write(scores) };
    1
}

#[repr(C)]
pub struct CAggregateRequest {
    pub value: CAggregateInput,
    pub name: *const u8,
    pub name_size: u64,
}

pub type AggregateReader =
    Option<unsafe extern "C" fn(u64, u32, *mut CAggregateRequest, *mut c_void) -> i32>;

type ReadValue<'a> = dyn Fn(usize, u32, f64, &str) -> Option<CAggregateInput> + 'a;

struct ReadRatio<'a> {
    index: usize,
    number: f64,
    host: bool,
    read: &'a ReadValue<'a>,
}

impl AggregateValue for ReadRatio<'_> {
    fn number(&self) -> f64 {
        self.number
    }

    fn compare(&self, operator: AggregateOperator, operand: u32) -> Option<bool> {
        (self.read)(self.index, operator as u32, f64::from(operand), "")
            .map(|value| value.has_pi_equivalent != 0)
    }

    fn logarithm(&self) -> Option<f64> {
        let number = if self.host {
            if (self.read)(self.index, 1, 0.0, "")?.has_pi_equivalent == 0 {
                return Some(0.0);
            }
            (self.read)(self.index, 6, 0.0, "")?.pi_equivalent
        } else {
            self.number
        };
        if number <= 0.0 {
            (self.read)(self.index, 4, number, "").map(|value| value.pi_equivalent)
        } else {
            Some(number.ln())
        }
    }

    fn exponential(&self, exponent: f64) -> Option<f64> {
        let value = exponent.exp();
        if self.host && crate::finite(exponent) && value.is_infinite() {
            (self.read)(self.index, 7, exponent, "").map(|value| value.pi_equivalent)
        } else {
            Some(value)
        }
    }
}

/// Request host primitives only in the order selected by the shared calculator.
/// Host mode preserves positional tuple equality, including its common prefix.
#[no_mangle]
pub unsafe extern "C" fn benchset_aggregate_read(
    benchmarks: *const u32,
    count: u64,
    reader: AggregateReader,
    user: *mut c_void,
    out: *mut CAggregatedScores,
    host_names: u32,
) -> i32 {
    let Some(reader) = reader else { return 0 };
    if out.is_null() || (benchmarks.is_null() && count != 0) {
        return 0;
    }
    if host_names == 0 && count != BENCHMARKS.len() as u64 {
        return unsafe { write_scores(out, None) };
    }
    let read = |index: usize, field, operand, name: &str| {
        let mut request = CAggregateRequest {
            value: CAggregateInput {
                benchmark: 0,
                status: 0,
                pi_equivalent: operand,
                has_pi_equivalent: 0,
            },
            name: name.as_ptr(),
            name_size: name.len() as u64,
        };
        let success = unsafe { reader(index as u64, field, &mut request, user) };
        (success != 0).then_some(request.value)
    };
    let equivalent = aggregate(
        usize::try_from(count).unwrap_or(usize::MAX),
        |index, expected| {
            if host_names == 0 {
                (unsafe { *benchmarks.add(index) }) == expected as u32
            } else {
                read(index, 5, 0.0, expected.name())
                    .is_some_and(|value| value.has_pi_equivalent != 0)
            }
        },
        |index| read(index, 0, 0.0, "").map_or(u32::MAX, |value| value.status),
        |index| {
            let value = read(index, 1, 0.0, "")?;
            if value.has_pi_equivalent == 0 {
                return None;
            }
            Some(ReadRatio {
                index,
                number: if host_names != 0 {
                    read(index, 6, 0.0, "")?.pi_equivalent
                } else {
                    value.pi_equivalent
                },
                host: host_names != 0,
                read: &read,
            })
        },
    );
    unsafe { write_scores(out, equivalent) }
}

#[cfg(test)]
#[allow(unsafe_op_in_unsafe_fn)]
#[path = "aggregation_ffi_tests.rs"]
mod tests;

#[cfg(test)]
#[allow(unsafe_op_in_unsafe_fn)]
#[path = "aggregate_protocol_tests.rs"]
mod protocol_tests;
