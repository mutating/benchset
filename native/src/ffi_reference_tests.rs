use super::*;
use crate::{Phase, Sample};
use std::ffi::CStr;
use std::mem::{align_of, size_of, MaybeUninit};

struct Owned(*mut Handle);
impl Owned {
    fn new() -> Self {
        let handle = benchset_create();
        assert!(!handle.is_null());
        Self(handle)
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        unsafe { benchset_free(self.0) }
    }
}

fn size<T>() -> u32 {
    size_of::<T>() as u32
}

fn config() -> Config {
    let mut out = MaybeUninit::uninit();
    assert_eq!(unsafe { benchset_default_config(out.as_mut_ptr()) }, 1);
    Config {
        warmup_seconds: 0.0,
        measurement_seconds: 0.025,
        ..unsafe { out.assume_init() }
    }
}

unsafe fn v2(handle: *const Handle) -> CResultV2 {
    #[repr(C)]
    struct Guarded {
        before: u64,
        out: MaybeUninit<CResultV2>,
        after: u64,
    }
    let mut guarded = Guarded {
        before: 0x123456789abcdef0,
        out: MaybeUninit::uninit(),
        after: 0xfedcba9876543210,
    };
    assert_eq!(
        benchset_result_v2(handle, guarded.out.as_mut_ptr(), size::<CResultV2>()),
        1
    );
    assert_eq!(guarded.before, 0x123456789abcdef0);
    assert_eq!(guarded.after, 0xfedcba9876543210);
    guarded.out.assume_init()
}

unsafe fn v3(handle: *const Handle) -> CResultV3 {
    #[repr(C)]
    struct Guarded {
        before: u64,
        out: MaybeUninit<CResultV3>,
        after: u64,
    }
    let mut guarded = Guarded {
        before: 0x123456789abcdef0,
        out: MaybeUninit::uninit(),
        after: 0xfedcba9876543210,
    };
    assert_eq!(
        benchset_result_v3(handle, guarded.out.as_mut_ptr(), size::<CResultV3>()),
        1
    );
    assert_eq!(guarded.before, 0x123456789abcdef0);
    assert_eq!(guarded.after, 0xfedcba9876543210);
    guarded.out.assume_init()
}

// Only inspect bytes initialized by write_bytes; no invalid pointer is dereferenced
// and no padding of an initialized Rust value is read.
unsafe fn unchanged<T>(invoke: impl FnOnce(*mut T) -> i32) {
    let mut storage = MaybeUninit::<T>::uninit();
    let bytes = storage.as_mut_ptr().cast::<u8>();
    bytes.write_bytes(0xa5, size_of::<T>());
    assert_eq!(invoke(storage.as_mut_ptr()), 0);
    for index in 0..size_of::<T>() {
        assert_eq!(bytes.add(index).read(), 0xa5, "output byte {index} changed");
    }
}

macro_rules! offset {
    ($value:expr, $field:tt) => {{
        let value = &$value;
        &value.$field as *const _ as usize - value as *const _ as usize
    }};
}

