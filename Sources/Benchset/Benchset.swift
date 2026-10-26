import CBenchset
import Foundation

public enum Benchmark: UInt32 {
    case singleCore = 1
    case multiCore = 2
}

public struct ReferenceMachine {
    public let name: String
    public let shortName: String
    public let osName: String
    public let osVersion: String
    public let cpuArchitecture: String
    /// Nominal installed capacity, in bytes.
    public let memoryBytes: UInt64
    public let cpuCores: UInt32
}

/// One benchmark's sustained iterations/s, pooled across three reference repeats.
public struct ReferenceBenchmarkResult {
    public let benchmark: String
    public let score: Double
}

/// The final reference series, from its first warmup to its last measurement.
public struct ReferenceRun {
    /// UTC Unix seconds at the start of the series' first warmup.
    public let startedAtUnixSeconds: Double
    public var startedAt: Date { Date(timeIntervalSince1970: startedAtUnixSeconds) }
    /// CPU temperature in Celsius at the start of the whole series.
    public let startTemperatureC: Double
    /// CPU temperature in Celsius after the series' final measurement.
    public let endTemperatureC: Double
    /// Seconds before each reference measurement; does not change runtime defaults.
    public let warmupSeconds: Double
    /// One pooled score per benchmark, rather than one entry per repeat.
    public let results: [ReferenceBenchmarkResult]
}

private let piReference = try! loadReference(using: .live)
public let piReferenceMachine: ReferenceMachine = piReference.machine
public let piReferenceRun: ReferenceRun = piReference.run

public struct Progress {
    public let phase: UInt32
    public let workers: UInt32
    public let fraction: Double
    public let phaseElapsedSeconds: Double
    public let phaseDurationSeconds: Double
    public let iterations: UInt64
}

public struct Sample {
    public let elapsedSeconds: Double
    public let durationSeconds: Double
    public let iterations: UInt64
    public let iterationsPerSecond: Double
}

public struct BenchmarkResult {
    public let benchmark: Benchmark
    public let version: String
    public let status: UInt32
    public let reason: String?
    public let score: Double?
    public var unit: String { "iterations/s" }
    public let workers: UInt32
    public let warmupSeconds: Double
    public let measurementSeconds: Double
    public let elapsedSeconds: Double
    public let iterations: UInt64
    public let checksum: UInt64
    public let samples: [Sample]
    public let variationPercent: Double?
    public let unstable: Bool
    /// Score divided by the same benchmark's Pi reference score. Present for
    /// completed results, including unstable ones; nil after failure/cancellation.
    public let piEquivalent: Double?
    /// The shared calibration, available for every returned result status.
    public var referenceMachine: ReferenceMachine { piReferenceMachine }
    public var referenceRun: ReferenceRun { piReferenceRun }
    public var success: Bool { status == BENCHSET_COMPLETED }
}

public enum BenchsetError: Error { case alreadyRunning, allocationFailed, resultUnavailable }

internal struct ReferenceAPI {
    var abiVersion: () -> UInt32
    var machine: (UnsafeMutablePointer<BenchsetReferenceMachine>, UInt32) -> Int32
    var run: (UnsafeMutablePointer<BenchsetReferenceRun>, UInt32) -> Int32
    var result: (UInt64, UnsafeMutablePointer<BenchsetReferenceBenchmarkResult>, UInt32) -> Int32

    static let live = ReferenceAPI(
        abiVersion: { benchset_abi_version() },
        machine: { benchset_reference_machine($0, $1) },
        run: { benchset_reference_run($0, $1) },
        result: { benchset_reference_result($0, $1, $2) }
    )
}

internal func loadReference(using native: ReferenceAPI) throws -> (machine: ReferenceMachine, run: ReferenceRun) {
    guard native.abiVersion() == BENCHSET_ABI_VERSION else { throw BenchsetError.resultUnavailable }
    var machine = BenchsetReferenceMachine()
    guard native.machine(&machine, UInt32(MemoryLayout<BenchsetReferenceMachine>.size)) == 1,
          let name = machine.name, let shortName = machine.short_name,
          let osName = machine.os_name, let osVersion = machine.os_version,
          let architecture = machine.cpu_architecture else { throw BenchsetError.resultUnavailable }
    // Copy every string before another getter can replace its backing buffer.
    let copiedMachine = ReferenceMachine(
        name: String(cString: name), shortName: String(cString: shortName),
        osName: String(cString: osName), osVersion: String(cString: osVersion),
        cpuArchitecture: String(cString: architecture), memoryBytes: machine.memory_bytes,
        cpuCores: machine.cpu_cores
    )
    var run = BenchsetReferenceRun()
    guard native.run(&run, UInt32(MemoryLayout<BenchsetReferenceRun>.size)) == 1 else {
        throw BenchsetError.resultUnavailable
    }
    var results: [ReferenceBenchmarkResult] = []
    for index in 0..<run.result_count {
        var result = BenchsetReferenceBenchmarkResult()
        guard native.result(index, &result, UInt32(MemoryLayout<BenchsetReferenceBenchmarkResult>.size)) == 1,
              let benchmark = result.benchmark else { throw BenchsetError.resultUnavailable }
        results.append(ReferenceBenchmarkResult(benchmark: String(cString: benchmark), score: result.score))
    }
    return (copiedMachine, ReferenceRun(
        startedAtUnixSeconds: run.started_at_unix_seconds,
        startTemperatureC: run.start_temperature_c, endTemperatureC: run.end_temperature_c,
        warmupSeconds: run.warmup_seconds, results: results
    ))
}

