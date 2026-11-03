# Rust core centralization regression matrix

The six groups below combine the independent regression reviews of moving
aggregation, suite execution, validation, the benchmark catalog, defaults and
calibration into Rust. Tests check observable behavior and transport separately:
matching arithmetic alone cannot detect a wrapper that recalculates a native
result, and an unchanged fixture cannot expose a handwritten metadata copy.

## Review and evidence inventory

| Group | Independent review set |
|---|---|
| 1. Aggregation | `aggregate_review_a`, `aggregate_review_b`, `aggregate_review_c` |
| 2. Suite execution | `suite_review_a`, `suite_review_b`, `suite_review_c` |
| 3. Validation | `validation_review_a`, `validation_review_b`, `validation_review_c` |
| 4. Catalog | `registry_review_a`, `registry_review_b`, `registry_review_c` |
| 5. Defaults | `defaults_review_a`, `defaults_review_b`, `defaults_review_c` |
| 6. Calibration | `calibration_review_a`, `calibration_review_b`, `calibration_review_c` |

Local mutation evidence is deliberately outside the source distribution:

| Label | Reports and logs under `target/` |
|---|---|
| A | `aggregation-audit/results.json`, `python-results.json`, `swift-results.json`, `audit-gap-results.json`; adjacent baseline and individual mutant logs |
| M | `metadata-mutations/results.json`; each record identifies its exact test and log |
| S | `swift-suite-mutations/results.json`; adjacent baseline and individual mutant logs |
| D | `swift-defaults-mutations/results.json`; native-defaults baseline and individual mutant logs |
| C | `core-centralization-audit/results.json`; baseline and individual mutant logs |
| P | `python-core-audit/mutation-results.json`; each record identifies exact tests and its adjacent mutant log |
| W | `core-centralization-audit/artifact-mutation.json`; real stale-snapshot wheel rebuild and its failing assertion |

Reports record a successful unmodified baseline and individual deliberately
regressed copies. All 210 directed checks were detected by regression tests:
A 60 (21 Rust/storage, 13 Python transport, 6 Swift, 20 audit-gap checks), M 44,
S 23, D 11, C 24, P 47 and W 1. Infrastructure failures are not counted as
killed mutants. Two Swift mutations (`force_unwrap_progress` and
`double_free_suite_handle`) triggered runtime traps inside the designated tests
after successful compilation; the other 208 were detected by assertions.
New tests have docstrings or doc-comments explaining their regression target.
Final verification and local platform scope are recorded below.

Pre-transfer Python characterization includes direct-constructor storage
(`A/baseline-python-storage.log`), unknown-name and surrogate behavior
(`A/prechange-unknown-names.log`, `prechange-surrogate-names.log`,
`prechange-validation-surrogates.log`). Existing CLI, duration, ownership,
metadata and packaged-consumer tests supplied the other original contracts.
Boundary/exporter tests additionally cover capabilities introduced by the
transfer; their unmodified baselines precede their directed mutations.
The later aggregation audit cases were also run against original source:
`aggregation-audit/audit-gap-head-baseline.log` records 13 passing cases.
`A/baseline-rust.log` verifies retained-ratio/status contracts alongside the
new FFI tests; it is an unmutated implementation baseline, not a separate
record of the pre-transfer Rust tree.

## 1. Aggregation

