"""CavaMenuWidget - cava visualizer with a control-centre menu.

Subclasses the built-in ``CavaWidget`` and adds a popup menu opened on left
click containing: a keyboard language switcher, audio output selection, a
volume slider, brightness slider(s) and a Bluetooth section. Opening the
Bluetooth section powers the radio on automatically.

The compiled module is injected into YASB's library.zip as
``core/widgets/yasb/cava_menu.pyc`` (see install_cava_menu_fixed.ps1) and must be
re-installed after every YASB update, because updates overwrite library.zip.
"""

import ctypes
import logging
import re
import struct
import winreg
from ctypes import wintypes

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
)

from core.utils.qobject import is_valid_qobject
from core.utils.utilities import PopupWidget
from core.validation.widgets.base_model import CustomBaseModel
from core.validation.widgets.yasb.cava import CavaConfig
from core.validation.widgets.yasb.bluetooth import BluetoothMenuConfig
from core.widgets.services.brightness.service import BrightnessService
from core.widgets.services.volume.service import AudioOutputService
from core.widgets.yasb.cava import CavaWidget

try:
    from core.widgets.services.bluetooth.bluetooth_managers import BluetoothManager
    from core.widgets.services.bluetooth.bluetooth_widgets import BluetoothMenu

    _HAS_BLUETOOTH = True
except Exception:
    _HAS_BLUETOOTH = False
    logging.warning("CavaMenuWidget: bluetooth support unavailable")

try:
    from win32con import WM_INPUTLANGCHANGEREQUEST

    from core.utils.win32.bindings import kernel32, user32
    from core.utils.win32.constants import (
        LOCALE_NAME_MAX_LENGTH,
        LOCALE_SISO3166CTRYNAME,
        LOCALE_SISO639LANGNAME,
        LOCALE_SISO639LANGNAME2,
        LOCALE_SLANGUAGE,
        LOCALE_SNAME,
        LOCALE_SNATIVECTRYNAME,
        LOCALE_SNATIVELANGNAME,
    )

    _HAS_LANGUAGE = True
except Exception:
    _HAS_LANGUAGE = False
    logging.warning("CavaMenuWidget: keyboard language support unavailable")

# IME conversion mode bit: set while an IME is in its "native" (e.g. Chinese)
# mode. When cleared the IME types the latin alphabet instead - this is the
# state change the bar indicator surfaces.
IME_CMODE_NATIVE = 0x0001

try:
    imm32 = ctypes.WinDLL("imm32")
    imm32.ImmGetContext.argtypes = [wintypes.HWND]
    imm32.ImmGetContext.restype = wintypes.HANDLE
    imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
    imm32.ImmReleaseContext.restype = wintypes.BOOL
    imm32.ImmGetConversionStatus.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    imm32.ImmGetConversionStatus.restype = wintypes.BOOL
    imm32.ImmSetConversionStatus.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    imm32.ImmSetConversionStatus.restype = wintypes.BOOL

    _HAS_IME = True
except Exception:
    imm32 = None
    _HAS_IME = False
    logging.warning("CavaMenuWidget: IME status support unavailable")

# ASUS battery charge limit (a.k.a. Battery Care / RSOC). The limit is set
# through the ATKACPI device, which - unlike the root\wmi AsusAtkWmi_WMNB class
# - is writable by a normal (non-elevated) process. The current value lives in
# the registry under ChargingRate.
ASUS_ATKACPI_PATH = r"\\.\ATKACPI"
ASUS_IOCTL_CONTROL = 0x0022240C
ASUS_METHOD_DEVS = 0x53564544
ASUS_DEVID_BATTERY_LIMIT = 0x00120057
ASUS_CHARGING_RATE_KEY = r"SOFTWARE\ASUS\ASUS System Control Interface\AsusOptimization\ASUS Keyboard Hotkeys"

try:
    _kernel32 = ctypes.WinDLL("kernel32")
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.DeviceIoControl.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _HAS_ATKACPI = True
except Exception:
    _kernel32 = None
    _HAS_ATKACPI = False
    logging.warning("CavaMenuWidget: ASUS ATKACPI support unavailable")


class CavaMenuConfig(CavaConfig):
    """Extends CavaConfig with the popup control-centre options."""

    class MenuConfig(CustomBaseModel):
        blur: bool = True
        round_corners: bool = True
        round_corners_type: str = "normal"
        border_color: str = "System"
        alignment: str = "center"
        direction: str = "down"
        offset_top: int = 6
        offset_left: int = 0
        show_language: bool = True

    menu: MenuConfig = MenuConfig()


