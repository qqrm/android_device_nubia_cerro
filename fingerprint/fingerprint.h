/*
 * SPDX-FileCopyrightText: 2025 The LineageOS Project
 * SPDX-License-Identifier: Apache-2.0
 */
#ifndef ANDROID_INCLUDE_HARDWARE_FINGERPRINT_H
#define ANDROID_INCLUDE_HARDWARE_FINGERPRINT_H

#include <hardware/hardware.h>
#include <hardware/hw_auth_token.h>

#define NUM_FINGERS 7

typedef enum fingerprint_msg_type {
    FINGERPRINT_ERROR = -1,
    FINGERPRINT_ACQUIRED = 1,
    FINGERPRINT_TEMPLATE_ENROLLING = 3,
    FINGERPRINT_TEMPLATE_REMOVED = 4,
    FINGERPRINT_AUTHENTICATED = 5,
    FINGERPRINT_TEMPLATE_ENUMERATING = 6,
    FINGERPRINT_GENERATE_CHALLENGE = 7,
    FINGERPRINT_REVOKE_CHALLENGE = 8,
    FINGERPRINT_GET_AUTHENTICATOR_ID = 9,
    FINGERPRINT_INVALIDATE_AUTHENTICATOR_ID = 10,
    GF_FINGERPRINT_BIG_DATA = 1013,
} fingerprint_msg_type_t;

typedef enum fingerprint_error {
    FINGERPRINT_ERROR_HW_UNAVAILABLE = 1,
    FINGERPRINT_ERROR_UNABLE_TO_PROCESS = 2,
    FINGERPRINT_ERROR_TIMEOUT = 3,
    FINGERPRINT_ERROR_NO_SPACE = 4,
    FINGERPRINT_ERROR_CANCELED = 5,
    FINGERPRINT_ERROR_UNABLE_TO_REMOVE = 6,
    FINGERPRINT_ERROR_LOCKOUT = 7,
    FINGERPRINT_ERROR_VENDOR_BASE = 1000
} fingerprint_error_t;

typedef enum fingerprint_acquired_info {
    FINGERPRINT_ACQUIRED_GOOD = 0,
    FINGERPRINT_ACQUIRED_PARTIAL = 1,
    FINGERPRINT_ACQUIRED_INSUFFICIENT = 2,
    FINGERPRINT_ACQUIRED_IMAGER_DIRTY = 3,
    FINGERPRINT_ACQUIRED_TOO_SLOW = 4,
    FINGERPRINT_ACQUIRED_TOO_FAST = 5,
    FINGERPRINT_ACQUIRED_DETECTED = 6,
    FINGERPRINT_ACQUIRED_VENDOR_BASE = 1000
} fingerprint_acquired_info_t;

typedef struct fingerprint_finger_id {
    uint32_t fid;
} fingerprint_finger_id_t;

typedef struct fingerprint_enroll {
    fingerprint_finger_id_t finger;
    uint32_t samples_remaining;
    uint64_t msg;
} fingerprint_enroll_t;

typedef struct fingerprint_iterator {
    fingerprint_finger_id_t fingers[NUM_FINGERS];
} fingerprint_iterator_t;

typedef fingerprint_iterator_t fingerprint_enumerated_t;
typedef fingerprint_iterator_t fingerprint_removed_t;

typedef struct fingerprint_acquired {
    fingerprint_acquired_info_t acquired_info;
} fingerprint_acquired_t;

typedef struct fingerprint_authenticated {
    fingerprint_finger_id_t finger;
    hw_auth_token_t hat;
} fingerprint_authenticated_t;

typedef struct fingerprint_msg {
    fingerprint_msg_type_t type;
    union {
        fingerprint_error_t error;
        fingerprint_enroll_t enroll;
        fingerprint_enumerated_t enumerated;
        fingerprint_removed_t removed;
        fingerprint_acquired_t acquired;
        fingerprint_authenticated_t authenticated;
        uint64_t data;
    } data;
} fingerprint_msg_t;

typedef void (*fingerprint_notify_t)(const fingerprint_msg_t* msg);

typedef struct fingerprint_device {
    struct hw_device_t common;
    fingerprint_notify_t notify;
    int (*set_notify)(struct fingerprint_device* dev, fingerprint_notify_t notify);
    uint64_t (*generateChallenge)(struct fingerprint_device* dev);
    int (*revokeChallenge)(struct fingerprint_device* dev, uint64_t challenge);
    int (*enroll)(struct fingerprint_device* dev, const hw_auth_token_t* hat);
    uint64_t (*getAuthenticatorId)(struct fingerprint_device* dev);
    uint64_t (*invalidateAuthenticatorId)(struct fingerprint_device* dev);
    void (*cancel)(void);
    int (*enumerate)(struct fingerprint_device* dev);
    uint64_t (*remove)(struct fingerprint_device* dev, const int32_t* enrollmentIds, int count);
    void (*setActiveGroup)(struct fingerprint_device* dev, int userId);
    int (*authenticate)(struct fingerprint_device* dev, uint64_t operation_id);
    void* reserved0[2];
    int (*sendCustomizedCommand)(struct fingerprint_device* dev, int command, int extras);
    void* reserved1[2];
} fingerprint_device_t;

typedef struct fingerprint_module {
    struct hw_module_t common;
} fingerprint_module_t;

#endif /* ANDROID_INCLUDE_HARDWARE_FINGERPRINT_H */
