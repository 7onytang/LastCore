import Foundation
import Security
import SwiftUI

@MainActor
final class ClientModel: ObservableObject {
    @Published var controllerURL = UserDefaults.standard.string(forKey: "controllerURL") ?? ""
    @Published var nodeName = UserDefaults.standard.string(forKey: "nodeName") ?? Host.current().localizedName ?? "Mac"
    @Published var endpoint = UserDefaults.standard.string(forKey: "endpoint") ?? "http://127.0.0.1:9000"
    @Published var registrationToken = ""
    @Published var acceptingTasks = true
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
    private var activeControllerURL = ""
    private let session = URLSession(configuration: .ephemeral, delegate: NoRedirectDelegate(), delegateQueue: nil)
    private let persistCredentials: Bool

    init(persistCredentials: Bool = true) {
        self.persistCredentials = persistCredentials
    }

    var statusText: String {
        guard isRunning else { return "已停止" }
        return acceptingTasks ? "已连接 · 接收任务" : "已连接 · 暂停接单"
    }

    func start() {
        guard !isRunning else { return }
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
        isRunning = true
        lastError = nil
        loopTask = Task { await runLoop() }
    }

    func stop() {
        loopTask?.cancel()
        loopTask = nil
        isRunning = false
        currentTask = "空闲"
        addMessage("节点已停止")
    }

    private func runLoop() async {
        var retryDelay: UInt64 = 1
        while !Task.isCancelled {
            do {
                try await loadOrRegister()
                try await heartbeat()
                if acceptingTasks { try await pollAssignments() }
                lastError = nil
                retryDelay = 1
                try await Task.sleep(for: .seconds(2))
            } catch is CancellationError {
                break
            } catch {
                lastError = error.localizedDescription
                addMessage("连接错误：\(error.localizedDescription)")
                if nodeID == nil && nodeToken == nil {
                    isRunning = false
                    return
                }
                do { try await Task.sleep(for: .seconds(retryDelay)) } catch { break }
                retryDelay = min(retryDelay * 2, 30)
            }
        }
        isRunning = false
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
            "compute_weight": 1.0
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

    private func heartbeat() async throws {
        guard let nodeID, let nodeToken else { throw ClientError.message("节点凭据尚未载入") }
        _ = try await request("POST", "/v1/nodes/\(nodeID)/heartbeat", token: nodeToken, body: [
            "available_memory_mb": memoryMB,
            "accepting_tasks": acceptingTasks
        ])
    }

    private func pollAssignments() async throws {
        guard let nodeID, let nodeToken else { return }
        let response = try await request("GET", "/v1/nodes/\(nodeID)/assignments", token: nodeToken)
        guard let assignments = response["assignments"] as? [[String: Any]] else { return }
        for assignment in assignments {
            try Task.checkCancellation()
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
            await execute(assignment, id: id)
            currentTask = "空闲"
        }
    }

    private func execute(_ assignment: [String: Any], id: String) async {
        guard let nodeID, let nodeToken else { return }
        guard let requestData = assignment["request"] as? [String: Any],
              requestData["model_id"] as? String == "lastcore/linear-demo-v1",
              let input = requestData["input_vector"] as? [Double],
              let seed = requestData["weight_seed"] as? Int,
              let start = assignment["layer_start"] as? Int,
              let end = assignment["layer_end"] as? Int else {
            _ = try? await report(nodeID, nodeToken, id, ["status": "failed", "error": "unsupported workload; expected lastcore/linear-demo-v1 with input_vector"])
            addMessage("任务 \(id) 不受支持，已报告失败")
            return
        }
        do {
            _ = try await report(nodeID, nodeToken, id, ["status": "running"])
            let result = (start..<end).map { row in
                var total = weight(seed: UInt32(truncatingIfNeeded: seed), row: UInt32(row), column: UInt32(input.count))
                for (column, value) in input.enumerated() {
                    total += weight(seed: UInt32(truncatingIfNeeded: seed), row: UInt32(row), column: UInt32(column)) * value
                }
                return tanh(total)
            }
            let acknowledgement = try await report(nodeID, nodeToken, id, ["status": "completed", "result": result])
            guard acknowledgement["status"] as? String == "completed",
                  let savedResult = acknowledgement["result"] as? [Double], savedResult == result else {
                throw ClientError.message("云端尚未确认同一计算结果")
            }
            lastResult = savedResult
            lastCompletedTaskID = assignment["task_id"] as? String
            addMessage("任务 \(id) 完成（输出 \(result.count) 项）")
        } catch {
            _ = try? await report(nodeID, nodeToken, id, ["status": "failed", "error": String(error.localizedDescription.prefix(2048))])
            addMessage("任务 \(id) 失败：\(error.localizedDescription)")
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
                         body: [String: Any]? = nil) async throws -> [String: Any] {
        let base = activeControllerURL
        guard let url = URL(string: base + path) else { throw ClientError.message("Controller 地址无效") }
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body, options: [.fragmentsAllowed, .sortedKeys])
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw ClientError.message("Controller 未返回 HTTP 响应") }
        guard (200..<300).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw ClientError.message("HTTP \(http.statusCode)：\(String(describing: detail?["detail"] ?? "请求失败"))")
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
