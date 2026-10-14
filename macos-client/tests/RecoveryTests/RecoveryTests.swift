import Foundation
func XCTFail(_ message: String) { fatalError(message) }
func XCTAssertTrue(_ value: @autoclosure () throws -> Bool, _ message: String = "assert true") rethrows {
    if try !value() { fatalError(message) }
}
func XCTAssertFalse(_ value: @autoclosure () throws -> Bool, _ message: String = "assert false") rethrows {
    if try value() { fatalError(message) }
}
func XCTAssertEqual<T: Equatable>(_ lhs: @autoclosure () throws -> T, _ rhs: @autoclosure () throws -> T) rethrows {
    if try lhs() != rhs() { fatalError("assert equal") }
}
func XCTAssertNil<T>(_ value: T?) { if value != nil { fatalError("assert nil") } }
func XCTAssertThrowsError<T>(_ value: @autoclosure () throws -> T) {
    do { _ = try value(); fatalError("expected error") } catch {}
}

private final class FixtureProtocol: URLProtocol {
    static var holdCompletion = false
    static var waiting: FixtureProtocol?
    static var handler: ((URLRequest) throws -> (Int, [String: Any]))!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, body) = try Self.handler(request)
            if Self.holdCompletion && request.url!.path.hasSuffix("/report") { Self.waiting = self; return }
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try JSONSerialization.data(withJSONObject: body))
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

private func requestBody(_ request: URLRequest) throws -> [String: Any] {
    if let data = request.httpBody { return try JSONSerialization.jsonObject(with: data) as! [String: Any] }
    let stream = request.httpBodyStream!
    stream.open()
    defer { stream.close() }
    var data = Data()
    var bytes = [UInt8](repeating: 0, count: 1024)
    while stream.hasBytesAvailable {
        let count = stream.read(&bytes, maxLength: bytes.count)
        guard count >= 0 else { throw URLError(.cannotDecodeRawData) }
        if count == 0 { break }
        data.append(contentsOf: bytes.prefix(count))
    }
    return try JSONSerialization.jsonObject(with: data) as! [String: Any]
}

final class RecoveryTests {
    @MainActor
    func fixture() throws -> (ClientModel, URL) {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [FixtureProtocol.self]
        let model = ClientModel(persistCredentials: false, session: URLSession(configuration: configuration),
            journalURL: directory.appendingPathComponent("pending.json"))
        return (model, directory)
    }

    let record = PendingReport(origin: "https://fixture.invalid", nodeID: "node", taskID: "task",
        assignmentID: "assignment", modelID: "lastcore/linear-demo-v1", layerStart: 0, layerEnd: 2,
        status: "completed", result: [0.1, 0.2], error: nil)

    func acknowledgement(_ record: PendingReport) -> [String: Any] {
        ["id": record.taskID, "status": record.status, "request": ["model_id": record.modelID],
         "assignments": [["id": record.assignmentID, "node_id": record.nodeID, "task_id": record.taskID,
                          "layer_start": record.layerStart, "layer_end": record.layerEnd,
                          "status": record.status, "result": record.result as Any? ?? NSNull()]]]
    }

    @MainActor
    func testLostAckReplaysExactCachedOutcomeAfterRestart() async throws {
        let (first, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        try first.reports.put(record)
        let original = try Data(contentsOf: first.reports.url)
        FixtureProtocol.handler = { _ in throw URLError(.networkConnectionLost) }
        do {
            try await first.replayPendingReports(origin: record.origin, nodeID: record.nodeID, token: "fixture-token")
            XCTFail("Lost ACK must remain pending")
        } catch {}
        try XCTAssertEqual(try Data(contentsOf: first.reports.url), original)
        XCTAssertNil(first.lastResult)
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FixtureProtocol.self]
        let restarted = ClientModel(persistCredentials: false, session: URLSession(configuration: config),
            journalURL: first.reports.url)
        let ack = acknowledgement(record)
        FixtureProtocol.handler = { request in
            XCTAssertEqual(request.url?.host, "fixture.invalid")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer fixture-token")
            let body = try requestBody(request)
            XCTAssertEqual(Set(body.keys), Set(["status", "result"]))
            XCTAssertEqual(body["status"] as? String, "completed")
            XCTAssertEqual(body["result"] as? [Double], self.record.result)
            return (200, ack)
        }
        try await restarted.replayPendingReports(origin: record.origin, nodeID: record.nodeID, token: "fixture-token")
        XCTAssertEqual(restarted.lastResult, record.result)
        XCTAssertEqual(restarted.lastCompletedTaskID, record.taskID)
        try XCTAssertTrue(try restarted.reports.load().isEmpty)
        XCTAssertFalse(String(data: original, encoding: .utf8)!.contains("fixture-token"))
    }

