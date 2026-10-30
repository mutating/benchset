@testable import Benchset
import CBenchset
import Darwin
import Foundation
import Testing

private final class NativeStub {
    let pointer = OpaquePointer(bitPattern: 0x1234)!
    var abi: UInt32 = BENCHSET_ABI_VERSION
    var abiChecks = 0
    var allocationFails = false
    var configAvailable = true
    var resultAvailable = true
    var missingSample: UInt64?
    var creates = 0
    var frees = 0
    var cancels = 0
    var configs: [BenchsetConfig] = []
    var value = BenchsetResult()
    var piEquivalent = 0.12345678901234567
    var hasPiEquivalent: UInt32 = 1
    var resultSizes: [UInt32] = []
    var samples: [BenchsetSample] = []
    var phases: [UInt32] = [1, 2, 3, 4, 5]
    var reasonPointer: UnsafeMutablePointer<CChar>?
    let versionPointer = strdup("test-version")!
    var action: ((NativeStub, NativeCallback, UnsafeMutableRawPointer) -> Void)?

    init() {
        value.abi_version = BENCHSET_ABI_VERSION
        value.status = 0
        value.workers = UInt32.max
        value.score = 123.25
        value.warmup_seconds = 2.5
        value.measurement_seconds = 3.5
        value.elapsed_seconds = 4.5
        value.iterations = UInt64.max
        value.checksum = UInt64.max - 1
        value.variation_percent = 0
        value.has_variation = 1
        value.unstable = 7
        var sample = BenchsetSample()
        sample.elapsed_seconds = 4.5
        sample.duration_seconds = 1.5
        sample.iterations = UInt64.max
        sample.iterations_per_second = 42.123456789012345
        samples = [sample]
        sample.elapsed_seconds = 9.75
        sample.duration_seconds = 2.25
        sample.iterations = UInt64.max - 7
        sample.iterations_per_second = 123.98765432109876
        samples.append(sample)
    }
    deinit { Darwin.free(reasonPointer); Darwin.free(versionPointer) }
    func setReason(_ string: String?) {
        Darwin.free(reasonPointer)
        reasonPointer = string.map { strdup($0)! }
    }
    var api: NativeAPI {
        NativeAPI(
            abiVersion: { self.abiChecks += 1; return self.abi },
            create: { self.creates += 1; return self.allocationFails ? nil : self.pointer },
            defaultConfig: { out in
                out.pointee = BenchsetConfig()
                out.pointee.abi_version = BENCHSET_ABI_VERSION
                return self.configAvailable ? 1 : 0
            },
            run: { handle, config, callback, context in
                #expect(handle == self.pointer)
                self.configs.append(config.pointee)
                if let action = self.action { action(self, callback, context) }
                else {
                    for phase in self.phases {
                        var progress = BenchsetProgress()
                        progress.phase = phase
                        progress.workers = UInt32.max
                        progress.fraction = 0.12345678901234567
                        progress.phase_elapsed_seconds = 1.2345678901234567
                        progress.phase_duration_seconds = 2.345678901234567
                        progress.iterations = UInt64.max
                        withUnsafePointer(to: &progress) { callback($0, context) }
                    }
                }
                return self.value.status
            },
            cancel: { handle in #expect(handle == self.pointer); self.cancels += 1 },
            result: { _, out, size in
                self.resultSizes.append(size)
                #expect(size == UInt32(MemoryLayout<BenchsetResultV2>.size))
                self.value.sample_count = UInt64(self.samples.count)
                out.pointee = BenchsetResultV2(base: self.value, pi_equivalent: self.piEquivalent,
                                              has_pi_equivalent: self.hasPiEquivalent)
                return self.resultAvailable ? 1 : 0
            },
            sample: { _, index, out in
                if self.missingSample == index { return 0 }
                out.pointee = self.samples[Int(index)]
                return 1
            },
            reason: { _ in self.reasonPointer.map { UnsafePointer($0) } },
            version: { UnsafePointer(self.versionPointer) },
            free: { handle in #expect(handle == self.pointer); self.frees += 1 }
        )
    }
}

private final class LockedBox<T> {
    private let lock = NSLock()
    private var storage: T
    init(_ value: T) { storage = value }
    var value: T {
        get { lock.lock(); defer { lock.unlock() }; return storage }
        set { lock.lock(); defer { lock.unlock() }; storage = newValue }
    }
}

func expectRunnerReusable(_ runner: BenchmarkRunner, warmupSeconds: Double = 10) throws {
    var started = false
    let result = try runner.run(.singleCore, warmupSeconds: warmupSeconds, measurementSeconds: 10) {
        if $0.phase == 1 || $0.phase == 2 {
            started = true
            runner.cancel()
        }
    }
    #expect(started)
    #expect(result.status == 1)
    #expect(result.score == nil && result.piEquivalent == nil)
    expectResultReference(result)
}

@Suite(.serialized)
struct BenchsetTests {

    /// Keep the suite reservation across native-handle teardown and replacement.
    /// A contender queued during free must never run between the owner's two
    /// benchmarks; both another suite and a standalone run obey that reservation.
    /// Repeat queued contenders to exercise scheduler handoffs without requiring
    /// either thread to acquire the lock first in any particular iteration.
    @Test func suiteReservationSurvivesNativeHandleReplacement() throws {
        for suiteContender in [false, true] {
            for _ in 0..<100 {
                let stub = NativeStub()
                let freeing = DispatchSemaphore(value: 0)
                let attempting = DispatchSemaphore(value: 0)
                let finished = DispatchSemaphore(value: 0)
                let secondStarted = LockedBox(false)
                let intruded = LockedBox(false)
                let owner = Thread.current
                var native = stub.api
                let free = native.free
                native.free = { handle in
                    free(handle)
                    if stub.frees == 1 {
                        freeing.signal()
                        #expect(attempting.wait(timeout: .now() + 5) == .success)
                        usleep(1000)
                    }
                }
                stub.action = { stub, _, _ in
                    if Thread.current === owner {
                        if stub.configs.last?.benchmark == Benchmark.multiCore.rawValue { secondStarted.value = true }
                    } else if !secondStarted.value { intruded.value = true }
                }
                let runner = BenchmarkRunner(native: native)
                DispatchQueue.global().async {
                    defer { finished.signal() }
                    guard freeing.wait(timeout: .now() + 5) == .success else { return }
                    attempting.signal()
                    if suiteContender { _ = try? runner.runAll() }
                    else { _ = try? runner.run(.singleCore) }
                }
                let result = try? runner.runAll()
                #expect(finished.wait(timeout: .now() + 5) == .success)
                try #require(result?.results.count == 2)
                try #require(!intruded.value)
            }
        }
    }
    /// Concurrent suite contenders must share one owner while cancellation remains safe.
    /// The blocked native call makes overlap deterministic; Thread Sanitizer also
    /// checks the reservation and cleanup against concurrent cancellation reads.
    @Test func concurrentSuitesKeepSingleOwnerDuringCancellation() throws {
        let stub = NativeStub()
        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        stub.action = { stub, _, _ in
            if stub.configs.count == 1 {
                entered.signal()
                release.wait()
            }
        }
        let runner = BenchmarkRunner(native: stub.api)
        let start = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let outcomes = LockedBox<[String]>([])
        let outcomeLock = NSLock()
        let group = DispatchGroup()
        for _ in 0..<8 {
            group.enter()
            DispatchQueue.global().async {
                start.wait()
                let outcome: String
                do {
                    let result = try runner.runAll(warmupSeconds: 0, measurementSeconds: 0.01)
                    outcome = result.results.count == 2 ? "owner" : "incomplete"
                } catch BenchsetError.alreadyRunning { outcome = "busy" }
                catch { outcome = "unexpected" }
                outcomeLock.lock()
                outcomes.value = outcomes.value + [outcome]
                outcomeLock.unlock()
                finished.signal()
                group.leave()
            }
        }
        for _ in 0..<8 { start.signal() }
        defer { release.signal(); _ = group.wait(timeout: .now() + 5) }
        #expect(entered.wait(timeout: .now() + 5) == .success)
        let cancelDone = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            for _ in 0..<500 { runner.cancel(); usleep(100) }
            cancelDone.signal()
        }
        for _ in 0..<7 { #expect(finished.wait(timeout: .now() + 5) == .success) }
        release.signal()
        #expect(group.wait(timeout: .now() + 5) == .success)
        #expect(cancelDone.wait(timeout: .now() + 5) == .success)
        #expect(outcomes.value.filter { $0 == "owner" }.count == 1)
        #expect(outcomes.value.filter { $0 == "busy" }.count == 7)
        #expect(stub.creates == 2 && stub.frees == 2)
    }

    /// Keeps progress on the caller thread, full sample payloads and one result per benchmark.
    /// Detects lossy wrapping, dropped callbacks and accepted duplicate/trailing benchmarks.
    @Test func fullSuiteGeometricMeanAndForwarding() throws {
        let stub = NativeStub()
        stub.action = { stub, callback, context in
            stub.piEquivalent = stub.configs.count == 1 ? 0.5 : 2.0
            for phase in stub.phases {
                var progress = BenchsetProgress()
                progress.phase = phase
                progress.workers = UInt32.max - UInt32(stub.configs.count)
                progress.fraction = 0.12345678901234567
                progress.phase_elapsed_seconds = 1.2345678901234567
                progress.phase_duration_seconds = 2.345678901234567
                progress.iterations = UInt64.max - UInt64(stub.configs.count)
                withUnsafePointer(to: &progress) { callback($0, context) }
            }
        }
        let runner = BenchmarkRunner(native: stub.api)
        var benchmarks: [Benchmark] = []
        var phases: [UInt32] = []
        let caller = Thread.current
        let result = try runner.runAll(warmupSeconds: 0.25, measurementSeconds: 0.75) { benchmark, progress in
            #expect(Thread.current === caller)
            benchmarks.append(benchmark)
            phases.append(progress.phase)
            let ordinal: UInt64 = benchmark == .singleCore ? 1 : 2
            #expect(progress.workers == UInt32.max - UInt32(ordinal))
            #expect(progress.fraction.bitPattern == (0.12345678901234567 as Double).bitPattern)
            #expect(progress.phaseElapsedSeconds.bitPattern == (1.2345678901234567 as Double).bitPattern)
            #expect(progress.phaseDurationSeconds.bitPattern == (2.345678901234567 as Double).bitPattern)
            #expect(progress.iterations == UInt64.max - ordinal)
        }
        #expect(phases == stub.phases + stub.phases)
        #expect(result.results.map { $0.benchmark } == [.singleCore, .multiCore])
        #expect(result.results.map { $0.piEquivalent } == [0.5, 2.0])
        for snapshot in result.results {
            #expect(snapshot.samples.count == stub.samples.count)
            for (actual, expected) in zip(snapshot.samples, stub.samples) {
                #expect(actual.elapsedSeconds == expected.elapsed_seconds)
                #expect(actual.durationSeconds == expected.duration_seconds)
                #expect(actual.iterations == expected.iterations)
                #expect(actual.iterationsPerSecond.bitPattern == expected.iterations_per_second.bitPattern)
            }
        }
        #expect(result.aggregatedScores.piEquivalent == 1.0)
        #expect(stub.configs.map { $0.benchmark } == [1, 2])
        #expect(stub.configs.allSatisfy { $0.warmup_seconds == 0.25 && $0.measurement_seconds == 0.75 })
        #expect(benchmarks == Array(repeating: .singleCore, count: stub.phases.count)
            + Array(repeating: .multiCore, count: stub.phases.count))
        #expect(stub.creates == 2 && stub.frees == 2)
        #expect(result.results.allSatisfy { $0.unstable })
        #expect(SuiteResult(results: []).aggregatedScores.piEquivalent == nil)
        #expect(SuiteResult(results: [result.results[0]]).aggregatedScores.piEquivalent == nil)
        #expect(SuiteResult(results: [result.results[0], result.results[0]]).aggregatedScores.piEquivalent == nil)
        #expect(SuiteResult(results: result.results.reversed()).aggregatedScores.piEquivalent == nil)
        #expect(SuiteResult(results: result.results + [result.results[1]]).aggregatedScores.piEquivalent == nil)
    }

    /// Stops at either unsuccessful benchmark and requires every Pi coefficient.
    /// Missing one ratio cannot silently become an aggregate of only the remaining values.
    /// No-argument suite calls keep the documented 120/60-second native windows.
    @Test func fullSuiteStopsAfterFailureOrCancellation() throws {
        for stop in [1, 2] {
            for status: UInt32 in [BENCHSET_FAILED, BENCHSET_CANCELLED] {
                let stub = NativeStub()
                stub.action = { stub, _, _ in
                    if stub.configs.count == stop { stub.value.status = status }
                }
                let result = try BenchmarkRunner(native: stub.api).runAll()
                #expect(stub.configs.allSatisfy { $0.warmup_seconds == 120 && $0.measurement_seconds == 60 })
                #expect(result.results.count == stop)
                #expect(result.results.last?.status == status)
                #expect(result.aggregatedScores.piEquivalent == nil)
                #expect(stub.frees == stop)
            }
        }
        let stub = NativeStub()
        stub.hasPiEquivalent = 0
        let missing = try BenchmarkRunner(native: stub.api).runAll()
        #expect(missing.results.count == 2)
        #expect(missing.aggregatedScores.piEquivalent == nil)
        for missingAt in [1, 2] {
            let partialStub = NativeStub()
            partialStub.action = { stub, _, _ in
                stub.hasPiEquivalent = stub.configs.count == missingAt ? 0 : 1
            }
            let partial = try BenchmarkRunner(native: partialStub.api).runAll()
            #expect(partial.results.count == 2)
            #expect(partial.results[missingAt - 1].piEquivalent == nil)
            #expect(partial.results[2 - missingAt].piEquivalent != nil)
            #expect(partial.aggregatedScores.piEquivalent == nil)
        }
        stub.hasPiEquivalent = 1
        stub.piEquivalent = 0
        let zero = try BenchmarkRunner(native: stub.api).runAll()
        #expect(zero.aggregatedScores.piEquivalent == 0)
        stub.allocationFails = true
        #expect(throws: BenchsetError.allocationFailed) {
            try BenchmarkRunner(native: stub.api).runAll()
        }
    }

