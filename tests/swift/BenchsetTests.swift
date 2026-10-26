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
    var action: ((NativeCallback, UnsafeMutableRawPointer) -> Void)?

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
                if let action = self.action { action(callback, context) }
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
        stub.action = { _, _ in
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
        stub.action = { callback, context in
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

    @Test func callbackCapturesAndRunnerReleaseOnAllExits() throws {
        final class Probe { var calls = 0 }
        for stage in ["abi", "allocation", "config", "result", "sample", "completed"] {
            let stub = NativeStub()
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
