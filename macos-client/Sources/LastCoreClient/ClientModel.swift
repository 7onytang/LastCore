import Foundation
import Security
import SwiftUI

@MainActor
final class ClientModel: ObservableObject {
    @Published var controllerURL = UserDefaults.standard.string(forKey: "controllerURL") ?? ""
    @Published var nodeName = UserDefaults.standard.string(forKey: "nodeName") ?? Host.current().localizedName ?? "Mac"
    @Published var endpoint = UserDefaults.standard.string(forKey: "endpoint") ?? "http://127.0.0.1:9000"
    @Published var registrationToken = ""
    @Published var acceptingTasks = true {
        didSet { if oldValue != acceptingTasks { acceptingRevision &+= 1 } }
    }
    enum ConnectionState { case stopped, connecting, connected, retrying, unreachable, stopping }
    @Published private(set) var connectionState: ConnectionState = .stopped
    @Published private(set) var confirmedAcceptingTasks: Bool?
    @Published private(set) var sessionOrigin: String?
    @Published private(set) var lastHeartbeatAt: Date?
    @Published private(set) var isRunning = false
    @Published private(set) var nodeID: String?
    @Published private(set) var currentTask = "空闲"
    @Published private(set) var lastError: String?
    @Published private(set) var messages: [String] = []
    @Published private(set) var lastResult: [Double]?
    @Published private(set) var lastCompletedTaskID: String?

    private var loopTask: Task<Void, Never>?
    private var nodeToken: String?
    private var startedAssignments = Set<String>()
    private let memoryMB = 4096
    private let computeWeight = 1.0
    private var sessionGeneration = UUID()
    private var acceptingRevision: UInt64 = 0
    private var confirmedRevision: UInt64?
    private var activeControllerURL = ""
    private let session: URLSession
    let reports: PendingReports
    private let persistCredentials: Bool

    init(persistCredentials: Bool = true, session: URLSession? = nil, journalURL: URL? = nil) {
        self.persistCredentials = persistCredentials
        self.session = session ?? URLSession(configuration: .ephemeral, delegate: NoRedirectDelegate(), delegateQueue: nil)
        let defaultJournal = persistCredentials
            ? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("LastCoreClient/pending-reports.json")
            : FileManager.default.temporaryDirectory.appendingPathComponent("LastCoreClient-" + UUID().uuidString)
                .appendingPathComponent("pending-reports.json")
        self.reports = PendingReports(url: journalURL ?? defaultJournal)
    }

    var isConnectionConfirmed: Bool { connectionState == .connected }
    var isStopping: Bool { connectionState == .stopping }
    var acceptanceText: String {
        guard isRunning, !isStopping else { return "接单状态未确认" }
        guard connectionState == .connected, confirmedRevision == acceptingRevision,
              confirmedAcceptingTasks == acceptingTasks else {
            return acceptingTasks ? "接收新任务待心跳确认" : "暂停接单待心跳确认"
        }
        return acceptingTasks ? "接收新任务 · 心跳已确认" : "暂停新任务 · 心跳已确认"
    }
    var configuredMemoryText: String { "\(memoryMB) MB · 配置声明，非系统实测" }
    var configuredComputeText: String { "\(computeWeight) · 配置权重，非基准测试" }
    var heartbeatText: String {
        guard let lastHeartbeatAt else { return "尚未确认" }
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        return formatter.string(from: lastHeartbeatAt) + "（北京时间）"
    }
    var statusText: String {
        switch connectionState {
        case .stopped: return "已停止"
        case .connecting: return "连接中 · 等待认证心跳"
        case .connected: return "已连接 · " + acceptanceText
        case .retrying: return "连接未确认 · 正在重试"
        case .unreachable: return "未连接 · 请检查错误后重试"
        case .stopping: return "停止中 · 等待会话清理"
        }
    }
    private var canClaimNewWork: Bool {
        isRunning && !isStopping && acceptingTasks && connectionState == .connected
            && confirmedRevision == acceptingRevision && confirmedAcceptingTasks == true
    }

