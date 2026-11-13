"""The page, in a real browser, against the demo library.

Needs Playwright with Chromium (pip install playwright; playwright install
chromium). Skipped when it is not there.
"""

from __future__ import annotations

import re

import pytest

from censarr import auth, config, db

pytestmark = pytest.mark.ui
sync_api = pytest.importorskip("playwright.sync_api")

VIEWS = ["home", "shows", "movies", "upcoming", "queue", "cleaned", "settings"]
DESKTOP = {"width": 1366, "height": 900}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def demo():
    from demo import Demo
    from censarr import main
    saved = {(mod, name): getattr(mod, name) for mod, name in (
        (config, "CONFIG_DIR"), (config, "CONFIG_FILE"), (db, "DB_PATH"),
        (db, "_local"), (db, "_schema_ready"), (main, "CACHE_DIR"))}
    d = Demo()
    d.up()
    yield d
    d.stop()
    for (mod, name), value in saved.items():
        setattr(mod, name, value)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright: {exc}")
        yield b
        b.close()


class Page:
    """A page plus everything that went wrong on it."""

    def __init__(self, browser, demo, viewport, scheme="dark"):
        self.ctx = browser.new_context(viewport=viewport, color_scheme=scheme)
        self.page = self.ctx.new_page()
        self.errors: list[str] = []
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.url = demo.url

    def go(self, view: str):
        self.page.goto(f"{self.url}/#/{view}")
        self.page.wait_for_load_state("networkidle")
        return self.page

    def close(self):
        self.ctx.close()


@pytest.fixture
def desk(browser, demo):
    demo.configure()
    p = Page(browser, demo, DESKTOP)
    yield p
    p.close()


@pytest.fixture
def phone(browser, demo):
    demo.configure()
    p = Page(browser, demo, PHONE)
    yield p
    p.close()


# ---------------------------------------------------------------- every view

