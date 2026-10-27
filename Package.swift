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
                      url: "https://github.com/mutating/benchset/releases/download/v0.0.4/Benchset.xcframework.zip",
                      checksum: "0e6643b1daa58773a93aec6b4228b2d81580f058dc5348609457363a9b5ec4ae"),
        .target(name: "Benchset", dependencies: ["CBenchset"]),
        .testTarget(name: "BenchsetTests", dependencies: ["Benchset"], path: "tests/swift")
    ]
)
