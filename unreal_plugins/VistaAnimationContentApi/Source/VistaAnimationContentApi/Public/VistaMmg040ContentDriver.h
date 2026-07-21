#pragma once

#include "CoreMinimal.h"
#include "VistaAnimationContentDriver.h"

enum class EVistaMmg040PinnedAsset : uint8 {
  PawnClass,
  SkeletalMesh,
  Skeleton,
  AnimBlueprint,
  ControlRig,
  IkRig,
  LookAtMontage,
  BraceMontage,
  DragMontage,
  LiftFootMontage,
  PauseMontage,
  FallMontage,
  RecoverMontage,
  PickUpMontage
};

enum class EVistaMmg040RootMotionPolicy : uint8 {
  Required,
  Forbidden,
  NotApplicable
};

struct FVistaMmg040PinnedAssetContract {
  EVistaMmg040PinnedAsset Asset = EVistaMmg040PinnedAsset::PawnClass;
  FString AssetId;
  FString ObjectPath;
  FString ExpectedClass;
  TOptional<EVistaMmg040PinnedAsset> SkeletonAsset;
  TArray<FString> RequiredNotifies;
  EVistaMmg040RootMotionPolicy RootMotionPolicy =
      EVistaMmg040RootMotionPolicy::NotApplicable;
};

/** One exact parameter variant live-verified for the production mmg_040
 * profile. */
struct FVistaMmg040ExactParameterContract {
  TOptional<int32> DurationSec;
  TOptional<int32> DistanceCm;
  TOptional<int32> HeightCm;
  TOptional<FString> Hand;
  TOptional<FString> Foot;
  TOptional<FString> Direction;
};

struct FVistaMmg040PinnedActionContract {
  EVistaAnimationAction Action = EVistaAnimationAction::Pause;
  FString ActionName;
  FString AdapterId;
  FString BridgeActionId;
  EVistaMmg040PinnedAsset ImplementationAsset =
      EVistaMmg040PinnedAsset::PauseMontage;
  TArray<EVistaMmg040PinnedAsset> SupportingAssets;
  FString CompletionSignal;
  int32 TimeoutMs = 0;
  TArray<FString> ActorCapabilities;
  TArray<FString> TargetCapabilities;
  TArray<FString> AnchorKinds;
  FVistaMmg040ExactParameterContract Parameters;
  TArray<FString> LiveChecks;
};

/** Immutable result produced by the operator-owned live UE inspection. */
struct FVistaMmg040VerifiedAssetReceipt {
  EVistaMmg040PinnedAsset Asset = EVistaMmg040PinnedAsset::PawnClass;
  FString ObjectPath;
  FString ObservedClass;
  FString PackageSha256;
  TOptional<EVistaMmg040PinnedAsset> SkeletonAsset;
  TArray<FString> NotifyNames;
  bool bLoaded = false;
  bool bRootMotionEnabled = false;
};

struct FVistaMmg040VerifiedActionReceipt {
  EVistaAnimationAction Action = EVistaAnimationAction::Pause;
  EVistaMmg040PinnedAsset ImplementationAsset =
      EVistaMmg040PinnedAsset::PauseMontage;
  FString CompletionSignal;
  FString BehaviorEvidenceSha256;
  bool bImplementationMatches = false;
  bool bSkeletonMatches = false;
  bool bCompletionSignalObserved = false;
  bool bIkContactVerified = false;
  bool bObjectAttachmentVerified = false;
  bool bRootMotionVerified = false;
  bool bCollisionVerified = false;
  bool bRecoveryAlignmentVerified = false;
  TArray<FString> ObservedLiveChecks;
  FVistaMmg040ExactParameterContract VerifiedParameters;
};

struct FVistaMmg040VerifiedProfileReceipt {
  FString Schema;
  FString SourceContractSha256;
  FString ProfileId;
  FString ProfileRevision;
  FString ContentRevision;
  FString ContentDigest;
  FString VerificationReceiptId;
  FString VerifiedAtUtc;
  FString VerificationStatus;
  FString VerificationOperatorId;
  FString VerificationMethod;
  FString ProjectName;
  FString EngineVersion;
  FString ProjectRevision;
  FString ProjectDescriptorSha256;
  TArray<FString> PassedChecks;
  TArray<FVistaMmg040VerifiedAssetReceipt> Assets;
  TArray<FVistaMmg040VerifiedActionReceipt> Actions;
};

/** Fresh read-only runtime observation; every field is compared to the pin. */
struct FVistaMmg040AssetInspection {
  EVistaMmg040PinnedAsset Asset = EVistaMmg040PinnedAsset::PawnClass;
  FString ObjectPath;
  FString ObservedClass;
  FString PackageSha256;
  TOptional<EVistaMmg040PinnedAsset> SkeletonAsset;
  TArray<FString> NotifyNames;
  bool bLoaded = false;
  bool bRootMotionEnabled = false;
};

