"""
StickS3 software AEC reference + realtime (talk-over) mode (build-time patch).
ES8311 on StickS3 has no hardware loopback, so the codec wrapper keeps a copy of
everything written to the speaker and feeds it to the ESP-SR AFE as the reference
channel ("MR" format). Enables CONFIG_USE_DEVICE_AEC -> listening mode "realtime",
so the mic stays open while Joy speaks and the user can interrupt by talking.
Env: AEC_REF_DELAY  samples (24 kHz) to hold the reference back to line it up with
     the echo picked up by the mic (default 1440 = 60 ms, the I2S DMA depth).
"""
import os, json
delay = int(os.environ.get("AEC_REF_DELAY", "1440"))

# 1) ES8311 Read/Write -> protected so the StickS3 wrapper can call them
h = "main/audio/codecs/es8311_audio_codec.h"
s = open(h, encoding="utf-8").read()
old = "    virtual int Read(int16_t* dest, int samples) override;\n    virtual int Write(const int16_t* data, int samples) override;"
if "protected:\n" + old not in s:
    assert s.count(old) == 1, "es8311_audio_codec.h changed upstream"
    s = s.replace(old, "protected:\n" + old)
    open(h, "w", encoding="utf-8").write(s)

# 2) StickS3 codec wrapper with a software reference ring buffer
p = "main/boards/m5stack/stick-s3/m5stack_stick_s3.cc"
s = open(p, encoding="utf-8").read()
if "joy_aec_ref" in s:
    print("AEC patch already applied"); raise SystemExit
def rep(old, new):
    global s
    assert s.count(old) == 1, f"stick-s3 board changed upstream, cannot find: {old[:60]!r}"
    s = s.replace(old, new)
rep('#include "M5PM1.h"', '#include "M5PM1.h"\n#include <mutex>\n#include <vector>\n#include <algorithm>\n#include <esp_timer.h>')
rep("""                          mclk, bclk, ws, dout, din, pa_pin, es8311_addr, use_mclk, pa_inverted) {}""",
f"""                          mclk, bclk, ws, dout, din, pa_pin, es8311_addr, use_mclk, pa_inverted) {{
        // patched (joy_aec_ref): mic + software reference -> AFE "MR"
        input_reference_ = true;
        input_channels_ = 2;
        ref_ring_.assign(kRefRingSize, 0);
    }}

protected:
    static constexpr int kRefRingSize = 24000;   // 1 s at 24 kHz
    static constexpr int kRefDelay = {delay};
    std::vector<int16_t> ref_ring_;
    std::vector<int16_t> mic_buf_;
    std::mutex ref_mutex_;
    int ref_head_ = 0;
    int ref_count_ = 0;
    int64_t last_write_us_ = 0;

    int Read(int16_t* dest, int samples) override {{
        int mono = samples / 2;
        if ((int)mic_buf_.size() < mono) mic_buf_.resize(mono);
        Es8311AudioCodec::Read(mic_buf_.data(), mono);
        std::lock_guard<std::mutex> lock(ref_mutex_);
        bool idle = (esp_timer_get_time() - last_write_us_) > 300000;
        if (idle) {{ ref_head_ = 0; ref_count_ = 0; }}
        int avail = std::max(0, ref_count_ - kRefDelay);
        int take = std::min(avail, mono);
        int pad = mono - take;
        for (int i = 0; i < mono; i++) {{
            int16_t r = 0;
            if (i >= pad) {{
                r = ref_ring_[ref_head_];
                ref_head_ = (ref_head_ + 1) % kRefRingSize;
                ref_count_--;
            }}
            dest[2 * i] = mic_buf_[i];
            dest[2 * i + 1] = r;
        }}
        return samples;
    }}

    int Write(const int16_t* data, int samples) override {{
        {{
            std::lock_guard<std::mutex> lock(ref_mutex_);
            for (int i = 0; i < samples; i++) {{
                if (ref_count_ == kRefRingSize) {{
                    ref_head_ = (ref_head_ + 1) % kRefRingSize;
                    ref_count_--;
                }}
                ref_ring_[(ref_head_ + ref_count_) % kRefRingSize] = data[i];
                ref_count_++;
            }}
            last_write_us_ = esp_timer_get_time();
        }}
        return Es8311AudioCodec::Write(data, samples);
    }}

public:""")
open(p, "w", encoding="utf-8").write(s)

# 3) allow device-side AEC for this board and switch it on
k = "main/Kconfig.projbuild"
ks = open(k, encoding="utf-8").read()
a = "|| BOARD_TYPE_ATK_DNESP32S3_BOX3)"
if "BOARD_TYPE_M5STACK_STICK_S3)" not in ks:
    assert ks.count(a) == 1, "Kconfig USE_DEVICE_AEC list changed upstream"
    ks = ks.replace(a, "|| BOARD_TYPE_ATK_DNESP32S3_BOX3 || BOARD_TYPE_M5STACK_STICK_S3)")
    open(k, "w", encoding="utf-8").write(ks)
c = "main/boards/m5stack/stick-s3/config.json"
cj = json.load(open(c))
app = cj["builds"][0]["sdkconfig_append"]
if "CONFIG_USE_DEVICE_AEC=y" not in app:
    app.append("CONFIG_USE_DEVICE_AEC=y")
json.dump(cj, open(c, "w"), indent=4)
print(f"StickS3 software AEC applied (ref delay {delay} samples), realtime mode on")
