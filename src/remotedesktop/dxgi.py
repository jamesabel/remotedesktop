"""Fast screen capture via DXGI Desktop Duplication (Windows), with ctypes.

QScreen.grabWindow costs ~96 ms per 4K frame (GDI BitBlt + conversion) and
runs on the GUI thread every capture tick, capping the effective frame rate
near 10 fps and delaying everything else on the event loop. The DXGI
duplication API instead hands us the desktop as a GPU texture only when
something actually changed: a grab is a GPU copy plus one CPU memcpy
(~10 ms at 4K), and an unchanged screen costs ~nothing.

`DesktopDuplication.create()` returns None wherever duplication is not
available (non-Windows, no interactive desktop, RDP sessions, rotated
displays) — the caller falls back to grabWindow. `grab()` returns the
current desktop as a QImage; when nothing changed since the last grab it
returns the *same QImage object* (callers can use an identity check to skip
diffing). It returns None when the duplication was lost (secure desktop,
display-mode change) — the caller should close() and re-create() later.

All COM calls are hand-rolled vtable dispatch; only the handful of methods
used here are declared, by their fixed vtable slots.
"""

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtGui import QImage

_log = logging.getLogger("remotedesktop.dxgi")

_UINT = ctypes.c_uint
_HRESULT = ctypes.c_int32  # signed, checked manually (HRESULT restype would raise)


def _signed(code: int) -> int:
    return ctypes.c_int32(code).value


DXGI_ERROR_WAIT_TIMEOUT = _signed(0x887A0027)
DXGI_ERROR_ACCESS_LOST = _signed(0x887A0026)

_D3D_DRIVER_TYPE_HARDWARE = 1
_D3D11_SDK_VERSION = 7
_D3D11_USAGE_STAGING = 3
_D3D11_CPU_ACCESS_READ = 0x20000
_D3D11_MAP_READ = 1
_DXGI_MODE_ROTATION_UNSPECIFIED = 0
_DXGI_MODE_ROTATION_IDENTITY = 1


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def of(cls, d1, d2, d3, *d4):
        return cls(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))


_IID_IDXGIDevice = _GUID.of(0x54EC77FA, 0x1377, 0x44E6, 0x8C, 0x32, 0x88, 0xFD, 0x5F, 0x44, 0xC8, 0x4C)
_IID_IDXGIOutput1 = _GUID.of(0x00CDDEA8, 0x939B, 0x4B83, 0xA3, 0x40, 0xA6, 0x85, 0x22, 0x66, 0x66, 0xCC)
_IID_ID3D11Texture2D = _GUID.of(0x6F15AAF2, 0xD208, 0x4E89, 0x9A, 0xB4, 0x48, 0x95, 0x35, 0xD3, 0x4F, 0x9C)


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", _UINT),
        ("Monitor", ctypes.c_void_p),
    ]


class _DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
    _fields_ = [("Position", wintypes.POINT), ("Visible", wintypes.BOOL)]


class _DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
    _fields_ = [
        ("LastPresentTime", ctypes.c_int64),
        ("LastMouseUpdateTime", ctypes.c_int64),
        ("AccumulatedFrames", _UINT),
        ("RectsCoalesced", wintypes.BOOL),
        ("ProtectedContentMaskedOut", wintypes.BOOL),
        ("PointerPosition", _DXGI_OUTDUPL_POINTER_POSITION),
        ("TotalMetadataBufferSize", _UINT),
        ("PointerShapeBufferSize", _UINT),
    ]


class _DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", _UINT), ("Quality", _UINT)]


class _D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [
        ("Width", _UINT),
        ("Height", _UINT),
        ("MipLevels", _UINT),
        ("ArraySize", _UINT),
        ("Format", _UINT),
        ("SampleDesc", _DXGI_SAMPLE_DESC),
        ("Usage", _UINT),
        ("BindFlags", _UINT),
        ("CPUAccessFlags", _UINT),
        ("MiscFlags", _UINT),
    ]


class _D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", _UINT), ("DepthPitch", _UINT)]


def _method(com_ptr, slot: int, *argtypes, restype=_HRESULT):
    """Bind vtable slot `slot` of a COM interface pointer as a callable."""
    vtable = ctypes.cast(com_ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtable[slot])


def _release(com_ptr) -> None:
    if com_ptr:
        _method(com_ptr, 2, restype=ctypes.c_ulong)(com_ptr)  # IUnknown::Release


def _query_interface(com_ptr, iid: _GUID):
    out = ctypes.c_void_p()
    hr = _method(com_ptr, 0, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p))(
        com_ptr, ctypes.byref(iid), ctypes.byref(out)
    )
    if hr < 0:
        raise OSError(f"QueryInterface failed: {hr:#x}")
    return out