| Scenarios | Exact tests | Directed mutations / evidence |
|---|---|---|
| Complete ordered set only; no partial, duplicate, reordered or extra result; failed/cancelled/unknown status remains unavailable even with retained coefficients | Rust `incomplete_invalid_or_failed_suites_have_no_full_score`, `unsuccessful_results_with_present_ratios_have_no_aggregate`; Python `test_no_full_score_for_partial_missing_or_invalid_suite`, `test_aggregate_rejects_unsuccessful_results_with_retained_ratios`; Swift `aggregateRejectsUnsuccessfulResultsWithRetainedRatios`, `fullSuiteGeometricMeanAndForwarding` | A: `length`, `order`, `status-failed-single`, `status-failed-multi`, `status-cancelled-single`, `status-cancelled-multi`, six `swift-status-*` |
| Equal weights; original stored ratios rather than scores or rounded displays; unstable completed results included; zero remains present; validate all values before zero short-circuit; reject negative/NaN/infinite values at either position | Rust `aggregate_is_geometric_mean_of_ratios`, `geometric_mean_handles_zeros_and_extreme_ratios_without_multiplication`; Python `test_suite_aggregates_ratios_and_preserves_results`, `test_suite_rejects_invalid_ratios`; Swift `fullSuiteLogarithmicRangeZerosAndInvalidRatios` | A: `finite`, `negative`, `missing`, `zero`, `zero-hides-invalid`, `ratio-rounding`, `weight` |
| Marshal every input field/count; preserve unrelated native output, nonstandard nonzero presence flags, absent values, zero and precision; never recalculate in Python | Rust `aggregate_ffi_preserves_raw_fields_and_presence`; Python `test_aggregate_boundary_preserves_inputs_and_native_output`, `test_aggregate_boundary_reports_unavailable_native_output` | A: `ffi-nonzero-presence`, `ffi-ignore-presence`, `ffi-status`, `ffi-ratio`; `python-wire-*`, `python-output-*`, `python-getter-rejection` |
| Unknown strings never alias registered members, including UTF-8, embedded NUL and lone surrogates | Python `test_aggregate_does_not_alias_unknown_benchmark_names` | A: `python-wire-benchmark`, `python-native-nul`, `python-native-surrogates` |
| Python storage constructor retains supplied tuple and scores; `from_results` preserves original result objects; import/storage inspection remains native-free | `test_direct_suite_constructor_stores_supplied_fields_without_aggregation`, `test_direct_suite_constructor_remains_native_free_in_a_fresh_interpreter`, `test_suite_aggregates_ratios_and_preserves_results`, `test_calibration_imports_and_serialization_do_not_load_native_code` | A: `constructor-recomputes`, `constructor-copies-results`, `gap-nativefree-constructor` |
| C NULL/count/output contract, including nonzero NULL counts 1/2/3/MAX; no writes on rejection or surrounding canary damage; valid caller-owned buffers only | Rust `aggregate_pointer_and_count_contract` | A: `ffi-empty-count`, `ffi-null-contract`, `gap-ffi-null-output-write`, `gap-ffi-output-canary` |
| Independent public C header layout, offsets, field/count widths and direct aggregate call preserve nonstandard presence flags | `test_public_native_consumers[suite.c-C full suite smoke passed]`, `tests/native/suite.c` static assertions and direct `benchset_aggregate` call | A: `gap-c-count-width`, `gap-c-field-width` |
| First and both ratios missing; negative zero becomes present positive zero; minimum subnormal and maximum finite inputs; any zero skips every logarithm and preserves existing floating-point exception flags | Rust `every_missing_ratio_prevents_aggregation`, `aggregate_float_endpoints_and_negative_zero`; Python `test_aggregate_requires_each_ratio_independently`, `test_aggregate_negative_zero_is_present_canonical_positive_zero`, `test_aggregate_preserves_actual_finite_float_endpoints`, `test_zero_aggregate_preserves_floating_environment` | A: six `gap-python/rust-missing-*` checks, `gap-python-zero-input`, `gap-python-zero-sign`, `gap-rust-zero-sign`, `gap-rust-endpoint-max`, `gap-rust-endpoint-min`, `gap-fenv-all-logs`, `gap-fenv-nonzero-logs` |
| Pure aggregation remains usable inside active measurement callbacks and does not disturb reservation/cancellation/reuse | `test_aggregation_is_available_inside_active_benchmark_callbacks` | A: `gap-active-reservation`; `gap-active-head-baseline` verifies original behavior |
| Same-version older library must reject missing aggregate symbol before allocating | `test_aggregate_is_a_required_native_symbol_before_allocation` | A: `gap-required-symbol` |

## 2. Suite execution

