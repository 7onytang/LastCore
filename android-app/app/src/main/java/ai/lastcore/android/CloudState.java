package ai.lastcore.android;

import org.json.JSONObject;

/** Implementations retain a process lock and private atomic state for the entire worker lifetime. */
interface CloudState extends AutoCloseable {
    JSONObject load() throws Exception;
    void save(JSONObject state) throws Exception;
    @Override void close() throws Exception;
}