#[test]
fn extended_and_metadata_c_layout_is_explicit() {
    let (machine, run, results) = metadata();
    let result = CResult::default();
    let extended = CResultV2 {
        base: result,
        pi_equivalent: 0.0,
        has_pi_equivalent: 0,
    };
    assert_eq!(benchset_abi_version(), 1);
    assert_eq!((size::<CResult>(), align_of::<CResult>()), (88, 8));
    assert_eq!((size::<CResultV2>(), align_of::<CResultV2>()), (104, 8));
    assert_eq!(offset!(extended, base), 0);
    assert_eq!(offset!(extended, pi_equivalent), 88);
    assert_eq!(offset!(extended, has_pi_equivalent), 96);
    let linked = CResultV3 {
        base: extended,
        reference_machine: ptr::null(),
        reference_run: ptr::null(),
    };
    assert_eq!((size::<CResultV3>(), align_of::<CResultV3>()), (120, 8));
    assert_eq!(offset!(linked, base), 0);
    assert_eq!(offset!(linked, reference_machine), 104);
    assert_eq!(offset!(linked, reference_run), 112);
    assert_eq!(offset!(result, score), 16);
    assert_eq!(offset!(result, iterations), 56);
    assert_eq!(offset!(result, has_variation), 84);
    assert_eq!(
        (size::<CReferenceMachine>(), align_of::<CReferenceMachine>()),
        (56, 8)
    );
    assert_eq!(offset!(machine, short_name), 8);
    assert_eq!(offset!(machine, os_name), 16);
    assert_eq!(offset!(machine, os_version), 24);
    assert_eq!(offset!(machine, cpu_architecture), 32);
    assert_eq!(offset!(machine, memory_bytes), 40);
    assert_eq!(offset!(machine, cpu_cores), 48);
    assert_eq!(
        (size::<CReferenceRun>(), align_of::<CReferenceRun>()),
        (40, 8)
    );
    assert_eq!(offset!(run, start_temperature_c), 8);
    assert_eq!(offset!(run, end_temperature_c), 16);
    assert_eq!(offset!(run, warmup_seconds), 24);
    assert_eq!(offset!(run, result_count), 32);
    assert_eq!(
        (
            size::<CReferenceBenchmarkResult>(),
            align_of::<CReferenceBenchmarkResult>()
        ),
        (16, 8)
    );
    assert_eq!(offset!(results[0], score), 8);
}

#[test]
fn metadata_null_size_and_index_fail_without_writing() {
    unsafe {
        assert_eq!(
            benchset_reference_machine(ptr::null_mut(), size::<CReferenceMachine>()),
            0
        );
        assert_eq!(
            benchset_reference_run(ptr::null_mut(), size::<CReferenceRun>()),
            0
        );
        assert_eq!(
            benchset_reference_result(0, ptr::null_mut(), size::<CReferenceBenchmarkResult>()),
            0
        );
        for wrong in [
            0,
            size::<CReferenceMachine>() - 1,
            size::<CReferenceMachine>() + 1,
            u32::MAX,
        ] {
            unchanged::<CReferenceMachine>(|out| benchset_reference_machine(out, wrong));
        }
        for wrong in [
            0,
            size::<CReferenceRun>() - 1,
            size::<CReferenceRun>() + 1,
            u32::MAX,
        ] {
            unchanged::<CReferenceRun>(|out| benchset_reference_run(out, wrong));
        }
        for wrong in [
            0,
            size::<CReferenceBenchmarkResult>() - 1,
            size::<CReferenceBenchmarkResult>() + 1,
            u32::MAX,
        ] {
            unchanged::<CReferenceBenchmarkResult>(|out| benchset_reference_result(0, out, wrong));
        }
        let (_, run, _) = metadata();
        assert_eq!(run.result_count, 2);
        for index in [2, run.result_count, u64::MAX] {
            unchanged::<CReferenceBenchmarkResult>(|out| {
                benchset_reference_result(index, out, size::<CReferenceBenchmarkResult>())
            });
        }
    }
}

fn metadata() -> (
    CReferenceMachine,
    CReferenceRun,
    Vec<CReferenceBenchmarkResult>,
) {
    let mut machine = MaybeUninit::uninit();
    let mut run = MaybeUninit::uninit();
    assert_eq!(
        unsafe { benchset_reference_machine(machine.as_mut_ptr(), size::<CReferenceMachine>()) },
        1
    );
    assert_eq!(
        unsafe { benchset_reference_run(run.as_mut_ptr(), size::<CReferenceRun>()) },
        1
    );
    let run = unsafe { run.assume_init() };
    let results = (0..run.result_count)
        .map(|index| {
            let mut out = MaybeUninit::uninit();
            assert_eq!(
                unsafe {
                    benchset_reference_result(
                        index,
                        out.as_mut_ptr(),
                        size::<CReferenceBenchmarkResult>(),
                    )
                },
                1
            );
            unsafe { out.assume_init() }
        })
        .collect();
    (unsafe { machine.assume_init() }, run, results)
}

