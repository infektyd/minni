// swift-tools-version: 6.0
// Core package for Mac-side `swift test` if you do not want the Xcode test host.
import PackageDescription

let package = Package(
    name: "UsageDockCore",
    platforms: [.macOS(.v15)],
    products: [
        .library(name: "UsageDockCore", targets: ["UsageDockCore"]),
    ],
    targets: [
        .target(
            name: "UsageDockCore",
            path: "Sources/UsageDockCore"
        ),
        .testTarget(
            name: "UsageDockCoreTests",
            dependencies: ["UsageDockCore"],
            path: "Tests/UsageDockCoreTests",
            exclude: ["ClaudeCredentialsTests.swift"]
        ),
    ]
)