# Vtable slots (fixed by the header declaration order):
# IDXGIObject: 0-2 IUnknown, 3-5 private data, 6 GetParent.
_SLOT_GETPARENT = 6
# IDXGIDevice: 7 GetAdapter.
_SLOT_DEVICE_GETADAPTER = 7
# IDXGIAdapter: 7 EnumOutputs.
_SLOT_ADAPTER_ENUMOUTPUTS = 7
# IDXGIOutput: 7 GetDesc (then 8-18); IDXGIOutput1 adds 19-21, 22 DuplicateOutput.
_SLOT_OUTPUT_GETDESC = 7
_SLOT_OUTPUT1_DUPLICATEOUTPUT = 22
# IDXGIOutputDuplication: 8 AcquireNextFrame, 14 ReleaseFrame.
_SLOT_DUPL_ACQUIRENEXTFRAME = 8
_SLOT_DUPL_RELEASEFRAME = 14
# ID3D11Device: 5 CreateTexture2D.
_SLOT_D3D_CREATETEXTURE2D = 5
# ID3D11DeviceContext (after 0-6 ID3D11DeviceChild): 14 Map, 15 Unmap, 47 CopyResource.
_SLOT_CTX_MAP = 14
_SLOT_CTX_UNMAP = 15
_SLOT_CTX_COPYRESOURCE = 47
# ID3D11Texture2D: 10 GetDesc (0-2 IUnknown, 3-6 DeviceChild, 7-9 Resource).
_SLOT_TEX_GETDESC = 10


