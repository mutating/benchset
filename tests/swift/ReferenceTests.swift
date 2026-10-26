@testable import Benchset
import CBenchset
import Darwin
import Foundation
import Testing

private final class ReferenceStub {
    var abi: UInt32 = BENCHSET_ABI_VERSION
    var failAt: String?
    var nilMachineField: String?
    var nilResultAt: UInt64?
    var calls: [String] = []
    var beforeGetter: ((String) -> Void)?
    private var storage: [UnsafeMutablePointer<CChar>] = []
    private(set) var machine = BenchsetReferenceMachine()
    private(set) var run = BenchsetReferenceRun()
    private(set) var results: [BenchsetReferenceBenchmarkResult] = []

    init() {
        machine.name = string("Raspberry Pi 5 Model B Rev 1.0")
        machine.short_name = string("pi")
        machine.os_name = string("Ubuntu Server")
        machine.os_version = string("26.04 LTS")
        machine.cpu_architecture = string("aarch64")
        machine.memory_bytes = 8_589_934_592
        machine.cpu_cores = 4
        run.started_at_unix_seconds = 1792767590.517391
        run.start_temperature_c = 41.35
        run.end_temperature_c = 66.65
        run.warmup_seconds = 1248
        run.result_count = 2
        results = [
            BenchsetReferenceBenchmarkResult(benchmark: string("single_core"), score: 47231.227029007096),
            BenchsetReferenceBenchmarkResult(benchmark: string("multi_core"), score: 181953.17419139884)
        ]
    }
    deinit { for pointer in storage { pointer.deallocate() } }

    private func string(_ value: String) -> UnsafePointer<CChar> {
        let pointer = UnsafeMutablePointer<CChar>.allocate(capacity: 128)
        _ = value.withCString { strcpy(pointer, $0) }
        storage.append(pointer)
        return UnsafePointer(pointer)
    }
    func overwrite(_ pointer: UnsafePointer<CChar>, with value: String) {
        _ = value.withCString { strcpy(UnsafeMutablePointer(mutating: pointer), $0) }
    }
    func overwriteAll() {
        for pointer in storage { overwrite(UnsafePointer(pointer), with: "replaced") }
    }

    var api: ReferenceAPI {
        ReferenceAPI(
            abiVersion: { self.calls.append("abi"); return self.abi },
            machine: { out, size in
                self.calls.append("machine")
                self.beforeGetter?("machine")
                #expect(size == UInt32(MemoryLayout<BenchsetReferenceMachine>.size))
                var value = self.machine
                switch self.nilMachineField {
                case "name": value.name = nil
                case "short_name": value.short_name = nil
                case "os_name": value.os_name = nil
                case "os_version": value.os_version = nil
                case "cpu_architecture": value.cpu_architecture = nil
                default: break
                }
                out.pointee = value
                return self.failAt == "machine" ? 0 : 1
            },
            run: { out, size in
                self.calls.append("run")
                self.beforeGetter?("run")
                #expect(size == UInt32(MemoryLayout<BenchsetReferenceRun>.size))
                out.pointee = self.run
                return self.failAt == "run" ? 0 : 1
            },
            result: { index, out, size in
                let label = "result\(index)"
                self.calls.append(label)
                self.beforeGetter?(label)
                #expect(size == UInt32(MemoryLayout<BenchsetReferenceBenchmarkResult>.size))
                var value = self.results[Int(index)]
                if self.nilResultAt == index { value.benchmark = nil }
                out.pointee = value
                return self.failAt == label ? 0 : 1
            }
        )
    }
}

private func expectReference(_ machine: ReferenceMachine, _ run: ReferenceRun) {
    #expect(machine.name == "Raspberry Pi 5 Model B Rev 1.0")
    #expect(machine.shortName == "pi")
    #expect(machine.osName == "Ubuntu Server" && machine.osVersion == "26.04 LTS")
    #expect(machine.cpuArchitecture == "aarch64")
    #expect(machine.memoryBytes == 8_589_934_592 && machine.cpuCores == 4)
    #expect(run.startedAtUnixSeconds.bitPattern == (1792767590.517391 as Double).bitPattern)
    #expect(run.startedAt.timeIntervalSince1970 == run.startedAtUnixSeconds)
    #expect(run.startTemperatureC.bitPattern == (41.35 as Double).bitPattern)
    #expect(run.endTemperatureC.bitPattern == (66.65 as Double).bitPattern)
    #expect(run.warmupSeconds == 1248)
    #expect(run.results.map { $0.benchmark } == ["single_core", "multi_core"])
    #expect(Set(run.results.map { $0.benchmark }).count == 2)
    guard run.results.count == 2 else { return }
    #expect(run.results[0].score.bitPattern == (47231.227029007096 as Double).bitPattern)
    #expect(run.results[1].score.bitPattern == (181953.17419139884 as Double).bitPattern)
    #expect(run.results[0].score == Double(42_508_112) / 900.000162475)
    #expect(run.results[1].score == Double(163_757_952) / 900.000523364)
}