    /// Guards log-space range, unrounded ratios and invalid inputs at either position before zero handling.
    /// Unit coefficients preserve the calibration baseline and equal benchmark weights.
    @Test func fullSuiteLogarithmicRangeZerosAndInvalidRatios() throws {
        let cases: [(Double, Double, Double, Double)] = [
            (1, 1, 1, 1e-15), (1, 4, 2, 1e-15), (4, 1, 2, 1e-15),
            (0, 2, 0, 1e-15), (2, 0, 0, 1e-15), (1e-300, 1e300, 1, 1e-12),
            (1e300, 1e300, 1e300, 1e-12), (1e-300, 1e-300, 1e-300, 1e-12),
            (1.0 / 3, 2.0 / 3, sqrt(2) / 3, 1e-15)
        ]
        for (single, multi, expected, tolerance) in cases {
            let stub = NativeStub()
            stub.action = { stub, _, _ in stub.piEquivalent = stub.configs.count == 1 ? single : multi }
            let suite = try BenchmarkRunner(native: stub.api).runAll()
            let actual = try #require(suite.aggregatedScores.piEquivalent)
            if expected == 0 || expected == 1 { #expect(actual == expected) }
            else { #expect(abs(actual / expected - 1) < tolerance) }
        }
        for invalid in [-1.0, Double.infinity, -Double.infinity, Double.nan] {
            for other in [0.0, 1.0] {
                for (single, multi) in [(invalid, other), (other, invalid)] {
                    let stub = NativeStub()
                    stub.action = { stub, _, _ in stub.piEquivalent = stub.configs.count == 1 ? single : multi }
                    let suite = try BenchmarkRunner(native: stub.api).runAll()
                    #expect(suite.aggregatedScores.piEquivalent == nil)
                }
            }
        }
    }

    /// Cancels the active first benchmark and prevents later work and a full-suite score.
    @Test func fullSuiteActualCancellation() throws {
        let runner = BenchmarkRunner()
        let result = try runner.runAll { benchmark, progress in
            #expect(benchmark == .singleCore)
            if progress.phase == BENCHSET_WARMUP || progress.phase == BENCHSET_MEASUREMENT { runner.cancel() }
        }
        #expect(result.results.count == 1)
        #expect(result.results[0].status == BENCHSET_CANCELLED)
        #expect(result.aggregatedScores.piEquivalent == nil)
    }

    /// Keeps cancellation across handles without erasing the completed benchmark's score or Pi value.
    /// The next benchmark must cancel before work, and the runner must remain reusable.
    @Test func fullSuiteCancellationSurvivesCompletedBenchmark() throws {
        let runner = BenchmarkRunner()
        let suite = try runner.runAll(warmupSeconds: 0, measurementSeconds: 0.08) { benchmark, progress in
            if benchmark == .singleCore && progress.phase == BENCHSET_PHASE_COMPLETED { runner.cancel() }
        }
        #expect(suite.results.map { $0.status } == [BENCHSET_COMPLETED, BENCHSET_CANCELLED])
        let first = suite.results[0]
        let score = Double(first.iterations) / first.elapsedSeconds
        #expect(score > 0)
        #expect(first.score == score)
        #expect(first.piEquivalent == score / piReferenceRun.results[0].score)
        #expect(suite.results[1].score == nil && suite.results[1].piEquivalent == nil)
        #expect(suite.results[1].iterations == 0)
        #expect(suite.aggregatedScores.piEquivalent == nil)
        try expectRunnerReusable(runner)
    }

    /// Rejects callback reentry and propagates native errors even after one benchmark completed.
    /// Checks cleanup and runner reuse so a second-result retrieval failure cannot become partial success.
    @Test func fullSuiteRejectsReentryAndRecoversAfterError() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        stub.action = { [unowned runner] _, _, _ in
            #expect(throws: BenchsetError.alreadyRunning) { try runner.runAll() }
            #expect(throws: BenchsetError.alreadyRunning) { try runner.run(.singleCore) }
        }
        #expect(try runner.runAll().results.count == 2)
        stub.allocationFails = true
        #expect(throws: BenchsetError.allocationFailed) { try runner.runAll() }
        stub.allocationFails = false
        #expect(try runner.runAll().results.count == 2)

        let failingRun = stub.configs.count + 2
        stub.action = { stub, _, _ in
            if stub.configs.count == failingRun { stub.resultAvailable = false }
        }
        let freesBefore = stub.frees
        var published: SuiteResult?
        #expect(throws: BenchsetError.resultUnavailable) { published = try runner.runAll() }
        #expect(published == nil)
        #expect(stub.configs.count == failingRun)
        #expect(stub.frees == freesBefore + 2)
        runner.cancel()
        #expect(stub.cancels == 0)
        stub.resultAvailable = true
        stub.action = nil
        #expect(try runner.runAll().results.count == 2)
    }

    @Test func resultAccountingAndProgress() throws {
        for benchmark in [Benchmark.singleCore, .multiCore] {
            let caller = Thread.current
            var events: [Benchset.Progress] = []
            let result = try BenchmarkRunner().run(benchmark, warmupSeconds: 0.03, measurementSeconds: 0.05) {
                #expect(Thread.current === caller)
                events.append($0)
            }
            #expect(result.success)
            expectResultReference(result)
            #expect(result.benchmark == benchmark)
            #expect(result.workers == (benchmark == .singleCore ? 1 : UInt32(ProcessInfo.processInfo.activeProcessorCount)))
            #expect((result.score ?? 0) > 0)
            #expect(result.score! == Double(result.iterations) / result.elapsedSeconds)
            let referenceScore = benchmark == .singleCore ? 47231.227029007096 : 181953.17419139884
            #expect(result.piEquivalent == result.score! / referenceScore)
            #expect(result.piEquivalent!.isFinite && result.piEquivalent! > 0)
            #expect(result.samples.reduce(0) { $0 + $1.iterations } == result.iterations)
            #expect(abs(result.samples.reduce(0) { $0 + $1.durationSeconds } - result.elapsedSeconds) < 1e-9)
            #expect(result.warmupSeconds == 0.03)
            #expect(result.measurementSeconds == 0.05)
            #expect(result.version == String(cString: benchset_version()))
            #expect(result.unit == "iterations/s")
            #expect(result.reason == nil)
            #expect(result.variationPercent == nil)
            #expect(!result.unstable)
            #expect(events.first?.phase == 1 || events.first?.phase == 2)
            #expect(events.contains { $0.phase == 2 })
            #expect(events.last?.phase == 3)
            #expect(events.last?.fraction == 1)
            #expect(events.last?.phaseElapsedSeconds == result.elapsedSeconds)
            #expect(events.last?.iterations == result.iterations)
            #expect(events.allSatisfy { (0...1).contains($0.fraction) && $0.workers == result.workers })
            #expect(zip(events, events.dropFirst()).allSatisfy { $0.fraction <= $1.fraction })
        }
    }

    @Test func cancellationAndReuseInEachPhase() throws {
        for phase: UInt32 in [1, 2] {
            let runner = BenchmarkRunner()
            runner.cancel()
            var events: [Benchset.Progress] = []
            let result = try runner.run(.multiCore, warmupSeconds: phase == 1 ? 10 : 0, measurementSeconds: 0.1) { progress in
                events.append(progress)
                if progress.phase == phase { runner.cancel(); runner.cancel() }
            }
            #expect(result.status == 1)
            #expect(!result.success)
            #expect(result.score == nil)
            #expect(result.piEquivalent == nil)
            #expect(result.reason == "cancelled")
            expectResultReference(result)
            #expect(events.last?.phase == 4)
            runner.cancel()
            try expectRunnerReusable(runner)
        }
    }

    @Test func samplesVariationAndLateCancellation() throws {
        let runner = BenchmarkRunner()
        let result = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 1.1)
        // A scheduler pause can skip the intermediate one-second snapshot.
        // Fixed native values below verify multi-sample transport separately.
        #expect(!result.samples.isEmpty)
        #expect((result.variationPercent != nil) == (result.samples.count >= 2))
        #expect(result.unstable == (result.variationPercent.map { $0 > 10 } ?? false))
        #expect(result.samples.reduce(0) { $0 + $1.iterations } == result.iterations)
        #expect(abs(result.samples.reduce(0) { $0 + $1.durationSeconds } - result.elapsedSeconds) < 1e-9)
        #expect(result.samples.last?.elapsedSeconds == result.elapsedSeconds)
        expectResultReference(result)
        let cancelled = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 1.5) {
            #expect($0.phase != 1)
            if $0.phase == 2 && $0.phaseElapsedSeconds > 1.1 { runner.cancel() }
        }
        #expect(cancelled.status == 1)
        #expect(!cancelled.samples.isEmpty)
        #expect(cancelled.score == nil)
        #expect(cancelled.piEquivalent == nil)
        expectResultReference(cancelled)
    }

    @Test func invalidDurationsAndRecovery() throws {
        let runner = BenchmarkRunner()
        let invalid: [(Double, Double)] = [(-1, 1), (.nan, 1), (.infinity, 1), (-.infinity, 1), (.greatestFiniteMagnitude, 1), (0, 0), (0, -1), (0, .nan), (0, .infinity), (0, -.infinity), (0, .greatestFiniteMagnitude), (0, 1e-12)]
        for (warmup, measurement) in invalid {
            let result = try runner.run(.singleCore, warmupSeconds: warmup, measurementSeconds: measurement)
            #expect(result.status == 2)
            #expect(!result.success)
            #expect(result.score == nil)
            #expect(result.piEquivalent == nil)
            #expect(result.reason?.isEmpty == false)
            expectResultReference(result)
            #expect(warmup.isNaN ? result.warmupSeconds.isNaN : result.warmupSeconds == warmup)
            #expect(measurement.isNaN ? result.measurementSeconds.isNaN : result.measurementSeconds == measurement)
        }
        try expectRunnerReusable(runner, warmupSeconds: -0.0)
    }

    @Test func reentrantSameRunnerAndGlobalBusy() throws {
        let runner = BenchmarkRunner()
        var checked = false
        let result = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.03) { progress in
            guard !checked, progress.phase == 2 else { return }
            checked = true
            #expect(throws: BenchsetError.alreadyRunning) {
                try runner.run(.multiCore, warmupSeconds: 0, measurementSeconds: 0.01)
            }
            let other = try! BenchmarkRunner().run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.01)
            #expect(other.status == 2)
            #expect(other.piEquivalent == nil)
            #expect(other.reason == "another benchmark is already running")
            expectResultReference(other)
        }
        #expect(checked)
        #expect(result.success)
        expectResultReference(result)
        try expectRunnerReusable(runner)
    }

    @Test func externalCancellationAndConcurrentRun() throws {
        let runner = BenchmarkRunner()
        let started = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let output = LockedBox<Result<BenchmarkResult, Error>?>(nil)
        DispatchQueue.global().async {
            let caller = Thread.current
            var first = true
            output.value = Result {
                try runner.run(.multiCore, warmupSeconds: 10, measurementSeconds: 10) { _ in
                    #expect(Thread.current === caller)
                    if first { first = false; started.signal(); #expect(release.wait(timeout: .now() + 5) == .success) }
                }
            }
            finished.signal()
        }
        #expect(started.wait(timeout: .now() + 5) == .success)
        defer { release.signal() }
        #expect(throws: BenchsetError.alreadyRunning) { try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.02) }
        runner.cancel()
        release.signal()
        #expect(finished.wait(timeout: .now() + 5) == .success)
        let cancelled = try output.value!.get()
        #expect(cancelled.status == 1)
        expectResultReference(cancelled)
        try expectRunnerReusable(runner)
    }

    @Test func cancellationDuringAllocationIsDeliveredToLiveHandle() throws {
        let stub = NativeStub()
        stub.value.status = 1
        stub.setReason("cancelled before starting")
        let allocating = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        let cancelStarted = DispatchSemaphore(value: 0)
        let cancelDone = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let output = LockedBox<Result<BenchmarkResult, Error>?>(nil)
        var native = stub.api
        native.create = {
            stub.creates += 1
            allocating.signal()
            #expect(release.wait(timeout: .now() + 5) == .success)
            return stub.pointer
        }
        stub.action = { _, _, _ in
            #expect(cancelDone.wait(timeout: .now() + 5) == .success)
        }
        let runner = BenchmarkRunner(native: native)
        DispatchQueue.global().async {
            output.value = Result { try runner.run(.singleCore) }
            finished.signal()
        }
        #expect(allocating.wait(timeout: .now() + 5) == .success)
        DispatchQueue.global().async {
            cancelStarted.signal()
            runner.cancel()
            cancelDone.signal()
        }
        #expect(cancelStarted.wait(timeout: .now() + 5) == .success)
        release.signal()
        #expect(finished.wait(timeout: .now() + 5) == .success)
        let cancelled = try output.value!.get()
        #expect(cancelled.status == 1)
        expectResultReference(cancelled)
        #expect(stub.cancels == 1)
        #expect(stub.creates == 1 && stub.frees == 1)
        runner.cancel()
        #expect(stub.cancels == 1)
    }

    @Test func cancellationDuringCleanupNeverUsesFreedHandle() throws {
        let stub = NativeStub()
        let freeing = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        let cancelStarted = DispatchSemaphore(value: 0)
        let cancelDone = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let blockFree = LockedBox(true)
        let output = LockedBox<Result<BenchmarkResult, Error>?>(nil)
        var native = stub.api
        native.free = { handle in
            #expect(handle == stub.pointer)
            stub.frees += 1
            if blockFree.value {
                freeing.signal()
                #expect(release.wait(timeout: .now() + 5) == .success)
            }
        }
        let runner = BenchmarkRunner(native: native)
        DispatchQueue.global().async {
            output.value = Result { try runner.run(.singleCore) }
            finished.signal()
        }
        defer { release.signal() }
        #expect(freeing.wait(timeout: .now() + 5) == .success)
        DispatchQueue.global().async {
            cancelStarted.signal()
            runner.cancel()
            cancelDone.signal()
        }
        #expect(cancelStarted.wait(timeout: .now() + 5) == .success)
        // Allow cancellation to attempt the runner lock while free is paused.
        // It may return early safely if the handle has already been cleared.
        let cancelledDuringFree = cancelDone.wait(timeout: .now() + 0.2) == .success
        release.signal()
        #expect(finished.wait(timeout: .now() + 5) == .success)
        if !cancelledDuringFree { #expect(cancelDone.wait(timeout: .now() + 5) == .success) }
        let result = try #require(output.value)
        let completed = try result.get()
        #expect(completed.success)
        expectResultReference(completed)
        #expect(stub.creates == 1 && stub.frees == 1 && stub.cancels == 0)
        blockFree.value = false
        let reused = try runner.run(.singleCore)
        #expect(reused.success)
        expectResultReference(reused)
        runner.cancel()
        #expect(stub.creates == 2 && stub.frees == 2 && stub.cancels == 0)
    }

    @Test func cleanupWaitsForAnInFlightNativeCancellation() throws {
        let stub = NativeStub()
        let running = DispatchSemaphore(value: 0)
        let releaseRun = DispatchSemaphore(value: 0)
        let cancelling = DispatchSemaphore(value: 0)
        let releaseCancel = DispatchSemaphore(value: 0)
        let freed = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let cancelDone = DispatchSemaphore(value: 0)
        let cancelInFlight = LockedBox(false)
        let output = LockedBox<Result<BenchmarkResult, Error>?>(nil)
        stub.action = { _, _, _ in
            running.signal()
            #expect(releaseRun.wait(timeout: .now() + 5) == .success)
        }
        var native = stub.api
        native.cancel = { handle in
            #expect(handle == stub.pointer)
            cancelInFlight.value = true
            cancelling.signal()
            #expect(releaseCancel.wait(timeout: .now() + 5) == .success)
            cancelInFlight.value = false
        }
        native.free = { handle in
            #expect(handle == stub.pointer)
            #expect(!cancelInFlight.value, "free must not overlap a native call using the handle")
            stub.frees += 1
            freed.signal()
        }
        let runner = BenchmarkRunner(native: native)
        defer { releaseRun.signal(); releaseCancel.signal() }
        DispatchQueue.global().async {
            output.value = Result { try runner.run(.singleCore) }
            finished.signal()
        }
        #expect(running.wait(timeout: .now() + 5) == .success)
        DispatchQueue.global().async {
            runner.cancel()
            cancelDone.signal()
        }
        #expect(cancelling.wait(timeout: .now() + 5) == .success)
        releaseRun.signal()
        // Give cleanup a chance to attempt free while cancellation owns the
        // handle. The assertion in free checks actual overlap, not elapsed time.
        _ = freed.wait(timeout: .now() + 0.2)
        releaseCancel.signal()
        #expect(cancelDone.wait(timeout: .now() + 5) == .success)
        #expect(finished.wait(timeout: .now() + 5) == .success)
        let result = try #require(output.value)
        #expect(try result.get().success)
        #expect(stub.frees == 1)
    }

    @Test func terminalCancellationKeepsCompletedResult() throws {
        let runner = BenchmarkRunner()
        let result = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.03) {
            if $0.phase == 3 { runner.cancel() }
        }
        #expect(result.success)
        #expect(result.score != nil)
        #expect(result.piEquivalent != nil)
        expectResultReference(result)
    }

    @Test func everyNativeFieldAndOptionalMapping() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        for status: UInt32 in [0, 1, 2] {
            stub.value.status = status
            for reason in [nil, "", "Причина ошибки"] as [String?] {
                stub.setReason(reason)
                var progress: [Benchset.Progress] = []
                let result = try runner.run(.multiCore, warmupSeconds: 0.75, measurementSeconds: 1.25) { progress.append($0) }
                #expect(result.benchmark == .multiCore)
                #expect(result.version == "test-version")
                #expect(result.status == status)
                expectResultReference(result)
                #expect(result.success == (status == 0))
                #expect(result.score == (status == 0 ? 123.25 : nil))
                #expect(result.piEquivalent == (status == 0 ? stub.piEquivalent : nil))
                #expect(result.reason == (reason?.isEmpty == false ? reason : nil))
                #expect(result.workers == UInt32.max)
                #expect(result.warmupSeconds == 2.5)
                #expect(result.measurementSeconds == 3.5)
                #expect(result.elapsedSeconds == 4.5)
                #expect(result.iterations == UInt64.max)
                #expect(result.checksum == UInt64.max - 1)
                #expect(result.unit == "iterations/s")
                #expect(result.variationPercent == 0)
                #expect(result.unstable)
                #expect(result.samples.count == 2)
                for (sample, expected) in zip(result.samples, stub.samples) {
                    #expect(sample.elapsedSeconds == expected.elapsed_seconds)
                    #expect(sample.durationSeconds == expected.duration_seconds)
                    #expect(sample.iterations == expected.iterations)
                    #expect(sample.iterationsPerSecond.bitPattern == expected.iterations_per_second.bitPattern)
                }
                #expect(progress.map { $0.phase } == [1, 2, 3, 4, 5])
                for p in progress {
                    #expect(p.workers == UInt32.max && p.iterations == UInt64.max)
                    #expect(p.fraction.bitPattern == (0.12345678901234567 as Double).bitPattern)
                    #expect(p.phaseElapsedSeconds.bitPattern == (1.2345678901234567 as Double).bitPattern)
                    #expect(p.phaseDurationSeconds.bitPattern == (2.345678901234567 as Double).bitPattern)
                }
                #expect(stub.configs.last?.benchmark == 2)
                #expect(stub.configs.last?.warmup_seconds == 0.75)
                #expect(stub.configs.last?.measurement_seconds == 1.25)
            }
        }
        stub.value.status = 0
        stub.value.score = 0
        #expect(try runner.run(.singleCore).score == 0)
        stub.value.has_variation = 0
        stub.value.unstable = 0
        stub.samples = []
        let result = try runner.run(.singleCore) // exercises public duration/default callback values
        #expect(result.variationPercent == nil)
        #expect(!result.unstable)
        #expect(result.samples.isEmpty)
        expectResultReference(result)
        #expect(stub.configs.last?.warmup_seconds == 120)
        #expect(stub.configs.last?.measurement_seconds == 60)
        #expect(stub.configs.last?.benchmark == 1)
        #expect(stub.frees == stub.creates)
    }

    @Test func variationUsesEveryNonzeroPresenceFlagAtDoublePrecision() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        let variation = 12.345678901234567
        stub.value.variation_percent = variation
        for present: UInt32 in [0, 1, 7, UInt32.max] {
            stub.value.has_variation = present
            for status: UInt32 in [0, 1, 2] {
                stub.value.status = status
                let result = try runner.run(.singleCore)
                #expect(result.variationPercent?.bitPattern == (present == 0 ? nil : variation.bitPattern))
            }
        }
        #expect(stub.creates == stub.frees)
    }

    @Test func nativeErrorsFreeExactlyOnceAndRunnerIsReusable() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        stub.allocationFails = true
        #expect(throws: BenchsetError.allocationFailed) { try runner.run(.singleCore) }
        #expect(stub.frees == 0)
        stub.allocationFails = false
        stub.configAvailable = false
        #expect(throws: BenchsetError.resultUnavailable) { try runner.run(.singleCore) }
        #expect(stub.frees == 1)
        stub.configAvailable = true
        stub.resultAvailable = false
        #expect(throws: BenchsetError.resultUnavailable) { try runner.run(.singleCore) }
        #expect(stub.frees == 2)
        stub.resultAvailable = true
        stub.missingSample = 1
        #expect(throws: BenchsetError.resultUnavailable) { try runner.run(.singleCore) }
        #expect(stub.frees == 3)
        stub.missingSample = nil
        let recovered = try runner.run(.singleCore)
        #expect(recovered.success)
        expectResultReference(recovered)
        #expect(stub.frees == 4)
        runner.cancel()
        #expect(stub.cancels == 0)
    }

    @Test func nullableCallbacksAndContextLifetime() throws {
        final class Probe { var calls = 0 }
        weak var observed: Probe?
        func makeCallback() -> (Benchset.Progress) -> Void {
            let probe = Probe()
            observed = probe
            return { _ in probe.calls += 1 }
        }
        let stub = NativeStub()
        stub.action = { _, callback, context in
            #expect(observed != nil)
            callback(nil, context)
            var progress = BenchsetProgress()
            withUnsafePointer(to: &progress) { callback($0, nil); callback($0, context) }
            callback(nil, nil)
            #expect(observed?.calls == 1)
        }
        let runner = BenchmarkRunner(native: stub.api)
        let completed = try runner.run(.singleCore, onProgress: makeCallback())
        #expect(completed.success)
        expectResultReference(completed)
        #expect(observed == nil)
        #expect(stub.frees == 1)
    }

    /// Release callback captures, the runner and the fake native owner on every exit.
    /// A saved action receives its owner as an argument, avoiding a stub/action
    /// retain cycle that would also leak the stub's C strings and sample storage.
    @Test func callbackCapturesAndRunnerReleaseOnAllExits() throws {
        final class Probe { var calls = 0 }
        for stage in ["abi", "allocation", "config", "result", "sample", "completed"] {
            weak var observedStub: NativeStub?
            func exercise() throws {
                let stub = NativeStub()
                observedStub = stub
                stub.action = { stub, callback, context in
                    var progress = BenchsetProgress()
                    progress.workers = stub.value.workers
                    withUnsafePointer(to: &progress) { callback($0, context) }
                }
                stub.abi = stage == "abi" ? 0 : BENCHSET_ABI_VERSION
                stub.allocationFails = stage == "allocation"
                stub.configAvailable = stage != "config"
                stub.resultAvailable = stage != "result"
                stub.missingSample = stage == "sample" ? 0 : nil
                weak var observed: Probe?
                weak var observedRunner: BenchmarkRunner?
                func makeCallback() -> (Benchset.Progress) -> Void {
                    let probe = Probe()
                    observed = probe
                    return { _ in probe.calls += 1 }
                }
                do {
                    let runner = BenchmarkRunner(native: stub.api)
                    observedRunner = runner
                    var published: BenchmarkResult?
                    if stage == "completed" {
                        let completed = try runner.run(.singleCore, onProgress: makeCallback())
                        published = completed
                        #expect(completed.success)
                        expectResultReference(completed)
                    } else {
                        let error: BenchsetError = stage == "allocation" ? .allocationFailed : .resultUnavailable
                        #expect(throws: error) {
                            published = try runner.run(.singleCore, onProgress: makeCallback())
                        }
                        #expect(published == nil)
                    }
                    #expect(observed == nil)
                    #expect(stub.frees == (stage == "abi" || stage == "allocation" ? 0 : 1))
                }
                #expect(observedRunner == nil)
            }
            try exercise()
            #expect(observedStub == nil)
        }
    }

    @Test func incompatibleABIStopsBeforeCreatingHandleAndPermitsRetry() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        for abi: UInt32 in [0, 2, 3, UInt32.max] {
            stub.abi = abi
            #expect(throws: BenchsetError.resultUnavailable) { try runner.run(.singleCore) }
            #expect(stub.creates == 0 && stub.frees == 0)
            #expect(stub.configs.isEmpty && stub.resultSizes.isEmpty)
        }
        stub.abi = BENCHSET_ABI_VERSION
        let recovered = try runner.run(.singleCore)
        #expect(recovered.piEquivalent == stub.piEquivalent)
        expectResultReference(recovered)
        #expect(stub.creates == 1 && stub.frees == 1)
        #expect(stub.abiChecks == 5)
    }

    @Test func nativeMultiplierIsCopiedAtDoublePrecisionWithPresenceAndStatus() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        // These values intentionally disagree with score / reference: Swift maps
        // the authoritative native multiplier rather than recomputing it.
        for benchmark in [Benchmark.singleCore, .multiCore] {
            for ratio in [0.0, 0.5, 1.0, 2.0, 0.12345678901234567, 1e-12, 1e6] {
                stub.piEquivalent = ratio
                for present: UInt32 in [0, 1, 7, UInt32.max] {
                    stub.hasPiEquivalent = present
                    for status: UInt32 in [0, 1, 2] {
                        stub.value.status = status
                        let result = try runner.run(benchmark)
                        if status == 0 && present != 0 {
                            #expect(result.piEquivalent?.bitPattern == ratio.bitPattern)
                        } else {
                            #expect(result.piEquivalent == nil)
                        }
                    }
                }
            }
        }
        stub.value.status = 0
        stub.value.score = 0
        stub.piEquivalent = 0
        stub.hasPiEquivalent = 1
        let zero = try runner.run(.singleCore)
        #expect(zero.score == 0 && zero.piEquivalent == 0)
        expectResultReference(zero)
        #expect(stub.creates == stub.frees)
    }

    @Test func completedStabilityBoundaryAndValueOwnershipPreserveMultiplier() throws {
        let stub = NativeStub()
        let runner = BenchmarkRunner(native: stub.api)
        for variation in [10.0, 10.0.nextUp] {
            stub.value.variation_percent = variation
            stub.value.unstable = variation > 10 ? 1 : 0
            let result = try runner.run(.multiCore)
            #expect(result.success && result.score == stub.value.score)
            #expect(result.piEquivalent?.bitPattern == stub.piEquivalent.bitPattern)
            #expect(result.unstable == (variation > 10))
            expectResultReference(result)
        }
        stub.setReason("Скопированная причина")
        let originalRatio = stub.piEquivalent
        var native = stub.api
        native.free = { handle in
            #expect(handle == stub.pointer)
            stub.frees += 1
            stub.piEquivalent = 99
            stub.samples.removeAll()
            stub.setReason(nil)
        }
        let result = try BenchmarkRunner(native: native).run(.singleCore)
        #expect(result.piEquivalent == originalRatio)
        #expect(result.reason == "Скопированная причина")
        #expect(result.samples.count == 2)
        #expect(result.version == "test-version")
        expectResultReference(result)
        #expect(stub.creates == stub.frees)
    }

    @Test func resultReferencesOutliveNativeCleanupAndRunner() throws {
        for benchmark in [Benchmark.singleCore, .multiCore] {
            for status: UInt32 in [0, 1, 2] {
                let stub = NativeStub()
                stub.value.status = status
                stub.setReason("native reason")
                weak var observedRunner: BenchmarkRunner?
                var retained: BenchmarkResult?
                do {
                    var native = stub.api
                    native.free = { handle in
                        #expect(handle == stub.pointer)
                        stub.frees += 1
                        stub.value = BenchsetResult()
                        stub.piEquivalent = 99
                        stub.samples.removeAll()
                        stub.setReason(nil)
                    }
                    let runner = BenchmarkRunner(native: native)
                    observedRunner = runner
                    retained = try runner.run(benchmark)
                    #expect(stub.creates == 1 && stub.frees == 1)
                }
                #expect(observedRunner == nil)
                let result = try #require(retained)
                #expect(result.status == status)
                expectResultReference(result)
                let nativeCalls = (stub.abiChecks, stub.creates, stub.frees, stub.configs.count,
                                   stub.resultSizes.count)
                DispatchQueue.concurrentPerform(iterations: 8) { _ in
                    expectResultReference(result)
                }
                #expect(stub.abiChecks == nativeCalls.0 && stub.creates == nativeCalls.1)
                #expect(stub.frees == nativeCalls.2 && stub.configs.count == nativeCalls.3)
                #expect(stub.resultSizes.count == nativeCalls.4)
            }
        }
    }
}