| Scenarios | Exact tests | Directed mutations / evidence |
|---|---|---|
| Rust owns sequence and early stop at either unsuccessful position; retain complete prefix and unsuccessful diagnostics; observer sees results before subsequent work; every nonzero observer result stops; caller-thread callbacks | Rust `suite_order_and_stop_after_every_unsuccessful_position`, `public_suite_progress_cancellation_and_invalid_duration`, `observation_streams_results_and_controls_later_work_without_rewriting_them`, `observation_reports_invalid_configuration_and_rejects_null_suite`; Python `test_run_all_returns_suite_and_stops_on_failure`, `test_public_api_forwards_durations_and_callback_without_running_long_benchmarks` | C: `observer_drop`, `observer_ignore_stop`, `observer_wrong_id`, `observer_cancellation`, `observer_nonzero_stop`; P: `explicit_suite_mode`, `suite_free_mode` |
| Copy every child result/reason/sample/ratio before borrowed buffers disappear; decoding failure at either child keeps earlier results and frees suite once | Swift `suiteSnapshotsOwnEachNativePayload`; Python `test_suite_child_decoder_failure_keeps_prefix_and_frees_once`; native `actual_suite_getters_progress_cancellation_and_owned_results`, `suite_getters_preserve_zero_aggregate_and_indexed_samples` | S: `drop_first_child`, `drop_reason`, `drop_iterations`, `drop_ratio`, `drop_sample_time`, `drop_sample_iterations`, `ignore_result_getter_failure`, `ignore_sample_getter_failure` |
| Callback BaseExceptions stop future notifications/work; terminal callback failure cannot publish completed score; preserve prefix; no host exception unwinds through C; malformed contexts/IDs/child handles rejected after return | Python `test_suite_callback_failure_stops_notifications_and_keeps_completed_prefix`, `test_callback_base_exceptions_stop_notifications_and_allow_reuse`; Swift `suiteCallbackBoundaryRejectsMalformedResultsWithoutUnwinding` | S: `continue_after_callback_error`, `accept_nil_result_context`, `force_unwrap_progress`, `accept_unknown_result`, `accept_nil_child`, `ignore_progress_id`, `ignore_result_error`; P: `suite_callback_base_exceptions_do_not_cross_c`, `suppress_notifications_after_callback_failure`, `host_failure_overrides_completed_status`, `terminal_callback_failure_stops_suite`, `host_failure_hides_native_aggregate` |
| Cancellation reaches proper single/suite API; sticky between children; concurrent/reentrant owners remain excluded; cleanup waits for cancellation; no double-free/leak; captures and runner released on every exit | Swift `cancellationUsesTheActiveNativeHandleKind`, `suiteReservationSurvivesNativeHandleReplacement`, `concurrentSuitesKeepSingleOwnerDuringCancellation`, `suiteCallbackCapturesReleaseOnAllExits`, `fullSuiteRejectsReentryAndRecoversAfterError`, `fullSuiteCancellationSurvivesCompletedBenchmark`, `cleanupWaitsForAnInFlightNativeCancellation`; Python existing ownership, SIGINT and cleanup tests | S: `wrong_cancel_api`, `wrong_free_api`, `retain_context`, `leak_suite_handle`, `double_free_suite_handle`, `ignore_allocation_failure`, `ignore_abi`, `ignore_config_failure`; C: `observer_cancellation` |
| CLI flushes each result on main thread before next native progress, preserves stderr/stdout and exits 0/1/2/130; intermediate write failure releases blocked observer and joins workers; late SIGINT suppresses aggregate; interruption during first rendering retains already printed output, exits 130 without an extra footer | `test_redirected_result_is_flushed_before_the_next_benchmark_starts`, `test_real_native_cli_flushes_first_result_before_second_progress`, `test_real_native_cli_render_failure_releases_blocked_observer`, `test_cli_interrupt_during_first_result_render_stops_the_suite`, `test_cli_exit_stream_and_early_stop`, `test_interrupted_completed_cli_suite_has_no_aggregate`, `test_cli_repeated_interrupt_has_priority_over_completed_result`, `test_sigint_at_worker_lifecycle_boundaries_waits_for_teardown`, `test_real_ctrl_c_cancels_each_phase_and_stops_the_suite` | P: `intermediate_result_is_flushed`, `native_next_waits_until_render_ack`, `rendering_stays_on_main_thread`, `render_failure_cancels_owned_suite`, `intermediate_render_interrupt_is_quiet` |
| Final summary failure/count mismatch or a prefix-only backend cannot become CLI success; preserve unrelated native aggregate, zero and noncanonical presence without recalculation | `test_cli_suite_getter_failure_after_completed_children_is_not_success`, `test_suite_rejects_mismatched_native_snapshot_count`, `test_suite_forwards_native_aggregate_without_recalculation`, `test_cli_rejects_a_prefix_only_backend_and_restores_worker_ownership` | P: `failed_suite_observer_is_not_skipped`, `native_snapshot_count_checked`, `native_presence_not_truthiness_of_score`, `nonzero_native_presence_flags`, `native_aggregate_not_rounded`, `native_aggregate_not_recomputed`, `prefix_only_backend_cannot_report_success` |
| Suite handle kind used for cancellation/SIGINT/free; loading/allocation/execution/free lifecycle leaves no live handle or lost owner; callback/getter/result-observer errors release captures without GC after native return | `test_suite_cancellation_targets_its_owned_native_handle`, `test_sigint_defers_through_handle_lifetime_and_restores_runner`, `test_suite_child_decoder_failure_keeps_prefix_and_frees_once`, `test_suite_getter_failures_release_callback_captures_without_gc`, `test_live_callback_capture_released_without_gc`, `test_result_observer_exceptions_wait_for_native_return_and_release_captures` | P: `suite_cancel_mode`, `suite_interrupt_cancel_mode`, `suite_free_mode`, `signal_cannot_cancel_freeing_handle`, `suite_callback_failure_cancels_native`, `child_decode_failure_stops_and_keeps_prefix`, `suite_callback_traceback_cycles_cleared`, five `result_observer_*` checks |

