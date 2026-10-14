import Benchset

let runner = BenchmarkRunner()
let result = try runner.run(.singleCore, warmupSeconds: 0.1, measurementSeconds: 0.2)
print("benchset \(result.version): \(result.score ?? 0) \(result.unit)")
