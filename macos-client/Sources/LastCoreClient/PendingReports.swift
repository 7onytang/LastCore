import Foundation

struct PendingReport: Codable, Equatable {
    let origin: String
    let nodeID: String
    let taskID: String
    let assignmentID: String
    let modelID: String
    let layerStart: Int
    let layerEnd: Int
    let status: String
    let result: [Double]?
    let error: String?
}

// MainActor serializes mutations. Atomic replacement preserves the previous journal on write failure.
@MainActor
final class PendingReports {
    let url: URL
    init(url: URL) { self.url = url }

    func load() throws -> [PendingReport] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let rows = try JSONDecoder().decode([PendingReport].self, from: Data(contentsOf: url))
        guard rows.allSatisfy({ !$0.origin.isEmpty && !$0.nodeID.isEmpty && !$0.taskID.isEmpty && !$0.assignmentID.isEmpty &&
            (($0.status == "completed" && $0.modelID == "lastcore/linear-demo-v1" &&
              $0.layerStart >= 0 && $0.layerEnd <= 4096 && $0.layerStart < $0.layerEnd &&
              $0.result?.count == $0.layerEnd - $0.layerStart && $0.result != nil && !(($0.result ?? []).isEmpty) &&
              ($0.result ?? []).count <= 4096 && ($0.result ?? []).allSatisfy { $0.isFinite && abs($0) <= 1 }) ||
             ($0.status == "failed" && $0.result == nil)) }) else {
            throw CocoaError(.fileReadCorruptFile)
        }
        return rows
    }

    func put(_ record: PendingReport) throws {
        var rows = try load()
        if let previous = rows.first(where: { $0.origin == record.origin && $0.nodeID == record.nodeID && $0.assignmentID == record.assignmentID }) {
            guard previous == record else { throw CocoaError(.fileWriteFileExists) }
            return
        }
        rows.append(record)
        try save(rows)
    }

    func remove(_ record: PendingReport) throws {
        try save(load().filter { $0 != record })
    }

    private func save(_ records: [PendingReport]) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        try JSONEncoder().encode(records).write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
    }
}