    func start() {
        guard !isRunning, loopTask == nil else { return }
        guard let base = URL(string: controllerURL.trimmingCharacters(in: .whitespacesAndNewlines)),
              base.host != nil, base.user == nil, base.password == nil,
              base.query == nil, base.fragment == nil,
              base.path.isEmpty || base.path == "/",
              base.scheme == "https" || (base.scheme == "http" && ["localhost", "127.0.0.1", "::1"].contains(base.host ?? "")) else {
            lastError = "公网 Controller 必须使用 HTTPS，地址不能带用户名、路径或查询参数"
            return
        }
        guard !nodeName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              URL(string: endpoint)?.host != nil else {
            lastError = "请填写节点名称和有效的本机地址"
            return
        }
        if registrationToken.isEmpty && (!persistCredentials || KeychainCredentials.load(for: controllerURL) == nil) {
            lastError = "首次注册需要提供注册凭据"
            return
        }

        if persistCredentials { saveSettings() }
        activeControllerURL = base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        nodeID = nil
        nodeToken = nil
        lastResult = nil
        lastCompletedTaskID = nil
        startedAssignments.removeAll()
        sessionGeneration = UUID()
        let generation = sessionGeneration
        sessionOrigin = activeControllerURL
        confirmedAcceptingTasks = nil
        confirmedRevision = nil
        lastHeartbeatAt = nil
        connectionState = .connecting
        isRunning = true
        lastError = nil
        loopTask = Task { await runLoop(generation: generation) }
    }

    func stop() {
        guard loopTask != nil, !isStopping else { return }
        connectionState = .stopping
        confirmedAcceptingTasks = nil
        confirmedRevision = nil
        loopTask?.cancel()
        addMessage("正在停止，等待原会话清理")
    }

    private func runLoop(generation: UUID) async {
        defer {
            if generation == sessionGeneration {
                isRunning = false
                loopTask = nil
                currentTask = "空闲"
                confirmedAcceptingTasks = nil
                confirmedRevision = nil
                if connectionState != .unreachable { connectionState = .stopped }
                addMessage(connectionState == .stopped ? "节点已停止" : "会话已结束，请检查错误")
            }
        }
        var retryDelay: UInt64 = 1
        while !Task.isCancelled {
            do {
                try await loadOrRegister()
                try await heartbeat(generation: generation)
                if let nodeID, let nodeToken {
                    try await replayPendingReports(origin: activeControllerURL, nodeID: nodeID, token: nodeToken)
                }
                if canClaimNewWork { try await pollAssignments() }
                lastError = nil
                retryDelay = 1
                try await Task.sleep(for: .seconds(2))
            } catch is CancellationError {
                break
            } catch {
                if Task.isCancelled { break }
                confirmedAcceptingTasks = nil
                confirmedRevision = nil
                lastError = error.localizedDescription
                addMessage("会话错误：\(error.localizedDescription)")
                if error is CocoaError || (nodeID == nil && nodeToken == nil) {
                    connectionState = .unreachable
                    return
                }
                connectionState = .retrying
                do { try await Task.sleep(for: .seconds(retryDelay)) } catch { break }
                retryDelay = min(retryDelay * 2, 30)
            }
        }
    }

    private func loadOrRegister() async throws {
        if nodeID != nil && nodeToken != nil { return }
        if persistCredentials, let saved = KeychainCredentials.load(for: activeControllerURL) {
            nodeID = saved.nodeID
            nodeToken = saved.nodeToken
            return
        }
        guard !registrationToken.isEmpty else {
            throw ClientError.message("首次注册需要提供注册凭据")
        }
        let response = try await request("POST", "/v1/nodes/register", token: registrationToken, body: [
            "name": nodeName,
            "platform": "macOS-\(ProcessInfo.processInfo.processArchitecture)",
            "endpoint": endpoint,
            "available_memory_mb": memoryMB,
            "compute_weight": computeWeight
        ])
        guard let id = response["node_id"] as? String,
              let token = response["node_token"] as? String else {
            throw ClientError.message("Controller 注册响应缺少节点凭据")
        }
        if persistCredentials {
            try KeychainCredentials.save(nodeID: id, nodeToken: token, for: activeControllerURL)
        }
        nodeID = id
        nodeToken = token
        registrationToken = ""
        addMessage("节点注册完成：\(id)")
    }

    private func heartbeat(generation: UUID) async throws {
        guard let nodeID, let nodeToken else { throw ClientError.message("节点凭据尚未载入") }
        let desired = acceptingTasks, revision = acceptingRevision
        let response = try await request("POST", "/v1/nodes/\(nodeID)/heartbeat", token: nodeToken, body: [
            "available_memory_mb": memoryMB,
            "accepting_tasks": desired
        ])
        try Task.checkCancellation()
        guard generation == sessionGeneration, !isStopping, revision == acceptingRevision else { return }
        guard response["id"] as? String == nodeID,
              let acknowledged = response["accepting_tasks"] as? NSNumber,
              CFGetTypeID(acknowledged) == CFBooleanGetTypeID(), acknowledged.boolValue == desired else {
            throw ClientError.message("认证心跳未确认同一节点与接单设置")
        }
        confirmedAcceptingTasks = desired
        confirmedRevision = revision
        lastHeartbeatAt = Date()
        connectionState = .connected
    }

