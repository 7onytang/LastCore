import Foundation

@main
struct SmokeClient {
    @MainActor
    static func main() async throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let data = try Data(contentsOf: root.appendingPathComponent("private.json"))
        let config = try JSONSerialization.jsonObject(with: data) as! [String: String]
        let model = ClientModel(persistCredentials: false)
        model.controllerURL = config["origin"]!
        model.registrationToken = config["registration"]!
        model.nodeName = "isolated-swift-smoke"
        model.endpoint = "http://127.0.0.1:9000"
        model.start()
        defer { model.stop() }
        var publishedID = false
        for _ in 0..<300 {
            if let id = model.nodeID, !publishedID {
                let ready = try JSONSerialization.data(withJSONObject: ["node_id": id])
                try ready.write(to: root.appendingPathComponent("ready.json"), options: .atomic)
                publishedID = true
            }
            if let result = model.lastResult, let id = model.lastCompletedTaskID {
                let outcome = try JSONSerialization.data(withJSONObject: ["task_id": id, "result": result,
                    "ui_observable_result_published": true, "scope": "production Swift ClientModel, isolated loopback Controller"])
                try outcome.write(to: root.appendingPathComponent("result.json"), options: .atomic)
                return
            }
            if !model.isRunning { throw NSError(domain: "Swift smoke", code: 1) }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw NSError(domain: "Swift smoke timeout", code: 2)
    }
}