fn assert_metadata() {
    let (machine, run, results) = metadata();
    for (actual, expected) in [
        (machine.name, "Raspberry Pi 5 Model B Rev 1.0"),
        (machine.short_name, "pi"),
        (machine.os_name, "Ubuntu Server"),
        (machine.os_version, "26.04 LTS"),
        (machine.cpu_architecture, "aarch64"),
    ] {
        let text = unsafe { CStr::from_ptr(actual) };
        assert_eq!(text.to_str().unwrap(), expected);
        assert_eq!(text.to_bytes_with_nul().last(), Some(&0));
    }
    assert_eq!(machine.memory_bytes, 8_589_934_592);
    assert_eq!(machine.cpu_cores, 4);
    assert_eq!(run.started_at_unix_seconds, 1_792_767_590.517391);
    assert_eq!(run.start_temperature_c, 41.35);
    assert_eq!(run.end_temperature_c, 66.65);
    assert_eq!(run.warmup_seconds, 1248.0);
    assert_eq!(run.result_count, 2);
    for (actual, (name, score)) in results.iter().zip([
        ("single_core", 47_231.227029007096),
        ("multi_core", 181_953.17419139884),
    ]) {
        assert_eq!(
            unsafe { CStr::from_ptr(actual.benchmark) }
                .to_str()
                .unwrap(),
            name
        );
        assert_eq!(actual.score, score);
    }
}

unsafe fn assert_reference_views(machine: *const CReferenceMachine, run: *const CReferenceRun) {
    assert!(!machine.is_null());
    assert!(!run.is_null());
    assert_metadata();
    let (mut copied_machine, mut copied_run, _) = metadata();
    let machine = &*machine;
    let run = &*run;
    for (actual, copied) in [
        (machine.name, copied_machine.name),
        (machine.short_name, copied_machine.short_name),
        (machine.os_name, copied_machine.os_name),
        (machine.os_version, copied_machine.os_version),
        (machine.cpu_architecture, copied_machine.cpu_architecture),
    ] {
        assert_eq!(actual, copied);
        assert_eq!(CStr::from_ptr(actual), CStr::from_ptr(copied));
    }
    assert_eq!(machine.memory_bytes, copied_machine.memory_bytes);
    assert_eq!(machine.cpu_cores, copied_machine.cpu_cores);
    for (actual, copied) in [
        (
            run.started_at_unix_seconds,
            copied_run.started_at_unix_seconds,
        ),
        (run.start_temperature_c, copied_run.start_temperature_c),
        (run.end_temperature_c, copied_run.end_temperature_c),
        (run.warmup_seconds, copied_run.warmup_seconds),
    ] {
        assert_eq!(actual.to_bits(), copied.to_bits());
    }
    assert_eq!(run.result_count, copied_run.result_count);
    // Scalar getters return independent structs, with borrowed global strings.
    copied_machine.name = ptr::null();
    copied_machine.memory_bytes = 0;
    copied_machine.cpu_cores = 0;
    copied_run.started_at_unix_seconds = 0.0;
    copied_run.result_count = 0;
    assert_ne!(machine.name, copied_machine.name);
    assert_ne!(machine.memory_bytes, copied_machine.memory_bytes);
    assert_ne!(machine.cpu_cores, copied_machine.cpu_cores);
    assert_ne!(
        run.started_at_unix_seconds,
        copied_run.started_at_unix_seconds
    );
    assert_ne!(run.result_count, copied_run.result_count);
}

