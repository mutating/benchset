@testable import Benchset
import CBenchset
import Testing

private func aggregateInput(_ benchmark: Benchmark, _ status: UInt32, _ ratio: Double?) -> BenchmarkResult {
    BenchmarkResult(
        benchmark: benchmark, version: "test", status: status, reason: nil, score: 999,
        workers: 1, warmupSeconds: 0, measurementSeconds: 1, elapsedSeconds: 1,
        iterations: 1, checksum: 0, samples: [], variationPercent: nil, unstable: false,
        piEquivalent: ratio
    )
}

/// Rejects status independently of a retained ratio, including unknown native status codes.
/// Both positions guard caller-built snapshots that must not publish a successful aggregate.
@Test func aggregateRejectsUnsuccessfulResultsWithRetainedRatios() {
    for status: UInt32 in [BENCHSET_FAILED, BENCHSET_CANCELLED, UInt32.max] {
        for position in 0..<2 {
            var results = [aggregateInput(.singleCore, BENCHSET_COMPLETED, 0.5),
                           aggregateInput(.multiCore, BENCHSET_COMPLETED, 2)]
            results[position] = aggregateInput(results[position].benchmark, status, results[position].piEquivalent)
            #expect(SuiteResult(results: results).aggregatedScores.piEquivalent == nil)
        }
    }
}
