// swift-tools-version:5.3
import PackageDescription

// Development checkouts and the offline ZIP use a locally built XCFramework.
// Release tags replace this binary target with an immutable URL and checksum.
let package = Package(
    name: "Benchset",
    platforms: [.macOS(.v10_12)],
    products: [.library(name: "Benchset", targets: ["Benchset"])],
    targets: [
        .binaryTarget(name: "CBenchset",
                      url: "https://github.com/mutating/benchset/releases/download/v0.0.9/Benchset.xcframework.zip",
                      checksum: "619d13ae2e632f6d2971846be6e82395746a74927819d8185fde3c8c0d68f1bc"),
        .target(name: "Benchset", dependencies: ["CBenchset"]),
        .testTarget(name: "BenchsetTests", dependencies: ["Benchset"], path: "tests/swift")
    ]
)
