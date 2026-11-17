"""
Joy IR remote for M5StickS3 (build-time patch).
Adds MCP tools:
  self.ir.learn(name)   start learning one remote button (async; poll self.ir.status)
  self.ir.status        learning progress: idle / waiting / capturing / done / timeout / error
  self.ir.send(name, repeat)  send a learned button
  self.ir.list          learned button names
  self.ir.delete(name)  delete one button, or "all"
IR TX = GPIO46, IR RX = GPIO42 (RX needs the speaker amp off: PM1 G3 low while learning).
Codes are stored raw (microsecond mark/space list) in NVS namespace "joy_ir".
"""
p = "main/boards/m5stack/stick-s3/m5stack_stick_s3.cc"
s = open(p, encoding="utf-8").read()
if "joy_ir" in s:
    print("IR patch already applied"); raise SystemExit

def rep(old, new):
    global s
    assert s.count(old) == 1, f"stick-s3 board changed upstream, cannot find: {old[:60]!r}"
    s = s.replace(old, new)

rep('#include "M5PM1.h"', '''#include "M5PM1.h"
#include "mcp_server.h"
#include <driver/rmt_tx.h>
#include <driver/rmt_rx.h>
#include <freertos/queue.h>
#include <esp_timer.h>
#include <vector>
#include <atomic>
#include <algorithm>''')

