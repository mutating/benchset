import CBenchset
import Foundation

public enum Benchmark: UInt32 {
    case singleCore = 1
    case multiCore = 2
}

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
    public var success: Bool { status == BENCHSET_COMPLETED }
}

public enum BenchsetError: Error { case alreadyRunning, allocationFailed, resultUnavailable }

internal final class CallbackContext {
    let callback: (Progress) -> Void
    init(_ callback: @escaping (Progress) -> Void) { self.callback = callback }
}

// A private function table keeps ownership/error handling testable without
// changing the public Swift API or replacing the benchmark algorithm.
internal typealias NativeCallback = @convention(c) (UnsafePointer<BenchsetProgress>?, UnsafeMutableRawPointer?) -> Void
internal struct NativeAPI {
    var create: () -> OpaquePointer?
    var defaultConfig: (UnsafeMutablePointer<BenchsetConfig>) -> Int32
    var run: (OpaquePointer, UnsafePointer<BenchsetConfig>, NativeCallback, UnsafeMutableRawPointer) -> UInt32
    var cancel: (OpaquePointer) -> Void
    var result: (OpaquePointer, UnsafeMutablePointer<BenchsetResult>) -> Int32
    var sample: (OpaquePointer, UInt64, UnsafeMutablePointer<BenchsetSample>) -> Int32
    var reason: (OpaquePointer) -> UnsafePointer<CChar>?
    var version: () -> UnsafePointer<CChar>
    var free: (OpaquePointer) -> Void

    static let live = NativeAPI(
        create: { benchset_create() }, defaultConfig: { benchset_default_config($0) },
        run: { benchset_run($0, $1, $2, $3) }, cancel: { benchset_cancel($0) },
        result: { benchset_result($0, $1) }, sample: { benchset_sample($0, $1, $2) },
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
            native.run(current, &config, { pointer, rawContext in
                forwardProgress(pointer, rawContext)
            }, Unmanaged.passUnretained(context).toOpaque())
        }
        var result = BenchsetResult()
        guard native.result(current, &result) == 1 else { throw BenchsetError.resultUnavailable }
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
            unstable: result.unstable != 0
        )
    }
}