@pytest.mark.parametrize("size", ["desk", "phone"])
def test_every_view_renders_cleanly(request, size):
    p = request.getfixturevalue(size)
    for view in VIEWS:
        page = p.go(view)
        heading = page.locator(f"#view-{view} h1")
        assert heading.is_visible(), view
        overflow = page.evaluate("document.scrollingElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"{view} scrolls sideways by {overflow}px at {size}"
        # Nothing left in a loading state once the network is quiet.
        page.wait_for_timeout(300)
        assert page.locator(f"#view-{view} .skeleton").count() == 0, f"{view} still loading"
        assert page.locator(f"#view-{view} .alert.error").count() == 0, f"{view} shows an error"
    assert p.errors == []


def test_phone_nav_puts_the_rest_behind_more(phone):
    page = phone.go("home")
    assert page.locator(".sidebar [data-nav=settings]").is_hidden()
    page.get_by_role("button", name="More").click()
    page.locator("#more-sheet").get_by_role("link", name="Settings").click()
    page.locator("#view-settings").wait_for()
    assert not page.locator("#more-sheet").is_visible()


def test_touch_targets_are_big_enough_on_a_phone(phone):
    page = phone.go("queue")
    small = page.evaluate("""() => [...document.querySelectorAll(
        '#view-queue button:not([disabled]), .sidebar a, .sidebar button')]
      .filter(el => el.offsetParent)
      .map(el => el.getBoundingClientRect())
      .filter(r => r.width < 32 || r.height < 32).length""")
    assert small == 0


# ---------------------------------------------------------------- keyboard

def test_keyboard_opens_a_show_and_escape_gives_focus_back(desk):
    page = desk.go("shows")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.className") == "skip"
    target = page.get_by_role("button", name=re.compile("^The Quiet Harbour"))
    target.focus()
    page.keyboard.press("Enter")
    sheet = page.locator("#sheet")
    sheet.wait_for(state="visible")
    assert page.locator("#sheet-title").inner_text() == "The Quiet Harbour"
    page.locator(".season-toggle").first.wait_for()
    # Focus is inside the sheet, and a season opens from the keyboard.
    toggle = page.locator(".season-toggle").first
    toggle.focus()
    page.keyboard.press("Enter")
    assert toggle.get_attribute("aria-expanded") == "true"
    assert page.locator("#season-1 .row").count() == 8
    page.keyboard.press("Escape")
    sheet.wait_for(state="hidden")
    assert page.evaluate("document.activeElement.textContent").strip() == "The Quiet Harbour"


def test_overflow_menu_works_from_the_keyboard(desk):
    page = desk.go("shows")
    page.get_by_role("button", name=re.compile("^The Quiet Harbour")).click()
    page.locator(".season-toggle").first.click()
    summary = page.locator("details.menu summary").first
    summary.focus()
    page.keyboard.press("Enter")
    assert page.locator("details.menu[open]").count() == 1
    page.keyboard.press("Escape")
    assert page.locator("details.menu[open]").count() == 0


def test_queue_reorder_keeps_keyboard_focus(desk, demo):
    page = desk.go("queue")
    first = page.locator("#job-list .row").first
    title = first.locator(".title .muted").inner_text()
    down = first.get_by_role("button", name=re.compile("Move .* down"))
    down.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(600)
    assert page.locator("#job-list .row").nth(1).locator(".title .muted").inner_text() == title
    focused = page.evaluate("document.activeElement.getAttribute('aria-label')")
    assert focused and "down" in focused and title.split(" · ")[0] in focused
    # Put it back, for the other tests.
    page.evaluate("document.activeElement.parentElement.querySelector('[data-where=up]').click()")
    page.wait_for_timeout(400)


def test_the_film_switch_turns_on_and_stays_on(desk):
    page = desk.go("movies")
    switch = page.get_by_role("switch", name="Censor new films automatically")
    assert not switch.is_checked()
    page.locator("#view-movies label.switch").click()
    page.locator(".toast", has_text="New films will be censored").wait_for()
    assert db.is_monitored("films", "new")
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.get_by_role("switch", name="Censor new films automatically").is_checked()
    queue = desk.go("queue")
    assert "new films censored as they arrive" in queue.locator("#monitor-status").inner_text().lower()
    page = desk.go("movies")
    page.locator("#view-movies label.switch").click()
    page.locator(".toast", has_text="no longer censored").wait_for()
    assert not db.is_monitored("films", "new")
    assert desk.errors == []


def test_selecting_several_jobs(desk):
    page = desk.go("queue")
    boxes = page.locator("#job-list .pick")
    boxes.nth(0).check()
    boxes.nth(2).click(modifiers=["Shift"])
    assert page.locator("#selection-count").inner_text().startswith("3 selected")
    page.get_by_role("button", name="Clear selection").click()
    page.locator("#selection-bar").wait_for(state="hidden")
    assert page.locator("#job-list .pick:checked").count() == 0


# ---------------------------------------------------------------- settings

def test_every_setting_saves_and_comes_back(desk, demo, tmp_path):
    page = desk.go("settings")
    form = page.locator("#settings-form")
    form.wait_for()
    media_dir = str(tmp_path)

    # Library: the Sonarr address, then switch to Jellyfin, which hides it
    # and moves the Jellyfin fields into this section.
    page.fill("[name='sonarr.url']", "http://sonarr.test:8989")
    assert page.locator("[name='radarr.url']").count() == 0
    page.locator("input[name=library_source][value=jellyfin]").check()
    assert page.locator("[name='sonarr.url']").is_hidden()
    page.fill("[name=jellyfin_url]", "http://jellyfin.test:8096")
    page.fill("[name=jellyfin_api_key]", "jf-key")

    # Words.
    toggles = page.locator("#categories input")
    toggles.nth(1).uncheck(force=True)
    for host, word in (("custom_words", "muppet"), ("allow_words", "dickens"), ("check_in_context", "prick")):
        box = page.locator(f"[data-tags={host}] input")
        box.fill(word)
        box.press("Enter")
    page.locator("[data-tags=check_in_context] .tag", has_text="jesus").get_by_role("button").click()

    # Listening.
    page.get_by_label("Use my own Whisper server").check()
    page.fill("[name=asr_url]", "http://whisper.test:8000")
    page.fill("[name=asr_remote_model]", "Systran/faster-whisper-small.en")
    page.fill("[name=asr_api_key]", "asr-key")
    page.locator("[name=trim_silence]").check(force=True)

    # Muting.
    page.fill("[name=pad_start]", "0.2")
    page.fill("[name=pad_end]", "0.3")
    page.fill("[name=fade]", "0.05")
    page.fill("[name=track_title]", "Family Friendly")

    # Second opinion: another Whisper server, and a model.
    page.fill("[name=recheck_url]", "http://speaches.test:8000")
    page.fill("[name=recheck_model]", "Systran/faster-whisper-large-v3")
    page.fill("[name=recheck_api_key]", "again-key")
    assert "muted in a file with none" in page.locator("#context-note").inner_text()
    page.fill("[name=judge_url]", "http://ollama.test:11434")
    assert "sent to the model below" in page.locator("#context-note").inner_text()
    page.fill("[name=judge_model]", "qwen3.5:4b")
    page.select_option("[name=judge_threads]", "4")

    # Media server: Plex, with its own fields, and a wait policy.
    page.locator("input[name=media_server][value=plex]").check()
    page.fill("[name=plex_url]", "http://plex.test:32400")
    page.fill("[name=plex_token]", "plex-token")
    page.select_option("[name=hold_policy]", "playing")

    # Advanced.
    page.fill("[name=bitrate_surround]", "448k")
    page.fill("[name=bitrate_stereo]", "160k")
    page.fill("[name=ffmpeg_threads]", "3")
    page.select_option("[name=compute_type]", "int8")
    page.fill("[name=judge_keep_alive]", "1m")
    page.locator("[name=keep_backup]").check(force=True)

    assert page.locator("#save-bar").is_visible()
    page.locator("#settings-save").click()
    page.locator("#save-bar").wait_for(state="hidden")

    s = config.load()
    assert s.library_source == "jellyfin"
    assert (s.jellyfin_url, s.jellyfin_api_key) == ("http://jellyfin.test:8096", "jf-key")
    assert s.sonarr.url == "http://sonarr.test:8989" and s.sonarr.api_key == "demo-sonarr-key"
    assert "mild" not in s.categories and "strong" in s.categories
    assert s.custom_words == ["muppet"] and s.allow_words == ["dickens"]
    assert s.check_in_context == ["cock", "christ", "prick"]
    assert (s.asr_backend, s.asr_url, s.asr_remote_model, s.asr_api_key) == (
        "remote", "http://whisper.test:8000", "Systran/faster-whisper-small.en", "asr-key")
    assert s.trim_silence is True
    assert (s.recheck_url, s.recheck_model, s.recheck_api_key) == (
        "http://speaches.test:8000", "Systran/faster-whisper-large-v3", "again-key")
    assert (s.pad_start, s.pad_end, s.fade, s.track_title) == (0.2, 0.3, 0.05, "Family Friendly")
    assert "Censored - English" in s.known_track_titles
    assert (s.judge_url, s.judge_model, s.judge_threads) == ("http://ollama.test:11434", "qwen3.5:4b", 4)
    assert (s.media_server, s.plex_url, s.plex_token, s.hold_policy) == (
        "plex", "http://plex.test:32400", "plex-token", "playing")
    assert (s.bitrate_surround, s.bitrate_stereo, s.ffmpeg_threads, s.compute_type,
            s.judge_keep_alive, s.keep_backup) == ("448k", "160k", 3, "int8", "1m", True)

    # And the page shows the saved values after a reload, secrets masked.
    page.reload()
    page.wait_for_load_state("networkidle")
    assert page.locator("[name=jellyfin_url]").input_value() == "http://jellyfin.test:8096"
    assert page.locator("[name=jellyfin_api_key]").input_value() == "********"
    assert page.locator("[name=track_title]").input_value() == "Family Friendly"
    assert page.locator("[name=hold_policy]").input_value() == "playing"
    assert page.locator("[name=keep_backup]").is_checked()
    assert page.locator("[name=recheck_url]").input_value() == "http://speaches.test:8000"
    assert page.locator("[data-tags=custom_words] .tag").all_inner_texts() == ["muppet"]
    assert page.locator("#save-bar").is_hidden()
    assert page.locator("#nav-upcoming").is_hidden()      # Jellyfin has no calendar
    assert desk.errors == []


def test_bad_values_are_shown_on_their_fields(desk):
    page = desk.go("settings/muting")
    page.fill("[name=pad_start]", "9")
    page.fill("[name='sonarr.url']", "sonarr:8989")
    page.locator("#settings-save").click()
    page.wait_for_timeout(400)
    assert "2 things to fix" in page.locator("#save-state").inner_text()
    assert page.locator("[name=pad_start]").get_attribute("aria-invalid") == "true"
    assert "between 0 and 2" in page.locator("[data-error-for=pad_start]").inner_text()
    assert "http://" in page.locator("[data-error-for='sonarr.url']").inner_text()
    assert config.load().pad_start == 0.12            # nothing was written


def test_test_button_uses_what_is_typed(desk, demo):
    page = desk.go("settings")
    page.fill("[name='sonarr.url']", "http://127.0.0.1:9")
    page.get_by_role("button", name="Test Sonarr").click()
    result = page.locator("[data-result=sonarr]")
    result.wait_for()
    page.wait_for_function("document.querySelector('[data-result=sonarr]').textContent.includes('reach')")
    page.fill("[name='sonarr.url']", demo.sonarr.url)
    page.get_by_role("button", name="Test Sonarr").click()
    page.wait_for_function("document.querySelector('[data-result=sonarr]').textContent.includes('Connected')")


def test_leaving_with_unsaved_changes_asks(desk):
    page = desk.go("settings")
    page.fill("[name=track_title]", "Something Else")
    page.locator(".sidebar [data-nav=home]").click()
    dialog = page.locator("#ask")
    dialog.wait_for(state="visible")
    assert "Leave without saving" in page.locator("#ask-title").inner_text()
    page.locator("#ask").get_by_role("button", name="Cancel").click()
    assert page.locator("#view-settings").is_visible()
    assert page.locator("[name=track_title]").input_value() == "Something Else"
    page.locator(".sidebar [data-nav=home]").click()
    page.locator("#ask").get_by_role("button", name="Discard changes").click()
    page.locator("#view-home").wait_for()
    assert config.load().track_title == "Censored - English"


# ---------------------------------------------------------------- that was wrong

def test_that_was_wrong_puts_the_word_on_a_list_and_undo_takes_it_off(desk):
    page = desk.go("cleaned")
    page.locator("#view-cleaned [data-action=open-job]").last.click()
    page.locator(".detection").first.wait_for()
    row = page.locator(".detection", has_text="Dick,")
    row.locator("summary", has_text="That was wrong").click()
    row.get_by_role("button", name=re.compile("Never mute .* anywhere")).click()
    page.locator(".toast", has_text="will never be muted").wait_for()
    assert config.load().allow_words == ["dick"]
    assert "Word lists changed" in page.locator("#sheet-body").inner_text()
    page.locator("#sheet .toast").get_by_role("button", name="Undo").click()
    page.locator(".toast", has_text="back off").wait_for()
    assert config.load().allow_words == []


def test_one_word_can_be_left_in_then_the_file_censored_again(desk):
    page = desk.go("cleaned")
    page.locator("#view-cleaned [data-action=open-job]").last.click()
    page.locator(".detection").first.wait_for()
    row = page.locator(".detection", has_text="Dick,")
    row.locator("summary", has_text="That was wrong").click()
    row.get_by_role("button", name=re.compile("Leave this one in")).click()
    page.locator(".toast", has_text="will be left in next time").wait_for()
    body = page.locator("#sheet-body").inner_text()
    assert "1 word you corrected" in body and "Censor again to apply it" in body
    assert config.load().allow_words == [] and not config.load().allow_words_by_title.get("Dick")
    # Changed my mind.
    row.locator("summary", has_text="That was wrong").click()
    row.get_by_role("button", name="Undo my choice for this one").click()
    page.locator(".toast", has_text="is gone").wait_for()
    assert "you corrected" not in page.locator("#sheet-body").inner_text()
    assert desk.errors == []


def test_a_word_left_in_can_be_made_always_muted(desk):
    page = desk.go("cleaned")
    page.locator("#view-cleaned .row", has_text=re.compile("The Quiet Harbour.*S01E02")).locator(
        "[data-action=open-job]").click()
    row = page.locator(".detection", has_text="Left in")
    row.wait_for()
    row.locator("summary", has_text="That was wrong").click()
    row.get_by_role("button", name=re.compile("Always mute")).click()
    page.locator(".toast", has_text="always be muted").wait_for()
    assert "cock" not in config.load().check_in_context


# ---------------------------------------------------------------- first run

def test_a_fresh_install_gets_a_checklist(browser, demo):
    config.save(config.Settings())
    p = Page(browser, demo, DESKTOP)
    try:
        page = p.go("home")
        card = page.locator("#home-setup .setup")
        card.wait_for()
        assert "Finish setting up" in card.inner_text()
        assert "Add the Sonarr address and API key, or a media server." in card.inner_text()
        assert page.locator("#settings-dot").is_visible()
        # Home says what went wrong rather than sitting on "Loading".
        assert page.locator("#home-problems .alert").is_visible()
        card.get_by_role("link", name="Fix").first.click()
        page.locator("#view-settings").wait_for()
        page.locator("#settings-form").wait_for()
    finally:
        p.close()
        demo.configure()


def test_the_login_gate(browser, demo):
    s = demo.configure()
    s.auth_hash, s.auth_salt = auth.hash_password("long enough")
    s.auth_secret = auth.new_secret()
    s.auth_user = "amy"
    config.save(s)
    p = Page(browser, demo, PHONE)
    try:
        page = p.go("home")
        page.locator("#gate").wait_for(state="visible")
        page.fill("#gate-user", "amy")
        page.fill("#gate-pass", "wrong password")
        page.locator("#gate-submit").click()
        page.locator("#gate-error").wait_for(state="visible")
        assert "wrong username or password" in page.locator("#gate-error").inner_text()
        page.fill("#gate-pass", "long enough")
        page.locator("#gate-submit").click()
        page.locator("#gate").wait_for(state="hidden")
        page.locator("#view-home h1").wait_for()
    finally:
        p.close()
        demo.configure()


# ---------------------------------------------------------------- contrast

def _rgb(value: str, over=(0, 0, 0)):
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    nums = [float(x) for x in re.findall(r"[\d.]+", value)]
    r, g, b = nums[:3]
    a = nums[3] if len(nums) > 3 else 1.0
    return tuple(round(c * a + o * (1 - a)) for c, o in zip((r, g, b), over))


def _lum(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [
    ("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("text", "surface-3"),
    ("muted", "bg"), ("muted", "surface"), ("muted", "surface-2"), ("muted", "surface-3"),
    ("accent-text", "bg"), ("accent-text", "surface"), ("accent-text", "surface-2"),
    ("on-accent", "accent"), ("on-danger", "danger"),
    ("error", "bg"), ("error", "surface"), ("error", "surface-2"),
    ("warn", "surface"), ("warn", "surface-2"), ("info", "surface"),
]
SOFT_PAIRS = [("accent-text", "accent-soft"), ("warn", "warn-soft"),
              ("error", "error-soft"), ("info", "info-soft")]
EDGE_PAIRS = [("line-strong", "surface"), ("line-strong", "surface-2"),
              ("focus", "bg"), ("focus", "surface"), ("accent", "surface")]


@pytest.mark.parametrize("scheme", ["dark", "light"])
def test_colour_contrast(browser, demo, scheme):
    p = Page(browser, demo, DESKTOP, scheme=scheme)
    try:
        page = p.go("home")
        names = sorted({n for pair in TEXT_PAIRS + SOFT_PAIRS + EDGE_PAIRS for n in pair})
        tokens = page.evaluate("""(names) => {
          const s = getComputedStyle(document.documentElement);
          return Object.fromEntries(names.map(n => [n, s.getPropertyValue('--' + n).trim()]));
        }""", names)
    finally:
        p.close()
    surface = _rgb(tokens["surface"])
    bad = []
    for fg, bg in TEXT_PAIRS:
        ratio = contrast(_rgb(tokens[fg]), _rgb(tokens[bg]))
        if ratio < 4.5:
            bad.append(f"{fg} on {bg}: {ratio:.2f}")
    for fg, bg in SOFT_PAIRS:
        ratio = contrast(_rgb(tokens[fg]), _rgb(tokens[bg], over=surface))
        if ratio < 4.5:
            bad.append(f"{fg} on {bg}: {ratio:.2f}")
    for fg, bg in EDGE_PAIRS:
        ratio = contrast(_rgb(tokens[fg]), _rgb(tokens[bg]))
        if ratio < 3:
            bad.append(f"{fg} edge on {bg}: {ratio:.2f}")
    assert bad == [], f"{scheme}: " + "; ".join(bad)


def test_a_destructive_question_starts_on_cancel(desk):
    page = desk.go("queue")
    page.get_by_role("button", name="Empty the queue").click()
    page.locator("#ask").wait_for(state="visible")
    assert page.evaluate("document.activeElement.textContent") == "Cancel"
    page.keyboard.press("Enter")
    page.locator("#ask").wait_for(state="hidden")
    assert page.locator("#job-list .pick").count() == 5       # nothing cancelled



def test_never_mute_in_this_show_only_and_undo(desk):
    page = desk.go("cleaned")
    page.locator("#view-cleaned .row", has_text=re.compile("The Quiet Harbour.*S01E01")).locator(
        "[data-action=open-job]").click()
    row = page.locator(".detection", has_text="Dick,")
    row.wait_for()
    row.locator("summary", has_text="That was wrong").click()
    row.get_by_role("button", name="Never mute “Dick” in The Quiet Harbour").click()
    page.locator(".toast", has_text="in The Quiet Harbour").wait_for()
    s = config.load()
    assert s.allow_words_by_title["The Quiet Harbour"] == ["dick"]
    assert "dick" not in s.allow_words
    page.locator("#sheet .toast").get_by_role("button", name="Undo").click()
    page.locator(".toast", has_text="back off").wait_for()
    assert "The Quiet Harbour" not in config.load().allow_words_by_title


def test_words_worth_a_listen_are_pointed_out(desk):
    page = desk.go("cleaned")
    flagged = page.locator("#view-cleaned .row", has_text="worth a listen")
    assert flagged.count() >= 1
    page.select_option("#cleaned-filter", "check")
    page.wait_for_timeout(400)
    rows = page.locator("#cleaned-list .row")
    assert rows.count() == flagged.count()
    assert all("worth a listen" in t for t in rows.all_inner_texts())
    rows.first.locator("[data-action=open-job]").click()
    page.locator(".detection.check").first.wait_for()
    body = page.locator("#sheet-body").inner_text()
    assert "worth a listen" in body
    assert "Only the subtitles have it: “Don't turn your back on me, you pussy.”" in body
    # Muted on the subtitles' word: flagged, and still muted.
    assert "Muted" in page.locator(".detection.check", has_text="pussy").first.inner_text()
    # Unsure, but the subtitles had it: settled, so not flagged.
    assert page.locator(".detection.check", has_text="damn").count() == 0


def test_per_show_exceptions_can_be_edited_in_settings(desk):
    page = desk.go("settings/words")
    block = page.locator(".title-exception", has_text="Signal Hill")
    block.wait_for()
    assert block.locator(".tag").all_inner_texts() == ["dick"]
    box = block.locator("input")
    box.fill("prick")
    box.press("Enter")
    page.locator("#settings-save").click()
    page.locator("#save-bar").wait_for(state="hidden")
    assert config.load().allow_words_by_title == {"Signal Hill": ["dick", "prick"]}
    page.locator(".title-exception", has_text="Signal Hill").get_by_role("button", name=re.compile("Remove every")).click()
    page.locator("#settings-save").click()
    page.locator("#save-bar").wait_for(state="hidden")
    assert config.load().allow_words_by_title == {}



def test_a_menu_at_the_bottom_opens_upwards(desk):
    page = desk.go("cleaned")
    page.locator("#view-cleaned .row", has_text=re.compile("The Quiet Harbour.*S01E07")).locator(
        "[data-action=open-job]").click()
    last = page.locator(".detection").last
    last.wait_for()
    last.evaluate("el => el.scrollIntoView({block: 'end'})")
    last.locator("summary").click()
    menu = last.locator(".menu-list")
    box, area = menu.bounding_box(), page.locator("#sheet-body").bounding_box()
    assert box["y"] + box["height"] <= area["y"] + area["height"]


def test_the_subtitles_are_not_a_switch_any_more(desk):
    page = desk.go("settings/judge")
    page.locator("#settings-form").wait_for()
    assert page.locator("[name=subtitle_opinion]").count() == 0
    assert page.locator("[name=subtitle_search]").count() == 0
    assert "sounds like" in page.locator("#s-judge").inner_text()
    assert desk.errors == []


def test_the_app_is_installable(browser, demo):
    """What Chrome checks before it offers "Install app": a manifest it can
    read, with a name, a start page, standalone display and 192 and 512 px
    icons, and a service worker. Chrome also needs a secure address - here
    localhost - which a plain http:// LAN address is not."""
    import json
    import struct
    import urllib.request
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(demo.url + "/")
        page.wait_for_load_state("networkidle")
        assert page.evaluate("navigator.serviceWorker.ready.then(r => !!r.active)")
        cdp = ctx.new_cdp_session(page)
        assert cdp.send("Page.getInstallabilityErrors")["installabilityErrors"] == []
        got = cdp.send("Page.getAppManifest")
        assert got["errors"] == [] and got["url"].endswith("/static/manifest.json")
        manifest = json.loads(got["data"])
        assert manifest["name"] and manifest["short_name"] and manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        sizes = set()
        for icon in manifest["icons"]:
            with urllib.request.urlopen(demo.url + icon["src"]) as resp:
                head = resp.read(24)
            assert head[:8] == b"\x89PNG\r\n\x1a\n"
            w, h = struct.unpack(">II", head[16:24])
            assert f"{w}x{h}" == icon["sizes"]
            sizes.add(icon["sizes"])
        assert {"192x192", "512x512"} <= sizes
    finally:
        ctx.close()
