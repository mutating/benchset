@testable import Benchset
import CBenchset
import Darwin
import Foundation
import Testing

private final class NativeStub {
    let pointer = OpaquePointer(bitPattern: 0x1234)!
    var allocationFails = false
    var configAvailable = true
    var resultAvailable = true
    var missingSample: UInt64?
    var creates = 0
    var frees = 0
    var cancels = 0
    var configs: [BenchsetConfig] = []
    var value = BenchsetResult()
    var samples: [BenchsetSample] = []
    var phases: [UInt32] = [1, 2, 3, 4, 5]
    var reasonPointer: UnsafeMutablePointer<CChar>?
    let versionPointer = strdup("test-version")!
    var action: ((NativeCallback, UnsafeMutableRawPointer) -> Void)?

    init() {
        value.abi_version = 1
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
        sample.iterations_per_second = 42.5
        samples = [sample, sample]
    }
    deinit { Darwin.free(reasonPointer); Darwin.free(versionPointer) }
    func setReason(_ string: String?) {
        Darwin.free(reasonPointer)
        reasonPointer = string.map { strdup($0)! }
    }
    var api: NativeAPI {
        NativeAPI(
            create: { self.creates += 1; return self.allocationFails ? nil : self.pointer },
            defaultConfig: { out in
                out.pointee = BenchsetConfig()
                out.pointee.abi_version = 1
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
                        progress.fraction = 0.375
                        progress.phase_elapsed_seconds = 1.25
                        progress.phase_duration_seconds = 2.5
                        progress.iterations = UInt64.max
                        withUnsafePointer(to: &progress) { callback($0, context) }
                    }
                }
                return self.value.status
            },
            cancel: { handle in #expect(handle == self.pointer); self.cancels += 1 },
            result: { _, out in
                self.value.sample_count = UInt64(self.samples.count)
                out.pointee = self.value
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
            #expect(result.benchmark == benchmark)
            #expect(result.workers == (benchmark == .singleCore ? 1 : UInt32(ProcessInfo.processInfo.activeProcessorCount)))
            #expect((result.score ?? 0) > 0)
            #expect(result.score! == Double(result.iterations) / result.elapsedSeconds)
            #expect(result.samples.reduce(0) { $0 + $1.iterations } == result.iterations)
            #expect(abs(result.samples.reduce(0) { $0 + $1.durationSeconds } - result.elapsedSeconds) < 1e-9)
            #expect(result.warmupSeconds == 0.03)
            #expect(result.measurementSeconds == 0.05)
            #expect(result.version == String(cString: benchset_version()))
            #expect(result.unit == "iterations/s")
            #expect(result.reason == nil)
            #expect(result.variationPercent == nil)
            #expect(!result.unstable)
            #expect(events.first?.phase == 1)
            #expect(events.contains { $0.phase == 2 })
            #expect(events.last?.phase == 3)
            #expect(events.last?.fraction == 1)
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
            let result = try runner.run(.multiCore, warmupSeconds: 0.04, measurementSeconds: 0.1) { progress in
                events.append(progress)
                if progress.phase == phase { runner.cancel(); runner.cancel() }
            }
            #expect(result.status == 1)
            #expect(!result.success)
            #expect(result.score == nil)
            #expect(result.reason == "cancelled")
            #expect(events.last?.phase == 4)
            runner.cancel()
            #expect(try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.02).success)
        }
    }

    @Test func samplesVariationAndCancellationAfterSamples() throws {
        let runner = BenchmarkRunner()
        let result = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 1.1)
        #expect(result.samples.count >= 2)
        #expect(result.variationPercent != nil)
        #expect(result.unstable == (result.variationPercent! > 10))
        #expect(result.samples.reduce(0) { $0 + $1.iterations } == result.iterations)
        let cancelled = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 1.5) {
            #expect($0.phase != 1)
            if $0.phase == 2 && $0.phaseElapsedSeconds > 1.1 { runner.cancel() }
        }
        #expect(cancelled.status == 1)
        #expect(cancelled.samples.count >= 2)
        #expect(cancelled.score == nil)
    }

    @Test func invalidDurationsAndRecovery() throws {
        let runner = BenchmarkRunner()
        let invalid: [(Double, Double)] = [(-1, 1), (.nan, 1), (.infinity, 1), (-.infinity, 1), (.greatestFiniteMagnitude, 1), (0, 0), (0, -1), (0, .nan), (0, .infinity), (0, -.infinity), (0, .greatestFiniteMagnitude), (0, 1e-12)]
        for (warmup, measurement) in invalid {
            let result = try runner.run(.singleCore, warmupSeconds: warmup, measurementSeconds: measurement)
            #expect(result.status == 2)
            #expect(!result.success)
            #expect(result.score == nil)
            #expect(result.reason?.isEmpty == false)
        }
        #expect(try runner.run(.singleCore, warmupSeconds: -0.0, measurementSeconds: 0.02).success)
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
            #expect(other.reason == "another benchmark is already running")
        }
        #expect(checked)
        #expect(result.success)
        #expect(try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.02).success)
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
        #expect(try output.value!.get().status == 1)
        #expect(try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.02).success)
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
        #expect(try output.value!.get().status == 1)
        #expect(stub.cancels == 1)
        #expect(stub.creates == 1 && stub.frees == 1)
        runner.cancel()
        #expect(stub.cancels == 1)
    }

    @Test func terminalCancellationKeepsCompletedResult() throws {
        let runner = BenchmarkRunner()
        let result = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.03) {
            if $0.phase == 3 { runner.cancel() }
        }
        #expect(result.success)
        #expect(result.score != nil)
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
                #expect(result.success == (status == 0))
                #expect(result.score == (status == 0 ? 123.25 : nil))
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
                for sample in result.samples {
                    #expect(sample.elapsedSeconds == 4.5 && sample.durationSeconds == 1.5)
                    #expect(sample.iterations == UInt64.max && sample.iterationsPerSecond == 42.5)
                }
                #expect(progress.map { $0.phase } == [1, 2, 3, 4, 5])
                for p in progress {
                    #expect(p.workers == UInt32.max && p.iterations == UInt64.max)
                    #expect(p.fraction == 0.375 && p.phaseElapsedSeconds == 1.25 && p.phaseDurationSeconds == 2.5)
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
        #expect(stub.configs.last?.warmup_seconds == 120)
        #expect(stub.configs.last?.measurement_seconds == 60)
        #expect(stub.configs.last?.benchmark == 1)
        #expect(stub.frees == stub.creates)
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
        #expect(try runner.run(.singleCore).success)
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
        #expect(try runner.run(.singleCore, onProgress: makeCallback()).success)
        #expect(observed == nil)
        #expect(stub.frees == 1)
    }
}
