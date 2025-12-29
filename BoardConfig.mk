#
# SPDX-FileCopyrightText: The LineageOS Project
# SPDX-License-Identifier: Apache-2.0
#

include device/nubia/sm8650-common/BoardConfigCommon.mk

# HIDL
ODM_MANIFEST_FILES += \
    $(DEVICE_PATH)/manifest_odm.xml

# Kernel
TARGET_KERNEL_CONFIG += \
    oem/boards/cerro_diff.config

# Properties
TARGET_VENDOR_PROP += $(DEVICE_PATH)/vendor.prop

# Security patch
BOOT_SECURITY_PATCH := 2025-12-01
VENDOR_SECURITY_PATCH := $(BOOT_SECURITY_PATCH)

include vendor/nubia/cerro/BoardConfigVendor.mk