## 3. Validation

| Scenarios | Exact tests | Directed mutations / evidence |
|---|---|---|
| Unknown benchmark before warmup before measurement; exact API/CLI errors; zero warmup allowed, zero/negative-zero measurement rejected; finite, negative, huge/subnanosecond and NaN cases retain requested values | `test_validation_preserves_exact_errors_precedence_and_requested_values`, `test_native_duration_limits_preserve_requested_diagnostics`, `test_programmatic_cli_rejects_invalid_durations_with_option_diagnostic`; Rust `validation_rules_and_messages_preserve_each_interface`, `invalid_config_matrix`; Swift `invalidDurationsAndRecovery` | C: `unknown_id`, `warmup_finite`, `measurement_finite`, `warmup_zero`, `measurement_zero`, `warmup_reason`, `measurement_reason`, `cli_reason` |
| Invalid reentry retains option-error precedence; numeric conversion of both arguments precedes loading/validation; bad benchmark types (including None) raise TypeError instead of selecting a suite; invalid suite options do not allocate; CLI warmup error precedes measurement | `test_reentrant_invalid_request_keeps_validation_precedence`, `test_invalid_python_types_raise_before_start_and_do_not_poison_runner`, `test_all_entrypoints_convert_both_numbers_before_validation_or_loading`, `test_invalid_benchmark_types_cannot_select_a_suite`, `test_numeric_conversion_keeps_priority_over_invalid_benchmark_type`, `test_suite_invalid_durations_keep_exact_diagnostics_without_allocation`, `test_cli_invalid_warmup_precedes_invalid_measurement` | P: `invalid_name_types_rejected`, `type_check_after_numeric_conversion`, `both_float_conversions_precede_native_load`, `warmup_conversion_precedes_measurement`, `measurement_conversion_not_skipped`, `validation_before_busy_diagnostic`, `validate_before_handle_allocation`, `cli_duration_argument_order` |
| Loading failure wins before a core is published, without releasing another call's owner; an executing runner reuses its existing core for validation/busy diagnostics rather than attempting a failing reload; ordinary loader errors remain failed results/clean CLI output | `test_busy_initialization_load_failure_keeps_priority_without_releasing_owner`, `test_busy_runner_uses_its_loaded_core_when_new_loading_fails`, `test_loading_failure_has_priority_over_invalid_arguments`, `test_cli_loader_failure_is_rendered_without_a_traceback` | P: `loading_priority_during_busy_initialization`, `busy_loaded_core_reuse`, `loser_must_not_release_owner`, `runtime_load_error_is_failed_result`, `cli_validation_runtime_load_error_is_quiet` |
| Existing C vocabulary/validation precedence remains exact, including reserved/unknown/duration/subnanosecond failure and requested NaN/signed-zero bits | Rust `existing_c_calls_keep_their_exact_validation_diagnostics` | C: `legacy_unknown_reason`, `legacy_reserved_reason`, `legacy_duration_reason`, `legacy_tiny_reason`, `legacy_warmup_bits`, `legacy_measurement_bits` |
| Names are exact bytes: no trimming, case folding, hyphen replacement, prefix/suffix matching or NUL truncation; UTF-8/surrogates rejected as unknown without encoding errors | Rust `names_are_length_delimited_and_catalog_driven`; Python `test_validation_preserves_exact_errors_precedence_and_requested_values`, `test_python_transports_native_registry_ids_and_utf8_byte_lengths` | C: `name_trim`, `name_case`, `name_hyphen`, `name_nul`, `name_len`; A: `python-native-surrogates`, `python-native-nul` |

