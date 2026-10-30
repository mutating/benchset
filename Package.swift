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
                      url: "https://github.com/mutating/benchset/releases/download/v0.0.6/Benchset.xcframework.zip",
                      checksum: "989e592027e5e5e072a2d7a1ebc7971f12d7e41b6ced25e6d0ea37ce8043602d"),
        .target(name: "Benchset", dependencies: ["CBenchset"]),
        .testTarget(name: "BenchsetTests", dependencies: ["Benchset"], path: "tests/swift")
    ]
)