class DesktopDuplication:
    """One duplicated output (the primary monitor), grabbed on demand."""

    def __init__(self, device, context, duplication, staging, width: int, height: int) -> None:
        self._device = device
        self._context = context
        self._duplication = duplication
        self._staging = staging
        self._width = width
        self._height = height
        self._last_image: QImage | None = None

    @classmethod
    def create(cls) -> "DesktopDuplication | None":
        if sys.platform != "win32":
            return None
        device = context = dxgi_device = adapter = output = duplication = None
        try:
            d3d11 = ctypes.windll.d3d11
            device = ctypes.c_void_p()
            context = ctypes.c_void_p()
            hr = d3d11.D3D11CreateDevice(
                None, _D3D_DRIVER_TYPE_HARDWARE, None, 0, None, 0,
                _D3D11_SDK_VERSION, ctypes.byref(device), None, ctypes.byref(context),
            )
            if hr < 0:
                raise OSError(f"D3D11CreateDevice failed: {hr:#x}")
            dxgi_device = _query_interface(device, _IID_IDXGIDevice)
            adapter = ctypes.c_void_p()
            hr = _method(dxgi_device, _SLOT_DEVICE_GETADAPTER, ctypes.POINTER(ctypes.c_void_p))(
                dxgi_device, ctypes.byref(adapter)
            )
            if hr < 0:
                raise OSError(f"GetAdapter failed: {hr:#x}")
            output, desc = cls._primary_output(adapter)
            if desc.Rotation not in (_DXGI_MODE_ROTATION_UNSPECIFIED, _DXGI_MODE_ROTATION_IDENTITY):
                raise OSError(f"display rotation {desc.Rotation} not supported")
            output1 = _query_interface(output, _IID_IDXGIOutput1)
            try:
                duplication = ctypes.c_void_p()
                hr = _method(
                    output1, _SLOT_OUTPUT1_DUPLICATEOUTPUT,
                    ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                )(output1, device, ctypes.byref(duplication))
                if hr < 0:
                    raise OSError(f"DuplicateOutput failed: {hr:#x}")
            finally:
                _release(output1)
            width = desc.DesktopCoordinates.right - desc.DesktopCoordinates.left
            height = desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top
            staging = cls._create_staging(device, width, height)
            _log.info("DXGI desktop duplication active (%dx%d)", width, height)
            return cls(device, context, duplication, staging, width, height)
        except OSError as error:
            _log.info("DXGI desktop duplication unavailable (%s) — using grabWindow", error)
            for com_ptr in (duplication, output, adapter, dxgi_device, context, device):
                _release(com_ptr)
            return None

    @staticmethod
    def _primary_output(adapter):
        """The output whose desktop rect starts at (0, 0) — the primary —
        or the first attached output if none does."""
        first = None
        index = 0
        while True:
            output = ctypes.c_void_p()
            hr = _method(
                adapter, _SLOT_ADAPTER_ENUMOUTPUTS, _UINT, ctypes.POINTER(ctypes.c_void_p)
            )(adapter, index, ctypes.byref(output))
            if hr < 0 or not output:
                break
            desc = _DXGI_OUTPUT_DESC()
            hr = _method(output, _SLOT_OUTPUT_GETDESC, ctypes.POINTER(_DXGI_OUTPUT_DESC))(
                output, ctypes.byref(desc)
            )
            if hr >= 0 and desc.AttachedToDesktop:
                if desc.DesktopCoordinates.left == 0 and desc.DesktopCoordinates.top == 0:
                    if first is not None:
                        _release(first[0])
                    return output, desc
                if first is None:
                    first = (output, desc)
                    index += 1
                    continue
            _release(output)
            index += 1
        if first is None:
            raise OSError("no attached DXGI output")
        return first

    @staticmethod
    def _create_staging(device, width: int, height: int):
        desc = _D3D11_TEXTURE2D_DESC()
        desc.Width = width
        desc.Height = height
        desc.MipLevels = 1
        desc.ArraySize = 1
        desc.Format = 87  # DXGI_FORMAT_B8G8R8A8_UNORM: the duplication format
        desc.SampleDesc.Count = 1
        desc.Usage = _D3D11_USAGE_STAGING
        desc.CPUAccessFlags = _D3D11_CPU_ACCESS_READ
        staging = ctypes.c_void_p()
        hr = _method(
            device, _SLOT_D3D_CREATETEXTURE2D,
            ctypes.POINTER(_D3D11_TEXTURE2D_DESC), ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(device, ctypes.byref(desc), None, ctypes.byref(staging))
        if hr < 0:
            raise OSError(f"CreateTexture2D (staging) failed: {hr:#x}")
        return staging

    def grab(self) -> QImage | None:
        """The current desktop; the previous QImage object if unchanged;
        None if the duplication is lost (close() and re-create() later)."""
        frame_info = _DXGI_OUTDUPL_FRAME_INFO()
        resource = ctypes.c_void_p()
        hr = _method(
            self._duplication, _SLOT_DUPL_ACQUIRENEXTFRAME,
            _UINT, ctypes.POINTER(_DXGI_OUTDUPL_FRAME_INFO), ctypes.POINTER(ctypes.c_void_p),
        )(self._duplication, 0, ctypes.byref(frame_info), ctypes.byref(resource))
        if hr == DXGI_ERROR_WAIT_TIMEOUT:
            # Nothing changed since the last acquired frame.
            return self._last_image if self._last_image is not None else None
        if hr < 0:
            if hr != DXGI_ERROR_ACCESS_LOST:
                _log.warning("AcquireNextFrame failed: %#x — dropping DXGI capture", hr & 0xFFFFFFFF)
            return None
        try:
            texture = _query_interface(resource, _IID_ID3D11Texture2D)
            try:
                _method(
                    self._context, _SLOT_CTX_COPYRESOURCE,
                    ctypes.c_void_p, ctypes.c_void_p, restype=None,
                )(self._context, self._staging, texture)
            finally:
                _release(texture)
        except OSError:
            _release(resource)
            _method(self._duplication, _SLOT_DUPL_RELEASEFRAME)(self._duplication)
            return None
        _release(resource)
        _method(self._duplication, _SLOT_DUPL_RELEASEFRAME)(self._duplication)

        mapped = _D3D11_MAPPED_SUBRESOURCE()
        hr = _method(
            self._context, _SLOT_CTX_MAP,
            ctypes.c_void_p, _UINT, _UINT, _UINT, ctypes.POINTER(_D3D11_MAPPED_SUBRESOURCE),
        )(self._context, self._staging, 0, _D3D11_MAP_READ, 0, ctypes.byref(mapped))
        if hr < 0 or not mapped.pData:
            _log.warning("Map (staging) failed: %#x — dropping DXGI capture", hr & 0xFFFFFFFF)
            return None
        try:
            buffer = (ctypes.c_char * (mapped.RowPitch * self._height)).from_address(mapped.pData)
            # .copy() both detaches from the mapped memory and tightens the
            # row stride — the one CPU memcpy of the whole path.
            image = QImage(
                buffer, self._width, self._height, mapped.RowPitch, QImage.Format.Format_RGB32
            ).copy()
        finally:
            _method(self._context, _SLOT_CTX_UNMAP, ctypes.c_void_p, _UINT, restype=None)(
                self._context, self._staging, 0
            )
        self._last_image = image
        return image

    def close(self) -> None:
        for attribute in ("_staging", "_duplication", "_context", "_device"):
            _release(getattr(self, attribute))
            setattr(self, attribute, None)
        self._last_image = None