fn assert_same_legacy(a: &CResult, b: &CResult) {
    macro_rules! same {
        ($($field:ident),+ $(,)?) => {
            $(assert_eq!(a.$field, b.$field, stringify!($field));)+
        };
    }
    same!(
        abi_version,
        status,
        benchmark,
        workers,
        iterations,
        checksum,
        sample_count,
        unstable,
        has_variation,
    );
    for (a, b) in [
        (a.score, b.score),
        (a.warmup_seconds, b.warmup_seconds),
        (a.measurement_seconds, b.measurement_seconds),
        (a.elapsed_seconds, b.elapsed_seconds),
        (a.variation_percent, b.variation_percent),
    ] {
        assert_eq!(a.to_bits(), b.to_bits());
    }
}

fn assert_same_v3(a: &CResultV3, b: &CResultV3) {
    assert_same_legacy(&a.base.base, &b.base.base);
    assert_eq!(a.base.pi_equivalent, b.base.pi_equivalent);
    assert_eq!(a.base.has_pi_equivalent, b.base.has_pi_equivalent);
    assert_eq!(a.reference_machine, b.reference_machine);
    assert_eq!(a.reference_run, b.reference_run);
}

#[test]
fn metadata_golden_without_handles_and_concurrent_reads() {
    assert_metadata();
    let threads: Vec<_> = (0..8)
        .map(|_| {
            std::thread::spawn(|| {
                for _ in 0..16 {
                    assert_metadata();
                }
            })
        })
        .collect();
    for thread in threads {
        thread.join().unwrap();
    }
}

#[test]
fn result_v2_null_wrong_size_and_before_run_preserve_all_bytes() {
    unsafe {
        let h = Owned::new();
        assert_eq!(
            benchset_result_v2(h.0, ptr::null_mut(), size::<CResultV2>()),
            0
        );
        unchanged::<CResultV2>(|out| benchset_result_v2(ptr::null(), out, size::<CResultV2>()));
        unchanged::<CResultV2>(|out| benchset_result_v2(h.0, out, size::<CResultV2>()));
        // Repeat after a result exists, so rejecting sizes cannot accidentally
        // be explained only by the missing stored result.
        assert_eq!(benchset_run(h.0, ptr::null(), None, ptr::null_mut()), 2);
        for wrong in [
            0,
            88,
            size::<CResultV2>() - 1,
            size::<CResultV2>() + 1,
            u32::MAX,
        ] {
            unchanged::<CResultV2>(|out| benchset_result_v2(h.0, out, wrong));
        }
        let result = v2(h.0);
        assert_eq!(result.base.status, 2);
        assert_eq!(result.pi_equivalent, 0.0);
        assert_eq!(result.has_pi_equivalent, 0);
    }
}

#[test]
fn result_v3_null_wrong_size_and_before_run_preserve_all_bytes() {
    unsafe {
        let h = Owned::new();
        for ready in [false, true] {
            if ready {
                assert_eq!(benchset_run(h.0, ptr::null(), None, ptr::null_mut()), 2);
            }
            for handle in [ptr::null(), h.0 as *const Handle] {
                assert_eq!(
                    benchset_result_v3(handle, ptr::null_mut(), size::<CResultV3>()),
                    0
                );
                for wrong in [0, 88, 104, 119, 121, u32::MAX] {
                    unchanged::<CResultV3>(|out| benchset_result_v3(handle, out, wrong));
                }
            }
            unchanged::<CResultV3>(|out| benchset_result_v3(ptr::null(), out, size::<CResultV3>()));
            if ready {
                let result = v3(h.0);
                assert_eq!(result.base.base.status, Status::Failed as u32);
                assert_eq!(result.base.has_pi_equivalent, 0);
                assert_reference_views(result.reference_machine, result.reference_run);
            } else {
                unchanged::<CResultV3>(|out| benchset_result_v3(h.0, out, size::<CResultV3>()));
            }
        }
    }
}