/** Backend start result cannot choose or rewrite the driver-owned handle. */
struct FVistaMmg040BackendStartOutput {
  double EngineTimeSec = 0.0;
};

/**
 * Narrow project integration surface. Asset identity is an enum selected by
 * the concrete driver; callers cannot supply an object path, montage, class,
 * function, Python body, console command, or generic action name.
 */
class VISTAANIMATIONCONTENTAPI_API IVistaMmg040ProjectBackend {
public:
  virtual ~IVistaMmg040ProjectBackend() = default;

  virtual bool InspectPinnedAsset(EVistaMmg040PinnedAsset Asset,
                                  FVistaMmg040AssetInspection &Output,
                                  FString &OutSafeErrorCode) = 0;
  virtual bool ResolveActorBinding(const FString &BindingId,
                                   FVistaAnimationRuntimeBinding &Output,
                                   FString &OutSafeErrorCode) = 0;
  virtual bool ResolveTargetBinding(const FString &BindingId,
                                    FVistaAnimationRuntimeBinding &Output,
                                    FString &OutSafeErrorCode) = 0;

  virtual bool Snapshot(EVistaAnimationAction Action,
                        const FString &ActorBindingId,
                        const TOptional<FString> &TargetBindingId,
                        FVistaAnimationSnapshotOutput &Output,
                        FString &OutSafeErrorCode) = 0;

  /**
   * Every Start method receives a process-local driver-generated handle.
   * Returning false MUST be side-effect-free: the backend must atomically undo
   * any montage, IK, root-motion, collision, or target state before returning.
   * Returning true transfers that exact handle to the driver and requires a
   * finite engine time; invalid postconditions trigger RollbackFailedStart.
   */
  virtual bool StartLookAt(const FString &ActorBindingId,
                           const FString &TargetBindingId,
                           const FString &ActionHandle,
                           const FVistaAnimationActionParameters &Parameters,
                           FVistaMmg040BackendStartOutput &Output,
                           FString &OutSafeErrorCode) = 0;
  virtual bool StartPickUp(const FString &ActorBindingId,
                           const FString &TargetBindingId,
                           const FString &ActionHandle,
                           const FVistaAnimationActionParameters &Parameters,
                           FVistaMmg040BackendStartOutput &Output,
                           FString &OutSafeErrorCode) = 0;
  virtual bool StartBrace(const FString &ActorBindingId,
                          const FString &TargetBindingId,
                          const FString &ActionHandle,
                          const FVistaAnimationActionParameters &Parameters,
                          FVistaMmg040BackendStartOutput &Output,
                          FString &OutSafeErrorCode) = 0;
  virtual bool StartDrag(const FString &ActorBindingId,
                         const FString &TargetBindingId,
                         const FString &ActionHandle,
                         const FVistaAnimationActionParameters &Parameters,
                         FVistaMmg040BackendStartOutput &Output,
                         FString &OutSafeErrorCode) = 0;
  virtual bool StartLiftFoot(const FString &ActorBindingId,
                             const FString &TargetBindingId,
                             const FString &ActionHandle,
                             const FVistaAnimationActionParameters &Parameters,
                             FVistaMmg040BackendStartOutput &Output,
                             FString &OutSafeErrorCode) = 0;
  virtual bool StartPause(const FString &ActorBindingId,
                          const TOptional<FString> &TargetBindingId,
                          const FString &ActionHandle,
                          const FVistaAnimationActionParameters &Parameters,
                          FVistaMmg040BackendStartOutput &Output,
                          FString &OutSafeErrorCode) = 0;
  virtual bool StartFall(const FString &ActorBindingId,
                         const FString &ActionHandle,
                         const FVistaAnimationActionParameters &Parameters,
                         FVistaMmg040BackendStartOutput &Output,
                         FString &OutSafeErrorCode) = 0;
  virtual bool StartRecover(const FString &ActorBindingId,
                            const FString &ActionHandle,
                            const FVistaAnimationActionParameters &Parameters,
                            FVistaMmg040BackendStartOutput &Output,
                            FString &OutSafeErrorCode) = 0;

  /**
   * Compensate a successful start whose postconditions were invalid. Returning
   * true proves the exact handle has no remaining montage/IK/root-motion state.
   */
  virtual bool RollbackFailedStart(EVistaAnimationAction Action,
                                   const FString &ActionHandle,
                                   FString &OutSafeErrorCode) = 0;

