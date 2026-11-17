# 编译时给 xiaozhi 固件加“设备端提醒”：
#   MCP 工具 self.reminder.add / self.reminder.list / self.reminder.cancel
#   提醒存在 NVS（重启不丢），每 15 秒检查一次，到点且设备空闲时用 "[device_call]..." 让服务器播报
import sys, re
p = "main/mcp_server.cc"
s = open(p, encoding="utf-8").read()
if "JOY_REMINDERS" in s:
    print("reminders already patched"); sys.exit(0)

HELPERS = r'''
// ===== JOY_REMINDERS begin =====
#include <esp_timer.h>
#include <ctime>
#include <mutex>
#include <vector>
#include <string>
namespace joy_reminders {
struct Item { int64_t due; int32_t repeat; std::string msg; };
static std::mutex mu;
static std::vector<Item> items;
static esp_timer_handle_t timer = nullptr;
static const char* RTAG = "Reminders";
static const int kMax = 20;

static std::string Clean(const std::string& in) {
    std::string out;
    for (char c : in) {
        if (c == '"' || c == '\\' || c == '|') continue;
        if (c == '\n' || c == '\r' || c == '\t') { out += ' '; continue; }
        out += c;
    }
    if (out.size() > 160) out.resize(160);
    return out;
}
static void Save() {  // caller holds mu
    std::string data;
    for (auto& it : items) data += std::to_string((long long)it.due) + "|" + std::to_string(it.repeat) + "|" + it.msg + "\n";
    Settings st("reminders", true);
    st.SetString("list", data);
}
static void Load() {
    Settings st("reminders", false);
    std::string data = st.GetString("list", "");
    items.clear();
    size_t pos = 0;
    while (pos < data.size()) {
        size_t nl = data.find('\n', pos); if (nl == std::string::npos) nl = data.size();
        std::string line = data.substr(pos, nl - pos); pos = nl + 1;
        size_t a = line.find('|'), b = line.find('|', a == std::string::npos ? 0 : a + 1);
        if (a == std::string::npos || b == std::string::npos) continue;
        items.push_back({(int64_t)atoll(line.substr(0, a).c_str()), (int32_t)atoi(line.substr(a + 1, b - a - 1).c_str()), line.substr(b + 1)});
    }
}
static bool TimeValid() { return time(nullptr) > 1735689600; }  // 2025-01-01 之后才算已同步
static void Tick(void*) {
    if (!TimeValid()) return;
    int64_t now = time(nullptr);
    std::string to_say;
    {
        std::lock_guard<std::mutex> lk(mu);
        bool changed = false;
        for (size_t i = 0; i < items.size();) {
            auto& it = items[i];
            if (it.due > now) { ++i; continue; }
            if (now - it.due > 30 * 60) {  // 超过 30 分钟没能播报：重复的顺延，一次性的丢弃
                if (it.repeat > 0) { while (it.due <= now) it.due += it.repeat; ++i; }
                else items.erase(items.begin() + i);
                changed = true; continue;
            }
            if (to_say.empty() && Application::GetInstance().GetDeviceState() == kDeviceStateIdle) {
                to_say = it.msg;
                if (it.repeat > 0) { while (it.due <= now) it.due += it.repeat; ++i; }
                else items.erase(items.begin() + i);
                changed = true; continue;
            }
            ++i;  // 设备忙，下次再试
        }
        if (changed) Save();
    }
    if (!to_say.empty()) {
        ESP_LOGI(RTAG, "Reminder due: %s", to_say.c_str());
        Application::GetInstance().WakeWordInvoke("[device_call]" + to_say);
    }
}
static void Init() {
    std::lock_guard<std::mutex> lk(mu);
    if (timer) return;
    Load();
    esp_timer_create_args_t args = {};
    args.callback = &Tick; args.name = "joy_reminders";
    esp_timer_create(&args, &timer);
    esp_timer_start_periodic(timer, 15ULL * 1000 * 1000);
}
}  // namespace joy_reminders
// ===== JOY_REMINDERS end =====
'''

TOOLS = r'''
    // JOY_REMINDERS tools
    joy_reminders::Init();
    AddTool("self.reminder.add",
            "Set a reminder on this device. delay_seconds is how many seconds from now it should go off. "
            "message is the exact sentence Joy will say out loud when it is due. repeat_hours: 0 for a one-off, 24 for every day.",
            PropertyList({Property("delay_seconds", kPropertyTypeInteger, 10, 31 * 24 * 3600),
                          Property("message", kPropertyTypeString),
                          Property("repeat_hours", kPropertyTypeInteger, 0, 0, 24 * 7)}),
            [](const PropertyList& properties) -> ReturnValue {
                if (!joy_reminders::TimeValid()) return std::string("{\"ok\":false,\"error\":\"clock not set\"}");
                std::lock_guard<std::mutex> lk(joy_reminders::mu);
                if ((int)joy_reminders::items.size() >= joy_reminders::kMax) return std::string("{\"ok\":false,\"error\":\"too many reminders\"}");
                int64_t due = time(nullptr) + properties["delay_seconds"].value<int>();
                int32_t rep = properties["repeat_hours"].value<int>() * 3600;
                joy_reminders::items.push_back({due, rep, joy_reminders::Clean(properties["message"].value<std::string>())});
                joy_reminders::Save();
                return std::string("{\"ok\":true,\"count\":") + std::to_string(joy_reminders::items.size()) + "}";
            });
    AddTool("self.reminder.list",
            "List reminders on this device. Each has seconds_from_now, repeat_hours and message.",
            PropertyList(),
            [](const PropertyList& properties) -> ReturnValue {
                std::lock_guard<std::mutex> lk(joy_reminders::mu);
                int64_t now = time(nullptr);
                std::string out = "[";
                for (size_t i = 0; i < joy_reminders::items.size(); ++i) {
                    auto& it = joy_reminders::items[i];
                    if (i) out += ",";
                    out += "{\"index\":" + std::to_string(i) + ",\"seconds_from_now\":" + std::to_string((long long)(it.due - now)) +
                           ",\"repeat_hours\":" + std::to_string(it.repeat / 3600) + ",\"message\":\"" + it.msg + "\"}";
                }
                return out + "]";
            });
    AddTool("self.reminder.cancel",
            "Cancel a reminder by index (from self.reminder.list), or -1 to cancel all reminders.",
            PropertyList({Property("index", kPropertyTypeInteger, -1, 100)}),
            [](const PropertyList& properties) -> ReturnValue {
                std::lock_guard<std::mutex> lk(joy_reminders::mu);
                int idx = properties["index"].value<int>();
                if (idx < 0) joy_reminders::items.clear();
                else if (idx < (int)joy_reminders::items.size()) joy_reminders::items.erase(joy_reminders::items.begin() + idx);
                else return std::string("{\"ok\":false,\"error\":\"no such reminder\"}");
                joy_reminders::Save();
                return std::string("{\"ok\":true,\"count\":") + std::to_string(joy_reminders::items.size()) + "}";
            });
'''

inc = '#include "settings.h"\n'
anchor = '    AddTool("self.audio_speaker.set_volume",'
assert inc in s and anchor in s, "mcp_server.cc changed upstream"
assert '#include "application.h"' in s
s = s.replace(inc, inc + HELPERS, 1)
s = s.replace(anchor, TOOLS + "\n" + anchor, 1)
open(p, "w", encoding="utf-8").write(s)
print("reminders patched")