Native-free imports, calibration, signatures, direct storage constructors and
CLI help/version are separate from runtime validation, which may load Rust.
The loading-first failure contract is explicitly tested rather than retaining
handwritten Python validation as a fallback. Help/version tests forbid both
Python Native aliases; P `help_does_not_initialize_native` catches eager loading.

## 4. Benchmark catalog

| Scenarios | Exact tests | Directed mutations / evidence |
|---|---|---|
| Rust name/order changes regenerate metadata; checked snapshot is fresh; changed values reach fresh Python import/models/signatures and CLI settings rather than handwritten copies | `test_rust_generated_snapshot_tracks_registry_defaults_and_calibration`, `test_python_metadata_and_signature_defaults_are_native_free` | M: `freeze-generated-benchmark-order`, `reverse-public-registry`, `reuse-stale-generated-snapshot`, `handwritten-python-registry` |
| Binding accepts arbitrary native-returned ID and full UTF-8 byte length, including NUL/surrogates; does not reconstruct 1/2 from strings | `test_python_transports_native_registry_ids_and_utf8_byte_lengths`; Rust `names_are_length_delimited_and_catalog_driven` | C: `name_len`, `unknown_id`, `name_nul`; P: `native_id_used_without_python_registry`, `utf8_byte_length`, `unknown_name_surrogates_remain_unknown` |
| Execution/aggregation follow the Rust catalog; old C identities/layouts remain stable | Rust `suite_order_and_stop_after_every_unsuccessful_position`, `incomplete_invalid_or_failed_suites_have_no_full_score`; existing unpacked Rust/C consumers | C observer mutations; legacy ABI/platform gates below |

## 5. Default options

| Scenarios | Exact tests | Directed mutations / evidence |
|---|---|---|
| Native-free Python constants/signatures/dataclass defaults remain real floats, not sentinels; current and altered Rust defaults reach APIs, models and CLI; explicit values bypass defaults without rounding | `test_python_metadata_and_signature_defaults_are_native_free`, `test_rust_generated_snapshot_tracks_registry_defaults_and_calibration`, `test_public_api_forwards_durations_and_callback_without_running_long_benchmarks`, `test_defaults_repeated_options_and_environment_independence` | M: `integer-default-instead-of-float`, `default-signature-sentinel`, `freeze-generated-defaults`, `round-generated-float-defaults`, `round-measurement-default`, `handwritten-python-warmup`, `handwritten-python-measurement` |
| Swift obtains omitted windows from ABI-checked native config; getter failure publishes nothing; both entry points preserve explicit signed-zero bits and partially omitted arguments; rebuilt native defaults prove no current-value fallback | Swift `defaultLoaderChecksABIAndGetterBeforePublishingValues`, `defaultsFollowNativeConfigAndPreserveExplicitSignedZero`; Rust `defaults_versions_and_layout`, `public_defaults_names_and_shared_cancellation` | D: `freeze_public_warmup`, `freeze_public_measurement`, `freeze_suite_arguments`, `freeze_single_arguments`, `warmup_zero_sentinel`, `measurement_zero_sentinel`, `lose_warmup_signed_zero`, `lose_measurement_signed_zero`, `ignore_abi_guard`, `ignore_getter_failure`, `publish_failed_getter_payload`; altered-core baseline uses 0.125/0.375 |

## 6. Calibration and build publication

