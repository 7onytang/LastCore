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
    static var heldPaths = Set<String>()
    static var held: [FixtureProtocol] = []
    private var heldReply: (Int, [String: Any])?
    static var waiting: FixtureProtocol?
    static var handler: ((URLRequest) throws -> (Int, [String: Any]))!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, body) = try Self.handler(request)
            if Self.holdCompletion && request.url!.path.hasSuffix("/report") {
                Self.waiting = self
                return
            }
            if Self.heldPaths.contains(request.url!.path) {
                heldReply = (status, body)
                Self.held.append(self)
                return
            }
            try deliver(status, body)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    func finishHeld() throws {
        guard let (status, body) = heldReply else { return }
        heldReply = nil
        try deliver(status, body)
    }
    private func deliver(_ status: Int, _ body: [String: Any]) throws {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try JSONSerialization.data(withJSONObject: body))
        client?.urlProtocolDidFinishLoading(self)
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
            case "/v1/nodes/node/heartbeat": return (200, ["id": "node", "accepting_tasks": try requestBody(request)["accepting_tasks"]!])
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
            case "/v1/nodes/node/heartbeat": return (200, ["id": "node", "accepting_tasks": try requestBody(request)["accepting_tasks"]!])
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
        XCTAssertTrue(model.isRunning, "Stopping must retain the old loop until cleanup")
        XCTAssertTrue(model.isStopping)
        try await Task.sleep(for: .milliseconds(20))
        model.start()
        XCTAssertTrue(model.isRunning)
        model.stop()
        try await Task.sleep(for: .milliseconds(20))
        XCTAssertFalse(model.isRunning)
    }

    @MainActor
    func waitUntil(_ description: String, _ predicate: () -> Bool) async throws {
        for _ in 0..<500 {
            if predicate() { return }
            try await Task.sleep(for: .milliseconds(10))
        }
        XCTFail("Timed out: " + description)
    }

    func sessionReply(_ request: URLRequest) throws -> (Int, [String: Any]) {
        switch request.url!.path {
        case "/v1/nodes/register": return (200, ["node_id": "node", "node_token": "fixture-node"])
        case "/v1/nodes/node/heartbeat": return (200, ["id": "node", "accepting_tasks": try requestBody(request)["accepting_tasks"]!])
        case "/v1/nodes/node/assignments": return (200, ["assignments": []])
        default: XCTFail("Unexpected route"); return (500, [:])
        }
    }

    @MainActor
    func testConnectingRequiresAuthenticatedHeartbeatAndUsesSessionConfiguration() async throws {
        let (model, directory) = try fixture()
        defer { FixtureProtocol.heldPaths=[]; FixtureProtocol.held=[]; try? FileManager.default.removeItem(at: directory) }
        FixtureProtocol.handler = sessionReply
        FixtureProtocol.heldPaths=["/v1/nodes/register"]
        model.controllerURL=record.origin
        model.registrationToken="PRIVATE_FIXTURE_REGISTRATION"
        model.start()
        XCTAssertTrue(model.connectionState == .connecting)
        XCTAssertFalse(model.isConnectionConfirmed)
        XCTAssertNil(model.confirmedAcceptingTasks)
        try await waitUntil("held registration") { FixtureProtocol.held.count == 1 }
        let registration=try requestBody(FixtureProtocol.held[0].request)
        XCTAssertEqual(registration["available_memory_mb"] as? Int,4096)
        XCTAssertEqual(registration["compute_weight"] as? Double,1.0)
        XCTAssertTrue(model.configuredMemoryText.contains("非系统实测"))
        XCTAssertTrue(model.configuredComputeText.contains("非基准测试"))
        model.controllerURL="https://changed.invalid"
        FixtureProtocol.heldPaths=["/v1/nodes/node/heartbeat"]
        try FixtureProtocol.held[0].finishHeld()
        try await waitUntil("held first heartbeat") { FixtureProtocol.held.count == 2 }
        XCTAssertFalse(model.isConnectionConfirmed)
        XCTAssertEqual(model.sessionOrigin,record.origin)
        XCTAssertEqual(FixtureProtocol.held[1].request.url?.host,"fixture.invalid")
        XCTAssertFalse(model.statusText.contains("PRIVATE_FIXTURE"))
        XCTAssertEqual(FixtureProtocol.held[1].request.value(forHTTPHeaderField:"Authorization"),"Bearer fixture-node")
        try FixtureProtocol.held[1].finishHeld()
        try await waitUntil("confirmed connection") { model.isConnectionConfirmed }
        XCTAssertEqual(model.confirmedAcceptingTasks,true)
        XCTAssertTrue(model.heartbeatText.contains("北京时间"))
        model.stop()
        XCTAssertTrue(model.isStopping && model.isRunning)
        try await waitUntil("stopped cleanup") { !model.isRunning }
        XCTAssertTrue(model.connectionState == .stopped)
    }

    @MainActor
    func testNetworkFailureIsNotConnectedAndSuccessfulHeartbeatRecovers() async throws {
        let (model, directory) = try fixture()
        defer { try? FileManager.default.removeItem(at: directory) }
        var heartbeats=0
        FixtureProtocol.handler = { request in
            if request.url!.path.hasSuffix("/heartbeat") {
                heartbeats += 1
                if heartbeats == 1 { throw URLError(.networkConnectionLost) }
            }
            return try self.sessionReply(request)
        }
        model.controllerURL=record.origin; model.registrationToken="fixture"
        model.start()
        try await waitUntil("retry state") { model.connectionState == .retrying }
        XCTAssertFalse(model.isConnectionConfirmed)
        XCTAssertNil(model.confirmedAcceptingTasks)
        XCTAssertTrue(model.lastError != nil)
        try await waitUntil("heartbeat recovered") { model.isConnectionConfirmed }
        XCTAssertTrue(heartbeats >= 2)
        model.stop(); try await waitUntil("cleanup") { !model.isRunning }
    }

    @MainActor
    func testAuthenticatedSessionErrorClearsConfirmationWithoutLeakingServerDetail() async throws {
        let (model,directory)=try fixture()
        defer { try? FileManager.default.removeItem(at:directory) }
        FixtureProtocol.handler={ request in
            if request.url!.path.hasSuffix("/assignments") {
                return (500,["detail":"PRIVATE_SERVER_TOKEN_PLACEHOLDER"])
            }
            return try self.sessionReply(request)
        }
        model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
        try await waitUntil("authenticated session now retrying") { model.connectionState == .retrying }
        XCTAssertTrue(model.lastHeartbeatAt != nil,"Must have had an actual authenticated heartbeat first")
        XCTAssertFalse(model.isConnectionConfirmed);XCTAssertNil(model.confirmedAcceptingTasks)
        XCTAssertFalse((model.lastError ?? "").contains("PRIVATE_SERVER_TOKEN"))
        XCTAssertFalse(model.messages.joined().contains("PRIVATE_SERVER_TOKEN"))
        model.stop();try await waitUntil("cleanup") { !model.isRunning }
    }

    @MainActor
    func testInitialNetworkFailureEndsUnconfirmed() async throws {
        let (model,directory)=try fixture()
        defer { try? FileManager.default.removeItem(at:directory) }
        FixtureProtocol.handler={ _ in throw URLError(.cannotConnectToHost) }
        model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
        try await waitUntil("unreachable registration") { !model.isRunning }
        XCTAssertTrue(model.connectionState == .unreachable)
        XCTAssertFalse(model.isConnectionConfirmed)
        XCTAssertNil(model.lastHeartbeatAt)
    }

    @MainActor
    func testMalformedOrUnmatchedHeartbeatNeverConfirms() async throws {
        for bad in [[String:Any](), ["id":"other", "accepting_tasks":true], ["id":"node", "accepting_tasks":1], ["id":"node", "accepting_tasks":false]] {
            let (model,directory)=try fixture()
            FixtureProtocol.handler={ request in
                if request.url!.path.hasSuffix("/heartbeat") { return (200,bad) }
                return try self.sessionReply(request)
            }
            model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
            try await waitUntil("bad heartbeat rejected") { model.connectionState == .retrying }
            XCTAssertFalse(model.isConnectionConfirmed);XCTAssertNil(model.lastHeartbeatAt)
            model.stop();try await waitUntil("cleanup") { !model.isRunning }
            try? FileManager.default.removeItem(at:directory)
        }
    }

    @MainActor
    func testDesiredRevisionRejectsDelayedHeartbeatEvenWhenValueReturnsToSame() async throws {
        let (model,directory)=try fixture()
        defer { FixtureProtocol.heldPaths=[];FixtureProtocol.held=[];try? FileManager.default.removeItem(at:directory) }
        FixtureProtocol.handler=sessionReply
        FixtureProtocol.heldPaths=["/v1/nodes/node/heartbeat"]
        model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
        try await waitUntil("first held heartbeat") { FixtureProtocol.held.count == 1 }
        model.acceptingTasks=false;model.acceptingTasks=true
        try FixtureProtocol.held[0].finishHeld()
        try await Task.sleep(for:.milliseconds(30))
        XCTAssertFalse(model.isConnectionConfirmed)
        XCTAssertNil(model.confirmedAcceptingTasks)
        try await waitUntil("new revision heartbeat") { FixtureProtocol.held.count == 2 }
        try FixtureProtocol.held[1].finishHeld()
        try await waitUntil("new revision confirmed") { model.isConnectionConfirmed }
        model.stop();try await waitUntil("cleanup") { !model.isRunning }
    }

    @MainActor
    func testPauseAndResumeWaitForMatchingHeartbeat() async throws {
        let (model,directory)=try fixture()
        defer { FixtureProtocol.heldPaths=[];FixtureProtocol.held=[];try? FileManager.default.removeItem(at:directory) }
        FixtureProtocol.handler=sessionReply
        model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
        try await waitUntil("connected") { model.isConnectionConfirmed }
        FixtureProtocol.heldPaths=["/v1/nodes/node/heartbeat"]
        model.acceptingTasks=false
        XCTAssertTrue(model.acceptanceText.contains("待心跳确认"))
        try await waitUntil("held pause") { FixtureProtocol.held.count == 1 }
        try XCTAssertEqual(try requestBody(FixtureProtocol.held[0].request)["accepting_tasks"] as? Bool,false)
        try FixtureProtocol.held[0].finishHeld()
        try await waitUntil("pause confirmed") { model.confirmedAcceptingTasks == false }
        XCTAssertTrue(model.acceptanceText.contains("暂停新任务 · 心跳已确认"))
        model.acceptingTasks=true
        XCTAssertTrue(model.acceptanceText.contains("待心跳确认"))
        try await waitUntil("held resume") { FixtureProtocol.held.count == 2 }
        try FixtureProtocol.held[1].finishHeld()
        try await waitUntil("resume confirmed") { model.confirmedAcceptingTasks == true }
        model.stop();try await waitUntil("cleanup") { !model.isRunning }
    }

    @MainActor
    func testPauseKeepsCurrentReportAndBlocksNextAssignmentWithoutComputing() async throws {
        let (model,directory)=try fixture()
        defer { FixtureProtocol.heldPaths=[];FixtureProtocol.held=[];try? FileManager.default.removeItem(at:directory) }
        let pending=PendingReport(origin:record.origin,nodeID:"node",taskID:"task-a",assignmentID:"a",modelID:record.modelID,layerStart:0,layerEnd:1,status:"failed",result:nil,error:"unsupported or invalid bounded linear workload")
        var posts=0
        FixtureProtocol.handler={ request in
            if request.url!.path.hasSuffix("/assignments") {
                return (200,["assignments":["a","b"].map { id in ["id":id,"task_id":"task-"+id,"status":"pending","task_status":"queued","deadline":Date().timeIntervalSince1970+60,"layer_start":0,"layer_end":1,"request":["model_id":self.record.modelID]] as [String:Any] }])
            }
            if request.url!.path.hasSuffix("/report") {
                XCTAssertTrue(request.url!.path.hasSuffix("/a/report"),"Pause must not claim B")
                posts += 1
                try XCTAssertEqual(try requestBody(request)["status"] as? String,"failed")
                return (200,self.acknowledgement(pending))
            }
            return try self.sessionReply(request)
        }
        FixtureProtocol.heldPaths=["/v1/nodes/node/assignments/a/report"]
        model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
        try await waitUntil("current report held") { FixtureProtocol.held.count == 1 }
        model.acceptingTasks=false
        XCTAssertEqual(model.currentTask,"task-a")
        XCTAssertTrue(model.isRunning && !model.isStopping)
        try XCTAssertEqual(model.reports.load(),[pending])
        try FixtureProtocol.held[0].finishHeld()
        try await waitUntil("current report cleanup") { model.currentTask == "空闲" }
        XCTAssertEqual(posts,1)
        try XCTAssertTrue(model.reports.load().isEmpty)
        model.stop();try await waitUntil("cleanup") { !model.isRunning }
    }

    @MainActor
    func testStopRejectsLateHeartbeatAndKeepsStartDisabledUntilCleanup() async throws {
        let (model,directory)=try fixture()
        defer { FixtureProtocol.heldPaths=[];FixtureProtocol.held=[];try? FileManager.default.removeItem(at:directory) }
        FixtureProtocol.handler=sessionReply
        FixtureProtocol.heldPaths=["/v1/nodes/node/heartbeat"]
        model.controllerURL=record.origin;model.registrationToken="fixture";model.start()
        try await waitUntil("held heartbeat") { FixtureProtocol.held.count == 1 }
        model.stop();model.start()
        XCTAssertTrue(model.isStopping && model.isRunning)
        XCTAssertFalse(model.isConnectionConfirmed)
        try FixtureProtocol.held[0].finishHeld()
        try await waitUntil("cleanup") { !model.isRunning }
        XCTAssertTrue(model.connectionState == .stopped)
        XCTAssertNil(model.confirmedAcceptingTasks)
        XCTAssertNil(model.lastHeartbeatAt)
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
        try await tests.testConnectingRequiresAuthenticatedHeartbeatAndUsesSessionConfiguration()
        try await tests.testNetworkFailureIsNotConnectedAndSuccessfulHeartbeatRecovers()
        try await tests.testAuthenticatedSessionErrorClearsConfirmationWithoutLeakingServerDetail()
        try await tests.testInitialNetworkFailureEndsUnconfirmed()
        try await tests.testMalformedOrUnmatchedHeartbeatNeverConfirms()
        try await tests.testDesiredRevisionRejectsDelayedHeartbeatEvenWhenValueReturnsToSame()
        try await tests.testPauseAndResumeWaitForMatchingHeartbeat()
        try await tests.testPauseKeepsCurrentReportAndBlocksNextAssignmentWithoutComputing()
        try await tests.testStopRejectsLateHeartbeatAndKeepsStartDisabledUntilCleanup()
        print("17 recovery/session fixture tests passed; no real network, GUI or calculation")
    }
}
