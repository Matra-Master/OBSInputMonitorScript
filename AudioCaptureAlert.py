"""
OBS Audio Capture Alert Plugin
Monitors audio input devices and triggers configurable alerts when silence is detected.

Features:
- Configurable silence threshold (in dB and duration)
- Extensible alert method system (source visibility, desktop notification, ...)
- Real-time audio level monitoring
- Configurable check interval timer so as to use fewer resources
- Event logging option for debugging
"""

import obspython as obs
from types import SimpleNamespace
from ctypes import *
from ctypes.util import find_library
import math
import subprocess
import platform
import threading

# ---------------------------------------------------------------------------
# OBS native library bindings
# ---------------------------------------------------------------------------

obsffi = CDLL(find_library("obs"))
G = SimpleNamespace()

def wrap(funcname, restype, argtypes):
    """Simplify wrapping ctypes functions in obsffi"""
    func = getattr(obsffi, funcname)
    func.restype = restype
    func.argtypes = argtypes
    globals()["g_" + funcname] = func

class Source(Structure):
    pass

class Volmeter(Structure):
    pass

volmeter_callback_t = CFUNCTYPE(None, c_void_p, POINTER(c_float), POINTER(c_float), POINTER(c_float))

wrap("obs_get_source_by_name", POINTER(Source), argtypes=[c_char_p])
wrap("obs_source_release", None, argtypes=[POINTER(Source)])
wrap("obs_volmeter_create", POINTER(Volmeter), argtypes=[c_int])
wrap("obs_volmeter_destroy", None, argtypes=[POINTER(Volmeter)])
wrap("obs_volmeter_add_callback", None, argtypes=[POINTER(Volmeter), volmeter_callback_t, c_void_p])
wrap("obs_volmeter_remove_callback", None, argtypes=[POINTER(Volmeter), volmeter_callback_t, c_void_p])
wrap("obs_volmeter_attach_source", c_bool, argtypes=[POINTER(Volmeter), POINTER(Source)])

@volmeter_callback_t
def volmeter_callback(data, mag, peak, input):
    G.noise = float(peak[0])

# ---------------------------------------------------------------------------
# Alert method base class
# ---------------------------------------------------------------------------

class AlertMethod:
    """
    Base class for all alert methods.

    Subclass this to add a new alert type. Each method is responsible for:
      - Declaring its own OBS properties via register_properties()
      - Reading its settings via update_from_settings()
      - Reacting to silence/restoration via on_silence() / on_restored()

    The `enabled` attribute is managed automatically by the base class using
    the key returned by enabled_key (defaults to "<id>_enabled").
    """

    # Human-readable label shown as the checkbox text in the OBS UI
    label = "Alert Method"

    # Short snake_case identifier used to namespace OBS settings keys
    id = "alert_method"

    def __init__(self):
        self.enabled = False

    @property
    def enabled_key(self):
        return f"{self.id}_enabled"

    def register_properties(self, props, sources):
        """Add method-specific OBS properties to props. sources is the enumerated source list."""
        pass

    def set_defaults(self, settings):
        """Set default values for this method's settings."""
        pass

    def update_from_settings(self, settings):
        """Read this method's settings from the OBS settings object."""
        self.enabled = obs.obs_data_get_bool(settings, self.enabled_key)

    def on_silence(self):
        """Called once when silence threshold is first reached."""
        pass

    def on_restored(self):
        """Called once when audio is restored after a silence alert."""
        pass

    def reset(self):
        """Called when the plugin is toggled or reset."""
        pass

# ---------------------------------------------------------------------------
# Alert method: Source Visibility
# ---------------------------------------------------------------------------