| Scenarios | Exact tests | Directed mutations / evidence |
|---|---|---|
| Generated values derive from Rust, survive a real incremental rebuild, and need no historical JSON; fresh Python consumes regenerated data | `test_rust_generated_snapshot_tracks_registry_defaults_and_calibration`; `test_calibration_fixture_python_and_native_metadata_agree_without_a_handle` | M: `freeze-reference-machine-name`, `round-generated-reference-score`, `reuse-stale-generated-snapshot`, `handwritten-python-reference-score` |
| Precise float types/bits including integral floats, negative zero, subnormal/max finite; large integer capacities remain integers; UTF-8, quote/backslash/control escaping; source/output Unicode paths | `test_rust_generated_snapshot_tracks_registry_defaults_and_calibration`, `test_python_metadata_and_signature_defaults_are_native_free` | M: `integer-or-signedzero-reference-score`, `normalize-negative-zero-score`, `underflow-subnormal-score`, `clamp-extreme-score`, `truncate-u64-capacity`, `round-reference-warmup`, `drop-unicode-metadata-characters`, `decode-snapshot-as-ascii`, `drop-json-quote-escaping`, `drop-json-backslash-escaping`, `drop-json-control-escaping` |
| Shared global identity, frozen nested tuples, serializable copies, UTC epoch/date precision, every terminal status, callback/thread reads and retained lifetime | `test_result_reference_fields_are_public_required_types_with_global_defaults`, `test_reference_models_are_frozen_including_nested_results_and_tuple`, `test_result_reference_serialization_copies_data_without_mutating_globals`, `test_reference_timestamp_is_aware_utc_and_json_keeps_primitive_epoch_seconds`, `test_globals_remain_identical_and_readable_during_callbacks_and_after_terminal_states`, `test_retained_result_metadata_outlives_runner_and_is_readable_during_later_run`; Swift `globalReferenceMetadataMatchesRecordedSeries`, `metadataCopiesStringsBeforeNextGetterAndOwnsEverySnapshot` | M: `mutable-reference-results`, `rounded-reference-score`, `string-memory-capacity`, `lose-shared-reference-identity`; existing native/reference ownership tests |
| Use exact Cargo-reported path and UTF-8 JSON; reject missing/ambiguous reports and stale conventional output; retain build environment/working directory | `test_build_publishes_exact_reported_metadata_atomically` | M: `drop-cargo-json-format`, `lose-build-environment`, `wrong-build-working-directory`, `pick-stale-conventional-output`, `ignore-ambiguous-output-report`, `lose-utf8-cargo-decoding`, `publish-empty-snapshot` |
| Stage library and metadata before publishing; each copy/write/rename failure restores old complete files; first-install failure/KeyboardInterrupt removes new library; no staging remnants | `test_build_publishes_exact_reported_metadata_atomically`, `test_first_install_metadata_failure_removes_unpublished_library`, `test_developer_build_copies_the_fresh_host_library_after_successful_cargo`, `test_native_build_installs_fresh_target_binary_in_wheel_or_editable_package` | M: `publish-in-place-before-replace`, `ignore-metadata-publication-error`, `leave-staging-file`, `replace-library-before-staging-metadata`, `skip-backup-restoration`, `skip-first-install-rollback`, `ignore-interruption-rollback` |
| Package generated snapshot, build script and catalog in source/wheel installs; poison source snapshot before real sdist-to-wheel build so stale checked metadata cannot ship; native/Swift consumers remain Python-independent | `test_sdist_builds_an_installable_wheel_with_api_and_both_cli_entrypoints`; existing crate/native ZIP/offline Swift consumer tests in `test_reference_artifacts.py`; `scripts/check_source_install.py` | W: `artifact-stale-snapshot`, killed at wheel `reference_machine` assertion; unpacked source/wheel/native/Swift consumer gates passed |

Publication uses atomic replacement of each file and rollback on a failed
publication. Two separate renames do not promise an arbitrary concurrent reader
an atomic transaction over the library/snapshot pair.

## Cross-cutting gates and scope

- Public typing: `test_result_reference_public_type_contract` and
  `test_positive_result_reference_consumer_imports_without_native_code`, plus
  strict mypy/lint. Metadata constants keep `Tuple[str, ...]` and `float` types.
- Compatibility: Rust 1.74 library/example CI; existing C/C++ and legacy ABI-1/V2
  consumers, explicit wire-layout tests, unpacked distribution consumers and the
  normal Swift deployment target. New APIs are additive.
- Manual C declarations, Swift enum cases and ctypes wire structures are fixed
  ABI marshaling, not independent benchmark policy. The historical
  `native/reference-calibration.json` remains independent provenance evidence;
  runtime generation does not read it.
- Arbitrary object/complex-valued fields outside the annotated public model
  types, undefined C pointers/alignment/lifetimes, genuine OOM, speed thresholds
  and an atomic transaction for arbitrary concurrent two-file readers are not
  claimed contracts. Real supported float/string/status values and ordinary
  Python numeric conversion errors remain covered.

