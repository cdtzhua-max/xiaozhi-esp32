"""
Joy screen layout (build-time patch, multiline chat mode):
- subtitles never cover the top status bar (max height = screen - top bar), long text scrolls
- left-aligned text with a little line spacing; what the user said in blue, Joy in the theme colour
- a sentence that doesn't fit scrolls up slowly while it is spoken
"""
p = "main/display/lcd_display.cc"
s = open(p, encoding="utf-8").read()
if "joy-layout" in s:
    print("display patch already applied"); raise SystemExit
def rep(old, new):
    global s
    assert s.count(old) == 1, f"lcd_display.cc changed upstream, cannot find: {old[:60]!r}"
    s = s.replace(old, new)

rep("""    lv_obj_set_style_text_align(chat_message_label_, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_set_style_text_color(chat_message_label_, lvgl_theme->text_color(), 0);
    lv_obj_align(chat_message_label_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);  // Hide until there is content""",
"""    // joy-layout: left aligned, airy lines, never over the top bar, scroll if too long
    lv_obj_set_style_text_align(chat_message_label_, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_text_line_space(chat_message_label_, 3, 0);
    lv_obj_set_style_text_color(chat_message_label_, lvgl_theme->text_color(), 0);
    lv_obj_align(chat_message_label_, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_obj_set_style_pad_all(bottom_bar_, lvgl_theme->spacing(3), 0);
    lv_obj_set_style_pad_bottom(bottom_bar_, lvgl_theme->spacing(6), 0);  // keep last line off the rounded corner
    lv_obj_set_style_pad_left(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_pad_right(bottom_bar_, lvgl_theme->spacing(4), 0);
    lv_obj_set_style_bg_opa(bottom_bar_, LV_OPA_80, 0);
    lv_obj_update_layout(top_bar_);
    lv_obj_set_style_max_height(bottom_bar_, height_ - lv_obj_get_height(top_bar_) - 2, 0);
    lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(bottom_bar_, LV_DIR_VER);
    lv_obj_add_flag(bottom_bar_, LV_OBJ_FLAG_HIDDEN);  // Hide until there is content""")

rep("""    if (bottom_bar_ != nullptr) {
        lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, 0);
    }
#endif
}""", """    if (bottom_bar_ != nullptr) {
        // joy-layout: user's words in light blue, Joy's in the normal text colour; start from the top
        bool is_user = role != nullptr && strcmp(role, "user") == 0;
        auto* th = static_cast<LvglTheme*>(current_theme_);
        bool light_bg = lv_color_luminance(th->background_color()) > 128;
        lv_color_t user_color = light_bg ? lv_palette_darken(LV_PALETTE_BLUE, 2)
                                         : lv_palette_lighten(LV_PALETTE_LIGHT_BLUE, 2);
        lv_obj_set_style_text_color(chat_message_label_, is_user ? user_color : th->text_color(), 0);
        lv_obj_align(bottom_bar_, LV_ALIGN_BOTTOM_MID, 0, 0);
        lv_anim_delete(bottom_bar_, nullptr);
        lv_obj_update_layout(bottom_bar_);
        lv_obj_scroll_to_y(bottom_bar_, 0, LV_ANIM_OFF);
        // joy-layout: if the sentence doesn't fit, scroll it up slowly while it's being spoken
        int32_t overflow = lv_obj_get_scroll_bottom(bottom_bar_);
        if (overflow > 0) {
            lv_anim_t a;
            lv_anim_init(&a);
            lv_anim_set_var(&a, bottom_bar_);
            lv_anim_set_values(&a, 0, overflow);
            lv_anim_set_delay(&a, 1800);
            lv_anim_set_duration(&a, (uint32_t)overflow * 60);
            lv_anim_set_exec_cb(&a, [](void* obj, int32_t v) {
                lv_obj_scroll_to_y(static_cast<lv_obj_t*>(obj), v, LV_ANIM_OFF);
            });
            lv_anim_start(&a);
        }
    }
#endif
}""")
# dark theme (black background, white text) by default; ignore an old stored "light"
rep('std::string theme_name = settings.GetString("theme", "light");',
    'std::string theme_name = "dark";  // joy-layout: always dark (black background, white text)')
open(p, "w", encoding="utf-8").write(s)
print("Joy screen layout applied")