#[test]
fn rejected_duration_bits_and_all_legacy_fields_survive_each_result_getter() {
    unsafe {
        for workload in [1, 2] {
            for (warmup, measurement) in [
                (f64::from_bits(0x7ff8_1234_5678_9abc), 0.025),
                (0.0, f64::from_bits(0xfff8_9876_5432_1234)),
                (f64::INFINITY, 0.025),
                (0.0, f64::NEG_INFINITY),
                (-0.0, 0.0),
                (0.0, -0.0),
                (0.0, 1e-12), // Positive f64 rounds to a zero Duration.
            ] {
                let h = Owned::new();
                let config = Config {
                    benchmark: workload,
                    warmup_seconds: warmup,
                    measurement_seconds: measurement,
                    ..config()
                };
                assert_eq!(
                    benchset_run(h.0, &config, None, ptr::null_mut()),
                    Status::Failed as u32
                );
                let mut legacy = CResult::default();
                assert_eq!(benchset_result(h.0, &mut legacy), 1);
                let extended = v2(h.0);
                let linked = v3(h.0);
                for value in [&legacy, &extended.base, &linked.base.base] {
                    assert_eq!(value.status, Status::Failed as u32);
                    assert_eq!(value.benchmark, workload);
                    assert_eq!(value.warmup_seconds.to_bits(), warmup.to_bits());
                    assert_eq!(value.measurement_seconds.to_bits(), measurement.to_bits());
                    assert_same_legacy(value, &legacy);
                }
                assert_eq!(extended.has_pi_equivalent, 0);
                assert_eq!(linked.base.has_pi_equivalent, 0);
                assert_eq!(extended.pi_equivalent, linked.base.pi_equivalent);
                assert_reference_views(linked.reference_machine, linked.reference_run);
            }
        }
    }
}

#[test]
fn legacy_getter_canary_and_extended_mapping_preserve_all_fields() {
    #[repr(C)]
    struct Guarded {
        before: u64,
        result: CResult,
        after: [u64; 4],
    }
    unsafe {
        for (status, ratio, unstable) in [
            None,
            Some(0.0),
            Some(0.5),
            Some(1.0),
            Some(2.0),
            Some(0.12345678901234568),
            Some(1e-200),
            Some(1e100),
        ]
        .into_iter()
        .flat_map(|ratio| [false, true].map(move |unstable| (ratio, unstable)))
        .flat_map(|(ratio, unstable)| {
            [Status::Completed, Status::Cancelled, Status::Failed]
                .map(move |status| (status, ratio, unstable))
        }) {
            let h = Owned::new();
            assert_eq!(
                execute(
                    h.0,
                    &config(),
                    None,
                    ptr::null_mut(),
                    &mut |benchmark, options, _, _| {
                        let mut result =
                            BenchmarkResult::failed(benchmark, options, "готово\0reason".into());
                        result.status = status;
                        result.score = (status == Status::Completed).then_some(123.5);
                        result.pi_equivalent = if status == Status::Completed {
                            ratio
                        } else {
                            None
                        };
                        result.workers = u32::MAX;
                        result.elapsed_seconds = 4.0;
                        result.iterations = u64::MAX;
                        result.checksum = u64::MAX - 1;
                        result.variation_percent = Some(10.5);
                        result.unstable = unstable;
                        result.samples = vec![Sample {
                            elapsed_seconds: 4.0,
                            duration_seconds: 3.0,
                            iterations: u64::MAX,
                            iterations_per_second: 123.5,
                        }];
                        result
                    }
                ),
                status as u32
            );
            let mut old = Guarded {
                before: 0x123456789abcdef0,
                result: CResult::default(),
                after: [0xfedcba9876543210; 4],
            };
            assert_eq!(benchset_result(h.0, &mut old.result), 1);
            assert_eq!(old.before, 0x123456789abcdef0);
            assert_eq!(old.after, [0xfedcba9876543210; 4]);
            let current = v2(h.0);
            let linked = v3(h.0);
            for value in [&old.result, &current.base, &linked.base.base] {
                assert_eq!(
                    (
                        value.abi_version,
                        value.status,
                        value.benchmark,
                        value.workers
                    ),
                    (1, status as u32, 1, u32::MAX)
                );
                assert_eq!(
                    (value.score, value.warmup_seconds, value.measurement_seconds),
                    (
                        if status == Status::Completed {
                            123.5
                        } else {
                            0.0
                        },
                        0.0,
                        0.025
                    )
                );
                assert_eq!(
                    (value.elapsed_seconds, value.variation_percent),
                    (4.0, 10.5)
                );
                assert_eq!(
                    (value.iterations, value.checksum, value.sample_count),
                    (u64::MAX, u64::MAX - 1, 1)
                );
                assert_eq!(
                    (value.unstable, value.has_variation),
                    (u32::from(unstable), 1)
                );
            }
            let expected_ratio = if status == Status::Completed {
                ratio
            } else {
                None
            };
            assert_eq!(
                current.has_pi_equivalent,
                u32::from(expected_ratio.is_some())
            );
            assert_eq!(current.pi_equivalent, expected_ratio.unwrap_or(0.0));
            assert_eq!(linked.base.has_pi_equivalent, current.has_pi_equivalent);
            assert_eq!(linked.base.pi_equivalent, current.pi_equivalent);
            assert_reference_views(linked.reference_machine, linked.reference_run);
            // Neither getter consumes or modifies the stored result.
            assert_eq!(v2(h.0).pi_equivalent, current.pi_equivalent);
            let repeated = v3(h.0);
            assert_eq!(repeated.reference_machine, linked.reference_machine);
            assert_eq!(repeated.reference_run, linked.reference_run);
            assert_eq!(benchset_result(h.0, &mut old.result), 1);
            assert_eq!(old.after, [0xfedcba9876543210; 4]);
            assert_eq!(
                CStr::from_ptr(benchset_reason(h.0)).to_str().unwrap(),
                "готово reason"
            );
        }
    }
}