## Centralization validation before iterative review

Verified on macOS ARM64; the repository's Linux/Windows/other-architecture CI
matrix remains necessary for those platforms. Counts below describe separate
recorded selections, not an invented combined collection total.

| Check | Verified result / evidence |
|---|---|
| Python excluding artifact consumers | 1326 passed, plus 5 subsequently added resource/core cases passed; `target/core-centralization-audit/python-coverage-tests.log` |
| Artifact and native consumers | 144 passed, 1 legacy-Python compatibility skip; `target/core-centralization-audit/artifact-tests.log` |
| Python runtime statement/branch coverage | 591 statements and 108 branches, all covered; `target/core-centralization-audit/python-coverage.txt` |
| Rust unit tests and native coverage | 71 tests passed; 100% lines, regions, functions and instantiations; `target/core-centralization-audit/rust-coverage.log` |
| Swift tests and native coverage | 41 tests passed; 100% lines, regions, functions and instantiations; `target/core-centralization-audit/swift-coverage.log` |
| Thread Sanitizer | Actual concurrent-suite ownership run passed; `target/swift-suite-thread-sanitizer.log` |
| Build/package utilities | 359 passed; `target/core-centralization-audit/build-tests.log` |
| Minimum Rust and static checks | Rust 1.74 library/example, cargo fmt/Clippy, Ruff, strict mypy for package/tests and git diff whitespace checks passed |

LLVM's native reports have no branch counters: native 100% coverage above does
not claim branch coverage. Generated-package subprocesses remove inherited
coverage variables so independent library copies cannot inflate the runtime
coverage inventory.

## Iterative post-transfer review

Eight rounds each used three fresh, independent, read-only reviewers. The last
two rounds inspected the final production implementation and both returned zero
new confirmed regressions. Speculation, resolved duplicates and the unsupported
inputs listed above were not counted as useful findings.

| Round | Useful findings | Confirmed scenario and correction |
|---|---:|---|
| 1 | 1 | Signal reentry before ownership publication overwrote the single/suite handle kind; claim ownership before publishing the mode. |
| 2 | 1 | SIGINT after a completed rename mixed publication generations; protect both renames and roll back only an incomplete pair. |
| 3 | 1 | A backend error after aborted CLI output published a second result to an absent consumer; stop publishing after abort. |
| 4 | 1 | Ctrl+C could not interrupt intermediate output blocked by a full pipe; preserve interruptible render/flush/separator operations and bypass stdout for a pending interruption. |
| 5 | 1 | SIGINT immediately after installing the output handler skipped lifetime-handler restoration; protect the transition itself. |
| 6 | 2 | SIGINT could also interrupt handler restoration; latch the first output Ctrl+C before raising and defer repeats through cleanup. SIGINT during publication rollback destroyed the backup; defer default main-thread SIGINT through commit/rollback, check between commits and rethrow after restoring consistency. |
| 7 | 0 | Python runtime, Rust/C/Swift boundaries and integration/build review. |
| 8 | 0 | Independent repeat of the full review; second consecutive clean round. |

Each confirmed finding had a failing regression assertion before its production
fix and passing assertions afterward. New tests document their purpose; signal
and blocked-output probes use real signals, native calls or filesystem mutations
with explicit synchronization and bounded cleanup.

| Contract | Regression tests |
|---|---|
| Reentrant signal preserves native handle kind and runner reuse | `test_reentrant_signal_before_suite_claim_preserves_native_handle_kind` |
| Publication stays consistent after either rename and during rollback, for existing and first installations | `test_publication_signal_after_rename_keeps_one_complete_generation`, `test_sigint_during_publication_rollback_keeps_complete_files` |
| Publication preserves background-thread and custom/ignored signal support | `test_publication_preserves_thread_and_signal_handler_support` |
| Aborted output cannot deadlock a second backend publication | `test_cli_output_abort_does_not_wait_for_a_second_backend_error` |
| Ctrl+C interrupts blocked render, flush and separator; pending interruption bypasses stdout | `test_sigint_interrupts_blocked_intermediate_output` |
| Repeated Ctrl+C across output-handler transitions waits for actual native cleanup | `test_repeated_sigint_at_output_handoff_waits_for_native_cleanup` |
| Custom/ignored output signal policy, background CLI and late-interrupt precedence remain intact | `test_intermediate_output_preserves_caller_sigint_policy`, `test_cli_can_run_outside_main_thread`, `test_cli_rejects_a_prefix_only_backend_and_restores_worker_ownership` |