    private func pollAssignments() async throws {
        guard let nodeID, let nodeToken else { return }
        let response = try await request("GET", "/v1/nodes/\(nodeID)/assignments", token: nodeToken)
        guard let assignments = response["assignments"] as? [[String: Any]] else { return }
        for assignment in assignments {
            try Task.checkCancellation()
            guard canClaimNewWork else { break }
            guard let id = assignment["id"] as? String,
                  !startedAssignments.contains(id) else { continue }
            if ["completed", "failed", "cancelled"].contains(assignment["status"] as? String ?? "") {
                startedAssignments.insert(id)
                continue
            }
            guard ["queued", "running"].contains(assignment["task_status"] as? String ?? ""),
                  let deadline = assignment["deadline"] as? Double,
                  deadline > Date().timeIntervalSince1970 else {
                startedAssignments.insert(id)
                continue
            }
            startedAssignments.insert(id)
            currentTask = (assignment["task_id"] as? String) ?? id
            try await execute(assignment, id: id)
            currentTask = "空闲"
        }
    }

    private func execute(_ assignment: [String: Any], id: String) async throws {
        guard let nodeID, let nodeToken else { return }
        guard let taskID = assignment["task_id"] as? String else {
            throw ClientError.message("任务缺少来源标识")
        }
        // A previously persisted outcome must be replayed before considering execution.
        if try reports.load().contains(where: { $0.origin == activeControllerURL && $0.nodeID == nodeID && $0.assignmentID == id }) {
            try await replayPendingReports(origin: activeControllerURL, nodeID: nodeID, token: nodeToken)
            return
        }
        guard let requestData = assignment["request"] as? [String: Any],
              requestData["model_id"] as? String == "lastcore/linear-demo-v1",
              let input = requestData["input_vector"] as? [Double],
              let seed = requestData["weight_seed"] as? Int,
              let start = assignment["layer_start"] as? Int,
              let end = assignment["layer_end"] as? Int,
              !input.isEmpty, input.count <= 256, input.allSatisfy({ $0.isFinite && abs($0) <= 1_000_000 }),
              (0...2_147_483_647).contains(seed), 0 <= start, start < end, end <= 4096 else {
            try reports.put(PendingReport(origin: activeControllerURL, nodeID: nodeID,
                taskID: taskID, assignmentID: id, modelID: requestModel(assignment),
                layerStart: assignment["layer_start"] as? Int ?? 0, layerEnd: assignment["layer_end"] as? Int ?? 0,
                status: "failed", result: nil,
                error: "unsupported or invalid bounded linear workload"))
            try await replayPendingReports(origin: activeControllerURL, nodeID: nodeID, token: nodeToken)
            return
        }
        do {
            _ = try await report(nodeID, nodeToken, id, ["status": "running"])
        } catch {
            // A transport outage is not an execution failure. Reclaim safely on the next poll.
            startedAssignments.remove(id)
            throw error
        }
        let result: [Double]
        do {
            try Task.checkCancellation()
            result = try (start..<end).map { row -> Double in
                try Task.checkCancellation()
                guard let deadline = assignment["deadline"] as? Double,
                      deadline > Date().timeIntervalSince1970 else { throw CancellationError() }
                var total = weight(seed: UInt32(seed), row: UInt32(row), column: UInt32(input.count))
                for (column, value) in input.enumerated() {
                    total += weight(seed: UInt32(seed), row: UInt32(row), column: UInt32(column)) * value
                }
                return tanh(total)
            }
        } catch {
            startedAssignments.remove(id)
            throw error
        }
        // Save before POST: an interrupted or lost ACK can only replay these exact values.
        try reports.put(PendingReport(origin: activeControllerURL, nodeID: nodeID,
            taskID: taskID, assignmentID: id, modelID: "lastcore/linear-demo-v1",
            layerStart: start, layerEnd: end, status: "completed", result: result, error: nil))
        try await replayPendingReports(origin: activeControllerURL, nodeID: nodeID, token: nodeToken)
    }

    private func requestModel(_ assignment: [String: Any]) -> String {
        (assignment["request"] as? [String: Any])?["model_id"] as? String ?? "unknown"
    }