class SourceVisibilityAlert(AlertMethod):
    label = "Show/Hide a Scene Source"
    id = "source_visibility"

    def __init__(self):
        super().__init__()
        self.source_name = ""

    def _set_visible_all_scenes(self, visible):
        scenes = obs.obs_frontend_get_scenes()
        if not scenes:
            if G.event_logging:
                print("[SourceVisibility] No scenes found!")
            return
        for scene in scenes:
            scene_obj = obs.obs_scene_from_source(scene)
            if not scene_obj:
                continue
            item = obs.obs_scene_find_source(scene_obj, self.source_name)
            if item:
                obs.obs_sceneitem_set_visible(item, visible)
                if G.event_logging:
                    print(f"[SourceVisibility] '{self.source_name}' visibility -> {visible} "
                          f"in scene '{obs.obs_source_get_name(scene)}'")
        obs.source_list_release(scenes)

    def register_properties(self, props, sources):
        source_list = obs.obs_properties_add_list(props, "sv_source", "Scene Source",
            obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
        obs.obs_property_list_add_string(source_list, "Select a Source", "")
        for source in sources:
            source_id = obs.obs_source_get_id(source)
            name = obs.obs_source_get_name(source)
            if source_id == "image_source":
                obs.obs_property_list_add_string(source_list, f"Image: {name}", f"Image: {name}")
            elif source_id == "ffmpeg_source":
                obs.obs_property_list_add_string(source_list, f"Media: {name}", f"Media: {name}")
            elif source_id in ["dshow_input", "v4l2_input"]:
                obs.obs_property_list_add_string(source_list, f"Video: {name}", f"Video: {name}")
        obs.obs_properties_add_text(props, "sv_source_help",
            "Select the image, media, or video source to show when silence is detected.\n"
            "It will be hidden again when audio is restored.",
            obs.OBS_TEXT_INFO)

    def update_from_settings(self, settings):
        super().update_from_settings(settings)
        combined = obs.obs_data_get_string(settings, "sv_source")
        if combined and ":" in combined:
            self.source_name = combined.split(":", 1)[1].strip()
        else:
            self.source_name = ""
        if G.event_logging:
            print(f"[SourceVisibility] enabled={self.enabled} source='{self.source_name}'")

    def on_silence(self):
        if not self.source_name:
            print("[SourceVisibility] No source selected, skipping.")
            return
        self._set_visible_all_scenes(True)

    def on_restored(self):
        if not self.source_name:
            return
        self._set_visible_all_scenes(False)

# ---------------------------------------------------------------------------
# Alert method: Desktop Notification
# ---------------------------------------------------------------------------

class DesktopNotificationAlert(AlertMethod):
    label = "Desktop Notification"
    id = "desktop_notification"

    def __init__(self):
        super().__init__()
        self.message = "Microphone is silent!"
        self.icon_path = ""

    def _send(self, title, message):
        def _notify():
            system = platform.system()
            try:
                if system == "Linux":
                    cmd = ["notify-send", title, message]
                    if self.icon_path:
                        cmd += ["-i", self.icon_path]
                    subprocess.Popen(cmd)
                elif system == "Windows":
                    image_xml = (f'<image placement="appLogoOverride" src="{self.icon_path}"/>'
                                 if self.icon_path else "")
                    ps_script = f"""
$xml = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      {image_xml}
      <text>{title}</text>
      <text>{message}</text>
    </binding>
  </visual>
</toast>
"@
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom,ContentType=WindowsRuntime]|Out-Null
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("OBS Studio").Show($toast)
"""
                    subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                elif system == "Darwin":
                    script = f'display notification "{message}" with title "{title}"'
                    subprocess.Popen(["osascript", "-e", script])
            except Exception as e:
                print(f"[DesktopNotification] Error: {e}")

        threading.Thread(target=_notify, daemon=True).start()

    def register_properties(self, props, sources):
        obs.obs_properties_add_text(props, "dn_message", "Silence Alert Message", obs.OBS_TEXT_DEFAULT)
        obs.obs_properties_add_text(props, "dn_message_help",
            "Message shown in the desktop notification when silence is detected.",
            obs.OBS_TEXT_INFO)
        obs.obs_properties_add_path(props, "dn_icon", "Notification Icon (optional)",
            obs.OBS_PATH_FILE, "Images (*.png *.jpg *.ico *.bmp)", "")
        obs.obs_properties_add_text(props, "dn_icon_help",
            "Optional icon shown in the notification.\n"
            "Supported on Linux and Windows. Not available on macOS.",
            obs.OBS_TEXT_INFO)

    def set_defaults(self, settings):
        obs.obs_data_set_default_string(settings, "dn_message", "Microphone is silent!")

    def update_from_settings(self, settings):
        super().update_from_settings(settings)
        self.message = obs.obs_data_get_string(settings, "dn_message") or "Microphone is silent!"
        self.icon_path = obs.obs_data_get_string(settings, "dn_icon") or ""
        if G.event_logging:
            print(f"[DesktopNotification] enabled={self.enabled} message='{self.message}'")

    def on_silence(self):
        self._send("OBS Audio Alert", self.message)

    def on_restored(self):
        self._send("OBS Audio Alert", "Microphone audio restored.")

# ---------------------------------------------------------------------------
# Alert method registry
# To add a new alert method: subclass AlertMethod and append an instance here.
# ---------------------------------------------------------------------------

ALERT_METHODS = [
    SourceVisibilityAlert(),
    DesktopNotificationAlert(),
]

# ---------------------------------------------------------------------------
# Global plugin state
# ---------------------------------------------------------------------------

OBS_FADER_LOG = 2
G.lock = False
G.start_delay = 1
G.duration = 0
G.noise = -math.inf
G.tick = 10000
G.tick_mili = G.tick * 0.001
G.mic_source_name = ""
G.volmeter = None
G.silence_duration = 0
G.silence_threshold = 60
G.silence_db_threshold = -60
G.plugin_enabled = False
G.enable_only_active = False
G.event_logging = True
G.alert_sent = False

def output_to_file(volume):
    with open("current_db_volume_of_source_status.txt", "w", encoding="utf-8") as f:
        f.write(f"Peak Volume: {volume} dB\n")

G.callback = output_to_file

# ---------------------------------------------------------------------------
# Event loop
# ---------------------------------------------------------------------------

def event_loop():
    """Check audio levels every tick interval."""
    if G.enable_only_active:
        if not (obs.obs_frontend_streaming_active() or obs.obs_frontend_recording_active()):
            if G.event_logging:
                print("Not streaming or recording - plugin inactive")
            return

    if G.event_logging:
        print(f"G.noise = {G.noise} dB (Silence Duration: {G.silence_duration}s)")

    if G.duration > G.start_delay:
        if not G.lock:
            if G.event_logging:
                print("Initializing volmeter...")
            source = g_obs_get_source_by_name(G.mic_source_name.encode("utf-8"))
            if not source:
                print(f"Error: Audio Capture source '{G.mic_source_name}' not found!")
                return
            G.volmeter = g_obs_volmeter_create(OBS_FADER_LOG)
            if not G.volmeter:
                print("Error: Failed to create volmeter!")
                g_obs_source_release(source)
                return
            g_obs_volmeter_add_callback(G.volmeter, volmeter_callback, None)
            if g_obs_volmeter_attach_source(G.volmeter, source):
                g_obs_source_release(source)
                G.lock = True
                if G.event_logging:
                    print("Volmeter attached to Audio Capture source.")
            else:
                print("Error: Failed to attach volmeter to Audio Capture source!")
                g_obs_volmeter_destroy(G.volmeter)
                g_obs_source_release(source)
                return

        if G.noise <= G.silence_db_threshold or math.isinf(G.noise):
            G.silence_duration += G.tick / 1000
            if G.silence_duration >= G.silence_threshold:
                if G.event_logging:
                    print(f"Silence detected for {G.silence_threshold}s. Triggering alerts.")
                if not G.alert_sent:
                    for method in ALERT_METHODS:
                        if method.enabled:
                            method.on_silence()
                    G.alert_sent = True
        else:
            if G.alert_sent:
                for method in ALERT_METHODS:
                    if method.enabled:
                        method.on_restored()
                G.alert_sent = False
            G.silence_duration = 0

        G.callback(G.noise)
    else:
        G.duration += G.tick_mili

# ---------------------------------------------------------------------------
# OBS script hooks
# ---------------------------------------------------------------------------

def script_unload():
    obs.timer_remove(event_loop)
    if G.volmeter:
        g_obs_volmeter_remove_callback(G.volmeter, volmeter_callback, None)
        g_obs_volmeter_destroy(G.volmeter)
        print("Volmeter and callback removed.")
    else:
        print("No volmeter to clean up.")

def script_defaults(settings):
    obs.obs_data_set_default_int(settings, "tick_interval", 10)
    obs.obs_data_set_default_int(settings, "silence_threshold", 60)
    for method in ALERT_METHODS:
        method.set_defaults(settings)

def _sep(props, key):
    obs.obs_properties_add_text(props, key, "──────────────────────────────", obs.OBS_TEXT_INFO)

def script_properties():
    props = obs.obs_properties_create()

    # Audio Capture Source
    mic_list = obs.obs_properties_add_list(props, "mic_source_name", "Audio Capture Source",
        obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING)
    obs.obs_property_list_add_string(mic_list, "Select an Audio Source", "")
    obs.obs_properties_add_text(props, "mic_source_help",
        "Select the audio input device to monitor for silence.\n"
        "This should be your microphone or audio capture source.",
        obs.OBS_TEXT_INFO)
    _sep(props, "sep1")

    # Timer Settings
    obs.obs_properties_add_int(props, "tick_interval", "Timer Interval (seconds)", 1, 60, 1)
    obs.obs_properties_add_text(props, "tick_interval_help",
        "How often (in seconds) to check audio levels.\n"
        "Lower values are more responsive but use more CPU.",
        obs.OBS_TEXT_INFO)
    _sep(props, "sep2")

    # Silence Threshold
    obs.obs_properties_add_int(props, "silence_threshold", "Silence Threshold Duration (seconds)", 1, 600, 1)
    obs.obs_properties_add_text(props, "silence_threshold_help",
        "How long (in seconds) of silence is required before triggering alerts.\n"
        "Prevents brief pauses from triggering.",
        obs.OBS_TEXT_INFO)
    _sep(props, "sep3")

    # Plugin Control
    obs.obs_properties_add_bool(props, "plugin_enabled", "Enable Plugin Globally")
    obs.obs_properties_add_text(props, "plugin_enabled_help",
        "Enable the plugin.\n"
        "When not checked the plugin is disabled regardless of any other option.",
        obs.OBS_TEXT_INFO)
    _sep(props, "sep4")
    obs.obs_properties_add_bool(props, "enable_only_active", "Enable only when streaming/recording")
    obs.obs_properties_add_text(props, "enable_only_active_help",
        "Only trigger alerts when actively streaming or recording.\n"
        "When disabled, plugin works in all OBS states.",
        obs.OBS_TEXT_INFO)
    _sep(props, "sep5")

    # Event Logging
    obs.obs_properties_add_bool(props, "event_logging", "Enable Event Logging")
    obs.obs_properties_add_text(props, "event_logging_help",
        "Enable detailed logging of plugin events to the OBS log.\n"
        "Useful for debugging but may impact performance.",
        obs.OBS_TEXT_INFO)
    _sep(props, "sep6")

    # Enumerate sources once; pass the list to each method
    sources = obs.obs_enum_sources() or []

    # Per-method sections
    for i, method in enumerate(ALERT_METHODS):
        obs.obs_properties_add_bool(props, method.enabled_key, f"Enable: {method.label}")
        method.register_properties(props, sources)
        _sep(props, f"sep_method_{i}")

    if sources:
        # Populate mic dropdown
        for source in sources:
            source_id = obs.obs_source_get_id(source)
            name = obs.obs_source_get_name(source)
            if source_id in ["wasapi_input_capture", "wasapi_output_capture",
                              "coreaudio_input_capture", "dshow_input",
                              "pulse_input_capture", "pulse_output_capture",
                              "alsa_input_capture", "jack_output_capture"]:
                obs.obs_property_list_add_string(mic_list, name, name)
        obs.source_list_release(sources)

    return props

def script_update(settings):
    G.mic_source_name = obs.obs_data_get_string(settings, "mic_source_name")
    G.tick = (obs.obs_data_get_int(settings, "tick_interval") or 10) * 1000
    G.tick_mili = G.tick * 0.001
    G.silence_threshold = obs.obs_data_get_int(settings, "silence_threshold") or 60

    prev_plugin_enabled = G.plugin_enabled
    prev_enable_only_active = G.enable_only_active

    G.plugin_enabled = obs.obs_data_get_bool(settings, "plugin_enabled")
    G.enable_only_active = obs.obs_data_get_bool(settings, "enable_only_active")
    G.event_logging = obs.obs_data_get_bool(settings, "event_logging")

    for method in ALERT_METHODS:
        method.update_from_settings(settings)

    if prev_plugin_enabled != G.plugin_enabled or prev_enable_only_active != G.enable_only_active:
        G.silence_duration = 0
        G.alert_sent = False
        if G.event_logging:
            print("Reset silence duration due to plugin state change")

    obs.timer_remove(event_loop)
    if G.plugin_enabled:
        obs.timer_add(event_loop, G.tick)

    if G.event_logging:
        print(f"Audio Capture Source: {G.mic_source_name}")
        print(f"Timer Interval: {G.tick / 1000}s  Silence Threshold: {G.silence_threshold}s")
        print(f"Plugin Enabled: {G.plugin_enabled}")
        for method in ALERT_METHODS:
            print(f"  [{method.id}] enabled={method.enabled}")

# Add the event loop to the OBS timer (only if already enabled on load)
if G.plugin_enabled:
    obs.timer_add(event_loop, G.tick)
