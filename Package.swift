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
                      url: "https://github.com/mutating/benchset/releases/download/v0.0.3/Benchset.xcframework.zip",
                      checksum: "f50ee204c5a636497bdae1f0a2bb8a7987042f58bd84c5153cae6201e1e35aea"),
        .target(name: "Benchset", dependencies: ["CBenchset"]),
        .testTarget(name: "BenchsetTests", dependencies: ["Benchset"], path: "tests/swift")
    ]
)