#[test]
fn incompatible_abi_prefix_rejected_before_tail_and_new_handle_still_works() {
    #[repr(C, align(8))]
    struct Prefix {
        abi_version: u32,
        struct_size: u32,
    }
    unsafe {
        // A claimed full size must not make an incompatible ABI read a tail
        // which does not exist in this valid, aligned prefix allocation.
        let prefix = Prefix {
            abi_version: 2,
            struct_size: size::<Config>(),
        };
        let h = Owned::new();
        assert_eq!(
            benchset_run(
                h.0,
                (&prefix as *const Prefix).cast(),
                None,
                ptr::null_mut()
            ),
            2
        );
        assert!(CStr::from_ptr(benchset_reason(h.0))
            .to_str()
            .unwrap()
            .contains("incompatible"));
        assert_eq!(v2(h.0).has_pi_equivalent, 0);
        assert_eq!(v2(h.0).base.score, 0.0);
        let _serial = crate::tests::TEST_LOCK.lock().unwrap();
        let current = Owned::new();
        let status = benchset_run(current.0, &config(), None, ptr::null_mut());
        let result = v2(current.0);
        assert_eq!(result.base.status, status);
        if status == Status::Completed as u32 {
            assert!(result.base.score > 0.0);
            assert_eq!(result.has_pi_equivalent, 1);
            assert_eq!(
                result.pi_equivalent,
                result.base.score / 47_231.227029007096
            );
        } else {
            assert_eq!(status, Status::Failed as u32);
            assert_eq!(
                CStr::from_ptr(benchset_reason(current.0)).to_str().unwrap(),
                "measurement window was too short to complete any work"
            );
            assert!(result.base.iterations == 0 || result.base.elapsed_seconds == 0.0);
            assert_eq!(result.base.score, 0.0);
            assert_eq!(result.has_pi_equivalent, 0);
            assert_eq!(result.pi_equivalent, 0.0);
        }
    }
}