All 22 additional directed mutations were killed by regression assertions
(rounds 1–6: 1, 2, 1, 4, 2, 12). Baselines, individual mutant logs, RED/GREEN
logs and the round ledger are under `target/post-transfer-review/`; each
`round-N/evidence.json` records its mutation results, and `rounds.json` records
the reviewers and the two-round clean streak. These are separate from the
210 centralization checks recorded earlier.

Final verification on macOS ARM64:

- Complete Python selection: 1500 passed, 1 legacy-Python compatibility skip;
  `final-tests.log`. Two subsequently added caller-policy cases and the two-case
  prefix/late-interrupt selection passed in `final-cli-policy-tests.log` and
  `final-late-interrupt-tests.log` (the latter includes one previously existing
  case). These selections are reported separately to avoid double counting.
- Python runtime coverage: all 605 statements and 118 branches covered;
  `final-coverage.txt`.
- Ruff, strict package mypy, test mypy and git whitespace checks passed.
- Rust and Swift implementation files were unchanged by these review fixes;
  their native tests, coverage and consumer checks are recorded in the preceding
  validation table. The full Python run includes artifact/consumer checks.

## Original-requirements completion review

Three independent reviewers (`requirements_architecture`,
`requirements_bindings`, `requirements_evidence`) each received the full
original task and all six duplication groups. All three checked every group,
README, binary C independence, thin binding boundaries, preserved behavior and
the test/mutation evidence. No remaining production duplication or behavioral
defect was confirmed. Fixed wire declarations, DTO construction, presentation
and generated snapshots do not independently implement benchmark rules.

Two further rounds each used three fresh reviewers, again receiving the entire
task and all six original duplication locations. Both returned zero new
confirmed findings after checking the corrections below. In total, nine
independent reviewers checked all six groups; the last two rounds were clean.

| Round | Reviewers | New production defects | New evidence findings |
|---|---|---:|---:|
| 1 | `requirements_architecture`, `requirements_bindings`, `requirements_evidence` | 0 | 2 |
| 2 | `requirements_round2_a`, `requirements_round2_b`, `requirements_round2_c` | 0 | 0 |
| 3 | `requirements_round3_a`, `requirements_round3_b`, `requirements_round3_c` | 0 | 0 |

This review identified an evidence gap: the Python finite-endpoint test had an
unmutated baseline, but the endpoint mutations selected only its Rust analogue.
Two renamed existing tests also lacked recorded failures under their own names.
The isolated audit `target/requirements-review/check_mutations.py` closes all
three targets without changing production code or weakening tests:

| Test | Individually reproduced regressions |
|---|---|
| `test_aggregate_preserves_actual_finite_float_endpoints` | Input/output maximum clamping, subnormal flushing and float32 narrowing; Python direct-product aggregation (7 mutations). |
| `test_invalid_durations_do_not_allocate_a_native_handle` | Allocation before validation, generic diagnostics, lost requested durations, invalid status or present score/ratio (7 mutations). |
| `test_positive_result_reference_consumer_imports_without_native_code` | Eager native loading on import or storage construction (2 mutations). |

The unmodified selection passed all 13 cases; every one of the 16 mutations
was detected by its designated test: 15 assertion failures and one explicit
`pytest.fail` on allocation before validation, with no collection/build failures.
Logs and exact selections are recorded in
`target/requirements-review/mutation-results.json`. These checks supplement
the earlier 210 + 22; they do not inflate those historical totals. No test
needed strengthening. This review also corrected the distinction between
assertion failures and the two expected Swift runtime traps above.
The same 13 selected cases passed again in the original checkout after the
isolated mutations (`target/requirements-review/final-verification.log`). No
production or test sources changed during this completion audit. Review
outcomes and the clean-round ledger are recorded in
`target/requirements-review/round-1-reviewers.json`, `round-2-reviewers.json`,
`round-3-reviewers.json` and `review-results.json`.

Original-source baselines and mutation logs establish the tested contracts and
outcomes. The complete historical order of edits and the original 18 reviewer
conversations additionally rely on task history; the repository does not
archive those conversations, and coverage alone is not a proof of completeness.