internal final class CallbackContext {
    let callback: (Progress) -> Void
    init(_ callback: @escaping (Progress) -> Void) { self.callback = callback }
}

// A private function table keeps ownership/error handling testable without
// changing the public Swift API or replacing the benchmark algorithm.
internal typealias NativeCallback = @convention(c) (UnsafePointer<BenchsetProgress>?, UnsafeMutableRawPointer?) -> Void
internal struct NativeAPI {
    var abiVersion: () -> UInt32
    var create: () -> OpaquePointer?
    var defaultConfig: (UnsafeMutablePointer<BenchsetConfig>) -> Int32
    var run: (OpaquePointer, UnsafePointer<BenchsetConfig>, NativeCallback, UnsafeMutableRawPointer) -> UInt32
    var cancel: (OpaquePointer) -> Void
    var result: (OpaquePointer, UnsafeMutablePointer<BenchsetResultV2>, UInt32) -> Int32
    var sample: (OpaquePointer, UInt64, UnsafeMutablePointer<BenchsetSample>) -> Int32
    var reason: (OpaquePointer) -> UnsafePointer<CChar>?
    var version: () -> UnsafePointer<CChar>
    var free: (OpaquePointer) -> Void

    static let live = NativeAPI(
        abiVersion: { benchset_abi_version() },
        create: { benchset_create() }, defaultConfig: { benchset_default_config($0) },
        run: { benchset_run($0, $1, $2, $3) }, cancel: { benchset_cancel($0) },
        result: { benchset_result_v2($0, $1, $2) }, sample: { benchset_sample($0, $1, $2) },
        reason: { benchset_reason($0) }, version: { benchset_version() },
        free: { benchset_free($0) }
    )
}

internal func forwardProgress(_ pointer: UnsafePointer<BenchsetProgress>?, _ rawContext: UnsafeMutableRawPointer?) {
    guard let value = pointer?.pointee, let rawContext = rawContext else { return }
    let context = Unmanaged<CallbackContext>.fromOpaque(rawContext).takeUnretainedValue()
    context.callback(Progress(
        phase: value.phase, workers: value.workers, fraction: value.fraction,
        phaseElapsedSeconds: value.phase_elapsed_seconds,
        phaseDurationSeconds: value.phase_duration_seconds, iterations: value.iterations
    ))
}

/// Run from a background queue. Callbacks execute on that queue, not the UI queue.
/// cancel() is safe from another thread, including inside a progress callback.
public final class BenchmarkRunner {
    private let lock = NSLock()
    private let native: NativeAPI
    private var handle: OpaquePointer?

    public init() { self.native = .live }
    internal init(native: NativeAPI) { self.native = native }

    public func cancel() {
        lock.lock()
        defer { lock.unlock() }
        if let handle = handle { native.cancel(handle) }
    }

    public func run(
        _ benchmark: Benchmark,
        warmupSeconds: Double = 120,
        measurementSeconds: Double = 60,
        onProgress: @escaping (Progress) -> Void = { _ in }
    ) throws -> BenchmarkResult {
        lock.lock()
        guard handle == nil else { lock.unlock(); throw BenchsetError.alreadyRunning }
        guard native.abiVersion() == BENCHSET_ABI_VERSION else { lock.unlock(); throw BenchsetError.resultUnavailable }
        guard let current = native.create() else { lock.unlock(); throw BenchsetError.allocationFailed }
        handle = current
        lock.unlock()
        defer {
            lock.lock()
            handle = nil
            native.free(current)
            lock.unlock()
        }
        var config = BenchsetConfig()
        guard native.defaultConfig(&config) == 1 else { throw BenchsetError.resultUnavailable }
        config.benchmark = benchmark.rawValue
        config.warmup_seconds = warmupSeconds
        config.measurement_seconds = measurementSeconds
        let context = CallbackContext(onProgress)
        _ = withExtendedLifetime(context) {
            native.run(current, &config, forwardProgress, Unmanaged.passUnretained(context).toOpaque())
        }
        var extended = BenchsetResultV2()
        guard native.result(current, &extended, UInt32(MemoryLayout<BenchsetResultV2>.size)) == 1 else {
            throw BenchsetError.resultUnavailable
        }
        let result = extended.base
        var samples: [Sample] = []
        for index in 0..<result.sample_count {
            var sample = BenchsetSample()
            guard native.sample(current, index, &sample) == 1 else { throw BenchsetError.resultUnavailable }
            samples.append(Sample(elapsedSeconds: sample.elapsed_seconds,
                                  durationSeconds: sample.duration_seconds, iterations: sample.iterations,
                                  iterationsPerSecond: sample.iterations_per_second))
        }
        let reason = native.reason(current).map { String(cString: $0) }
        return BenchmarkResult(
            benchmark: benchmark, version: String(cString: native.version()), status: result.status,
            reason: reason?.isEmpty == false ? reason : nil,
            score: result.status == BENCHSET_COMPLETED ? result.score : nil, workers: result.workers,
            warmupSeconds: result.warmup_seconds, measurementSeconds: result.measurement_seconds,
            elapsedSeconds: result.elapsed_seconds, iterations: result.iterations, checksum: result.checksum,
            samples: samples, variationPercent: result.has_variation != 0 ? result.variation_percent : nil,
            unstable: result.unstable != 0,
            piEquivalent: result.status == BENCHSET_COMPLETED && extended.has_pi_equivalent != 0
                ? extended.pi_equivalent : nil
        )
    }
}