func expectResultReference(_ result: BenchmarkResult) {
    // These assignments also enforce required Swift value types, rather than
    // optional metadata or native pointers in the public result API.
    let machine: ReferenceMachine = result.referenceMachine
    let run: ReferenceRun = result.referenceRun
    expectReference(machine, run)
    #expect([machine.name, machine.shortName, machine.osName, machine.osVersion, machine.cpuArchitecture]
        == [piReferenceMachine.name, piReferenceMachine.shortName, piReferenceMachine.osName,
            piReferenceMachine.osVersion, piReferenceMachine.cpuArchitecture])
    #expect(machine.memoryBytes == piReferenceMachine.memoryBytes)
    #expect(machine.cpuCores == piReferenceMachine.cpuCores)
    #expect(run.startedAtUnixSeconds.bitPattern == piReferenceRun.startedAtUnixSeconds.bitPattern)
    #expect(run.startedAt == piReferenceRun.startedAt)
    #expect(run.startTemperatureC.bitPattern == piReferenceRun.startTemperatureC.bitPattern)
    #expect(run.endTemperatureC.bitPattern == piReferenceRun.endTemperatureC.bitPattern)
    #expect(run.warmupSeconds.bitPattern == piReferenceRun.warmupSeconds.bitPattern)
    #expect(run.results.map { $0.benchmark } == piReferenceRun.results.map { $0.benchmark })
    #expect(run.results.map { $0.score.bitPattern } == piReferenceRun.results.map { $0.score.bitPattern })
}

extension BenchsetTests {
    @Test func resultReferencesPreserveMemberwiseConstructionAndValueSemantics() {
        for benchmark in [Benchmark.singleCore, .multiCore] {
            for status: UInt32 in [0, 1, 2] {
                // Keep the complete pre-reference memberwise initializer: the
                // new computed properties must not introduce required arguments.
                let result = BenchmarkResult(
                    benchmark: benchmark, version: "constructed", status: status,
                    reason: status == 0 ? nil : "constructed failure", score: status == 0 ? 0 : nil,
                    workers: 1, warmupSeconds: 0, measurementSeconds: 1, elapsedSeconds: 1,
                    iterations: 0, checksum: 0, samples: [], variationPercent: nil,
                    unstable: false, piEquivalent: status == 0 ? 0 : nil
                )
                expectResultReference(result)
                let copied = result
                let machine = result.referenceMachine
                let run = result.referenceRun
                var localResults = run.results
                localResults.removeAll()
                #expect(localResults.isEmpty)
                #expect(run.results.count == 2)
                expectReference(machine, run)
                expectResultReference(result)
                expectResultReference(copied)
            }
        }
    }

    @Test func globalReferenceMetadataMatchesRecordedSeries() throws {
        expectReference(piReferenceMachine, piReferenceRun)
        let snapshot = try loadReference(using: .live)
        expectReference(snapshot.machine, snapshot.run)
        var copiedResults = snapshot.run.results
        copiedResults.removeAll()
        #expect(copiedResults.isEmpty)
        #expect(snapshot.run.results.count == 2 && piReferenceRun.results.count == 2)
        // The timestamp denotes the same UTC instant in every display zone.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!
        let utc = calendar.dateComponents([.year, .month, .day, .hour, .minute, .second], from: snapshot.run.startedAt)
        #expect(utc.year == 2026 && utc.month == 10 && utc.day == 23)
        #expect(utc.hour == 14 && utc.minute == 59 && utc.second == 50)
        calendar.timeZone = TimeZone(secondsFromGMT: 3 * 3600)!
        let local = calendar.dateComponents([.day, .hour], from: snapshot.run.startedAt)
        #expect(local.day == 23 && local.hour == 17)
        #expect(snapshot.run.startedAtUnixSeconds == 1792767590.517391)
    }

    @Test func metadataLoaderChecksABIBeforeAnyGetterAndCanRetry() throws {
        let stub = ReferenceStub()
        for abi: UInt32 in [0, 2, 3, UInt32.max] {
            stub.calls = []
            stub.abi = abi
            #expect(throws: BenchsetError.resultUnavailable) { try loadReference(using: stub.api) }
            #expect(stub.calls == ["abi"])
        }
        stub.abi = BENCHSET_ABI_VERSION
        stub.calls = []
        let value = try loadReference(using: stub.api)
        expectReference(value.machine, value.run)
        #expect(stub.calls == ["abi", "machine", "run", "result0", "result1"])
    }