  virtual bool Wait(const FString &ActionHandle,
                    FVistaAnimationWaitOutput &Output,
                    FString &OutSafeErrorCode) = 0;
  virtual bool Stop(const FString &ActionHandle, const FString &Reason,
                    FVistaAnimationStopOutput &Output,
                    FString &OutSafeErrorCode) = 0;
  virtual bool Release(const FString &ActionHandle,
                       FVistaAnimationReleaseOutput &Output,
                       FString &OutSafeErrorCode) = 0;
  virtual bool Restore(const FString &SnapshotId, const FString &StateDigest,
                       FVistaAnimationRestoreOutput &Output,
                       FString &OutSafeErrorCode) = 0;
  virtual bool CaptureEvidence(const FVistaAnimationEvidenceCaptureInput &Input,
                               FVistaAnimationEvidenceCaptureOutput &Output,
                               FString &OutSafeErrorCode) = 0;
};

/**
 * Concrete mmg_040 policy/content driver. It cannot be constructed without an
 * exact inspection receipt for every pinned asset and all eight fixed actions.
 */
class VISTAANIMATIONCONTENTAPI_API FVistaMmg040ContentDriver final
    : public IVistaAnimationContentDriver {
public:
  static TSharedPtr<FVistaMmg040ContentDriver, ESPMode::ThreadSafe>
  Create(const FVistaMmg040VerifiedProfileReceipt &Receipt,
         TSharedRef<IVistaMmg040ProjectBackend, ESPMode::ThreadSafe> Backend,
         FString &OutSafeErrorCode);

  static const FString &SourceContractSha256();
  static const TArray<FVistaMmg040PinnedAssetContract> &PinnedAssets();
  static const TArray<FVistaMmg040PinnedActionContract> &PinnedActions();

  virtual bool ValidateTrustedProfile(
      const FVistaAnimationDriverProfileProof &Proof,
      const TArray<FVistaAnimationDriverTrustedAction> &Actions,
      FString &OutSafeErrorCode) const override;
  virtual bool Preflight(const FVistaAnimationPreflightInput &Input,
                         FVistaAnimationPreflightOutput &Output,
                         FString &OutSafeErrorCode) override;
  virtual bool Snapshot(EVistaAnimationAction Action,
                        const FString &ActorBindingId,
                        const TOptional<FString> &TargetBindingId,
                        FVistaAnimationSnapshotOutput &Output,
                        FString &OutSafeErrorCode) override;
  virtual bool Start(EVistaAnimationAction Action,
                     const FString &ActorBindingId,
                     const TOptional<FString> &TargetBindingId,
                     const FVistaAnimationActionParameters &Parameters,
                     FVistaAnimationStartOutput &Output,
                     FString &OutSafeErrorCode) override;
  virtual bool Wait(const FString &ActionHandle,
                    FVistaAnimationWaitOutput &Output,
                    FString &OutSafeErrorCode) override;
  virtual bool Stop(const FString &ActionHandle, const FString &Reason,
                    FVistaAnimationStopOutput &Output,
                    FString &OutSafeErrorCode) override;
  virtual bool Release(const FString &ActionHandle,
                       FVistaAnimationReleaseOutput &Output,
                       FString &OutSafeErrorCode) override;
  virtual bool Restore(const FString &SnapshotId, const FString &StateDigest,
                       FVistaAnimationRestoreOutput &Output,
                       FString &OutSafeErrorCode) override;
  virtual bool CaptureEvidence(const FVistaAnimationEvidenceCaptureInput &Input,
                               FVistaAnimationEvidenceCaptureOutput &Output,
                               FString &OutSafeErrorCode) override;

private:
  FVistaMmg040ContentDriver(
      const FVistaMmg040VerifiedProfileReceipt &InReceipt,
      TSharedRef<IVistaMmg040ProjectBackend, ESPMode::ThreadSafe> InBackend);

  static bool
  ValidateReceipt(const FVistaMmg040VerifiedProfileReceipt &Candidate,
                  FString &OutSafeErrorCode);
  bool IsPreflightBindingAllowed(EVistaAnimationAction Action,
                                 const FString &ActorBindingId,
                                 const TOptional<FString> &TargetBindingId,
                                 FString &OutSafeErrorCode) const;

  FVistaMmg040VerifiedProfileReceipt Receipt;
  TSharedRef<IVistaMmg040ProjectBackend, ESPMode::ThreadSafe> Backend;
  mutable FCriticalSection StateMutex;
  bool bPreflightReady = false;
  bool bMutationQuarantined = false;
  TSet<EVistaAnimationAction> ReadyActions;
  TSet<FString> ReadyActors;
  TSet<FString> ReadyTargets;
  TSet<FString> ReadyActionTargetPairs;
  FString HandleNamespace;
  uint64 NextHandleSequence = 1;
  TMap<FString, EVistaAnimationAction> ActiveHandles;
};
