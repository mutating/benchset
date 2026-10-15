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
                      url: "https://github.com/mutating/benchset/releases/download/v0.0.2/Benchset.xcframework.zip",
                      checksum: "29af959b4b1604abb0365f197cfceb73b7c54dc1713e24541b82b04c1956268f"),
        .target(name: "Benchset", dependencies: ["CBenchset"]),
        .testTarget(name: "BenchsetTests", dependencies: ["Benchset"], path: "tests/swift")
    ]
)