    @Test func metadataFailuresAreAtomicAndAllowCompleteRetry() throws {
        let expectedCalls = [
            "machine": ["abi", "machine"],
            "run": ["abi", "machine", "run"],
            "result0": ["abi", "machine", "run", "result0"],
            "result1": ["abi", "machine", "run", "result0", "result1"]
        ]
        for failure in ["machine", "run", "result0", "result1"] {
            let stub = ReferenceStub()
            stub.failAt = failure
            var published: (machine: ReferenceMachine, run: ReferenceRun)?
            do { published = try loadReference(using: stub.api) }
            catch { #expect(error as? BenchsetError == .resultUnavailable) }
            #expect(published == nil)
            #expect(stub.calls == expectedCalls[failure])
            stub.failAt = nil
            let retry = try loadReference(using: stub.api)
            expectReference(retry.machine, retry.run)
        }
        for field in ["name", "short_name", "os_name", "os_version", "cpu_architecture"] {
            let stub = ReferenceStub()
            stub.nilMachineField = field
            #expect(throws: BenchsetError.resultUnavailable) { try loadReference(using: stub.api) }
            #expect(stub.calls == ["abi", "machine"])
        }
        for index: UInt64 in [0, 1] {
            let stub = ReferenceStub()
            stub.nilResultAt = index
            #expect(throws: BenchsetError.resultUnavailable) { try loadReference(using: stub.api) }
            stub.nilResultAt = nil
            expectReference(try loadReference(using: stub.api).machine, try loadReference(using: stub.api).run)
        }
    }

    @Test func metadataCopiesStringsBeforeNextGetterAndOwnsEverySnapshot() throws {
        var snapshot: (machine: ReferenceMachine, run: ReferenceRun)!
        do {
            let stub = ReferenceStub()
            stub.beforeGetter = { [unowned stub] getter in
                if getter == "run" { stub.overwrite(stub.machine.name!, with: "overwritten by run") }
                if getter == "result1" { stub.overwrite(stub.results[0].benchmark!, with: "overwritten by next result") }
            }
            snapshot = try loadReference(using: stub.api)
            stub.overwriteAll()
            expectReference(snapshot.machine, snapshot.run)
        }
        // The temporary buffers and function-table closures have all gone away.
        expectReference(snapshot.machine, snapshot.run)
        var first = snapshot.run.results
        first.remove(at: 0)
        #expect(first.count == 1 && snapshot.run.results.count == 2)
        expectReference(piReferenceMachine, piReferenceRun)
    }

    @Test func metadataCopiesUTF8AsSwiftStrings() throws {
        let stub = ReferenceStub()
        stub.overwrite(stub.machine.name!, with: "Эталон π 🍓")
        stub.overwrite(stub.results[0].benchmark!, with: "бенчмарк_π")
        let value = try loadReference(using: stub.api)
        stub.overwriteAll()
        #expect(value.machine.name == "Эталон π 🍓")
        #expect(value.run.results[0].benchmark == "бенчмарк_π")
    }

    @Test func referenceReadsAreSafeDuringCallbacksAndAcrossThreads() throws {
        let runner = BenchmarkRunner()
        let failed = try runner.run(.multiCore, warmupSeconds: 0, measurementSeconds: 0)
        #expect(failed.status == 2 && failed.piEquivalent == nil)
        expectResultReference(failed)
        var callbacks = 0
        let measured = try runner.run(.singleCore, warmupSeconds: 0, measurementSeconds: 0.03) { _ in
            let value = try! loadReference(using: .live)
            expectReference(value.machine, value.run)
            expectReference(piReferenceMachine, piReferenceRun)
            expectResultReference(failed)
            if callbacks == 0 {
                DispatchQueue.concurrentPerform(iterations: 8) { _ in
                    let concurrent = try! loadReference(using: .live)
                    expectReference(concurrent.machine, concurrent.run)
                    expectResultReference(failed)
                }
            }
            callbacks += 1
        }
        #expect(callbacks > 0)
        // A descheduled worker can miss this short window; metadata remains available.
        if measured.success {
            #expect(measured.piEquivalent != nil)
        } else {
            #expect(measured.status == 2)
            #expect(measured.reason == "measurement window was too short to complete any work")
            #expect(measured.score == nil && measured.piEquivalent == nil)
            #expect(measured.iterations == 0)
        }
        expectResultReference(measured)
        let cancelled = try runner.run(.singleCore, warmupSeconds: 10, measurementSeconds: 0.02) { _ in
            expectResultReference(measured)
            expectResultReference(failed)
            runner.cancel()
        }
        #expect(cancelled.status == 1 && cancelled.piEquivalent == nil)
        expectResultReference(cancelled)
        let retained = [measured, failed, cancelled]
        DispatchQueue.concurrentPerform(iterations: 8) { _ in
            let value = try! loadReference(using: .live)
            expectReference(value.machine, value.run)
            for result in retained {
                var localResults = result.referenceRun.results
                localResults.removeAll()
                #expect(localResults.isEmpty)
                expectResultReference(result)
            }
        }
        expectReference(try loadReference(using: .live).machine, try loadReference(using: .live).run)
        try expectRunnerReusable(runner)
    }
}
