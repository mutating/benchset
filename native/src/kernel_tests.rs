use super::*;

impl Kernel {
    pub(crate) fn broken_for_test() -> Self {
        Self {
            table: vec![],
            input: [0; INPUT_BYTES],
            state: 0,
        }
    }
}

#[test]
fn deterministic_stateful_and_independent() {
    let mut a = Kernel::new(0).unwrap();
    let mut b = Kernel::new(0).unwrap();
    let mut c = Kernel::new(1).unwrap();
    for _ in 0..8 {
        assert_eq!(a.step(), b.step());
        assert_ne!(a.state, c.step());
    }
    let before = a.table.clone();
    a.step();
    assert_ne!(a.table, before);
    assert_eq!(a.table.len(), WORDS);
}

#[test]
fn golden_workload_vectors() {
    // Fixed vectors captured before changing the kernel: protect workload identity.
    for (worker, expected) in [
        (
            0,
            [
                3178289567863269773,
                4512211901833312295,
                1677653858987281238,
            ],
        ),
        (
            1,
            [
                18228149274796182878,
                4142944746614797920,
                8459165180143766608,
            ],
        ),
        (
            usize::MAX,
            [
                10247092875403938220,
                10884731871517564497,
                5635134307842295282,
            ],
        ),
    ] {
        let mut kernel = Kernel::new(worker).unwrap();
        for checksum in expected {
            assert_eq!(kernel.step(), checksum);
        }
    }
}

#[test]
fn crafted_parser_inputs_and_wrapping_states() {
    // Every transition is deliberately present; it also includes tokens in
    // invalid states, so '+' and '.' fall through after inappropriate prefixes.
    let tokens = b" +1.2e; -3,4E\n5e x+..e9;; ++;1+;1.e;1.2+; ,E;";
    for state in [0, 1, u64::MAX] {
        let mut kernel = Kernel::new(0).unwrap();
        kernel.state = state;
        for (index, byte) in kernel.input.iter_mut().enumerate() {
            *byte = tokens[index % tokens.len()];
        }
        let before = kernel.state;
        let after = kernel.step();
        assert_ne!(before, after);
    }
}

#[test]
fn capacity_failure_is_reported_without_real_oom() {
    assert!(allocate_table(usize::MAX).unwrap_err().contains("capacity"));
    let result = Kernel::new_with_allocator(0, &mut || Err("allocation fault".into()));
    assert!(matches!(result, Err(reason) if reason == "allocation fault"));
    assert!(allocate_table(0).unwrap().is_empty());
}
