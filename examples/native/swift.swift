import Benchset

let runner = BenchmarkRunner()
let result = try runner.run(.singleCore, warmupSeconds: 0.1, measurementSeconds: 0.2)
let machine: ReferenceMachine = result.referenceMachine
let reference: ReferenceRun = result.referenceRun
print("Reference: \(machine.name) (\(machine.shortName)), warmup \(reference.warmupSeconds) s")
if let score = result.score, let equivalent = result.piEquivalent {
    print("benchset \(result.version): \(score) \(result.unit), \(equivalent) x \(machine.shortName)")
} else {
    print(result.reason ?? "benchmark failed")
}