NS = r'''
// ===== Joy IR remote (patched) =====
namespace joy_ir {
static const char* kTag = "JoyIR";
static constexpr gpio_num_t kTx = GPIO_NUM_46;
static constexpr gpio_num_t kRx = GPIO_NUM_42;
static constexpr uint32_t kRes = 1000000;      // 1 tick = 1 us
static constexpr size_t kRxSymbols = 96;       // 2 RMT blocks on ESP32-S3 (48 each)

// 0 idle, 1 waiting for Joy to stop talking, 2 capturing, 3 done, 4 timeout, 5 error
static std::atomic<int> g_state{0};
static std::atomic<int> g_count{0};
static std::string g_name;

static std::string Key(const std::string& in) {
    std::string k;
    for (char c : in) {
        if (c >= 'A' && c <= 'Z') c = c - 'A' + 'a';
        if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_') k += c;
        else if (c == ' ' || c == '-') k += '_';
        if (k.size() >= 15) break;
    }
    return k;
}

static std::vector<std::string> Names() {
    Settings st("joy_ir");
    std::string all = st.GetString("_names");
    std::vector<std::string> v;
    size_t pos = 0;
    while (pos < all.size()) {
        size_t c = all.find(',', pos);
        if (c == std::string::npos) c = all.size();
        if (c > pos) v.push_back(all.substr(pos, c - pos));
        pos = c + 1;
    }
    return v;
}

static void SaveNames(const std::vector<std::string>& v) {
    std::string all;
    for (auto& n : v) { if (!all.empty()) all += ","; all += n; }
    Settings st("joy_ir", true);
    st.SetString("_names", all);
}

static void Save(const std::string& key, const std::vector<uint16_t>& d) {
    std::string enc;
    for (size_t i = 0; i < d.size(); i++) { if (i) enc += ","; enc += std::to_string(d[i]); }
    {
        Settings st("joy_ir", true);
        st.SetString(key, enc);
    }
    auto v = Names();
    if (std::find(v.begin(), v.end(), key) == v.end()) { v.push_back(key); SaveNames(v); }
}

static std::vector<uint16_t> Load(const std::string& key) {
    Settings st("joy_ir");
    std::string enc = st.GetString(key);
    std::vector<uint16_t> d;
    size_t pos = 0;
    while (pos < enc.size()) {
        size_t c = enc.find(',', pos);
        if (c == std::string::npos) c = enc.size();
        if (c > pos) d.push_back((uint16_t)atoi(enc.substr(pos, c - pos).c_str()));
        pos = c + 1;
    }
    return d;
}

static bool OnRxDone(rmt_channel_handle_t, const rmt_rx_done_event_data_t* ed, void* ctx) {
    BaseType_t hp = pdFALSE;
    xQueueSendFromISR(static_cast<QueueHandle_t>(ctx), ed, &hp);
    return hp == pdTRUE;
}

static std::vector<uint16_t> Capture(int timeout_ms) {
    std::vector<uint16_t> out;
    rmt_channel_handle_t rx = nullptr;
    rmt_rx_channel_config_t cfg = {};
    cfg.gpio_num = kRx;
    cfg.clk_src = RMT_CLK_SRC_DEFAULT;
    cfg.resolution_hz = kRes;
    cfg.mem_block_symbols = kRxSymbols;
    if (rmt_new_rx_channel(&cfg, &rx) != ESP_OK) {
        ESP_LOGE(kTag, "rmt rx channel failed");
        return out;
    }
    QueueHandle_t q = xQueueCreate(1, sizeof(rmt_rx_done_event_data_t));
    rmt_rx_event_callbacks_t cbs = {};
    cbs.on_recv_done = OnRxDone;
    rmt_rx_register_event_callbacks(rx, &cbs, q);
    rmt_enable(rx);
    std::vector<rmt_symbol_word_t> buf(kRxSymbols);
    rmt_receive_config_t rc = {};
    rc.signal_range_min_ns = 1250;
    rc.signal_range_max_ns = 12000000;
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (esp_timer_get_time() < deadline) {
        if (rmt_receive(rx, buf.data(), buf.size() * sizeof(rmt_symbol_word_t), &rc) != ESP_OK) break;
        int left_ms = (int)((deadline - esp_timer_get_time()) / 1000);
        rmt_rx_done_event_data_t ev = {};
        if (left_ms <= 0 || xQueueReceive(q, &ev, pdMS_TO_TICKS(left_ms)) != pdTRUE) break;
        if (ev.num_symbols < 8) continue;  // noise, keep listening
        for (size_t i = 0; i < ev.num_symbols; i++) {
            if (ev.received_symbols[i].duration0) out.push_back(ev.received_symbols[i].duration0);
            if (ev.received_symbols[i].duration1) out.push_back(ev.received_symbols[i].duration1);
        }
        break;
    }
    rmt_disable(rx);
    rmt_del_channel(rx);
    vQueueDelete(q);
    ESP_LOGI(kTag, "captured %d durations", (int)out.size());
    return out;
}

static bool Send(const std::vector<uint16_t>& d, int repeat) {
    if (d.size() < 2) return false;
    rmt_channel_handle_t tx = nullptr;
    rmt_tx_channel_config_t tc = {};
    tc.gpio_num = kTx;
    tc.clk_src = RMT_CLK_SRC_DEFAULT;
    tc.resolution_hz = kRes;
    tc.mem_block_symbols = 48;
    tc.trans_queue_depth = 2;
    if (rmt_new_tx_channel(&tc, &tx) != ESP_OK) {
        ESP_LOGE(kTag, "rmt tx channel failed");
        return false;
    }
    rmt_carrier_config_t cc = {};
    cc.frequency_hz = 38000;
    cc.duty_cycle = 0.33f;
    rmt_apply_carrier(tx, &cc);
    rmt_encoder_handle_t enc = nullptr;
    rmt_copy_encoder_config_t ec = {};
    rmt_new_copy_encoder(&ec, &enc);
    std::vector<rmt_symbol_word_t> sym((d.size() + 1) / 2);
    for (size_t i = 0; i < sym.size(); i++) {
        uint32_t m = std::clamp<uint32_t>(d[2 * i], 1, 32767);
        uint32_t sp = (2 * i + 1 < d.size()) ? std::clamp<uint32_t>(d[2 * i + 1], 1, 32767) : 1000;
        sym[i].level0 = 1;
        sym[i].duration0 = m;
        sym[i].level1 = 0;
        sym[i].duration1 = sp;
    }
    rmt_enable(tx);
    rmt_transmit_config_t tcfg = {};
    bool ok = true;
    for (int r = 0; r < repeat; r++) {
        if (rmt_transmit(tx, enc, sym.data(), sym.size() * sizeof(rmt_symbol_word_t), &tcfg) != ESP_OK) { ok = false; break; }
        rmt_tx_wait_all_done(tx, 1000);
        vTaskDelay(pdMS_TO_TICKS(60));
    }
    rmt_disable(tx);
    rmt_del_encoder(enc);
    rmt_del_channel(tx);
    return ok;
}

struct LearnArgs { M5PM1* pmic; std::string key; };

static void LearnTask(void* p) {
    auto* a = static_cast<LearnArgs*>(p);
    auto& app = Application::GetInstance();
    // let Joy finish the spoken instruction first (speaker amp must be off while receiving)
    for (int i = 0; i < 40 && app.GetDeviceState() != kDeviceStateSpeaking; i++) vTaskDelay(pdMS_TO_TICKS(100));
    for (int i = 0; i < 200 && app.GetDeviceState() == kDeviceStateSpeaking; i++) vTaskDelay(pdMS_TO_TICKS(100));
    vTaskDelay(pdMS_TO_TICKS(400));
    g_state = 2;
    if (a->pmic) a->pmic->digitalWrite(3, LOW);
    vTaskDelay(pdMS_TO_TICKS(50));
    auto d = Capture(12000);
    if (a->pmic) a->pmic->digitalWrite(3, HIGH);
    if (d.size() >= 8) {
        Save(a->key, d);
        g_count = (int)d.size();
        g_state = 3;
    } else {
        g_state = d.empty() ? 4 : 5;
    }
    delete a;
    vTaskDelete(nullptr);
}

static void RegisterTools(M5PM1* pmic) {
    auto& mcp = McpServer::GetInstance();
    mcp.AddTool("self.ir.learn",
        "Start learning one TV remote button. The user points their remote at the device and presses the button within about 12 seconds. "
        "name is a short button name like power, vol_up, vol_down, mute, ch_up, ch_down, input. Poll self.ir.status for the result.",
        PropertyList({Property("name", kPropertyTypeString)}),
        [pmic](const PropertyList& props) -> ToolResult {
            std::string key = Key(props["name"].value<std::string>());
            if (key.empty() || key == "_names") return std::string("{\"ok\":false,\"error\":\"bad name\"}");
            int st = g_state.load();
            if (st == 1 || st == 2) return std::string("{\"ok\":false,\"error\":\"busy\"}");
            g_name = key;
            g_count = 0;
            g_state = 1;
            auto* a = new LearnArgs{pmic, key};
            if (xTaskCreate(LearnTask, "joy_ir_learn", 4096, a, 4, nullptr) != pdPASS) {
                delete a; g_state = 5;
                return std::string("{\"ok\":false,\"error\":\"task\"}");
            }
            return std::string("{\"ok\":true,\"started\":true}");
        });
    mcp.AddTool("self.ir.status", "Progress of the current IR learning.",
        PropertyList(),
        [](const PropertyList&) -> ToolResult {
            static const char* names[] = {"idle", "waiting", "capturing", "done", "timeout", "error"};
            int st = g_state.load();
            return std::string("{\"state\":\"") + names[st] + "\",\"name\":\"" + g_name + "\",\"count\":" + std::to_string(g_count.load()) + "}";
        });
    mcp.AddTool("self.ir.send", "Send a learned TV remote button. repeat = how many presses (1-10).",
        PropertyList({Property("name", kPropertyTypeString), Property("repeat", kPropertyTypeInteger, 1, 1, 10)}),
        [](const PropertyList& props) -> ToolResult {
            std::string key = Key(props["name"].value<std::string>());
            auto d = Load(key);
            if (d.size() < 8) return std::string("{\"ok\":false,\"error\":\"not learned\"}");
            bool ok = Send(d, props["repeat"].value<int>());
            return std::string(ok ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"send failed\"}");
        });
    mcp.AddTool("self.ir.list", "List learned TV remote buttons.",
        PropertyList(),
        [](const PropertyList&) -> ToolResult {
            std::string out = "[";
            auto v = Names();
            for (size_t i = 0; i < v.size(); i++) { if (i) out += ","; out += "\"" + v[i] + "\""; }
            return out + "]";
        });
    mcp.AddTool("self.ir.delete", "Delete a learned button by name, or \"all\".",
        PropertyList({Property("name", kPropertyTypeString)}),
        [](const PropertyList& props) -> ToolResult {
            std::string key = Key(props["name"].value<std::string>());
            auto v = Names();
            Settings st("joy_ir", true);
            if (key == "all") {
                for (auto& n : v) st.SetString(n, "");
                st.SetString("_names", "");
                return std::string("{\"ok\":true}");
            }
            auto it = std::find(v.begin(), v.end(), key);
            if (it == v.end()) return std::string("{\"ok\":false,\"error\":\"not found\"}");
            st.SetString(key, "");
            v.erase(it);
            std::string all;
            for (auto& n : v) { if (!all.empty()) all += ","; all += n; }
            st.SetString("_names", all);
            return std::string("{\"ok\":true}");
        });
    ESP_LOGI(kTag, "IR tools registered");
}
}  // namespace joy_ir

// reduce the output volume to 60%'''
rep("\n// reduce the output volume to 60%", NS)
rep("""        InitializeButtons();
        GetBacklight()->SetBrightness(60);""", """        InitializeButtons();
        joy_ir::RegisterTools(pmic_);  // patched: Joy IR remote
        GetBacklight()->SetBrightness(60);""")
open(p, "w", encoding="utf-8").write(s)

c = "main/CMakeLists.txt"
cs = open(c, encoding="utf-8").read()
old = """        m5stack__m5pm1
    )"""
assert cs.count(old) == 1, "CMakeLists stick-s3 block changed upstream"
cs = cs.replace(old, """        m5stack__m5pm1
        esp_driver_rmt
    )""")
open(c, "w", encoding="utf-8").write(cs)
print("Joy IR remote patch applied (TX G46, RX G42)")