class CavaMenuWidget(CavaWidget):
    validation_schema = CavaMenuConfig

    def __init__(self, config: CavaMenuConfig):
        super().__init__(config)
        self._menu = None
        self._bt_menu = None
        self._bt_manager = None
        self._brightness = None
        self._volume_icon = None
        self._focused_window_hwnd = None
        self._available_languages = None
        self._device_combo = None
        self._device_icon = None
        self._volume_slider = None
        self._volume_value = None
        self._language_buttons = {}
        self._last_layout_handle = None
        self._battery_button = None
        self._battery_status = None
        self._battery_icon = None
        self._atk_handle = None
        self._battery_limit = None
        self._battery_care_limit = 80
        self._battery_available = False

        if _HAS_ATKACPI:
            limit = self._read_battery_care_limit()
            if limit is not None:
                self._battery_available = True
                self._battery_limit = limit
                if 0 < limit < 100:
                    self._battery_care_limit = limit

        # Volume service
        try:
            self._audio = AudioOutputService()
            self._volume = self._audio.get_volume_interface()
        except Exception:
            self._audio = None
            self._volume = None
            logging.exception("CavaMenuWidget: failed to init audio service")

        # Register with the shared service so it installs its device-change
        # callbacks and invalidates its cache. Without registering, the service
        # never hooks the MMDevice notifications, so its cached device list and
        # speaker endpoint go stale the moment a new output (e.g. a Bluetooth
        # speaker) is connected - which is exactly why switching devices used
        # to misbehave.
        if self._audio is not None:
            try:
                self._audio.register_widget(self)
            except Exception:
                logging.exception("CavaMenuWidget: failed to register with audio service")

        # Brightness service
        try:
            self._brightness = BrightnessService.instance(1000)
        except Exception:
            logging.exception("CavaMenuWidget: failed to init brightness service")

        # Bluetooth
        if _HAS_BLUETOOTH:
            try:
                menu_cfg = self.config.menu
                bt_config = BluetoothMenuConfig(
                    alignment=menu_cfg.alignment,
                    direction=menu_cfg.direction,
                    offset_top=menu_cfg.offset_top,
                    offset_left=menu_cfg.offset_left,
                )
                self._bt_manager = BluetoothManager.acquire()
                self._bt_menu = BluetoothMenu(self, bt_config, self._bt_manager)
                self._bt_manager.status_updated.connect(self._bt_menu.apply_status)
                self._bt_manager.refresh_failed.connect(self._bt_menu.on_refresh_failed)
                self._bt_manager.scan_started.connect(self._bt_menu.on_scan_started)
                self._bt_manager.scan_completed.connect(self._bt_menu.on_scan_completed)
                self._bt_manager.connection_finished.connect(self._bt_menu.on_connection_finished)
                self._bt_manager.start()
            except Exception:
                self._bt_manager = None
                self._bt_menu = None
                logging.exception("CavaMenuWidget: failed to init bluetooth service")

        self.register_callback("toggle_menu", self._toggle_menu)
        self.callback_left = "toggle_menu"
        self.callback_middle = self.config.callbacks.on_middle
        self.callback_right = self.config.callbacks.on_right
        self.destroyed.connect(self._on_destroyed)

        # Bar-side IME indicator: shows the native/alpha state (e.g. Chinese or English alphabet mode)
        # whenever a CJK input method is active for the focused window.
        self._ime_indicator = QLabel()
        self._ime_indicator.setProperty("class", "ime-indicator")
        self._ime_indicator.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ime_indicator.setToolTip("Input mode - click to switch")
        self._ime_indicator.mousePressEvent = lambda event: self._toggle_ime()
        self._ime_indicator.hide()
        try:
            self._widget_container_layout.addWidget(self._ime_indicator)
        except Exception:
            self._ime_indicator = None

        if _HAS_IME and _HAS_LANGUAGE:
            self._ime_timer = QTimer(self)
            self._ime_timer.setInterval(400)
            self._ime_timer.timeout.connect(self._poll_input_state)
            self._ime_timer.start()
        else:
            self._ime_timer = None

    def _on_destroyed(self, *args):
        try:
            if self._audio is not None:
                self._audio.unregister_widget(self)
        except Exception:
            pass
        try:
            if self._atk_handle is not None:
                _kernel32.CloseHandle(self._atk_handle)
                self._atk_handle = None
        except Exception:
            pass
        try:
            if self._bt_manager is not None:
                for signal, slot in (
                    (getattr(self._bt_manager, "status_updated", None), getattr(self._bt_menu, "apply_status", None)),
                    (getattr(self._bt_manager, "refresh_failed", None), getattr(self._bt_menu, "on_refresh_failed", None)),
                    (getattr(self._bt_manager, "scan_started", None), getattr(self._bt_menu, "on_scan_started", None)),
                    (getattr(self._bt_manager, "scan_completed", None), getattr(self._bt_menu, "on_scan_completed", None)),
                    (getattr(self._bt_manager, "connection_finished", None), getattr(self._bt_menu, "on_connection_finished", None)),
                ):
                    try:
                        if signal is not None and slot is not None:
                            signal.disconnect(slot)
                    except Exception:
                        pass
                if self._bt_menu is not None:
                    self._bt_menu.detach()
                try:
                    self._bt_manager.release()
                except Exception:
                    pass
        except Exception:
            pass

    def _on_menu_destroyed(self, *args):
        self._menu = None
        self._language_buttons = {}
        self._device_combo = None
        self._device_icon = None
        self._volume_slider = None
        self._volume_value = None
        self._battery_button = None
        self._battery_status = None
        self._battery_icon = None

    def _toggle_menu(self):
        try:
            if is_valid_qobject(self._menu) and self._menu.isVisible():
                self._menu.hide_animated()
                return
        except Exception:
            pass
        self._capture_foreground_window()
        self._show_menu()

    def _capture_foreground_window(self):
        if not _HAS_LANGUAGE:
            return
        try:
            self._focused_window_hwnd = user32.GetForegroundWindow()
        except Exception:
            self._focused_window_hwnd = None

    def _show_menu(self):
        menu_cfg = self.config.menu
        self._menu = PopupWidget(
            self,
            menu_cfg.blur,
            menu_cfg.round_corners,
            menu_cfg.round_corners_type,
            menu_cfg.border_color,
        )
        self._menu.setProperty("class", "control-center-menu")
        # The popup deletes itself after closing - clear the references so the
        # next click rebuilds it instead of touching deleted C++ objects.
        self._menu.destroyed.connect(self._on_menu_destroyed)
        layout = QVBoxLayout(self._menu)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._build_header(layout)

        if self._bt_menu is not None:
            self._build_bluetooth_button(layout)
            self._add_divider(layout)

        if menu_cfg.show_language:
            self._add_section(layout, "Keyboard")
            self._build_language_row(layout)

        if self._audio is not None:
            self._add_section(layout, "Output")
            self._build_device_row(layout)

        if self._volume is not None:
            self._add_section(layout, "Volume")
            self._build_volume_row(layout)

        if self._brightness is not None:
            self._add_section(layout, "Brightness")
            self._build_brightness_rows(layout)

        if _HAS_ATKACPI:
            self._build_battery_row(layout)

        self._menu.adjustSize()
        self._menu.setPosition(
            menu_cfg.alignment,
            menu_cfg.direction,
            menu_cfg.offset_left,
            menu_cfg.offset_top,
        )
        self._menu.show()

    # ------------------------------------------------------------------
    # Menu chrome (header / section labels / dividers)
    # ------------------------------------------------------------------
    def _build_header(self, layout):
        header = QFrame()
        header.setProperty("class", "cc-header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        icon = QLabel("\uf013")
        icon.setProperty("class", "cc-header-icon")
        header_layout.addWidget(icon)

        title = QLabel("Control Centre")
        title.setProperty("class", "cc-title")
        header_layout.addWidget(title, 1)

        refresh = QPushButton("\uf021")
        refresh.setProperty("class", "cc-refresh")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.setToolTip("Refresh devices and levels")
        refresh.clicked.connect(self._refresh_all)
        header_layout.addWidget(refresh)

        layout.addWidget(header)

    def _add_section(self, layout, text):
        label = QLabel(text.upper())
        label.setProperty("class", "cc-section")
        layout.addWidget(label)

    def _add_divider(self, layout):
        divider = QFrame()
        divider.setProperty("class", "cc-divider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)

    # ------------------------------------------------------------------
    # Keyboard language
    # ------------------------------------------------------------------
    @staticmethod
    def _short_label(text, max_len=20):
        """Collapse a device/language name to a compact label.

        Drops any parenthesised driver/model detail and ellipsises anything that
        is still too long, so the closed combo box never runs off.
        """
        if not text:
            return text
        short = re.split(r"\s*\(", text, 1)[0].strip()
        if not short:
            short = text.strip()
        if len(short) > max_len:
            short = short[: max_len - 1].rstrip() + "\u2026"
        return short

    def _build_language_row(self, layout):
        """Render one compact button per installed keyboard layout instead of a
        drop-down. The active layout is highlighted, so both seeing and
        switching the current input language is a single click."""
        if not _HAS_LANGUAGE:
            return
        try:
            languages = self._get_available_languages()
            if not languages:
                return

            row = QFrame()
            row.setProperty("class", "language-row")
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setHorizontalSpacing(6)
            row_layout.setVerticalSpacing(6)

            current_handle = self._get_current_layout_handle()
            self._language_buttons = {}
            for index, lang in enumerate(languages):
                code = (lang.get("code") or "").upper()
                button = QPushButton(code)
                button.setProperty("class", "lang-button")
                button.setCheckable(True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setToolTip(lang.get("layouts") or lang.get("name") or code)
                button.setChecked(lang.get("id") == current_handle)
                button.clicked.connect(
                    lambda checked=False, lang_id=lang.get("id"): self._on_language_button(lang_id)
                )
                row_layout.addWidget(button, index // 4, index % 4)
                self._language_buttons[lang.get("id")] = button

            layout.addWidget(row)
        except Exception:
            logging.exception("CavaMenuWidget: failed to build language row")

    def _on_language_button(self, lang_id):
        try:
            if lang_id is None:
                return
            self._switch_to_language(lang_id)
            self._refresh_language_buttons()
        except Exception:
            logging.exception("CavaMenuWidget: failed to switch keyboard layout")

    def _refresh_language_buttons(self):
        buttons = getattr(self, "_language_buttons", None)
        if not buttons:
            return
        current = self._get_current_layout_handle()
        for lang_id, button in buttons.items():
            try:
                if is_valid_qobject(button):
                    button.setChecked(lang_id == current)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # IME live state (Chinese/Japanese/Korean native vs. latin mode)
    # ------------------------------------------------------------------
    def _target_window(self):
        """Window whose input state we should reflect. While the popup is open
        it would otherwise become the foreground window, so we pin the window
        that was focused when the menu was opened."""
        try:
            if is_valid_qobject(self._menu) and self._menu.isVisible() and self._focused_window_hwnd:
                return self._focused_window_hwnd
        except Exception:
            pass
        return user32.GetForegroundWindow()

    def _focused_layout_handle(self):
        try:
            hwnd = self._target_window()
            if not hwnd:
                return 0
            thread_id = user32.GetWindowThreadProcessId(hwnd, None)
            return user32.GetKeyboardLayout(thread_id) if thread_id else 0
        except Exception:
            return 0

    @staticmethod
    def _layout_language_code(handle):
        try:
            lang_id = handle & 0xFFFF
            if not lang_id:
                return ""
            buf = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
            kernel32.GetLocaleInfoW(lang_id, LOCALE_SISO639LANGNAME, buf, LOCALE_NAME_MAX_LENGTH)
            return buf.value.lower()
        except Exception:
            return ""

    def _get_ime_conversion(self, hwnd):
        if not _HAS_IME or not hwnd:
            return None
        himc = None
        try:
            himc = imm32.ImmGetContext(hwnd)
            if not himc:
                return None
            conversion = wintypes.DWORD()
            sentence = wintypes.DWORD()
            if not imm32.ImmGetConversionStatus(himc, ctypes.byref(conversion), ctypes.byref(sentence)):
                return None
            return conversion.value
        except Exception:
            return None
        finally:
            if himc:
                try:
                    imm32.ImmReleaseContext(hwnd, himc)
                except Exception:
                    pass

    def _set_ime_conversion(self, hwnd, conversion):
        if not _HAS_IME or not hwnd:
            return False
        himc = None
        try:
            himc = imm32.ImmGetContext(hwnd)
            if not himc:
                return False
            current = wintypes.DWORD()
            sentence = wintypes.DWORD()
            imm32.ImmGetConversionStatus(himc, ctypes.byref(current), ctypes.byref(sentence))
            return bool(imm32.ImmSetConversionStatus(himc, conversion, sentence.value))
        except Exception:
            return False
        finally:
            if himc:
                try:
                    imm32.ImmReleaseContext(hwnd, himc)
                except Exception:
                    pass

    def _toggle_ime(self):
        try:
            hwnd = self._target_window()
            conversion = self._get_ime_conversion(hwnd)
            if conversion is None:
                return
            self._set_ime_conversion(hwnd, conversion ^ IME_CMODE_NATIVE)
            self._poll_input_state()
        except Exception:
            logging.exception("CavaMenuWidget: failed to toggle IME mode")

    def _poll_input_state(self):
        try:
            handle = self._focused_layout_handle()
            if handle != self._last_layout_handle:
                self._last_layout_handle = handle
                self._refresh_language_buttons()
            conversion = self._get_ime_conversion(self._target_window())
            self._update_ime_indicator(handle, conversion)
        except Exception:
            pass

    def _update_ime_indicator(self, handle, conversion):
        indicator = getattr(self, "_ime_indicator", None)
        if indicator is None or not is_valid_qobject(indicator):
            return
        code = self._layout_language_code(handle)
        if code not in ("zh", "ja", "ko") or conversion is None:
            self._ime_state = None
            indicator.hide()
            return
        native = bool(conversion & IME_CMODE_NATIVE)
        state = f"{code}:{'native' if native else 'alpha'}"
        if state == getattr(self, "_ime_state", None) and indicator.isVisible():
            return
        self._ime_state = state
        if code == "zh":
            indicator.setText("\u4e2d" if native else "\u82f1")
        elif code == "ja":
            indicator.setText("\u3042" if native else "A")
        else:
            indicator.setText("\ud55c" if native else "A")
        indicator.setProperty("class", "ime-indicator native" if native else "ime-indicator alpha")
        try:
            indicator.style().unpolish(indicator)
            indicator.style().polish(indicator)
        except Exception:
            pass
        indicator.show()

    def _get_available_languages(self):
        if self._available_languages is not None:
            return self._available_languages
        languages = []
        try:
            num_layouts = user32.GetKeyboardLayoutList(0, None)
            if num_layouts == 0:
                return languages
            layout_array = (ctypes.c_void_p * num_layouts)()
            user32.GetKeyboardLayoutList(num_layouts, layout_array)
            current_layout = user32.ActivateKeyboardLayout(0, 0)
            seen_handles = set()
            for i in range(num_layouts):
                layout_handle = layout_array[i]
                if layout_handle is None or layout_handle in seen_handles:
                    continue
                seen_handles.add(layout_handle)
                try:
                    lang_id = layout_handle & 0xFFFF
                    lang_name_buf = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
                    lang_code_buf = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
                    country_code_buf = ctypes.create_unicode_buffer(LOCALE_NAME_MAX_LENGTH)
                    kernel32.GetLocaleInfoW(lang_id, LOCALE_SLANGUAGE, lang_name_buf, LOCALE_NAME_MAX_LENGTH)
                    if not kernel32.GetLocaleInfoW(
                        lang_id, LOCALE_SISO639LANGNAME, lang_code_buf, LOCALE_NAME_MAX_LENGTH
                    ):
                        kernel32.GetLocaleInfoW(
                            lang_id, LOCALE_SISO639LANGNAME2, lang_code_buf, LOCALE_NAME_MAX_LENGTH
                        )
                    kernel32.GetLocaleInfoW(
                        lang_id, LOCALE_SISO3166CTRYNAME, country_code_buf, LOCALE_NAME_MAX_LENGTH
                    )
                    lang_name = lang_name_buf.value
                    lang_code = lang_code_buf.value
                    country_code = country_code_buf.value
                    k_layouts = None
                    try:
                        user32.ActivateKeyboardLayout(ctypes.c_void_p(layout_handle), 0)
                        klid_buf = ctypes.create_unicode_buffer(9)
                        if user32.GetKeyboardLayoutNameW(klid_buf):
                            reg_path = (
                                "SYSTEM\\CurrentControlSet\\Control\\Keyboard Layouts\\"
                                + klid_buf.value.upper()
                            )
                            try:
                                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path) as key:
                                    try:
                                        k_layouts, _ = winreg.QueryValueEx(key, "Layout Text")
                                    except FileNotFoundError:
                                        k_layouts, _ = winreg.QueryValueEx(key, "Layout Display Name")
                            except (FileNotFoundError, OSError):
                                pass
                    except Exception:
                        pass
                    if lang_name and lang_code:
                        languages.append(
                            {
                                "id": layout_handle,
                                "handle": layout_handle,
                                "name": lang_name,
                                "code": lang_code,
                                "country": country_code,
                                "layouts": k_layouts if k_layouts else lang_name,
                            }
                        )
                except Exception:
                    continue
            user32.ActivateKeyboardLayout(ctypes.c_void_p(current_layout), 0)
            languages.sort(key=lambda x: x["name"])
            self._available_languages = languages
        except Exception:
            logging.exception("CavaMenuWidget: failed to enumerate keyboard layouts")
        return languages

    def _get_current_layout_handle(self):
        try:
            hwnd = self._focused_window_hwnd or user32.GetForegroundWindow()
            thread_id = user32.GetWindowThreadProcessId(hwnd, None)
            return user32.GetKeyboardLayout(thread_id)
        except Exception:
            return 0

    def _activate_layout(self, focus_window, target_layout):
        result = 0
        try:
            is_valid_window = False
            if focus_window and focus_window != 0:
                class_name = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(focus_window, class_name, 256)
                shell_classes = ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd")
                is_valid_window = class_name.value not in shell_classes
            if is_valid_window:
                user32.SetForegroundWindow(focus_window)
                user32.SendMessageW(focus_window, WM_INPUTLANGCHANGEREQUEST, 0, target_layout)
                result = target_layout
            else:
                result = user32.ActivateKeyboardLayout(ctypes.c_void_p(target_layout), 0)
        except Exception:
            logging.exception("CavaMenuWidget: failed to activate keyboard layout")
        return result

    def _switch_to_language(self, target_lang_id):
        try:
            available_languages = self._get_available_languages()
            target_layout = None
            for lang_info in available_languages:
                if lang_info["id"] == target_lang_id:
                    target_layout = lang_info["handle"]
                    break
            if target_layout is None:
                return False
            result = self._activate_layout(self._focused_window_hwnd, target_layout)
            if result == 0:
                layout_str = f"{target_layout & 0xFFFFFFFF:08X}"
                loaded_layout = user32.LoadKeyboardLayoutW(ctypes.c_wchar_p(layout_str), 1)
                if loaded_layout:
                    self._activate_layout(self._focused_window_hwnd, loaded_layout)
            self._available_languages = None
            return True
        except Exception:
            logging.exception("CavaMenuWidget: failed to switch language")
            return False

    # ------------------------------------------------------------------
    # Battery charge limit (ASUS Battery Care / RSOC)
    # ------------------------------------------------------------------
    @staticmethod
    def _read_battery_care_limit():
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ASUS_CHARGING_RATE_KEY) as key:
                value, _ = winreg.QueryValueEx(key, "ChargingRate")
            return int(value)
        except Exception:
            return None

    def _atk_device(self):
        if not _HAS_ATKACPI:
            return None
        if self._atk_handle is not None:
            return self._atk_handle
        try:
            handle = _kernel32.CreateFileW(
                ASUS_ATKACPI_PATH,
                0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
                0x1 | 0x2,  # FILE_SHARE_READ | FILE_SHARE_WRITE
                None,
                3,  # OPEN_EXISTING
                0x80,  # FILE_ATTRIBUTE_NORMAL
                None,
            )
            if handle == wintypes.HANDLE(-1).value:
                logging.warning("CavaMenuWidget: could not open ATKACPI device")
                return None
            self._atk_handle = handle
        except Exception:
            logging.exception("CavaMenuWidget: failed to open ATKACPI device")
            return None
        return self._atk_handle

    def _apply_battery_limit(self, value):
        handle = self._atk_device()
        if handle is None:
            return False
        try:
            payload = struct.pack("<II", ASUS_DEVID_BATTERY_LIMIT, int(value))
            args = struct.pack("<II", ASUS_METHOD_DEVS, len(payload)) + payload
            buffer = (ctypes.c_ubyte * len(args)).from_buffer_copy(args)
            out = (ctypes.c_ubyte * 16)()
            returned = wintypes.DWORD(0)
            ok = _kernel32.DeviceIoControl(
                handle, ASUS_IOCTL_CONTROL, buffer, len(args), out, 16, ctypes.byref(returned), None
            )
            if not ok:
                return False
            result = struct.unpack_from("<I", out, 0)[0]
            return result == 1
        except Exception:
            logging.exception("CavaMenuWidget: failed to set battery charge limit")
            return False

    def _build_battery_row(self, layout):
        try:
            if self._battery_limit is None:
                limit = self._read_battery_care_limit()
                if limit is None:
                    self._battery_available = False
                    return
                self._battery_limit = limit
                if 0 < limit < 100:
                    self._battery_care_limit = limit
            self._battery_available = True
            limit = self._battery_limit

            self._add_section(layout, "Battery")

            row = QFrame()
            row.setProperty("class", "battery-row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)

            icon = QLabel("\uf240" if limit >= 100 else "\uf243")
            icon.setProperty("class", "battery-icon")
            self._battery_icon = icon
            row_layout.addWidget(icon)

            status = QLabel("Full charge" if limit >= 100 else f"Limit {limit}%")
            status.setProperty("class", "battery-status")
            status.setMinimumWidth(0)
            self._battery_status = status
            row_layout.addWidget(status, 1)

            if limit >= 100:
                button = QPushButton(f"Set {self._battery_care_limit}%")
                button.setToolTip(f"Restore the {self._battery_care_limit}% charge limit")
            else:
                button = QPushButton("Set 100%")
                button.setToolTip("Allow charging up to 100%")
            button.setProperty("class", "battery-toggle")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(self._toggle_battery_full)
            self._battery_button = button
            row_layout.addWidget(button)

            layout.addWidget(row)
        except Exception:
            logging.exception("CavaMenuWidget: failed to build battery row")

    def _refresh_battery_row(self):
        limit = self._battery_limit
        if limit is None:
            return
        if self._battery_button is not None and is_valid_qobject(self._battery_button):
            if limit >= 100:
                self._battery_button.setText(f"Set {self._battery_care_limit}%")
                self._battery_button.setToolTip(f"Restore the {self._battery_care_limit}% charge limit")
            else:
                self._battery_button.setText("Set 100%")
                self._battery_button.setToolTip("Allow charging up to 100%")
        if self._battery_status is not None and is_valid_qobject(self._battery_status):
            self._battery_status.setText("Full charge" if limit >= 100 else f"Limit {limit}%")
        if self._battery_icon is not None and is_valid_qobject(self._battery_icon):
            self._battery_icon.setText("\uf240" if limit >= 100 else "\uf243")

    def _toggle_battery_full(self):
        if self._battery_limit is None:
            return
        target = self._battery_care_limit if self._battery_limit >= 100 else 100
        if self._apply_battery_limit(target):
            self._battery_limit = target
        self._refresh_battery_row()

    # ------------------------------------------------------------------
    # Audio output
    # ------------------------------------------------------------------
    @staticmethod
    def _is_bluetooth_name(name):
        lowered = (name or "").lower()
        return any(
            keyword in lowered
            for keyword in (
                "bluetooth",
                " bt ",
                "bt-",
                "buds",
                "airpod",
                "beats",
                "jbl",
                "bose",
                "sonos",
                "soundcore",
                "anker",
                "marshall",
            )
        )

    def _device_icon_for(self, name):
        """Best-effort icon for an output device based on its friendly name."""
        lowered = (name or "").lower()
        if any(keyword in lowered for keyword in ("headphone", "headset", "ear", "bud", "airpod", "wh-", "wf-")):
            return "\uf025"  # headphones
        if self._is_bluetooth_name(name):
            return "\uf293"  # bluetooth
        if any(keyword in lowered for keyword in ("tv", "television", "monitor", "display", "hdmi")):
            return "\uf26c"  # television
        return "\uf028"  # speaker

    def _default_device_id(self):
        if self._audio is None:
            return None
        try:
            device_id = self._audio.get_default_device_id()
            if device_id:
                return device_id
        except Exception:
            pass
        try:
            speakers = self._audio.get_speakers()
            if speakers:
                return getattr(speakers, "id", None)
        except Exception:
            pass
        return None

    @staticmethod
    def _detail_token(name, max_len=18):
        """Extract a compact, human-meaningful token from the parenthesised
        part of a Windows endpoint name, e.g.
        ``Speakers (Realtek(R) Audio)`` -> ``Realtek Audio`` and
        ``Headphones (WH-1000XM4)`` -> ``WH-1000XM4``. Returns "" when there
        is nothing useful to show."""
        if not name or "(" not in name:
            return ""
        inner = name[name.find("(") :].replace("(", " ").replace(")", " ")
        tokens = [t for t in re.split(r"\s+", inner) if len(re.sub(r"[^A-Za-z0-9]", "", t)) >= 2]
        token = " ".join(tokens).strip()
        if not token:
            return ""
        if len(token) > max_len:
            token = token[: max_len - 1].rstrip() + "\u2026"
        return token

    def _device_label_map(self, devices):
        """Build compact display labels that stay unique.

        Windows names are almost always "Type (Model detail)". Stripping the
        parentheses collapses distinct devices (e.g. two "Speakers") onto the
        same label. When that happens we promote the differing model detail to
        the label - the leading icon already conveys the device type - so
        labels stay short *and* distinguishable.
        """
        bases = {}
        for _device_id, name in devices:
            base = self._short_label(name) or "Unknown device"
            bases.setdefault(base, 0)
            bases[base] += 1

        labels = {}
        seen = {}
        for device_id, name in devices:
            base = self._short_label(name) or "Unknown device"
            if bases.get(base, 0) > 1:
                label = self._detail_token(name) or base
            else:
                label = base
            if label in seen:
                seen[label] += 1
                label = f"{label} ({seen[label]})"
            else:
                seen[label] = 1
            labels[device_id] = label
        return labels

    def _populate_device_combo(self, combo, devices):
        default_id = self._default_device_id()
        labels = self._device_label_map(devices)

        def sort_key(item):
            device_id, name = item
            return (
                0 if device_id == default_id else 1,
                0 if self._is_bluetooth_name(name) else 1,
                (name or "").lower(),
            )

        combo.blockSignals(True)
        try:
            combo.clear()
            for device_id, name in sorted(devices, key=sort_key):
                label = labels.get(device_id) or self._short_label(name) or "Unknown device"
                combo.addItem(f"{self._device_icon_for(name)}  {label}", device_id)
                index = combo.count() - 1
                combo.setItemData(index, name, Qt.ItemDataRole.ToolTipRole)

            target = default_id
            if target:
                index = combo.findData(target)
                if index < 0:
                    # Endpoint ids occasionally differ in case between the
                    # default lookup and the enumeration - fall back to a
                    # case-insensitive match so the combo still selects it.
                    lowered = str(target).lower()
                    for candidate in range(combo.count()):
                        data = combo.itemData(candidate)
                        if data and str(data).lower() == lowered:
                            index = candidate
                            break
                if index >= 0:
                    combo.setCurrentIndex(index)
        finally:
            combo.blockSignals(False)

    def _build_device_row(self, layout):
        try:
            devices = self._audio.get_all_devices()
            if not devices:
                return
            row = QFrame()
            row.setProperty("class", "device-row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)

            icon = QLabel("\uf025")
            icon.setProperty("class", "device-icon")
            icon.setToolTip("Audio output device")
            self._device_icon = icon
            row_layout.addWidget(icon)

            combo = QComboBox()
            combo.setProperty("class", "device-selector")
            combo.view().setTextElideMode(Qt.TextElideMode.ElideRight)
            self._device_combo = combo

            self._populate_device_combo(combo, devices)

            combo.currentIndexChanged.connect(self._on_device_selected)
            row_layout.addWidget(combo, 1)

            refresh = QPushButton("\uf021")
            refresh.setProperty("class", "cc-refresh")
            refresh.setCursor(Qt.CursorShape.PointingHandCursor)
            refresh.setToolTip("Rescan audio devices")
            refresh.clicked.connect(self._refresh_all)
            row_layout.addWidget(refresh)

            layout.addWidget(row)
        except Exception:
            logging.exception("CavaMenuWidget: failed to build device selector")

    def _on_device_selected(self, index: int):
        if self._audio is None:
            return
        try:
            combo = self.sender()
            if not isinstance(combo, QComboBox):
                return
            device_id = combo.itemData(index)
            if not device_id:
                return

            switched = False
            try:
                switched = bool(self._audio.set_default_device(device_id))
            except Exception:
                logging.exception("CavaMenuWidget: failed to set default audio device")

            # Re-resolve the endpoint even if the call reported failure so the
            # slider/icon reflect whatever Windows actually made default.
            self._volume = self._audio.get_volume_interface()
            self._refresh_volume_slider()
            self._update_volume_icon()
            if not switched:
                logging.warning("CavaMenuWidget: Windows rejected audio switch to %s", device_id)
        except Exception:
            logging.exception("CavaMenuWidget: failed to switch audio device")

    def _refresh_all(self):
        if self._audio is not None:
            try:
                # Force the shared cache to drop so we re-enumerate endpoints.
                self._audio._invalidate_cache()
            except Exception:
                pass
        self._reinitialize_audio()

    def _refresh_device_combo(self):
        combo = self._device_combo
        if combo is None or not is_valid_qobject(combo):
            return
        try:
            devices = self._audio.get_all_devices() if self._audio is not None else []
        except Exception:
            devices = []
        if devices:
            self._populate_device_combo(combo, devices)

    def _reinitialize_audio(self):
        """Called by the audio service when the default output device changes."""
        if self._audio is None:
            return
        try:
            self._volume = self._audio.get_volume_interface()
        except Exception:
            logging.exception("CavaMenuWidget: failed to refresh volume interface")
        self._refresh_device_combo()
        self._refresh_volume_slider()
        self._update_volume_icon()

    def _update_label(self):
        """Invoked by the audio service on any system volume change."""
        self._refresh_volume_slider()
        self._update_volume_icon()

    # ------------------------------------------------------------------
    # Volume
    # ------------------------------------------------------------------
    def _build_volume_row(self, layout):
        try:
            row = QFrame()
            row.setProperty("class", "slider-row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)

            icon = QLabel()
            icon.setProperty("class", "slider-icon")
            icon.setCursor(Qt.CursorShape.PointingHandCursor)
            icon.mousePressEvent = lambda event: self._toggle_mute()
            row_layout.addWidget(icon)

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setProperty("class", "slider-control")
            slider.setRange(0, 100)
            try:
                slider.setValue(round(self._volume.GetMasterVolumeLevelScalar() * 100))
            except Exception:
                pass

            value = QLabel()
            value.setProperty("class", "value")
            value.setText(f"{slider.value()}%")

            self._volume_icon = icon
            self._volume_slider = slider
            self._volume_value = value
            self._update_volume_icon()

            slider.valueChanged.connect(lambda v, s=slider, label=value: self._on_volume_changed(v, s, label))
            row_layout.addWidget(slider, 1)
            row_layout.addWidget(value)
            layout.addWidget(row)
        except Exception:
            logging.exception("CavaMenuWidget: failed to build volume slider")

    def _refresh_volume_slider(self):
        slider = self._volume_slider
        if slider is None or not is_valid_qobject(slider):
            return
        vol = self._fresh_volume()
        if vol is None:
            return
        try:
            value = round(vol.GetMasterVolumeLevelScalar() * 100)
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
            if self._volume_value is not None and is_valid_qobject(self._volume_value):
                self._volume_value.setText(f"{value}%")
        except Exception:
            pass

    def _fresh_volume(self):
        """Return the live endpoint-volume interface for the current default
        device. The service lazily re-creates it whenever the default device
        changes, so we must not hold onto the one captured at init."""
        if self._audio is None:
            return None
        try:
            return self._audio.get_volume_interface()
        except Exception:
            logging.exception("CavaMenuWidget: failed to refresh volume interface")
            return self._volume

    def _volume_icon_for(self, value: int, muted: bool) -> str:
        if muted or value <= 0:
            return "\uf026"
        if value <= 50:
            return "\uf027"
        return "\uf028"

    def _update_volume_icon(self):
        try:
            if getattr(self, "_volume_icon", None) is None:
                return
            vol = self._fresh_volume()
            if vol is None:
                return
            muted = bool(vol.GetMute())
            value = round(vol.GetMasterVolumeLevelScalar() * 100)
            self._volume_icon.setText(self._volume_icon_for(value, muted))
        except Exception:
            pass

    def _toggle_mute(self):
        try:
            vol = self._fresh_volume()
            if vol is not None:
                vol.SetMute(not bool(vol.GetMute()), None)
                self._update_volume_icon()
        except Exception:
            logging.exception("CavaMenuWidget: failed to toggle mute")

    def _on_volume_changed(self, value: int, slider: QSlider, label: QLabel):
        try:
            label.setText(f"{value}%")
            vol = self._fresh_volume()
            if vol is not None:
                vol.SetMasterVolumeLevelScalar(value / 100, None)
                if bool(vol.GetMute()) != (value == 0):
                    vol.SetMute(value == 0, None)
                self._update_volume_icon()
        except Exception:
            logging.exception("CavaMenuWidget: failed to set volume")

    # ------------------------------------------------------------------
    # Brightness
    # ------------------------------------------------------------------
    def _build_brightness_rows(self, layout):
        try:
            monitors = self._brightness.get_monitors()
            for hmonitor, _name in monitors:
                row = QFrame()
                row.setProperty("class", "slider-row")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(0)

                icon = QLabel()
                icon.setProperty("class", "slider-icon")
                row_layout.addWidget(icon)

                slider = QSlider(Qt.Orientation.Horizontal)
                slider.setProperty("class", "slider-control")
                slider.setRange(0, 100)
                try:
                    current = self._brightness.get_brightness(hmonitor)
                    if current is not None:
                        slider.setValue(int(current))
                except Exception:
                    pass

                value = QLabel()
                value.setProperty("class", "value")
                value.setText(f"{slider.value()}%")

                icon.setText(self._brightness_icon_for(slider.value()))

                slider.valueChanged.connect(
                    lambda v, h=hmonitor, label=value, ic=icon: self._on_brightness_changed(v, h, label, ic)
                )
                row_layout.addWidget(slider, 1)
                row_layout.addWidget(value)
                layout.addWidget(row)
        except Exception:
            logging.exception("CavaMenuWidget: failed to build brightness slider")

    def _brightness_icon_for(self, value: int) -> str:
        if value <= 25:
            return "\ue32b"
        if value <= 50:
            return "\ue37b"
        if value <= 75:
            return "\ue302"
        return "\ue30d"

    def _on_brightness_changed(self, value: int, hmonitor: int, label: QLabel, icon: QLabel):
        try:
            label.setText(f"{value}%")
            icon.setText(self._brightness_icon_for(value))
            if self._brightness is not None:
                self._brightness.set_brightness(hmonitor, value)
        except Exception:
            logging.exception("CavaMenuWidget: failed to set brightness")

    # ------------------------------------------------------------------
    # Bluetooth
    # ------------------------------------------------------------------
    def _build_bluetooth_button(self, layout):
        """A full-width navigation tile for the Bluetooth submenu, shown just
        below the header so it reads as a primary action rather than an
        afterthought at the bottom of the popup."""
        try:
            bt_button = QPushButton("\uf293  Bluetooth")
            bt_button.setProperty("class", "bt-button")
            bt_button.setCursor(Qt.CursorShape.PointingHandCursor)
            bt_button.setToolTip("Open Bluetooth devices")
            bt_button.clicked.connect(self._open_bluetooth)
            layout.addWidget(bt_button)
        except Exception:
            logging.exception("CavaMenuWidget: failed to build bluetooth button")

    def _open_bluetooth(self):
        try:
            if self._menu is not None and self._menu.isVisible():
                self._menu.hide_animated()
        except Exception:
            pass
        try:
            if self._bt_manager is not None:
                try:
                    if not self._bt_manager.is_radio_on():
                        self._bt_manager.set_radio(True)
                except Exception:
                    logging.exception("CavaMenuWidget: failed to power on bluetooth")
            if self._bt_menu is not None:
                self._bt_menu.show_menu()
        except Exception:
            logging.exception("CavaMenuWidget: failed to open bluetooth menu")