    @MainActor
    func testOtherOriginOrIdentityCannotReplayOrPublish() async throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        try model.reports.put(record)
        FixtureProtocol.handler = { _ in XCTFail("No cross-identity requests"); return (500, [:]) }
        try await model.replayPendingReports(origin: "https://other.invalid", nodeID: record.nodeID, token: "unused")
        try await model.replayPendingReports(origin: record.origin, nodeID: "other", token: "unused")
        try XCTAssertEqual(try model.reports.load(), [record])
        XCTAssertNil(model.lastResult)
    }

    @MainActor
    func testWrongAckAndTerminalConflictsRetainRecord() async throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        try model.reports.put(record)
        var wrong = acknowledgement(record)
        wrong["id"] = "other-task"
        var wrongNode = acknowledgement(record)
        var assignment = (wrongNode["assignments"] as! [[String: Any]])[0]
        assignment["node_id"] = "other-node"
        wrongNode["assignments"] = [assignment]
        var wrongResult = acknowledgement(record)
        assignment = (wrongResult["assignments"] as! [[String: Any]])[0]
        assignment["result"] = [0.9, 0.8]
        wrongResult["assignments"] = [assignment]
        var wrongRange = acknowledgement(record)
        assignment = (wrongRange["assignments"] as! [[String: Any]])[0]
        assignment["layer_end"] = 3
        wrongRange["assignments"] = [assignment]
        var wrongModel = acknowledgement(record)
        wrongModel["request"] = ["model_id": "other-model"]
        for (status, response) in [(200, wrong), (200, wrongNode), (200, wrongResult),
                                   (200, wrongRange), (200, wrongModel), (404, ["detail": "not found"]), (409, ["detail": "terminal"])] {
            FixtureProtocol.handler = { _ in (status, response) }
            do {
                try await model.replayPendingReports(origin: record.origin, nodeID: record.nodeID, token: "fixture")
                XCTFail("Unconfirmed outcome must remain pending")
            } catch {}
            try XCTAssertEqual(try model.reports.load(), [record])
            XCTAssertNil(model.lastResult)
        }
    }

    @MainActor
    func testFailedOutcomeRequiresAcknowledgement() async throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        let failed = PendingReport(origin: record.origin, nodeID: record.nodeID, taskID: record.taskID,
            assignmentID: record.assignmentID, modelID: record.modelID, layerStart: 0, layerEnd: 2,
            status: "failed", result: nil, error: "fixture failure")
        try model.reports.put(failed)
        let ack = acknowledgement(failed)
        FixtureProtocol.handler = { _ in (200, ack) }
        try await model.replayPendingReports(origin: failed.origin, nodeID: failed.nodeID, token: "fixture")
        try XCTAssertTrue(try model.reports.load().isEmpty)
        XCTAssertNil(model.lastResult)
    }

    @MainActor
    func testCorruptionAndInvalidRangeFailClosed() throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        try model.reports.put(record)
        try Data("{bad json".utf8).write(to: model.reports.url)
        XCTAssertThrowsError(try model.reports.load())
        XCTAssertThrowsError(try model.reports.put(record))
        try XCTAssertEqual(try String(contentsOf: model.reports.url, encoding: .utf8), "{bad json")
        let invalid = PendingReport(origin: record.origin, nodeID: record.nodeID, taskID: record.taskID,
            assignmentID: record.assignmentID, modelID: record.modelID, layerStart: 0, layerEnd: 3,
            status: "completed", result: record.result, error: nil)
        try JSONEncoder().encode([invalid]).write(to: model.reports.url)
        XCTAssertThrowsError(try model.reports.load())
    }
    @MainActor
    func testRunningReportOutageRetriesWithoutComputingOrFailing() async throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        var runningReports = 0
        FixtureProtocol.handler = { request in
            switch request.url!.path {
            case "/v1/nodes/register": return (200, ["node_id": "node", "node_token": "fixture-node"])
            case "/v1/nodes/node/heartbeat": return (200, [:])
            case "/v1/nodes/node/assignments":
                return (200, ["assignments": [["id": "assignment", "task_id": "task", "status": "pending",
                    "task_status": "queued", "deadline": Date().timeIntervalSince1970 + 60,
                    "layer_start": 0, "layer_end": 1,
                    "request": ["model_id": "lastcore/linear-demo-v1", "input_vector": [1.0], "weight_seed": 7]]]])
            default:
                let stream = request.httpBodyStream!
                stream.open()
                defer { stream.close() }
                var bytes = [UInt8](repeating: 0, count: 4096)
                let count = stream.read(&bytes, maxLength: bytes.count)
                let payload = try JSONSerialization.jsonObject(with: Data(bytes.prefix(count))) as! [String: Any]
                XCTAssertEqual(payload["status"] as? String, "running")
                runningReports += 1
                throw URLError(.networkConnectionLost)
            }
        }
        model.controllerURL = record.origin
        model.registrationToken = "fixture-registration"
        model.start()
        try await Task.sleep(for: .milliseconds(2300))
        model.stop()
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertTrue(runningReports >= 2, "Transient running outage must permit retry")
        XCTAssertNil(model.lastResult)
        try XCTAssertTrue(model.reports.load().isEmpty)
    }

    @MainActor
    func testStopDuringCompletionAwaitPreservesOutcomeThenReplays() async throws {
        let (model, directory) = try fixture()
        defer {
            FixtureProtocol.holdCompletion = false
            FixtureProtocol.waiting = nil
            try? FileManager.default.removeItem(at: directory)
        }
        try model.reports.put(record)
        let original = try Data(contentsOf: model.reports.url)
        var completionBodies: [[String: Any]] = []
        let ack = acknowledgement(record)
        FixtureProtocol.handler = { request in
            switch request.url!.path {
            case "/v1/nodes/register": return (200, ["node_id": "node", "node_token": "fixture-node"])
            case "/v1/nodes/node/heartbeat": return (200, [:])
            case "/v1/nodes/node/assignments": return (200, ["assignments": []])
            default:
                let body = try requestBody(request)
                XCTAssertEqual(Set(body.keys), Set(["status", "result"]))
                XCTAssertEqual(body["status"] as? String, "completed")
                XCTAssertEqual(body["result"] as? [Double], self.record.result)
                completionBodies.append(body)
                return (200, ack)
            }
        }
        FixtureProtocol.holdCompletion = true
        model.controllerURL = record.origin
        model.registrationToken = "fixture-registration"
        model.start()
        for _ in 0..<100 where FixtureProtocol.waiting == nil {
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertTrue(FixtureProtocol.waiting != nil, "Fixture must pause in completion ACK await")
        model.stop()
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertFalse(model.isRunning)
        XCTAssertNil(model.lastResult)
        try XCTAssertEqual(Data(contentsOf: model.reports.url), original)
        FixtureProtocol.holdCompletion = false
        FixtureProtocol.waiting = nil
        model.registrationToken = "fixture-registration"
        model.start()
        for _ in 0..<100 where model.lastResult == nil {
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTAssertEqual(model.lastResult, record.result)
        XCTAssertEqual(completionBodies.count, 2)
        XCTAssertEqual(completionBodies[0] as NSDictionary, completionBodies[1] as NSDictionary)
        try XCTAssertTrue(model.reports.load().isEmpty)
        model.stop()
        try await Task.sleep(for: .milliseconds(50))
        model.controllerURL = "https://other.invalid"
        model.registrationToken = "fixture-registration"
        model.start()
        XCTAssertNil(model.lastResult)
        XCTAssertNil(model.lastCompletedTaskID)
        model.stop()
        try await Task.sleep(for: .milliseconds(20))
    }

    @MainActor
    func testStopCannotStartAConcurrentOldLoop() async throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        FixtureProtocol.handler = { _ in XCTFail("Cancelled before first request"); return (500, [:]) }
        model.controllerURL = record.origin
        model.registrationToken = "fixture-registration"
        model.start()
        XCTAssertTrue(model.isRunning)
        model.stop()
        model.start()
        XCTAssertFalse(model.isRunning, "Restart must wait for cancelled old loop cleanup")
        try await Task.sleep(for: .milliseconds(20))
        model.start()
        XCTAssertTrue(model.isRunning)
        model.stop()
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertFalse(model.isRunning)
    }

}

@main
struct RecoveryFixtureRunner {
    @MainActor static func main() async throws {
        let tests = RecoveryTests()
        try await tests.testLostAckReplaysExactCachedOutcomeAfterRestart()
        try await tests.testOtherOriginOrIdentityCannotReplayOrPublish()
        try await tests.testWrongAckAndTerminalConflictsRetainRecord()
        try await tests.testFailedOutcomeRequiresAcknowledgement()
        try tests.testCorruptionAndInvalidRangeFailClosed()
        try await tests.testRunningReportOutageRetriesWithoutComputingOrFailing()
        try await tests.testStopDuringCompletionAwaitPreservesOutcomeThenReplays()
        try await tests.testStopCannotStartAConcurrentOldLoop()
        print("8 recovery fixture tests passed; no real network, GUI or calculation")
    }
}