#[test]
fn metadata_inside_callback_and_reference_views_survive_all_handle_outcomes() {
    let _serial = crate::tests::TEST_LOCK.lock().unwrap();
    struct Context {
        handle: *mut Handle,
        cancel: bool,
        callbacks: usize,
    }
    unsafe extern "C" fn callback(progress: *const CProgress, raw: *mut c_void) {
        // Do not panic across this boundary; remember the getter return codes
        // and check the full metadata from the ordinary calling frame below.
        let context = &mut *raw.cast::<Context>();
        let mut machine = MaybeUninit::uninit();
        let mut run = MaybeUninit::uninit();
        let mut result = MaybeUninit::uninit();
        if benchset_reference_machine(machine.as_mut_ptr(), size::<CReferenceMachine>()) == 1
            && benchset_reference_run(run.as_mut_ptr(), size::<CReferenceRun>()) == 1
            && benchset_reference_result(
                1,
                result.as_mut_ptr(),
                size::<CReferenceBenchmarkResult>(),
            ) == 1
        {
            context.callbacks += 1;
        }
        if context.cancel
            && [Phase::Warmup as u32, Phase::Measurement as u32].contains(&(*progress).phase)
        {
            benchset_cancel(context.handle);
        }
    }
    unsafe {
        let (machine, _, references) = metadata();
        let name = machine.name;
        let short = machine.short_name;
        let benchmark = references[1].benchmark;
        let mut canonical = None;
        for (workload, status, cancel_phase, precancel) in [1, 2].into_iter().flat_map(|workload| {
            [
                (Status::Completed, None, false),
                (Status::Cancelled, Some(Phase::Warmup), false),
                (Status::Cancelled, Some(Phase::Measurement), false),
                (Status::Cancelled, None, true),
                (Status::Failed, None, false),
            ]
            .map(move |(status, phase, precancel)| (workload, status, phase, precancel))
        }) {
            let h = Owned::new();
            let mut context = Context {
                handle: h.0,
                cancel: cancel_phase.is_some(),
                callbacks: 0,
            };
            let c = Config {
                benchmark: workload,
                warmup_seconds: match cancel_phase {
                    Some(Phase::Warmup) => 10.0,
                    Some(_) => 0.0,
                    None => 0.025,
                },
                measurement_seconds: if cancel_phase.is_some() { 10.0 } else { 0.025 },
                reserved: u32::from(status == Status::Failed),
                ..config()
            };
            if precancel {
                benchset_cancel(h.0);
            }
            let actual = benchset_run(
                h.0,
                &c,
                Some(callback),
                (&mut context as *mut Context).cast(),
            );
            if status == Status::Completed && actual == Status::Failed as u32 {
                assert_eq!(
                    CStr::from_ptr(benchset_reason(h.0)).to_str().unwrap(),
                    "measurement window was too short to complete any work"
                );
            } else {
                assert_eq!(actual, status as u32);
            }
            if status != Status::Failed && !precancel {
                assert!(context.callbacks > 0);
            }
            let mut result = v3(h.0);
            assert_eq!(result.base.base.status, actual);
            assert_eq!(result.base.base.benchmark, workload);
            assert_eq!(
                result.base.has_pi_equivalent,
                u32::from(actual == Status::Completed as u32)
            );
            if actual != Status::Completed as u32 {
                assert_eq!(result.base.base.score, 0.0);
                assert_eq!(result.base.pi_equivalent, 0.0);
                if status == Status::Completed {
                    assert!(
                        result.base.base.iterations == 0 || result.base.base.elapsed_seconds == 0.0
                    );
                }
            }
            let links = (result.reference_machine, result.reference_run);
            assert_reference_views(links.0, links.1);
            assert_eq!(*canonical.get_or_insert(links), links);
            let repeated = v3(h.0);
            assert_eq!((repeated.reference_machine, repeated.reference_run), links);
            drop(h);
            // Retain only the global pointers; neither the output buffer nor
            // the freed handle owns the referenced machine/run structs.
            ptr::write_bytes(&mut result, 0, 1);
            assert!(result.reference_machine.is_null());
            assert!(result.reference_run.is_null());
            assert_reference_views(links.0, links.1);
            assert_eq!(
                CStr::from_ptr(name).to_str().unwrap(),
                "Raspberry Pi 5 Model B Rev 1.0"
            );
            assert_eq!(CStr::from_ptr(short).to_str().unwrap(), "pi");
            assert_eq!(CStr::from_ptr(benchmark).to_str().unwrap(), "multi_core");
            assert_metadata();
            let (again, _, _) = metadata();
            assert_eq!(again.name, name);
        }
    }
}

