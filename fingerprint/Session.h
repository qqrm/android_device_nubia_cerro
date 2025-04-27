/*
 * SPDX-FileCopyrightText: 2024 The LineageOS Project
 * SPDX-License-Identifier: Apache-2.0
 */

#pragma once

#include "FingerprintEngine.h"
#include "thread/WorkerThread.h"

#include <aidl/android/hardware/biometrics/fingerprint/BnSession.h>
#include <aidl/android/hardware/biometrics/fingerprint/ISessionCallback.h>

#include <android-base/logging.h>

using ::aidl::android::hardware::biometrics::common::ICancellationSignal;
using ::aidl::android::hardware::biometrics::common::OperationContext;
using ::aidl::android::hardware::biometrics::fingerprint::PointerContext;
using ::aidl::android::hardware::keymaster::HardwareAuthToken;

namespace aidl::android::hardware::biometrics::fingerprint {

void onClientDeath(void* cookie);

class Session : public BnSession {
  public:
    Session(int userId, std::shared_ptr<ISessionCallback> cb, FingerprintEngine* engine,
            WorkerThread* worker);
    ndk::ScopedAStatus generateChallenge() override;
    ndk::ScopedAStatus revokeChallenge(int64_t challenge) override;
    ndk::ScopedAStatus enroll(const HardwareAuthToken& hat,
                              std::shared_ptr<ICancellationSignal>* out) override;
    ndk::ScopedAStatus authenticate(int64_t operationId,
                                    std::shared_ptr<ICancellationSignal>* out) override;
    ndk::ScopedAStatus detectInteraction(std::shared_ptr<ICancellationSignal>* out) override;
    ndk::ScopedAStatus enumerateEnrollments() override;
    ndk::ScopedAStatus removeEnrollments(const std::vector<int32_t>& enrollmentIds) override;
    ndk::ScopedAStatus getAuthenticatorId() override;
    ndk::ScopedAStatus invalidateAuthenticatorId() override;
    ndk::ScopedAStatus resetLockout(const HardwareAuthToken& hat) override;
    ndk::ScopedAStatus close() override;
    ndk::ScopedAStatus onPointerDown(int32_t pointerId, int32_t x, int32_t y, float minor,
                                     float major) override;
    ndk::ScopedAStatus onPointerUp(int32_t pointerId) override;
    ndk::ScopedAStatus onUiReady() override;
    ndk::ScopedAStatus authenticateWithContext(int64_t operationId, const OperationContext& context,
                                               std::shared_ptr<ICancellationSignal>* out) override;
    ndk::ScopedAStatus enrollWithContext(const HardwareAuthToken& hat,
                                         const OperationContext& context,
                                         std::shared_ptr<ICancellationSignal>* out) override;
    ndk::ScopedAStatus detectInteractionWithContext(
            const OperationContext& context, std::shared_ptr<ICancellationSignal>* out) override;
    ndk::ScopedAStatus onPointerDownWithContext(const PointerContext& context) override;
    ndk::ScopedAStatus onPointerUpWithContext(const PointerContext& context) override;
    ndk::ScopedAStatus onContextChanged(const OperationContext& context) override;
    ndk::ScopedAStatus onPointerCancelWithContext(const PointerContext& context) override;
    ndk::ScopedAStatus setIgnoreDisplayTouches(bool shouldIgnore) override;

    binder_status_t linkToDeath(AIBinder* binder);
    bool isClosed();

  private:
    template <typename Func>
    void schedule(Func&& func) {
        LOG(INFO) << __func__;
        bool success = mWorker->schedule(Callable::from(std::forward<Func>(func)));
        if (!success) {
            LOG(ERROR) << "Failed to schedule";
        }
    }

    // The sensor and user ID for which this session was created.
    int32_t mUserId;

    // Callback for talking to the framework. This callback must only be called from non-binder
    // threads to prevent nested binder calls and consequently a binder thread exhaustion.
    // Practically, it means that this callback should always be called from the worker thread.
    std::shared_ptr<ISessionCallback> mCb;

    // Module that communicates to the actual fingerprint hardware, keystore, TEE, etc. In real
    // life such modules typically consume a lot of memory and are slow to initialize. This is here
    // to showcase how such a module can be used within a Session without incurring the high
    // initialization costs every time a Session is constructed.
    FingerprintEngine* mEngine;

    // Worker thread that allows to schedule tasks for asynchronous execution.
    WorkerThread* mWorker;

    // Binder death handler.
    AIBinder_DeathRecipient* mDeathRecipient;

    bool mClosed = false;
};

}  // namespace aidl::android::hardware::biometrics::fingerprint
