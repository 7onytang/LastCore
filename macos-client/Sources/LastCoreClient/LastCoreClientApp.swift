import SwiftUI

@main
struct LastCoreClientApp: App {
    @StateObject private var model = ClientModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .frame(minWidth: 620, minHeight: 560)
        }
        .windowResizability(.contentSize)
    }
}

struct ContentView: View {
    @ObservedObject var model: ClientModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            Form {
                Section("Controller") {
                    TextField("https://controller.example.com", text: $model.controllerURL)
                        .textFieldStyle(.roundedBorder)
                    TextField("节点名称", text: $model.nodeName)
                        .textFieldStyle(.roundedBorder)
                    TextField("本机可达地址", text: $model.endpoint)
                        .textFieldStyle(.roundedBorder)
                    SecureField("注册凭据（仅首次注册需要）", text: $model.registrationToken)
                        .textFieldStyle(.roundedBorder)
                }
                .disabled(model.isRunning)

                Section("节点") {
                    LabeledContent("节点 ID", value: model.nodeID ?? "尚未注册")
                    LabeledContent("设备", value: "macOS-\(ProcessInfo.processInfo.processArchitecture)")
                    Toggle("接收新任务", isOn: $model.acceptingTasks)
                        .disabled(!model.isRunning)
                    LabeledContent("当前任务", value: model.currentTask)
                }

                Section("活动") {
                    ForEach(model.messages.reversed(), id: \.self) { message in
                        Text(message)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                    if model.messages.isEmpty {
                        Text("尚无活动记录").foregroundStyle(.secondary)
                    }
                }
                if let result = model.lastResult {
                    Section("最近一次计算结果 · 云端已确认") {
                        Text(model.lastCompletedTaskID ?? "")
                            .font(.caption).textSelection(.enabled)
                        Text(result.prefix(16).map { String(format: "%.8f", $0) }.joined(separator: ", "))
                            .font(.system(.body, design: .monospaced)).textSelection(.enabled)
                        if result.count > 16 { Text("显示前 16 项，共 \(result.count) 项") }
                    }
                }
            }
            .formStyle(.grouped)

            Divider()
            HStack {
                Text(model.lastError ?? "仅执行内置线性演示任务，不会下载模型文件")
                    .font(.caption)
                    .foregroundStyle(model.lastError == nil ? Color.secondary : Color.red)
                    .lineLimit(2)
                Spacer()
                if model.isRunning {
                    Button("停止", systemImage: "stop.fill", role: .destructive) {
                        model.stop()
                    }
                } else {
                    Button("连接节点", systemImage: "network") {
                        model.start()
                    }
                    .keyboardShortcut(.defaultAction)
                }
            }
            .padding(16)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 24, weight: .medium))
                .foregroundStyle(.teal)
                .frame(width: 42, height: 42)
            VStack(alignment: .leading, spacing: 3) {
                Text("LastCore 节点").font(.title3.weight(.semibold))
                Text("LastCoreClient 0.1.1 · 线性任务").font(.caption2).foregroundStyle(.secondary)
                Text(model.statusText)
                    .font(.caption)
                    .foregroundStyle(model.isRunning ? .green : .secondary)
            }
            Spacer()
            Circle()
                .fill(model.isRunning ? Color.green : Color.gray.opacity(0.5))
                .frame(width: 9, height: 9)
                .accessibilityLabel(model.statusText)
        }
        .padding(16)
    }
}