    func replayPendingReports(origin: String, nodeID: String, token: String) async throws {
        for pending in try reports.load() where pending.origin == origin && pending.nodeID == nodeID {
            var body: [String: Any] = ["status": pending.status]
            if let result = pending.result { body["result"] = result }
            if let error = pending.error { body["error"] = error }
            let acknowledgement = try await request("POST",
                "/v1/nodes/\(nodeID)/assignments/\(pending.assignmentID)/report", token: token,
                body: body, origin: origin)
            guard acknowledgement["id"] as? String == pending.taskID,
                  acknowledgement["status"] as? String == pending.status,
                  let assignments = acknowledgement["assignments"] as? [[String: Any]],
                  let saved = assignments.first(where: { $0["id"] as? String == pending.assignmentID }),
                  saved["node_id"] as? String == nodeID,
                  saved["task_id"] as? String == pending.taskID,
                  saved["layer_start"] as? Int == pending.layerStart,
                  saved["layer_end"] as? Int == pending.layerEnd,
                  (acknowledgement["request"] as? [String: Any])?["model_id"] as? String == pending.modelID,
                  saved["status"] as? String == pending.status,
                  (saved["result"] as? [Double]) == pending.result else {
                throw ClientError.message("云端尚未确认同一任务结果；待回报记录已保留")
            }
            try Task.checkCancellation()
            try reports.remove(pending)
            startedAssignments.insert(pending.assignmentID)
            if let result = pending.result {
                lastResult = result
                lastCompletedTaskID = pending.taskID
                addMessage("任务 \(pending.assignmentID) 完成（输出 \(result.count) 项）")
            } else {
                addMessage("任务 \(pending.assignmentID) 已确认失败")
            }
        }
    }

    private func weight(seed: UInt32, row: UInt32, column: UInt32) -> Double {
        var value = seed ^ (row &* 0x9E3779B1) ^ (column &* 0x85EBCA77)
        value ^= value >> 16
        value = value &* 0x7FEB352D
        value ^= value >> 15
        value = value &* 0x846CA68B
        value ^= value >> 16
        return (Double(value) / Double(UInt32.max)) * 2.0 - 1.0
    }

    private func report(_ nodeID: String, _ token: String, _ assignmentID: String,
                        _ body: [String: Any]) async throws -> [String: Any] {
        try await request("POST", "/v1/nodes/\(nodeID)/assignments/\(assignmentID)/report", token: token, body: body)
    }

    private func request(_ method: String, _ path: String, token: String,
                         body: [String: Any]? = nil, origin: String? = nil) async throws -> [String: Any] {
        let base = origin ?? activeControllerURL
        guard let url = URL(string: base + path) else { throw ClientError.message("Controller 地址无效") }
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [.fragmentsAllowed, .sortedKeys])
        }
        let (data, response) = try await session.data(for: request)
        try Task.checkCancellation()
        guard let http = response as? HTTPURLResponse else { throw ClientError.message("Controller 未返回 HTTP 响应") }
        guard (200..<300).contains(http.statusCode) else {
            throw ClientError.message("HTTP \(http.statusCode)：请求尚未确认，待回报记录已保留")
        }
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw ClientError.message("Controller 返回了无效 JSON")
        }
        return object
    }

    private func saveSettings() {
        UserDefaults.standard.set(controllerURL, forKey: "controllerURL")
        UserDefaults.standard.set(nodeName, forKey: "nodeName")
        UserDefaults.standard.set(endpoint, forKey: "endpoint")
    }

    private func addMessage(_ message: String) {
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(identifier: "Asia/Shanghai")
        formatter.dateFormat = "HH:mm:ss"
        let time = formatter.string(from: Date())
        messages.append("\(time)  \(message)")
        messages = Array(messages.suffix(30))
    }
}

private final class NoRedirectDelegate: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest,
                    completionHandler: @escaping (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

private enum ClientError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        if case let .message(text) = self { return text }
        return nil
    }
}

private struct KeychainCredentials: Codable {
    let nodeID: String
    let nodeToken: String
    private static let service = "ai.lastcore.node"
    private static func account(for controllerURL: String) -> String {
        "credentials:" + controllerURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
    }

    static func load(for controllerURL: String) -> KeychainCredentials? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account(for: controllerURL),
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(Self.self, from: data)
    }

    static func save(nodeID: String, nodeToken: String, for controllerURL: String) throws {
        let data = try JSONEncoder().encode(Self(nodeID: nodeID, nodeToken: nodeToken))
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account(for: controllerURL)
        ]
        let attributes: [String: Any] = [kSecValueData as String: data]
        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var insert = query
            insert[kSecValueData as String] = data
            let addStatus = SecItemAdd(insert as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw ClientError.message("无法将节点凭据保存到 macOS 钥匙串（\(addStatus)）") }
        } else if status != errSecSuccess {
            throw ClientError.message("无法更新 macOS 钥匙串中的节点凭据（\(status)）")
        }
    }
}

extension ProcessInfo {
    var processArchitecture: String {
        #if arch(arm64)
        return "arm64"
        #elseif arch(x86_64)
        return "x86_64"
        #else
        return "unknown"
        #endif
    }
}