#[test]
fn extended_getter_recovers_poisoned_result_mutex() {
    unsafe {
        let h = Owned::new();
        let poisoned = std::panic::catch_unwind(AssertUnwindSafe(|| {
            let _guard = (*h.0).result.lock().unwrap();
            panic!("poison result storage");
        }));
        assert!(poisoned.is_err());
        assert_eq!(
            execute(h.0, &config(), None, ptr::null_mut(), &mut |_, _, _, _| {
                panic!("native runner failed")
            }),
            Status::Failed as u32
        );
        let result = v2(h.0);
        assert_eq!(result.base.status, Status::Failed as u32);
        assert_eq!(result.base.score, 0.0);
        assert_eq!(result.has_pi_equivalent, 0);
        assert_eq!(result.pi_equivalent, 0.0);
        let linked = v3(h.0);
        assert_eq!(linked.base.base.status, Status::Failed as u32);
        assert_reference_views(linked.reference_machine, linked.reference_run);
    }
}

#[test]
fn global_busy_and_rejected_rerun_keep_reference_views_and_stored_results() {
    let _serial = crate::tests::TEST_LOCK.lock().unwrap();
    struct Context {
        active: *mut Handle,
        busy: *mut Handle,
        config: Config,
        busy_status: Option<u32>,
    }
    unsafe extern "C" fn callback(_: *const CProgress, raw: *mut c_void) {
        let context = &mut *raw.cast::<Context>();
        if context.busy_status.is_none() {
            context.busy_status = Some(benchset_run(
                context.busy,
                &context.config,
                None,
                ptr::null_mut(),
            ));
            benchset_cancel(context.active);
        }
    }
    unsafe {
        let active = Owned::new();
        let busy = Owned::new();
        let mut context = Context {
            active: active.0,
            busy: busy.0,
            config: config(),
            busy_status: None,
        };
        assert_eq!(
            benchset_run(
                active.0,
                &config(),
                Some(callback),
                (&mut context as *mut Context).cast(),
            ),
            Status::Cancelled as u32
        );
        assert_eq!(context.busy_status, Some(Status::Failed as u32));
        assert_eq!(
            CStr::from_ptr(benchset_reason(busy.0)).to_str().unwrap(),
            "another benchmark is already running"
        );
        let cancelled = v3(active.0);
        let failed = v3(busy.0);
        assert_eq!(cancelled.base.base.status, Status::Cancelled as u32);
        assert_eq!(failed.base.base.status, Status::Failed as u32);
        assert_eq!(cancelled.reference_machine, failed.reference_machine);
        assert_eq!(cancelled.reference_run, failed.reference_run);
        for (handle, before) in [(active.0, &cancelled), (busy.0, &failed)] {
            assert_reference_views(before.reference_machine, before.reference_run);
            assert_eq!(before.base.has_pi_equivalent, 0);
            let reason = benchset_reason(handle);
            assert_eq!(
                benchset_run(handle, &config(), None, ptr::null_mut()),
                Status::Failed as u32
            );
            assert_same_v3(before, &v3(handle));
            assert_eq!(benchset_reason(handle), reason);
        }
    }
}
