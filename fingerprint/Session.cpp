/*
 * SPDX-FileCopyrightText: 2024 The LineageOS Project
 * SPDX-License-Identifier: Apache-2.0
 */

#include "Session.h"

#include "util/CancellationSignal.h"

namespace aidl::android::hardware::biometrics::fingerprint {

void onClientDeath(void* cookie) {
    LOG(INFO) << "FingerprintService has died";
    Session* session = static_cast<Session*>(cookie);
    if (session && !session->isClosed()) {
        session->close();
    }
}

Session::Session(int32_t userId, std::shared_ptr<ISessionCallback> cb, FingerprintEngine* engine,
                 WorkerThread* worker)
    : mUserId(userId), mCb(cb), mEngine(engine), mWorker(worker) {
    mDeathRecipient = AIBinder_DeathRecipient_new(onClientDeath);
    mEngine->setSessionCallback(cb.get());
    mEngine->setActiveGroup(mUserId);
}

binder_status_t Session::linkToDeath(AIBinder* binder) {
    return AIBinder_linkToDeath(binder, mDeathRecipient, this);
}

bool Session::isClosed() {
    return mClosed;
}

ndk::ScopedAStatus Session::generateChallenge() {
    LOG(INFO) << __func__;
    schedule([this] { mEngine->generateChallengeImpl(); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::revokeChallenge(int64_t challenge) {
    LOG(INFO) << __func__;
    schedule([this, challenge] { mEngine->revokeChallengeImpl(challenge); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::enroll(const HardwareAuthToken& hat,
                                   std::shared_ptr<ICancellationSignal>* out) {
    LOG(INFO) << __func__;

    std::promise<void> cancellationPromise;
    auto cancFuture = cancellationPromise.get_future();

    schedule([this, hat, cancFuture = std::move(cancFuture)] {
        if (shouldCancel(cancFuture)) {
            mCb->onError(Error::CANCELED, 0 /* vendorCode */);
        } else {
            mEngine->enrollImpl(hat, cancFuture);
        }
    });

    *out = SharedRefBase::make<CancellationSignal>(std::move(cancellationPromise));
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::authenticate(int64_t operationId,
                                         std::shared_ptr<ICancellationSignal>* out) {
    LOG(INFO) << __func__;

    std::promise<void> cancPromise;
    auto cancFuture = cancPromise.get_future();

    schedule([this, operationId, cancFuture = std::move(cancFuture)] {
        if (shouldCancel(cancFuture)) {
            mCb->onError(Error::CANCELED, 0 /* vendorCode */);
        } else {
            mEngine->authenticateImpl(operationId, cancFuture);
        }
    });

    *out = SharedRefBase::make<CancellationSignal>(std::move(cancPromise));
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::detectInteraction(std::shared_ptr<ICancellationSignal>* out) {
    LOG(INFO) << __func__;

    std::promise<void> cancellationPromise;
    auto cancFuture = cancellationPromise.get_future();

    schedule([this, cancFuture = std::move(cancFuture)] {
        if (shouldCancel(cancFuture)) {
            mCb->onError(Error::CANCELED, 0 /* vendorCode */);
        } else {
            mEngine->detectInteractionImpl(cancFuture);
        }
    });

    *out = SharedRefBase::make<CancellationSignal>(std::move(cancellationPromise));
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::enumerateEnrollments() {
    LOG(INFO) << __func__;
    schedule([this] { mEngine->enumerateEnrollmentsImpl(); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::removeEnrollments(const std::vector<int32_t>& enrollmentIds) {
    LOG(INFO) << __func__;
    schedule([this, enrollmentIds] { mEngine->removeEnrollmentsImpl(enrollmentIds); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::getAuthenticatorId() {
    LOG(INFO) << __func__;
    schedule([this] { mEngine->getAuthenticatorIdImpl(); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::invalidateAuthenticatorId() {
    LOG(INFO) << __func__;
    schedule([this] { mEngine->invalidateAuthenticatorIdImpl(); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::resetLockout(const HardwareAuthToken& hat) {
    LOG(INFO) << __func__;
    schedule([this, hat] { mEngine->resetLockoutImpl(hat); });
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::onPointerDown(int32_t pointerId, int32_t x, int32_t y, float minor,
                                          float major) {
    LOG(INFO) << __func__;
    mEngine->onPointerDownImpl(pointerId, x, y, minor, major);
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::onPointerUp(int32_t pointerId) {
    LOG(INFO) << __func__;
    mEngine->onPointerUpImpl(pointerId);
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::onUiReady() {
    LOG(INFO) << __func__;
    mEngine->onUiReadyImpl();
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::authenticateWithContext(
        int64_t operationId, const common::OperationContext& /*context*/,
        std::shared_ptr<common::ICancellationSignal>* out) {
    return authenticate(operationId, out);
}

ndk::ScopedAStatus Session::enrollWithContext(const keymaster::HardwareAuthToken& hat,
                                              const common::OperationContext& /*context*/,
                                              std::shared_ptr<common::ICancellationSignal>* out) {
    return enroll(hat, out);
}

ndk::ScopedAStatus Session::detectInteractionWithContext(
        const common::OperationContext& /*context*/,
        std::shared_ptr<common::ICancellationSignal>* out) {
    return detectInteraction(out);
}

ndk::ScopedAStatus Session::onPointerDownWithContext(const PointerContext& context) {
    return onPointerDown(context.pointerId, context.x, context.y, context.minor, context.major);
}

ndk::ScopedAStatus Session::onPointerUpWithContext(const PointerContext& context) {
    return onPointerUp(context.pointerId);
}

ndk::ScopedAStatus Session::onContextChanged(const common::OperationContext& /*context*/) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::onPointerCancelWithContext(const PointerContext& /*context*/) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::setIgnoreDisplayTouches(bool /*shouldIgnore*/) {
    return ndk::ScopedAStatus::ok();
}

ndk::ScopedAStatus Session::close() {
    LOG(INFO) << __func__;

    mClosed = true;
    mEngine->setSessionCallback(nullptr);
    mCb->onSessionClosed();
    AIBinder_DeathRecipient_delete(mDeathRecipient);

    return ndk::ScopedAStatus::ok();
}

}  // namespace aidl::android::hardware::biometrics::fingerprint
