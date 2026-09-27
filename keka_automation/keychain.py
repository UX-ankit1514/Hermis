from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_char_p, c_int32, c_uint32, c_void_p


SECURITY = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Security.framework/Security")
CORE_FOUNDATION = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

ERR_SEC_SUCCESS = 0
ERR_SEC_ITEM_NOT_FOUND = -25300

SECURITY.SecKeychainFindGenericPassword.argtypes = [
    c_void_p,
    c_uint32,
    c_char_p,
    c_uint32,
    c_char_p,
    POINTER(c_uint32),
    POINTER(c_void_p),
    POINTER(c_void_p),
]
SECURITY.SecKeychainFindGenericPassword.restype = c_int32
SECURITY.SecKeychainAddGenericPassword.argtypes = [
    c_void_p,
    c_uint32,
    c_char_p,
    c_uint32,
    c_char_p,
    c_uint32,
    c_void_p,
    POINTER(c_void_p),
]
SECURITY.SecKeychainAddGenericPassword.restype = c_int32
SECURITY.SecKeychainItemModifyAttributesAndData.argtypes = [c_void_p, c_void_p, c_uint32, c_void_p]
SECURITY.SecKeychainItemModifyAttributesAndData.restype = c_int32
SECURITY.SecKeychainItemDelete.argtypes = [c_void_p]
SECURITY.SecKeychainItemDelete.restype = c_int32
SECURITY.SecKeychainItemFreeContent.argtypes = [c_void_p, c_void_p]
SECURITY.SecKeychainItemFreeContent.restype = c_int32
CORE_FOUNDATION.CFRelease.argtypes = [c_void_p]
CORE_FOUNDATION.CFRelease.restype = None


class KeychainError(RuntimeError):
    pass


def _encoded(value: str) -> bytes:
    return value.encode("utf-8")


def _find_item(service: str, account: str) -> tuple[int, c_void_p]:
    service_data = _encoded(service)
    account_data = _encoded(account)
    item = c_void_p()
    status = SECURITY.SecKeychainFindGenericPassword(
        None,
        len(service_data),
        service_data,
        len(account_data),
        account_data,
        None,
        None,
        byref(item),
    )
    return int(status), item


def store_secret(service: str, account: str, secret: str) -> None:
    if not secret:
        raise ValueError("Secret must not be empty")
    service_data = _encoded(service)
    account_data = _encoded(account)
    secret_data = bytearray(secret.encode("utf-8"))
    secret_buffer = (ctypes.c_ubyte * len(secret_data)).from_buffer(secret_data)
    item = c_void_p()
    try:
        status, existing = _find_item(service, account)
        if status == ERR_SEC_SUCCESS:
            try:
                result = SECURITY.SecKeychainItemModifyAttributesAndData(
                    existing,
                    None,
                    len(secret_data),
                    ctypes.cast(secret_buffer, c_void_p),
                )
            finally:
                CORE_FOUNDATION.CFRelease(existing)
        elif status == ERR_SEC_ITEM_NOT_FOUND:
            result = SECURITY.SecKeychainAddGenericPassword(
                None,
                len(service_data),
                service_data,
                len(account_data),
                account_data,
                len(secret_data),
                ctypes.cast(secret_buffer, c_void_p),
                byref(item),
            )
            if item:
                CORE_FOUNDATION.CFRelease(item)
        else:
            raise KeychainError(f"Keychain lookup failed with OSStatus {status}")
        if result != ERR_SEC_SUCCESS:
            raise KeychainError(f"Keychain write failed with OSStatus {int(result)}")
    finally:
        ctypes.memset(ctypes.addressof(secret_buffer), 0, len(secret_data))
        for index in range(len(secret_data)):
            secret_data[index] = 0


def read_secret(service: str, account: str) -> str:
    service_data = _encoded(service)
    account_data = _encoded(account)
    length = c_uint32()
    data = c_void_p()
    status = SECURITY.SecKeychainFindGenericPassword(
        None,
        len(service_data),
        service_data,
        len(account_data),
        account_data,
        byref(length),
        byref(data),
        None,
    )
    if status != ERR_SEC_SUCCESS:
        raise KeychainError("Required macOS Keychain item is unavailable")
    try:
        if length.value == 0:
            raise KeychainError("macOS Keychain item is empty")
        return ctypes.string_at(data, length.value).decode("utf-8")
    finally:
        SECURITY.SecKeychainItemFreeContent(None, data)


def delete_secret(service: str, account: str) -> None:
    status, item = _find_item(service, account)
    if status == ERR_SEC_ITEM_NOT_FOUND:
        return
    if status != ERR_SEC_SUCCESS:
        raise KeychainError(f"Keychain lookup failed with OSStatus {status}")
    try:
        result = SECURITY.SecKeychainItemDelete(item)
        if result != ERR_SEC_SUCCESS:
            raise KeychainError(f"Keychain delete failed with OSStatus {int(result)}")
    finally:
        CORE_FOUNDATION.CFRelease(item)
