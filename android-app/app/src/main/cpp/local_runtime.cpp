// JNI lifetime/limit adapter over the pinned official MIT llama.cpp C API.
#include <jni.h>
#include "llama.h"
#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

namespace {
struct Session {
    std::atomic<bool> cancelled{false};
    std::atomic<bool> used{false};
    std::chrono::steady_clock::time_point deadline;
};
std::mutex registry_mutex;
std::unordered_map<jlong, std::shared_ptr<Session>> sessions;
std::atomic<jlong> next_id{1};
std::once_flag backend_once;
std::shared_ptr<Session> get(jlong id) {
    std::lock_guard<std::mutex> lock(registry_mutex);
    auto it = sessions.find(id);
    if (it == sessions.end()) throw std::runtime_error("Runtime session is closed");
    return it->second;
}
bool aborted(void * data) {
    auto * s = static_cast<Session *>(data);
    return s->cancelled.load() || std::chrono::steady_clock::now() >= s->deadline;
}
bool progress(float, void * data) { return !aborted(data); }
void check(Session & s) { if (aborted(&s)) throw std::runtime_error("Inference cancelled or timed out"); }
void throw_java(JNIEnv * env, const char * message) {
    jclass type = env->FindClass("java/lang/IllegalStateException");
    if (type) env->ThrowNew(type, message);
}
std::string bytes(JNIEnv * env, jbyteArray array, size_t maximum) {
    if (!array) throw std::runtime_error("Missing runtime input");
    const jsize length = env->GetArrayLength(array);
    if (length <= 0 || static_cast<size_t>(length) > maximum) throw std::runtime_error("Runtime input limit exceeded");
    std::string value(length, '\0');
    env->GetByteArrayRegion(array, 0, length, reinterpret_cast<jbyte *>(value.data()));
    if (env->ExceptionCheck() || value.find('\0') != std::string::npos) throw std::runtime_error("Invalid UTF-8 input");
    return value;
}
std::string generate(Session & s, const std::string & path, const std::string & prompt, int limit) {
    if (s.used.exchange(true)) throw std::runtime_error("Runtime session already used");
    s.deadline = std::chrono::steady_clock::now() + std::chrono::seconds(90);
    check(s);
    std::call_once(backend_once, [] {
        // Suppress upstream diagnostic paths/content; errors below remain fixed messages.
        llama_log_set([](ggml_log_level, const char *, void *) {}, nullptr);
        llama_backend_init();
    });
    auto mp = llama_model_default_params();
    mp.n_gpu_layers = 0;
    mp.progress_callback = progress;
    mp.progress_callback_user_data = &s;
    std::unique_ptr<llama_model, decltype(&llama_model_free)> model(
        llama_model_load_from_file(path.c_str(), mp), llama_model_free);
    check(s);
    if (!model) throw std::runtime_error("Unable to load verified model");
    auto * vocab = llama_model_get_vocab(model.get());
    int count = -llama_tokenize(vocab, prompt.data(), prompt.size(), nullptr, 0, true, true);
    if (count <= 0 || count > 192 || count + limit > 256) throw std::runtime_error("Prompt context limit exceeded");
    std::vector<llama_token> tokens(count);
    if (llama_tokenize(vocab, prompt.data(), prompt.size(), tokens.data(), count, true, true) != count)
        throw std::runtime_error("Prompt tokenization failed");
    auto cp = llama_context_default_params();
    cp.n_ctx = 256; cp.n_batch = 256; cp.n_ubatch = 256;
    cp.n_threads = 2; cp.n_threads_batch = 2;
    cp.abort_callback = aborted; cp.abort_callback_data = &s;
    std::unique_ptr<llama_context, decltype(&llama_free)> context(llama_init_from_model(model.get(), cp), llama_free);
    if (!context) throw std::runtime_error("Unable to create bounded context");
    std::unique_ptr<llama_sampler, decltype(&llama_sampler_free)> sampler(llama_sampler_init_greedy(), llama_sampler_free);
    if (!sampler) throw std::runtime_error("Unable to create sampler");
    auto batch = llama_batch_get_one(tokens.data(), count);
    std::string result;
    for (int i = 0; i < limit; ++i) {
        check(s);
        if (llama_decode(context.get(), batch) != 0) throw std::runtime_error("Inference decode failed or cancelled");
        check(s);
        llama_token token = llama_sampler_sample(sampler.get(), context.get(), -1);
        if (llama_vocab_is_eog(vocab, token)) break;
        char piece[256];
        int length = llama_token_to_piece(vocab, token, piece, sizeof(piece), 0, true);
        if (length < 0 || result.size() + length > 16384) throw std::runtime_error("Output size limit exceeded");
        result.append(piece, length);
        tokens[0] = token;
        batch = llama_batch_get_one(tokens.data(), 1);
    }
    check(s);
    return result;
}
}
extern "C" JNIEXPORT jlong JNICALL Java_ai_lastcore_android_LocalRuntime_nativeCreate(JNIEnv * env, jclass) {
    try {
        std::lock_guard<std::mutex> lock(registry_mutex);
        if (sessions.size() >= 4) throw std::runtime_error("Too many runtime sessions");
        jlong id = next_id.fetch_add(1);
        sessions.emplace(id, std::make_shared<Session>());
        return id;
    } catch (...) { throw_java(env, "Unable to create runtime session"); return 0; }
}
extern "C" JNIEXPORT void JNICALL Java_ai_lastcore_android_LocalRuntime_nativeCancel(JNIEnv *, jclass, jlong id) {
    std::lock_guard<std::mutex> lock(registry_mutex);
    auto it = sessions.find(id);
    if (it != sessions.end()) it->second->cancelled.store(true);
}
extern "C" JNIEXPORT void JNICALL Java_ai_lastcore_android_LocalRuntime_nativeClose(JNIEnv *, jclass, jlong id) {
    std::lock_guard<std::mutex> lock(registry_mutex);
    auto it = sessions.find(id);
    if (it != sessions.end()) { it->second->cancelled.store(true); sessions.erase(it); }
}
extern "C" JNIEXPORT jbyteArray JNICALL Java_ai_lastcore_android_LocalRuntime_nativeGenerate(
    JNIEnv * env, jclass, jlong id, jbyteArray path, jbyteArray prompt, jint limit) {
    try {
        if (limit < 1 || limit > 64) throw std::runtime_error("Token limit exceeded");
        auto session = get(id); // Shared ownership prevents close/cancel from freeing active native state.
        std::string output = generate(*session, bytes(env, path, 4096), bytes(env, prompt, 2048), limit);
        jbyteArray value = env->NewByteArray(output.size());
        if (value) env->SetByteArrayRegion(value, 0, output.size(), reinterpret_cast<const jbyte *>(output.data()));
        return value;
    } catch (const std::exception & e) { if (!env->ExceptionCheck()) throw_java(env, e.what()); }
    catch (...) { if (!env->ExceptionCheck()) throw_java(env, "Native runtime failed"); }
    return nullptr;
}
