// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LastCoreClient",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "LastCoreClient", targets: ["LastCoreClient"])],
    targets: [.executableTarget(name: "LastCoreClient")]
)
